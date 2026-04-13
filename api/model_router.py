"""
api/model_router.py -- Auto LLM Router for Hermes WebUI

Classifies user intent from the prompt and selects the best available model
from the user's configured model list. Operates purely on keyword/heuristic
matching -- no LLM call required, so it adds zero latency.

Usage:
    from api.model_router import auto_model_for, ROUTER_TIERS
    model = auto_model_for("review this code for bugs")
    # → 'openrouter/mistralai/codestral-2508'
"""

from __future__ import annotations

import re

# ------------------------------------------------------------------ #
# Available model tiers (subset of user's available_models config)
# Each entry: model_id, tier label, description
# ------------------------------------------------------------------ #

ROUTER_TIERS = [
    # Tier 0: vision (only if image attachment detected -- handled at call site)
    # Tier 1: fast / cheap (simple Q&A, greetings, one-liners)
    {
        "id": "openrouter/minimax/minimax-m2.7",
        "tier": "fast",
        "label": "MiniMax M2.7",
        "keywords": [],
        "patterns": [
            re.compile(r"^(hi|hey|hello|yo|howdy|what'?s up|sup|greetings)\b", re.I),
            re.compile(r"^what('?s| is)? (the )?time\b", re.I),
            re.compile(r"^how (much|big|large|tall|old|long|far)\b", re.I),
            re.compile(r"^what (is|are|can|could|should|does|do|will|would)[\s\?]", re.I),
            re.compile(r"^who (am I|are you|is|was|does|did|can|should)\b", re.I),
            re.compile(r"^can I (ask|get|have|make|do|use|see|try)\b", re.I),
            re.compile(r"^can you (help|do|show|give|tell|explain|list)\b", re.I),
            re.compile(r"^generate a? ?password\b", re.I),
            re.compile(r"^roll a die\b", re.I),
            re.compile(r"^what('?s| is)? (2|4|6|8|10|12|16|20|100)\s*\+\s*\d+\b", re.I),
            re.compile(r"^(yes|no|ok(ay)?|sure|yeah|yep|nope|lmk)\s*[!\?\.]*\s*$", re.I),
        ],
    },
    # Tier 2: code specialist (code review, debugging, refactoring, explain code)
    {
        "id": "openrouter/mistralai/codestral-2508",
        "tier": "code",
        "label": "Codestral (Code)",
        "keywords": [
            "code", "debug", "refactor", "linter", "eslint", "prettier",
            "python", "javascript", "typescript", "rust", "golang", "java",
            "bug", "error", "exception", "stack trace", "traceback",
            "import", "module", "function", "class", "variable",
            "api", "endpoint", "route", "handler", "middleware",
            "compile", "build", "deploy", "docker", "git", "github",
            "test", "pytest", "unittest", "coverage", "ci/cd",
            "regex", "sql", "query", "schema", "migration",
            "terminal", "shell", "bash", "zsh", "cli", "script",
            "file", "directory", "path", "symlink", "permission",
            "html", "css", "json", "yaml", "toml", "markdown",
            "optimize", "performance", "latency", "throughput",
            "security", "vulnerability", "xss", "sql injection",
            "code review", "pull request", "merge", "branch",
        ],
        "patterns": [
            re.compile(r"(debug|refactor|review|explain).*(code|function|class|file)\b", re.I),
            re.compile(r"(fix|find|catch|handle).*(bug|error|exception|issue)\b", re.I),
            re.compile(r"(write|create|generate|build).*(function|class|api|script)\b", re.I),
            re.compile(r"(terminal|bash|shell|cmd|cli).*(command|run|execute)\b", re.I),
            re.compile(r"(python|javascript|js|ts|rust|go|java|sql|html|css)\s", re.I),
            re.compile(r"(import|from|require|def|class|fn|func|pub|let|const|var)\s", re.I),
            re.compile(r"(test|spec|suite|expect|assert)\s", re.I),
            re.compile(r"(git|github|commit|push|pull|branch|merge|PR)\b", re.I),
        ],
    },
    # Tier 3: reasoning (complex analysis, multi-step, math proofs, architecture)
    {
        "id": "openrouter/qwen/qwen3-235b-a22b-2507",
        "tier": "reasoning",
        "label": "Qwen3 235B (Reasoning)",
        "keywords": [
            "reason", "think", "analyze", "analysis", "思考",
            "solve", "explain", "why", "how", "compare",
            "difference between", "pros and cons", "tradeoff",
            "architecture", "design pattern", "system design",
            "algorithm", "complexity", "optimization", "performance",
            "math", "proof", "theorem", "calculate", "compute",
            "evaluate", "assess", "judge", "decide", "recommend",
            "strategy", "plan", "approach", "methodology",
            "research", "investigate", "discover", "explore",
            "hypothesis", "theory", "model", "framework",
            "benchmark", "experiment", "ab test", "a/b test",
        ],
        "patterns": [
            re.compile(r"(analyze|evaluate|compare|contrast|assess).*(and|vs|versus|with)\b", re.I),
            re.compile(r"(what|how).*(would you|should I|could we|should we)\b", re.I),
            re.compile(r"(design|architect|plan|strategy).*(for|of|to|that)\b", re.I),
            re.compile(r"(complex|complicated|difficult|hard).*(problem|question|issue|task)\b", re.I),
            re.compile(r"(multi.?step|multi.?step|several|multiple|different).*(task|step|part|phase)\b", re.I),
            re.compile(r"(prove|demonstrate|show that|illustrate)\b", re.I),
            re.compile(r"(architecture|system).*(design|overview|diagram|blueprint)\b", re.I),
        ],
    },
    # Tier 4: creative (stories, songs, marketing copy, brainstorming)
    {
        "id": "openrouter/meta-llama/llama-4-maverick",
        "tier": "creative",
        "label": "Llama 4 Maverick (Creative)",
        "keywords": [
            "write", "creative", "story", "poem", "song", "rhyme",
            "blog", "post", "article", "copywriting", "marketing",
            "brainstorm", "idea", "brainstorming", "creative",
            "narrative", "fiction", "character", "plot", "scene",
            "script", "screenplay", "dialogue", "monologue",
            "advertisement", "ad copy", "tagline", "slogan",
            "email template", "outreach", "cold email", "pitch",
            "presentation", "slides", "pitch deck",
            "haiku", "limerick", "verse", "lyrics",
        ],
        "patterns": [
            re.compile(r"(write|compose|generate|create).*(story|poem|song|lyrics|haiku)\b", re.I),
            re.compile(r"(brainstorm|ideate|come up with).*(ideas?|concepts?|options?)\b", re.I),
            re.compile(r"(marketing|copywriting|advertisement|ad).*(copy|text|content|script)\b", re.I),
            re.compile(r"(blog |article |post |tweet ).*(about|on|for)\b", re.I),
        ],
    },
    # Tier 5: Grok fast (concise answers, quick summaries, one-shot tasks)
    {
        "id": "openrouter/x-ai/grok-4.1-fast",
        "tier": "grok",
        "label": "Grok 4.1 Fast",
        "keywords": [
            "quick", "brief", "short", "concise", "summary", "summarize",
            "tl dr", "tldr", "one shot", "one-shot", "quick question",
            "quick answer", "just", "simply", "basically",
        ],
        "patterns": [
            re.compile(r"^(quick|brief|short|concise|simply|basically|just)\s", re.I),
            re.compile(r"(summarize|tl dr|tldr|summary of)\b", re.I),
            re.compile(r"^(what|who|when|where|why|how)\s.+\?", re.I),
        ],
    },
    # Tier 6: vision (if user attaches an image -- detected at call site via attachments)
    # This is handled by the attachment flag, not pattern matching
    {
        "id": "openrouter/google/gemma-4-31b-it",
        "tier": "vision",
        "label": "Gemma 4 31B (Vision)",
        "keywords": [],
        "patterns": [],
        "attachment_required": True,
    },
]

