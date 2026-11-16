"""Insights across matches: nights, rivalries, player profiles, trophies and chat rankings.

Nothing here reads a demo. Everything is aggregated from the analyses already stored, so it is cheap and it
covers every match the database has. Two layers:

* pure functions over a list of :class:`Entry` (an analysis plus when it was played) - ``rivalries_of``,
  ``profile_of``, ``trophies_of``, ``chat_rankings_of``, ``nights_of``, ``players_of``;
* the same with a database session and a :class:`Scope` - ``rivalries``, ``player_profile``, ``trophies``,
  ``chat_rankings``, ``nights``, ``players`` - which is what the REST API and the MCP server call.

Players are SteamIDs; bots (``BOT:<name>``) never appear in a ranking. Definitions are documented in
``docs/metrics.md`` ("Across matches") - change code, doc and a test together.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import service
from .analysis import chat as chat_model
from .analysis import weapons
from .analysis.schema import HighlightCandidate, KillEvent, MatchAnalysis
from .config import get_settings
from .db import Highlight, Match
from .schemas import (
    ChatExample,
    ChatPlayerRow,
    ChatPodium,
    ChatPodiumEntry,
    ChatRankingsOut,
    InsightPlayer,
    InsightScope,
    NightOut,
    PlayerProfileOut,
    PlayerSummary,
    ProfileMatch,
    ProfilePlay,
    ProfileWeapon,
    RivalriesOut,
    RivalryPair,
    RivalryPlayer,
    RivalryRef,
    TrophiesOut,
    TrophyOut,
    TrophyRunnerUp,
)

NIGHT_SHIFT = timedelta(hours=12)  # a night runs from noon to noon: a match at 02:00 belongs to the day before
RIVALRY_SORTS = ("duels", "diff", "imbalance")

# Trophy thresholds: below these nobody gets the trophy.
MVP_MIN_ROUNDS_SHARE = 0.5  # of the rounds played by whoever played the most in the scope
CLUTCH_MIN_ENEMIES = 2  # enemies beaten over the won clutches: one 1v2, or two 1v1
ENTRY_MIN_OPENINGS = 3
STREAK_MIN_KILLS = 5
DROUGHT_MIN_ROUNDS = 5
LOW_HP_MAX = 10
LOW_HP_MIN_KILLS = 2
TEAM_FLASH_MIN = 5
SHARP_AIM_MIN_SHOTS = 150
WHIFF_MIN = 2
KNIFE_OUT_MIN = 2
BAITER_MIN_ROUNDS = 3
ECONOMIST_MIN_KILLS = 8
HEADSHOT_MIN_KILLS = 10

PODIUM_SIZE = 3
PODIUM_EXAMPLES = 2
CHAT_NOTE = ("Word-list classification of all-chat, made for banter between friends - not moderation. It misses "
             "sarcasm and anything it has no word for, and team chat is not in the demos.")

TROPHIES: dict[str, tuple[str, str, str, str]] = {  # key -> title pt, title en, description pt, description en
    "mvp": ("MVP", "MVP", "Maior rating", "Highest rating"),
    "clutch_king": ("Rei do clutch", "Clutch king", "Mais clutches vencidos", "Most clutches won"),
    "entry": ("Abre-alas", "Entry fragger", "Mais primeiras kills do round", "Most opening kills"),
    "kill_streak": ("Imparável", "Unstoppable", "Maior sequência de kills sem morrer",
                    "Longest run of kills without dying"),
    "drought": ("Seca", "Dry spell", "Mais rounds seguidos sem matar ninguém",
                "Most rounds in a row without a kill"),
    "low_hp": ("No talo", "By a thread", f"Mais kills com {LOW_HP_MAX} de vida ou menos",
               f"Most kills on {LOW_HP_MAX} HP or less"),
    "team_flasher": ("Cegador de amigos", "Friendly flasher", "Mais companheiros cegados com flash",
                     "Most team-mates blinded by his flashes"),
    "friendly_fire": ("Fogo amigo", "Friendly fire", "Mais companheiros mortos", "Most team-mates killed"),
    "sharp_aim": ("Mira calibrada", "Dialed in", "Melhor aproveitamento de tiros", "Best share of bullets on target"),
    "whiff": ("Mira descalibrada", "Stormtrooper", "Mais pentes descarregados de perto sem acertar ninguém",
              "Most magazines emptied at close range without a hit"),
    "knife_out": ("Faca na mão", "Knife out", "Mais mortes com a faca na mão", "Most deaths holding the knife"),
    "baiter": ("Baiter", "Baiter", "Mais rounds perdidos em que sobrou por último sem matar ninguém",
               "Most lost rounds as the last one alive without a single kill"),
    "economist": ("Economista", "Economist", "Menos dinheiro gasto por kill", "Least money spent per kill"),
    "headshot_machine": ("Máquina de headshot", "Headshot machine", "Maior percentual de kills na cabeça",
                         "Highest share of headshot kills"),
}

CHAT_PODIUMS: dict[str, tuple[str, str]] = {  # key -> title pt, title en; in the order they are returned
    "toxic": ("Boca suja", "Potty mouth"),
    "whine": ("Chorão", "Cry-baby"),
    "death_cry": ("Choro pós-morte", "Cries after dying"),
    "talker": ("Tagarela", "Chatterbox"),
    "laugh": ("Risadinha", "Giggler"),
    "nice": ("Gente boa", "Good sport"),
    "shouting": ("Gritaria", "Caps lock"),
}
# What a line can be tagged as -> the count it adds to on the player's row.
_CHAT_FIELD = {"whine": "whine", "death_cry": "death_cries", "toxic": "toxic", "laugh": "laugh", "nice": "nice",
               "shouting": "shouting"}
# The count each podium ranks by. "talker" is every line typed, whatever it says: it has no lines to quote.
_PODIUM_FIELD = {**_CHAT_FIELD, "talker": "messages"}


# ---------------------------------------------------------------------------------------------- scope

@dataclass(frozen=True)
class Scope:
    """Which matches to look at. Every filter given must hold; none = every analyzed match."""

    since: datetime | None = None  # played at or after
    until: datetime | None = None  # played before
    match_id: str | None = None
    night: date | None = None  # the evening the night started, local time (see ``night_of``)


@dataclass
class Entry:
    """One analyzed match and when it was played."""

    match_id: str
    played_at: datetime  # aware, UTC
    night: date
    analysis: MatchAnalysis


def is_bot(steamid: str | None) -> bool:
    return not steamid or steamid.startswith("BOT:")


def _utc(d: datetime) -> datetime:
    """The database stores naive UTC; a naive value is taken as UTC."""
    return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)


def local_zone() -> tzinfo | None:
    """The time zone nights are counted in: ``DEMOREEL_TIMEZONE``, else this machine's (None)."""
    name = get_settings().timezone
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError) as exc:  # ZoneInfoNotFoundError is a KeyError
        raise service.ServiceError(f"DEMOREEL_TIMEZONE={name!r} is not a known IANA time zone. "
                                   "Example of a valid one: 'America/Sao_Paulo'.") from exc


