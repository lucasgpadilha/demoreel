// Building blocks drawn from the game's own notation: the play tape, the round tape, kill-feed lines,
// posters, running-order rows and scorebug rows.

import { el, fill, icon, reducedMotion, safeUrl } from './dom.js';
import { t, clock, joinMeta, mapName, nightOf, timeOfDay, shortDate, whenText } from './i18n.js';
import {
  curationWord, hrefMatch, hrefPlay, hrefPlayer, isBestOfMatch, isGraded, pendingRender, playLine, playToken, playWhere, renderFormat,
  sortRenders,
  timeline, videoLabel,
} from './plays.js';
import { data } from './data.js';

// ------------------------------------------------------------------------------------------- play tape

/** The fingerprint of a play: segments end to end, one mark per kill. Not interactive. */
export function playTape(h, tickrate) {
  const tl = timeline(h, tickrate);
  const tape = el('div', { class: 'tape', 'aria-hidden': 'true' });
  if (!tl.duration) return tape;
  for (const s of tl.segments) {
    tape.appendChild(el('span', { class: `tape-seg${s.replay ? ' is-replay' : ''}`, style: { left: `${(s.start / tl.duration) * 100}%`, width: `${((s.end - s.start) / tl.duration) * 100}%` } }));
  }
  for (const k of tl.kills) tape.appendChild(el('span', { class: 'tape-kill', style: { left: `${(k.time / tl.duration) * 100}%` } }));
  return tape;
}

/** What kind of video this is: "POV" (solid), "Replay 2D" (outline), plus the format when it is not 16:9. */
export function videoTag(render) {
  return el('span', { class: `vtag vtag-${render.renderer === 'ingame' ? 'ingame' : 'radar'}` }, videoLabel(render));
}

// ------------------------------------------------------------------------------------------- posters

function attachPreview(frame, render) {
  const src = safeUrl(render.video_url);
  if (!src || reducedMotion()) return;
  let timer = 0;
  let video = null;
  const stop = () => {
    clearTimeout(timer);
    if (video) {
      video.pause();
      video.removeAttribute('src');
      video.load();
      video.remove();
      video = null;
    }
  };
  frame.addEventListener('pointerenter', (e) => {
    if (e.pointerType !== 'mouse') return;
    timer = setTimeout(() => {
      video = el('video', { class: 'poster-preview', muted: true, loop: true, playsInline: true, preload: 'auto', 'aria-hidden': 'true', tabindex: '-1' });
      video.muted = true;
      video.src = src;
      frame.appendChild(video);
      video.play().catch(() => {});
    }, 220);
  });
  frame.addEventListener('pointerleave', stop);
}

/**
 * The grade of a play: tier letter and score ("S 79"), set as type, not as a badge. Nothing for plays the
 * scorer has not graded (older matches). `big` is the play page version.
 */
export function grade(h, { big = false } = {}) {
  if (!isGraded(h)) return null;
  const score = Math.round(h.score);
  return el('span', { class: `grade grade-${h.tier}${big ? ' grade-big' : ''}`, role: 'img', 'aria-label': t('grade.aria', { score, tier: h.tier }), title: t('grade.aria', { score, tier: h.tier }) },
    el('span', { class: 'grade-tier', 'aria-hidden': 'true' }, h.tier), el('span', { class: 'grade-score', 'aria-hidden': 'true' }, String(score)));
}

/** Where a play happened. Inside a match page the map is known, so the team is named instead. */
function whereText(play, context) {
  const { h, m } = play;
  if (context !== 'match') return playWhere(h, m);
  const team = h.team && m && m[h.team] ? m[h.team].name : null;
  const round = h.round ? t('play.round', { round: h.round }) : '';
  return joinMeta(team, team ? round.toLowerCase() : round);
}

/**
 * Notation + who + what + where, the caption used under posters and in rows. The curation word ("Jogada da
 * partida", "Destaque") leads the where-line; `showFeatured: false` drops "Destaque" in lists where every
 * play is one, so the word is not repeated down the page.
 */
function playCaption(play, { showPlayer = true, showFeatured = true, context } = {}) {
  const { h } = play;
  const name = h.player && h.player.name;
  const word = isBestOfMatch(h) || showFeatured ? curationWord(h) : '';
  return [
    el('span', { class: 'token' }, playToken(h)),
    el('span', { class: 'cap' },
      showPlayer && name ? el('span', { class: 'cap-name' }, name) : null,
      el('span', { class: 'cap-line' }, playLine(h)),
      el('span', { class: 'cap-where' }, word ? el('span', { class: `cap-word${isBestOfMatch(h) ? ' is-best' : ''}` }, word) : null, word ? metaGap() : null, whereText(play, context))),
  ];
}

