"""Builds the *actions file* the CS Demo Manager plugin executes while CS2 plays a demo. Pure functions only.

The file is ``<demo path>.json`` (so ``match.dem.json``), a list of *sequences*, each a list of
``{"tick": int, "cmd": str}`` actions. The plugin runs an action when the playback reaches its tick and moves to
the next sequence (restarting the demo from tick 0) on the internal ``go_to_next_sequence`` command.

Ported from CS Demo Manager (MIT, https://github.com/akiver/cs-demo-manager):

* ``src/node/video/generation/create-cs2-video-json-file.ts`` - command list and tick offsets
* ``src/node/counter-strike/json-actions-file/json-actions-file-generator.ts`` - action helpers, tick clamp
* ``src/node/video/generation/get-sequence-name.ts`` - ``<number>-sequence``
* ``cs2-server-plugin/cs2-server-plugin/sequences.cpp`` - how the plugin consumes the file

Differences from the reference, all deliberate:

* HLAE-only commands (``mirv_streams ...``, ``mirv_deathmsg ...``, ``mirv_replace_name ...``) are not emitted:
  this module only drives the ``startmovie`` recording system. :func:`recording_commands` is the single place
  an HLAE backend would plug into.
* ``ClipSegment.speed`` is ignored - the reference has no playback-speed support in this path.
* Ticks are clamped to the end of the demo when ``max_tick`` is known, because the plugin never runs an action
  placed after the last tick (the game would then stay open until the watchdog kills it).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from ..base import Clip, ClipSegment

# json-actions-file-generator.ts getValidTick(): "Commands must be executed at least ~1.5 seconds (tickrate 64)
# after a demo_gototick 0 command because the game may skip these first ticks."
MIN_ACTION_TICK = 96
# First tick a take may start at. Nothing runs before MIN_ACTION_TICK, so a take starting there would seek, aim the
# camera and start recording all on that one tick, and ``spec_player`` is ignored on the tick of a
# ``demo_gototick``: the whole take was then filmed from whichever player the game had picked. One second of
# playback after the seek leaves the camera commands on ticks of their own, as in every other take.
FIRST_RECORD_TICK = MIN_ACTION_TICK + 64
# create-cs2-video-json-file.ts: `quit` / `go_to_next_sequence` run 64 ticks after the sequence's end tick.
POST_SEQUENCE_TICKS = 64
# create-cs2-video-json-file.ts: "Pause the playback ... a few ticks before the sequence's start tick".
PAUSE_LEAD_TICKS = 4
CAMERA_LEAD_TICKS = 8  # the first camera of a take is also set this many ticks before recording starts
# Segments shorter than this are dropped (nothing useful can be recorded).
MIN_SEQUENCE_TICKS = 8
# Distance kept between the last recorded tick / last action and the end of the demo.
END_OF_DEMO_MARGIN_TICKS = 8

# create-cs2-video-json-file.ts `mandatoryCommands`, minus "mirv_streams record screen enabled 1" (HLAE only).
MANDATORY_COMMANDS: tuple[str, ...] = (
    "sv_cheats 1",
    "volume 1",
    "cl_hud_telemetry_frametime_show 0",
    "cl_hud_telemetry_net_misdelivery_show 0",
    "cl_hud_telemetry_ping_show 0",
    "cl_hud_telemetry_serverrecvmargin_graph_show 0",
    "cl_trueview_show_status 0",
    "r_show_build_info 0",
)

SlotLookup = Callable[[str, int], "int | None"]


@dataclass(frozen=True)
class Action:
    tick: int
    cmd: str


@dataclass(frozen=True)
class CameraCut:
    """Switch the camera to the player in ``slot`` (see ``slots.py``) at ``tick``."""

    tick: int
    slot: int


@dataclass(frozen=True)
class RecordSequence:
    """One recorded take. ``number`` is its position in the final video (1-based), not the recording order."""

    number: int
    start_tick: int
    end_tick: int
    cameras: tuple[CameraCut, ...] = ()
    xray: tuple[tuple[int, int], ...] = ()  # tick ranges with player outlines on (see ``xray.py``)

    @property
    def name(self) -> str:
        """Name given to ``startmovie``; CS2 writes ``<name>%08d.tga`` and ``<name>.wav`` (get-sequence-name.ts)."""
        return f"{self.number}-sequence"

    @property
    def ticks(self) -> int:
        return self.end_tick - self.start_tick


@dataclass(frozen=True)
class ActionOptions:
    fps: int = 30
    tickrate: float = 64
    only_death_notices: bool = True  # CSDM default: hide the HUD except the kill feed
    show_xray: bool = False  # CSDM default is True; a first-person highlight looks wrong with wallhack outlines
    show_assists: bool = True
    player_voices: bool = False  # CSDM default is True; voice comms are private, so opt-in here
    true_view: bool = False  # cl_demo_predict, CSDM default
    close_game: bool = True
    extra_commands: tuple[str, ...] = ()  # run at each sequence's setup tick (CSDM's per-sequence "cfg")
    recording_system: str = "startmovie"  # "startmovie" (the game's own recorder) or "hlae"
    frame_format: str = "tga"  # "tga" (lossless, huge) or "jpg" (~10x smaller, slightly lossy)
    # HLAE only: folder the takes are written to (forward slashes) and the ffmpeg output options of its encoder.
    hlae_output_dir: str = ""
    hlae_ffmpeg_args: str = "-c:v libx264 -preset veryfast -crf 17 -pix_fmt yuv420p"
    max_tick: int | None = None  # last tick of the demo, when known


@dataclass
class Plan:
    sequences: list[RecordSequence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def valid_tick(tick: int) -> int:
    """Port of ``JSONActionsFileGenerator.getValidTick``."""
    return max(MIN_ACTION_TICK, int(tick))


def recording_commands(system: str, sequence_name: str, fps: int,
                       frame_format: str = "tga") -> tuple[str, str, str]:
    """``(fps command, start command, end command)`` of a recording system.

    create-cs2-video-json-file.ts uses ``host_framerate`` + ``startmovie <name>`` / ``endmovie`` for the game's
    own recorder and ``mirv_streams record fps|start|end`` for HLAE. Only the former is implemented.
    """
    if system == "startmovie":
        start = f"startmovie {sequence_name}"
        if frame_format == "jpg":
            # Not in the reference's video flow (it records TGA), but the same plugin uses `startmovie … jpg`
            # for its camera previews. JPEG frames are ~10x smaller than 32-bit TGA, which matters because
            # writing 3.7 MB per frame is what stalls recording on ordinary SSDs. Once any format is named the
            # audio has to be asked for explicitly.
            start += " jpg wav"
        elif frame_format != "tga":
            raise ValueError(f"Unsupported frame format {frame_format!r} (use 'tga' or 'jpg').")
        return f"host_framerate {int(fps)}", start, "endmovie"
    if system == "hlae":
        return f"mirv_streams record fps {int(fps)}", "mirv_streams record start", "mirv_streams record end"
    raise ValueError(f"Unsupported recording system {system!r} (use 'startmovie' or 'hlae').")


def hlae_setup_commands(sequence_name: str, number: int, options: ActionOptions) -> list[str]:
    """Port of the HLAE branch of ``createCs2VideoJsonFile``: HLAE pipes the frames of the take straight into
    ffmpeg (no image files), writing ``<dir>/<take>/video.mp4`` and the sound to ``<dir>/<take>/take0000/audio.wav``.
    ``{QUOTE}`` is HLAE's placeholder for a double quote inside a settings string."""
    out = options.hlae_output_dir.replace("\\", "/").rstrip("/")
    if not out or '"' in out:
        raise ValueError("HLAE needs an output folder (without double quotes in its path).")
    take = f"{out}/{sequence_name}"
    preset = f"demoreel{int(number)}"
    return [
        "mirv_streams record startMovieWav 1",
        f'mirv_streams record name "{take}"',
        f'mirv_streams settings add ffmpeg {preset} "{options.hlae_ffmpeg_args} '
        f'{{QUOTE}}{take}\\\\video.mp4{{QUOTE}}"',
        f"mirv_streams record screen settings {preset}",
    ]