def night_of(played_at: datetime, tz: tzinfo | None = None) -> date:
    """The night a match belongs to: the local date twelve hours before it was played, so games that run past
    midnight stay with the evening they started on."""
    return (_utc(played_at).astimezone(tz or local_zone()) - NIGHT_SHIFT).date()


def entry(match_id: str, analysis: MatchAnalysis, played_at: datetime, tz: tzinfo | None = None) -> Entry:
    when = _utc(played_at)
    return Entry(match_id=match_id, played_at=when, night=night_of(when, tz), analysis=analysis)


@lru_cache(maxsize=256)
def _slim_analysis(path: str, mtime: float) -> MatchAnalysis:
    """Own copy of a stored analysis without the per-tick activity (most of the file). A whole season stays in
    memory this way; ``service.load_analysis`` keeps 16 full matches, which a season would thrash."""
    a = MatchAnalysis.model_validate_json(Path(path).read_text(encoding="utf-8"))
    for r in a.rounds:
        r.activity = {}
    for h in [*a.highlights, *a.fails]:
        h.action_ticks = []
    return a


def _ready_matches(s: Session) -> list[tuple[Match, datetime]]:
    rows = s.scalars(select(Match).where(Match.status == "ready", Match.analysis_path.isnot(None))).all()
    return sorted(((m, _utc(m.played_at or m.created_at)) for m in rows), key=lambda x: (x[1], x[0].id))


def _in_window(when: datetime, scope: Scope) -> bool:
    return not ((scope.since is not None and when < _utc(scope.since))
                or (scope.until is not None and when >= _utc(scope.until)))


def load_entries(s: Session, scope: Scope | None = None) -> list[Entry]:
    """The analyzed matches of a scope, oldest first. An unknown match or a night without matches is an error;
    a ``since``/``until`` window may simply be empty."""
    scope = scope or Scope()
    if scope.match_id is not None:
        m = service.get_match(s, scope.match_id)
        service.load_analysis(m)  # raises the "not analyzed yet" conflict
        rows = [(m, _utc(m.played_at or m.created_at))]
    else:
        rows = _ready_matches(s)
    tz = local_zone()
    out: list[Entry] = []
    for m, when in rows:
        if not _in_window(when, scope) or (scope.night is not None and night_of(when, tz) != scope.night):
            continue
        p = service.abs_path(m.analysis_path or "")
        try:
            analysis = _slim_analysis(str(p), p.stat().st_mtime)
        except OSError:  # the row outlived its file: nothing to aggregate
            continue
        out.append(entry(m.id, analysis, when, tz))
    if not out and scope.match_id is not None:
        raise service.ServiceError(f"Match {scope.match_id!r} is outside the requested night / since / until "
                                   "window. Drop those filters to use the match.")
    if not out and scope.night is not None:
        known = sorted({night_of(when, tz) for _m, when in _ready_matches(s) if _in_window(when, scope)}, reverse=True)
        raise service.NotFound(f"No analyzed match was played on the night of {scope.night.isoformat()}. "
                               + (f"Nights with matches: {', '.join(d.isoformat() for d in known[:30])}."
                                  if known else "There are no analyzed matches yet."))
    return out


def _scope_out(entries: list[Entry], scope: Scope | None) -> InsightScope:
    scope = scope or Scope()
    return InsightScope(since=scope.since, until=scope.until, match_id=scope.match_id,
                        night=scope.night.isoformat() if scope.night else None,
                        matches=[e.match_id for e in entries],
                        nights=sorted({e.night.isoformat() for e in entries}),
                        rounds=sum(len(e.analysis.rounds) for e in entries))


# ---------------------------------------------------------------------------------------------- players

def _names(entries: list[Entry]) -> dict[str, str]:
    """SteamID -> the name used in the most recent match."""
    out: dict[str, str] = {}
    for e in sorted(entries, key=lambda x: (x.played_at, x.match_id)):
        for p in e.analysis.players:
            out[p.steamid] = p.name
    return out


def _aliases(entries: list[Entry]) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = defaultdict(list)
    for e in sorted(entries, key=lambda x: (x.played_at, x.match_id), reverse=True):
        for p in e.analysis.players:
            if not is_bot(p.steamid) and p.name not in seen[p.steamid]:
                seen[p.steamid].append(p.name)
    return dict(seen)


def _ref(steamid: str, names: dict[str, str]) -> InsightPlayer:
    return InsightPlayer(steamid=steamid, name=names.get(steamid, steamid))


def _known_players(entries: list[Entry]) -> str:
    aliases = _aliases(entries)
    rows = sorted(aliases.items(), key=lambda x: (x[1][0].lower(), x[0]))
    return ", ".join(f"{names[0]} ({sid})" for sid, names in rows) or "none"


def find_player(entries: list[Entry], player: str) -> str:
    """SteamID, or a (partial, case-insensitive) name the player used in any match of the scope -> SteamID."""
    aliases = _aliases(entries)
    if player in aliases:
        return player
    want = player.strip().lower()
    exact = [sid for sid, names in aliases.items() if any(n.lower() == want for n in names)]
    found = exact or [sid for sid, names in aliases.items() if want and any(want in n.lower() for n in names)]
    if len(found) == 1:
        return found[0]
    if not found:
        raise service.NotFound(f"No player matches {player!r} in the {len(entries)} matches of this scope. "
                               f"Players: {_known_players(entries)}")
    options = ", ".join(f"{aliases[sid][0]} ({sid})" for sid in sorted(found))
    raise service.ServiceError(f"{player!r} is ambiguous: {options}. Use the SteamID.")


