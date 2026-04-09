"""
Analysis Service — scores the interview using the official 14-criterion rubric.

IMPORTANT SCORING RULE:
  If the transcript contains no real spoken content (empty, placeholder text,
  or the candidate just read questions back), all scores must reflect that
  honestly — low scores, not fabricated middle scores.
"""

import os
import json
import threading
from openai import OpenAI


def _get_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is not set.")
    return OpenAI(api_key=api_key)


SYSTEM_PROMPT = """
You are a strict, impartial interview assessor. You score candidates ONLY on what
they actually said in the transcript. You never invent or assume content.

CRITICAL HONESTY RULE:
- If the transcript shows the candidate gave no real answers (e.g., repeated the
  questions back, gave one-word answers, said nothing, or the transcript contains
  only placeholder text like "Candidate answered this question"), you MUST give
  low scores (1-2 out of 5, and 20-40 out of 100). Do NOT fabricate a positive
  assessment. State clearly in the summary that no real answers were detected.

SCORING RUBRIC — score each of the 14 criteria on a 1–5 scale:
  1 = Not demonstrated at all
  2 = Very weak / barely attempted
  3 = Adequate but inconsistent
  4 = Good, mostly meets expectations
  5 = Excellent, fully meets expectations

THE 14 CRITERIA:
  1.  Speaks clearly
  2.  Speaks concisely
  3.  Uses appropriate grammar
  4.  Demonstrates appropriate non-verbal (tone, energy, engagement)
  5.  Speaks enthusiastically
  6.  Speaks confidently
  7.  Uses appropriate pace
  8.  Monitors time remaining (doesn't rush or stall unnaturally)
  9.  Defines decision criteria for scenario questions
  10. Identifies next appropriate step for scenario questions
  11. Evaluates consequences for scenario questions
  12. Makes appropriate recommendation for scenario questions
  13. Asks questions at the end
  14. Gives a clear closing statement

Return ONLY valid JSON — no prose, no markdown fences — in this exact schema:

{
  "overallDimensions": {
    "ability": {
      "label": "Ability",
      "score": <0-100 integer — average of criteria 9-12 × 20>,
      "feedback": "<1-2 sentences, specific to what was said or NOT said>"
    },
    "knowledge": {
      "label": "Knowledge",
      "score": <0-100 integer — based on technical accuracy of answers>,
      "feedback": "<1-2 sentences>"
    },
    "skillset": {
      "label": "Skillset",
      "score": <0-100 integer — based on criteria 1-4 and 7-8>,
      "feedback": "<1-2 sentences>"
    },
    "attitude": {
      "label": "Attitude",
      "score": <0-100 integer — based on criteria 5-6 and 13-14>,
      "feedback": "<1-2 sentences>"
    }
  },
  "technicalCommunication": [
    { "criterion": "Speaks clearly",                              "score": <1-5>, "comment": "<specific evidence from transcript or 'No spoken content detected'>" },
    { "criterion": "Speaks concisely",                           "score": <1-5>, "comment": "..." },
    { "criterion": "Uses appropriate grammar",                   "score": <1-5>, "comment": "..." },
    { "criterion": "Demonstrates appropriate non-verbal",        "score": <1-5>, "comment": "..." },
    { "criterion": "Speaks enthusiastically",                    "score": <1-5>, "comment": "..." },
    { "criterion": "Speaks confidently",                         "score": <1-5>, "comment": "..." },
    { "criterion": "Uses appropriate pace",                      "score": <1-5>, "comment": "..." },
    { "criterion": "Monitors time remaining",                    "score": <1-5>, "comment": "..." },
    { "criterion": "Defines decision criteria (scenario Qs)",    "score": <1-5>, "comment": "..." },
    { "criterion": "Identifies next step (scenario Qs)",         "score": <1-5>, "comment": "..." },
    { "criterion": "Evaluates consequences (scenario Qs)",       "score": <1-5>, "comment": "..." },
    { "criterion": "Makes appropriate recommendation (scenario Qs)", "score": <1-5>, "comment": "..." },
    { "criterion": "Asks questions at the end",                  "score": <1-5>, "comment": "..." },
    { "criterion": "Gives a clear closing statement",            "score": <1-5>, "comment": "..." }
  ],
  "strengths": ["<only list real strengths visible in transcript, or state none if none>"],
  "improvements": ["<specific, actionable improvement>", ...],
  "summary": "<3-5 sentences. If no real content was provided, state that clearly and explain scores reflect absence of answers.>"
}
"""


def analyze_interview(
    job_title: str,
    company_name: str,
    questions: list,
    transcript: str,
    timeout_seconds: int = 45,
) -> dict | None:
    """
    Scores the interview against the 14-criterion rubric.
    Returns None on failure so the caller can handle gracefully.
    """
    questions_text = "\n".join(
        f"  Q{i+1} [{q.get('category','').upper()}]: {q.get('text','')}"
        for i, q in enumerate(questions)
    )

    # Detect obviously empty transcripts to give the AI a strong hint
    empty_signals = [
        "candidate answered this question",
        "no spoken answer detected",
        "(no real answer)",
        "candidate read the question",
    ]
    transcript_lower = (transcript or "").lower()
    is_empty = not transcript or all(s in transcript_lower for s in ["answer:"]) and len(transcript) < 300
    is_placeholder = any(sig in transcript_lower for sig in empty_signals)

    emptiness_note = ""
    if is_empty or is_placeholder:
        emptiness_note = (
            "\n\n⚠️ IMPORTANT: The transcript appears to contain NO real spoken answers. "
            "The candidate likely just clicked through without speaking. "
            "You MUST score all criteria 1 or 2. Do NOT fabricate positive scores."
        )

    user_message = f"""
Job Title: {job_title}
Company: {company_name}

Interview Questions:
{questions_text}

Full Transcript of Candidate Answers:
{transcript or "(No transcript — candidate did not speak or transcription was unavailable.)"}
{emptiness_note}
"""

    result: dict | None = None
    error_holder: list = []

    def _call():
        nonlocal result
        try:
            response = _get_client().chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.1,   # Very low — we want consistent, honest scoring
                max_tokens=2000,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            result = json.loads(content)
        except Exception as exc:
            error_holder.append(exc)
            print(f"Analysis error: {exc}")

    thread = threading.Thread(target=_call, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)

    if thread.is_alive():
        print(f"analyze_interview timed out after {timeout_seconds}s")
        return None
    if error_holder:
        return None
    return result
