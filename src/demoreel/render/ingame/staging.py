"""Files the game reads: the demo and the actions file that must sit next to it as ``<demo path>.json``.

The plugin takes the path given to ``+playdemo`` and loads ``<that path>.json``
(``cs2-server-plugin/cs2-server-plugin/sequences.cpp`` ``LoadSequencesFile``), so the demo is hard-linked (or
copied when linking is impossible) into the job's scratch folder and the JSON is written there. That keeps the
user's demo folder clean and gives a short, ASCII-only path - CS2 fails on paths with other characters
(CS Demo Manager's ``assert-demo-path-is-valid.ts``).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..base import RenderError
from .launcher import is_basic_latin


@dataclass
class StagedDemo:
    path: Path  # what goes after +playdemo
    owned: bool  # True: a link/copy we created and delete afterwards; False: the user's file, used in place

    @property
    def actions_path(self) -> Path:
        return Path(str(self.path) + ".json")

    def write_actions(self, text: str) -> Path:
        self.actions_path.write_text(text, encoding="utf-8", newline="\n")
        return self.actions_path

    def cleanup(self) -> None:
        """Never raises."""
        try:
            self.actions_path.unlink(missing_ok=True)
            if self.owned:
                self.path.unlink(missing_ok=True)
                try:
                    self.path.parent.rmdir()
                except OSError:
                    pass
        except OSError:
            pass


def stage_demo(demo_path: Path, work_dir: Path) -> StagedDemo:
    demo = Path(os.path.abspath(demo_path))
    if not demo.is_file():
        raise RenderError(f"Demo file not found: {demo}")
    stage_dir = Path(os.path.abspath(work_dir)) / "ingame"
    if is_basic_latin(str(stage_dir)):
        stage_dir.mkdir(parents=True, exist_ok=True)
        target = stage_dir / "demo.dem"
        target.unlink(missing_ok=True)
        try:
            os.link(demo, target)
        except OSError:  # other volume, filesystem without hard links...
            shutil.copyfile(demo, target)
        return StagedDemo(path=target, owned=True)
    if is_basic_latin(str(demo)):
        staged = StagedDemo(path=demo, owned=False)
        if staged.actions_path.exists():
            raise RenderError(f"{staged.actions_path} already exists: another tool (CS Demo Manager?) is using "
                              f"this demo. Close it or remove the file.")
        return staged
    raise RenderError("CS2 cannot play a demo whose path contains non-ASCII characters, and neither the data "
                      f"folder ({stage_dir}) nor the demo ({demo}) has an ASCII-only path. Move the demoreel "
                      "data folder (DEMOREEL_DATA_DIR) to a path without accents or special characters.")
