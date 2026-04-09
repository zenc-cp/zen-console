"""api/vision.py — Image analysis via mmx vision describe.

Called from routes.py POST /api/vision/describe.
Allows Hermes users to drop images into workspace and ask about them.
"""
from __future__ import annotations

import json
import shutil
import subprocess


def mmx_available() -> bool:
    """Return True if the ``mmx`` binary is on PATH and exits cleanly."""
    if shutil.which("mmx") is None:
        return False
    try:
        result = subprocess.run(
            ["mmx", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def describe_image(
    image_path_or_url: str,
    prompt: str = "Describe the image.",
    file_id: str = None,
) -> dict:
    """Describe an image using ``mmx vision describe``.

    Args:
        image_path_or_url: Local path or HTTP URL to the image.
            Ignored if *file_id* is provided.
        prompt: Instruction sent to the vision model.
        file_id: If provided, uses ``--file-id`` instead of ``--image``.

    Returns:
        ``{"content": "...", "source": "mmx", "ok": True}`` on success.
        ``{"content": "", "error": "...", "ok": False}`` on failure.
    """
    if not mmx_available():
        return {"content": "", "error": "mmx not available", "ok": False}

    try:
        cmd = ["mmx", "vision", "describe", "--prompt", prompt, "--quiet"]
        if file_id:
            cmd += ["--file-id", file_id]
        else:
            cmd += ["--image", image_path_or_url]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
            return {"content": "", "error": f"mmx exited {result.returncode}: {stderr}", "ok": False}

        stdout = result.stdout.strip()
        # mmx may return JSON or plain text
        try:
            data = json.loads(stdout)
            content = (
                data.get("description")
                or data.get("content")
                or data.get("text")
                or str(data)
            )
        except (json.JSONDecodeError, AttributeError):
            content = stdout

        return {"content": content, "source": "mmx", "ok": True}

    except subprocess.TimeoutExpired:
        return {"content": "", "error": "mmx vision describe timed out after 30s", "ok": False}
    except Exception as exc:
        return {"content": "", "error": str(exc), "ok": False}


def upload_and_describe(local_path: str, prompt: str = "Describe the image.") -> dict:
    """Upload a local file then describe it via its MiniMax file_id.

    First calls ``mmx file upload --file {path} --purpose vision --quiet``
    to obtain a *file_id*, then calls :func:`describe_image` with that id.

    Args:
        local_path: Absolute or relative path to the local image file.
        prompt: Instruction sent to the vision model.

    Returns:
        Same shape as :func:`describe_image`.
    """
    if not mmx_available():
        return {"content": "", "error": "mmx not available", "ok": False}

    try:
        upload_result = subprocess.run(
            ["mmx", "file", "upload", "--file", local_path, "--purpose", "vision", "--quiet"],
            capture_output=True,
            text=True,
            timeout=60,
        )

        if upload_result.returncode != 0:
            stderr = (upload_result.stderr or upload_result.stdout or "").strip()
            return {"content": "", "error": f"mmx file upload failed: {stderr}", "ok": False}

        stdout = upload_result.stdout.strip()
        file_id = None
        try:
            data = json.loads(stdout)
            file_id = (
                data.get("file_id")
                or data.get("fileId")
                or data.get("id")
            )
        except (json.JSONDecodeError, AttributeError):
            file_id = stdout.strip()

        if not file_id:
            return {"content": "", "error": "mmx file upload returned no file_id", "ok": False}

        return describe_image(image_path_or_url="", prompt=prompt, file_id=str(file_id))

    except subprocess.TimeoutExpired:
        return {"content": "", "error": "mmx file upload timed out", "ok": False}
    except Exception as exc:
        return {"content": "", "error": str(exc), "ok": False}
