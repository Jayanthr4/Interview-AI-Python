"""
DynamoDB Service

DynamoDB schema (PK + SK table):
  PK  = USER#{email}
  SK  = INTERVIEW#{session_id}

Fields stored per session:
  email, company, role, otp, status, questions  — set at creation
  transcript      — full transcript text          (set at completion)
  transcriptUrl   — S3 URL for transcript.txt     (set at completion)
  report          — JSON string of AI report      (set at completion)
  reportUrl       — S3 URL for report.json        (set at completion)
  videoUrl        — S3 URL for video.webm         (set at completion)
  completed_date  — ISO timestamp                 (set at completion)
  created_date    — ISO timestamp                 (set at creation)
"""

import os
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError


def _get_table():
    table_name = os.getenv("DYNAMODB_TABLE")
    if not table_name:
        raise ValueError(
            "DYNAMODB_TABLE is not set. Copy .env.example to .env and fill in your values."
        )
    dynamodb = boto3.resource(
        "dynamodb",
        region_name=os.getenv("AWS_REGION", "us-east-1"),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    )
    return dynamodb.Table(table_name)


# ── Create ────────────────────────────────────────────────────────────────────

def create_interview_session(data: dict) -> dict:
    """
    Creates a new interview session record in DynamoDB.
    data: { email, company, role, questions, otp }
    Returns: { sessionId, otp }
    """
    session_id = f"SESSION_{uuid.uuid4().hex[:16].upper()}"
    table = _get_table()

    item = {
        "PK": f"USER#{data['email']}",
        "SK": f"INTERVIEW#{session_id}",
        "email": data["email"],
        "company": data["company"],
        "role": data["role"],
        "status": "QUESTIONS_READY",
        "otp": data["otp"],
        "questions": json.dumps(data["questions"]),
        "created_date": datetime.now(timezone.utc).isoformat(),
    }

    try:
        table.put_item(Item=item)
        return {"sessionId": session_id, "otp": data["otp"]}
    except ClientError as e:
        print(f"DynamoDB create error: {e.response['Error']}")
        raise


# ── Read — by email + OTP ─────────────────────────────────────────────────────

def get_session_by_email_otp(email: str, otp: str) -> Optional[dict]:
    """
    Finds a session by email + OTP. Checks 24-hour expiry.
    Returns the full session dict, {"expired": True}, or None.
    """
    table = _get_table()
    try:
        response = table.query(
            KeyConditionExpression=Key("PK").eq(f"USER#{email}")
            & Key("SK").begins_with("INTERVIEW#")
        )
        items = response.get("Items", [])
        item = next((i for i in items if i.get("otp") == otp), None)
        if not item:
            return None

        return _normalize(item, check_expiry=True)

    except ClientError as e:
        print(f"DynamoDB query error: {e.response['Error']}")
        return None


# ── Read — by session ID (for report page) ────────────────────────────────────

def get_session_by_id(email: str, session_id: str) -> Optional[dict]:
    """Fetches a single full session record for the report page."""
    table = _get_table()
    try:
        response = table.get_item(
            Key={"PK": f"USER#{email}", "SK": f"INTERVIEW#{session_id}"}
        )
        item = response.get("Item")
        if not item:
            return None
        return _normalize(item, check_expiry=False)
    except ClientError as e:
        print(f"DynamoDB get_item error: {e.response['Error']}")
        return None


# ── Atomic completion save ────────────────────────────────────────────────────

