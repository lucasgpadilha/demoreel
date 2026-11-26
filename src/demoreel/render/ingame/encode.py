"""From the raw files CS2's ``startmovie`` wrote to one mp4.

Where the game writes (CS Demo Manager, MIT: ``src/node/counter-strike/get-recording-folder-path.ts``,
``src/node/video/generation/get-sequence-raw-files.ts`` and ``delete-sequences-raw-files.ts``):

* frames: ``game/csgo/csdm/movie/<timestamp>/<name>%08d.tga`` - inside the plugin folder, because it is the
  first ``Game`` search path while the plugin is installed;
* audio: ``game/csgo/movie/<timestamp>/<name>.wav`` - "since the Armory update the wav files are created inside
  csgo/movie instead of csgo/csdm/movie", i.e. the frames folder with its last ``csdm`` component removed.

The ffmpeg invocation follows ``generate-video-with-ffmpeg.ts`` (``-framerate N -i <pattern> -i <wav> -vcodec
libx264 -acodec ... -pix_fmt yuv420p -crf N``) with additions that make the per-take files safe to join with
``ffmpeg.concat(..., reencode=False)``: fixed output size, AAC 48 kHz stereo, audio padded/cut to the exact
video length, and a silent track when the game produced no WAV.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import ffmpeg
from ..base import RenderCancelled, RenderError
from .settings import IngameSettings

AUDIO_SAMPLE_RATE = 48000
AUDIO_BITRATE = "192k"
# Uncompressed 32-bit TGA, the worst case of what startmovie writes: used for the disk space estimate.
TGA_BYTES_PER_PIXEL = 4
WAV_BYTES_PER_SECOND = 44100 * 2 * 2


FRAME_SUFFIXES = {".tga", ".jpg", ".jpeg"}


def tga_recording_dir(settings: IngameSettings) -> Path:
    """``game/csgo/csdm/movie`` (getRecordingFolderPath)."""
    return settings.plugin_install_dir / "movie"


def wav_recording_dir(settings: IngameSettings) -> Path:
    """``game/csgo/movie``."""
    return settings.csgo_dir / "movie"


def wav_dir_for(tga_dir: Path) -> Path:
    """Port of ``rawFilesFolderPath.replace(/csdm\\/(?!.*csdm\\/)/, '')``: drop the last ``csdm`` component."""
    parts = list(tga_dir.parts)
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == "csdm":
            del parts[index]
            return Path(*parts)
    return tga_dir


@dataclass
class RawTake:
    name: str
    tga_dir: Path
    prefix: str  # file name up to the frame number, with the casing found on disk
    digits: int
    first_frame: int
    frame_count: int
    wav_path: Path | None
    warnings: list[str] = field(default_factory=list)
    extension: str = "tga"  # as found on disk: tga, or jpg when recorded with ingame_frames=jpg

    @property
    def pattern(self) -> str:
        """ffmpeg image2 pattern; ``%`` in the folder name must be doubled."""
        folder = str(self.tga_dir).replace("%", "%%")
        return str(Path(folder) / f"{self.prefix.replace('%', '%%')}%0{self.digits}d.{self.extension}")


# The names demoreel gives its takes (actions.RecordSequence.name): `<n>-sequence`, frames `<n>-sequence<digits>`.
TAKE_FILE = re.compile(r"^\d+-sequence(?:\d+\.(?:tga|jpe?g)|\.wav)$", re.IGNORECASE)


@dataclass(frozen=True)
class RawScope:
    """What the two raw folders (``csdm/movie`` and ``csgo/movie``) held *before* this recording: the names of
    their top-level entries. Those belong to someone else - CS Demo Manager names its takes like ours and may
    keep their raw files, and ``csgo/movie`` is where anybody's ``startmovie`` writes - so they are never read
    as ours and never deleted. Empty = everything there is ours."""

    frames: frozenset[str] = frozenset()
    audio: frozenset[str] = frozenset()
    frames_root: bool = False  # csdm/movie itself existed (else it is ours, and goes once empty)
    audio_root: bool = False  # csgo/movie itself existed

    def to_dict(self) -> dict[str, Any]:
        return {"frames": sorted(self.frames), "audio": sorted(self.audio), "frames_root": self.frames_root,
                "audio_root": self.audio_root}

    @classmethod
    def from_dict(cls, data: dict | None) -> RawScope:
        data = data or {}
        return cls(frames=frozenset(map(str, data.get("frames") or ())),
                   audio=frozenset(map(str, data.get("audio") or ())),
                   frames_root=bool(data.get("frames_root")), audio_root=bool(data.get("audio_root")))


def snapshot_raw(settings: IngameSettings) -> RawScope:
    """The :class:`RawScope` of right now, taken before the game is started. Never raises."""
    def names(root: Path) -> frozenset[str]:
        try:
            return frozenset(p.name for p in root.iterdir()) if root.is_dir() else frozenset()
        except OSError:
            return frozenset()

    frames, audio = tga_recording_dir(settings), wav_recording_dir(settings)
    return RawScope(frames=names(frames), audio=names(audio), frames_root=frames.is_dir(),
                    audio_root=audio.is_dir())


def _ours(root: Path, keep: frozenset[str]) -> list[Path]:
    """Top-level entries of ``root`` that were not there before the recording."""
    try:
        return [p for p in root.iterdir() if p.name not in keep] if root.is_dir() else []
    except OSError:
        return []


def _walk(entries: Iterable[Path]) -> Iterator[Path]:
    for entry in entries:
        if entry.is_dir():
            yield from entry.rglob("*")
        else:
            yield entry


def _frame_files(recording_dir: Path, name: str,
                 scope: RawScope = RawScope()) -> dict[Path, list[tuple[int, str, int]]]:
    """``{folder: [(frame number, prefix as on disk, digit count), ...]}`` for ``<name><digits>.tga`` files."""
    pattern = re.compile(rf"^({re.escape(name)})(\d+)\.(?:tga|jpe?g)$", re.IGNORECASE)
    by_dir: dict[Path, list[tuple[int, str, int]]] = {}
    for path in _walk(_ours(recording_dir, scope.frames)):
        match = pattern.match(path.name)
        if match and path.is_file():
            by_dir.setdefault(path.parent, []).append((int(match.group(2)), match.group(1), len(match.group(2))))
    return by_dir


def frame_names(settings: IngameSettings, scope: RawScope = RawScope()) -> list[str]:
    """Lower-case file names of the frames this recording wrote so far (progress reporting). Never raises."""
    try:
        return [p.name.lower() for p in _walk(_ours(tga_recording_dir(settings), scope.frames))
                if p.suffix.lower() in FRAME_SUFFIXES]
    except OSError:
        return []


def find_wav(settings: IngameSettings, tga_dir: Path, name: str, scope: RawScope = RawScope()) -> Path | None:
    candidates = [wav_dir_for(tga_dir) / f"{name}.wav", tga_dir / f"{name}.wav"]
    audio_root = wav_recording_dir(settings)
    for candidate in candidates:
        try:
            older = candidate.relative_to(audio_root).parts[0] in scope.audio
        except (ValueError, IndexError):
            older = False
        if not older and candidate.is_file() and candidate.stat().st_size > 44:  # more than a bare RIFF header
            return candidate
    for root, keep in ((audio_root, scope.audio), (tga_recording_dir(settings), scope.frames)):
        for path in _walk(_ours(root, keep)):
            if path.name.lower() == f"{name}.wav".lower() and path.is_file() and path.stat().st_size > 44:
                return path
    return None


def find_raw_take(settings: IngameSettings, name: str, scope: RawScope = RawScope()) -> RawTake:
    """Locate the frames and the audio of the take recorded with ``startmovie <name>``."""
    recording_dir = tga_recording_dir(settings)
    by_dir = _frame_files(recording_dir, name, scope)
    if not by_dir:
        raise RenderError(f"CS2 wrote no frames for '{name}' in {recording_dir}. The plugin probably did not "
                          f"load (CS2 update?) or the game closed before recording.")
    warnings: list[str] = []
    tga_dir = max(by_dir, key=lambda d: d.stat().st_mtime) if len(by_dir) > 1 else next(iter(by_dir))
    if len(by_dir) > 1:
        warnings.append(f"Frames of '{name}' found in {len(by_dir)} folders; using the most recent one.")
    frames = sorted(by_dir[tga_dir])
    first, prefix, digits = frames[0]
    count = len(frames)
    if frames[-1][0] - first + 1 != count:
        # ffmpeg's image sequence reader stops at the first gap.
        contiguous = 1
        while contiguous < count and frames[contiguous][0] == first + contiguous:
            contiguous += 1
        warnings.append(f"Take '{name}' has missing frames; only the first {contiguous} of {count} are used.")
        count = contiguous
    wav = find_wav(settings, tga_dir, name, scope)
    if wav is None:
        warnings.append(f"No audio was recorded for '{name}'; the take is silent.")
    on_disk = next((p.suffix.lstrip(".") for p in tga_dir.iterdir()
                    if p.suffix.lower() in FRAME_SUFFIXES and p.name.lower().startswith(prefix.lower())), "tga")
    return RawTake(name=name, tga_dir=tga_dir, prefix=prefix, digits=digits, first_frame=first,
                   frame_count=count, wav_path=wav, warnings=warnings, extension=on_disk)


def even(value: int) -> int:
    """yuv420p needs even dimensions."""
    return max(2, int(value) - (int(value) % 2))


def video_filter(width: int, height: int, swap_red_blue: bool = False) -> str:
    w, h = even(width), even(height)
    filters = []
    if swap_red_blue:
        # Without CSDM's Vulkan layer, CS2 on Linux writes RGBA bytes into files that expect BGRA.
        filters.append("colorchannelmixer=rr=0:rb=1:br=1:bb=0")
    filters += [f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=bicubic",
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2", "setsar=1"]
    return ",".join(filters)


def build_encode_args(take: RawTake, out_path: Path, *, width: int, height: int, fps: int, crf: int,
                      swap_red_blue: bool = False) -> list[str]:
    """Arguments for :func:`demoreel.render.ffmpeg.run` (it adds ``-hide_banner -loglevel error -y``)."""
    duration = take.frame_count / float(fps)
    args = ["-framerate", str(int(fps)), "-start_number", str(take.first_frame), "-i", take.pattern]
    if take.wav_path is not None:
        args += ["-i", str(take.wav_path)]
    else:
        args += ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_SAMPLE_RATE}"]
    args += [
        "-map", "0:v:0", "-map", "1:a:0",
        "-frames:v", str(take.frame_count),
        "-vf", video_filter(width, height, swap_red_blue),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(int(crf)), "-pix_fmt", "yuv420p",
        "-r", str(int(fps)),
        "-af", "apad", "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", str(AUDIO_SAMPLE_RATE), "-ac", "2",
        "-t", f"{duration:.6f}",
        "-movflags", "+faststart", str(out_path),
    ]
    return args


def encode_take(take: RawTake, out_path: Path, *, width: int, height: int, fps: int, crf: int,
                swap_red_blue: bool = False) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(build_encode_args(take, out_path, width=width, height=height, fps=fps, crf=crf,
                                 swap_red_blue=swap_red_blue), f"encoding of {take.name}")
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise RenderError(f"ffmpeg produced no video for {take.name}.")
    return out_path


@dataclass
class EncodeResult:
    parts: list[Path]
    frames: int
    duration_s: float
    warnings: list[str]
    # Frames each take really has, in video order. A take can come out shorter than planned (it is cut at the
    # first missing frame): composition places every later take, its HUD and captions, from these numbers.
    take_frames: list[int] = field(default_factory=list)


def encode_takes(settings: IngameSettings, names: Sequence[str], work_dir: Path, *, width: int, height: int,
                 fps: int, crf: int, swap_red_blue: bool = False,
                 progress: Callable[[int, int], None] = lambda done, total: None,
                 cancelled: Callable[[], bool] = lambda: False, scope: RawScope = RawScope()) -> EncodeResult:
    """Encode every take (``names`` in final-video order) to ``work_dir/<name>.mp4``."""
    takes = [find_raw_take(settings, name, scope) for name in names]  # fail before encoding anything
    parts: list[Path] = []
    warnings: list[str] = []
    for index, take in enumerate(takes):
        if cancelled():
            raise RenderCancelled("Render cancelled while encoding.")
        progress(index, len(takes))
        parts.append(encode_take(take, work_dir / f"{take.name}.mp4", width=width, height=height, fps=fps,
                                 crf=crf, swap_red_blue=swap_red_blue))
        warnings.extend(take.warnings)
    progress(len(takes), len(takes))
    frames = sum(t.frame_count for t in takes)
    return EncodeResult(parts=parts, frames=frames, duration_s=frames / float(fps), warnings=warnings,
                        take_frames=[t.frame_count for t in takes])


def finalize(parts: Sequence[Path], out_path: Path, thumbnail_path: Path | None, duration_s: float) -> list[str]:
    """Join the takes into ``out_path`` and write the poster frame. Returns warnings."""
    warnings: list[str] = []
    ffmpeg.concat(list(parts), out_path)
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise RenderError("ffmpeg produced an empty video.")
    if thumbnail_path is not None:
        try:
            ffmpeg.thumbnail(out_path, thumbnail_path, at_s=min(1.0, duration_s / 2))
        except RenderError as exc:
            warnings.append(f"Thumbnail not written: {exc}")
    return warnings


def estimate_raw_bytes(frames: int, width: int, height: int, seconds: float) -> int:
    return int(frames * width * height * TGA_BYTES_PER_PIXEL + seconds * WAV_BYTES_PER_SECOND)


def _prune_empty_dirs(root: Path) -> None:
    if not root.is_dir():
        return
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def cleanup_raw(settings: IngameSettings, names: Iterable[str] = (), scope: RawScope = RawScope()) -> None:
    """Delete the raw frames and audio this recording wrote (they are huge). Never raises.

    Only files named like demoreel's takes (``<n>-sequence…``, plus ``names``) are deleted, and only in folders
    that appeared after ``scope`` was taken; then the folders that are left empty. What was in ``csdm/movie``
    and ``csgo/movie`` before - CS Demo Manager's raw files, the user's own ``startmovie`` folders, even empty
    ones - stays as it was. A raw folder that did not exist before goes once empty.
    """
    takes = [re.compile(rf"^{re.escape(name)}(?:\d+\.(?:tga|jpe?g)|\.wav)$", re.IGNORECASE) for name in names]

    def ours(path: Path) -> bool:
        return bool(TAKE_FILE.match(path.name)) or any(p.match(path.name) for p in takes)

    try:
        for root, keep, existed in ((tga_recording_dir(settings), scope.frames, scope.frames_root),
                                    (wav_recording_dir(settings), scope.audio, scope.audio_root)):
            entries = _ours(root, keep)
            for path in list(_walk(entries)):
                if path.is_file() and ours(path):
                    try:
                        path.unlink()
                    except OSError:
                        pass
            for entry in entries:
                if entry.is_dir():
                    _prune_empty_dirs(entry)
            if not existed:
                try:
                    root.rmdir()
                except OSError:
                    pass  # not empty
        try:
            settings.plugin_install_dir.rmdir()  # only succeeds once the plugin binaries are gone too
        except OSError:
            pass
    except Exception:  # noqa: BLE001 - cleanup is best effort
        pass


# --- HLAE takes: already encoded video + a WAV ---------------------------------------------------------

def hlae_take_files(hlae_dir: Path, name: str) -> tuple[Path, Path | None]:
    """``(video, wav or None)`` of one take recorded by HLAE (see ``actions.hlae_setup_commands``)."""
    take = Path(hlae_dir) / name
    video = take / "video.mp4"
    if not video.is_file() or video.stat().st_size == 0:
        raise RenderError(f"HLAE recorded no video for {name} (expected {video}). Check that HLAE supports the "
                          "installed CS2 version and that its ffmpeg path is set.")
    wavs = sorted(take.glob("take*/audio.wav"))
    return video, (wavs[-1] if wavs else None)


def hlae_bytes(hlae_dir: Path) -> tuple[int, int]:
    """``(takes with a video file, bytes written so far)``: the only progress signal while HLAE records."""
    count = size = 0
    try:
        for video in Path(hlae_dir).glob("*/video.mp4"):
            count += 1
            size += video.stat().st_size
    except OSError:
        pass
    return count, size


_STREAM_SIZE = re.compile(r"Video:.*?\b(\d{2,5})x(\d{2,5})\b")
_FRAME_COUNT = re.compile(r"frame=\s*(\d+)")


def video_info(path: Path) -> tuple[int, int, int]:
    """``(width, height, frames)`` of a video file, by demuxing it (no decode)."""
    proc = subprocess.run([ffmpeg.ffmpeg_path(), "-hide_banner", "-i", str(path), "-map", "0:v:0", "-c", "copy",
                           "-f", "null", "-"], capture_output=True, text=True, errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    size = _STREAM_SIZE.search(proc.stderr)
    frames = _FRAME_COUNT.findall(proc.stderr)
    if proc.returncode != 0 or not size or not frames:
        raise RenderError(f"Could not read {path.name}: {proc.stderr.strip()[-400:]}")
    return int(size.group(1)), int(size.group(2)), int(frames[-1])


@dataclass
class HlaeResult(EncodeResult):
    """What :func:`mux_hlae_takes` made; ``take_frames`` are the frames HLAE really wrote."""


def mux_hlae_takes(hlae_dir: Path, names: Sequence[str], work_dir: Path, *, width: int, height: int, fps: int,
                   crf: int, progress: Callable[[int, int], None] = lambda done, total: None,
                   cancelled: Callable[[], bool] = lambda: False) -> HlaeResult:
    """Turn HLAE's takes into parts :func:`finalize` can join: the picture is copied as recorded (re-encoded
    only when its size is not the requested one), the sound is encoded and cut to the picture's length."""
    files = [hlae_take_files(hlae_dir, name) for name in names]  # fail before muxing anything
    parts: list[Path] = []
    warnings: list[str] = []
    take_frames: list[int] = []
    w, h = even(width), even(height)
    for index, (name, (video, wav)) in enumerate(zip(names, files, strict=True)):
        if cancelled():
            raise RenderCancelled("Render cancelled while encoding.")
        progress(index, len(files))
        vw, vh, frames = video_info(video)
        duration = frames / float(fps)
        out = Path(work_dir) / f"{name}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        args = ["-r", str(int(fps)), "-i", str(video)]
        if wav is not None:
            args += ["-i", str(wav)]
        else:
            warnings.append(f"{name}: no sound was recorded.")
            args += ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_SAMPLE_RATE}"]
        args += ["-map", "0:v:0", "-map", "1:a:0"]
        if (vw, vh) == (w, h):
            args += ["-c:v", "copy"]
        else:
            warnings.append(f"{name}: recorded at {vw}x{vh}, scaled to {w}x{h}.")
            args += ["-vf", video_filter(w, h), "-c:v", "libx264", "-preset", "veryfast", "-crf", str(int(crf)),
                     "-pix_fmt", "yuv420p"]
        args += ["-af", "apad", "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", str(AUDIO_SAMPLE_RATE), "-ac", "2",
                 "-t", f"{duration:.6f}", "-movflags", "+faststart", str(out)]
        ffmpeg.run(args, f"muxing of {name}")
        if not out.is_file() or out.stat().st_size == 0:
            raise RenderError(f"ffmpeg produced no video for {name}.")
        parts.append(out)
        take_frames.append(frames)
    progress(len(files), len(files))
    total = sum(take_frames)
    return HlaeResult(parts=parts, frames=total, duration_s=total / float(fps), warnings=warnings,
                      take_frames=take_frames)
