"""The plugin's metadata agrees with the skill and the specification (AGENTS.md, "Versions and releases"). Skips when
the skill folder isn't in its repository, e.g. copied into an agent's skills folder."""

import json
import re
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
REPO = SKILL.parent.parent
PLUGIN = REPO / ".claude-plugin"
SPEC = REPO / "docs" / "spec.md"
pytestmark = pytest.mark.skipif(not ((PLUGIN / "plugin.json").is_file() and SPEC.is_file()),
                                reason="the skill folder is not in its repository")


def manifest(name: str) -> dict:
    return json.loads((PLUGIN / name).read_text(encoding="utf-8"))


def test_the_plugin_and_its_marketplace_entry_name_the_skill():
    plugin, [entry] = manifest("plugin.json"), manifest("marketplace.json")["plugins"]
    skill_name = re.search(r"^name:\s*(\S+)\s*$", (SKILL / "SKILL.md").read_text(encoding="utf-8"), re.M).group(1)
    assert plugin["name"] == entry["name"] == skill_name == SKILL.name, "a skill's name must equal its folder's"
    assert entry["source"] == "./", "the marketplace installs this repository"
    assert entry["description"] == plugin["description"]


def test_the_plugin_version_follows_the_specification():
    """Spec vX.Y is plugin X.Y.Z (Z for releases without a spec change); the skill's README names the same spec.
    Only plugin.json carries the version: Claude Code warns when the marketplace entry has one too."""
    version = manifest("plugin.json")["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), version
    spec = re.search(r"specification v(\d+\.\d+)\b", SPEC.read_text(encoding="utf-8")).group(1)
    readme = re.search(r"\) is v(\d+\.\d+)\.", (SKILL / "README.md").read_text(encoding="utf-8")).group(1)
    assert version.rsplit(".", 1)[0] == spec == readme
    assert "version" not in manifest("marketplace.json")["plugins"][0]
