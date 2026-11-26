"""The agent skill (skills/demoreel), its zip for skill marketplaces and the Claude Code plugin manifests."""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "demoreel"

_spec = importlib.util.spec_from_file_location("build_skill_zip", ROOT / "scripts" / "build_skill_zip.py")
bsz = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bsz)

# Named in SKILL.md ahead of mcp_server.py; drop a name from here once the server registers it.
DOCUMENTED_AHEAD = {"set_category"}


def _skill(tmp_path: Path, frontmatter: str | None = None, body: str = "Body.\n", name: str = "demo-skill") -> Path:
    d = tmp_path / name
    d.mkdir()
    frontmatter = frontmatter or f"name: {name}\ndescription: Does things."
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return d


def test_the_repository_skill_packs_with_skill_md_at_the_root(tmp_path):
    out = bsz.build(SKILL, tmp_path / "dist" / "demoreel-skill.zip")
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert names[0] == "SKILL.md"
        assert all(n == "SKILL.md" or n.split("/")[0] in bsz.FOLDERS for n in names)
        assert "references/rest-api.md" in names
        for n in names:
            assert b"\r\n" not in zf.read(n), f"{n} was packed with CRLF line endings"
        fields = bsz.frontmatter(zf.read("SKILL.md").decode("utf-8"))
    assert fields["name"] == "demoreel"
    assert "CS2" in fields["description"] and len(fields["description"]) <= 1024
    assert out.stat().st_size < bsz.MAX_BYTES
    again = bsz.build(SKILL, tmp_path / "again.zip")
    assert again.read_bytes() == out.read_bytes(), "the same sources must give the same zip"


def test_the_skill_only_names_tools_the_mcp_server_has():
    tree = ast.parse((ROOT / "src" / "demoreel" / "mcp_server.py").read_text(encoding="utf-8"))
    registered = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        and any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in {"tool", "prompt"}
                for d in node.decorator_list)}
    assert {"list_matches", "get_job", "create_highlight"} <= registered
    named = set(re.findall(r"`([a-z][a-z_]+)\(", (SKILL / "SKILL.md").read_text(encoding="utf-8")))
    assert named, "SKILL.md names no tools"
    unknown = named - registered - DOCUMENTED_AHEAD
    assert not unknown, f"SKILL.md names tools the MCP server does not have: {sorted(unknown)}"


def test_the_plugin_manifests_point_at_the_skill_and_the_local_mcp_endpoint():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    entry = next(p for p in market["plugins"] if p["name"] == plugin["name"])
    assert entry["source"] == "./"  # the repository root is the plugin
    assert (ROOT / "skills" / "demoreel" / "SKILL.md").is_file()
    server = plugin["mcpServers"]["demoreel"]
    assert server["type"] == "http" and server["url"].endswith("/mcp")
    assert not (ROOT / ".mcp.json").exists(), "a root .mcp.json would also be the project MCP config of the repo"


def test_dist_is_ignored_by_git():
    assert "dist/" in (ROOT / ".gitignore").read_text(encoding="utf-8").split()


@pytest.mark.parametrize(("frontmatter", "message"), [
    ("description: Does things.", "name"),
    ("name: Demo_Skill\ndescription: Does things.", "not valid"),
    ("name: other-name\ndescription: Does things.", "folder name"),
    ("name: demo-skill", "needs a description"),
    ("name: demo-skill\ndescription: Use when: the user asks.", "': '"),
    ("name: demo-skill\ndescription: Renders <video> files.", "'<'"),
    ("name: demo-skill\ndescription: \"Not closed.", "not closed"),
    ("name: demo-skill\ndescription: " + "x" * 1025, "1024"),
])
def test_bad_frontmatter_is_refused_with_what_to_change(tmp_path, frontmatter, message):
    with pytest.raises(bsz.SkillError, match=re.escape(message)):
        bsz.check(_skill(tmp_path, frontmatter))


def test_quoted_values_may_hold_colons(tmp_path):
    d = _skill(tmp_path, 'name: demo-skill\ndescription: "Use when: the user asks."')
    assert bsz.check(d) == [(d / "SKILL.md").resolve()]


def test_links_must_stay_inside_the_skill(tmp_path):
    d = _skill(tmp_path, body="See [the docs](https://example.org/docs), [below](#more) and [ref](references/a.md).\n"
                             "```\n[not a link](../ignored-in-code.md)\n```\n")
    (d / "references").mkdir()
    (d / "references" / "a.md").write_text("Back to [the skill](../SKILL.md#flows).\n", encoding="utf-8")
    assert len(bsz.check(d)) == 2
    bad = _skill(tmp_path, body="See [metrics](../../docs/metrics.md).\n", name="other")
    with pytest.raises(bsz.SkillError, match="outside the skill"):
        bsz.check(bad)


def test_stray_files_next_to_skill_md_are_refused(tmp_path):
    d = _skill(tmp_path)
    (d / "notes.txt").write_text("private", encoding="utf-8")
    with pytest.raises(bsz.SkillError, match="not allowed next to SKILL.md"):
        bsz.check(d)


def test_the_command_line_reports_errors_without_a_traceback(tmp_path, capsys):
    d = _skill(tmp_path, "name: demo-skill")
    assert bsz.main(["--skill", str(d), "--out", str(tmp_path / "x.zip")]) == 1
    assert "needs a description" in capsys.readouterr().err
    assert not (tmp_path / "x.zip").exists()
    good = _skill(tmp_path, name="good")
    assert bsz.main(["--skill", str(good), "--check"]) == 0
    assert bsz.main(["--skill", str(good), "--out", str(tmp_path / "good.zip")]) == 0
    assert zipfile.ZipFile(tmp_path / "good.zip").namelist() == ["SKILL.md"]
