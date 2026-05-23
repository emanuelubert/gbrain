# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-deepen/_shared/llm_prose.py -->
# Contract:   LLM prose composition via LOCAL LM Studio :1234 (qwen3.6-35b).
#             Direct API call (bypasses ~/.hermes/scripts/llm_call_by_role.sh
#             because that script's sibling-discovery would include external
#             instances reserved for other sessions — e.g., :3 reserved for
#             a separate Hermes session per S191-D119+2). We do our own
#             pool-constrained round-robin via LINKEDIN_DEEPEN_LMS_POOL
#             (default: qwen3.6-35b-a3b-ud-mlx + qwen3.6-35b-a3b-ud-mlx:2).
# Inputs:     prompt string, optional max_tokens / temperature.
# Outputs:    composed paragraph (str) + provenance-tag footer.
# Invariants:
#             - NEVER calls Anthropic / external APIs. LOCAL :1234 ONLY.
#             - Every composed paragraph carries provenance tag.
#             - Pool restricted to LINKEDIN_DEEPEN_LMS_POOL env var (CSV) or
#               POOL_DEFAULT below; siblings outside the pool (e.g., :3) are
#               never touched even if `lms ps` shows them.
#             - If LM Studio unreachable / returns empty → returns "";
#               caller falls back to deterministic template.
# Idempotent: subprocess call is non-deterministic (LLM); caller manages
#             idempotency via per-(page, section, row-hash) registry.

"""LLM prose composer (local-only, qwen3.6-35b, pool-constrained).

Direct call to LM Studio :1234 chat-completions; bypasses the shared
llm_call_by_role.sh because we need pool restriction (excludes :3 reserved
for a separate Hermes session).
"""

from __future__ import annotations

import json as _json
import os
import random
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional


LMS_API = "http://localhost:1234/v1/chat/completions"
DEFAULT_ROLE = "enrich_dispatcher_heavy"   # informational; goes in provenance
DEFAULT_MODEL_FOR_PROVENANCE = "qwen3.6-35b"
DEFAULT_TIMEOUT_S = 180

# Linkedin-deepen pool: primary + :2 explicitly. Excludes :3 (separate
# Hermes session per operator S191-D119+2). Overridable via env var
# LINKEDIN_DEEPEN_LMS_POOL=<comma-csv>.
POOL_DEFAULT = (
    "qwen3.6-35b-a3b-ud-mlx",
    "qwen3.6-35b-a3b-ud-mlx:2",
)


class LLMProseError(Exception):
    pass


def _pool() -> tuple:
    raw = os.environ.get("LINKEDIN_DEEPEN_LMS_POOL")
    if raw:
        return tuple(s.strip() for s in raw.split(",") if s.strip())
    return POOL_DEFAULT


def compose_prose(
    prompt: str,
    *,
    role: str = DEFAULT_ROLE,
    max_tokens: int = 500,
    temperature: float = 0.2,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    mock_subprocess=None,
) -> str:
    """Compose a prose paragraph via local LM Studio. Returns the
    paragraph + provenance footer; empty string on any failure.

    mock_subprocess (test-only): callable(prompt, role) → str; bypasses
    HTTP when provided (legacy name kept for test compat).
    """
    if mock_subprocess is not None:
        try:
            text = mock_subprocess(prompt, role)
            return _attach_provenance(text, role) if text else ""
        except Exception:
            return ""

    pool = _pool()
    if not pool:
        return ""
    model = random.choice(pool)

    payload = _json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
    }).encode("utf-8")

    req = urllib.request.Request(
        LMS_API,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError):
        return ""

    try:
        j = _json.loads(body)
        choice = j["choices"][0]["message"]
        # D63.1 fallback: some qwen models put output in reasoning_content
        # when content is empty (Qwen3-Thinking pattern).
        text = (choice.get("content") or "").strip()
        if not text:
            text = (choice.get("reasoning_content") or "").strip()
    except (KeyError, IndexError, ValueError, TypeError):
        return ""
    if not text:
        return ""
    return _attach_provenance(text, role)


def _attach_provenance(text: str, role: str) -> str:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    footer = (
        f"\n\n*[LLM-composed from LinkedIn structured data; "
        f"role={role}; model={DEFAULT_MODEL_FOR_PROVENANCE}; ts={ts}]*"
    )
    return text.rstrip() + footer
