// The browsing pages: all plays (by kind, a page at a time), one player, the roster, all matches.

import { el, fill } from '../dom.js';
import { t, joinMeta, list as listWords, mapName, num, scoreText, whenText } from '../i18n.js';
import { errorMessage } from '../api.js';
import { data, enrichPlays, loadFeed, loadPlayerPlays, loadRoster } from '../data.js';
import { failKindLabel, hrefMatch } from '../plays.js';
import { duelFromSelf, duelRow } from '../insights.js';
import { matchList, playPoster, playRow } from '../parts.js';
import { isOrganizer } from '../session.js';
import { button, empty, failure, linkButton, loading, setPageTitle } from '../ui.js';
import { rosterList } from './home.js';

const KINDS = [
  ['', 'plays.all'], ['featured', 'plays.featured'], ['clutch', 'plays.kind.clutch'], ['ace', 'plays.kind.ace'], ['quad_kill', 'plays.kind.quad'],
  ['triple_kill', 'plays.kind.triple'], ['double_kill', 'plays.kind.double'],
];
const PAGE = 24;

const playsHref = (kind) => `#/plays${kind ? `?kind=${encodeURIComponent(kind)}` : ''}`;

export function posterGrid(plays, opts) {
  return el('div', { class: 'posters' }, plays.map((p) => playPoster(p, opts)));
}

// ------------------------------------------------------------------------------------------- all plays

export function mountPlays(root, route) {
  let alive = true;
  let offset = 0;
  const plays = [];
  const kind = route.query.get('kind') || '';
  const opts = { showFeatured: kind !== 'featured' };
  const grid = el('div', { class: 'posters' });
  const count = el('p', null);
  const more = button(t('plays.more'), { onclick: () => load() });
  const tail = el('div', { class: 'count-line', hidden: true }, count, more);
  const state = el('div', null, loading());

  fill(root,
    el('header', { class: 'page-head' }, el('h1', null, t('plays.title'))),
    el('div', { class: 'filters' },
      el('nav', { class: 'kinds', 'aria-label': t('plays.byKind') }, KINDS.map(([value, label]) =>
        el('a', { href: playsHref(value), 'aria-current': value === kind ? 'true' : null }, t(label))))),
    grid, state, tail);

  // "Destaques" sits with the kinds in the filter but is its own question to the server.
  const filter = kind === 'featured' ? { featured: true } : { kind: kind || undefined };

  function load() {
    more.disabled = true;
    loadFeed({ limit: PAGE, offset, ...filter }).then(async ({ plays: page, total }) => {
      if (!alive) return;
      const fresh = (kind === 'featured' ? page.filter((p) => p.h.featured === true) : page).filter((p) => !plays.some((q) => q.h.id === p.h.id));
      if (fresh.some((p) => !Array.isArray(p.h.segments))) await enrichPlays(fresh, 12).catch(() => {});
      if (!alive) return;
      offset += page.length;
      plays.push(...fresh);
      for (const p of fresh) grid.appendChild(playPoster(p, opts));
      fill(state);
      if (!plays.length) {
        fill(state, empty(kind ? t('plays.noneFiltered') : t(isOrganizer() ? 'plays.none' : 'plays.noneVisitor'),
          kind ? linkButton(t('plays.clear'), '#/plays') : linkButton(t('nav.matches'), '#/matches')));
        return;
      }
      // The feed says how many plays there are in all (X-Total-Count); an older server does not, and then a
      // full page means there may be more.
      const known = Number.isFinite(total);
      const maybeMore = known ? offset < total : page.length === PAGE;
      tail.hidden = false;
      count.textContent = known && maybeMore ? t('plays.shownOf', { n: plays.length, total })
        : t(maybeMore ? 'plays.shown' : 'plays.shownAll', { n: plays.length });
      more.hidden = !maybeMore;
      more.disabled = false;
    }).catch((err) => {
      if (!alive) return;
      more.disabled = false;
      fill(state, failure(errorMessage(err), () => {
        fill(state, loading());
        load();
      }));
    });
  }
  load();
  return () => {
    alive = false;
  };
}

// ------------------------------------------------------------------------------------------- one player

