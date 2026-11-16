"""Final assembly: intro bumper + body + outro bumper, with transitions, music and loudness normalisation.

One ffmpeg invocation builds the whole thing from a generated filter graph, so the parts may differ in
codec, size, frame rate or audio layout — everything is conformed to the requested output.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..render import ffmpeg
from ..render.base import RenderError

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


@dataclass
class Part:
    path: Path
    transition: str = "cut"  # how this part is entered: cut | fade | fadewhite | fadeblack | wipeleft | …
    transition_s: float = 0.2


def probe(path: Path) -> MediaInfo:
    """Duration / size / fps / audio presence, read from ffmpeg's own banner (no ffprobe dependency)."""
    proc = subprocess.run([ffmpeg.ffmpeg_path(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                          errors="replace", creationflags=_NO_WINDOW)
    text = proc.stderr
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
    v = re.search(r"Stream #\d+:\d+.*Video:.*?(\d{2,5})x(\d{2,5}).*?(\d+(?:\.\d+)?) fps", text)
    if not m or not v:
        raise RenderError(f"Cannot read media info from {path.name}.")
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return MediaInfo(dur, int(v.group(1)), int(v.group(2)), float(v.group(3)), "Audio:" in text)


def assemble(parts: list[Part], out: Path, *, width: int, height: int, fps: int, crf: int = 20,
             music: Path | None = None, music_gain_db: float = -16.0, loudness: float | None = -15.0) -> float:
    """Join ``parts`` into ``out``. Returns the duration of the result in seconds."""
    if not parts:
        raise RenderError("Nothing to assemble.")
    infos = [probe(p.path) for p in parts]
    args: list[str] = []
    for p in parts:
        args += ["-i", str(p.path)]
    graph: list[str] = []
    for i, info in enumerate(infos):
        graph.append(f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                     f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps},format=yuv420p,setsar=1,settb=AVTB[v{i}]")
        if info.has_audio:
            graph.append(f"[{i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                         f"apad,atrim=0:{info.duration:.3f},asetpts=PTS-STARTPTS[a{i}]")
        else:
            graph.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{info.duration:.3f},asetpts=PTS-STARTPTS[a{i}]")

    v_prev, a_prev, total = "v0", "a0", infos[0].duration
    for i in range(1, len(parts)):
        p, info = parts[i], infos[i]
        d = 0.0 if p.transition == "cut" else max(0.04, min(p.transition_s, info.duration / 2, infos[i - 1].duration / 2))
        if d:
            graph.append(f"[{v_prev}][v{i}]xfade=transition={p.transition}:duration={d:.3f}:offset={total - d:.3f}[x{i}]")
            graph.append(f"[{a_prev}][a{i}]acrossfade=d={d:.3f}:c1=tri:c2=tri[y{i}]")
        else:
            graph.append(f"[{v_prev}][v{i}]concat=n=2:v=1:a=0[x{i}]")
            graph.append(f"[{a_prev}][a{i}]concat=n=2:v=0:a=1[y{i}]")
        v_prev, a_prev, total = f"x{i}", f"y{i}", total + info.duration - d

    audio = a_prev
    if music is not None and music.is_file():
        args += ["-stream_loop", "-1", "-i", str(music)]
        mi = len(parts)
        fade_out = min(1.5, total / 4)
        graph.append(f"[{mi}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,atrim=0:{total:.3f},"
                     f"volume={music_gain_db}dB,afade=t=in:d=0.4,afade=t=out:st={total - fade_out:.3f}:d={fade_out:.3f}[m0]")
        # Duck the music under whatever the game / bumpers are doing, then sum.
        graph.append(f"[{audio}]asplit=2[main][key]")
        graph.append("[m0][key]sidechaincompress=threshold=0.03:ratio=6:attack=15:release=350[m1]")
        graph.append("[main][m1]amix=inputs=2:normalize=0:duration=first[mixed]")
        audio = "mixed"
    if loudness is not None:
        graph.append(f"[{audio}]loudnorm=I={loudness}:TP=-1.5:LRA=11,aresample=48000[aout]")
        audio = "aout"

    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run([*args, "-filter_complex", ";".join(graph), "-map", f"[{v_prev}]", "-map", f"[{audio}]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p", "-r", str(fps),
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-t", f"{total:.3f}", "-movflags", "+faststart",
                str(out)], "final assembly")
    return total
