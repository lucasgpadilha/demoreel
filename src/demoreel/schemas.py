"""Request/response models of the REST API and MCP tools (the match analysis models live in analysis/schema.py)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field

from .analysis.schema import HighlightSegment

# The database stores naive UTC timestamps; the API always states the offset so clients cannot misread them.
UTCDateTime = Annotated[datetime, AfterValidator(lambda d: d.replace(tzinfo=UTC) if d.tzinfo is None else d)]


class JobOut(BaseModel):
    id: str
    type: str
    status: Literal["queued", "running", "succeeded", "failed", "canceled"]
    progress: float = Field(description="0.0 – 1.0")
    message: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    match_id: str | None = None
    render_id: str | None = None
    created_at: UTCDateTime
    started_at: UTCDateTime | None = None
    finished_at: UTCDateTime | None = None
    done: bool = Field(description="True once the job reached a final state.")


class TeamScore(BaseModel):
    name: str | None
    score: int | None


class MatchOut(BaseModel):
    id: str
    status: Literal["queued", "analyzing", "ready", "failed"]
    error: str | None = None
    filename: str
    size_bytes: int
    sha256: str
    map: str | None = None
    server_name: str | None = None
    team_a: TeamScore
    team_b: TeamScore
    rounds: int | None = None
    duration_s: float | None = None
    tickrate: int | None = None
    total_ticks: int | None = None
    highlight_count: int = 0
    played_at: UTCDateTime | None = Field(None, description="When the match was played, if its source says so.")
    source_id: str | None = None
    remote_id: str | None = None
    page_url: str | None = Field(None, description="The match page on the site it was imported from.")
    brand: str | None = None
    created_at: UTCDateTime
    analyzed_at: UTCDateTime | None = None
    links: dict[str, str] = {}


class RenderOptionsIn(BaseModel):
    format: Literal["16:9", "9:16", "1:1"] | None = Field(
        None, description="Shape of the video. 9:16 (1080x1920) is laid out for phones and ready to post. "
                          "Default 16:9. Ignored when width and height are both given.")
    width: int | None = Field(None, ge=320, le=3840)
    height: int | None = Field(None, ge=240, le=2160)
    fps: int | None = Field(None, ge=10, le=120)
    crf: int | None = Field(None, ge=10, le=40, description="x264 quality; lower is better (default 20).")
    title_cards: bool | None = Field(None, description="Show a title card before each clip.")
    intro: bool | None = Field(None, description="Brand intro bumper (default: on when a brand kit is set).")
    outro: bool | None = Field(None, description="Brand outro bumper (default: on when a brand kit is set).")
    music: bool | None = Field(None, description="Brand music bed, if the kit has one (default: on).")
    hud: bool | None = Field(None, description="Branded HUD over in-game footage: nameplate, score, kill counter "
                                               "(default: on).")
    ramp: bool | None = Field(None, description="In-game videos: play the quiet stretches between two kills fast "
                                                "instead of in real time (default: on).")
    chat: bool | None = Field(None, description="In-game videos: show what the lobby typed about the play at the "
                                                "end of it (default: as the brand kit says, on).")
    extra: dict[str, Any] = Field(default_factory=dict, description="Renderer specific options.")


class RenderOut(BaseModel):
    id: str
    match_id: str
    kind: Literal["highlight", "reel"]
    title: str | None = None
    highlight_ids: list[str]
    renderer: str
    options: dict[str, Any] = {}
    status: Literal["queued", "running", "ready", "failed", "canceled"]
    error: str | None = None
    job_id: str | None = None
    video_url: str | None = None
    thumbnail_url: str | None = None
    duration_s: float | None = None
    size_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    format: str = Field("16:9", description="16:9, 9:16 or 1:1.")
    body_offset_s: float | None = Field(None, description="Seconds into the video where the play itself starts "
                                                          "(after the intro bumper). Add it to times computed "
                                                          "from ticks. None for videos made before this field.")
    kill_marks: list[dict[str, Any]] | None = Field(None, description=(
        "When each kill of the play happens in this video: [{\"tick\": demo tick, \"t\": seconds from the start "
        "of the video}]. Exact, including the intro, speed ramps and freeze frames. None for videos made before "
        "this field and for 2D replays (estimate from the segments and body_offset_s there)."))
    stale: bool = Field(False, description="True if a highlight was edited after this video was rendered.")
    created_at: UTCDateTime
    finished_at: UTCDateTime | None = None


class HighlightPlayer(BaseModel):
    steamid: str | None
    name: str | None


class HighlightOut(BaseModel):
    id: str
    match_id: str
    kind: str
    title: str
    description: str
    round: int | None
    player: HighlightPlayer
    score: float
    category: Literal["play", "fail"] = Field("play", description="fail = whiffs, team flashes, team kills, own "
                                                                 "grenades… (docs/metrics.md).")
    facts: dict[str, Any] = Field(default_factory=dict, description=(
        "Fails: what happened, as data - e.g. whiff: shots, weapon, distance_m, killer; team flash: thrower, "
        "blinded, blind_s, killer, seconds_to_death. People are {steamid, name}. Build the sentence from this, "
        "not from the English title."))
    team: Literal["team_a", "team_b"] | None = None
    tier: Literal["S", "A", "B", "C"] | None = Field(None, description="S = a play of the match … C = minor.")
    featured: bool = Field(False, description="One of the best plays of its team, or a stand-out play: these "
                                              "get first-person videos; the others stay as 2D replays.")
    featured_rank: int | None = None
    breakdown: dict[str, float] = Field(default_factory=dict, description=(
        "Why the score: (feat + impact + style) * economy * stakes * outcome - penalty. See docs/metrics.md."))
    tags: list[str]
    segments: list[HighlightSegment]
    kill_ticks: list[int]
    chat: list[dict[str, Any]] = Field(default_factory=list, description=(
        "What was typed in the all-chat right after this play's kills (at most 3 lines; crying and laughing "
        "first): tick, name, steamid, text, kind, relation (victim | killer | other)."))
    duration_s: float
    origin: Literal["auto", "manual", "agent"]
    version: int
    renders: list[RenderOut] = []
    created_at: UTCDateTime
    updated_at: UTCDateTime


class HighlightCreate(BaseModel):
    """Create a highlight in one of three ways: explicit ``segments``; a tick range (``start_tick``/``end_tick``);
    or ``round`` + ``player_steamid`` to clip that player's kills in that round automatically."""

    title: str | None = None
    description: str | None = None
    tags: list[str] = []
    segments: list[HighlightSegment] | None = None
    start_tick: int | None = None
    end_tick: int | None = None
    pov_steamid: str | None = None
    round: int | None = None
    player_steamid: str | None = None
    origin: Literal["manual", "agent"] = "manual"


class HighlightPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    segments: list[HighlightSegment] | None = Field(None, description="Replaces all segments.")
    featured: bool | None = Field(None, description="Promote a play to (or remove it from) the featured ones, "
                                                    "which get first-person videos. A manual choice is kept "
                                                    "when the match is re-scored. Send null explicitly to hand "
                                                    "the decision back to the automatic curation.")


# ---- edit operations: small, validated, composable — designed for LLM tool calls ---------------------

class OpTrim(BaseModel):
    op: Literal["trim"]
    segment: int = Field(0, description="Segment index (0-based).")
    start_delta_s: float = Field(0.0, description="Seconds added to the start (negative = start earlier).")
    end_delta_s: float = Field(0.0, description="Seconds added to the end (positive = end later).")


class OpSetRange(BaseModel):
    op: Literal["set_range"]
    segment: int = 0
    start_tick: int
    end_tick: int


class OpSetPov(BaseModel):
    op: Literal["set_pov"]
    segment: int | None = Field(None, description="Segment index, or null for every segment.")
    steamid: str | None = Field(description="Player to watch; null = free overview camera.")


class OpSetSpeed(BaseModel):
    op: Literal["set_speed"]
    segment: int | None = None
    speed: float = Field(gt=0, le=8, description="1 = real time, 0.5 = slow motion, 2 = fast forward.")


class OpSetCaption(BaseModel):
    op: Literal["set_caption"]
    segment: int = 0
    caption: str | None


class OpAddSegment(BaseModel):
    op: Literal["add_segment"]
    start_tick: int
    end_tick: int
    pov_steamid: str | None = None
    speed: float = Field(1.0, gt=0, le=8)
    caption: str | None = None
    index: int | None = Field(None, description="Insert position; default = append.")


