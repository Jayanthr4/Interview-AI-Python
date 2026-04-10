"""
Storage Service — all S3 operations.

Files stored per session under: interviews/{safe_email}/{session_id}/
  video.webm       — browser uploads directly via pre-signed PUT
  transcript.txt   — Flask uploads server-side
  report.json      — Flask uploads server-side

The pre-signed URL uses virtual-hosted style for regions that require it,
with a fallback to path-style. CORS must be set on the bucket for video upload.
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


def _safe_email(email: str) -> str:
    return email.replace("@", "_at_").replace(".", "_").replace("+", "_")


def _key(email: str, session_id: str, filename: str) -> str:
    """Clean S3 key — no # or special chars that cause URL issues."""
    return f"interviews/{_safe_email(email)}/{session_id}/{filename}"


def _public_url(bucket: str, region: str, key: str) -> str:
    """Virtual-hosted URL — works for all AWS regions."""
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def s3_configured() -> bool:
    return bool(os.getenv("AWS_S3_BUCKET"))


# ── Pre-signed upload URL (browser → S3 directly) ────────────────────────────

def generate_video_upload_url(email: str, session_id: str) -> dict:
    """
    Generates a pre-signed S3 PUT URL the browser uses to upload video.
    ExpiresIn=7200 (2h) to handle large files on slow connections.
    Returns: { uploadUrl, publicUrl, key }

    Uses SigV4 signing which is required for many regions and avoids
    the SignatureDoesNotMatch error when Content-Type is included.
    """
    bucket = _bucket()
    region = _region()
    key = _key(email, session_id, "video.webm")

    # Use SigV4 explicitly — required for non-us-east-1 regions and fixes
    # most "SignatureDoesNotMatch" errors with browser PUT uploads
    from botocore.config import Config as BotocoreConfig
    s3_client = boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        config=BotocoreConfig(signature_version="s3v4"),
    )

    try:
        url = s3_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": bucket,
                "Key": key,
                "ContentType": "video/webm",
            },
            ExpiresIn=7200,
        )
        public_url = _public_url(bucket, region, key)
        print(f"[S3] Pre-signed PUT URL generated for key: {key}")
        return {
            "uploadUrl": url,
            "publicUrl": public_url,
            "key": key,
        }
    except ClientError as e:
        msg = e.response["Error"].get("Message", str(e))
        raise RuntimeError(f"S3 presign failed: {msg}") from e


# ── Server-side uploads ───────────────────────────────────────────────────────

def upload_transcript(email: str, session_id: str, transcript: str) -> str:
    """Uploads transcript as UTF-8 text. Returns public URL."""
    bucket = _bucket()
    region = _region()
    key = _key(email, session_id, "transcript.txt")
    _s3().put_object(
        Bucket=bucket,
        Key=key,
        Body=transcript.encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )
    return _public_url(bucket, region, key)


def upload_report(email: str, session_id: str, report: dict) -> str:
    """Uploads AI report as formatted JSON. Returns public URL."""
    bucket = _bucket()
    region = _region()
    key = _key(email, session_id, "report.json")
    _s3().put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(report, indent=2, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )
    return _public_url(bucket, region, key)


# ── Diagnostics ───────────────────────────────────────────────────────────────

def check_bucket_cors() -> list:
    try:
        return _s3().get_bucket_cors(Bucket=_bucket()).get("CORSRules", [])
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchCORSConfiguration":
            return []
        raise


def apply_bucket_cors(allowed_origins: list[str]) -> bool:
    """
    Applies the minimal CORS policy needed for browser video uploads.
    Call once from CLI: python -c "from services.storage_service import apply_bucket_cors; apply_bucket_cors(['http://localhost:5000'])"
    """
    cors_config = {
        "CORSRules": [
            {
                "AllowedHeaders": ["*"],
                "AllowedMethods": ["GET", "PUT", "POST"],
                "AllowedOrigins": allowed_origins,
                "ExposeHeaders": ["ETag"],
                "MaxAgeSeconds": 3600,
            }
        ]
    }
    try:
        _s3().put_bucket_cors(Bucket=_bucket(), CORSConfiguration=cors_config)
        return True
    except ClientError as e:
        print(f"CORS apply failed: {e}")
        return False
