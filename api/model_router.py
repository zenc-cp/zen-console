"""
api/model_router.py -- Auto LLM Router for Hermes WebUI

Routes user messages to the best model based on intent classification.
ALL models MUST support tool calling — Hermes requires tools for every interaction.

Rules:
  - Default: MiniMax M2.7 (fast, cheap, tool-capable)
  - Code-heavy: Gemma 4 31B (strong at code + tool calling)
  - Deep reasoning: Qwen3 235B (heavyweight, only for explicitly complex tasks)
  - Creative: Llama 4 Maverick (narrative, brainstorming)
  - Quick: Grok 4.1 Fast (concise answers)
  - Vision: Gemma 4 31B (multimodal)

Codestral is EXCLUDED — it does not support tool calling.
"""

from __future__ import annotations
import re

# ------------------------------------------------------------------ #
# Router tiers — every model here MUST support function/tool calling
# ------------------------------------------------------------------ #

ROUTER_TIERS = [
    # Tier 1: default / fast (simple Q&A, greetings, general tasks)
    {
        "id": "openrouter/minimax/minimax-m2.7",
        "tier": "fast",
        "label": "MiniMax M2.7",
        "keywords": [],
        "patterns": [
            re.compile(r"^(hi|hey|hello|yo|howdy|what'?s up|sup|greetings)\b", re.I),
            re.compile(r"^(yes|no|ok(ay)?|sure|yeah|yep|nope|lmk)\s*[!\?\.]*\s*$", re.I),
        ],
    },
    # Tier 2: code (Gemma 4 31B — strong code + full tool support)
    {
        "id": "openrouter/google/gemma-4-31b-it",
        "tier": "code",
        "label": "Gemma 4 31B (Code)",
        "keywords": [
            "refactor", "debug", "linter", "stack trace", "traceback",
            "code review", "pull request", "unit test", "pytest",
        ],
        "patterns": [
            re.compile(r"(debug|refactor|review).*(code|function|class|module)\b", re.I),
            re.compile(r"(fix|find).*(bug|error|exception)\b", re.I),
            re.compile(r"(write|create|implement).*(function|class|api|endpoint)\b", re.I),
            re.compile(r"(code review|PR review|pull request)", re.I),
        ],
    },
    # Tier 3: reasoning (Qwen3 235B — only for explicitly complex tasks)
    {
        "id": "openrouter/qwen/qwen3-235b-a22b-2507",
        "tier": "reasoning",
        "label": "Qwen3 235B (Reasoning)",
        "keywords": [
            "analyze in depth", "deep analysis", "compare and contrast",
            "system design", "architecture design", "prove", "theorem",
        ],
        "patterns": [
            re.compile(r"(deep|thorough|comprehensive)\s+(analysis|review|audit|dive)\b", re.I),
            re.compile(r"(design|architect)\s+(a |the )?(system|architecture|platform)\b", re.I),
            re.compile(r"(prove|demonstrate|mathematical)\b", re.I),
        ],
    },
    # Tier 4: creative (Llama 4 Maverick)
    {
        "id": "openrouter/meta-llama/llama-4-maverick",
        "tier": "creative",
        "label": "Llama 4 Maverick (Creative)",
        "keywords": [
            "story", "poem", "song", "lyrics", "haiku",
            "brainstorm", "creative writing",
        ],
        "patterns": [
            re.compile(r"(write|compose).*(story|poem|song|lyrics|haiku)\b", re.I),
            re.compile(r"(brainstorm|ideate).*(ideas?|concepts?)\b", re.I),
        ],
    },
    # Tier 5: Grok fast (quick answers)
    {
        "id": "openrouter/x-ai/grok-4.1-fast",
        "tier": "grok",
        "label": "Grok 4.1 Fast",
        "keywords": [],
        "patterns": [
            re.compile(r"(summarize|tl ?dr|summary of)\b", re.I),
        ],
    },
    # Tier 6: vision (image attachments)
    {
        "id": "openrouter/google/gemma-4-31b-it",
        "tier": "vision",
        "label": "Gemma 4 31B (Vision)",
        "keywords": [],
        "patterns": [],
        "attachment_required": True,
    },
]

DEFAULT_ROUTER_MODEL = "openrouter/minimax/minimax-m2.7"


def _score_tier(tier: dict, prompt: str, has_attachment: bool) -> float:
    """Score how well a tier matches. Higher = better match."""
    if tier.get("attachment_required"):
        return 100.0 if has_attachment else 0.0

    score = 0.0
    prompt_lower = prompt.lower()

    for kw in tier.get("keywords", []):
        if kw.lower() in prompt_lower:
            score += 1.0

    for pat in tier.get("patterns", []):
        if pat.search(prompt):
            score += 3.0

    return score


def auto_model_for(prompt: str, has_attachment: bool = False) -> str:
    """Classify prompt intent and return the best tool-capable model."""
    if not prompt or not prompt.strip():
        return DEFAULT_ROUTER_MODEL

    best_score = -1.0
    best_tier_id = DEFAULT_ROUTER_MODEL

    for tier in ROUTER_TIERS:
        score = _score_tier(tier, prompt.strip(), has_attachment)
        if score > best_score:
            best_score = score
            best_tier_id = tier["id"]

    # Require strong signal (3+ points = at least one pattern match)
    # to override default. Prevents weak keyword matches from routing
    # every technical message to a specialist model.
    if best_score < 3.0:
        return DEFAULT_ROUTER_MODEL

    return best_tier_id


def get_tier_for_model(model_id: str) -> str:
    """Return the tier label for a given model ID."""
    for tier in ROUTER_TIERS:
        if tier["id"] == model_id:
            return tier["tier"]
    return "fast"