@dataclass
class _Totals:
    matches: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    rounds: int = 0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    damage: int = 0
    headshot_kills: int = 0
    kast_w: float = 0.0
    rating_w: float = 0.0
    opening_kills: int = 0
    opening_deaths: int = 0
    trade_kills: int = 0
    clutches_won: int = 0
    clutches_attempted: int = 0
    team_kills: int = 0
    teammates_flashed: int = 0
    enemies_flashed: int = 0
    flash_assists: int = 0
    flashes_thrown: int = 0
    shots_fired: int = 0
    shots_hit: int = 0
    money_spent: int = 0
    money_kills: int = 0  # kills in the matches whose money is known
    multi: Counter = field(default_factory=Counter)
    clutches_by_size: Counter = field(default_factory=Counter)
    weapons: Counter = field(default_factory=Counter)

    @property
    def adr(self) -> float:
        return round(self.damage / self.rounds, 1) if self.rounds else 0.0

    @property
    def kast(self) -> float:
        return round(self.kast_w / self.rounds, 1) if self.rounds else 0.0

    @property
    def rating(self) -> float:
        return round(self.rating_w / self.rounds, 2) if self.rounds else 0.0

    @property
    def headshot_pct(self) -> float:
        return round(100.0 * self.headshot_kills / self.kills, 1) if self.kills else 0.0


def _totals(entries: list[Entry]) -> dict[str, _Totals]:
    """Every human player's numbers added up over the matches. Rates are weighted by rounds played."""
    out: dict[str, _Totals] = defaultdict(_Totals)
    for e in entries:
        a = e.analysis
        won = {"team_a": a.team_a.won, "team_b": a.team_b.won}
        for p in a.players:
            if is_bot(p.steamid):
                continue
            t = out[p.steamid]
            t.matches += 1
            result = won.get(p.team)
            t.wins += result is True
            t.losses += result is False
            t.draws += result is None
            t.rounds += p.rounds_played
            t.kills += p.kills
            t.deaths += p.deaths
            t.assists += p.assists
            t.damage += p.damage
            t.headshot_kills += p.headshot_kills
            t.kast_w += p.kast * p.rounds_played
            t.rating_w += p.rating * p.rounds_played
            t.opening_kills += p.opening_kills
            t.opening_deaths += p.opening_deaths
            t.trade_kills += p.trade_kills
            t.clutches_won += p.clutches_won
            t.clutches_attempted += p.clutches_attempted
            t.team_kills += p.team_kills
            t.teammates_flashed += p.utility.teammates_flashed
            t.enemies_flashed += p.utility.enemies_flashed
            t.flash_assists += p.utility.flash_assists
            t.flashes_thrown += p.utility.flashes_thrown
            t.shots_fired += p.shots_fired
            t.shots_hit += p.shots_hit
            if p.money_spent is not None:
                t.money_spent += p.money_spent
                t.money_kills += p.kills
            t.multi.update({k: v for k, v in p.multi_kills.items() if v})
            t.clutches_by_size.update(p.clutches_by_size)
            t.weapons.update(p.weapon_kills)
    return dict(out)


def players_of(entries: list[Entry]) -> list[PlayerSummary]:
    """Everybody who played in the scope, most matches first."""
    names, aliases = _names(entries), _aliases(entries)
    rows = [PlayerSummary(player=_ref(sid, names), names=aliases.get(sid, []), matches=t.matches, wins=t.wins,
                          rounds=t.rounds, kills=t.kills, deaths=t.deaths, adr=t.adr, rating=t.rating)
            for sid, t in _totals(entries).items()]
    return sorted(rows, key=lambda r: (-r.matches, r.player.name.lower(), r.player.steamid))


# ---------------------------------------------------------------------------------------------- nights

def nights_of(entries: list[Entry]) -> list[NightOut]:
    """One row per night, newest first."""
    by_night: dict[date, list[Entry]] = defaultdict(list)
    for e in entries:
        by_night[e.night].append(e)
    out = []
    for night in sorted(by_night, reverse=True):
        rows = sorted(by_night[night], key=lambda x: (x.played_at, x.match_id))
        humans = {p.steamid for e in rows for p in e.analysis.players if not is_bot(p.steamid)}
        out.append(NightOut(date=night.isoformat(), matches=[e.match_id for e in rows],
                            maps=[e.analysis.map for e in rows], players=len(humans),
                            rounds=sum(len(e.analysis.rounds) for e in rows),
                            first_played_at=rows[0].played_at, last_played_at=rows[-1].played_at))
    return out


# ---------------------------------------------------------------------------------------------- rivalries

def is_humiliation(k: KillEvent) -> bool:
    """Knife or Zeus."""
    return weapons.weapon_class(k.weapon) == "knife" or weapons.normalize(k.weapon) == "taser"


def _duels(e: Entry) -> Iterator[KillEvent]:
    """Kills of one human on an enemy human (no team kills, no suicides), in match order."""
    for r in e.analysis.rounds:
        for k in r.kills:
            if k.attacker is None or k.is_suicide or k.is_teamkill or k.attacker.steamid == k.victim.steamid:
                continue
            if is_bot(k.attacker.steamid) or is_bot(k.victim.steamid):
                continue
            yield k


@dataclass
class _Duels:
    kills: Counter = field(default_factory=Counter)  # (attacker, victim) -> kills
    openings: Counter = field(default_factory=Counter)
    humiliations: Counter = field(default_factory=Counter)
    faced: Counter = field(default_factory=Counter)  # frozenset({x, y}) -> matches on opposite teams
    players: set[str] = field(default_factory=set)

    def ref(self, me: str, other: str, names: dict[str, str]) -> RivalryRef:
        mine, his = self.kills[(me, other)], self.kills[(other, me)]
        return RivalryRef(player=_ref(other, names), kills=mine, deaths=his, diff=mine - his,
                          matches=self.faced[frozenset((me, other))])