class OpRemoveSegment(BaseModel):
    op: Literal["remove_segment"]
    segment: int


class OpMoveSegment(BaseModel):
    op: Literal["move_segment"]
    segment: int
    to: int


class OpSplitSegment(BaseModel):
    op: Literal["split_segment"]
    segment: int
    at_tick: int = Field(description="Tick inside the segment where it is cut in two.")


class OpSetMeta(BaseModel):
    op: Literal["set_meta"]
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None


EditOp = Annotated[
    OpTrim | OpSetRange | OpSetPov | OpSetSpeed | OpSetCaption | OpAddSegment | OpRemoveSegment | OpMoveSegment
    | OpSplitSegment | OpSetMeta,
    Field(discriminator="op"),
]


class EditRequest(BaseModel):
    ops: list[EditOp] = Field(min_length=1, description="Applied in order; all-or-nothing.")
    origin: Literal["manual", "agent"] | None = None


class RenderRequest(BaseModel):
    renderer: str | None = Field(None, description="'radar', 'ingame' or 'auto' (default: server setting).")
    options: RenderOptionsIn = Field(default_factory=RenderOptionsIn)


class ReelRequest(BaseModel):
    """A reel is several highlights rendered back to back into one video."""

    title: str | None = None
    highlight_ids: list[str] | None = Field(None, description="Explicit order. Omit to use the top-scored ones.")
    top: int = Field(5, ge=1, le=30, description="How many top highlights to include when highlight_ids is omitted.")
    player_steamid: str | None = Field(None, description="Only this player's highlights (with `top`).")
    renderer: str | None = None
    options: RenderOptionsIn = Field(default_factory=RenderOptionsIn)


class WebhookIn(BaseModel):
    url: str
    events: list[str] = Field(default_factory=list, description="Empty = all events.")
    secret: str | None = None


class WebhookOut(BaseModel):
    id: str
    url: str
    events: list[str]
    active: bool
    created_at: UTCDateTime


class BrandOut(BaseModel):
    slug: str
    name: str
    tagline: str | None = None
    url: str | None = None
    logo_url: str | None = None
    colors: dict[str, str]
    fonts: dict[str, str]
    locale: str
    ui: dict[str, Any] = Field(default_factory=dict, description="Web UI options of the kit (see docs/branding.md).")


class SourceOut(BaseModel):
    id: str
    name: str
    url: str | None = None
    paused_until: UTCDateTime | None = Field(None, description="Set while the source is being left alone after "
                                                                "it reported overload; imports are refused.")
    paused_reason: str | None = None


class RemoteMatchOut(BaseModel):
    remote_id: str
    map: str | None = None
    played_at: UTCDateTime | None = None
    score_a: int | None = None
    score_b: int | None = None
    has_demo: bool
    page_url: str | None = None
    match_id: str | None = Field(None, description="Set once this match has been imported here.")
    status: str | None = Field(None, description="Status of the imported match, or 'importing' while downloading.")


class ImportRequest(BaseModel):
    remote_ids: list[str] = Field(min_length=1, max_length=50)
    auto_render_top: int | None = Field(None, ge=0, le=20)


class ImportAccepted(BaseModel):
    jobs: list[JobOut]
    skipped: list[str] = Field(default_factory=list, description="Remote ids already imported or without a demo.")
    detail: str | None = None


class FeedMatch(BaseModel):
    id: str
    tickrate: int | None = None
    map: str | None
    team_a: TeamScore
    team_b: TeamScore
    played_at: UTCDateTime | None = None


class FeedHighlight(BaseModel):
    id: str
    kind: str
    title: str
    description: str
    round: int | None
    player: HighlightPlayer
    score: float
    team: Literal["team_a", "team_b"] | None = None
    tier: Literal["S", "A", "B", "C"] | None = Field(None, description="S = a play of the match … C = minor.")
    featured: bool = Field(False, description="One of the best plays of its team, or a stand-out play: these "
                                              "get first-person videos; the others stay as 2D replays.")
    featured_rank: int | None = None
    breakdown: dict[str, float] = Field(default_factory=dict, description=(
        "Why the score: (feat + impact + style) * economy * stakes * outcome - penalty. See docs/metrics.md."))
    tags: list[str]
    duration_s: float
    segments: list[HighlightSegment] = []
    kill_ticks: list[int] = []