def plan_sequences(clips: Iterable[Clip], slot_of: SlotLookup | Mapping[str, int] | None = None, *,
                   max_tick: int | None = None, merge_contiguous: bool = True) -> Plan:
    """Turn clips into recordable sequences, numbered in the order they must appear in the final video.

    Back-to-back segments of the same clip (the next one starts where the previous one ends) become a single
    sequence with a camera cut in the middle - CSDM models this as several ``playerCameras`` in one sequence -
    which avoids a reload of the demo and gives a seamless cut. Everything else is one sequence per segment.
    """
    plan = Plan()
    lookup = _as_lookup(slot_of)
    ignored_speed = False
    for clip_index, clip in enumerate(clips, start=1):
        previous: ClipSegment | None = None
        for segment in clip.segments:
            start = max(FIRST_RECORD_TICK, int(segment.start_tick))
            end = int(segment.end_tick)
            if max_tick is not None and max_tick > 0:
                end = min(end, max_tick - END_OF_DEMO_MARGIN_TICKS)
            if end - start < MIN_SEQUENCE_TICKS:
                plan.warnings.append(
                    f"Clip {clip_index}: segment {segment.start_tick}-{segment.end_tick} skipped (too short, "
                    f"before tick {FIRST_RECORD_TICK} or past the end of the demo).")
                previous = None
                continue
            if abs(float(segment.speed or 1.0) - 1.0) > 1e-6:
                ignored_speed = True

            slot: int | None = None
            if segment.pov_steamid:
                slot = lookup(str(segment.pov_steamid), start)
                if slot is None:
                    plan.warnings.append(
                        f"Clip {clip_index}: player {segment.pov_steamid} has no spectator slot in this demo "
                        f"(bot or unknown SteamID); the camera is left where the game puts it.")

            last = plan.sequences[-1] if plan.sequences else None
            contiguous = (merge_contiguous and previous is not None and last is not None
                          and 0 <= start - last.end_tick <= 1)
            if contiguous and last is not None:
                cameras = last.cameras
                if slot is not None and (not cameras or cameras[-1].slot != slot):
                    cameras = (*cameras, CameraCut(tick=start, slot=slot))
                plan.sequences[-1] = RecordSequence(last.number, last.start_tick, end, cameras)
            else:
                cameras = (CameraCut(tick=start, slot=slot),) if slot is not None else ()
                plan.sequences.append(RecordSequence(len(plan.sequences) + 1, start, end, cameras))
            previous = segment
    if ignored_speed:
        plan.warnings.append("Segment playback speed is ignored by the in-game renderer (recorded at 1x).")
    return plan