def _count_duels(entries: list[Entry]) -> _Duels:
    d = _Duels()
    for e in entries:
        team = {p.steamid: p.team for p in e.analysis.players if not is_bot(p.steamid)}
        d.players |= set(team)
        ids = sorted(team)
        for i, x in enumerate(ids):
            for y in ids[i + 1:]:
                if team[x] != team[y]:
                    d.faced[frozenset((x, y))] += 1
        for k in _duels(e):
            key = (k.attacker.steamid, k.victim.steamid)  # type: ignore[union-attr]
            d.kills[key] += 1
            d.openings[key] += k.is_opening
            d.humiliations[key] += is_humiliation(k)
    return d


def default_min_duels(matches: int) -> int:
    """Two kills per match in the scope, never under 3 nor over 10."""
    return max(3, min(10, 2 * matches))


def _nemesis_and_victim(d: _Duels, me: str, min_duels: int, names: dict[str, str]
                        ) -> tuple[RivalryRef | None, RivalryRef | None]:
    """'Carrasco' and 'freguês': the opponents with the best and the worst balance against ``me``."""
    rivals = sorted({v for a, v in d.kills if a == me} | {a for a, v in d.kills if v == me})
    rows = [(o, d.kills[(me, o)], d.kills[(o, me)]) for o in rivals]
    rows = [r for r in rows if r[1] + r[2] >= min_duels]
    worst = min(rows, key=lambda r: (r[1] - r[2], -r[2], r[0]), default=None)  # biggest deficit, then most deaths
    best = min(rows, key=lambda r: (r[2] - r[1], -r[1], r[0]), default=None)  # biggest surplus, then most kills
    nemesis = d.ref(me, worst[0], names) if worst and worst[1] < worst[2] else None
    victim = d.ref(me, best[0], names) if best and best[1] > best[2] else None
    return nemesis, victim


def rivalries_of(entries: list[Entry], *, player: str | None = None, min_duels: int | None = None,
                 sort: str = "duels", limit: int | None = None, scope: Scope | None = None) -> RivalriesOut:
    """Who kills whom.

    ``pairs``: every two players with at least ``min_duels`` kills between them (default: two per match in the
    scope, between 3 and 10), the one ahead first, with the opening kills and the knife / Zeus kills of each
    on the other. ``sort``: ``duels`` (most kills between them), ``diff`` or ``imbalance`` (diff / duels).
    ``players``: for everybody (or just ``player``) the nemesis ("carrasco": best balance against him) and the
    favourite victim ("freguês"), among opponents with at least ``min_duels`` kills between them.
    """
    if sort not in RIVALRY_SORTS:
        raise service.ServiceError(f"sort must be one of {', '.join(RIVALRY_SORTS)} (got {sort!r}).")
    if min_duels is not None and min_duels < 1:
        raise service.ServiceError("min_duels must be 1 or more.")
    names = _names(entries)
    d = _count_duels(entries)
    if player is not None and player not in d.players:
        raise service.NotFound(f"No player with SteamID {player!r} in the {len(entries)} matches of this scope. "
                               f"Players: {_known_players(entries)}")
    floor = min_duels if min_duels is not None else default_min_duels(len(entries))

    pairs: list[RivalryPair] = []
    for key in {frozenset(k) for k in d.kills}:
        x, y = sorted(key)
        if player is not None and player not in key:
            continue
        kx, ky = d.kills[(x, y)], d.kills[(y, x)]
        if kx + ky < floor:
            continue
        a, b = (x, y) if kx >= ky else (y, x)
        ka, kb = max(kx, ky), min(kx, ky)
        pairs.append(RivalryPair(
            a=_ref(a, names), b=_ref(b, names), a_kills=ka, b_kills=kb, duels=ka + kb, diff=ka - kb,
            imbalance=round((ka - kb) / (ka + kb), 3), matches=d.faced[key],
            a_openings=d.openings[(a, b)], b_openings=d.openings[(b, a)],
            a_humiliations=d.humiliations[(a, b)], b_humiliations=d.humiliations[(b, a)]))
    order = {"duels": lambda p: (-p.duels, -p.diff), "diff": lambda p: (-p.diff, -p.duels),
             "imbalance": lambda p: (-p.imbalance, -p.duels)}[sort]
    pairs.sort(key=lambda p: (*order(p), p.a.steamid, p.b.steamid))

    people = []
    for sid in sorted([player] if player is not None else d.players, key=lambda x: (names.get(x, x).lower(), x)):
        nemesis, victim = _nemesis_and_victim(d, sid, floor, names)
        people.append(RivalryPlayer(player=_ref(sid, names),
                                    kills=sum(n for (a, _v), n in d.kills.items() if a == sid),
                                    deaths=sum(n for (_a, v), n in d.kills.items() if v == sid),
                                    nemesis=nemesis, victim=victim))
    return RivalriesOut(scope=_scope_out(entries, scope), min_duels=floor, total_pairs=len(pairs),
                        pairs=pairs[:limit] if limit else pairs, players=people)


# ---------------------------------------------------------------------------------------------- trophies

@dataclass
class _Cand:
    steamid: str
    value: float
    order: tuple  # sort key: the winner has the smallest
    detail: str = ""
    detail_en: str = ""
    match_id: str | None = None
    round: int | None = None


@dataclass
class _Run:
    length: int = 0
    first_round: int = 0
    last_round: int = 0
    index: int = 0  # position of the match in the scope
    match_id: str = ""
    map: str = ""


@dataclass
class _Moments:
    streak: dict[str, _Run] = field(default_factory=dict)  # best run of kills without dying
    drought: dict[str, _Run] = field(default_factory=dict)  # longest run of rounds without a kill
    low_hp: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))  # attacker health per kill
    knife_out: Counter = field(default_factory=Counter)
    baits: Counter = field(default_factory=Counter)
    tk_victims: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    whiffs: Counter = field(default_factory=Counter)
    clutch_enemies: Counter = field(default_factory=Counter)  # enemies alive at the start of the clutches won
    humiliation_kills: Counter = field(default_factory=Counter)
    humiliation_deaths: Counter = field(default_factory=Counter)


