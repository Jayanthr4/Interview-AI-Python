"""
AI Service — Python equivalent of services/aiService.ts

BUG FIXES vs original:
1. The React version mixed up GEMINI_API_KEY / OPENAI_API_KEY naming in .env.example
   but then called the OpenAI SDK. This version uses OPENAI_API_KEY consistently.
2. The original category mapping was inconsistent — the prompt asked for
   DEEP_TECHNICAL/SCENARIO but the TypeScript type expected 'technical'/'scenario'.
   Fixed: we normalize categories from the AI response.
3. Added a proper timeout via threading instead of Promise.race.
"""

import os
import json
import time
import threading
from typing import Optional
from openai import OpenAI

def _get_client() -> OpenAI:
    """Lazy-initialize the OpenAI client so .env is loaded first."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError(
            "OPENAI_API_KEY is not set. "
            "Copy .env.example to .env and add your key."
        )
    return OpenAI(api_key=api_key)

# Maps whatever the AI returns to valid category strings
CATEGORY_MAP = {
    "deep_technical": "technical",
    "technical": "technical",
    "scenario": "scenario",
    "behavioral": "behavioral",
    "closing": "closing",
    "intro": "intro",
}


def generate_questions(
    job_description: str,
    company_name: str,
    job_title: str,
    timeout_seconds: int = 15,
) -> list[dict]:
    """
    Generates 9 tailored interview questions using the OpenAI API.
    Returns an empty list on failure — the caller handles the error.
    """
    # Truncate JD to save tokens (matches React version)
    shortened_jd = job_description[:1500] if len(job_description) > 1500 else job_description

    messages = [
        {
            "role": "system",
            "content": (
                "You are an experienced technical recruiter. Given a job description "
                "and company context, you generate concise, role-appropriate interview "
                "questions as JSON."
            ),
        },
        {
            "role": "user",
            "content": f"""
Job title: {job_title}
Company: {company_name}

Job description:
{shortened_jd}

Task:
1. Do NOT include any introduction questions like "Tell me about yourself". We already handle that separately.
2. Generate exactly 9 interview questions for this role, divided across these categories:
   - technical: 4 questions
   - scenario: 3 questions
   - behavioral: 2 questions
3. Each question must be 1–2 sentences, no extra explanation.
4. Return ONLY valid JSON in this shape (no prose, no markdown):

{{
  "questions": [
    {{ "category": "technical", "text": "..." }},
    {{ "category": "scenario", "text": "..." }}
  ]
}}
""",
        },
    ]

    result: list[dict] = []
    error_holder: list[Exception] = []

    def _call_api():
        nonlocal result
        try:
            response = _get_client().chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                temperature=0.4,
                max_tokens=800,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            parsed = json.loads(content)

            # Handle wrapped or raw array shapes
            if isinstance(parsed, list):
                questions_array = parsed
            elif "questions" in parsed:
                questions_array = parsed["questions"]
            else:
                # Fallback: grab first list value in the dict
                questions_array = next(
                    (v for v in parsed.values() if isinstance(v, list)), []
                )

            now_ms = int(time.time() * 1000)
            result = [
                {
                    "id": f"q-{now_ms}-{i}",
                    "category": CATEGORY_MAP.get(
                        q.get("category", "").lower(), "technical"
                    ),
                    "text": q.get("text", "Question content unavailable."),
                    "guidance": "",
                    "suggestedTimeMinutes": 3,
                }
                for i, q in enumerate(questions_array)
                if isinstance(q, dict)
            ]
        except Exception as exc:
            error_holder.append(exc)
            print(f"Error generating questions: {exc}")

    thread = threading.Thread(target=_call_api, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)

    if thread.is_alive():
        print(f"generate_questions timed out after {timeout_seconds}s")
        return []

    if error_holder:
        return []

    return result