def with_xray(sequences: Iterable[RecordSequence], clips: Iterable[Clip],
              by_pov: Mapping[str, Sequence[tuple[int, int]]]) -> list[RecordSequence]:
    """Give every sequence the x-ray ranges of the players it follows (``by_pov`` from ``xray.windows``)."""
    from dataclasses import replace

    segments = [s for clip in clips for s in clip.segments]
    out = []
    for seq in sequences:
        spans: list[tuple[int, int]] = []
        for s in segments:
            lo, hi = max(int(s.start_tick), seq.start_tick), min(int(s.end_tick), seq.end_tick)
            if hi <= lo or not s.pov_steamid:
                continue
            for a, b in by_pov.get(str(s.pov_steamid), ()):
                if min(b, hi) > max(a, lo):
                    spans.append((max(a, lo), min(b, hi)))
        merged: list[tuple[int, int]] = []
        for a, b in sorted(set(spans)):
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        out.append(replace(seq, xray=tuple(merged)))
    return out


def recording_order(sequences: Iterable[RecordSequence]) -> list[RecordSequence]:
    """Port of ``sortSequencesByStartTick``: record by start tick, the number only breaks ties."""
    return sorted(sequences, key=lambda s: (s.start_tick, s.number))


def recording_batches(sequences: Iterable[RecordSequence], match_end_tick: int | None) -> list[list[RecordSequence]]:
    """The takes in recording order, split into game launches.

    Playing past the end of the match puts the game's end-of-match screen up, and it stays up for the rest of
    that game session: the demo restarting for the next take does not clear it, so every take recorded
    afterwards shows the winners' line-up instead of the play. A take that plays past the end of the match is
    therefore the last one of its launch, and whatever is left gets a fresh game. Usually that is one extra
    launch for the very last play of a match (the killer's take and the victim's for the replay both reach
    the end); without ``match_end_tick`` everything is one launch."""
    batches: list[list[RecordSequence]] = [[]]
    for seq in recording_order(sequences):
        batches[-1].append(seq)
        # playback goes on until the take's last action, POST_SEQUENCE_TICKS after the recording stops
        if match_end_tick is not None and match_end_tick > 0 and seq.end_tick + POST_SEQUENCE_TICKS >= match_end_tick:
            batches.append([])
    return [b for b in batches if b]