def _moments(entries: list[Entry]) -> _Moments:
    """What the per-match player stats do not carry: streaks, droughts and the kills that make a trophy."""
    m = _Moments()

    def keep(best: dict[str, _Run], sid: str, run: _Run) -> None:
        if sid not in best or run.length > best[sid].length:  # the earliest of equal runs stays
            best[sid] = replace(run)

    for index, e in enumerate(sorted(entries, key=lambda x: (x.played_at, x.match_id))):
        a = e.analysis
        humans = [p.steamid for p in a.players if not is_bot(p.steamid)]
        rounds = sorted(a.rounds, key=lambda r: r.number)
        streak: dict[str, _Run] = {}
        round_kills: Counter = Counter()
        for r in rounds:
            for k in sorted(r.kills, key=lambda x: x.tick):
                att, vic = (k.attacker.steamid if k.attacker else None), k.victim.steamid
                enemy = att is not None and not k.is_suicide and not k.is_teamkill and att != vic
                if enemy:
                    round_kills[(r.number, att)] += 1
                    run = streak.setdefault(att, _Run(index=index, match_id=e.match_id, map=a.map))  # type: ignore[arg-type]
                    if run.length == 0:
                        run.first_round = r.number
                    run.length, run.last_round = run.length + 1, r.number
                    keep(m.streak, att, run)  # type: ignore[arg-type]
                    if k.attacker_health is not None and 0 < k.attacker_health <= LOW_HP_MAX:
                        m.low_hp[att].append(k.attacker_health)  # type: ignore[index]
                    if is_humiliation(k):
                        m.humiliation_kills[att] += 1
                        m.humiliation_deaths[vic] += 1
                    # Same rule as the fail (analysis/fails.py): everybody had guns, he had the knife out.
                    if (not r.is_pistol and not k.after_round_end and weapons.weapon_class(k.weapon) != "knife"
                            and k.victim_weapon and weapons.weapon_class(k.victim_weapon) == "knife"):
                        m.knife_out[vic] += 1
                elif att is not None and k.is_teamkill and not k.is_suicide and att != vic:
                    m.tk_victims[att].append(k.victim.name)
                if vic in streak:
                    streak[vic].length = 0
            for c in r.clutches:
                if c.won:
                    m.clutch_enemies[c.player.steamid] += c.versus
                elif round_kills[(r.number, c.player.steamid)] == 0:
                    m.baits[c.player.steamid] += 1
        for sid in humans:
            run = _Run(index=index, match_id=e.match_id, map=a.map)
            for r in rounds:
                if sid not in r.ct_players and sid not in r.t_players:
                    continue  # a round he was not in neither counts nor ends the run
                if round_kills[(r.number, sid)]:
                    run.length = 0
                    continue
                if run.length == 0:
                    run.first_round = r.number
                run.length, run.last_round = run.length + 1, r.number
                keep(m.drought, sid, run)
        for f in a.fails:
            if f.kind == "fail_whiff":
                m.whiffs[f.player.steamid] += 1
    return m


def _span(run: _Run) -> tuple[str, str]:
    if run.first_round == run.last_round:
        return f"round {run.first_round}", f"round {run.first_round}"
    return f"rounds {run.first_round}–{run.last_round}", f"rounds {run.first_round}–{run.last_round}"


def _money(v: float, pt: bool) -> str:
    text = f"${v:,.0f}"
    return text.replace(",", ".") if pt else text


def _pct(v: float, pt: bool) -> str:
    return f"{v:.1f}".replace(".", ",") + "%" if pt else f"{v:.1f}%"


def _trophy(key: str, cands: list[_Cand], names: dict[str, str]) -> TrophyOut | None:
    ranked = sorted(cands, key=lambda c: (c.order, c.steamid))  # the SteamID settles what is still tied
    if not ranked:
        return None
    top = ranked[0]
    title_pt, title_en, about_pt, about_en = TROPHIES[key]
    return TrophyOut(key=key, title_pt=title_pt, title_en=title_en, description_pt=about_pt, description_en=about_en,
                     player=_ref(top.steamid, names), value=top.value, detail=top.detail, detail_en=top.detail_en,
                     match_id=top.match_id, round=top.round,
                     runners_up=[TrophyRunnerUp(player=_ref(c.steamid, names), value=c.value) for c in ranked[1:3]])