/** The separator between two facts as its own text node (", " or the kit's " · "). */
function metaGap() {
  return joinMeta('a', 'b').slice(1, -1);
}

/** What stands where the video would be when a play has none: on its way, vertical only, or nothing. */
function videoStateText(h) {
  const waiting = pendingRender(h.renders);
  if (waiting) return t(waiting.status === 'running' ? 'video.rendering' : 'video.queued');
  return sortRenders(h.renders).length ? t('video.verticalOnly') : t('video.none');
}

function playLabel(play, context) {
  const { h, r } = play;
  const name = (h.player && h.player.name) || '';
  const parts = [name, playLine(h), curationWord(h), whereText(play, context)];
  if (r) parts.push(videoLabel(r));
  if (isGraded(h)) parts.push(t('grade.aria', { score: Math.round(h.score), tier: h.tier }));
  return parts.filter(Boolean).join(', ');
}

/** A play as a poster: real frame, video kind, length, its tape, then the caption. Links to the play. */
export function playPoster(play, opts = {}) {
  const { h, m } = play;
  const r = play.r && renderFormat(play.r) === '16:9' ? play.r : null;
  const frame = el('span', { class: 'poster-frame' });
  const thumb = r && safeUrl(r.thumbnail_url);
  if (thumb) frame.appendChild(el('img', { src: thumb, alt: '', loading: 'lazy', decoding: 'async' }));
  if (r) {
    frame.appendChild(videoTag(r));
    frame.appendChild(el('span', { class: 'poster-time' }, clock(r.duration_s ?? h.duration_s)));
    attachPreview(frame, r);
  } else {
    // No 16:9 video yet. One may be queued or rendering (say so: it is not "missing"), or only a vertical
    // one exists, which is never cropped into a poster.
    frame.classList.add('is-empty');
    if (pendingRender(h.renders)) frame.classList.add('is-pending');
    frame.appendChild(el('span', { class: 'poster-none' }, videoStateText(h)));
  }
  return el('a', { class: 'poster', href: hrefPlay(h), 'aria-label': playLabel(play, opts.context) },
    frame,
    Array.isArray(h.segments) ? playTape(h, m && m.tickrate) : el('div', { class: 'tape', 'aria-hidden': 'true' }),
    el('span', { class: 'poster-cap' }, playCaption(play, opts), grade(h)));
}

/** A play as one line of a running order. `trailing` is an optional node shown at the end (state, action). */
export function playRow(play, { showPlayer = true, showFeatured = true, context, thumb = true, current = false, nowPlaying = false, trailing } = {}) {
  const { h } = play;
  const r = play.r && renderFormat(play.r) === '16:9' ? play.r : null;
  const src = thumb && r ? safeUrl(r.thumbnail_url) : null;
  const { m } = play;
  // The play on the stage is already described next to the video: its line in the running order only marks
  // the place ("Tocando agora"), it does not say everything twice.
  if (nowPlaying) {
    return el('li', { class: 'row is-current is-now', 'aria-current': 'true' },
      el('span', { class: 'row-link' },
        el('span', { class: 'row-thumb' }, src ? el('img', { src, alt: '', loading: 'lazy', decoding: 'async' }) : null),
        el('span', { class: 'token' }, playToken(h)),
        el('span', { class: 'cap' }, el('span', { class: 'cap-now' }, t('home.nowPlaying')), el('span', { class: 'cap-where' }, (h.player && h.player.name) || '')),
        el('span', { class: 'row-end' }, el('span', { class: 'row-time' }, clock((r && r.duration_s) ?? h.duration_s)))));
  }
  const waiting = !r ? pendingRender(h.renders) : null;
  const pic = thumb
    ? el('span', { class: `row-thumb${src ? '' : ' is-empty'}` },
      src ? el('img', { src, alt: '', loading: 'lazy', decoding: 'async' }) : (Array.isArray(h.segments) ? playTape(h, m && m.tickrate) : null),
      r ? el('span', { class: `row-kind row-kind-${r.renderer === 'ingame' ? 'ingame' : 'radar'}` }, r.renderer === 'ingame' ? t('video.ingameShort') : t('video.radarShort')) : null,
      waiting ? el('span', { class: 'row-state' }, t(waiting.status === 'running' ? 'video.rendering' : 'video.queued')) : null)
    : null;
  const link = el('a', { class: 'row-link', href: hrefPlay(h), 'aria-label': playLabel(play, context), 'aria-current': current ? 'true' : null },
    pic, playCaption(play, { showPlayer, showFeatured, context }),
    el('span', { class: 'row-end' }, grade(h), el('span', { class: 'row-time' }, clock((r && r.duration_s) ?? h.duration_s))));
  return el('li', { class: `row${current ? ' is-current' : ''}` }, link, trailing || null);
}

