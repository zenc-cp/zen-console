"""Read-only catalog handling for explicitly configured native Copilot.

No credentials, authentication probing, network or persistence. Non-Copilot
configuration returns None so the existing provider-discovery path is unchanged.
"""
from __future__ import annotations
import re


def configured_copilot_catalog(config: dict, fallback_default: str = "") -> dict | None:
    model = config.get("model")
    ids = config.get("available_models")
    if not isinstance(model, dict) or model.get("provider") != "copilot":
        return None
    if not isinstance(ids, list) or not ids:
        return None
    if any(not isinstance(mid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", mid) for mid in ids) or len(ids) != len(set(ids)):
        raise ValueError("Copilot catalog requires unique safe bare model IDs")
    metadata = config.get("dsh_model_alignment") or {}
    if not isinstance(metadata, dict):
        raise ValueError("Copilot alignment metadata must be a mapping")
    missing = metadata.get("unavailable_model_ids", [])
    if not isinstance(missing, list) or any(not isinstance(mid, str) for mid in missing):
        raise ValueError("Copilot availability metadata must be a model-ID list")
    unavailable = set(missing)
    default = model.get("default") or model.get("model") or fallback_default
    if not isinstance(default, str) or default not in ids or default in unavailable:
        raise ValueError("Copilot default must exist and be available in the configured catalog")
    rows = [{"id": mid, "label": mid + (" (unavailable in live catalog)" if mid in unavailable else ""),
             "disabled": mid in unavailable} for mid in ids]
    return {"active_provider": "copilot", "default_model": default,
            "groups": [{"provider": "GitHub Copilot", "models": rows}]}
