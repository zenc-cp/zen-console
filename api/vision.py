"""api/vision.py — Image analysis via OpenRouter vision models.

Called from routes.py POST /api/vision/describe.
Allows Hermes users to drop images into workspace and ask about them.

Backend: qwen/qwen3-vl-32b-instruct via OpenRouter (cheap, capable, no mmx required).
Fallback: z-ai/glm-5v-turbo (already in ZenOps model stack).

No mmx CLI required. Uses OPENROUTER_API_KEY from environment.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request
import urllib.error
from pathlib import Path

# Vision model priority — all available on OpenRouter, no Anthropic/OpenAI/Google
VISION_MODELS = [
    "qwen/qwen3-vl-32b-instruct",   # cheapest capable vision, $0.10/M
    "z-ai/glm-5v-turbo",            # fallback — already in ZenOps stack
    "google/gemma-4-31b-it:free",   # free fallback (multimodal)
]

OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def _api_key() -> str | None:
    return os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_KEY")


def _image_to_data_url(path_or_url: str) -> str:
    """Convert local path or URL to base64 data URL for OpenRouter vision API."""
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url  # pass URL directly — OpenRouter accepts it

    # Local file — encode as base64 data URL
    p = Path(path_or_url)
    if not p.exists():
        raise FileNotFoundError(f"Image not found: {path_or_url}")

    suffix = p.suffix.lower().lstrip(".")
    mime = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp",
    }.get(suffix, "image/jpeg")

    data = base64.b64encode(p.read_bytes()).decode()
    return f"data:{mime};base64,{data}"


def describe_image(
    image_path_or_url: str,
    prompt: str = "Describe the image in detail.",
    file_id: str = None,
    model: str = None,
) -> dict:
    """Describe an image using OpenRouter vision model.

    Args:
        image_path_or_url: Local file path or HTTP/S URL to the image.
        prompt: Instruction sent to the vision model.
        file_id: Unused (kept for API compatibility — mmx file_id not applicable here).
        model: Override vision model (default: qwen/qwen3-vl-32b-instruct).

    Returns:
        {"content": "...", "source": "openrouter", "model": "...", "ok": True} on success.
        {"content": "", "error": "...", "ok": False} on failure.
    """
    api_key = _api_key()
    if not api_key:
        return {"content": "", "error": "OPENROUTER_API_KEY not set", "ok": False}

    if not image_path_or_url and not file_id:
        return {"content": "", "error": "No image provided", "ok": False}

    # file_id not supported without mmx — return clear error
    if file_id and not image_path_or_url:
        return {
            "content": "",
            "error": "file_id mode requires mmx (not available). Provide image path or URL instead.",
            "ok": False,
        }

    selected_model = model or VISION_MODELS[0]

    try:
        image_ref = _image_to_data_url(image_path_or_url)
    except FileNotFoundError as e:
        return {"content": "", "error": str(e), "ok": False}
    except Exception as e:
        return {"content": "", "error": f"Image encoding error: {e}", "ok": False}

    payload = {
        "model": selected_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": image_ref},
                    },
                    {
                        "type": "text",
                        "text": prompt,
                    },
                ],
            }
        ],
        "max_tokens": 1024,
    }

    req = urllib.request.Request(
        f"{OPENROUTER_BASE}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://z3nops.com",
            "X-Title": "ZenOps Hermes",
        },
        method="POST",
    )

    # Try primary model, fall back on error
    models_to_try = [selected_model] + [m for m in VISION_MODELS if m != selected_model]

    for attempt_model in models_to_try:
        payload["model"] = attempt_model
        req.data = json.dumps(payload).encode()
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                body = json.loads(resp.read())
                content = body["choices"][0]["message"]["content"]
                return {
                    "content": content,
                    "source": "openrouter",
                    "model": attempt_model,
                    "ok": True,
                }
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")
            # Try next model on 4xx (model may not support vision)
            if e.code in (400, 404, 422):
                continue
            return {"content": "", "error": f"HTTP {e.code}: {err_body[:200]}", "ok": False}
        except urllib.error.URLError as e:
            return {"content": "", "error": f"Network error: {e.reason}", "ok": False}
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            return {"content": "", "error": f"Response parse error: {e}", "ok": False}
        except Exception as e:
            return {"content": "", "error": str(e), "ok": False}

    return {"content": "", "error": "All vision models failed", "ok": False}


def upload_and_describe(local_path: str, prompt: str = "Describe the image in detail.") -> dict:
    """Describe a local image file directly (no upload step needed with OpenRouter).

    Kept for API compatibility with old mmx-based implementation.
    OpenRouter accepts base64 inline — no separate upload required.
    """
    return describe_image(image_path_or_url=local_path, prompt=prompt)


def available() -> bool:
    """Return True if vision API is available (OPENROUTER_API_KEY set)."""
    return bool(_api_key())
