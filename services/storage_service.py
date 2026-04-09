"""
Storage Service — all S3 operations.

What gets stored where:
  S3 bucket/interviews/{email}/{session_id}/
    ├── video.webm       ← uploaded directly by browser via pre-signed PUT URL
    ├── transcript.txt   ← uploaded by Flask server
    └── report.json      ← uploaded by Flask server

DynamoDB stores:
    videoUrl        → S3 URL for video
    transcriptUrl   → S3 URL for transcript text file
    reportUrl       → S3 URL for report JSON file
    transcript      → full transcript text (also stored inline for quick access)
    report          → full report JSON string (also stored inline for quick access)
    status          → "SCORED"
"""

import os
import json
import boto3
from botocore.exceptions import ClientError
from datetime import datetime, timezone


def _s3():
    return boto3.client(
        "s3",
        region_name=os.getenv("AWS_REGION", "us-east-1"),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    )


def _bucket() -> str:
    b = os.getenv("AWS_S3_BUCKET", "")
    if not b:
        raise ValueError("AWS_S3_BUCKET is not set in .env")
    return b


def _region() -> str:
    return os.getenv("AWS_REGION", "us-east-1")


def _key(email: str, session_id: str, filename: str) -> str:
    """
    Build a clean S3 key. Avoids # which causes URL fragment issues in browsers.
    Pattern: interviews/{safe_email}/{session_id}/{filename}
    """
    safe_email = email.replace("@", "_at_").replace(".", "_").replace("+", "_")
    return f"interviews/{safe_email}/{session_id}/{filename}"


def _public_url(bucket: str, region: str, key: str) -> str:
    """Path-style URL — works for all regions and avoids bucket-name DNS issues."""
    return f"https://s3.{region}.amazonaws.com/{bucket}/{key}"


# ── Pre-signed video upload URL (browser → S3) ───────────────────────────────

def generate_video_upload_url(email: str, session_id: str) -> dict:
    """
    Returns a pre-signed PUT URL so the browser can upload the WebM video
    directly to S3 without routing gigabytes through Flask.

    Returns: { uploadUrl, publicUrl, key }
    Raises: RuntimeError on failure
    """
    bucket = _bucket()
    region = _region()
    key = _key(email, session_id, "video.webm")

    try:
        url = _s3().generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": key, "ContentType": "video/webm"},
            ExpiresIn=7200,  # 2 hours — large files take time
        )
        return {
            "uploadUrl": url,
            "publicUrl": _public_url(bucket, region, key),
            "key": key,
        }
    except ClientError as e:
        raise RuntimeError(f"S3 presign failed: {e.response['Error']['Message']}") from e


# ── Server-side uploads ───────────────────────────────────────────────────────

def upload_transcript(email: str, session_id: str, transcript: str) -> str:
    """
    Uploads the full interview transcript as a .txt file to S3.
    Returns the public URL.
    """
    bucket = _bucket()
    region = _region()
    key = _key(email, session_id, "transcript.txt")

    try:
        _s3().put_object(
            Bucket=bucket,
            Key=key,
            Body=transcript.encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
            Metadata={
                "session-id": session_id,
                "candidate-email": email,
            },
        )
        return _public_url(bucket, region, key)
    except ClientError as e:
        raise RuntimeError(f"S3 transcript upload failed: {e.response['Error']['Message']}") from e


def upload_report(email: str, session_id: str, report: dict) -> str:
    """
    Uploads the AI-generated report as a formatted .json file to S3.
    Returns the public URL.
    """
    bucket = _bucket()
    region = _region()
    key = _key(email, session_id, "report.json")

    try:
        _s3().put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(report, indent=2, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json; charset=utf-8",
            Metadata={
                "session-id": session_id,
                "candidate-email": email,
            },
        )
        return _public_url(bucket, region, key)
    except ClientError as e:
        raise RuntimeError(f"S3 report upload failed: {e.response['Error']['Message']}") from e


# ── CORS diagnostic ───────────────────────────────────────────────────────────

def check_bucket_cors() -> list:
    """Returns the bucket's CORS rules. Empty list means CORS is not configured."""
    try:
        resp = _s3().get_bucket_cors(Bucket=_bucket())
        return resp.get("CORSRules", [])
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchCORSConfiguration":
            return []
        raise


def s3_configured() -> bool:
    """Quick check — returns True if AWS_S3_BUCKET is set."""
    return bool(os.getenv("AWS_S3_BUCKET"))