// One call gives the player across matches (GET /insights/players/<id>): totals, best plays, fails, nemesis
// and favourite victim, chat habits, and one line per match. Two more fetch what only they have: his plays
// that have a video (the feed) and his main duels. On a server without insights the page falls back to
// reading the scoreboards match by match, as it used to.
export function mountPlayer(root, route) {
  let alive = true;
  const steamid = route.id;
  const title = el('h1', { class: 'nick' }, '');
  const lede = el('p', { class: 'lede' });
  const body = el('div', null, loading());
  fill(root, el('header', { class: 'page-head' }, title, lede), body);

  async function load() {
    fill(body, loading());
    try {
      const [profile, feed] = await Promise.all([data.playerProfile(steamid), loadFeed({ player: steamid, limit: 12 }).catch(() => ({ plays: [] }))]);
      if (!alive) return;
      if (!profile) {
        await loadLegacy();
        return;
      }
      const self = profile.player || { steamid, name: steamid };
      title.textContent = self.name;
      setPageTitle(self.name);
      lede.textContent = t('player.summary', {
        matches: t('count.matches', { n: profile.matches || 0 }), wins: t('count.wins', { n: profile.wins || 0 }),
        rating: num(profile.rating, 2), kd: num(profile.kd_ratio, 2), adr: num(profile.adr, 1),
      });

      // plays: the ones with a video as posters, then his best ones that have none yet as lines
      const withVideo = feed.plays.filter((p) => p.h.player && p.h.player.steamid === steamid);
      const seen = new Set(withVideo.map((p) => p.h.id));
      const mapOf = new Map((profile.history || []).map((m) => [m.match_id, m.map]));
      const asPlay = (x) => ({ h: { ...x, id: x.highlight_id, player: self, tags: x.tags || [] }, m: { id: x.match_id, map: mapOf.get(x.match_id) }, r: null });
      const otherBest = (profile.best_plays || []).filter((x) => !seen.has(x.highlight_id)).map(asPlay);
      const fails = (profile.worst_fails || []).map(asPlay);
      const failKinds = Object.entries(profile.fails_by_kind || {}).sort((a, b) => b[1] - a[1]).map(([kind, n]) => `${n}× ${failKindLabel(kind).toLowerCase()}`);
      const chat = profile.chat || {};
      const chatBits = [
        chat.whine ? t('player.chatWhine', { n: chat.whine }) : null,
        chat.death_cries ? t('player.chatDeathCries', { n: chat.death_cries }) : null,
        chat.toxic ? t('player.chatToxic', { n: chat.toxic }) : null,
        chat.laugh ? t('player.chatLaugh', { n: chat.laugh }) : null,
        chat.nice ? t('player.chatNice', { n: chat.nice }) : null,
      ].filter(Boolean);
      const duelsHost = el('div', null);

      fill(body,
        withVideo.length || otherBest.length ? el('section', { class: 'section' },
          el('h2', null, t('player.plays')),
          withVideo.length ? posterGrid(withVideo, { showPlayer: false }) : null,
          otherBest.length ? [el('h3', { class: 'sub' }, t(withVideo.length ? 'player.bestOther' : 'player.best')),
            el('ol', { class: 'rows' }, otherBest.map((p) => playRow(p, { showPlayer: false, thumb: false })))] : null) : null,

        profile.nemesis || profile.victim ? el('section', { class: 'section' },
          el('div', { class: 'section-head' }, el('h2', null, t('player.duels')),
            el('a', { class: 'more', href: `#/rivalidades?player=${encodeURIComponent(steamid)}` }, t('player.allDuels'))),
          profile.nemesis ? el('div', { class: 'duel-labelled' }, el('p', { class: 'duel-label' }, t('player.nemesis')), el('ol', { class: 'duels' }, duelFromSelf(self, profile.nemesis))) : null,
          profile.victim ? el('div', { class: 'duel-labelled' }, el('p', { class: 'duel-label' }, t('player.victim')), el('ol', { class: 'duels' }, duelFromSelf(self, profile.victim))) : null,
          duelsHost) : null,

        profile.fails ? el('section', { class: 'section' },
          el('h2', null, t('nav.banter')),
          el('p', { class: 'note section-note' }, t('player.fails', { n: profile.fails }), failKinds.length ? ` ${listWords(failKinds)}.` : ''),
          fails.length ? el('ol', { class: 'rows' }, fails.map((p) => playRow(p, { showPlayer: false, thumb: false }))) : null) : null,

        chat.messages ? el('section', { class: 'section' },
          el('h2', null, t('player.chat')),
          el('p', null, t('player.chatMessages', { n: chat.messages }), chatBits.length ? ` ${capital(listWords(chatBits))}.` : ''),
          chat.cried_about && chat.cried_about_count ? el('p', { class: 'note' }, t('player.criedAbout', { n: chat.cried_about_count }), ' ',
            el('a', { href: `#/player/${encodeURIComponent(chat.cried_about.steamid)}` }, chat.cried_about.name), '.') : null) : null,

        (profile.history || []).length ? el('section', { class: 'section' }, el('h2', null, t('player.matches')), historyLines(profile.history)) : null);

      // his other main duels, beyond nemesis and victim
      data.rivalries({ player: steamid, min_duels: 4, limit: 6 }).then((res) => {
        if (!alive || !res) return;
        const skip = new Set([profile.nemesis && profile.nemesis.player.steamid, profile.victim && profile.victim.player.steamid]);
        const rest = (res.pairs || []).filter((p) => !skip.has(p.a.steamid === steamid ? p.b.steamid : p.a.steamid)).slice(0, 4);
        if (rest.length) fill(duelsHost, el('h3', { class: 'sub' }, t('player.otherDuels')), el('ol', { class: 'duels' }, rest.map(duelRow)));
      }).catch(() => {});
    } catch (err) {
      if (alive) fill(body, failure(errorMessage(err), load));
    }
  }

  // Servers without the insights endpoints: the old way, one scoreboard per match.
  async function loadLegacy() {
    const plays = await loadPlayerPlays(steamid);
    if (!alive) return;
    const matches = (await data.matches()).filter((m) => m.status === 'ready').slice(0, 24);
    const boards = await Promise.all(matches.map((m) => data.scoreboard(m.id).catch(() => null)));
    if (!alive) return;
    const lines = [];
    boards.forEach((sb, i) => {
      if (!sb) return;
      for (const team of sb.teams || []) {
        const row = (team.players || []).find((p) => p.steamid === steamid);
        if (row) lines.push({ match_id: matches[i].id, map: matches[i].map, played_at: matches[i].played_at, won: team.won, score: `${matches[i].team_a.score}-${matches[i].team_b.score}`, kills: row.kills, deaths: row.deaths, assists: row.assists, rating: row.rating });
      }
    });
    const name = (plays[0] && plays[0].h.player.name) || steamid;
    title.textContent = name;
    setPageTitle(name);
    const withVideo = plays.filter((p) => p.r);
    if (!plays.length && !lines.length) {
      fill(body, empty(t('player.unknown'), linkButton(t('nav.players'), '#/players')));
      return;
    }
    lede.textContent = t('player.lede', { plays: t('count.plays', { n: plays.length }), matches: t('count.matches', { n: lines.length }), videos: t('count.videos', { n: withVideo.length }) });
    fill(body,
      withVideo.length ? el('section', { class: 'section' }, el('h2', null, t('player.plays')), posterGrid(withVideo, { showPlayer: false })) : null,
      lines.length ? el('section', { class: 'section' }, el('h2', null, t('player.matches')), historyLines(lines)) : null);
  }

  load().catch((err) => {
    if (!alive) return;
    // An unknown player answers 404: say so plainly.
    fill(body, empty(t('player.unknown'), linkButton(t('nav.players'), '#/players')));
    title.textContent = title.textContent || t('players.title');
    console.warn(errorMessage(err));
  });
  return () => {
    alive = false;
  };
}

