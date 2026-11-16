// The vocabulary of a play: its notation ("1v3", "4K"), a sentence in the reader's language, its tape
// (segments laid end to end with a mark at every kill) and which of its videos to show.

import { t, has, joinMeta, list, mapName } from './i18n.js';

const KIND_KILLS = { ace: 5, quad_kill: 4, triple_kill: 3, double_kill: 2 };

export function clutchSize(h) {
  for (const tag of h.tags || []) {
    const m = /^clutch_1v(\d)$/.exec(tag);
    if (m) return Number(m[1]);
  }
  return null;
}

export function killCount(h) {
  if (Array.isArray(h.kill_ticks) && h.kill_ticks.length) return h.kill_ticks.length;
  if (KIND_KILLS[h.kind]) return KIND_KILLS[h.kind];
  const m = /\b(\d)K\b/.exec(h.title || '');
  if (m) return Number(m[1]);
  return h.kind === 'ace' ? 5 : null;
}

// ------------------------------------------------------------------------------------------- fails ("zoeira")

/** A fail is banter, not a play: it has its own page language and never enters the ranking of plays. */
export const isFail = (h) => Boolean(h) && (h.category === 'fail' || String(h.kind || '').startsWith('fail_'));

export const FAIL_KINDS = ['fail_whiff', 'fail_team_flash', 'fail_self_flash', 'fail_teamkill', 'fail_own_grenade', 'fail_fall', 'fail_bomb', 'fail_knife_out'];

/**
 * What can be recovered about a fail. The API describes fails in English prose only (title, description), so
 * the numbers and the other names are read back from those sentences; whatever does not parse stays empty
 * and the sentence falls back to a generic one for the kind.
 * `{shots, weapon, target, meters, mate, killer, seconds}`
 */
export function failFacts(h) {
  // The API says what happened as data (`facts`); the English prose is only parsed for plays analysed before
  // that field existed.
  const d = h.facts || {};
  if (Object.keys(d).length) {
    const name = (p) => (p && p.name) || undefined;
    const out = {};
    if (d.shots != null) out.shots = Number(d.shots);
    if (d.weapon_name || d.weapon) out.weapon = d.weapon_name || d.weapon;
    if (d.distance_m != null) out.meters = Number(d.distance_m);
    if (d.blind_s != null) out.seconds = Number(d.blind_s);
    if (name(d.killer)) out.killer = name(d.killer);
    if (h.kind === 'fail_whiff' && name(d.killer)) out.target = name(d.killer);
    if (h.kind === 'fail_team_flash' && name(d.blinded)) out.mate = name(d.blinded);
    if (h.kind === 'fail_teamkill' && name(d.victim)) out.mate = name(d.victim);
    return out;
  }
  const title = String(h.title || '');
  const text = String(h.description || '');
  const out = {};
  let m;
  if (h.kind === 'fail_whiff') {
    if ((m = /(\d+) shots?, zero hits/.exec(title))) out.shots = Number(m[1]);
    if ((m = /fired (\d+) (.+?) rounds? at (.+) from (\d+(?:\.\d+)?) m without/.exec(text))) {
      out.shots = out.shots || Number(m[1]);
      [, , out.weapon, out.target] = m;
      out.meters = Number(m[4]);
    }
  } else if (h.kind === 'fail_team_flash') {
    if ((m = /flashed (.+), who died blind$/.exec(title))) out.mate = m[1];
    if ((m = / for (\d+(?:\.\d+)?) s; (.+) killed him /.exec(text))) {
      out.seconds = Number(m[1]);
      out.killer = m[2];
    }
  } else if (h.kind === 'fail_self_flash') {
    if ((m = /own flash for (\d+(?:\.\d+)?) s/.exec(text))) out.seconds = Number(m[1]);
  } else if (h.kind === 'fail_teamkill') {
    if ((m = /team kill on (.+)$/.exec(title))) out.mate = m[1];
    if ((m = / with (.+)\.$/.exec(text))) out.weapon = m[1];
  } else if (h.kind === 'fail_own_grenade') {
    if ((m = /died to his own (.+)\.$/.exec(text))) out.weapon = m[1];
  } else if (h.kind === 'fail_knife_out') {
    if ((m = /was killed by (.+) with the knife in/.exec(text))) out.killer = m[1];
  }
  return out;
}