def save_completed_session(
    email: str,
    session_id: str,
    transcript: str,
    report: dict,
    video_url: Optional[str] = None,
    transcript_url: Optional[str] = None,
    report_url: Optional[str] = None,
) -> None:
    """
    Single atomic DynamoDB UpdateItem that writes ALL completion data at once:
      - transcript text (inline)
      - transcript S3 URL
      - report JSON (inline)
      - report S3 URL
      - video S3 URL
      - status = SCORED
      - completed_date

    Using one call prevents partial saves and race conditions.
    """
    table = _get_table()

    # Build update expression dynamically so optional fields don't write null
    set_parts = [
        "#s = :status",
        "#r = :report",
        "transcript = :transcript",
        "completed_date = :completed_date",
    ]
    names = {"#s": "status", "#r": "report"}
    values = {
        ":status": "SCORED",
        ":report": json.dumps(report),
        ":transcript": transcript,
        ":completed_date": datetime.now(timezone.utc).isoformat(),
    }

    if video_url:
        set_parts.append("videoUrl = :videoUrl")
        values[":videoUrl"] = video_url

    if transcript_url:
        set_parts.append("transcriptUrl = :transcriptUrl")
        values[":transcriptUrl"] = transcript_url

    if report_url:
        set_parts.append("reportUrl = :reportUrl")
        values[":reportUrl"] = report_url

    try:
        table.update_item(
            Key={"PK": f"USER#{email}", "SK": f"INTERVIEW#{session_id}"},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        print(f"✅ DynamoDB saved: session={session_id} video={'yes' if video_url else 'no'} "
              f"transcript={'yes' if transcript_url else 'no'} report={'yes' if report_url else 'no'}")
    except ClientError as e:
        print(f"DynamoDB save_completed error: {e.response['Error']}")
        raise


# ── Legacy helpers (kept for backward compat) ─────────────────────────────────

def update_session_video_url(email: str, session_id: str, video_url: str) -> None:
    """Updates only the videoUrl field. Prefer save_completed_session instead."""
    table = _get_table()
    try:
        table.update_item(
            Key={"PK": f"USER#{email}", "SK": f"INTERVIEW#{session_id}"},
            UpdateExpression="SET videoUrl = :url",
            ExpressionAttributeValues={":url": video_url},
        )
    except ClientError as e:
        print(f"DynamoDB update_video error: {e.response['Error']}")
        raise


def update_session_report(email: str, session_id: str, report: dict, transcript: str) -> None:
    """Legacy: use save_completed_session for new code."""
    save_completed_session(email, session_id, transcript, report)


# ── Admin — list all sessions ─────────────────────────────────────────────────

def list_all_sessions() -> list[dict]:
    """Scans the table and returns all INTERVIEW sessions, newest first."""
    table = _get_table()
    try:
        response = table.scan(
            FilterExpression=Key("SK").begins_with("INTERVIEW#")
        )
        sessions = [_normalize(item) for item in response.get("Items", [])]
        sessions.sort(key=lambda s: s.get("createdAt", 0), reverse=True)
        return sessions
    except ClientError as e:
        print(f"DynamoDB scan error: {e.response['Error']}")
        return []


# ── Internal normalizer ───────────────────────────────────────────────────────

def _normalize(item: dict, check_expiry: bool = False) -> Optional[dict]:
    """Converts a raw DynamoDB item into a clean session dict."""
    raw_sk = item.get("SK", "")
    session_id = raw_sk.split("#", 1)[1] if "#" in raw_sk else raw_sk

    questions_raw = item.get("questions", "[]")
    questions = json.loads(questions_raw) if isinstance(questions_raw, str) else questions_raw

    report_raw = item.get("report")
    report = json.loads(report_raw) if isinstance(report_raw, str) and report_raw else None

    created_raw = item.get("created_date", "")
    try:
        created_at = int(datetime.fromisoformat(created_raw).timestamp() * 1000)
    except (ValueError, TypeError):
        created_at = 0

    # OTP expiry: 24 hours
    if check_expiry and created_at:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if (now_ms - created_at) > 24 * 60 * 60 * 1000:
            return {"expired": True}

    return {
        "sessionId": session_id,
        "id": session_id,
        "otp": item.get("otp", ""),
        "candidateEmail": item.get("email", ""),
        "companyName": item.get("company", ""),
        "jobTitle": item.get("role", ""),
        "status": item.get("status", ""),
        "questions": questions,
        "createdAt": created_at,
        # S3 URLs
        "videoUrl": item.get("videoUrl"),
        "transcriptUrl": item.get("transcriptUrl"),
        "reportUrl": item.get("reportUrl"),
        # Inline data
        "transcript": item.get("transcript", ""),
        "report": report,
    }
