"""Public data contract of a match analysis.

Everything the API / MCP server returns about a match is one of these models. SteamIDs are always strings
(64-bit ids overflow JavaScript numbers). Ticks are demo ticks; divide by ``tickrate`` for seconds.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1

Side = Literal["CT", "T"]
TeamKey = Literal["team_a", "team_b"]


class PlayerRef(BaseModel):
    steamid: str
    name: str
    side: Side | None = None


class KillEvent(BaseModel):
    tick: int
    round: int
    clock_s: float = Field(description="Seconds since the round went live (freeze time end).")
    attacker: PlayerRef | None = None
    victim: PlayerRef
    assister: PlayerRef | None = None
    weapon: str
    weapon_name: str
    headshot: bool = False
    wallbang: bool = False
    noscope: bool = False
    through_smoke: bool = False
    attacker_blind: bool = False
    attacker_airborne: bool = False
    flash_assist: bool = False
    distance_m: float | None = None
    is_teamkill: bool = False
    is_suicide: bool = False
    is_opening: bool = False
    is_trade: bool = Field(False, description="Attacker avenged a teammate killed by the victim within the trade window.")
    after_round_end: bool = False
    attacker_place: str | None = None
    victim_place: str | None = None
    victim_weapon: str | None = Field(None, description="What the victim was holding when he died.")
    attacker_health: int | None = Field(None, description="Attacker's health at the kill.")
    victim_blind_s: float | None = Field(None, description="Seconds of flash blindness the victim still had.")
    fight_start_tick: int | None = Field(None, description=(
        "Where the exchange that led to this kill began for the attacker: the first of his shots / damage "
        "dealt or taken in an unbroken run (gaps of at most 2 s) ending at the kill. Clips start shortly "
        "before it instead of a fixed time before the kill."))


class ClutchEvent(BaseModel):
    round: int
    player: PlayerRef
    versus: int = Field(description="Enemies alive when the player became the last one standing.")
    start_tick: int
    won: bool
    kills: int
    survived: bool


class RoundSummary(BaseModel):
    number: int
    start_tick: int
    freeze_end_tick: int
    end_tick: int
    official_end_tick: int
    duration_s: float
    winner_side: Side
    winner_team: TeamKey
    reason: str
    ct_team: TeamKey
    t_team: TeamKey
    ct_players: list[str] = Field(default_factory=list, description="SteamIDs on CT this round.")
    t_players: list[str] = Field(default_factory=list, description="SteamIDs on T this round.")
    score_team_a: int = Field(description="Score after this round.")
    score_team_b: int
    is_pistol: bool = False
    is_overtime: bool = False
    bomb_planted: bool = False
    bomb_site: str | None = None
    plant_tick: int | None = None
    defuse_tick: int | None = None
    explode_tick: int | None = None
    planter: PlayerRef | None = None
    defuser: PlayerRef | None = None
    ct_equip_value: int | None = None
    t_equip_value: int | None = None
    ct_buy: str | None = None
    t_buy: str | None = None
    kills: list[KillEvent] = []
    clutches: list[ClutchEvent] = []
    activity: dict[str, list[int]] = Field(default_factory=dict, description=(
        "Per SteamID: the ticks at which the player fired a bullet, dealt damage or took damage from an enemy "
        "this round. Clips are cut and sped up around it."))


class UtilityStats(BaseModel):
    he_damage: int = 0
    fire_damage: int = 0
    flashes_thrown: int = 0
    smokes_thrown: int = 0
    hes_thrown: int = 0
    molotovs_thrown: int = 0
    enemies_flashed: int = 0
    teammates_flashed: int = 0
    enemy_blind_time_s: float = 0.0
    flash_assists: int = 0


class SideStats(BaseModel):
    rounds: int = 0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    damage: int = 0
    adr: float = 0.0
    kast: float = 0.0
    rating: float = 0.0


class PlayerStats(BaseModel):
    steamid: str
    name: str
    team: TeamKey
    rounds_played: int
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    kd_diff: int = 0
    kd_ratio: float = 0.0
    headshot_kills: int = 0
    headshot_pct: float = 0.0
    damage: int = 0
    adr: float = 0.0
    kast: float = Field(0.0, description="% of rounds with a Kill, Assist, Survival or Traded death.")
    kpr: float = 0.0
    dpr: float = 0.0
    impact: float = 0.0
    rating: float = Field(0.0, description="Approximation of HLTV Rating 2.0 (public reverse-engineered formula).")
    opening_kills: int = 0
    opening_deaths: int = 0
    trade_kills: int = 0
    traded_deaths: int = 0
    multi_kills: dict[str, int] = Field(default_factory=lambda: {"2k": 0, "3k": 0, "4k": 0, "5k": 0})
    clutches_won: int = 0
    clutches_attempted: int = 0
    clutches_by_size: dict[str, int] = Field(default_factory=dict, description='Won clutches keyed "1v1".."1v5".')
    wallbang_kills: int = 0
    noscope_kills: int = 0
    through_smoke_kills: int = 0
    blind_kills: int = 0
    airborne_kills: int = 0
    team_kills: int = 0
    shots_fired: int = 0
    shots_hit: int = 0
    accuracy: float = 0.0
    avg_kill_distance_m: float | None = None
    money_spent: int | None = None
    mvps: int | None = None
    utility: UtilityStats = Field(default_factory=UtilityStats)
    weapon_kills: dict[str, int] = Field(default_factory=dict)
    by_side: dict[str, SideStats] = Field(default_factory=dict)


class TeamSummary(BaseModel):
    key: TeamKey
    name: str
    score: int
    starting_side: Side
    players: list[str] = Field(description="SteamIDs.")
    rounds_won_ct: int = 0
    rounds_won_t: int = 0
    pistol_rounds_won: int = 0
    opening_kills: int = 0
    bomb_plants: int = 0
    bomb_defuses: int = 0
    won: bool | None = None


class SegmentCamera(BaseModel):
    """A fixed camera position, as CS2's ``spec_pos`` / ``getpos`` print it (``spec_goto x y z pitch yaw``)."""

    x: float
    y: float
    z: float
    pitch: float = Field(description="Up/down angle in degrees (positive looks down).")
    yaw: float = Field(description="Left/right angle in degrees.")


