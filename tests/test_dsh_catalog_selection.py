"""Isolated tests of the existing public selector; no runtime imports or server."""
import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]


def select(configuration):
    text = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "get_available_models")
    namespace = {"cfg": configuration, "DEFAULT_MODEL": "minimax/minimax-m2.7"}
    helper_path = ROOT / "api" / "model_catalog.py"
    if helper_path.exists():
        spec = importlib.util.spec_from_file_location("isolated_model_catalog", helper_path)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        namespace["configured_copilot_catalog"] = helper.configured_copilot_catalog
    isolated = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    exec(compile(isolated, "isolated_selector", "exec"), namespace)
    return namespace[function.name]()


def test_explicit_copilot_catalog_reports_correct_provider_default_and_missing_alias():
    cfg = {"model": {"provider": "copilot", "default": "gpt-6.1-sol"},
           "available_models": ["gpt-6.1-sol", "mai-code-1-flash-picker"],
           "dsh_model_alignment": {"unavailable_model_ids": ["mai-code-1-flash-picker"]}}
    result = select(cfg)
    assert result["active_provider"] == "copilot"
    assert result["default_model"] == "gpt-6.1-sol"
    assert len(result["groups"]) == 1
    group = result["groups"][0]
    assert group["provider"] == "GitHub Copilot"
    assert [m["id"] for m in group["models"]] == cfg["available_models"]
    assert group["models"][1]["disabled"] is True
    assert "unavailable" in group["models"][1]["label"].lower()


def test_existing_openrouter_catalog_path_is_unchanged():
    result = select({"model": {"provider": "openrouter"}, "available_models": ["openrouter/google/example"]})
    assert result == {"active_provider": "openrouter", "default_model": "minimax/minimax-m2.7",
                      "groups": [{"provider": "OpenRouter", "models": [{"id": "google/example", "label": "example"}]}]}


def test_startup_default_matches_copilot_config_despite_legacy_saved_default(tmp_path):
    import json
    import os
    import subprocess
    import sys
    home = tmp_path / "home"
    hermes = home / ".hermes"
    hermes.mkdir(parents=True)
    config = hermes / "config.yaml"
    config.write_text("model:\n  provider: copilot\n  default: gpt-6.1-sol\navailable_models: [gpt-6.1-sol]\n", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (state / "settings.json").write_text(json.dumps({"default_model": "minimax/minimax-m2.7", "default_workspace": str(workspace)}), encoding="utf-8")
    env = os.environ.copy()
    env.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(hermes), HERMES_CONFIG_PATH=str(config),
               HERMES_WEBUI_STATE_DIR=str(state), HERMES_WEBUI_DEFAULT_WORKSPACE=str(workspace),
               HERMES_WEBUI_AGENT_DIR=str(tmp_path / "absent-agent"), HERMES_WEBUI_DEFAULT_MODEL="minimax/minimax-m2.7",
               PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-c", "import api.config as c; print(c.DEFAULT_MODEL)"],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8, check=True)
    assert result.stdout.decode().strip() == "gpt-6.1-sol"
    assert json.loads((state / "settings.json").read_text())["default_model"] == "minimax/minimax-m2.7"


def test_invalid_copilot_catalog_or_default_is_rejected_without_provider_fallback():
    import pytest
    cases = [
        {"model": {"provider": "copilot", "default": "missing"}, "available_models": ["gpt-6.1-sol"]},
        {"model": {"provider": "copilot", "default": "mai-code-1-flash-picker"}, "available_models": ["mai-code-1-flash-picker"], "dsh_model_alignment": {"unavailable_model_ids": ["mai-code-1-flash-picker"]}},
        {"model": {"provider": "copilot", "default": "gpt-6.1-sol"}, "available_models": ["gpt-6.1-sol", "gpt-6.1-sol"]},
        {"model": {"provider": "copilot", "default": "@openrouter:other"}, "available_models": ["@openrouter:other"]},
    ]
    for cfg in cases:
        with pytest.raises(ValueError, match="Copilot"):
            select(cfg)