class FeedItem(BaseModel):
    highlight: FeedHighlight
    match: FeedMatch
    render: RenderOut


class ProduceRequest(BaseModel):
    """Make every video of a match in one go."""

    renderers: list[Literal["ingame", "radar"]] = Field(default_factory=lambda: ["ingame", "radar"])
    formats: list[Literal["16:9", "9:16", "1:1"]] = Field(default_factory=lambda: ["16:9", "9:16"])
    highlight_ids: list[str] | None = Field(None, description="Default: every highlight of the match.")
    pov: Literal["featured", "all"] = Field("featured", description=(
        "Which plays get first-person ('ingame') videos: only the featured ones (the best of each team plus "
        "stand-out plays - the default, recording costs game time) or all. 2D replays are made for every "
        "play either way. Ignored for plays named in highlight_ids."))
    force: bool = Field(False, description="Render again even where an up-to-date video exists.")


class ProduceAccepted(BaseModel):
    capture_job: JobOut | None = Field(None, description="The game session that records what is missing, if any.")
    render_jobs: int = Field(description="Renders queued now (more follow when the capture finishes).")
    pending_after_capture: int = 0
    skipped: int = Field(0, description="Videos that already exist and are up to date.")
    featured: int = Field(0, description="Plays that get first-person videos in this request.")
    detail: str | None = None


class UploadResult(BaseModel):
    match: MatchOut
    job: JobOut | None = None
    created: bool = Field(description="False when this exact demo (same SHA-256) was already uploaded.")


# ------------------------------------------------------------------ insights across matches (insights.py)

class InsightPlayer(BaseModel):
    steamid: str
    name: str = Field(description="The name used in the player's most recent match of the scope.")


class InsightScope(BaseModel):
    """Which matches an insight was computed from."""

    since: UTCDateTime | None = None
    until: UTCDateTime | None = None
    night: str | None = Field(None, description="YYYY-MM-DD: the evening the night started (local time).")
    match_id: str | None = None
    matches: list[str] = Field(default_factory=list, description="Ids of the matches used, oldest first.")
    nights: list[str] = Field(default_factory=list, description="Nights (YYYY-MM-DD) those matches belong to.")
    rounds: int = 0


class NightOut(BaseModel):
    """One night of play: every match played from noon of ``date`` to noon of the next day, local time."""

    date: str = Field(description="YYYY-MM-DD of the evening the night started (a match at 02:00 belongs to "
                                  "the night before).")
    matches: list[str] = Field(description="Match ids in the order they were played.")
    maps: list[str] = Field(default_factory=list)
    players: int = Field(description="Different human players seen that night.")
    rounds: int
    first_played_at: UTCDateTime | None = None
    last_played_at: UTCDateTime | None = None


class PlayerSummary(BaseModel):
    player: InsightPlayer
    names: list[str] = Field(default_factory=list, description="Every name the player used in the scope.")
    matches: int
    wins: int
    rounds: int
    kills: int
    deaths: int
    adr: float
    rating: float


class RivalryPair(BaseModel):
    """Two players who killed each other. ``a`` is the one ahead (ties: lower SteamID)."""

    a: InsightPlayer
    b: InsightPlayer
    a_kills: int = Field(description="Times a killed b.")
    b_kills: int = Field(description="Times b killed a.")
    duels: int = Field(description="a_kills + b_kills.")
    diff: int = Field(description="a_kills - b_kills (never negative).")
    imbalance: float = Field(description="diff / duels: 0 = even, 1 = one-way traffic.")
    matches: int = Field(description="Matches in which they were on opposite teams.")
    a_openings: int = Field(0, description="Rounds a opened by killing b.")
    b_openings: int = 0
    a_humiliations: int = Field(0, description="Knife or Zeus kills of a on b.")
    b_humiliations: int = 0


class RivalryRef(BaseModel):
    """One opponent seen from a player's side."""

    player: InsightPlayer
    kills: int = Field(description="Times the player killed this opponent.")
    deaths: int = Field(description="Times this opponent killed the player.")
    diff: int = Field(description="kills - deaths.")
    matches: int = 0