def sequence_actions(sequence: RecordSequence, options: ActionOptions, *, is_last: bool) -> list[Action]:
    """Actions of one sequence, in the exact order of ``createCs2VideoJsonFile`` (the plugin runs same-tick
    actions in file order)."""
    actions: list[Action] = []

    def add(tick: int, cmd: str) -> None:
        actions.append(Action(tick=valid_tick(tick), cmd=cmd))

    fps_cmd, start_cmd, end_cmd = recording_commands(options.recording_system, sequence.name, options.fps,
                                                     options.frame_format)

    for cmd in MANDATORY_COMMANDS:
        add(1, cmd)
    if options.recording_system == "hlae":
        add(1, "mirv_streams record screen enabled 1")
    add(1, "engine_no_focus_sleep 0")  # keep rendering at full speed when the window is in the background
    add(1, f"cl_demo_predict {1 if options.true_view else 0}")
    add(1, f"cl_draw_only_deathnotices {1 if options.only_death_notices else 0}")
    # enablePlayerVoices / disablePlayerVoices: bitmask of the user ids heard, -1 = everyone, 0 = nobody.
    voices = -1 if options.player_voices else 0
    add(1, f"tv_listen_voice_indices {voices}")
    add(1, f"tv_listen_voice_indices_h {voices}")

    setup_tick = max(1, sequence.start_tick - round(options.tickrate))
    if options.recording_system == "hlae":
        for cmd in hlae_setup_commands(sequence.name, sequence.number, options):
            add(setup_tick, cmd)
    add(setup_tick, f"spec_show_xray {1 if options.show_xray else 0}")
    add(setup_tick, f"mp_display_kill_assists {1 if options.show_assists else 0}")
    add(setup_tick, fps_cmd)
    for cmd in options.extra_commands:
        if cmd.strip():
            add(setup_tick, cmd.strip())

    # "Pause the playback for a few seconds to avoid seeing the loading screen/tint effect. Do it a few ticks
    # before the sequence's start tick because some ticks may be skipped..." (pause_playback is handled by the
    # plugin: demo_pause, sleep 2 s, demo_resume.)
    add(max(1, sequence.start_tick - PAUSE_LEAD_TICKS), "pause_playback")

    # "Go to 1 tick before the sequence's setup tick to make sure the setup commands are executed. ... Since an
    # October 2025 CS2 update, executing spec_player and demo_gototick on the same tick may cause spec_player to
    # be ignored. It's important to go to the setup tick before executing any spec_player command."
    # addGoToTick(1, max(1, setupTick - 1)): both the tick of the action and its target go through valid_tick.
    add(1, f"demo_gototick {valid_tick(max(1, setup_tick - 1))}")

    for camera in sequence.cameras:
        # addSpecPlayer: "It's important to set spec_mode before spec_player otherwise the spec_player command
        # may not work." spec_mode 1 = first person.
        if camera.tick <= sequence.start_tick:
            # Not in the reference: also aim the camera a few ticks *before* recording starts (and before the
            # pause), so the first recorded frame cannot show the previous point of view. The commands are
            # repeated on the start tick, as the reference does, in case the early ones were skipped.
            early = max(setup_tick + 1, sequence.start_tick - CAMERA_LEAD_TICKS)
            if early < camera.tick:
                add(early, "spec_mode 1")
                add(early, f"spec_player {camera.slot}")
        add(camera.tick, "spec_mode 1")
        add(camera.tick, f"spec_player {camera.slot}")

    # Outlines for kills through smoke / walls. The setup tick switched them off; a range that is already open
    # when the take starts is switched on *on* the start tick (earlier ticks may be skipped around the pause).
    if not options.show_xray:
        for lo, hi in sequence.xray:
            lo, hi = max(lo, sequence.start_tick), min(hi, sequence.end_tick)
            if hi <= lo:
                continue
            add(lo, "spec_show_xray 1")
            if hi < sequence.end_tick:
                add(hi, "spec_show_xray 0")

    add(sequence.start_tick, start_cmd)
    add(sequence.end_tick, end_cmd)

    after_tick = sequence.end_tick + POST_SEQUENCE_TICKS
    if options.max_tick is not None and options.max_tick > 0:
        # Never schedule past the end of the demo (it would not run), but always after the end command.
        after_tick = max(sequence.end_tick + 1, min(after_tick, options.max_tick - 2))
    add(after_tick, "quit" if (is_last and options.close_game) else "go_to_next_sequence")
    return actions


def build_actions(sequences: Sequence[RecordSequence], options: ActionOptions) -> list[list[Action]]:
    """One action list per sequence, in recording order."""
    ordered = recording_order(sequences)
    return [sequence_actions(seq, options, is_last=(i == len(ordered) - 1)) for i, seq in enumerate(ordered)]


def build_segment_actions(segments: Sequence[ClipSegment], slots: Mapping[str, int] | SlotLookup | None,
                          options: ActionOptions) -> list[list[Action]]:
    """Convenience: actions for a plain list of segments (each one its own clip, so nothing is merged)."""
    plan = plan_sequences([Clip(segments=[s]) for s in segments], slots, max_tick=options.max_tick)
    return build_actions(plan.sequences, options)


def to_json(groups: Sequence[Sequence[Action]]) -> str:
    """Serialize like ``JSONActionsFileGenerator.write``: ``[{"actions": [{"cmd": ..., "tick": ...}]}]``."""
    payload = [{"actions": [{"cmd": a.cmd, "tick": a.tick} for a in group]} for group in groups]
    return json.dumps(payload, indent=2)


def expected_frames(sequences: Iterable[RecordSequence], fps: int, tickrate: float) -> int:
    return sum(max(1, round(s.ticks / float(tickrate) * fps)) for s in sequences)


def _as_lookup(slot_of: SlotLookup | Mapping[str, int] | None) -> SlotLookup:
    if slot_of is None:
        return lambda steamid, tick: None
    if isinstance(slot_of, Mapping):
        mapping = slot_of
        return lambda steamid, tick: mapping.get(steamid)
    return slot_of
