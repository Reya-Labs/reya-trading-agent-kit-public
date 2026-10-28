"""Validate the SKILL.md agentskills.io manifest.

The skill spec requires YAML frontmatter with `name` (lowercase letters,
digits, hyphens; max 64 chars; must match the skill directory name when
installed) and `description` (1-1024 chars). Claude Code triggers the skill
off the description text alone, so these tests pin the contract — a doc edit
that breaks the frontmatter would otherwise silently stop the skill loading.
"""

import re

import yaml

from agent_kit.scripts._env import KIT_ROOT

SKILL_MD = KIT_ROOT / "SKILL.md"
CANONICAL_NAME = "reya-trading-agent-kit"


def _frontmatter() -> dict:
    text = SKILL_MD.read_text(encoding="utf-8")
    assert text.startswith("---\n"), "SKILL.md must start with YAML frontmatter"
    end = text.index("\n---", 4)
    parsed = yaml.safe_load(text[4:end])
    assert isinstance(parsed, dict), "frontmatter must be a YAML mapping"
    return parsed


def test_skill_md_exists_at_kit_root() -> None:
    assert SKILL_MD.is_file()


def test_frontmatter_name_is_valid_and_canonical() -> None:
    name = _frontmatter()["name"]
    assert re.fullmatch(r"[a-z0-9-]{1,64}", name), f"invalid skill name: {name!r}"
    assert name == CANONICAL_NAME


def test_frontmatter_description_within_spec_length() -> None:
    description = _frontmatter()["description"]
    assert isinstance(description, str)
    assert 1 <= len(description) <= 1024


def test_frontmatter_allows_bash() -> None:
    allowed_tools = _frontmatter().get("allowed-tools")
    assert isinstance(allowed_tools, list), "allowed-tools must be a list"
    assert "Bash" in allowed_tools


def test_frontmatter_states_compatibility_requirements() -> None:
    # The spec defines compatibility as free text (max 500 chars), so substring
    # checks are the right strength here.
    compatibility = _frontmatter().get("compatibility")
    assert isinstance(compatibility, str), "compatibility must be a string"
    assert "Python 3.12" in compatibility
    assert "Poetry" in compatibility
