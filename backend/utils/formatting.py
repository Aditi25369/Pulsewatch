"""
formatting.py
Shared utilities used across agents.
Centralises parse_agent_json so it's not duplicated in every agent file.
"""

from __future__ import annotations

import json
import re


def parse_agent_json(text: str) -> dict:
    """
    Extract a JSON object from an LLM response.
    Handles:
      - Raw JSON
      - JSON wrapped in ```json ... ``` fences
      - JSON embedded in prose (finds first { ... last })
    Falls back to {"summary": text, "confidence": 0.4} on failure.
    """
    if not text:
        return {"summary": "", "confidence": 0.4}

    # Strip markdown fences
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try raw JSON
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # Try finding outermost { }
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start != -1 and end > start:
            return json.loads(text[start:end])
    except json.JSONDecodeError:
        pass

    return {"summary": text[:500], "confidence": 0.4}


def format_duration(seconds: float) -> str:
    """Human-readable duration: 3661 → '1h 1m 1s'"""
    seconds = int(seconds)
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def format_confidence(score: float) -> str:
    """0.91 → '91%'"""
    return f"{round(score * 100)}%"