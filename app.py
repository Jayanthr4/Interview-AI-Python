"""
InterviewAI Pro — Flask Backend
"""

from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
import os
from dotenv import load_dotenv

load_dotenv()  # Must be before any service imports

from services.ai_service import generate_questions
from services.analysis_service import analyze_interview
from services.storage_service import (
    generate_video_upload_url,
    upload_transcript,
    upload_report,
    s3_configured,
)
from services.dynamodb_service import (
    create_interview_session,
    get_session_by_email_otp,
    get_session_by_id,
    save_completed_session,
    update_session_video_url,
    list_all_sessions,
)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-in-production")
CORS(app)


# ── Page routes ───────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return render_template("home.html")

@app.route("/register")
def register():
    return render_template("register.html")

@app.route("/start")
def start():
    return render_template("start.html")

@app.route("/interview/<session_id>")
def interview(session_id):
    return render_template("interview.html", session_id=session_id)

@app.route("/report/<session_id>")
def report(session_id):
    return render_template("report.html", session_id=session_id)

@app.route("/admin")
def admin():
    return render_template("admin.html")


# ── Session creation ──────────────────────────────────────────────────────────

@app.route("/api/create-session", methods=["POST"])
def api_create_session():
    data = request.get_json()
    required = ["candidateEmail", "companyName", "jobTitle", "jobDescription"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    import random
    otp = str(random.randint(100000, 999999))

    ai_questions = generate_questions(
        job_description=data["jobDescription"],
        company_name=data["companyName"],
        job_title=data["jobTitle"],
    )
    if not ai_questions:
        return jsonify({"error": "Failed to generate questions. Please try again."}), 500

    intro_question = {
        "id": "intro-1",
        "category": "intro",
        "text": "Tell me about yourself in the context of this role.",
        "guidance": (
            "Provide a brief (1-2 minute) overview of your background, "
            "key skills, and why you are a strong fit for this specific position."
        ),
        "suggestedTimeMinutes": 2,
    }
    all_questions = [intro_question] + ai_questions

    try:
        result = create_interview_session({
            "email": data["candidateEmail"],
            "company": data["companyName"],
            "role": data["jobTitle"],
            "questions": all_questions,
            "otp": otp,
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500

    return jsonify({"otp": otp, "sessionId": result["sessionId"]})


# ── OTP verification ──────────────────────────────────────────────────────────

@app.route("/api/verify-session", methods=["POST"])
def api_verify_session():
    data = request.get_json()
    email = data.get("email", "").strip()
    otp = data.get("otp", "").strip()

    if not email or not otp or len(otp) != 6:
        return jsonify({"error": "Valid email and 6-digit OTP are required."}), 400

    try:
        found = get_session_by_email_otp(email, otp)
    except ValueError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500

    if not found:
        return jsonify({"error": "Invalid email or OTP. Please check and try again."}), 404

    if found.get("expired"):
        return jsonify({"error": "This OTP has expired (valid for 24 hours). Please request a new session."}), 410

    return jsonify(found)


# ── Fetch session (for report page / refresh) ─────────────────────────────────

@app.route("/api/session/<session_id>", methods=["GET"])
def api_get_session(session_id):
    email = request.args.get("email", "").strip()
    if not email:
        return jsonify({"error": "email query param is required"}), 400
    try:
        session = get_session_by_id(email, session_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if not session:
        return jsonify({"error": "Session not found"}), 404
    return jsonify(session)


# ── Pre-signed S3 upload URL for video ───────────────────────────────────────

@app.route("/api/presigned-upload-url", methods=["POST"])
def api_presigned_upload_url():
    """
    Returns a pre-signed S3 PUT URL so the browser can upload the WebM
    recording directly to S3 — AWS credentials never leave the server.
    Returns 503 if S3 is not configured, so the browser can skip gracefully.
    """
    data = request.get_json()
    email = data.get("email", "").strip()
    session_id = data.get("sessionId", "").strip()

    if not email or not session_id:
        return jsonify({"error": "email and sessionId are required"}), 400

    if not s3_configured():
        return jsonify({
            "error": "S3 not configured (AWS_S3_BUCKET missing). Video upload skipped.",
            "skipped": True,
        }), 503

    try:
        result = generate_video_upload_url(email, session_id)
        return jsonify(result)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500


# ── Main completion endpoint: analyze + upload transcript/report + save all ───

@app.route("/api/complete-session", methods=["POST"])
def api_complete_session():
    """
    Called once when the interview finishes. Does everything in the right order:

    1. Run AI analysis on the transcript
    2. Upload transcript.txt → S3          (server-side)
    3. Upload report.json   → S3          (server-side)
    4. Save ALL data to DynamoDB in ONE atomic UpdateItem:
         - transcript text (inline)
         - transcript S3 URL
         - report JSON (inline)
         - report S3 URL
         - video S3 URL (passed in from browser after its upload)
         - status = SCORED

    Body: {
        email, sessionId, jobTitle, companyName,
        questions, transcript,
        videoUrl  (optional — null if S3 not configured)
    }
    """
    data = request.get_json()
    email       = data.get("email", "").strip()
    session_id  = data.get("sessionId", "").strip()
    job_title   = data.get("jobTitle", "")
    company     = data.get("companyName", "")
    questions   = data.get("questions", [])
    transcript  = data.get("transcript", "")
    video_url   = data.get("videoUrl")          # may be None if S3 not configured

    if not email or not session_id:
        return jsonify({"error": "email and sessionId are required"}), 400

    results = {
        "analysisOk": False,
        "transcriptS3Ok": False,
        "reportS3Ok": False,
        "dbSaveOk": False,
        "errors": [],
    }

    # ── Step 1: AI analysis ────────────────────────────────────────────────
    print(f"[complete-session] Running AI analysis for {session_id}...")
    report = analyze_interview(
        job_title=job_title,
        company_name=company,
        questions=questions,
        transcript=transcript,
    )
    if not report:
        results["errors"].append("AI analysis failed or timed out")
        # Continue anyway — save what we have
        report = {}
    else:
        results["analysisOk"] = True
        print(f"[complete-session] AI analysis complete")

    # ── Step 2 & 3: Upload transcript + report to S3 (server-side) ────────
    transcript_url = None
    report_url = None

    if s3_configured():
        # Upload transcript
        try:
            transcript_url = upload_transcript(email, session_id, transcript)
            results["transcriptS3Ok"] = True
            print(f"[complete-session] Transcript uploaded → {transcript_url}")
        except Exception as e:
            err = f"Transcript S3 upload failed: {e}"
            results["errors"].append(err)
            print(f"[complete-session] ⚠️  {err}")

        # Upload report JSON
        if report:
            try:
                report_url = upload_report(email, session_id, report)
                results["reportS3Ok"] = True
                print(f"[complete-session] Report uploaded → {report_url}")
            except Exception as e:
                err = f"Report S3 upload failed: {e}"
                results["errors"].append(err)
                print(f"[complete-session] ⚠️  {err}")
    else:
        print("[complete-session] S3 not configured — skipping file uploads")
        results["errors"].append("S3 not configured — transcript and report not uploaded to S3")

    # ── Step 4: Atomic DynamoDB save ──────────────────────────────────────
    try:
        save_completed_session(
            email=email,
            session_id=session_id,
            transcript=transcript,
            report=report,
            video_url=video_url,
            transcript_url=transcript_url,
            report_url=report_url,
        )
        results["dbSaveOk"] = True
        print(f"[complete-session] ✅ DynamoDB updated for {session_id}")
    except Exception as e:
        err = f"DynamoDB save failed: {e}"
        results["errors"].append(err)
        print(f"[complete-session] ❌ {err}")
        return jsonify({"error": err, "results": results}), 500

    return jsonify({
        "success": True,
        "report": report,
        "transcriptUrl": transcript_url,
        "reportUrl": report_url,
        "videoUrl": video_url,
        "results": results,
    })


# ── Legacy endpoint (kept for backward compat) ────────────────────────────────

@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    """Redirects to /api/complete-session for backward compatibility."""
    return api_complete_session()


@app.route("/api/update-video-url", methods=["POST"])
def api_update_video_url():
    """Kept for backward compat — prefer /api/complete-session."""
    data = request.get_json()
    email = data.get("email")
    session_id = data.get("sessionId")
    video_url = data.get("videoUrl")
    if not all([email, session_id, video_url]):
        return jsonify({"error": "email, sessionId, and videoUrl are required."}), 400
    try:
        update_session_video_url(email, session_id, video_url)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"success": True})


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.route("/api/admin/sessions", methods=["GET"])
def api_admin_sessions():
    try:
        sessions = list_all_sessions()
    except ValueError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500
    return jsonify(sessions)


# ── S3 CORS diagnostic ────────────────────────────────────────────────────────

@app.route("/api/check-s3-cors", methods=["GET"])
def api_check_s3_cors():
    """
    Dev helper — visit this URL to see if your S3 bucket has CORS configured.
    Required for browser video uploads to work.
    """
    from services.storage_service import check_bucket_cors
    if not s3_configured():
        return jsonify({"configured": False, "message": "AWS_S3_BUCKET not set"})
    try:
        rules = check_bucket_cors()
        if rules:
            return jsonify({"configured": True, "rules": rules})
        else:
            return jsonify({
                "configured": False,
                "message": "No CORS rules found on bucket — video upload from browser will fail.",
                "fix": "Add CORS policy shown in README to your S3 bucket.",
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug, host="0.0.0.0", port=int(os.getenv("PORT", 5020)))