class RivalryPlayer(BaseModel):
    player: InsightPlayer
    kills: int = Field(description="Kills on human opponents in the scope.")
    deaths: int
    nemesis: RivalryRef | None = Field(None, description="'Carrasco': the opponent with the best balance "
                                                         "against this player.")
    victim: RivalryRef | None = Field(None, description="'Freguês': the opponent this player has the best "
                                                        "balance against.")


class RivalriesOut(BaseModel):
    scope: InsightScope
    min_duels: int = Field(description="Pairs with fewer kills between them are left out.")
    total_pairs: int = Field(0, description="Pairs that pass min_duels, before `limit`.")
    pairs: list[RivalryPair]
    players: list[RivalryPlayer]


class TrophyRunnerUp(BaseModel):
    player: InsightPlayer
    value: float


class TrophyOut(BaseModel):
    key: str
    title_pt: str
    title_en: str
    description_pt: str = Field("", description="What the trophy is given for.")
    description_en: str = ""
    player: InsightPlayer
    value: float
    detail: str = Field("", description="The numbers behind it, in Brazilian Portuguese.")
    detail_en: str = ""
    match_id: str | None = Field(None, description="Set when the trophy is about one moment (a streak…).")
    round: int | None = None
    runners_up: list[TrophyRunnerUp] = Field(default_factory=list)


class TrophiesOut(BaseModel):
    scope: InsightScope
    kind: Literal["match", "night"]
    trophies: list[TrophyOut]


class ChatExample(BaseModel):
    text: str = Field(description="The line as it may be shown (slurs masked).")
    match_id: str
    round: int | None = None
    kind: str | None = None
    relation: str | None = Field(None, description="victim: written right after dying; killer; other.")
    about: str | None = Field(None, description="Name of who had just killed the author, for lines written "
                                                "right after dying.")


class ChatPlayerRow(BaseModel):
    player: InsightPlayer
    matches: int = 0
    rounds: int = Field(0, description="Rounds played in the scope (0: only typed, e.g. a spectator).")
    messages: int = Field(description="Lines typed, server commands excluded.")
    commands: int = 0
    whine: int = 0
    death_cries: int = Field(0, description="Whining or swearing within 20 s of the author's own death.")
    toxic: int = 0
    laugh: int = 0
    nice: int = Field(0, description="Praise or good-game lines.")
    shouting: int = 0
    whine_per_round: float = 0.0
    death_cries_per_round: float = 0.0
    toxic_per_round: float = 0.0
    laugh_per_round: float = 0.0
    nice_per_round: float = 0.0
    shouting_per_round: float = 0.0
    messages_per_round: float = Field(0.0, description="Lines typed per round played: the tie-break of `talker`.")
    cried_about: InsightPlayer | None = Field(None, description="Whose kills made this player cry the most.")
    cried_about_count: int = 0


class ChatPodiumEntry(BaseModel):
    player: InsightPlayer
    count: int
    per_round: float
    examples: list[ChatExample] = Field(default_factory=list)


class ChatPodium(BaseModel):
    key: str = Field(description="toxic | whine | death_cry | talker | laugh | nice | shouting. `talker` ranks by "
                                 "`messages` (every line typed) and has no examples.")
    title_pt: str
    title_en: str
    entries: list[ChatPodiumEntry]


class ChatRankingsOut(BaseModel):
    scope: InsightScope
    messages: int = Field(description="Lines typed by players in the scope, server commands excluded.")
    note: str = Field("", description="How far the numbers can be trusted.")
    podiums: list[ChatPodium]
    players: list[ChatPlayerRow]


class ProfilePlay(BaseModel):
    highlight_id: str | None = Field(None, description="Id of the stored highlight (GET /highlights/{id}).")
    match_id: str
    kind: str
    title: str
    round: int | None = None
    score: float
    tier: str | None = None
    featured: bool = False


class ProfileMatch(BaseModel):
    match_id: str
    map: str | None = None
    played_at: UTCDateTime | None = None
    night: str
    won: bool | None = None
    score: str = Field(description="Own team first, e.g. '13-11'.")
    kills: int
    deaths: int
    assists: int
    adr: float
    rating: float


