"""
Tiny Google Gemini client (free AI Studio key) for the learning agent.

Only plain-text trade/market data is ever sent: never API keys, passwords or
emails. On the free tier Google may use what we send to improve its models,
which is why nothing private goes in a prompt.
"""

import json
import os
import re
import urllib.error
import urllib.request

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"]


class QuotaExceeded(Exception):
    """Free-tier limit reached (HTTP 429). Try again later."""


class GeminiError(Exception):
    pass


def _models():
    first = os.environ.get("GEMINI_MODEL", "").strip()
    return [m for m in dict.fromkeys([first] + FALLBACK_MODELS) if m]


def _post(model, body, key):
    req = urllib.request.Request(
        API_URL.format(model=model), data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": key}, method="POST")
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read())


def _extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            return json.loads(match.group(0))
        raise GeminiError(f"Gemini did not return JSON: {text[:200]}")


def ask_json(prompt, temperature=0.4):
    """Send one prompt, get a parsed JSON object back. Tries the configured
    model first, then known free models if that one no longer exists."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise GeminiError("GEMINI_API_KEY is not set")
    last_error = None
    for model in _models():
        for json_mode in (True, False):
            body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": temperature}}
            if json_mode:
                body["generationConfig"]["responseMimeType"] = "application/json"
            try:
                data = _post(model, body, key)
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:300]
                if e.code == 429:
                    raise QuotaExceeded(detail)
                if e.code == 400 and json_mode and "responseMimeType" in detail:
                    continue  # this model doesn't take the JSON flag: ask again without it
                if e.code in (400, 404):
                    last_error = f"{model}: HTTP {e.code} {detail}"
                    break  # try the next model
                raise GeminiError(f"{model}: HTTP {e.code} {detail}")
            except urllib.error.URLError as e:
                raise GeminiError(f"network: {e.reason}")
            try:
                parts = data["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts)
            except (KeyError, IndexError):
                raise GeminiError(f"empty answer: {json.dumps(data)[:300]}")
            result = _extract_json(text)
            if isinstance(result, dict):
                result["_model"] = model
            return result
    raise GeminiError(last_error or "no Gemini model answered")