/** The notation of a fail: "0/14" (hits out of shots), "TK", "FLASH", "FACA"... */
export function failToken(h) {
  if (h.kind === 'fail_whiff') {
    const { shots } = failFacts(h);
    return shots ? `0/${shots}` : t('fail.token.fail_whiff');
  }
  return has(`fail.token.${h.kind}`) ? t(`fail.token.${h.kind}`) : t('fail.token.other');
}

/** One sentence in the reader's language: "14 tiros de AK-47 em Pipocaa, nenhum acerto". */
export function failLine(h) {
  const f = failFacts(h);
  switch (h.kind) {
    case 'fail_whiff':
      if (f.shots && f.weapon && f.target) return t('fail.whiffFull', { n: f.shots, weapon: f.weapon, target: f.target });
      return f.shots ? t('fail.whiffShots', { n: f.shots }) : t('fail.whiff');
    case 'fail_team_flash':
      if (f.mate && f.killer) return t('fail.teamFlashFull', { mate: f.mate, killer: f.killer });
      return f.mate ? t('fail.teamFlashMate', { mate: f.mate }) : t('fail.teamFlash');
    case 'fail_self_flash':
      return t('fail.selfFlash');
    case 'fail_teamkill':
      if (f.mate && f.weapon) return t('fail.teamkillFull', { mate: f.mate, weapon: f.weapon });
      return f.mate ? t('fail.teamkillMate', { mate: f.mate }) : t('fail.teamkill');
    case 'fail_own_grenade':
      return f.weapon ? t('fail.ownGrenadeWith', { weapon: f.weapon }) : t('fail.ownGrenade');
    case 'fail_fall':
      return t('fail.fall');
    case 'fail_bomb':
      return t('fail.bomb');
    case 'fail_knife_out':
      return f.killer ? t('fail.knifeOutBy', { killer: f.killer }) : t('fail.knifeOut');
    default:
      return h.title || t('fail.other');
  }
}

/** The name of a kind of fail, for filters and counts ("Tiro no nada", "Flash no amigo"). */
export const failKindLabel = (kind) => (has(`fail.kind.${kind}`) ? t(`fail.kind.${kind}`) : String(kind).replace(/^fail_/, '').replace(/_/g, ' '));

/** The big notation: "1v3", "4K", "ACE", "R2" (a hand-made cut of round 2); for a fail, its own ("0/14"). */
export function playToken(h) {
  if (isFail(h)) return failToken(h);
  const versus = clutchSize(h);
  if (h.kind === 'ace') return 'ACE';
  if (h.kind === 'clutch' && versus) return `1v${versus}`;
  if (h.kind === 'quad_kill') return '4K';
  if (h.kind === 'triple_kill') return '3K';
  if (h.kind === 'double_kill') return '2K';
  if (versus) return `1v${versus}`;
  if (h.kind === 'ninja_defuse') return 'Ninja';
  const n = killCount(h);
  if (h.kind === 'special_kill' && n) return `${n}K`;
  if (h.round) return `R${h.round}`;
  return n ? `${n}K` : '';
}

/** Titles written by the analyzer look like "<player> — 4K (AK-47)". Anything else was typed by a person. */
export function hasAutoTitle(h) {
  const name = h.player && h.player.name;
  return Boolean(name && typeof h.title === 'string' && h.title.startsWith(`${name} — `));
}

/** Weapon names from an analyzer title: "(Desert Eagle + AK-47)" -> ["Desert Eagle", "AK-47"]. */
export function titleWeapons(h) {
  if (!hasAutoTitle(h)) return [];
  const m = /\(([^()]+)\)\s*$/.exec(h.title);
  return m ? m[1].split(' + ').map((s) => s.trim()).filter(Boolean) : [];
}

/** One sentence: "Clutch 1v3 com 4 kills de Desert Eagle e AK-47". `weapons` overrides the title parse. */
export function playLine(h, weapons) {
  if (isFail(h)) return failLine(h);
  if (!hasAutoTitle(h) && h.title) return h.title;
  const versus = clutchSize(h);
  const n = killCount(h);
  const guns = weapons && weapons.length ? weapons : titleWeapons(h);
  const withGuns = guns.length ? t('play.withWeapons', { weapons: list(guns) }) : '';
  let head;
  if (h.kind === 'ace') head = t('play.ace');
  else if (h.kind === 'ninja_defuse') head = t('play.ninja');
  else if (h.kind === 'clutch' && versus) head = n ? t('play.clutchKills', { versus, n }) : t('play.clutch', { versus });
  else if (versus && n) head = t('play.killsClutch', { n, versus });
  else if (n) head = t('play.kills', { n });
  else head = t('play.cut');
  return `${head}${withGuns}`;
}

