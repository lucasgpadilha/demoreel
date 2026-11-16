// Shared, cached reads. Views ask here instead of calling the API directly, so navigating back and forth
// does not refetch, and so the community endpoints (brand, feed, sources) degrade in one place.

import { api, ApiError } from './api.js';
import { bestRender } from './plays.js';

const cache = new Map();

function cached(key, ttlMs, load) {
  const hit = cache.get(key);
  if (hit && Date.now() - hit.at < ttlMs) return hit.promise;
  const promise = load();
  cache.set(key, { at: Date.now(), promise });
  promise.catch(() => {
    if (cache.get(key) && cache.get(key).promise === promise) cache.delete(key);
  });
  return promise;
}

/** Drop cached reads whose key starts with `prefix` (after an edit, a render, an import). */
export function invalidate(prefix = '') {
  for (const key of [...cache.keys()]) if (key.startsWith(prefix)) cache.delete(key);
}

// An endpoint this server does not have yet answers 404 / 405. Remember that briefly instead of asking on
// every navigation, but not forever: the server may be upgraded while the page stays open.
const missingSince = new Map();
const MISSING_TTL = 30000;

async function optional(feature, load) {
  const since = missingSince.get(feature);
  if (since && Date.now() - since < MISSING_TTL) return undefined;
  try {
    const value = await load();
    missingSince.delete(feature);
    return value;
  } catch (err) {
    if (err instanceof ApiError && (err.status === 404 || err.status === 405 || err.status === 501)) {
      missingSince.set(feature, Date.now());
      return undefined;
    }
    throw err;
  }
}

export const data = {
  matches: () => cached('matches', 6000, () => api.listMatches({ limit: 200 })),
  match: (id) => cached(`match:${id}`, 4000, () => api.getMatch(id)),
  highlights: (id) => cached(`hl:${id}`, 12000, () => api.highlights(id, { limit: 500 })),
  /** The fails ("zoeira") of a match, funniest first. [] on servers without them. */
  fails: (id) => cached(`hl:${id}:fail`, 12000, async () => {
    const items = await api.highlights(id, { limit: 500, category: 'fail' });
    return (items || []).filter((h) => h.category === 'fail'); // an older server ignores the filter and sends plays
  }),
  // Across matches. Each resolves to undefined on a server that predates the insights endpoints.
  nights: () => cached('nights', 60000, () => optional('insights', () => api.nights())),
  rivalries: (query) => cached(`riv:${JSON.stringify(query || {})}`, 60000, () => optional('insights', () => api.rivalries(query))),
  playerProfile: (steamid) => cached(`profile:${steamid}`, 60000, () => optional('insights', () => api.playerProfile(steamid))),
  trophies: (query) => cached(`trophies:${JSON.stringify(query || {})}`, 60000, () => optional('insights', () => api.trophies(query))),
  chatRanks: (query) => cached(`chat:${JSON.stringify(query || {})}`, 60000, () => optional('insights', () => api.chatRanks(query))),
  rounds: (id) => cached(`rounds:${id}`, 3600000, () => api.rounds(id)),
  /** Kills of one round (small; used for the kill feed next to a play). */
  kills: (id, round) => cached(`kills:${id}:${round}`, 3600000, () => api.kills(id, { round })),
  scoreboard: (id) => cached(`sb:${id}`, 3600000, () => api.scoreboard(id)),
  players: (id) => cached(`players:${id}`, 3600000, () => api.players(id)),
  renders: (id) => cached(`renders:${id}`, 4000, () => api.renders(id)),
  capabilities: () => cached('caps', 20000, () => api.capabilities()),
  /** True when this server can produce every video of a match in one go (POST /matches/{id}/produce). */
  canProduce: () => cached('canProduce', 60000, async () => {
    try {
      const spec = await api.openapi();
      return Object.keys((spec && spec.paths) || {}).some((path) => /\/matches\/\{[^}]+\}\/produce$/.test(path));
    } catch {
      return false;
    }
  }),
  /** Brand kit, or undefined when this server has none. */
  brand: () => optional('brand', () => api.brand()),
  /** Community sources ([] when the server has none or predates them). */
  sources: async () => (await optional('sources', () => api.sources())) || [],
  sourceMatches: (id) => api.sourceMatches(id),
};

const readyMatches = async (limit) => (await data.matches()).filter((m) => m.status === 'ready').slice(0, limit);

/**
 * The best plays that have a video, best first, as `{h, m, r}` (highlight, match, render).
 * Uses GET /feed when the server has it; otherwise builds the same list from the latest matches.
 * `featured: true` keeps the featured plays only, `false` the others (servers without curation ignore it).
 * `since` / `until` (ISO) keep the plays of matches played in that window; `offset` pages through the list.
 * `category: 'fail'` asks for the fails instead of the plays. `total` is how many there are before paging
 * (null when the server does not say). `source` says which path answered.
 */
