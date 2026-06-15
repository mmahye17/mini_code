"""
Skill Loading — scan and load skill definitions from the skills directory.

Skills are markdown files with YAML frontmatter located in skills/*/SKILL.md.
Catalog is loaded at startup; full content is loaded on demand via load_skill().
"""

import yaml
from shared.config import SKILLS_DIR

SKILL_REGISTRY: dict[str, dict] = {}


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, parts[2].strip()


def scan_skills():
    """Scan the skills directory and populate the registry."""
    SKILL_REGISTRY.clear()
    if not SKILLS_DIR.exists():
        return
    for directory in sorted(SKILLS_DIR.iterdir()):
        if not directory.is_dir():
            continue
        manifest = directory / "SKILL.md"
        if not manifest.exists():
            continue
        raw = manifest.read_text()
        meta, _ = _parse_frontmatter(raw)
        name = meta.get("name", directory.name)
        desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
        SKILL_REGISTRY[name] = {
            "name": name,
            "description": desc,
            "content": raw,
        }


def list_skills() -> str:
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(
        f"- {skill['name']}: {skill['description']}"
        for skill in SKILL_REGISTRY.values())


def load_skill(name: str) -> str:
    """Load the full content of a skill by name."""
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        available = ", ".join(SKILL_REGISTRY.keys()) or "(none)"
        return f"Skill not found: {name}. Available: {available}"
    return skill["content"]
