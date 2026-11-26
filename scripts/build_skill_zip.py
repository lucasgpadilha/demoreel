"""Check the agent skill in ``skills/demoreel/`` and pack it as ``dist/demoreel-skill.zip``.

The archive is what skill marketplaces take: ``SKILL.md`` at its root with ``name`` and ``description`` in its
frontmatter, plus the skill's optional ``references/``, ``scripts/`` and ``assets/`` folders, at most 50 MB.
Text files are stored with LF line endings and every entry gets the same timestamp, so the same sources always
give the same bytes.

    uv run python scripts/build_skill_zip.py           # -> dist/demoreel-skill.zip
    uv run python scripts/build_skill_zip.py --check   # only check the skill

Standard library only: it runs without ``uv sync``.
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = ROOT / "skills" / "demoreel"
OUT = ROOT / "dist" / "demoreel-skill.zip"

MAX_BYTES = 50 * 1024 * 1024
FOLDERS = ("references", "scripts", "assets")  # what may sit next to SKILL.md
TEXT = {".md", ".txt", ".json", ".py", ".sh", ".ps1", ".yaml", ".yml", ".csv"}
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^(```|~~~).*?^\1", re.S | re.M)
EPOCH = (1980, 1, 1, 0, 0, 0)  # earliest date a zip entry can carry


class SkillError(Exception):
    """The skill cannot be packed; the message says what to change."""


def frontmatter(text: str) -> dict[str, str]:
    """Top-level ``key: value`` pairs of the YAML frontmatter (nested blocks are skipped, not parsed)."""
    text = text.replace("\r\n", "\n")
    if text.startswith("﻿"):
        raise SkillError("SKILL.md starts with a byte order mark; save it as UTF-8 without BOM.")
    if not text.startswith("---\n"):
        raise SkillError("SKILL.md must start with a '---' line opening the YAML frontmatter.")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise SkillError("SKILL.md frontmatter is not closed: add a '---' line after the last field.")
    fields: dict[str, str] = {}
    for n, line in enumerate(text[4:end].split("\n"), start=2):
        if not line.strip() or line.lstrip().startswith("#") or line[0] in " \t-":
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise SkillError(f"SKILL.md line {n}: expected 'key: value', got {line!r}.")
        value = value.strip()
        if value[:1] in {'"', "'"}:
            if len(value) < 2 or value[-1] != value[0]:
                raise SkillError(f"SKILL.md line {n}: the quoted value of {key.strip()!r} is not closed.")
            value = value[1:-1]
        elif ": " in value or " #" in value:
            raise SkillError(f"SKILL.md line {n}: the value of {key.strip()!r} contains ': ' or ' #', which YAML "
                             "reads as a nested key or a comment. Reword it or put the value in double quotes.")
        fields[key.strip()] = value
    return fields


def _check_links(path: Path, skill_dir: Path) -> None:
    """Relative links must point at files that are packed too; anything else breaks once the skill is zipped."""
    text = FENCE.sub("", path.read_text(encoding="utf-8"))
    for target in LINK.findall(text):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("#"):
            continue  # https:, mailto:, an anchor in the same file
        dest = (path.parent / target.split("#", 1)[0]).resolve()
        if not dest.is_relative_to(skill_dir) or not dest.is_file():
            raise SkillError(f"{path.relative_to(skill_dir).as_posix()}: link {target!r} points outside the skill "
                             "or at a missing file. Link to the repository on GitHub instead, or add the file to "
                             f"one of {', '.join(f + '/' for f in FOLDERS)}.")


def check(skill_dir: Path = SKILL_DIR) -> list[Path]:
    """Validate the skill folder; returns the files to pack, ``SKILL.md`` first."""
    skill_dir = skill_dir.resolve()
    skill = skill_dir / "SKILL.md"
    if not skill.is_file():
        raise SkillError(f"No SKILL.md in {skill_dir}.")
    fields = frontmatter(skill.read_text(encoding="utf-8"))
    name, description = fields.get("name", ""), fields.get("description", "")
    if not NAME.match(name) or len(name) > 64:
        raise SkillError(f"SKILL.md name {name!r} is not valid: use 1-64 lowercase letters, digits and single "
                         "hyphens (e.g. 'demoreel').")
    if name != skill_dir.name:
        raise SkillError(f"SKILL.md name {name!r} must match its folder name {skill_dir.name!r}.")
    if not description:
        raise SkillError("SKILL.md needs a description: what the skill does and when an agent should use it.")
    if len(description) > 1024:
        raise SkillError(f"SKILL.md description is {len(description)} characters; keep it to 1024.")
    if "<" in description or ">" in description:
        raise SkillError("SKILL.md description must not contain '<' or '>'.")
    if len(fields.get("compatibility", "")) > 500:
        raise SkillError("SKILL.md compatibility is longer than 500 characters; move the detail into the body.")

    files = [skill]
    for entry in sorted(skill_dir.iterdir()):
        if entry.name == "SKILL.md":
            continue
        if not (entry.is_dir() and entry.name in FOLDERS):
            raise SkillError(f"{entry.name!r} is not allowed next to SKILL.md; only the folders "
                             f"{', '.join(FOLDERS)} are packed. Move it into one of them or out of the skill.")
        files += sorted((p for p in entry.rglob("*") if p.is_file() and "__pycache__" not in p.parts),
                        key=lambda p: p.relative_to(skill_dir).as_posix())
    for f in files:
        if f.suffix.lower() == ".md":
            _check_links(f, skill_dir)
    return files


def build(skill_dir: Path = SKILL_DIR, out: Path = OUT) -> Path:
    """Write the zip (replacing an older one) and return its path."""
    skill_dir = skill_dir.resolve()
    files = check(skill_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f in files:
            data = f.read_bytes()
            if f.suffix.lower() in TEXT:
                data = data.replace(b"\r\n", b"\n")
            rel = f.relative_to(skill_dir)
            info = zipfile.ZipInfo(rel.as_posix(), date_time=EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if rel.parts[0] == "scripts" else 0o644) << 16
            zf.writestr(info, data)
    size = tmp.stat().st_size
    if size > MAX_BYTES:
        tmp.unlink()
        raise SkillError(f"The zip would be {size / 1e6:.1f} MB; the limit is {MAX_BYTES / 1e6:.0f} MB.")
    tmp.replace(out)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--skill", type=Path, default=SKILL_DIR, help="skill folder (default: skills/demoreel)")
    parser.add_argument("--out", type=Path, default=OUT, help="zip to write (default: dist/demoreel-skill.zip)")
    parser.add_argument("--check", action="store_true", help="only check the skill, write nothing")
    args = parser.parse_args(argv)
    try:
        if args.check:
            files = check(args.skill)
            print(f"ok: {len(files)} file(s) would be packed")
            return 0
        out = build(args.skill, args.out)
    except SkillError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    print(f"{out} ({out.stat().st_size / 1024:.1f} KB): {', '.join(names)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