/** "Anubis, round 8" */
export function playWhere(h, m) {
  const map = m && m.map ? mapName(m.map) : null;
  const round = h.round ? t('play.round', { round: h.round }) : null;
  return joinMeta(map, map && round ? round.toLowerCase() : round);
}

/** Tags in the words players use. Clutch size is left out: it is already the notation. */
export function tagWords(h) {
  const out = [];
  for (const tag of h.tags || []) {
    if (/^clutch_1v\d$/.test(tag) || tag === 'headshot') continue; // the notation and the kill feed already say it
    out.push(has(`tag.${tag}`) ? t(`tag.${tag}`) : tag.replace(/_/g, ' '));
  }
  return out;
}

/** "16:9" | "9:16" | "1:1". Servers that do not send `format` yet are read from the frame size. */
export function renderFormat(r) {
  if (r && typeof r.format === 'string' && /^\d+:\d+$/.test(r.format)) return r.format;
  const w = Number(r && r.width);
  const h = Number(r && r.height);
  if (!w || !h) return '16:9';
  const ratio = w / h;
  if (ratio > 1.2) return '16:9';
  return ratio < 0.8 ? '9:16' : '1:1';
}

export const isVertical = (r) => renderFormat(r) === '9:16';
const kindOf = (r) => (r.renderer === 'ingame' ? 'ingame' : 'radar');

/** A play has one video per variant: "ingame-16x9" (POV), "ingame-9x16" (POV vertical), "radar-16x9" ... */
export const variantKey = (r) => `${kindOf(r)}-${renderFormat(r).replace(':', 'x')}`;
export const variantParts = (key) => {
  const [renderer, shape = '16x9'] = String(key).split('-');
  return { renderer: renderer === 'radar' ? 'radar' : 'ingame', format: shape.replace('x', ':') };
};
export const variantLabel = (key) => (has(`variant.${key}`) ? t(`variant.${key}`) : key);

/** Ready videos, best first: fresh before out-of-date, POV before 2D, 16:9 before the others, newest first. */
export function sortRenders(renders) {
  return [...(renders || [])]
    .filter((r) => r.status === 'ready' && r.video_url)
    .sort((a, b) => (a.stale ? 1 : 0) - (b.stale ? 1 : 0)
      || (kindOf(a) === 'ingame' ? 0 : 1) - (kindOf(b) === 'ingame' ? 0 : 1)
      || (renderFormat(a) === '16:9' ? 0 : 1) - (renderFormat(b) === '16:9' ? 0 : 1)
      || String(b.created_at || '').localeCompare(String(a.created_at || '')));
}

/** Ready videos grouped by variant, each group best first: `Map(key -> [render, ...])`. */
export function variantsOf(renders) {
  const out = new Map();
  for (const r of sortRenders(renders)) {
    const key = variantKey(r);
    if (!out.has(key)) out.set(key, []);
    out.get(key).push(r);
  }
  return out;
}

/** A video of this play that is on its way: queued or being rendered right now. Null when there is none. */
export function pendingRender(renders) {
  const waiting = (renders || []).filter((r) => r.status === 'queued' || r.status === 'running');
  return waiting.find((r) => r.status === 'running') || waiting[0] || null;
}

/** The variant a queued / running render will become (it has no frame size yet). */
export function pendingKey(r) {
  const format = r.format || (r.options && r.options.format) || '16:9';
  return `${kindOf(r)}-${String(format).replace(':', 'x')}`;
}

/**
 * The video that stands for a play in posters, lists and the home stage: its POV 16:9, else its 2D 16:9.
 * Never a vertical one: a 9:16 frame cropped into a 16:9 poster shows a forehead and a floor.
 */
export const bestRender = (renders) => sortRenders(renders).find((r) => renderFormat(r) === '16:9') || null;

export const rendererLabel = (renderer) => (renderer === 'ingame' ? t('video.ingame') : t('video.radar'));

/** Badge text of a video: "POV", "Replay 2D", and the format when it is not the usual 16:9 ("POV 9:16"). */
export function videoLabel(r) {
  const format = renderFormat(r);
  const base = format === '16:9' ? rendererLabel(r.renderer) : (r.renderer === 'ingame' ? t('video.ingame') : t('video.radarShort'));
  return format === '16:9' ? base : `${base} ${format}`;
}