const capital = (text) => (text ? text.charAt(0).toUpperCase() + text.slice(1) : text);

// One line per match: the result and this player's numbers in a sentence. The full table lives in the
// scoreboard of each match, one tap away, so it is not rebuilt here.
function historyLines(history) {
  return el('ul', { class: 'pmatches' }, history.map((x) => {
    const [a, b] = String(x.score || '').split('-');
    return el('li', null, el('a', { href: hrefMatch(x.match_id) },
      el('span', { class: 'pmatch-name' }, `${mapName(x.map)} ${b !== undefined ? scoreText(a, b) : ''}`.trim()),
      el('span', { class: 'pmatch-rating', title: t('sb.ratingFull') }, num(x.rating, 2)),
      el('span', { class: 'pmatch-line' }, joinMeta(
        t(x.won ? 'player.won' : 'player.lost'),
        `${num(x.kills)} / ${num(x.deaths)} / ${num(x.assists)}`,
        x.played_at ? whenText(x.played_at) : null))));
  }));
}

// ------------------------------------------------------------------------------------------- roster / matches

export function mountPlayers(root) {
  let alive = true;
  let roster = [];
  const list = el('div', null, loading());
  const search = el('input', { class: 'input search', type: 'search', placeholder: t('players.search'), 'aria-label': t('players.search'), oninput: () => draw() });
  fill(root, el('header', { class: 'page-head' }, el('h1', null, t('players.title')), el('p', { class: 'lede' }, t('players.lede'))), search, list);

  function draw() {
    const q = search.value.trim().toLowerCase();
    const found = q ? roster.filter((p) => p.name.toLowerCase().includes(q)) : roster;
    fill(list, found.length ? rosterList(found) : empty(q ? t('players.noMatch', { q: search.value.trim() }) : t('home.noPlayers')));
  }
  const load = () => loadRoster().then((rows) => {
    if (!alive) return;
    roster = rows;
    draw();
  }).catch((err) => alive && fill(list, failure(errorMessage(err), load)));
  load();
  return () => {
    alive = false;
  };
}

export function mountMatches(root) {
  let alive = true;
  let timer = 0;
  const body = el('div', null, loading());
  fill(root, el('header', { class: 'page-head' }, el('h1', null, t('matches.title'))), body);
  const load = () => data.matches().then((matches) => {
    if (!alive) return;
    fill(body, matches.length
      ? el('section', { class: 'section' }, matchList(matches))
      : empty(t(isOrganizer() ? 'home.noMatches' : 'home.noMatchesVisitor'), isOrganizer() ? linkButton(t('nav.import'), '#/import', { kind: 'primary', icon: 'upload' }) : null));
    clearTimeout(timer);
    if (matches.some((m) => m.status === 'queued' || m.status === 'analyzing')) timer = setTimeout(load, 4000);
  }).catch((err) => alive && fill(body, failure(errorMessage(err), load)));
  load();
  return () => {
    alive = false;
    clearTimeout(timer);
  };
}