// ------------------------------------------------------------------------------------------- kill feed

const FLAGS = [
  ['headshot', 'kill.hs'], ['wallbang', 'kill.wallbang'], ['through_smoke', 'kill.smoke'], ['noscope', 'kill.noscope'],
  ['attacker_blind', 'kill.blind'], ['attacker_airborne', 'kill.airborne'],
];

const sideClass = (ref) => (ref && ref.side === 'CT' ? 'side-ct' : ref && ref.side === 'T' ? 'side-t' : '');

/**
 * One kill, the way the HUD writes it: attacker, weapon, victim. `time` is the label on the left
 * (round clock or video time). With `onSeek` the line is a button.
 */
export function killLine(kill, { time, onSeek, context = false, linkPlayers = false, attacker = true } = {}) {
  const who = (ref, cls) => {
    if (!ref) return el('span', { class: `kill-who ${cls}` }, t('kill.world'));
    if (linkPlayers) return el('a', { class: `kill-who ${cls} ${sideClass(ref)}`, href: hrefPlayer(ref.steamid) }, ref.name);
    return el('span', { class: `kill-who ${cls} ${sideClass(ref)}` }, ref.name);
  };
  const flags = FLAGS.filter(([key]) => kill[key]).map(([, label]) => el('span', { class: 'kill-flag' }, t(label)));
  if (context) {
    if (kill.is_opening) flags.push(el('span', { class: 'kill-flag kill-ctx' }, t('kill.opening')));
    if (kill.is_trade) flags.push(el('span', { class: 'kill-flag kill-ctx' }, t('kill.trade')));
    if (kill.is_teamkill) flags.push(el('span', { class: 'kill-flag kill-ctx' }, t('kill.teamkill')));
  }
  const body = [
    el('span', { class: 'kill-time' }, time ?? ''),
    attacker ? who(kill.attacker, 'kill-attacker') : null,
    el('span', { class: 'kill-how' }, el('span', { class: 'kill-weapon' }, kill.weapon_name || kill.weapon || ''), flags),
    who(kill.victim, 'kill-victim'),
  ];
  if (onSeek) {
    return el('li', { class: `kill${attacker ? '' : ' kill-solo'}` }, el('button', { type: 'button', class: 'kill-row', onclick: onSeek, title: t('kill.jump') }, body));
  }
  return el('li', { class: `kill${attacker ? '' : ' kill-solo'}` }, el('div', { class: 'kill-row' }, body));
}

// ------------------------------------------------------------------------------------------- round tape

export function reasonKind(reason) {
  const r = String(reason || '').toLowerCase();
  if (r.includes('defus')) return 'defuse';
  if (r.includes('explod') || r.includes('bomb')) return 'bomb';
  if (r.includes('time') || r.includes('saved') || r.includes('expired')) return 'time';
  return 'kills';
}

/** Index of the first round of each later period (side switch or overtime start). */
function periodStarts(rounds) {
  const starts = new Set();
  for (let i = 1; i < rounds.length; i += 1) {
    if (rounds[i].ct_team !== rounds[i - 1].ct_team || rounds[i].is_overtime !== rounds[i - 1].is_overtime) starts.add(i);
  }
  return starts;
}

/** Compact, read-only round history for a scorebug row. */
export function roundTapeMini(rounds) {
  const starts = periodStarts(rounds);
  const tape = el('span', { class: 'rtape-mini', 'aria-hidden': 'true' });
  rounds.forEach((r, i) => {
    const side = r.winner_side === 'CT' ? 'ct' : 't';
    tape.appendChild(el('span', { class: `rmini${starts.has(i) ? ' is-period' : ''}` },
      el('span', { class: `rmini-cell${r.winner_team === 'team_a' ? ` is-${side}` : ''}` }),
      el('span', { class: `rmini-cell${r.winner_team === 'team_b' ? ` is-${side}` : ''}` })));
  });
  return tape;
}

export function roundLabel(r, teams) {
  const winner = teams[r.winner_team] || '';
  const side = r.winner_side === 'CT' ? t('side.ct') : t('side.t');
  return t('round.aria', { n: r.number, winner, side, how: t(`reason.${reasonKind(r.reason)}`), a: r.score_team_a, b: r.score_team_b });
}