def trophies_of(entries: list[Entry], scope: Scope | None = None) -> TrophiesOut:
    """The trophies of one match or one night (``entries`` is taken as a whole).

    Fourteen trophies, each with a minimum under which nobody gets it (constants at the top of this module;
    definitions in docs/metrics.md). A player can win several. Ties go to the secondary number named in the
    docs and, if still tied, to the lower SteamID - the same input always gives the same winners. Bots never win.
    """
    names = _names(entries)
    totals = _totals(entries)
    m = _moments(entries)
    cands: dict[str, list[_Cand]] = {k: [] for k in TROPHIES}
    most_rounds = max((t.rounds for t in totals.values()), default=0)

    for sid, t in totals.items():
        if t.rounds and t.rounds >= MVP_MIN_ROUNDS_SHARE * most_rounds:
            line = f"{t.kills}-{t.deaths}-{t.assists}, ADR {t.adr:.0f}"
            cands["mvp"].append(_Cand(sid, t.rating, (-t.rating, -t.kills), line, line))
        if t.clutches_won and m.clutch_enemies[sid] >= CLUTCH_MIN_ENEMIES:
            sizes = ", ".join(f"{n}× {k}" if n > 1 else k for k, n in sorted(t.clutches_by_size.items(), reverse=True))
            cands["clutch_king"].append(_Cand(
                sid, t.clutches_won, (-t.clutches_won, -m.clutch_enemies[sid], t.clutches_attempted),
                f"{sizes} · venceu {t.clutches_won} de {t.clutches_attempted}",
                f"{sizes} · won {t.clutches_won} of {t.clutches_attempted}"))
        if t.opening_kills >= ENTRY_MIN_OPENINGS:
            cands["entry"].append(_Cand(
                sid, t.opening_kills, (-t.opening_kills, t.opening_deaths),
                f"abriu {t.opening_kills} rounds matando, morreu primeiro em {t.opening_deaths}",
                f"{t.opening_kills} opening kills, {t.opening_deaths} opening deaths"))
        if t.teammates_flashed >= TEAM_FLASH_MIN:
            cands["team_flasher"].append(_Cand(
                sid, t.teammates_flashed, (-t.teammates_flashed, t.flashes_thrown),
                f"cegou companheiros {t.teammates_flashed} vezes com {t.flashes_thrown} flashes",
                f"blinded team-mates {t.teammates_flashed} times with {t.flashes_thrown} flashbangs"))
        if t.shots_fired >= SHARP_AIM_MIN_SHOTS:
            acc = round(100.0 * t.shots_hit / t.shots_fired, 1)
            cands["sharp_aim"].append(_Cand(
                sid, acc, (-acc, -t.shots_fired),
                f"{_pct(acc, True)} dos tiros no alvo ({t.shots_hit} de {t.shots_fired})",
                f"{_pct(acc, False)} of bullets on target ({t.shots_hit} of {t.shots_fired})"))
        if t.money_spent > 0 and t.money_kills >= ECONOMIST_MIN_KILLS:
            cost = round(t.money_spent / t.money_kills)
            cands["economist"].append(_Cand(
                sid, cost, (cost, -t.money_kills),
                f"{_money(cost, True)} por kill ({_money(t.money_spent, True)} em {t.money_kills} kills)",
                f"{_money(cost, False)} per kill ({_money(t.money_spent, False)} for {t.money_kills} kills)"))
        if t.kills >= HEADSHOT_MIN_KILLS:
            cands["headshot_machine"].append(_Cand(
                sid, t.headshot_pct, (-t.headshot_pct, -t.headshot_kills),
                f"{t.headshot_kills} de {t.kills} kills na cabeça", f"{t.headshot_kills} of {t.kills} kills were headshots"))

    for sid, run in m.streak.items():
        if not is_bot(sid) and run.length >= STREAK_MIN_KILLS:
            pt, en = _span(run)
            cands["kill_streak"].append(_Cand(
                sid, run.length, (-run.length, run.index, run.first_round),
                f"{run.length} kills seguidas sem morrer ({run.map}, {pt})",
                f"{run.length} kills in a row without dying ({run.map}, {en})", run.match_id, run.first_round))
    for sid, run in m.drought.items():
        if run.length >= DROUGHT_MIN_ROUNDS:
            pt, en = _span(run)
            cands["drought"].append(_Cand(
                sid, run.length, (-run.length, run.index, run.first_round),
                f"{run.length} rounds seguidos sem matar ninguém ({run.map}, {pt})",
                f"{run.length} rounds in a row without a kill ({run.map}, {en})", run.match_id, run.first_round))
    for sid, hps in m.low_hp.items():
        if not is_bot(sid) and len(hps) >= LOW_HP_MIN_KILLS:
            cands["low_hp"].append(_Cand(
                sid, len(hps), (-len(hps), min(hps)),
                f"{len(hps)} kills com {LOW_HP_MAX} de vida ou menos (a mais apertada com {min(hps)})",
                f"{len(hps)} kills on {LOW_HP_MAX} HP or less (the closest on {min(hps)})"))
    for sid, victims in m.tk_victims.items():
        if not is_bot(sid) and victims:
            who = ", ".join(f"{name} ×{n}" if n > 1 else name for name, n in sorted(Counter(victims).items()))
            cands["friendly_fire"].append(_Cand(sid, len(victims), (-len(victims),), f"matou {who}", f"killed {who}"))
    for key, counter, floor, pt, en in (
            ("whiff", m.whiffs, WHIFF_MIN, "{n} pentes descarregados de perto sem acertar ninguém",
             "{n} magazines emptied at close range without a hit"),
            ("knife_out", m.knife_out, KNIFE_OUT_MIN, "morreu {n} vezes com a faca na mão",
             "died {n} times holding the knife"),
            ("baiter", m.baits, BAITER_MIN_ROUNDS, "sobrou por último em {n} rounds perdidos sem matar ninguém",
             "last one alive in {n} lost rounds without a single kill")):
        for sid, n in counter.items():
            if not is_bot(sid) and n >= floor:
                rounds = totals[sid].rounds if sid in totals else 0
                cands[key].append(_Cand(sid, n, (-n, rounds), pt.format(n=n), en.format(n=n)))

    found = [t for t in (_trophy(key, cands[key], names) for key in TROPHIES) if t is not None]
    if scope is not None and (scope.match_id or scope.night):
        kind = "match" if scope.match_id else "night"
    else:
        kind = "match" if len(entries) == 1 else "night"
    return TrophiesOut(scope=_scope_out(entries, scope), kind=kind, trophies=found)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------- chat

def chat_rows(e: Entry) -> list[dict[str, Any]]:
    """The match's chat with ``round``, ``kind``, ``tags`` and ``relation`` on every line. Analyses stored
    before the chat model have none of it: those are annotated here, on the fly (it is cheap)."""
    rows = e.analysis.chat
    if any("kind" not in m or "tags" not in m or "relation" not in m for m in rows):
        return chat_model.annotate(rows, e.analysis.rounds, e.analysis.tickrate)
    return rows


def _chat_keys(row: dict[str, Any]) -> list[str]:
    """The podiums a line counts for. A line counts for every category whose words it has."""
    tags = set(row.get("tags") or [])
    keys = [k for k in ("whine", "toxic", "laugh", "shouting") if k in tags]
    if tags & {"praise", "gg"}:
        keys.append("nice")
    if row.get("relation") == "victim" and row.get("kind") in ("whine", "toxic"):
        keys.append("death_cry")
    return keys


def _rate(count: int, rounds: int) -> float:
    return round(count / rounds, 3) if rounds else 0.0