# Default fallback model (MiniMax M2.7)
DEFAULT_ROUTER_MODEL = "openrouter/minimax/minimax-m2.7"


def _score_tier(tier: dict, prompt: str, has_attachment: bool) -> float:
    """Score how well a tier matches the prompt. Higher = better."""
    # Vision tier requires attachment
    if tier.get("attachment_required"):
        return 100.0 if has_attachment else 0.0

    score = 0.0
    prompt_lower = prompt.lower()

    # Keyword matches (each keyword = 1 point)
    for kw in tier.get("keywords", []):
        if kw.lower() in prompt_lower:
            score += 1.0

    # Pattern matches (each pattern = 3 points -- stronger signal)
    for pat in tier.get("patterns", []):
        if pat.search(prompt):
            score += 3.0

    return score


def auto_model_for(prompt: str, has_attachment: bool = False) -> str:
    """
    Classify the user prompt and return the best matching model ID.

    Args:
        prompt: The user's message text
        has_attachment: True if any file/image was attached (triggers vision tier)

    Returns:
        Model ID string (e.g. 'openrouter/mistralai/codestral-2508')
    """
    if not prompt or not prompt.strip():
        return DEFAULT_ROUTER_MODEL

    prompt = prompt.strip()

    # Score all tiers
    best_score = -1.0
    best_tier_id = DEFAULT_ROUTER_MODEL

    for tier in ROUTER_TIERS:
        score = _score_tier(tier, prompt, has_attachment)
        if score > best_score:
            best_score = score
            best_tier_id = tier["id"]

    # Threshold: require at least 1 keyword or 1 pattern match to override fast/default
    # If no tier triggered, keep the default
    if best_score < 1.0:
        return DEFAULT_ROUTER_MODEL

    return best_tier_id


def get_tier_for_model(model_id: str) -> str:
    """Return the tier label for a given model ID."""
    for tier in ROUTER_TIERS:
        if tier["id"] == model_id:
            return tier["tier"]
    return "fast"