/**
 * The match as a tape: two rows of rounds (top = team_a won, bottom = team_b won) coloured by the side that
 * won, the plays pinned above their rounds, equipment value below. Each round is a button.
 * `playsByRound`: Map(round -> count). Returns `{el, select(number|null)}`.
 */
export function roundTape(rounds, { teams, playsByRound = new Map(), selected = null, onSelect } = {}) {
  const starts = periodStarts(rounds);
  const maxEquip = Math.max(1, ...rounds.map((r) => Math.max(r.ct_equip_value || 0, r.t_equip_value || 0)));
  const buttons = new Map();
  const list = el('ol', { class: 'rtape', style: { '--rounds': rounds.length + starts.size } });
  rounds.forEach((r, i) => {
    const side = r.winner_side === 'CT' ? 'ct' : 't';
    const kind = reasonKind(r.reason);
    const mark = kind === 'bomb' ? icon('bomb', 12) : kind === 'defuse' ? icon('defuse', 12) : kind === 'time' ? icon('clock', 12) : null;
    const aSide = r.ct_team === 'team_a' ? 'ct' : 't';
    const bSide = aSide === 'ct' ? 't' : 'ct';
    const aEquip = aSide === 'ct' ? r.ct_equip_value : r.t_equip_value;
    const bEquip = bSide === 'ct' ? r.ct_equip_value : r.t_equip_value;
    const pins = playsByRound.get(r.number) || 0;
    const btn = el('button', {
      type: 'button', class: 'rnd', 'aria-pressed': selected === r.number ? 'true' : 'false', 'aria-label': roundLabel(r, teams), title: roundLabel(r, teams),
      onclick: () => onSelect && onSelect(r.number),
    },
      el('span', { class: `rnd-pin${pins ? ' has-plays' : ''}` }, pins > 1 ? el('span', null, String(pins)) : null),
      el('span', { class: `rnd-cell${r.winner_team === 'team_a' ? ` is-${side}` : ''}` }, r.winner_team === 'team_a' ? mark : null),
      el('span', { class: `rnd-cell${r.winner_team === 'team_b' ? ` is-${side}` : ''}` }, r.winner_team === 'team_b' ? mark : null),
      el('span', { class: 'rnd-eco' },
        el('span', { class: `rnd-eco-bar is-${aSide}`, style: { height: `${Math.round(((aEquip || 0) / maxEquip) * 100)}%` } }),
        el('span', { class: `rnd-eco-bar is-${bSide}`, style: { height: `${Math.round(((bEquip || 0) / maxEquip) * 100)}%` } })),
      el('span', { class: 'rnd-n' }, String(r.number)));
    buttons.set(r.number, btn);
    list.appendChild(el('li', { class: starts.has(i) ? 'is-period' : null }, btn));
  });
  return {
    el: list,
    select(number) {
      for (const [n, btn] of buttons) btn.setAttribute('aria-pressed', n === number ? 'true' : 'false');
    },
  };
}

// ------------------------------------------------------------------------------------------- matches

/** Score of a period of the match, e.g. the first half: `{a, b}`. */
export function halves(rounds) {
  const starts = [...periodStarts(rounds)];
  if (!starts.length) return [];
  const out = [];
  let prev = { a: 0, b: 0 };
  [...starts, rounds.length].forEach((end) => {
    const last = rounds[end - 1];
    out.push({ a: last.score_team_a - prev.a, b: last.score_team_b - prev.b, totalA: last.score_team_a, totalB: last.score_team_b });
    prev = { a: last.score_team_a, b: last.score_team_b };
  });
  return out;
}

export function matchWhen(m) {
  return m.played_at ? whenText(m.played_at) : t('match.uploaded', { date: shortDate(m.created_at) });
}

/** "Time A" means little on its own: the two or three best players of a team give it a face. */
export function teamFaces(board, key, max = 3) {
  const team = ((board && board.teams) || []).find((x) => x.key === key);
  if (!team) return '';
  return [...(team.players || [])].sort((x, y) => (y.rating || 0) - (x.rating || 0)).slice(0, max).map((p) => p.name).join(', ');
}