/**
 * The play tape. Segments end to end in video time, plus the offset of every kill:
 * `{duration, segments: [{index, start, end, seg}], kills: [{tick, time, segment}]}`.
 */
export function timeline(h, tickrate) {
  const rate = tickrate || 64;
  const out = { duration: 0, segments: [], kills: [] };
  let at = 0;
  (h.segments || []).forEach((seg, index) => {
    const speed = seg.speed || 1;
    const length = Math.max(0, (seg.end_tick - seg.start_tick) / rate / speed);
    const replay = seg.role === 'replay';
    out.segments.push({ index, start: at, end: at + length, seg, replay });
    // A replay shows a kill a second time, from the victim's eyes: it is drawn, but its kills are not counted again.
    for (const tick of replay ? [] : h.kill_ticks || []) {
      if (tick >= seg.start_tick && tick <= seg.end_tick && !out.kills.some((k) => k.tick === tick)) {
        out.kills.push({ tick, time: at + (tick - seg.start_tick) / rate / speed, segment: index });
      }
    }
    at += length;
  });
  out.kills.sort((a, b) => a.time - b.time);
  out.duration = at;
  return out;
}

/** Video time -> demo tick (for "split here"). Null when the time falls outside every segment. */
export function tickAt(tl, time, tickrate) {
  const rate = tickrate || 64;
  for (const s of tl.segments) {
    if (s.seg && time >= s.start && time <= s.end) {
      return Math.round(s.seg.start_tick + (time - s.start) * rate * (s.seg.speed || 1));
    }
  }
  return null;
}

// The brand intro and outro wrap the play inside the video. `render.body_offset_s` says where the play starts;
// videos made before that field existed fall back to the default bumper lengths (2.0 s in, 2.8 s out).
const INTRO_SHARE = 2.0 / 4.8;

/**
 * The tape as it sits inside one video, or null when it does not describe that video (the cut changed after
 * the render, or the lengths cannot be reconciled). Same shape as `timeline()`; `duration` is the video's.
 */
export function videoTimeline(h, render, tickrate) {
  const tl = timeline(h, tickrate);
  if (!render || render.stale || !tl.duration) return null;
  const total = Number.isFinite(render.duration_s) ? render.duration_s : tl.duration;
  // `kill_marks`: the exact second of every kill in this very video (bumper, speed ramps and freezes included).
  // With them there is nothing to estimate; the segments cannot be placed any more, so the tape is one strip.
  const marks = Array.isArray(render.kill_marks) ? render.kill_marks.filter((k) => k && Number.isFinite(k.t) && k.t >= 0 && k.t <= total + 0.5) : [];
  if (marks.length && total > 0) {
    return {
      duration: total,
      exact: true,
      segments: [{ index: 0, start: 0, end: total, seg: null, replay: false }],
      kills: marks.map((k) => ({ tick: k.tick, time: k.t, segment: 0 })).sort((a, b) => a.time - b.time),
    };
  }
  const extra = total - tl.duration;
  if (extra < -1.5 || extra > 14) return null;
  const known = Number.isFinite(render.body_offset_s);
  if (known ? render.body_offset_s === 0 && extra <= 1.5 : extra <= 1.5) return tl;
  const offset = known ? render.body_offset_s : extra * INTRO_SHARE;
  return {
    duration: total,
    offset,
    segments: tl.segments.map((seg) => ({ ...seg, start: seg.start + offset, end: seg.end + offset })),
    kills: tl.kills.map((k) => ({ ...k, time: k.time + offset })),
  };
}

// ------------------------------------------------------------------------------------------- curation

/** True when the scorer has graded this play. Matches analyzed before curation have no tier. */
export const isGraded = (h) => Boolean(h) && !isFail(h) && typeof h.tier === 'string' && Number.isFinite(h.score);
/** Featured plays ("destaques") get POV videos; the others stay as 2D replays. Null on old matches = no. */
export const isFeatured = (h) => Boolean(h) && h.featured === true;
/** The one play of its match: rank 1 among the featured. */
export const isBestOfMatch = (h) => isFeatured(h) && !isFail(h) && h.featured_rank === 1;