class HighlightSegment(BaseModel):
    """A continuous slice of the demo, watched from one player's point of view."""

    start_tick: int
    end_tick: int
    pov_steamid: str | None = Field(None, description="Player whose POV is shown. None = free/overview camera.")
    camera: SegmentCamera | None = Field(None, description=(
        "Watch the segment from this fixed position instead of a player (in-game renderer only). Leave "
        "pov_steamid empty. In CS2, `spec_pos` in the console prints the position of the free camera."))
    speed: float = Field(1.0, gt=0, le=8, description="Playback speed multiplier (0.5 = slow motion).")
    caption: str | None = None
    role: str = Field("play", description="play | replay (the same moment again from another point of view; "
                                          "the video HUD shows only the caption on it).")


class HighlightCandidate(BaseModel):
    kind: str = Field(description="ace | quad_kill | triple_kill | double_kill | clutch | clutch_attempt (left "
                                  "alone against 3+, one kill short) | special_kill | ninja_defuse")
    title: str
    description: str
    round: int
    player: PlayerRef
    score: float = Field(description="Relative interest score; higher is better.")
    tags: list[str] = []
    segments: list[HighlightSegment]
    kill_ticks: list[int] = []
    start_tick: int
    end_tick: int
    duration_s: float
    team: TeamKey | None = Field(None, description="Team of the player in that round.")
    tier: str | None = Field(None, description="S (a play of the match) | A | B | C, from the score.")
    breakdown: dict[str, float] = Field(default_factory=dict, description=(
        "Why the play has this score: points for feat (what was done), impact (how much it changed the round) "
        "and style (how), then the multipliers economy, stakes and outcome, and the penalty for long clips. "
        "score = (feat + impact + style) * economy * stakes * outcome - penalty. Also: impact_max (the "
        "most impact can be) and win_before (the team's chance of winning the round just before the play, 0-1)."))
    featured: bool = Field(False, description="Picked by the curation: one of the best plays of its team or a "
                                              "stand-out play. Featured plays get first-person videos.")
    featured_rank: int | None = Field(None, description="1 = best featured play of the match (fails are ranked "
                                                        "among fails).")
    category: str = Field("play", description="play | fail (see analysis/fails.py: whiffs, team flashes, team "
                                              "kills, own grenades…).")
    facts: dict[str, Any] = Field(default_factory=dict, description=(
        "Fails: what happened, as data (texts are for people). Keys by kind - whiff: shots, weapon, distance_m, "
        "killer{steamid,name}; team/self flash: thrower, blinded, blind_s, killer, seconds_to_death; teamkill: "
        "victim, weapon; own grenade / fall / bomb: weapon; knife out: killer, distance_m."))
    action_ticks: list[int] = Field(default_factory=list, description="Ticks inside the clip at which its player "
                                                                      "shot, hit or was hit (see RoundSummary.activity).")


class MatchAnalysis(BaseModel):
    schema_version: int = SCHEMA_VERSION
    map: str
    server_name: str | None = None
    demo_source: str | None = Field(None, description="e.g. 'SourceTV Demo' (GOTV) or a player POV recording.")
    tickrate: int
    total_ticks: int
    duration_s: float
    rounds_played: int = Field(description="Rounds present in the demo (and analyzed).")
    rounds_in_match: int | None = Field(None, description="Rounds the match actually had, from the scoreboard. "
                                                           "Larger than rounds_played when the demo is incomplete.")
    team_a: TeamSummary
    team_b: TeamSummary
    players: list[PlayerStats]
    rounds: list[RoundSummary]
    highlights: list[HighlightCandidate]
    fails: list[HighlightCandidate] = Field(default_factory=list, description="The match's fails, funniest first.")
    chat: list[dict] = Field(default_factory=list, description=(
        "All-chat lines in order: tick, name, steamid, text, plus round, kind (command | toxic | whine | laugh | "
        "praise | gg | talk), tags, and - when the line comes within 20 s of a kill - kill_tick, kill_attacker, "
        "kill_victim and relation (victim | killer | other). See analysis/chat.py."))
    warnings: list[str] = []