def _examples(key: str, lines: list[tuple[Entry, dict[str, Any]]], names: dict[str, str]) -> list[ChatExample]:
    """One or two lines to quote: the longer ones first, then the oldest. For crying and swearing, the lines
    written right after the author died come before the others."""
    after_death = key in ("whine", "toxic", "death_cry")
    ranked = sorted(lines, key=lambda x: (after_death and x[1].get("relation") != "victim",
                                          -min(len(str(x[1].get("text") or "")), 80),
                                          x[0].played_at, x[0].match_id, int(x[1].get("tick") or 0)))
    out: list[ChatExample] = []
    seen: set[str] = set()
    for e, row in ranked:
        text = str(row.get("text") or "").strip()
        plain = chat_model.normalize(text)
        if not text or plain in seen or not chat_model.shareable(text, row.get("kind")):
            continue  # never quote long free text, numbers or links: chat can hold addresses and phones
        seen.add(plain)
        died = row.get("relation") == "victim" and row.get("kill_attacker")
        out.append(ChatExample(text=chat_model.display_text(text), match_id=e.match_id, round=row.get("round"),
                               kind=row.get("kind"), relation=row.get("relation"),
                               about=names.get(row["kill_attacker"]) if died else None))
        if len(out) == PODIUM_EXAMPLES:
            break
    return out


def chat_rankings_of(entries: list[Entry], scope: Scope | None = None) -> ChatRankingsOut:
    """Who types what in all-chat: counts and per-round rates per player, and a podium per category.

    A banter ranking between friends built on a word list (``analysis/chat.py``) - **not moderation**. It
    misses sarcasm and anything it has no word for, and a word can land in the wrong category. Categories
    (a line counts for every one whose words it has): ``whine`` ("chorão": cheating accusations, luck,
    complaining), ``toxic`` ("boca suja": swearing), ``laugh`` ("risadinha"), ``nice`` ("gente boa": praise
    or gg), ``shouting`` (all caps) and, separately, ``death_cry``: whining or swearing within 20 s of the
    author's own death - the heart of the cry-baby ranking. Server commands (``!``, ``.``, ``/``) are not
    messages. Podiums, in the order ``toxic``, ``whine``, ``death_cry``, ``talker``, ``laugh``, ``nice``,
    ``shouting``: the three players with the most lines (at least 2; 1 when the scope is one match), ties by
    rate per round played then SteamID, each with one or two example lines shown through
    ``chat.display_text`` (slurs masked). ``talker`` ("tagarela") ranks by ``messages``, every line a player
    typed whatever it says, and quotes nothing. Only all-chat exists in the demos.
    """
    names = _names(entries)
    totals = _totals(entries)
    counts: dict[str, Counter] = defaultdict(Counter)
    lines: dict[tuple[str, str], list[tuple[Entry, dict[str, Any]]]] = defaultdict(list)
    cried: dict[str, Counter] = defaultdict(Counter)
    for e in entries:
        for row in chat_rows(e):
            sid = row.get("steamid")
            if is_bot(sid):
                continue
            names.setdefault(sid, str(row.get("name") or sid))
            if row.get("kind") == "command":
                counts[sid]["commands"] += 1
                continue
            counts[sid]["messages"] += 1
            for key in _chat_keys(row):
                counts[sid][_CHAT_FIELD[key]] += 1
                lines[(key, sid)].append((e, row))
                if key == "death_cry" and row.get("kill_attacker") and not is_bot(row["kill_attacker"]):
                    cried[sid][row["kill_attacker"]] += 1

    players: list[ChatPlayerRow] = []
    for sid, c in counts.items():
        t = totals.get(sid)
        rounds = t.rounds if t else 0
        about = min(cried[sid].items(), key=lambda x: (-x[1], x[0]), default=None)
        players.append(ChatPlayerRow(
            player=_ref(sid, names), matches=t.matches if t else 0, rounds=rounds, messages=c["messages"],
            commands=c["commands"], cried_about=_ref(about[0], names) if about else None,
            cried_about_count=about[1] if about else 0, messages_per_round=_rate(c["messages"], rounds),
            **{f: c[f] for f in _CHAT_FIELD.values()},
            **{f"{f}_per_round": _rate(c[f], rounds) for f in _CHAT_FIELD.values()}))
    players.sort(key=lambda p: (-p.messages, -p.commands, p.player.steamid))

    floor = 1 if len(entries) <= 1 else 2
    podiums = []
    for key, (title_pt, title_en) in CHAT_PODIUMS.items():
        f = _PODIUM_FIELD[key]
        ranked = sorted((p for p in players if getattr(p, f) >= floor),
                        key=lambda p: (-getattr(p, f), -getattr(p, f"{f}_per_round"), p.player.steamid))
        podiums.append(ChatPodium(key=key, title_pt=title_pt, title_en=title_en, entries=[
            ChatPodiumEntry(player=p.player, count=getattr(p, f), per_round=getattr(p, f"{f}_per_round"),
                            examples=_examples(key, lines[(key, p.player.steamid)], names))
            for p in ranked[:PODIUM_SIZE]]))
    return ChatRankingsOut(scope=_scope_out(entries, scope), messages=sum(p.messages for p in players),
                           note=CHAT_NOTE, podiums=podiums, players=players)


# ---------------------------------------------------------------------------------------------- profile

def _play(e: Entry, h: HighlightCandidate) -> ProfilePlay:
    return ProfilePlay(match_id=e.match_id, kind=h.kind, title=h.title, round=h.round, score=h.score, tier=h.tier,
                       featured=h.featured)


def _weapon_stats(entries: list[Entry], steamid: str) -> list[ProfileWeapon]:
    """His kills by weapon (the same kills as the scoreboard: no suicides, no team kills), with headshots."""
    kills: Counter = Counter()
    heads: Counter = Counter()
    for e in entries:
        for r in e.analysis.rounds:
            for k in r.kills:
                if k.attacker is None or k.attacker.steamid != steamid or k.is_suicide or k.is_teamkill:
                    continue
                w = weapons.display_name(k.weapon)
                kills[w] += 1
                heads[w] += k.headshot
    total = sum(kills.values())
    return [ProfileWeapon(weapon=w, kills=n, headshots=heads[w], headshot_pct=round(100.0 * heads[w] / n, 1),
                          share_pct=round(100.0 * n / total, 1))
            for w, n in sorted(kills.items(), key=lambda x: (-x[1], x[0]))]


