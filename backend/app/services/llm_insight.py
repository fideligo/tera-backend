"""Optional, consent-gated LLM commentary alongside the deterministic insight.

**This is not the insight.** `app/services/insight.py`'s rule engine is deterministic, tested and
invariant-covered, and this module never touches its output — it only ever adds one extra field,
`ai_commentary`, that the deterministic response does not have and does not need. Nothing here
runs unless a patient has explicitly consented on this specific check *and* an operator has
configured a key; either being absent returns `None` with no network call.

# Invariant 6 is enforced twice

Once in the system prompt, and once by filtering the model's actual output through
`language.find_forbidden_language` — the same deny-list the rest of the app's copy is tested
against. A hit is **discarded, not edited**: a filter that rewrites clinical language into
compliant-looking clinical language is a second, unreviewed source of exactly the thing invariant
6 exists to prevent. The patient sees "no AI commentary available" instead, and the deterministic
insight is what they read either way.

# What is sent

The rendered deterministic insight only — codes, wording, direction, a magnitude in baseline
standard deviations, the context flags already collected for CTX-01. No mmHg (invariant 1: the
schema this is built from has no pressure field), no raw signal (invariant 2: the deterministic
engine never receives one either), no name, no subject.
"""

from __future__ import annotations

import httpx

from app.config import LlmInsightSettings
from app.logging_config import get_logger
from app.services import language

log = get_logger(__name__)

_SYSTEM_PROMPT = (
    "You are writing one short paragraph of lifestyle context for a home blood-pressure "
    "monitoring app, based on a deterministic result the app already computed and already "
    "showed the patient. Rules, none of them optional:\n"
    "- Never diagnose. Never say the patient has, may have, or is likely to have any condition.\n"
    "- Never advise on medication: never suggest starting, stopping, increasing, decreasing or "
    "changing any dose or treatment.\n"
    "- Never state or imply a blood pressure number. The only place a pressure value comes from "
    "is a cuff the patient uses themselves; you were not given one and must not invent one.\n"
    "- Never reassure ('this is normal', 'nothing to worry about') and never alarm.\n"
    "- If the result says to see a clinician or take a cuff reading, do not soften that.\n"
    "- Write 2-4 plain sentences. No headings, no bullet points, no markdown.\n"
    "- If you are unsure what to say, write about general rest, hydration and monitoring "
    "consistency only — never about the condition itself."
)


def _build_user_prompt(insight: dict, context: dict | None) -> str:
    lines = [
        f"Result category: {insight.get('result_state', 'unknown')}",
        f"Priority action: {insight.get('priority_action_code', 'unknown')}",
        f"Hero wording already shown to the patient: {insight.get('hero', '')}",
    ]
    if context:
        lines.append(f"Context reported for this check: {context}")
    return "\n".join(lines)


def generate_commentary(
    *, insight: dict, context: dict | None, settings: LlmInsightSettings
) -> str | None:
    """Return a guarded paragraph, or `None` if unconfigured, unreachable, or refused by the
    deny-list filter. Never raises — a broken third-party API must not break the check.
    """
    if not settings.enabled:
        return None

    try:
        response = httpx.post(
            settings.api_url,
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": _build_user_prompt(insight, context)},
                ],
                "temperature": 0.3,
                "max_tokens": 220,
            },
            timeout=settings.timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        text = body["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001 - third-party call, any failure degrades to None
        # Metadata only. Never the prompt or the response — both may carry clinical wording, and
        # the logging deny-list scrubs by key name, not by knowing this exception's shape.
        log.warning("llm_insight_call_failed", extra={"exception_type": type(exc).__name__})
        return None

    hits = language.find_forbidden_language(text)
    if hits:
        # Discarded, not edited or truncated around the hit — see the module docstring for why.
        log.warning("llm_insight_discarded", extra={"forbidden_hit_count": len(hits)})
        return None

    return text
