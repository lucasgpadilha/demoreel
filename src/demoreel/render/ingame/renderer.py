"""``ingame`` renderer: records first-person footage with the real CS2 client.

Pipeline (driven the way CS Demo Manager does it, see ``docs/ingame-recording.md``), with one of two recording
backends: ``startmovie`` (the game's own recorder, image files then encoded; Windows and Linux) or ``hlae`` (HLAE
pipes the frames straight into ffmpeg; Windows only):

1. read the players' spectator slots from the demo, plan the takes and write ``<demo>.json`` (``actions.py``);
2. install the user-provided CSDM plugin and patch ``gameinfo.gi`` for the session (``plugin.py``);
3. start CS2 with ``-insecure +playdemo`` (through HLAE for ``hlae``) and wait for it to quit, with a watchdog
   (``launcher.py``);
4. encode the frames + WAV of each take (or mux HLAE's videos), join them, delete the raw files (``encode.py``).

Per-job knobs in ``RenderOptions.extra`` (all optional): ``ingame_hud`` ("deathnotices" | "full"),
``ingame_xray`` ("auto" | bool, see ``xray.py``), ``ingame_assists``, ``ingame_player_voices``, ``ingame_true_view`` (bools),
``ingame_commands`` (list of console commands run before each take), ``ingame_launch_args`` (list),
``ingame_keep_raw``, ``ingame_swap_red_blue`` (bools), ``ingame_lock_wait_s``, ``ingame_startup_timeout_s``,
``ingame_stall_timeout_s`` (seconds), ``ingame_frames`` ("jpg" | "tga"), ``ingame_backend`` ("auto" |
"startmovie" | "hlae") and ``ingame_hlae_encoder`` ("x264" | "nvenc").
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import ffmpeg
from ..base import (
    Availability,
    RecorderBusy,
    RenderCancelled,
    RenderError,
    RenderJob,
    RenderResult,
    player_view,
)
from . import actions, encode, launcher, plugin, procs, slots, staging, xray
from .settings import IngameSettings, default_lock_path, hlae_installs
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
GAME_EXIT_WAIT_S = 30.0  # after a launch, how long the game may take to be gone before its files are touched

log = logging.getLogger("demoreel.ingame")


PLUGIN_LOAD_TIMEOUT_S = 120.0  # no csdm.log by then: the plugin did not load, waiting longer changes nothing
QUICK_EXIT_S = 20.0  # a game gone this soon without recording never got to the demo
DIALOG_CHECK_S = 5.0  # how often the game is looked at for an error box while nothing is recorded
HLAE_FAILURES_NAME = "hlae-failures.json"  # <data_dir>/ingame: HLAE / CS2 pairs that did not record


class _GameDidNotQuit(RenderError):
    """Every frame was written but the game is still open (the final ``quit`` action did not run)."""


class HlaeFailed(RenderError):
    """Recording failed for a reason that is HLAE's - its loader, or its hook in the game - not the plugin's or
    the demo's. With the ``auto`` backend the render is then recorded again with the game's own recorder."""


# An error box that names HLAE's hook (after a CS2 update: "AfxHookSource2 ... addresses.cpp ...").
_HLAE_DIALOG = re.compile(r"afx|hlae|advancedfx|addresses\.cpp|mirv", re.IGNORECASE)


@dataclass(frozen=True)
class _Launch:
    """What a game launch's checks and messages need to know."""

    settings: IngameSettings
    binary: plugin.PluginBinary | None
    marker: str  # the demo the game plays: tells the recording's game from anybody else's
    hlae: bool
    patch: str | None  # installed CS2 patch, as steam.inf writes it
    plugin_timeout_s: float = PLUGIN_LOAD_TIMEOUT_S


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
                 dedicated_servers: Callable[[], list[procs.GameProcess]] = procs.dedicated_servers,
                 find_dialog: Callable[[str | None], str | None] = procs.game_dialog) -> None:
        self._settings = settings
        self._run_game = run_game
        self._run_loader = run_loader
        self._resolve_slots = resolve_slots
        self._is_cs2_running = is_cs2_running
        self._is_steam_running = is_steam_running
        self._dedicated_servers = dedicated_servers
        self._find_dialog = find_dialog  # (marker of the recording's game) -> "title: text" of an error box
        self._install_lock_cache: tuple[float, Path | None] | None = None

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

    # --- one recording at a time ------------------------------------------------------------------------

    def _lock_path(self) -> Path:
        return self._settings.lock_path if self._settings is not None else default_lock_path()

    def _install_lock(self) -> Path | None:
        """The lock of the CS2 install (shared by every data folder on this machine). Finding the install reads
        the registry and Steam's library list, and idle workers ask about once a second: kept for a minute."""
        if self._settings is not None:
            return self._settings.install_lock_path
        now = time.monotonic()
        if self._install_lock_cache is None or now - self._install_lock_cache[0] > 60.0:
            try:
                path = load_settings().install_lock_path
            except Exception:  # noqa: BLE001 - no install, no lock
                path = None
            self._install_lock_cache = (now, path)
        return self._install_lock_cache[1]

    def recorder_taken(self) -> bool:
        """Is another process recording with the game on this machine right now - from this data folder or
        another one using the same CS2? Cheap; never raises."""
        try:
            if launcher.RecordingLock(self._lock_path()).is_held_elsewhere():
                return True
            install = self._install_lock()
            return install is not None and launcher.RecordingLock(install).is_held_elsewhere()
        except Exception:  # noqa: BLE001
            return False

    @contextmanager
    def _hold(self, lock: Path, install: Path | None, wait_s: float, cancelled: Callable[[], bool],
              on_wait: Callable[[float], None]) -> Iterator[None]:
        """Both locks, always in this order (the data folder's, then the install's): no two processes can wait
        on each other."""
        with launcher.recording_lock(lock, wait_s, cancelled, on_wait):
            if install is None:
                yield
                return
            busy = ("Another demoreel, with another data folder, is recording with this CS2 install right now "
                    "(only one recording per CS2 folder at a time). Try again when it is finished.")
            with launcher.recording_lock(install, wait_s, cancelled, on_wait, busy_message=busy):
                yield

    @contextmanager
    def recorder(self, wait_s: float = 0.0, cancelled: Callable[[], bool] = lambda: False,
                 on_wait: Callable[[float], None] = lambda waited: None) -> Iterator[None]:
        """Keep this machine's recorder for several :meth:`render` calls in a row: nothing else gets the game
        in between. Raises :class:`RecorderBusy` when another process still has it after ``wait_s``."""
        with self._hold(self._lock_path(), self._install_lock(), wait_s, cancelled, on_wait):
            yield

    def recover_install(self) -> list[str]:
        """Put the CS2 folder back after a recording that was killed (``demoreel serve`` closed mid-recording,
        the machine shut down...), when that is safe: nothing records with this CS2 - both recording locks
        are taken for the moment of the repair - and the game is closed. ``serve``, ``worker`` and ``doctor``
        call it when they start. Returns what was done, or why it could not be; ``[]`` when there was nothing
        to do or a recording is in progress. Never raises."""
        try:
            settings = self.settings()
            if settings.cs2_dir is None or not plugin.leftovers(settings):
                return []
            with self._hold(settings.lock_path, settings.install_lock_path, 0.0, lambda: False, lambda waited: None):
                if self._is_cs2_running():
                    return ["The CS2 folder is still changed by a recording that was interrupted, but CS2 is "
                            "running, so it was left alone. Close the game: it is put back the next time "
                            "demoreel serve, worker or doctor starts, or before the next recording."]
                return plugin.recover_install(settings)
        except RecorderBusy:
            return []  # a recording is in progress: what it changed is not a leftover
        except Exception as exc:  # noqa: BLE001 - reported, never fatal at start-up
            return [f"Could not put the CS2 folder back after an interrupted recording: {exc}"]

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
                        "hlae_version": settings.hlae_version if settings.hlae_available else None,
                        "hlae_failed_with_this_patch": hlae_failure(settings) if settings.hlae_available else None,
                        "hlae_installs": [{"version": h.version, "path": str(h.exe), "usable": h.usable}
                                          for h in hlae_installs(settings.hlae_root)],
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
        static = len(problems)  # what someone has to set up; everything after this can pass by itself

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
            # Held by another process: it is recording, and the game running is its game. Held by this one (a
            # worker keeps it between its jobs): nothing of ours runs while this is asked, whatever runs is not.
            if not launcher.held_here(settings.lock_path):
                recording = launcher.RecordingLock(settings.lock_path).is_held_elsewhere() or (
                    settings.install_lock_path is not None
                    and launcher.RecordingLock(settings.install_lock_path).is_held_elsewhere())
        except Exception:  # noqa: BLE001
            recording = False
        details["recording_in_progress"] = recording
        left = [] if recording else plugin.leftovers(settings)
        details["interrupted_recording"] = left
        if left:
            problems.insert(0, "The CS2 folder is still changed by a recording that was interrupted ("
                            + ", ".join(left) + "). Do not start CS2 from Steam until it is put back: demoreel "
                            "does it by itself, with the game closed, when serve, worker or doctor starts and "
                            "before the next recording.")
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
        # Problems that go away by themselves - the owner closes the game, Steam starts, disk is freed, the
        # server stops, the interrupted recording is put back - as opposed to set-up that someone must fix. A job
        # that needs the game waits in the queue for the first kind, and fails with the reason for the second.
        details["transient"] = bool(problems) and static == 0
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
        with self._hold(settings.lock_path, settings.install_lock_path, wait_s, job.cancelled,
                        on_wait=lambda waited: job.progress(0.01, "Waiting for another in-game recording to finish")):
            auto = self._requested_backend(settings, extra) == "auto"
            try:
                return self._render_locked(job, settings)
            except HlaeFailed as exc:
                if not auto:
                    raise
                # After a CS2 update HLAE is usually the first to break: record this one with the game's own
                # recorder, and keep doing so for this HLAE and this CS2 patch until either changes.
                remember_hlae_failure(settings, str(exc))
                log.warning("HLAE failed; recording again with the game's own recorder: %s", exc)
                job.progress(0.05, "HLAE did not record; recording with the game's own recorder")
                result = self._render_locked(job, settings, backend="startmovie")
                result.details["warnings"].insert(0, f"Recorded with the game's own recorder instead of HLAE. "
                                                     f"{exc}")
                return result

    @staticmethod
    def _requested_backend(settings: IngameSettings, extra: dict[str, Any]) -> str:
        return str(extra.get("ingame_backend") or settings.backend or "auto").lower()

    @staticmethod
    def _backend(settings: IngameSettings, extra: dict[str, Any]) -> str:
        backend = IngameRenderer._requested_backend(settings, extra)
        if backend == "auto":  # HLAE records several times faster; use it wherever it is installed and works
            backend = "hlae" if settings.hlae_available and hlae_failure(settings) is None else "startmovie"
        if backend not in BACKENDS:
            raise RenderError(f"Unknown in-game recording backend {backend!r}. Available: auto, "
                              f"{', '.join(BACKENDS)}.")
        return backend

    def _render_locked(self, job: RenderJob, settings: IngameSettings, backend: str | None = None) -> RenderResult:
        """``backend``: forced (the ``auto`` retry after HLAE failed); otherwise as the request and the
        settings say."""
        options = job.options
        extra = options.extra or {}
        warnings: list[str] = []
        if backend is None:
            backend = self._backend(settings, extra)
            failed = hlae_failure(settings) if settings.hlae_available else None
            if backend == "startmovie" and failed and self._requested_backend(settings, extra) == "auto":
                warnings.append(f"{_hlae_name(settings)} is not used with this CS2 patch: it did not record with "
                                f"it before ({failed}). Install a newer HLAE in {settings.hlae_root}/<version>/ "
                                "to try HLAE again.")
        hlae = backend == "hlae"
        hlae_dir = Path(os.path.abspath(job.work_dir)) / "hlae"
        width, height, fps = encode.even(options.width), encode.even(options.height), max(1, int(options.fps))

        launcher.assert_can_start(self._is_cs2_running, self._is_steam_running)
        shared = self._shared_install(settings)
        if shared:
            raise RenderError(shared)
        restored = plugin.recover_install(settings)
        if restored:
            warnings.append("A previous run was interrupted and had left the CS2 folder changed; put back: "
                            + "; ".join(restored) + ".")

        cs2_patch = plugin.read_cs2_patch_version(settings)
        binary = plugin.select_plugin_binary(settings.plugin_dir, settings.platform, cs2_patch,
                                             settings.plugin_version)
        if binary is None:  # checked by _static_problems, but the folder may have changed since
            raise RenderError(plugin.missing_plugin_instruction(settings, cs2_patch))

        # 1. Plan ------------------------------------------------------------------------------------
        job.progress(0.02, "Reading player slots from the demo")
        tickrate = float(job.analysis.tickrate or 64)
        max_tick = int(job.analysis.total_ticks or 0) or None
        pov_ticks = [seg.start_tick for clip in job.clips for seg in clip.segments if player_view(seg.pov_steamid)]
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
        try:
            # The journal goes into the CS2 folder before anything else there changes: if demoreel is killed
            # from here on, whoever starts next puts the folder back (plugin.recover_install).
            with plugin.recording_session(settings, keep_raw=keep_raw) as scope:
                return self._record_and_encode(
                    job, settings, staged=staged, scope=scope, backend=backend, binary=binary, plan=plan,
                    batches=batches, action_options=action_options, warnings=warnings, hlae_dir=hlae_dir,
                    cs2_patch=cs2_patch, expected=expected)
        finally:
            staged.cleanup()
            if not keep_raw:
                shutil.rmtree(hlae_dir, ignore_errors=True)

    def _record_and_encode(self, job: RenderJob, settings: IngameSettings, *, staged: staging.StagedDemo,
                           scope: encode.RawScope, backend: str, binary: plugin.PluginBinary, plan: actions.Plan,
                           batches: list[list[actions.RecordSequence]], action_options: actions.ActionOptions,
                           warnings: list[str], hlae_dir: Path, cs2_patch: int | None,
                           expected: int) -> RenderResult:
        """Steps 3 and 4 of a render - every game launch, then the encoding - inside the recording journal."""
        options = job.options
        extra = options.extra or {}
        hlae = backend == "hlae"
        width, height, fps = encode.even(options.width), encode.even(options.height), max(1, int(options.fps))
        tickrate = action_options.tickrate
        names = [seq.name for seq in plan.sequences]  # final-video order
        game: launcher.GameRun | None = None  # the last launch
        game_seconds = 0.0  # all launches
        has_layer = False  # the Vulkan layer of the plugin (Linux), as installed for the last launch

        # CS2 fails on paths outside ASCII (CS Demo Manager checks the demo's): the private config folder and
        # HLAE's output folder reach the game as well.
        for what, path in (("its private config folder (USRLOCALCSGO)", settings.cfg_dir),
                           *((("HLAE's output folder", hlae_dir),) if hlae else ())):
            if not launcher.is_basic_latin(str(path)):
                raise RenderError(f"CS2 cannot use {what}, {path}: its path has characters outside ASCII. Move "
                                  "the demoreel data folder (DEMOREEL_DATA_DIR) - or, for `demoreel render`, the "
                                  "output file - to a path without accents or special characters.")
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

        # 3. Record ----------------------------------------------------------------------------------------
        launch = _Launch(settings=settings, binary=binary, marker=str(staged.path), hlae=hlae,
                         patch=plugin.read_cs2_patch_text(settings),
                         plugin_timeout_s=_number(extra, "ingame_plugin_timeout_s", PLUGIN_LOAD_TIMEOUT_S))
        recorded = 0
        for number, batch in enumerate(batches, start=1):
            if job.cancelled():
                raise RenderCancelled("Render cancelled.")
            staged.write_actions(actions.to_json(actions.build_actions(batch, action_options)))
            recorded += len(batch)
            batch_seconds = actions.expected_frames(batch, fps, tickrate) / float(fps)
            timeout_s = settings.timeout_s or (240.0 + 90.0 * len(batch) + 12.0 * batch_seconds)
            # What earlier launches wrote is no sign of this one starting: everything below counts from here.
            before = encode.hlae_bytes(hlae_dir)[0] if hlae else len(encode.frame_names(settings, scope))
            if hlae:
                on_poll = self._hlae_poller(job, hlae_dir, len(plan.sequences), startup_timeout_s,
                                            stall_timeout_s, done_at=recorded, baseline=before, launch=launch,
                                            in_launch=len(batch))
            else:
                on_poll = self._progress_poller(
                    job, settings, expected, startup_timeout_s, stall_timeout_s, last_name=batch[-1].name,
                    last_expected=actions.expected_frames([batch[-1]], fps, tickrate), scope=scope,
                    baseline=before, launch=launch)
            if number == 1:
                job.progress(0.05, "Starting CS2")
            run: launcher.GameRun | None = None
            gone = True
            with plugin.plugin_session(settings, binary) as installed:
                try:
                    run = (self._run_loader if hlae else self._run_game)(
                        command, env=launcher.build_env(settings, installed.env), timeout_s=timeout_s,
                        cancelled=job.cancelled, on_poll=on_poll, log_path=job.work_dir / "cs2-output.log",
                        kill_names=procs.CS2_PROCESS_NAMES, kill_marker=launch.marker)
                except _GameDidNotQuit as exc:
                    warnings.append(str(exc))
                except launcher.LoaderFailed as exc:
                    raise HlaeFailed(f"{_hlae_name(settings)} ({settings.hlae_exe}) could not start CS2 patch "
                                     f"{launch.patch or '(unknown)'}: {exc} {hlae_advice(settings)}") from exc
                finally:
                    # gameinfo.gi and the plugin's settings file are restored and the plugin removed when this
                    # block ends - also when the game was just killed: let it go first (the plugin writes its
                    # settings once more on shutdown, and Windows keeps server.dll locked while it runs).
                    gone = wait_until_gone(self._is_cs2_running, GAME_EXIT_WAIT_S)
                    if not gone:
                        log.warning("CS2 was still running %.0f s after the recording ended", GAME_EXIT_WAIT_S)
                has_layer = installed.vulkan_layer_installed
            if not gone:
                warnings.append(f"CS2 was still closing {GAME_EXIT_WAIT_S:.0f} s after recording; what it kept "
                                "locked in the CS2 folder is removed the next time demoreel starts.")
            if run is not None:
                after = encode.hlae_bytes(hlae_dir)[0] if hlae else len(encode.frame_names(settings, scope))
                if after <= before and run.duration_s < QUICK_EXIT_S:
                    raise RenderError(self._quick_exit_message(run, launch))
                game_seconds += run.duration_s
                game = run

        # 4. Encode ----------------------------------------------------------------------------------------
        swap = _flag(extra, "ingame_swap_red_blue", settings.is_linux and not has_layer)
        try:
            if hlae:
                encoded = encode.mux_hlae_takes(
                    hlae_dir, names, job.work_dir / "takes", width=width, height=height, fps=fps,
                    crf=options.crf, cancelled=job.cancelled,
                    progress=lambda done, total: job.progress(
                        0.70 + 0.25 * done / max(1, total), f"Joining take {min(done + 1, total)}/{total}"))
            else:
                encoded = encode.encode_takes(
                    settings, names, job.work_dir / "takes", width=width, height=height, fps=fps,
                    crf=options.crf, swap_red_blue=swap, cancelled=job.cancelled, scope=scope,
                    progress=lambda done, total: job.progress(
                        0.70 + 0.25 * done / max(1, total), f"Encoding take {min(done + 1, total)}/{total}"))
        except RenderCancelled:
            raise
        except RenderError as exc:
            raise RenderError(self._with_diagnostics(str(exc), launch, game)) from exc
        warnings.extend(encoded.warnings)
        job.progress(0.96, "Joining takes")
        warnings.extend(encode.finalize(encoded.parts, job.out_path, job.thumbnail_path, encoded.duration_s))

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
                # The tick ranges the video covers, in order: composition maps demo ticks to video time with it,
                # so each take counts with the frames it really has (a take cut short moves all that follows).
                "spans": [{"start_tick": seq.start_tick, "end_tick": seq.end_tick,
                           "frames": encoded.take_frames[i] if encoded.take_frames
                           else actions.expected_frames([seq], fps, tickrate)}
                          for i, seq in enumerate(plan.sequences)],
            })

    @staticmethod
    def _with_diagnostics(message: str, launch: _Launch, game: launcher.GameRun | None) -> str:
        parts = [message]
        if game is not None:
            parts.append(f"Game exit code: {game.returncode} after {game.duration_s:.0f} s.")
        log_tail = plugin.read_plugin_log(launch.settings, 800)
        parts.append(f"csdm.log: {log_tail}" if log_tail else
                     "No csdm.log was written. " + plugin.plugin_advice(launch.settings, launch.binary, launch.patch))
        return " ".join(parts)

    @staticmethod
    def _quick_exit_message(run: launcher.GameRun, launch: _Launch) -> str:
        text = (f"CS2 closed {run.duration_s:.0f} s after it was started, before it got to the demo (exit code "
                f"{run.returncode}). That is usually Steam: check that it is running and logged in, and that no "
                "CS2 update is pending or downloading (Steam closes the game then; start CS2 once from Steam to "
                "see).")
        if launch.hlae:
            text += (f" With HLAE the game can also close at once when {_hlae_name(launch.settings)} does not "
                     "support this CS2 patch: try DEMOREEL_INGAME_BACKEND=startmovie.")
        return f"{text} {run.output_tail[-400:]}".strip()

    def _not_started(self, launch: _Launch, elapsed: float, state: dict[str, float]) -> None:
        """While nothing is recorded yet: give up early when the game shows an error box or the plugin never
        loaded, instead of waiting for the whole start-up timeout with nothing to show for it."""
        settings = launch.settings
        if elapsed - state.get("dialog_at", -1e9) >= DIALOG_CHECK_S:
            state["dialog_at"] = elapsed
            dialog = self._find_dialog(launch.marker)
            if dialog:
                head = (f"CS2 showed an error box and was closed (it waits there for a click that never comes): "
                        f"\"{dialog[:300]}\".")
                if launch.hlae and _HLAE_DIALOG.search(dialog):
                    raise HlaeFailed(f"{head} It comes from {_hlae_name(settings)} ({settings.hlae_exe}), which "
                                     f"does not support CS2 patch {launch.patch or '(unknown)'} yet. "
                                     f"{hlae_advice(settings)}")
                raise RenderError(self._with_diagnostics(head, launch, None))
        if elapsed > launch.plugin_timeout_s and not plugin.plugin_loaded(settings):
            text = (f"Nothing was recorded and no csdm.log was written {int(launch.plugin_timeout_s)} s after CS2 "
                    f"was started; the game was closed. {plugin.plugin_advice(settings, launch.binary, launch.patch)}")
            if launch.hlae:
                text += (f" With HLAE, its hook can also stop the game before the plugin loads: "
                         f"{hlae_advice(settings)}")
            raise RenderError(text)

    @staticmethod
    def _hlae_encoder(extra: dict[str, Any], settings: IngameSettings) -> str:
        name = str(extra.get("ingame_hlae_encoder") or settings.hlae_encoder or "x264").lower()
        if name not in HLAE_ENCODERS:
            raise RenderError(f"ingame_hlae_encoder must be one of: {', '.join(HLAE_ENCODERS)}.")
        return HLAE_ENCODERS[name]

    def _hlae_poller(self, job: RenderJob, hlae_dir: Path, takes: int, startup_timeout_s: float,
                     stall_timeout_s: float, *, done_at: int | None = None, baseline: int = 0,
                     launch: _Launch | None = None, in_launch: int | None = None) -> Callable[[float], None]:
        """Progress + stall detection for HLAE, which writes one growing video per take instead of frames.

        ``takes`` is the whole recording; ``done_at`` how many of them exist when this launch is finished (the
        recording may take several launches); ``baseline`` how many existed when it started, and ``in_launch``
        how many it records."""
        done_at = takes if done_at is None else done_at
        state = {"checked_at": -10.0, "bytes": -1, "changed_at": 0.0}

        def on_poll(elapsed: float) -> None:
            if elapsed - state["checked_at"] < 2.0:
                return
            state["checked_at"] = elapsed
            started, size = encode.hlae_bytes(hlae_dir)
            if size != state["bytes"]:
                state["bytes"], state["changed_at"] = size, elapsed
            if started <= baseline:  # nothing of this launch yet
                if launch is not None:
                    self._not_started(launch, elapsed, state)
                if elapsed > startup_timeout_s:
                    raise self._hlae_did_not_record(launch, startup_timeout_s)
                job.progress(0.05, "Waiting for CS2 to load the demo")
                return
            idle = elapsed - state["changed_at"]
            if started >= done_at and idle > 60.0:
                # The last take's file exists and stopped growing: finished, or frozen in the middle of it? The
                # plugin logs every command it runs; with a log, finished means every take's end ran.
                ends = (plugin.commands_run(launch.settings, "mirv_streams record end")
                        if launch is not None else None)
                if ends is None or ends >= (in_launch or 0):
                    raise _GameDidNotQuit("Every take was recorded but CS2 did not quit by itself; it was closed.")
            if idle > stall_timeout_s:
                raise RenderError(f"Recording stalled: nothing written for {int(idle)} s (take {started}/{takes}); "
                                  "CS2 was closed.")
            job.progress(0.05 + 0.63 * min(1.0, (started - 0.5) / max(1, takes)),
                         f"Recording in CS2 (take {min(started, takes)}/{takes})")

        return on_poll

    @staticmethod
    def _hlae_did_not_record(launch: _Launch | None, timeout_s: float) -> RenderError:
        if launch is None:
            return RenderError(f"CS2 did not start recording within {int(timeout_s)} s and was closed. HLAE may not "
                               "support the installed CS2 version yet.")
        settings = launch.settings
        text = (f"{_hlae_name(settings)} ({settings.hlae_exe}) did not start recording with CS2 patch "
                f"{launch.patch or '(unknown)'} within {int(timeout_s)} s and the game was closed; it probably does "
                f"not support this CS2 version yet. {hlae_advice(settings)}")
        # The plugin ran the take's `mirv_streams record start` and no video came: that is HLAE's recorder.
        if (plugin.commands_run(settings, "mirv_streams record start") or 0) > 0:
            return HlaeFailed(text)
        return RenderError(text + " " + IngameRenderer._with_diagnostics("", launch, None).strip())

    def _progress_poller(self, job: RenderJob, settings: IngameSettings, expected: int,
                         startup_timeout_s: float, stall_timeout_s: float, *, last_name: str,
                         last_expected: int, scope: encode.RawScope = encode.RawScope(), baseline: int = 0,
                         launch: _Launch | None = None) -> Callable[[float], None]:
        """Progress + stall detection from the number of frames on disk, checked every ~2 s.

        ``last_name`` is the take recorded last: once (nearly) all of its frames exist and nothing new shows up
        for a minute, the game is considered done even though it did not quit. ``baseline``: frames written by
        earlier launches of the same recording, which say nothing about this one.
        """
        state = {"checked_at": -10.0, "frames": baseline, "changed_at": 0.0}
        last_nearly_done = max(1, last_expected - max(5, last_expected // 33))
        last_prefix = last_name.lower()

        def on_poll(elapsed: float) -> None:
            if elapsed - state["checked_at"] < 2.0:
                return
            state["checked_at"] = elapsed
            frame_names = encode.frame_names(settings, scope)
            frames = len(frame_names)
            if frames != state["frames"]:
                state["frames"], state["changed_at"] = frames, elapsed
            idle = elapsed - state["changed_at"]
            if frames <= baseline:  # nothing of this launch yet
                if launch is not None:
                    self._not_started(launch, elapsed, state)
                if elapsed > startup_timeout_s:
                    message = f"CS2 did not start recording within {int(startup_timeout_s)} s and was closed."
                    raise RenderError(self._with_diagnostics(message, launch, None) if launch else message)
                job.progress(0.05, "Waiting for CS2 to load the demo")
                return
            if idle > 60.0 and sum(1 for n in frame_names if n.startswith(last_prefix)) >= last_nearly_done:
                raise _GameDidNotQuit("All frames were recorded but CS2 did not quit by itself; it was closed.")
            if idle > stall_timeout_s:
                message = f"Recording stalled: no new frame for {int(idle)} s ({frames}/{expected}); CS2 was closed."
                raise RenderError(self._with_diagnostics(message, launch, None) if launch else message)
            job.progress(0.05 + 0.63 * min(1.0, frames / max(1, expected)),
                         f"Recording in CS2 ({min(frames, expected)}/{expected} frames)")

        return on_poll


def _hlae_name(settings: IngameSettings) -> str:
    return f"HLAE {settings.hlae_version}" if settings.hlae_version else "HLAE"


def hlae_advice(settings: IngameSettings) -> str:
    """What to do when HLAE does not record: where a newer one goes, and the game's own recorder meanwhile."""
    return (f"Install a newer HLAE ({HLAE_RELEASES}) in {settings.hlae_root}/<version>/ - the highest version "
            "there is used - or record with the game's own recorder: set DEMOREEL_INGAME_BACKEND=startmovie (per "
            "render: options.extra.ingame_backend = \"startmovie\").")


HLAE_RELEASES = "https://github.com/advancedfx/advancedfx/releases"


def _hlae_key(settings: IngameSettings) -> str:
    return f"{settings.hlae_version or settings.hlae_exe}|{plugin.read_cs2_patch_text(settings) or 'unknown patch'}"


def _read_failures(settings: IngameSettings) -> dict[str, Any]:
    try:
        data = json.loads((settings.data_dir / HLAE_FAILURES_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def hlae_failure(settings: IngameSettings) -> str | None:
    """Why the installed HLAE did not record with the installed CS2 patch, if it was tried and failed."""
    entry = _read_failures(settings).get(_hlae_key(settings))
    if not isinstance(entry, dict):
        return None
    return f"{entry.get('at', 'earlier')}: {str(entry.get('error') or 'it did not record')[:240]}"


def remember_hlae_failure(settings: IngameSettings, error: str) -> None:
    """``auto`` stops choosing this HLAE for this CS2 patch; a new HLAE folder or a CS2 update tries it again."""
    data = _read_failures(settings)
    data[_hlae_key(settings)] = {"at": time.strftime("%Y-%m-%d %H:%M"), "error": error[:1000]}
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        (settings.data_dir / HLAE_FAILURES_NAME).write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not remember that HLAE failed: %s", exc)


def wait_until_gone(is_running: Callable[[], bool | None], timeout_s: float = 10.0) -> bool:
    """Wait for the game process to be really gone before touching its files. ``False`` when it was still
    there after ``timeout_s``."""
    deadline = time.monotonic() + timeout_s
    while is_running():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)
    return True