def profile_of(entries: list[Entry], steamid: str, scope: Scope | None = None) -> PlayerProfileOut:
    """One player over the matches of the scope he played in: totals (ADR over all rounds; KAST and rating
    averaged by rounds played), best plays and fails, nemesis and favourite victim, chat numbers, history."""
    mine = [e for e in entries if any(p.steamid == steamid for p in e.analysis.players)]
    if not mine or is_bot(steamid):
        raise service.NotFound(f"No player with SteamID {steamid!r} in the {len(entries)} matches of this scope. "
                               f"Players: {_known_players(entries)}")
    names = _names(entries)
    t = _totals(mine)[steamid]
    m = _moments(mine)
    nemesis, victim = _nemesis_and_victim(_count_duels(mine), steamid, default_min_duels(len(mine)), names)
    chat = next((p for p in chat_rankings_of(mine).players if p.player.steamid == steamid), None)

    plays = sorted(((e, h) for e in mine for h in e.analysis.highlights if h.player.steamid == steamid),
                   key=lambda x: (-x[1].score, x[0].played_at, x[1].round))
    fails = sorted(((e, h) for e in mine for h in e.analysis.fails if h.player.steamid == steamid),
                   key=lambda x: (-x[1].score, x[0].played_at, x[1].round))
    history = []
    for e in sorted(mine, key=lambda x: (x.played_at, x.match_id), reverse=True):
        a = e.analysis
        p = next(p for p in a.players if p.steamid == steamid)
        own, other = (a.team_a, a.team_b) if p.team == "team_a" else (a.team_b, a.team_a)
        history.append(ProfileMatch(match_id=e.match_id, map=a.map, played_at=e.played_at, night=e.night.isoformat(),
                                    won=own.won, score=f"{own.score}-{other.score}", kills=p.kills, deaths=p.deaths,
                                    assists=p.assists, adr=p.adr, rating=p.rating))
    return PlayerProfileOut(
        player=_ref(steamid, names), names=_aliases(mine).get(steamid, []), scope=_scope_out(mine, scope),
        matches=t.matches, wins=t.wins, losses=t.losses, draws=t.draws, rounds=t.rounds, kills=t.kills,
        deaths=t.deaths, assists=t.assists, kd_ratio=round(t.kills / t.deaths, 2) if t.deaths else float(t.kills),
        kd_diff=t.kills - t.deaths, headshot_kills=t.headshot_kills, headshot_pct=t.headshot_pct, damage=t.damage,
        adr=t.adr, kast=t.kast, rating=t.rating, opening_kills=t.opening_kills, opening_deaths=t.opening_deaths,
        trade_kills=t.trade_kills, multi_kills={k: t.multi[k] for k in ("2k", "3k", "4k", "5k")},
        clutches_won=t.clutches_won, clutches_attempted=t.clutches_attempted,
        clutches_by_size=dict(sorted(t.clutches_by_size.items())), team_kills=t.team_kills,
        teammates_flashed=t.teammates_flashed, enemies_flashed=t.enemies_flashed, flash_assists=t.flash_assists,
        humiliation_kills=m.humiliation_kills[steamid], humiliation_deaths=m.humiliation_deaths[steamid],
        weapons=dict(t.weapons.most_common(5)), weapon_stats=_weapon_stats(mine, steamid), best_plays=[_play(e, h) for e, h in plays[:5]],
        fails=len(fails), fails_by_kind=dict(sorted(Counter(h.kind for _e, h in fails).items())),
        worst_fails=[_play(e, h) for e, h in fails[:3]], nemesis=nemesis, victim=victim, chat=chat, history=history)


# ---------------------------------------------------------------------------------------------- with a session

def nights(s: Session, scope: Scope | None = None) -> list[NightOut]:
    """The nights that have analyzed matches, newest first: date, match ids, players, rounds."""
    return nights_of(load_entries(s, scope))


def players(s: Session, scope: Scope | None = None) -> list[PlayerSummary]:
    return players_of(load_entries(s, scope))


def resolve_player(s: Session, player: str, scope: Scope | None = None) -> str:
    return find_player(load_entries(s, scope), player)


def rivalries(s: Session, scope: Scope | None = None, player: str | None = None, min_duels: int | None = None,
              *, sort: str = "duels", limit: int | None = None) -> RivalriesOut:
    return rivalries_of(load_entries(s, scope), player=player, min_duels=min_duels, sort=sort, limit=limit,
                        scope=scope)


def _stored_plays(s: Session, match_ids: list[str], steamid: str) -> tuple[list[ProfilePlay], list[ProfilePlay]]:
    """The player's plays and fails as stored (they have ids and survive re-scoring and manual edits)."""
    rows = s.scalars(select(Highlight).where(Highlight.match_id.in_(match_ids), Highlight.player_steamid == steamid)
                     .order_by(Highlight.score.desc(), Highlight.created_at, Highlight.id)).all()
    out: tuple[list[ProfilePlay], list[ProfilePlay]] = ([], [])
    for h in rows:
        out[1 if h.category == "fail" else 0].append(ProfilePlay(
            highlight_id=h.id, match_id=h.match_id, kind=h.kind, title=h.title, round=h.round, score=h.score,
            tier=h.tier, featured=bool(h.featured)))
    return out


def player_profile(s: Session, steamid: str, scope: Scope | None = None) -> PlayerProfileOut:
    """``profile_of`` with the best plays and worst fails taken from the stored highlights, so they carry ids."""
    out = profile_of(load_entries(s, scope), steamid, scope)
    plays, fails = _stored_plays(s, out.scope.matches, steamid)
    if plays:
        out.best_plays = plays[:5]
    if fails:
        out.worst_fails = fails[:3]
    return out


def trophies(s: Session, scope: Scope | None = None) -> TrophiesOut:
    """Trophies of one match (``scope.match_id``) or one night (``scope.night``). With neither, the most
    recent night that has matches (inside ``since`` / ``until`` when given)."""
    scope = scope or Scope()
    if scope.match_id is None and scope.night is None:
        tz = local_zone()
        found = [night_of(when, tz) for _m, when in _ready_matches(s) if _in_window(when, scope)]
        if not found:
            raise service.NotFound("There are no analyzed matches to give trophies for. Trophies are for one "
                                   "match (match_id) or one night (night=YYYY-MM-DD).")
        scope = replace(scope, night=max(found))
    return trophies_of(load_entries(s, scope), scope)


def chat_rankings(s: Session, scope: Scope | None = None) -> ChatRankingsOut:
    return chat_rankings_of(load_entries(s, scope), scope)
