"""
webui_tools/skills_tool.py -- Skill browsing for Hermes WebUI

Provides skills_list() and skill_view() by reading SKILL.md files
from the user's skills directory (~/.hermes/skills/ or ~/.hermes/<profile>/skills/).
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any

HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
SKILLS_DIR = HERMES_HOME / "skills"


def skills_list() -> dict:
    """Return all skills as a dict: {'skills': [{'name': ..., 'description': ...}, ...]}"""
    skills = []
    if not SKILLS_DIR.is_dir():
        return {"skills": skills}

    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
        except Exception:
            continue

        # Parse frontmatter for name and description
        name = skill_dir.name
        description = ""
        if text.startswith("---"):
            end = text.find("\n---\n", 4)
            if end > 0:
                fm_text = text[4:end]
                for line in fm_text.splitlines():
                    if line.startswith("name:"):
                        name = line.split(":", 1)[1].strip().strip('"').strip("'")
                    elif line.startswith("description:"):
                        description = line.split(":", 1)[1].strip().strip('"').strip("'")

        skills.append({"name": name, "description": description})

    return {"skills": skills}


def skill_view(name: str) -> dict:
    """Return skill content and linked files for a named skill.

    Args:
        name: skill directory name (supports subdirectory paths like 'mlops/training/unsloth')

    Returns:
        {'name': ..., 'description': ..., 'content': ..., 'linked_files': {...}}
    """
    if not name:
        return {"error": "name is required"}

    # name may be a path like 'mlops/training/unsloth'
    skill_path = SKILLS_DIR / name
    # Try exact match on directory
    if not skill_path.is_dir():
        # Fall back to glob search
        matches = list(SKILLS_DIR.rglob(name))
        for m in matches:
            if m.is_dir():
                skill_path = m
                break

    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return {"error": f"Skill '{name}' not found"}

    try:
        text = skill_md.read_text(encoding="utf-8")
    except Exception as e:
        return {"error": str(e)}

    # Parse frontmatter
    description = ""
    content = text
    if text.startswith("---"):
        end = text.find("\n---\n", 4)
        if end > 0:
            fm_text = text[4:end]
            content = text[end + 5:]
            for line in fm_text.splitlines():
                if line.startswith("description:"):
                    description = line.split(":", 1)[1].strip().strip('"').strip("'")

    # Collect linked files (scripts/, templates/, references/)
    linked_files = {}
    for subdir in ("scripts", "templates", "references"):
        sub_path = skill_path / subdir
        if sub_path.is_dir():
            linked_files[subdir] = []
            for f in sorted(sub_path.iterdir()):
                if f.is_file():
                    rel = f.relative_to(skill_path)
                    linked_files[subdir].append(str(rel))

    return {
        "name": name,
        "description": description,
        "content": content.strip(),
        "linked_files": linked_files,
    }