export async function loadFeed({ player, kind, featured, category, since, until, offset, limit = 24 } = {}) {
  const page = await optional('feed', () => api.feedPage({ limit, player, kind, featured, category, since, until, offset: offset || undefined }));
  const remote = page ? page.items : undefined;
  if (Array.isArray(remote)) {
    const plays = remote
      .filter((item) => item && item.highlight && item.render && item.match)
      .map((item) => ({ h: { ...item.highlight, match_id: item.match.id }, m: item.match, r: item.render }));
    return { plays, source: 'feed', total: page.total };
  }
  if (category === 'fail') return { plays: [], source: 'local', total: 0 }; // fails only come from servers that have them
  const matches = await readyMatches(12);
  const lists = await Promise.all(matches.map((m) => data.highlights(m.id).catch(() => [])));
  const plays = [];
  lists.forEach((items, i) => {
    for (const h of items) {
      if (player && (!h.player || h.player.steamid !== player)) continue;
      if (kind && h.kind !== kind) continue;
      if (featured !== undefined && featured !== null && (h.featured === true) !== featured) continue;
      const r = bestRender(h.renders);
      if (r) plays.push({ h, m: matches[i], r });
    }
  });
  plays.sort((a, b) => b.h.score - a.h.score);
  const inWindow = plays.filter((p) => {
    const at = p.m.played_at ? Date.parse(p.m.played_at) : NaN;
    if (since && !(at >= Date.parse(since))) return false;
    if (until && !(at <= Date.parse(until))) return false;
    return true;
  });
  const from = offset || 0;
  return { plays: inWindow.slice(from, from + limit), source: 'local', total: inWindow.length };
}

/**
 * Feed items carry no segments or kill ticks, which the tape needs, and only one of the play's videos.
 * Fill both in from the highlights of the matches involved (cached; at most `maxMatches` requests).
 * Mutates and returns `plays`.
 */
export async function enrichPlays(plays, maxMatches = 8) {
  const need = [...new Set(plays.filter((p) => !Array.isArray(p.h.segments)).map((p) => p.m.id))].slice(0, maxMatches);
  if (!need.length) return plays; // a current server sends segments, kill ticks and tickrate in the feed itself
  const matches = await data.matches().catch(() => []);
  const matchById = new Map(matches.map((m) => [m.id, m]));
  const lists = await Promise.all(need.map((id) => data.highlights(id).catch(() => [])));
  const byId = new Map(lists.flat().map((h) => [h.id, h]));
  for (const p of plays) {
    const full = byId.get(p.h.id);
    if (full) {
      p.h = { ...p.h, ...full };
      // The feed picks one video per play; make sure it is the POV 16:9 whenever the play has one.
      p.r = bestRender(full.renders) || p.r;
    }
    const m = matchById.get(p.m.id);
    if (m) p.m = { ...m, ...p.m, tickrate: m.tickrate };
  }
  return plays;
}

/**
 * Everyone who has a play in the latest matches: `[{steamid, name, plays, videos}]`, most plays first.
 * There is no players endpoint, so this reads the highlights of up to `maxMatches` matches.
 */
export async function loadRoster(maxMatches = 24) {
  const matches = await readyMatches(maxMatches);
  const lists = await Promise.all(matches.map((m) => data.highlights(m.id).catch(() => [])));
  const roster = new Map();
  lists.forEach((items) => {
    for (const h of items) {
      if (!h.player || !h.player.steamid) continue;
      let row = roster.get(h.player.steamid);
      if (!row) {
        row = { steamid: h.player.steamid, name: h.player.name || h.player.steamid, plays: 0, videos: 0, best: 0 };
        roster.set(row.steamid, row);
      }
      row.plays += 1;
      row.best = Math.max(row.best, h.score || 0);
      if (bestRender(h.renders)) row.videos += 1;
    }
  });
  return [...roster.values()].sort((a, b) => b.plays - a.plays || b.best - a.best);
}

/** All plays of one player in the latest matches (with or without video), best first, as `{h, m, r|null}`. */
export async function loadPlayerPlays(steamid, maxMatches = 24) {
  const matches = await readyMatches(maxMatches);
  const lists = await Promise.all(matches.map((m) => data.highlights(m.id).catch(() => [])));
  const plays = [];
  lists.forEach((items, i) => {
    for (const h of items) if (h.player && h.player.steamid === steamid) plays.push({ h, m: matches[i], r: bestRender(h.renders) });
  });
  plays.sort((a, b) => b.h.score - a.h.score);
  return plays;
}

/** The kills that make up a play (attacker = its player, in its round), in order. */
export async function loadPlayKills(h) {
  if (!h.round || !h.match_id || !h.player || !h.player.steamid) return [];
  const kills = await data.kills(h.match_id, h.round);
  const mine = kills.filter((k) => k.attacker && k.attacker.steamid === h.player.steamid && !k.is_teamkill && !k.is_suicide);
  const ticks = new Set(h.kill_ticks || []);
  return ticks.size ? mine.filter((k) => ticks.has(k.tick)) : mine;
}
