"""
api/model_router.py -- Auto LLM Router for Hermes WebUI

Routes user messages to the best model based on intent classification.
ALL models MUST support tool calling — Hermes requires tools for every interaction.

H1 migration (2026-06): all models now route through Azure AI Foundry
(endpoint: foundry-zen-eastus, sub-2 450e3a57, rg-foundry-zen, eastus).
Keyless Managed Identity auth — no OpenRouter key required.

Rules:
  - Default: Grok 4.1 Fast Reasoning (fast, tool-capable, Azure Foundry)
  - Code-heavy: GPT-5.4 Mini (strong at code + full tool support)
  - Deep reasoning: Claude Sonnet 4.6 (heavyweight, only for explicitly complex tasks)
  - Creative: Llama 4 Maverick (narrative, brainstorming)
  - Quick/summarise: Phi-4 (compact, fast)
  - Vision: GPT-5.4 Mini (multimodal, tool-capable)
"""

from __future__ import annotations
import re

# ------------------------------------------------------------------ #
# Router tiers — every model here MUST support function/tool calling
# All IDs are Azure AI Foundry deployment names (H1 — no openrouter prefix)
# ------------------------------------------------------------------ #

ROUTER_TIERS = [
    # Tier 1: default / fast (simple Q&A, greetings, general tasks)
    {
        "id": "grok-4-1-fast-reasoning",
        "tier": "fast",
        "label": "Grok 4.1 Fast Reasoning",
        "keywords": [],
        "patterns": [
            re.compile(r"^(hi|hey|hello|yo|howdy|what'?s up|sup|greetings)\b", re.I),
            re.compile(r"^(yes|no|ok(ay)?|sure|yeah|yep|nope|lmk)\s*[!\?\.]*\s*$", re.I),
        ],
    },
    # Tier 2: code (GPT-5.4 Mini — strong code + full tool support)
    {
        "id": "gpt-5.4-mini",
        "tier": "code",
        "label": "GPT-5.4 Mini (Code)",
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
    # Tier 3: reasoning (Claude Sonnet 4.6 — only for explicitly complex tasks)
    {
        "id": "claude-sonnet-4.6",
        "tier": "reasoning",
        "label": "Claude Sonnet 4.6 (Reasoning)",
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
    # Tier 4: creative (Llama 4 Maverick via Azure Foundry)
    {
        "id": "Llama-4-Maverick-17B-128E-Instruct-FP8",
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
    # Tier 5: quick summarise (Phi-4 — compact, fast)
    {
        "id": "phi-4",
        "tier": "quick",
        "label": "Phi-4 (Quick)",
        "keywords": [],
        "patterns": [
            re.compile(r"(summarize|tl ?dr|summary of)\b", re.I),
        ],
    },
    # Tier 6: vision (image attachments — GPT-5.4 Mini supports multimodal + tools)
    {
        "id": "gpt-5.4-mini",
        "tier": "vision",
        "label": "GPT-5.4 Mini (Vision)",
        "keywords": [],
        "patterns": [],
        "attachment_required": True,
    },
]

DEFAULT_ROUTER_MODEL = "grok-4-1-fast-reasoning"


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