/** A match as a scorebug row: map, the two teams around the score, its round tape, how many plays. */
export function matchRow(m) {
  const ready = m.status === 'ready';
  const a = m.team_a || {};
  const b = m.team_b || {};
  const aWon = ready && a.score > b.score;
  const bWon = ready && b.score > a.score;
  const tapeSlot = el('span', { class: 'mrow-tape' });
  const facesA = el('span', { class: 'mrow-faces' });
  const facesB = el('span', { class: 'mrow-faces' });
  const when = m.played_at ? timeOfDay(m.played_at) : t('match.uploadedShort', { date: shortDate(m.created_at) });
  let middle;
  if (ready) {
    middle = el('span', { class: 'mrow-score' },
      el('span', { class: `mrow-team mrow-team-a${aWon ? ' is-winner' : ''}` }, el('span', { class: 'mrow-teamname' }, a.name || t('team.a')), facesA),
      el('span', { class: `score${aWon ? ' is-winner' : ''}` }, String(a.score ?? '')),
      tapeSlot,
      el('span', { class: `score${bWon ? ' is-winner' : ''}` }, String(b.score ?? '')),
      el('span', { class: `mrow-team mrow-team-b${bWon ? ' is-winner' : ''}` }, el('span', { class: 'mrow-teamname' }, b.name || t('team.b')), facesB));
  } else {
    const text = m.status === 'failed' ? t('match.failed', { error: m.error || '' }) : t(`match.status.${m.status}`);
    middle = el('span', { class: `mrow-status is-${m.status}` }, text);
  }
  const label = ready
    ? t('match.aria', { map: mapName(m.map), a: a.name || '', sa: a.score, b: b.name || '', sb: b.score })
    : `${mapName(m.map)}, ${t(`match.status.${m.status}`)}`;
  const row = el('a', { class: 'mrow', href: hrefMatch(m.id), 'aria-label': label },
    el('span', { class: 'mrow-map' }, el('span', { class: 'mrow-mapname' }, m.map ? mapName(m.map) : m.filename), el('span', { class: 'mrow-when' }, when)),
    middle,
    el('span', { class: 'mrow-meta' }, ready ? t('match.plays', { n: m.highlight_count || 0 }) : ''));
  if (ready) {
    const io = new IntersectionObserver((entries) => {
      if (!entries.some((e) => e.isIntersecting)) return;
      io.disconnect();
      data.rounds(m.id).then((rounds) => fill(tapeSlot, roundTapeMini(rounds))).catch(() => {});
      data.scoreboard(m.id).then((board) => {
        facesA.textContent = teamFaces(board, 'team_a');
        facesB.textContent = teamFaces(board, 'team_b');
      }).catch(() => {});
    }, { rootMargin: '200px' });
    io.observe(row);
  }
  return row;
}

/** Matches grouped by the night they were played. Matches without a date go last, under their own heading. */
export function matchList(matches, { grouped = true } = {}) {
  if (!grouped) return el('div', { class: 'mrows' }, matches.map(matchRow));
  const groups = [];
  const byKey = new Map();
  for (const m of matches) {
    const night = nightOf(m.played_at);
    const key = night ? night.key : '';
    if (!byKey.has(key)) {
      const group = { key, label: night ? night.label : t('match.noDate'), items: [] };
      byKey.set(key, group);
      groups.push(group);
    }
    byKey.get(key).items.push(m);
  }
  groups.sort((x, y) => (x.key && y.key ? y.key.localeCompare(x.key) : x.key ? -1 : y.key ? 1 : 0));
  const single = groups.length === 1 && !groups[0].key;
  return el('div', { class: 'nights' }, groups.map((g) => el('section', { class: 'night' },
    single ? null : el('h3', { class: 'night-title' }, g.label),
    el('div', { class: 'mrows' }, g.items.map(matchRow)))));
}

/**
 * The kill feed of a play, tied to its video: each line shows when the kill happens in the clip, jumps
 * there when pressed, and lights up once the playhead has crossed it. Returns `{el, sync(seconds)}`.
 * Without a usable timeline (`tl` null) the lines show the round clock and are not buttons.
 */
export function playKillFeed(kills, tl, onSeek) {
  const timeByTick = new Map(tl ? tl.kills.map((k) => [k.tick, k.time]) : []);
  const lines = [];
  const list = el('ol', { class: 'kills' });
  for (const kill of kills) {
    const at = timeByTick.get(kill.tick);
    const line = at !== undefined
      ? killLine(kill, { time: clock(at), attacker: false, onSeek: onSeek ? () => onSeek(Math.max(0, at - 1.5)) : null })
      : killLine(kill, { time: clock(kill.clock_s), attacker: false });
    lines.push({ node: line, at });
    list.appendChild(line);
  }
  return {
    el: list,
    sync(seconds) {
      for (const line of lines) line.node.classList.toggle('is-hit', line.at !== undefined && seconds >= line.at);
    },
  };
}
