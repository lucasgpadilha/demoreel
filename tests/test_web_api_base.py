"""The web UI's `?api=` link parameter: another server is used only after the person confirms it.

Runs the real `web/js/api.js` in Node with the few browser globals it touches; skipped without Node.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

API_JS = Path(__file__).resolve().parent.parent / "src" / "demoreel" / "web" / "js" / "api.js"

HARNESS = r"""
import { pathToFileURL } from 'node:url';
const [apiJs, page, answer] = process.argv.slice(2);
const store = new Map();
const asked = [];
globalThis.location = new URL(page);
globalThis.document = { baseURI: new URL('/', page).href, documentElement: {} };
Object.defineProperty(globalThis, 'navigator', { value: { languages: ['en'] }, configurable: true });
globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v),
                            removeItem: (k) => store.delete(k) };
globalThis.window = { confirm: (message) => { asked.push(message); return answer === 'yes'; } };
const m = await import(pathToFileURL(apiJs).href);
console.log(JSON.stringify({ base: m.apiInfo.base, overridden: m.apiInfo.overridden, asked }));
"""


def run(tmp_path: Path, page: str, answer: str = "no") -> dict:
    harness = tmp_path / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    out = subprocess.run(["node", str(harness), str(API_JS), page, answer], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node to run the UI's modules")


def test_a_link_to_another_server_asks_first(tmp_path):
    page = "https://lances.mixdavila.example/?api=https://evil.example/x"
    refused = run(tmp_path, page, "no")
    assert refused["overridden"] is False and refused["base"] == "https://lances.mixdavila.example"
    assert len(refused["asked"]) == 1 and "https://evil.example" in refused["asked"][0]
    accepted = run(tmp_path, page, "yes")
    assert accepted["overridden"] is True and accepted["base"] == "https://evil.example/x"


def test_a_link_to_a_server_on_this_computer_needs_no_question(tmp_path):
    for target in ("http://127.0.0.1:8077", "http://localhost:8000", "http://[::1]:8000", "http://dev.localhost"):
        got = run(tmp_path, f"https://lances.mixdavila.example/?api={target}")
        assert got["overridden"] is True and got["asked"] == [], target
