"""Raw-file discovery, ffmpeg command construction and a tiny real encode (generated frames, bundled ffmpeg)."""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

from demoreel.render import ffmpeg
from demoreel.render.base import RenderCancelled, RenderError
from demoreel.render.ingame import encode
from demoreel.render.ingame.settings import IngameSettings

STAMP = "2026_11_14_21_30_00"


def make_settings(tmp_path: Path, platform: str = "win32") -> IngameSettings:
    cs2 = tmp_path / "cs2"
    (cs2 / "game" / "csgo").mkdir(parents=True, exist_ok=True)
    return IngameSettings(cs2_dir=cs2, steam_dir=None, steam_exe=None, plugin_dir=tmp_path / "plugins",
                          data_dir=tmp_path / "data" / "ingame", platform=platform)


def write_wav(path: Path, seconds: float, rate: int = 44100) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        samples = (int(8000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(int(seconds * rate)))
        wav.writeframes(b"".join(struct.pack("<hh", s, s) for s in samples))


def write_take(settings: IngameSettings, name: str, frames: int, *, first: int = 1, size=(64, 36),
               audio_s: float | None = 0.5, real_images: bool = False) -> Path:
    """Lay files out the way CS2 does: frames in csgo/csdm/movie/<stamp>, audio in csgo/movie/<stamp>."""
    tga_dir = encode.tga_recording_dir(settings) / STAMP
    tga_dir.mkdir(parents=True, exist_ok=True)
    for i in range(frames):
        path = tga_dir / f"{name}{first + i:08d}.tga"
        if real_images:
            from PIL import Image

            Image.new("RGB", size, (255, 40 * (i % 6), 0)).save(path, format="TGA")
        else:
            path.write_bytes(b"tga")
    if audio_s is not None:
        write_wav(encode.wav_recording_dir(settings) / STAMP / f"{name}.wav", audio_s)
    return tga_dir


# --- where the game writes --------------------------------------------------------------------------------


def test_recording_folders(tmp_path):
    settings = make_settings(tmp_path)
    assert encode.tga_recording_dir(settings) == tmp_path / "cs2" / "game" / "csgo" / "csdm" / "movie"
    assert encode.wav_recording_dir(settings) == tmp_path / "cs2" / "game" / "csgo" / "movie"


def test_wav_folder_is_the_frames_folder_without_its_last_csdm_component():
    frames = Path("/games/csdm/Counter-Strike Global Offensive/game/csgo/csdm/movie/2026_11_14")
    assert encode.wav_dir_for(frames) == Path("/games/csdm/Counter-Strike Global Offensive/game/csgo/movie/2026_11_14")
    assert encode.wav_dir_for(Path("/tmp/movie/x")) == Path("/tmp/movie/x")


def test_find_raw_take_locates_frames_and_audio(tmp_path):
    settings = make_settings(tmp_path)
    tga_dir = write_take(settings, "1-sequence", 5, first=1)
    write_take(settings, "11-sequence", 3, audio_s=None)  # must not be mistaken for take 1
    take = encode.find_raw_take(settings, "1-sequence")
    assert (take.tga_dir, take.first_frame, take.frame_count, take.digits) == (tga_dir, 1, 5, 8)
    assert take.pattern == str(tga_dir / "1-sequence%08d.tga")
    assert take.wav_path == encode.wav_recording_dir(settings) / STAMP / "1-sequence.wav"
    assert take.warnings == []
    assert sorted(encode.frame_names(settings))[0] == "1-sequence00000001.tga"
    assert len(encode.frame_names(settings)) == 8

    silent = encode.find_raw_take(settings, "11-sequence")
    assert silent.frame_count == 3 and silent.wav_path is None
    assert any("silent" in w for w in silent.warnings)


def test_find_raw_take_reports_missing_frames_and_gaps(tmp_path):
    settings = make_settings(tmp_path)
    with pytest.raises(RenderError, match="wrote no frames for '1-sequence'"):
        encode.find_raw_take(settings, "1-sequence")
    assert encode.frame_names(settings) == []

    tga_dir = write_take(settings, "2-sequence", 6, first=0)
    (tga_dir / "2-sequence00000003.tga").unlink()
    take = encode.find_raw_take(settings, "2-sequence")
    assert (take.first_frame, take.frame_count) == (0, 3)
    assert any("missing frames" in w for w in take.warnings)


# --- ffmpeg command ---------------------------------------------------------------------------------------


def test_encode_args_with_audio(tmp_path):
    settings = make_settings(tmp_path)
    write_take(settings, "1-sequence", 45, first=1)
    take = encode.find_raw_take(settings, "1-sequence")
    args = encode.build_encode_args(take, tmp_path / "out.mp4", width=1280, height=720, fps=30, crf=18)

    assert args[:6] == ["-framerate", "30", "-start_number", "1", "-i", take.pattern]
    assert args[6:8] == ["-i", str(take.wav_path)]
    assert args[-1] == str(tmp_path / "out.mp4")

    def value(flag: str) -> str:
        return args[args.index(flag) + 1]

    assert value("-c:v") == "libx264" and value("-crf") == "18" and value("-pix_fmt") == "yuv420p"
    assert value("-c:a") == "aac" and value("-ar") == "48000" and value("-ac") == "2"
    assert value("-frames:v") == "45" and value("-t") == "1.500000" and value("-r") == "30"
    assert value("-vf") == ("scale=1280:720:force_original_aspect_ratio=decrease:flags=bicubic,"
                            "pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1")
    assert value("-movflags") == "+faststart"
    assert "-y" not in args  # ffmpeg.run() adds it


def test_encode_args_without_audio_use_a_silent_track_and_optional_channel_swap(tmp_path):
    settings = make_settings(tmp_path, platform="linux")
    write_take(settings, "3-sequence", 10, audio_s=None)
    take = encode.find_raw_take(settings, "3-sequence")
    args = encode.build_encode_args(take, tmp_path / "o.mp4", width=1281, height=721, fps=60, crf=20,
                                    swap_red_blue=True)
    assert args[6:10] == ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
    vf = args[args.index("-vf") + 1]
    assert vf.startswith("colorchannelmixer=rr=0:rb=1:br=1:bb=0,scale=1280:720:")  # odd sizes are made even
    assert args[args.index("-map") + 1] == "0:v:0"


def test_percent_signs_in_the_folder_are_escaped_for_the_image_pattern(tmp_path):
    take = encode.RawTake(name="1-sequence", tga_dir=tmp_path / "100% movie", prefix="1-sequence", digits=8,
                          first_frame=0, frame_count=1, wav_path=None)
    assert take.pattern == str(tmp_path / "100%% movie" / "1-sequence%08d.tga")


def test_disk_estimate():
    one_minute = encode.estimate_raw_bytes(30 * 60, 1280, 720, 60)
    assert 6.0 < one_minute / 1024**3 < 6.5


# --- cleanup ----------------------------------------------------------------------------------------------


def test_cleanup_removes_frames_and_only_our_wav_files(tmp_path):
    settings = make_settings(tmp_path)
    write_take(settings, "1-sequence", 3)
    users_wav = encode.wav_recording_dir(settings) / "mine" / "my-own-movie.wav"
    write_wav(users_wav, 0.1)
    encode.cleanup_raw(settings, ["1-sequence", "2-sequence"])
    assert not encode.tga_recording_dir(settings).exists()
    assert not settings.plugin_install_dir.exists()  # empty plugin folder removed
    assert not (encode.wav_recording_dir(settings) / STAMP).exists()
    assert users_wav.is_file()
    encode.cleanup_raw(settings, ["1-sequence"])  # idempotent, never raises


# --- real encode ------------------------------------------------------------------------------------------


def test_takes_are_encoded_joined_and_thumbnailed_with_real_ffmpeg(tmp_path):
    settings = make_settings(tmp_path)
    write_take(settings, "1-sequence", 12, first=1, real_images=True, audio_s=0.2)
    write_take(settings, "2-sequence", 9, first=0, real_images=True, audio_s=None)  # silent take
    progress: list[tuple[int, int]] = []

    result = encode.encode_takes(settings, ["1-sequence", "2-sequence"], tmp_path / "work", width=128,
                                 height=72, fps=30, crf=30, progress=lambda d, t: progress.append((d, t)))
    assert [p.name for p in result.parts] == ["1-sequence.mp4", "2-sequence.mp4"]
    assert all(p.stat().st_size > 0 for p in result.parts)
    assert result.frames == 21 and result.duration_s == pytest.approx(0.7)
    assert progress == [(0, 2), (1, 2), (2, 2)]

    out, thumb = tmp_path / "final" / "reel.mp4", tmp_path / "final" / "reel.jpg"
    warnings = encode.finalize(result.parts, out, thumb, result.duration_s)
    assert warnings == []
    assert out.stat().st_size > 0 and thumb.stat().st_size > 0

    # Both streams made it through the stream-copy concat, at the requested size.
    import subprocess

    probe = subprocess.run([ffmpeg.ffmpeg_path(), "-hide_banner", "-i", str(out)], capture_output=True, text=True)
    assert "Video: h264" in probe.stderr and "128x72" in probe.stderr
    assert "Audio: aac" in probe.stderr and "48000 Hz" in probe.stderr


def test_an_ffmpeg_error_about_a_unicode_file_name_is_reported_not_lost_in_decoding(tmp_path):
    """ffmpeg writes UTF-8; read with the Windows code page, the byte 0x81 of "ā" raised UnicodeDecodeError."""
    missing = tmp_path / "nāo-existe.mp4"
    with pytest.raises(RenderError, match="probe failed") as excinfo:
        ffmpeg.run(["-i", str(missing), "-f", "null", "-"], "probe")
    assert "nāo-existe" in str(excinfo.value)


def test_encode_fails_before_touching_ffmpeg_when_a_take_is_missing_and_honours_cancel(tmp_path):
    settings = make_settings(tmp_path)
    write_take(settings, "1-sequence", 3, real_images=True)
    with pytest.raises(RenderError, match="2-sequence"):
        encode.encode_takes(settings, ["1-sequence", "2-sequence"], tmp_path / "work", width=64, height=36,
                            fps=30, crf=30)
    assert not (tmp_path / "work" / "1-sequence.mp4").exists()
    with pytest.raises(RenderCancelled):
        encode.encode_takes(settings, ["1-sequence"], tmp_path / "work", width=64, height=36, fps=30, crf=30,
                            cancelled=lambda: True)
