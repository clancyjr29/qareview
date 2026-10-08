"""Optional LLM judgment layer for QA Review.

Adjudicates flagged rows: confirms real issues, dismisses false positives, and
documents a written rationale when the guidelines are ambiguous. Works with
any OpenAI-compatible chat completions API (OpenAI, Ollama, Groq, OpenRouter,
a local server on Termux, etc.).

Configuration via environment variables:
    QA_LLM_API_KEY    API key (not needed for local endpoints like Ollama)
    QA_LLM_BASE_URL   default: https://api.openai.com/v1
    QA_LLM_MODEL      default: gpt-4o-mini
"""
import json
import os
import re
import urllib.error
import urllib.request

BASE_URL = os.environ.get("QA_LLM_BASE_URL", "https://api.openai.com/v1")
MODEL = os.environ.get("QA_LLM_MODEL", "gpt-4o-mini")
TIMEOUT = int(os.environ.get("QA_LLM_TIMEOUT", "60"))


def available():
    """True if we have a key or a local endpoint that doesn't need one."""
    key = os.environ.get("QA_LLM_API_KEY", "")
    local = "localhost" in BASE_URL or "127.0.0.1" in BASE_URL
    return bool(key or local)


def _post(payload):
    """Send a chat completions request; return the message content string.

    Kept as a separate function so tests can monkeypatch it.
    """
    key = os.environ.get("QA_LLM_API_KEY", "")
    req = urllib.request.Request(
        BASE_URL.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        body = json.loads(resp.read().decode())
    return body["choices"][0]["message"]["content"]


def _extract_json_array(text):
    """Pull the JSON array out of a model response, tolerating code fences."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON array in LLM response")
    return json.loads(text[start:end + 1])


def _rule_summary(rubric):
    lines = []
    for r in rubric.get("rules", []):
        lines.append(f"- {r.get('id', '?')} ({r.get('type')}, severity "
                     f"{r.get('severity', 'medium')}): {json.dumps(r, default=str)}")
    return "\n".join(lines)


def adjudicate(flagged_rows, rubric, batch_size=8, max_rows=50):
    """Ask the LLM to judge flagged rows.

    flagged_rows: list of {"row_num", "row", "issues"} dicts from the engine.
    Returns {row_num: {"verdict": "confirmed"|"dismissed"|"unclear",
                       "rationale": str, "resolution": str}}.
    """
    results = {}
    todo = flagged_rows[:max_rows]
    custom = (rubric.get("ai") or {}).get("instructions", "")

    system = (
        "You are a meticulous senior data QA reviewer. You review flagged "
        "data rows and judge whether each flag is a genuine problem or a "
        "false positive. Where the guidelines are ambiguous or unclear, you "
        "do not guess silently: you exercise sound judgment, state the "
        "interpretation you applied, and document your rationale. Reply "
        "with ONLY a JSON array, no prose, no code fences.\n"
        "Each element: {\"row_num\": <int>, \"verdict\": \"confirmed\" | "
        "\"dismissed\" | \"unclear\", \"rationale\": \"<why, in 1-3 "
        "sentences, citing the interpretation applied>\", \"resolution\": "
        "\"<one concrete suggested fix, or '' if none needed>\"}.\n"
        "Use \"confirmed\" when the flag is a genuine deviation, "
        "\"dismissed\" when the flag is a false positive, \"unclear\" only "
        "when the guidelines genuinely do not cover the case.")
    if custom:
        system += "\n\nAdditional reviewer instructions: " + custom

    for i in range(0, len(todo), batch_size):
        chunk = todo[i:i + batch_size]
        user_rows = [
            {"row_num": r["row_num"], "data": r["row"],
             "flags": [{"rule_id": x.rule_id, "severity": x.severity,
                        "message": x.message}
                       for x in r["issues"]]}
            for r in chunk]
        payload = {
            "model": MODEL,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",
                 "content": "Guidelines (rubric):\n" + _rule_summary(rubric) +
                            "\n\nFlagged rows to judge:\n" +
                            json.dumps(user_rows, default=str)}]}
        try:
            content = _post(payload)
            for item in _extract_json_array(content):
                num = item.get("row_num")
                if num in {r["row_num"] for r in chunk} and item.get("verdict") in (
                        "confirmed", "dismissed", "unclear"):
                    results[num] = {
                        "verdict": item["verdict"],
                        "rationale": str(item.get("rationale", ""))[:2000],
                        "resolution": str(item.get("resolution", ""))[:1000]}
        except (urllib.error.URLError, ValueError, KeyError, json.JSONDecodeError):
            continue  # a failed batch is non-fatal; rows keep rule verdicts
    return results


def suggest_fixes(flagged_rows, rubric, batch_size=8, max_rows=50):
    """Ask the LLM for corrected values for flagged fields.

    flagged_rows: [{"row_num", "row", "flagged_fields": [..]}, ...]
    Returns {row_num: {field: corrected_value}}.
    """
    results = {}
    todo = flagged_rows[:max_rows]
    system = (
        "You are a data-repair specialist. For each flagged row, propose "
        "corrected values for the flagged fields ONLY. Make the minimal "
        "change that resolves the flag; preserve the original type and "
        "formatting style. Do not invent substantive content when it "
        "cannot be reasonably inferred - omit that field instead. Reply "
        "with ONLY a JSON array, no prose, no code fences. Each element: "
        "{\"row_num\": <int>, \"corrections\": "
        "{\"<field>\": <new value>, ...}}.")
    for i in range(0, len(todo), batch_size):
        chunk = todo[i:i + batch_size]
        payload = {
            "model": MODEL,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",
                 "content": "Rules (rubric):\n" + _rule_summary(rubric) +
                            "\n\nRows to correct:\n" +
                            json.dumps(chunk, default=str)}]}
        try:
            content = _post(payload)
            for item in _extract_json_array(content):
                num = item.get("row_num")
                corr = item.get("corrections")
                if num in {r["row_num"] for r in chunk} \
                        and isinstance(corr, dict):
                    results[num] = {str(k): v for k, v in corr.items()}
        except (urllib.error.URLError, ValueError, KeyError,
                json.JSONDecodeError):
            continue  # non-fatal; unfixed rows keep their values
    return results


def write_narrative(analysis, dataset_name):
    """Executive-summary Markdown for an analysis. '' if unavailable."""
    if not available():
        return ""
    payload = {
        "model": MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system",
             "content": (
                 "You are a senior data-quality analyst. Write a concise "
                 "executive summary in Markdown for a non-technical "
                 "reviewer: overall data health, the biggest drivers of "
                 "quality issues, and the 2-3 most valuable next actions. "
                 "Use short paragraphs and bullets. Reference concrete "
                 "numbers from the analysis. No code fences. Max 250 "
                 "words.")},
            {"role": "user",
             "content": "Dataset: " + dataset_name +
                        "\n\nDataset analysis JSON:\n" +
                        json.dumps(analysis, default=str)}]}
    try:
        text = _post(payload).strip()
        # tolerate models that wrap in code fences anyway
        if text.startswith("```"):
            text = text.strip("`").lstrip("markdown").strip()
        return text
    except (urllib.error.URLError, ValueError, KeyError,
            json.JSONDecodeError):
        return ""