/** Featured plays in the order the match ranks them; hand-promoted ones (no rank) follow, best score first. */
export function byRank(a, b) {
  return (a.featured_rank ?? Infinity) - (b.featured_rank ?? Infinity) || (b.score || 0) - (a.score || 0);
}

/** "Jogada da partida" for rank 1, "Destaque" for the other featured plays, '' otherwise. */
export function curationWord(h) {
  if (isFail(h)) return ''; // banter is not ranked with the plays
  if (isBestOfMatch(h)) return t('curation.best');
  return isFeatured(h) ? t('curation.featured') : '';
}

// `breakdown.impact` is a share of `breakdown.impact_max` (the points for single-handedly taking a round from
// lost to won). Servers that do not send the maximum use 40.
const IMPACT_MAX = 40;
const STYLE_TAGS = ['collateral', 'noscope', 'through_smoke', 'wallbang', 'blind_kill', 'airborne', 'knife', 'zeus', 'he_kill', 'deagle_headshot', 'long_range'];

/**
 * Why a play got its score, as short sentences a player would say:
 * `[{dir: 'up' | 'down', text}]`. Empty when the play has no breakdown (older matches, hand-made cuts).
 * Reads `breakdown` (feat, impact, style, economy, stakes, outcome, penalty) and the tags.
 */
export function scoreReasons(h) {
  const b = h && h.breakdown && typeof h.breakdown === 'object' ? h.breakdown : {};
  if (!isGraded(h) || !Object.keys(b).length) return [];
  const tags = new Set(h.tags || []);
  const out = [];
  const up = (text) => out.push({ dir: 'up', text });
  const down = (text) => out.push({ dir: 'down', text });
  const num = (key, fallback) => (Number.isFinite(b[key]) ? b[key] : fallback);

  // what was done
  const versus = clutchSize(h);
  const n = killCount(h);
  if (h.kind === 'ace') up(t('why.ace'));
  else if (versus && n) up(t('why.clutchKills', { versus, n }));
  else if (versus) up(t('why.clutch', { versus }));
  else if (n) up(t('why.kills', { n }));

  // how much it changed the round
  const share = Math.max(0, Math.min(1, num('impact', 0) / (num('impact_max', IMPACT_MAX) || IMPACT_MAX)));
  const won = num('outcome', 1) >= 1;
  // `win_before`: the team's chance of taking the round before the first kill of the play.
  const before = Number.isFinite(b.win_before) ? b.win_before : 1 - share;
  if (share >= 0.6 && won && before <= 0.25) up(before < 0.005 ? t('why.decidedLost') : t('why.decided', { pct: Math.max(1, Math.round(before * 100)) }));
  else if (share >= 0.25) up(t('why.swing', { pct: Math.round(share * 100) }));
  else if (share < 0.12) down(t('why.lowImpact'));

  // how
  const styled = STYLE_TAGS.filter((tag) => tags.has(tag)).map((tag) => t(`tag.${tag}`));
  if (styled.length) up(t('why.style', { how: list(styled) }));
  else if (num('style', 0) >= 8) up(t('why.fast'));
  if (tags.has('opening_kill')) up(t('why.opening'));

  // against what
  const economy = num('economy', 1);
  if (economy >= 1.15) up(t('why.ecoFrag'));
  else if (economy > 1.01) up(t('why.worseBuy'));
  else if (economy <= 0.8) down(t('why.antiEco'));
  else if (economy < 0.99) down(t('why.betterBuy'));

  // when
  if (tags.has('overtime')) up(t('why.overtime'));
  if (tags.has('match_point')) up(t('why.matchPoint'));
  if (tags.has('close_game')) up(t('why.closeGame'));
  if (tags.has('pistol_round')) up(t('why.pistol'));
  if (tags.has('final_round')) up(t('why.finalRound'));
  if (num('stakes', 1) < 0.99) down(t('why.garbage'));

  // and how it ended
  if (!won) down(t('why.lost'));
  if (num('penalty', 0) >= 3) down(t('why.scattered'));
  // What raised the score first, what lowered it after: the list reads top to bottom as plus, then minus.
  return [...out.filter((r) => r.dir === 'up'), ...out.filter((r) => r.dir === 'down')];
}

export const hrefPlay = (h) => `#/play/${encodeURIComponent(h.id)}`;
export const hrefMatch = (id) => `#/match/${encodeURIComponent(id)}`;
export const hrefPlayer = (steamid) => `#/player/${encodeURIComponent(steamid)}`;
