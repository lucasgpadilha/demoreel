"""``ingame`` renderer: records first-person footage with the real CS2 client.

Pipeline (the recording backend is CS2's own ``startmovie``, driven the way CS Demo Manager does it, see
``docs/ingame-recording.md``):

1. read the players' spectator slots from the demo, plan the takes and write ``<demo>.json`` (``actions.py``);
2. install the user-provided CSDM plugin and patch ``gameinfo.gi`` for the session (``plugin.py``);
3. start CS2 with ``-insecure +playdemo`` and wait for it to quit, with a watchdog (``launcher.py``);
4. encode the TGA frames + WAV of each take, join them, delete the raw files (``encode.py``).

Per-job knobs in ``RenderOptions.extra`` (all optional): ``ingame_hud`` ("deathnotices" | "full"),
``ingame_xray`` ("auto" | bool, see ``xray.py``), ``ingame_assists``, ``ingame_player_voices``, ``ingame_true_view`` (bools),
``ingame_commands`` (list of console commands run before each take), ``ingame_launch_args`` (list),
``ingame_keep_raw``, ``ingame_swap_red_blue`` (bools), ``ingame_lock_wait_s``, ``ingame_startup_timeout_s``,
``ingame_stall_timeout_s`` (seconds) and ``ingame_backend`` (only "startmovie" exists).
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .. import ffmpeg
from ..base import Availability, RenderCancelled, RenderError, RenderJob, RenderResult
from . import actions, encode, launcher, plugin, procs, slots, staging, xray
from .settings import IngameSettings
from .settings import load as load_settings

# "startmovie": the game's own recorder (image files, then encoded) - works on Windows and Linux.
# "hlae": HLAE's recorder pipes frames straight into ffmpeg - Windows only, several times faster.
BACKENDS = ("startmovie", "hlae")
HLAE_ENCODERS = {
    "x264": "-c:v libx264 -preset veryfast -crf 17 -pix_fmt yuv420p",
    "nvenc": "-c:v h264_nvenc -preset p5 -rc constqp -qp 19 -pix_fmt yuv420p",
}
MIN_FREE_BYTES = 2 * 1024**3
DISK_HEADROOM_BYTES = 1024**3


class _GameDidNotQuit(RenderError):
    """Every frame was written but the game is still open (the final ``quit`` action did not run)."""


def _flag(extra: dict[str, Any], key: str, default: bool) -> bool:
    value = extra.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _number(extra: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(extra.get(key, default))
    except (TypeError, ValueError):
        return default


def _console_commands(value: Any) -> tuple[str, ...]:
    """``ingame_commands``: a list of commands, or one string with a command per line / separated by ``;``."""
    if value is None:
        return ()
    items = re.split(r"[\n;]", value) if isinstance(value, str) else [str(v) for v in value]
    return tuple(item.strip() for item in items if item.strip())


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return shlex.split(value)
    if isinstance(value, Iterable):
        return [str(v) for v in value]
    return []


class IngameRenderer:
    name = "ingame"
    description = ("First-person footage recorded with the real CS2 client (needs Windows or Linux with CS2, "
                   "Steam running and CS Demo Manager's CS2 plugin; one recording at a time).")

    def __init__(self, settings: IngameSettings | None = None, *,
                 run_game: Callable[..., launcher.GameRun] = launcher.run_game,
                 resolve_slots: Callable[[Path, Iterable[int]], actions.SlotLookup] = slots.resolve_slots,
                 run_loader: Callable[..., launcher.GameRun] = launcher.run_loader,
                 is_cs2_running: Callable[[], bool | None] = procs.is_cs2_running,
                 is_steam_running: Callable[[], bool | None] = procs.is_steam_running,
                 dedicated_servers: Callable[[], list[procs.GameProcess]] = procs.dedicated_servers) -> None:
        self._settings = settings
        self._run_game = run_game
        self._run_loader = run_loader
        self._resolve_slots = resolve_slots
        self._is_cs2_running = is_cs2_running
        self._is_steam_running = is_steam_running
        self._dedicated_servers = dedicated_servers

    def _shared_install(self, settings: IngameSettings) -> str | None:
        """Why recording must not start when a dedicated server runs from the very folder the game is recorded
        from; ``None`` when no server shares it. Servers from another copy of CS2 are no concern."""
        if settings.cs2_dir is None:
            return None
        try:
            root = settings.cs2_dir.resolve()
            shared = [p.pid for p in self._dedicated_servers()
                      if p.exe and Path(p.exe).resolve().is_relative_to(root)]
        except Exception:  # noqa: BLE001 - a broken process listing must not block recording
            return None
        if not shared:
            return None
        return (f"A dedicated game server (process {', '.join(map(str, shared))}) runs from the same CS2 folder "
                f"demoreel records with ({root}). Recording changes files of that folder for a moment "
                "(gameinfo.gi and a plugin): a server that starts or restarts meanwhile would load them, and the "
                "game would load the server's own add-ons. Record from a second copy of CS2 - install the game in "
                "another Steam library and point DEMOREEL_CS2_DIR at it - or stop the server while recording.")

    def settings(self) -> IngameSettings:
        return self._settings if self._settings is not None else load_settings()

    # --- availability ---------------------------------------------------------------------------------

    def availability(self) -> Availability:
        try:
            return self._availability(self.settings())
        except Exception as exc:  # noqa: BLE001 - the contract says: never raise
            return Availability(ok=False, reason=f"In-game renderer check failed: {exc}")

    def _static_problems(self, settings: IngameSettings, details: dict[str, Any]) -> list[str]:
        """Everything that does not depend on what is running right now."""
        problems: list[str] = []
        details.update({"platform": settings.platform, "backend": self._backend(settings, {}),
                        "hlae": str(settings.hlae_exe) if settings.hlae_available else None,
                        "plugin_dir": str(settings.plugin_dir)})
        if not (settings.is_windows or settings.is_linux):
            return [f"The in-game renderer needs Windows or Linux (CS2 does not run on {settings.platform})."]

        if settings.cs2_dir is None:
            problems.append("CS2 was not found. Install it with Steam or set DEMOREEL_CS2_DIR to the "
                            "'Counter-Strike Global Offensive' folder.")
            return problems
        details["cs2_dir"] = str(settings.cs2_dir)
        if not settings.game_executable.is_file():
            problems.append(f"CS2 executable not found at {settings.game_executable}.")
        if not settings.gameinfo_path.is_file():
            problems.append(f"{settings.gameinfo_path} is missing; verify the game files in Steam.")

        cs2_patch = plugin.read_cs2_patch_version(settings)
        details["cs2_patch"] = cs2_patch
        binary = plugin.select_plugin_binary(settings.plugin_dir, settings.platform, cs2_patch,
                                             settings.plugin_version)
        if binary is None:
            problems.append(plugin.missing_plugin_instruction(settings, cs2_patch))
        else:
            details.update({"plugin": str(binary.path), "plugin_version": binary.version,
                            "vulkan_layer": str(binary.vulkan_layer) if binary.vulkan_layer else None})

        if settings.is_linux:
            if settings.runtime_script is None or not settings.runtime_script.is_file():
                problems.append("Steam Linux Runtime 3.0 (sniper) not found; install it from Steam (Tools) or "
                                "set DEMOREEL_INGAME_RUNTIME_SCRIPT to its _v2-entry-point.")
            if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
                problems.append("No display: CS2 needs a desktop session (DISPLAY or WAYLAND_DISPLAY).")

        try:
            ffmpeg.ffmpeg_path()
        except Exception as exc:  # noqa: BLE001
            problems.append(str(exc))
        return problems

    def _availability(self, settings: IngameSettings) -> Availability:
        details: dict[str, Any] = {}
        problems = self._static_problems(settings, details)

        if settings.cs2_dir is not None:
            try:
                free = shutil.disk_usage(settings.cs2_dir).free
                per_second = encode.estimate_raw_bytes(30, 1280, 720, 1.0)
                details["free_disk_gb"] = round(free / 1024**3, 1)
                details["raw_gb_per_minute_720p30"] = round(per_second * 60 / 1024**3, 1)
                if free < MIN_FREE_BYTES:
                    problems.append(f"Only {details['free_disk_gb']} GB free on the CS2 drive; raw frames need "
                                    f"about {details['raw_gb_per_minute_720p30']} GB per minute at 720p30.")
            except OSError:
                pass

        steam = self._is_steam_running()
        details["steam_running"] = steam
        if steam is False:
            problems.append("Steam is not running. Start Steam and log in.")

        recording = False
        try:
            recording = launcher.RecordingLock(settings.lock_path).is_held_elsewhere()
        except Exception:  # noqa: BLE001
            recording = False
        details["recording_in_progress"] = recording
        cs2 = self._is_cs2_running()
        details["cs2_running"] = cs2
        if cs2 and not recording:
            problems.append("CS2 is already running; close the game to record. (Dedicated servers do not count; "
                            "a cs2 process started by another user or as administrator cannot be told apart "
                            "and is taken for the game.)")
        try:
            details["dedicated_servers"] = len(self._dedicated_servers())
        except Exception:  # noqa: BLE001
            details["dedicated_servers"] = None
        shared = self._shared_install(settings)
        if shared:
            problems.append(shared)

        details["problems"] = problems
        return Availability(ok=not problems, reason=problems[0] if problems else "", details=details)

    # --- render ------------------------------------------------------------------------------------------

    def render(self, job: RenderJob) -> RenderResult:
        settings = self.settings()
        extra = job.options.extra or {}
        self._backend(settings, extra)  # reject an unknown backend before anything else

        problems = self._static_problems(settings, {})
        if problems:
            raise RenderError(problems[0])
        if not job.clips or not any(clip.segments for clip in job.clips):
            raise RenderError("Nothing to render: no clips.")

        wait_s = _number(extra, "ingame_lock_wait_s", 1800.0)
        with launcher.recording_lock(
                settings.lock_path, wait_s, job.cancelled,
                on_wait=lambda waited: job.progress(0.01, "Waiting for another in-game recording to finish")):
            return self._render_locked(job, settings)

    @staticmethod
    def _backend(settings: IngameSettings, extra: dict[str, Any]) -> str:
        backend = str(extra.get("ingame_backend") or settings.backend or "auto").lower()
        if backend == "auto":  # HLAE records several times faster; use it wherever it is installed
            backend = "hlae" if settings.hlae_available else "startmovie"
        if backend not in BACKENDS:
            raise RenderError(f"Unknown in-game recording backend {backend!r}. Available: {', '.join(BACKENDS)}.")
        return backend

    def _render_locked(self, job: RenderJob, settings: IngameSettings) -> RenderResult:
        options = job.options
        extra = options.extra or {}
        backend = self._backend(settings, extra)
        hlae = backend == "hlae"
        hlae_dir = Path(os.path.abspath(job.work_dir)) / "hlae"
        warnings: list[str] = []
        width, height, fps = encode.even(options.width), encode.even(options.height), max(1, int(options.fps))

        launcher.assert_can_start(self._is_cs2_running, self._is_steam_running)
        shared = self._shared_install(settings)
        if shared:
            raise RenderError(shared)
        if plugin.recover_gameinfo(settings.gameinfo_path) == "restored":
            warnings.append("gameinfo.gi was still patched from a previous run that crashed; it was restored.")

        cs2_patch = plugin.read_cs2_patch_version(settings)
        binary = plugin.select_plugin_binary(settings.plugin_dir, settings.platform, cs2_patch,
                                             settings.plugin_version)
        if binary is None:  # checked by _static_problems, but the folder may have changed since
            raise RenderError(plugin.missing_plugin_instruction(settings, cs2_patch))

        # 1. Plan ------------------------------------------------------------------------------------
        job.progress(0.02, "Reading player slots from the demo")
        tickrate = float(job.analysis.tickrate or 64)
        max_tick = int(job.analysis.total_ticks or 0) or None
        pov_ticks = [seg.start_tick for clip in job.clips for seg in clip.segments if seg.pov_steamid]
        slot_lookup: actions.SlotLookup | None = None
        if pov_ticks:
            try:
                slot_lookup = self._resolve_slots(job.demo_path, [max(actions.FIRST_RECORD_TICK, t)
                                                                  for t in pov_ticks])
            except (RenderError, KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001 - demoparser2 panics surface as BaseException
                raise RenderError(f"Could not read the players' slots from the demo: {exc}") from exc
        plan = actions.plan_sequences(job.clips, slot_lookup, max_tick=max_tick)
        warnings.extend(plan.warnings)
        if not plan.sequences:
            raise RenderError("Nothing to record: every segment is empty or outside the demo. "
                              + " ".join(plan.warnings))
        xray_mode = xray.mode(extra)
        if xray_mode == "auto":
            plan.sequences[:] = actions.with_xray(plan.sequences, job.clips,
                                                  xray.windows(xray.match_kills(job.analysis), tickrate))
        demo_patch = slots.demo_patch_version(job.demo_path)
        if demo_patch is not None and cs2_patch is not None and demo_patch != cs2_patch:
            warnings.append(f"The demo was recorded with CS2 patch {demo_patch} but patch {cs2_patch} is "
                            f"installed; playback may fail or look wrong.")

        # JPEG frames by default: on the reference machine they record 4x faster than TGA (31 vs 7.6 frames/s,
        # no disk stalls) and take 60x less space, with no visible difference after the final encode.
        # `ingame_frames: "tga"` keeps the lossless path.
        frame_format = str(extra.get("ingame_frames", "jpg")).lower()
        if frame_format not in {"jpg", "tga"}:
            raise RenderError("ingame_frames must be 'jpg' or 'tga'.")
        action_options = actions.ActionOptions(
            fps=fps, tickrate=tickrate,
            only_death_notices=str(extra.get("ingame_hud", "deathnotices")).lower() != "full",
            show_xray=xray_mode == "on",
            show_assists=_flag(extra, "ingame_assists", True),
            player_voices=_flag(extra, "ingame_player_voices", False),
            true_view=_flag(extra, "ingame_true_view", False),
            extra_commands=_console_commands(extra.get("ingame_commands")),
            recording_system=backend, max_tick=max_tick,
            frame_format=frame_format, hlae_output_dir=hlae_dir.as_posix(),
            hlae_ffmpeg_args=self._hlae_encoder(extra, settings))
        names = [seq.name for seq in plan.sequences]  # final-video order
        # One game launch per batch: a take that plays past the end of the match must be the last of its launch.
        rounds = getattr(job.analysis, "rounds", None) or []
        batches = actions.recording_batches(plan.sequences, int(rounds[-1].end_tick) if rounds else None)
        expected = actions.expected_frames(plan.sequences, fps, tickrate)
        clip_seconds = expected / float(fps)

        raw_bytes = encode.estimate_raw_bytes(expected, width, height, clip_seconds)
        if frame_format == "jpg" or hlae:  # measured: 61 KB per 720p frame against 3.7 MB as TGA; budget generously
            raw_bytes //= 12
        needed = int(raw_bytes * 1.1) + DISK_HEADROOM_BYTES
        try:
            free = shutil.disk_usage(settings.cs2_dir).free  # type: ignore[arg-type]
        except OSError:
            free = needed
        if free < needed:
            raise RenderError(f"Not enough disk space on the CS2 drive for the raw frames: about "
                              f"{needed / 1024**3:.1f} GB needed, {free / 1024**3:.1f} GB free. Render fewer or "
                              f"shorter clips, or lower the resolution / fps.")

        # 2. Files the game reads ------------------------------------------------------------------------
        staged = staging.stage_demo(job.demo_path, job.work_dir)
        keep_raw = _flag(extra, "ingame_keep_raw", False)
        game: launcher.GameRun | None = None  # the last launch
        game_seconds = 0.0  # all launches
        installed: plugin.InstalledPlugin | None = None
        try:
            encode.cleanup_raw(settings, names)  # stale frames of an aborted run would end up in the video
            settings.cfg_dir.mkdir(parents=True, exist_ok=True)
            game_args = launcher.build_launch_args(staged.path, width, height,
                                                   _string_list(extra.get("ingame_launch_args")))
            if hlae:
                command = launcher.build_hlae_command(settings, game_args)
                launcher.register_hlae_ffmpeg(settings.hlae_exe, ffmpeg.ffmpeg_path())  # type: ignore[arg-type]
                shutil.rmtree(hlae_dir, ignore_errors=True)
                hlae_dir.mkdir(parents=True, exist_ok=True)
            else:
                command = launcher.build_command(settings, game_args)

            startup_timeout_s = _number(extra, "ingame_startup_timeout_s", 300.0)
            stall_timeout_s = _number(extra, "ingame_stall_timeout_s", 300.0)

            # 3. Record ----------------------------------------------------------------------------------
            recorded = 0
            for number, batch in enumerate(batches, start=1):
                if job.cancelled():
                    raise RenderCancelled("Render cancelled.")
                staged.write_actions(actions.to_json(actions.build_actions(batch, action_options)))
                recorded += len(batch)
                batch_seconds = actions.expected_frames(batch, fps, tickrate) / float(fps)
                timeout_s = settings.timeout_s or (240.0 + 90.0 * len(batch) + 12.0 * batch_seconds)
                if hlae:
                    on_poll = self._hlae_poller(job, hlae_dir, len(plan.sequences), startup_timeout_s,
                                                stall_timeout_s, done_at=recorded)
                else:
                    on_poll = self._progress_poller(
                        job, settings, expected, startup_timeout_s, stall_timeout_s, last_name=batch[-1].name,
                        last_expected=actions.expected_frames([batch[-1]], fps, tickrate))
                if number == 1:
                    job.progress(0.05, "Starting CS2")
                run: launcher.GameRun | None = None
                with plugin.plugin_session(settings, binary) as installed:
                    try:
                        run = (self._run_loader if hlae else self._run_game)(
                            command, env=launcher.build_env(settings, installed.env), timeout_s=timeout_s,
                            cancelled=job.cancelled, on_poll=on_poll, log_path=job.work_dir / "cs2-output.log",
                            kill_names=procs.CS2_PROCESS_NAMES)
                    except _GameDidNotQuit as exc:
                        warnings.append(str(exc))
                    # gameinfo.gi is restored and the plugin removed when this block ends: let the game go first.
                    wait_until_gone(self._is_cs2_running)
                if run is not None and run.returncode != 0 and run.duration_s < 2.0:
                    raise RenderError(f"CS2 failed to start (exit code {run.returncode}). {run.output_tail[-600:]}")
                if run is not None:
                    game_seconds += run.duration_s
                    game = run

            # 4. Encode ----------------------------------------------------------------------------------
            has_layer = installed is not None and installed.vulkan_layer_installed
            swap = _flag(extra, "ingame_swap_red_blue", settings.is_linux and not has_layer)
            take_frames: list[int] | None = None
            try:
                if hlae:
                    encoded = encode.mux_hlae_takes(
                        hlae_dir, names, job.work_dir / "takes", width=width, height=height, fps=fps,
                        crf=options.crf, cancelled=job.cancelled,
                        progress=lambda done, total: job.progress(
                            0.70 + 0.25 * done / max(1, total), f"Joining take {min(done + 1, total)}/{total}"))
                    take_frames = encoded.take_frames
                else:
                    encoded = encode.encode_takes(
                        settings, names, job.work_dir / "takes", width=width, height=height, fps=fps,
                        crf=options.crf, swap_red_blue=swap, cancelled=job.cancelled,
                        progress=lambda done, total: job.progress(
                            0.70 + 0.25 * done / max(1, total), f"Encoding take {min(done + 1, total)}/{total}"))
            except RenderCancelled:
                raise
            except RenderError as exc:
                raise RenderError(self._with_diagnostics(str(exc), settings, game)) from exc
            warnings.extend(encoded.warnings)
            job.progress(0.96, "Joining takes")
            warnings.extend(encode.finalize(encoded.parts, job.out_path, job.thumbnail_path, encoded.duration_s))
            plugin.delete_plugin_logs(settings)  # only kept after a failure, where the error message quotes it
        finally:
            staged.cleanup()
            if not keep_raw:
                shutil.rmtree(hlae_dir, ignore_errors=True)
                encode.cleanup_raw(settings, names)
                plugin.remove_game_generated(settings)  # the folder can only go once the raw frames are gone

        job.progress(1.0, "Done")
        return RenderResult(
            duration_s=encoded.duration_s, width=width, height=height, fps=fps,
            details={
                "renderer": self.name, "backend": backend, "sequences": len(plan.sequences),
                "frames": encoded.frames, "expected_frames": expected, "plugin_version": binary.version,
                "cs2_patch": cs2_patch, "game_exit_code": game.returncode if game else None,
                "game_seconds": round(game_seconds, 1) if game else None, "launches": len(batches),
                "warnings": warnings,
                "frame_format": "video" if hlae else action_options.frame_format,
                # The tick ranges the video covers, in order: composition maps demo ticks to video time with it.
                # HLAE reports the frames it really wrote per take; the game's recorder is trusted to the plan.
                "spans": [{"start_tick": seq.start_tick, "end_tick": seq.end_tick,
                           "frames": take_frames[i] if take_frames else actions.expected_frames([seq], fps, tickrate)}
                          for i, seq in enumerate(plan.sequences)],
            })

    @staticmethod
    def _with_diagnostics(message: str, settings: IngameSettings, game: launcher.GameRun | None) -> str:
        parts = [message]
        if game is not None:
            parts.append(f"Game exit code: {game.returncode} after {game.duration_s:.0f} s.")
        log_tail = plugin.read_plugin_log(settings, 800)
        parts.append(f"csdm.log: {log_tail}" if log_tail else
                     "No csdm.log was written: the plugin did not load (wrong build for this CS2 version?).")
        return " ".join(parts)

    @staticmethod
    def _hlae_encoder(extra: dict[str, Any], settings: IngameSettings) -> str:
        name = str(extra.get("ingame_hlae_encoder") or settings.hlae_encoder or "x264").lower()
        if name not in HLAE_ENCODERS:
            raise RenderError(f"ingame_hlae_encoder must be one of: {', '.join(HLAE_ENCODERS)}.")
        return HLAE_ENCODERS[name]

    def _hlae_poller(self, job: RenderJob, hlae_dir: Path, takes: int, startup_timeout_s: float,
                     stall_timeout_s: float, *, done_at: int | None = None) -> Callable[[float], None]:
        """Progress + stall detection for HLAE, which writes one growing video per take instead of frames.

        ``takes`` is the whole recording; ``done_at`` how many of them exist when this launch is finished (the
        recording may take several launches)."""
        done_at = takes if done_at is None else done_at
        state = {"checked_at": -10.0, "bytes": 0, "changed_at": 0.0}

        def on_poll(elapsed: float) -> None:
            if elapsed - state["checked_at"] < 2.0:
                return
            state["checked_at"] = elapsed
            started, size = encode.hlae_bytes(hlae_dir)
            if size != state["bytes"]:
                state["bytes"], state["changed_at"] = size, elapsed
            if started == 0:
                if elapsed > startup_timeout_s:
                    raise RenderError(f"CS2 did not start recording within {int(startup_timeout_s)} s and was "
                                      "closed. HLAE may not support the installed CS2 version yet.")
                job.progress(0.05, "Waiting for CS2 to load the demo")
                return
            idle = elapsed - state["changed_at"]
            if started >= done_at and idle > 60.0:
                raise _GameDidNotQuit("Every take was recorded but CS2 did not quit by itself; it was closed.")
            if idle > stall_timeout_s:
                raise RenderError(f"Recording stalled: nothing written for {int(idle)} s (take {started}/{takes}); "
                                  "CS2 was closed.")
            job.progress(0.05 + 0.63 * min(1.0, (started - 0.5) / max(1, takes)),
                         f"Recording in CS2 (take {min(started, takes)}/{takes})")

        return on_poll

    def _progress_poller(self, job: RenderJob, settings: IngameSettings, expected: int,
                         startup_timeout_s: float, stall_timeout_s: float, *, last_name: str,
                         last_expected: int) -> Callable[[float], None]:
        """Progress + stall detection from the number of frames on disk, checked every ~2 s.

        ``last_name`` is the take recorded last: once (nearly) all of its frames exist and nothing new shows up
        for a minute, the game is considered done even though it did not quit.
        """
        state = {"checked_at": -10.0, "frames": 0, "changed_at": 0.0}
        last_nearly_done = max(1, last_expected - max(5, last_expected // 33))
        last_prefix = last_name.lower()

        def on_poll(elapsed: float) -> None:
            if elapsed - state["checked_at"] < 2.0:
                return
            state["checked_at"] = elapsed
            frame_names = encode.frame_names(settings)
            frames = len(frame_names)
            if frames != state["frames"]:
                state["frames"], state["changed_at"] = frames, elapsed
            idle = elapsed - state["changed_at"]
            if frames == 0:
                if elapsed > startup_timeout_s:
                    raise RenderError(self._with_diagnostics(
                        f"CS2 did not start recording within {int(startup_timeout_s)} s and was closed.",
                        settings, None))
                job.progress(0.05, "Waiting for CS2 to load the demo")
                return
            if idle > 60.0 and sum(1 for n in frame_names if n.startswith(last_prefix)) >= last_nearly_done:
                raise _GameDidNotQuit("All frames were recorded but CS2 did not quit by itself; it was closed.")
            if idle > stall_timeout_s:
                raise RenderError(self._with_diagnostics(
                    f"Recording stalled: no new frame for {int(idle)} s ({frames}/{expected}); CS2 was closed.",
                    settings, None))
            job.progress(0.05 + 0.63 * min(1.0, frames / max(1, expected)),
                         f"Recording in CS2 ({min(frames, expected)}/{expected} frames)")

        return on_poll


def wait_until_gone(is_running: Callable[[], bool | None], timeout_s: float = 10.0) -> None:
    """Small helper for callers that need the game process to be really gone before touching its files."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and is_running():
        time.sleep(0.25)
