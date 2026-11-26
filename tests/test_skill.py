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
    assert "Not for CS:GO" in fields["description"]  # what it is not for, so it does not trigger on those
    assert out.stat().st_size < bsz.MAX_BYTES
    again = bsz.build(SKILL, tmp_path / "again.zip")
    assert again.read_bytes() == out.read_bytes(), "the same sources must give the same zip"


def _tools(kinds: tuple[str, ...] = ("tool", "prompt")) -> dict[str, set[str]]:
    """MCP tools (and prompts) the server registers, with their parameter names, read from the source."""
    tree = ast.parse((ROOT / "src" / "demoreel" / "mcp_server.py").read_text(encoding="utf-8"))
    return {
        node.name: {a.arg for a in node.args.args + node.args.kwonlyargs}
        for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        and any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in kinds
                for d in node.decorator_list)}


def _edit_ops() -> set[str]:
    from typing import get_args

    from demoreel.schemas import EditOp

    return {get_args(m.model_fields["op"].annotation)[0] for m in get_args(get_args(EditOp)[0])}


def test_the_skill_only_names_tools_the_mcp_server_has():
    registered = set(_tools())
    assert {"list_matches", "get_job", "create_highlight"} <= registered
    named = set(re.findall(r"`([a-z][a-z_]+)\(", (SKILL / "SKILL.md").read_text(encoding="utf-8")))
    assert named, "SKILL.md names no tools"
    unknown = named - registered
    assert not unknown, f"SKILL.md names tools the MCP server does not have: {sorted(unknown)}"


def test_the_skill_says_which_demoreel_it_was_tested_with():
    import tomllib

    from demoreel import __version__

    assert tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"] == __version__
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    m = re.search(r"tested with demoreel (\d+)\.(\d+)\.(\d+)", text)
    assert m, "SKILL.md must say which demoreel version it was tested with"
    assert tuple(map(int, m.groups())) <= tuple(map(int, __version__.split("."))), "the skill is ahead of the code"
    assert "GET /health" in text and "version" in text  # and how to check the server's


CALL = re.compile(r"`([a-z][a-z_]+)\(([^`]*)\)`")
ROUTE = re.compile(r"`(GET|POST|PATCH|PUT|DELETE) (/[^`\s]*)`|`(/[^`\s]*)`")


def _top_level(args: str) -> list[str]:
    """Arguments of a call as written in the skill, split on the commas that are not inside brackets."""
    out, depth, cur = [], 0, ""
    for ch in args:
        depth += ch in "([{"
        depth -= ch in ")]}"
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return [*out, cur]


def test_the_skill_only_names_parameters_the_tools_take():
    tools = _tools()
    checked, wrong = 0, []
    for name, args in CALL.findall((SKILL / "SKILL.md").read_text(encoding="utf-8")):
        for arg in _top_level(args):
            m = re.match(r"^([a-z_][a-z0-9_]*)\s*(?:=|$)", arg.strip())
            if m is None:
                continue  # a literal value ("play") or "..."
            checked += 1
            if m.group(1) not in tools.get(name, set()):
                wrong.append(f"{name}({m.group(1)}=)")
    assert checked >= 20, "the parameter check found almost no calls in SKILL.md"
    assert not wrong, f"SKILL.md passes parameters the tools do not take: {wrong}"


def _documented_routes(text: str) -> list[tuple[str, str]]:
    """(method, path) of every route named as `GET /path`. A bare `/path` after one on the same line continues
    it, as the reference's tables write them: same method, and the same prefix up to the shared segment
    (`/players/{steamid}` after `GET /api/v1/matches/{id}/players`) or the parent (`/rounds`)."""
    out = []
    for line in text.splitlines():
        method = base = None
        for m in ROUTE.finditer(line):
            if m.group(1):
                method, base = m.group(1), m.group(2).split("?")[0]
                path = base
            elif base is not None:
                short = m.group(3).split("?")[0].strip("/").split("/")
                parts = base.strip("/").split("/")
                prefix = parts[:parts.index(short[0])] if short[0] in parts else parts[:-1]
                path = "/" + "/".join(prefix + short)
            else:
                continue  # `/llms.txt` in a sentence: a page, not an API call
            out.append((method, re.sub(r"\{[^}]*\}", "{}", path)))
    return out


def test_the_rest_reference_only_names_routes_the_api_has():
    from demoreel.api import create_app

    paths = create_app().openapi()["paths"]
    real = {(m.upper(), re.sub(r"\{[^}]*\}", "{}", p)) for p, ops in paths.items() for m in ops}
    reference = (SKILL / "references" / "rest-api.md").read_text(encoding="utf-8")
    named = _documented_routes(reference) + _documented_routes((SKILL / "SKILL.md").read_text(encoding="utf-8"))
    assert ("GET", "/api/v1/matches/{}/players/{}") in named and ("GET", "/api/v1/insights/chat") in named
    assert len(named) >= 25, "the route check found almost no routes in the REST reference"
    unknown = sorted({f"{m} {p}" for m, p in named if (m, p) not in real})
    assert not unknown, f"the skill names REST routes the API does not have: {unknown}"


def test_the_agent_docs_list_every_tool_and_every_edit_operation():
    agents = (ROOT / "docs" / "agents.md").read_text(encoding="utf-8")
    table = agents.split("## Tools", 1)[1].split("\n## ", 1)[0]
    missing = {name for name in _tools(("tool",)) if f"`{name}`" not in table}
    assert not missing, f"docs/agents.md (## Tools) does not list: {sorted(missing)}"
    ops = _edit_ops()
    assert {"trim", "set_pov", "set_camera"} <= ops
    llms = (ROOT / "src" / "demoreel" / "web" / "llms.txt").read_text(encoding="utf-8")
    mcp = (ROOT / "src" / "demoreel" / "mcp_server.py").read_text(encoding="utf-8")
    for where, text, form in [("docs/agents.md", agents, '"op": "{}"'), ("llms.txt", llms, "`{}`"),
                              ("the edit_highlight tool", mcp, "{}{{"),
                              ("SKILL.md", (SKILL / "SKILL.md").read_text(encoding="utf-8"), "`{}`")]:
        missing = {op for op in ops if form.format(op) not in text}
        assert not missing, f"{where} does not document the edit operations {sorted(missing)}"


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