class ProfileWeapon(BaseModel):
    weapon: str = Field(description="Display name, e.g. 'AK-47'.")
    kills: int
    headshots: int
    headshot_pct: float = Field(description="Headshot kills / kills with this weapon, in %.")
    share_pct: float = Field(description="Kills with this weapon / all his kills, in %.")


class PlayerProfileOut(BaseModel):
    player: InsightPlayer
    names: list[str] = Field(default_factory=list)
    scope: InsightScope
    matches: int
    wins: int
    losses: int
    draws: int
    rounds: int
    kills: int
    deaths: int
    assists: int
    kd_ratio: float
    kd_diff: int
    headshot_kills: int
    headshot_pct: float
    damage: int
    adr: float = Field(description="Damage / rounds over the whole scope.")
    kast: float = Field(description="Average of the matches, weighted by rounds played.")
    rating: float = Field(description="Average of the matches, weighted by rounds played.")
    opening_kills: int
    opening_deaths: int
    trade_kills: int
    multi_kills: dict[str, int] = Field(default_factory=dict)
    clutches_won: int
    clutches_attempted: int
    clutches_by_size: dict[str, int] = Field(default_factory=dict)
    team_kills: int = 0
    teammates_flashed: int = 0
    enemies_flashed: int = 0
    flash_assists: int = 0
    humiliation_kills: int = Field(0, description="Knife or Zeus kills.")
    humiliation_deaths: int = 0
    weapons: dict[str, int] = Field(default_factory=dict, description="Kills with the five most used weapons.")
    weapon_stats: list[ProfileWeapon] = Field(default_factory=list, description=(
        "Every weapon he killed with, most kills first (ties: name): kills, headshots, HS% and share of his kills."))
    best_plays: list[ProfilePlay] = Field(default_factory=list)
    fails: int = 0
    fails_by_kind: dict[str, int] = Field(default_factory=dict)
    worst_fails: list[ProfilePlay] = Field(default_factory=list)
    nemesis: RivalryRef | None = None
    victim: RivalryRef | None = None
    chat: ChatPlayerRow | None = None
    history: list[ProfileMatch] = Field(default_factory=list, description="His matches, newest first.")


# ---------------------------------------------------------------------------------------- remote workers

class WorkerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.-]*$",
                      description="A label for the machine, e.g. 'recording-pc'.")
    capabilities: list[str] = Field(default_factory=lambda: ["cpu"],
                                    description="What it may take: 'cpu' (analysis, 2D videos, editing stored "
                                                "footage) and 'ingame' (recording with the CS2 client).")


class WorkerOut(BaseModel):
    id: str
    name: str
    capabilities: list[str]
    created_at: UTCDateTime
    last_seen_at: UTCDateTime | None = None
    online: bool = Field(description="Heard from in the last few minutes.")
    info: dict[str, Any] | None = Field(None, description="What the worker said about itself (version, system).")
    running_job: str | None = Field(None, description="Id of the job it is working on, if any.")
    masters: int = Field(0, description="Recordings (raw first-person footage) it keeps.")


class WorkerCreated(BaseModel):
    worker: WorkerOut
    token: str = Field(description="The worker's secret. Shown only now: the server keeps a hash of it.")


class WorkerClaim(BaseModel):
    capabilities: list[str] = Field(default_factory=lambda: ["cpu"],
                                    description="What the worker can take right now. Leave 'ingame' out while "
                                                "the machine must not record (people playing on it).")
    info: dict[str, Any] | None = Field(None, description="Version, operating system, anything worth showing.")


class WorkerProgress(BaseModel):
    progress: float = Field(0.0, ge=0.0, le=1.0)
    message: str | None = Field(None, max_length=255)


class WorkerFinish(BaseModel):
    status: Literal["succeeded", "failed", "canceled", "released"] = Field(
        description="'released' gives the job back untouched (e.g. players joined before recording started).")
    result: dict[str, Any] = Field(default_factory=dict, description="What came out; fields depend on the job type.")
    error: str | None = Field(None, max_length=2000, description="Why it failed, for a person to read.")
    retry: bool = Field(False, description="With 'failed': put the job back in the queue instead of giving up.")


class WorkerMasters(BaseModel):
    masters: list[dict[str, Any]] = Field(description="Every recording the worker keeps, as its .json files say.")
