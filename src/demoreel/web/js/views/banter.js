// What the mix laughs about and argues about: the fails ("zoeira"), the chat rankings, the rivalries, and
// one night at a glance (its trophies and matches). All of it comes from the insights endpoints; on a
// server without them each page says so instead of breaking.

import { el, fill } from '../dom.js';
import { t } from '../i18n.js';
import { errorMessage } from '../api.js';
import { data, loadFeed } from '../data.js';
import { bestRender, failKindLabel, FAIL_KINDS } from '../plays.js';
import { matchList, playPoster, playRow } from '../parts.js';
import { chatPodiums, chatTable, duelRow, nightName, scopeNav, trophyGrid, midSentence } from '../insights.js';
import { button, empty, failure, linkButton, loading, setPageTitle } from '../ui.js';

const PAGE = 24;

function hrefWith(base, params) {
  const q = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value) q.set(key, value);
  const qs = q.toString();
  return `${base}${qs ? `?${qs}` : ''}`;
}

/** The window of time of one night, for endpoints that filter by since / until. */
function windowOf(night) {
  if (!night || !night.first_played_at || !night.last_played_at) return {};
  return {
    since: new Date(Date.parse(night.first_played_at) - 1000).toISOString(),
    until: new Date(Date.parse(night.last_played_at) + 1000).toISOString(),
  };
}

// ------------------------------------------------------------------------------------------- zoeira

// Fewer fails with a video than this and the page also lists the ones still waiting for theirs.
const FEW = 6;

export function mountZoeira(root, route) {
  let alive = true;
  const night = route.query.get('night') || '';
  const kind = route.query.get('kind') || '';
  const href = (next) => hrefWith('#/zoeira', { night, kind, ...next });
  setPageTitle(t('nav.banter'));

  const scope = el('div', null);
  const grid = el('div', { class: 'posters' });
  const state = el('div', { class: 'banter-waiting' }, loading());
  const count = el('p', null);
  const more = button(t('plays.more'), { onclick: () => loadFails() });
  const tail = el('div', { class: 'count-line', hidden: true }, count, more);

  fill(root,
    el('header', { class: 'page-head' }, el('h1', null, t('banter.title')), el('p', { class: 'lede' }, t('banter.lede')),
      // What was typed in the chat has its own page; the link keeps the night being looked at.
      el('p', { class: 'page-links' }, el('a', { class: 'more', href: hrefWith('#/chat', { night }) }, t('banter.chatLink')))),
    scope,
    el('section', { class: 'section' },
      el('div', { class: 'section-head' }, el('h2', null, t('banter.fails'))),
      el('nav', { class: 'kinds', 'aria-label': t('banter.byKind') },
        el('a', { href: href({ kind: '' }), 'aria-current': kind ? null : 'true' }, t('plays.all')),
        FAIL_KINDS.map((k) => el('a', { href: href({ kind: k }), 'aria-current': k === kind ? 'true' : null }, failKindLabel(k)))),
      grid, tail, state));

  let nights = [];
  let offset = 0;
  const shown = [];

  function loadFails() {
    more.disabled = true;
    const chosen = nights.find((n) => n.date === night);
    loadFeed({ limit: PAGE, offset, category: 'fail', kind: kind || undefined, ...windowOf(chosen) }).then(async ({ plays, total }) => {
      if (!alive) return;
      const fresh = plays.filter((p) => !shown.some((q) => q.h.id === p.h.id));
      offset += plays.length;
      shown.push(...fresh);
      for (const p of fresh) grid.appendChild(playPoster(p));
      fill(state);
      const first = offset === plays.length;
      if (shown.length) {
        const all = total ?? shown.length;
        tail.hidden = false;
        count.textContent = all > shown.length ? t('banter.shownOf', { n: shown.length, total: all }) : t('banter.shownAll', { n: shown.length });
        more.hidden = !(all > shown.length && plays.length === PAGE);
        more.disabled = false;
        // While the batch is still rendering only a few have a video: the rest of the night follows as a list.
        if (!first || all >= FEW) return;
      }
      // Few or no fails have a video yet (they are rendered after the plays). Show the funniest ones of the
      // night as a list, each saying where its video is ("Na fila", "Gerando..."), instead of a thin page.
      const from = chosen || nights[0];
      const ids = from ? from.matches : [];
      const matches = (await data.matches().catch(() => [])).filter((m) => ids.includes(m.id));
      const lists = await Promise.all(matches.map((m) => data.fails(m.id).catch(() => [])));
      if (!alive) return;
      const rows = [];
      lists.forEach((items, i) => {
        for (const h of items) {
          if ((!kind || h.kind === kind) && !shown.some((q) => q.h.id === h.id)) rows.push({ h, m: matches[i], r: bestRender(h.renders) });
        }
      });
      rows.sort((x, y) => (y.h.score || 0) - (x.h.score || 0));
      if (!rows.length) {
        if (!shown.length) fill(state, empty(kind ? t('banter.noneKind') : t('banter.none'), kind ? linkButton(t('plays.clear'), href({ kind: '' })) : null));
        return;
      }
      fill(state,
        el('p', { class: 'note' }, t('banter.noVideosYet', { night: from ? midSentence(nightName(from.date).label) : '' })),
        el('ol', { class: 'rows' }, rows.slice(0, 12).map((p) => playRow(p))));
    }).catch((err) => {
      if (!alive) return;
      more.disabled = false;
      fill(state, failure(errorMessage(err), () => {
        fill(state, loading());
        loadFails();
      }));
    });
  }

  data.nights().then((list) => {
    if (!alive) return;
    nights = Array.isArray(list) ? list : [];
    if (nights.length) fill(scope, scopeNav(nights, night, (date) => href({ night: date })));
    loadFails();
  }).catch(() => {
    if (!alive) return;
    loadFails();
  });

  return () => {
    alive = false;
  };
}

// ------------------------------------------------------------------------------------------- chat

// What the group types in all-chat, for the season or one night: the podiums (who swears, who cries, who
// talks the most...) and then every player's numbers in one table. Nothing here is on another page.
export function mountChat(root, route) {
  let alive = true;
  const night = route.query.get('night') || '';
  const href = (next) => hrefWith('#/chat', { night, ...next });
  setPageTitle(t('nav.chat'));

  const scope = el('div', null);
  const body = el('div', null, loading());
  fill(root,
    el('header', { class: 'page-head' }, el('h1', null, t('chat.pageTitle')), el('p', { class: 'lede' }, t('chat.pageLede'))),
    scope,
    body);

  data.nights().then((list) => {
    if (alive && Array.isArray(list) && list.length) fill(scope, scopeNav(list, night, (date) => href({ night: date })));
  }).catch(() => {});

  function load() {
    fill(body, loading());
    data.chatRanks(night ? { night } : undefined).then((ranks) => {
      if (!alive) return;
      if (!ranks) {
        fill(body, empty(t('insights.unavailable')));
        return;
      }
      // Somebody who only typed server commands (!pause, .ready) said nothing: not a row of zeros.
      const players = (ranks.players || []).filter((p) => p && p.player && p.messages > 0);
      if (!players.length) {
        fill(body, empty(t('chat.none'), night ? linkButton(t('scope.season'), href({ night: '' })) : null));
        return;
      }
      fill(body,
        el('section', { class: 'section' },
          el('h2', null, t('chat.podiums')),
          el('p', { class: 'note' }, t('banter.chatNote', { n: ranks.messages || 0 })),
          chatPodiums(ranks) || el('p', { class: 'note' }, t('banter.chatNone'))),
        el('section', { class: 'section' },
          el('h2', null, t('chat.everyone')),
          chatTable(players)));
    }).catch((err) => {
      if (alive) fill(body, failure(errorMessage(err), load));
    });
  }
  load();
  return () => {
    alive = false;
  };
}

// ------------------------------------------------------------------------------------------- rivalries

const SORTS = [['', 'rivals.most'], ['even', 'rivals.even'], ['blowout', 'rivals.blowout']];
const MAX_PAIRS = 30;

export function mountRivalries(root, route) {
  let alive = true;
  const night = route.query.get('night') || '';
  const sort = route.query.get('sort') || '';
  const player = route.query.get('player') || '';
  const href = (next) => hrefWith('#/rivalidades', { night, sort, player, ...next });
  setPageTitle(t('nav.rivals'));

  const title = el('h1', null, t('rivals.title'));
  const lede = el('p', { class: 'lede' }, t('rivals.lede'));
  const scope = el('div', null);
  const body = el('div', null, loading());
  fill(root,
    el('header', { class: 'page-head' }, title, lede),
    scope,
    el('section', { class: 'section' },
      el('nav', { class: 'kinds', 'aria-label': t('rivals.sortLabel') }, SORTS.map(([value, label]) =>
        el('a', { href: href({ sort: value }), 'aria-current': value === sort ? 'true' : null }, t(label)))),
      body));

  data.nights().then((list) => {
    if (alive && Array.isArray(list) && list.length) fill(scope, scopeNav(list, night, (date) => href({ night: date })));
  }).catch(() => {});

  function load() {
    fill(body, loading());
    // A duel needs a few kills to mean something: fewer in one night or for one player than in a season.
    const query = {
      night: night || undefined,
      player: player || undefined,
      min_duels: player ? 4 : night ? 5 : 10,
      sort: sort === 'blowout' ? 'diff' : 'duels',
      limit: sort === 'even' ? 80 : MAX_PAIRS,
    };
    data.rivalries(query).then((res) => {
      if (!alive) return;
      if (!res) {
        fill(body, empty(t('insights.unavailable')));
        return;
      }
      let pairs = res.pairs || [];
      // "Mais equilibradas": the closest scores among the duels that were fought the most.
      if (sort === 'even') pairs = [...pairs].sort((a, b) => (a.imbalance ?? 1) - (b.imbalance ?? 1) || b.duels - a.duels);
      pairs = pairs.slice(0, MAX_PAIRS);
      if (player) {
        const self = (res.players || []).find((p) => p.player && p.player.steamid === player);
        const name = self ? self.player.name : (pairs[0] && (pairs[0].a.steamid === player ? pairs[0].a.name : pairs[0].b.name)) || '';
        if (name) {
          title.textContent = t('rivals.ofPlayer', { name });
          title.classList.add('nick');
          setPageTitle(t('rivals.ofPlayer', { name }));
          fill(lede, el('a', { class: 'more', href: href({ player: '' }) }, t('rivals.everyone')));
        }
      }
      if (!pairs.length) {
        fill(body, empty(t('rivals.none'), night ? linkButton(t('scope.season'), href({ night: '' })) : null));
        return;
      }
      fill(body, el('ol', { class: 'duels' }, pairs.map(duelRow)),
        el('p', { class: 'note duels-note' }, t('rivals.note', { n: res.min_duels ?? query.min_duels })));
    }).catch((err) => {
      if (alive) fill(body, failure(errorMessage(err), load));
    });
  }
  load();
  return () => {
    alive = false;
  };
}

// ------------------------------------------------------------------------------------------- one night

export function mountNight(root, route) {
  let alive = true;
  const body = el('div', null, loading());
  fill(root, body);

  async function load() {
    fill(body, loading());
    try {
      const nights = await data.nights();
      if (!alive) return;
      if (!Array.isArray(nights) || !nights.length) {
        fill(body, el('header', { class: 'page-head' }, el('h1', null, t('night.title'))), empty(nights ? t('night.none') : t('insights.unavailable')));
        return;
      }
      const at = Math.max(0, nights.findIndex((n) => n.date === route.id));
      const night = route.id && nights[at].date !== route.id ? null : nights[at];
      if (!night) {
        fill(body, el('header', { class: 'page-head' }, el('h1', null, t('night.title'))),
          empty(t('night.unknown'), linkButton(t('night.latest'), `#/night/${nights[0].date}`, { kind: 'primary' })));
        return;
      }
      const name = nightName(night.date).label;
      setPageTitle(name);
      const older = nights[at + 1];
      const newer = nights[at - 1];
      const trophiesHost = el('div', null, loading());
      const matchesHost = el('div', null, loading());
      fill(body,
        el('header', { class: 'page-head' },
          el('h1', null, name),
          el('p', { class: 'lede' }, t('night.lede', {
            matches: t('count.matches', { n: (night.matches || []).length }), players: t('count.players', { n: night.players || 0 }), rounds: t('match.rounds', { n: night.rounds || 0 }),
          })),
          el('nav', { class: 'night-nav', 'aria-label': t('night.other') },
            older ? el('a', { class: 'more', href: `#/night/${older.date}` }, t('night.older', { night: nightName(older.date).short })) : null,
            newer ? el('a', { class: 'more', href: `#/night/${newer.date}` }, t('night.newer', { night: nightName(newer.date).short })) : null,
            el('a', { class: 'more', href: hrefWith('#/zoeira', { night: night.date }) }, t('night.banter')),
            el('a', { class: 'more', href: hrefWith('#/chat', { night: night.date }) }, t('night.chat')),
            el('a', { class: 'more', href: hrefWith('#/rivalidades', { night: night.date }) }, t('night.rivals')))),
        el('section', { class: 'section' }, el('div', { class: 'section-head' }, el('h2', null, t('night.trophies'))), trophiesHost),
        el('section', { class: 'section' }, el('div', { class: 'section-head' }, el('h2', null, t('night.matches'))), matchesHost));

      data.trophies({ night: night.date }).then((res) => {
        if (!alive) return;
        const list = (res && res.trophies) || [];
        fill(trophiesHost, list.length ? trophyGrid(list) : el('p', { class: 'note' }, t('trophy.none')));
      }).catch((err) => alive && fill(trophiesHost, failure(errorMessage(err))));
      data.matches().then((matches) => {
        if (!alive) return;
        const mine = matches.filter((m) => (night.matches || []).includes(m.id));
        fill(matchesHost, mine.length ? matchList(mine, { grouped: false }) : el('p', { class: 'note' }, t('home.noMatchesShort')));
      }).catch((err) => alive && fill(matchesHost, failure(errorMessage(err))));
    } catch (err) {
      if (alive) fill(body, failure(errorMessage(err), load));
    }
  }
  load();
  return () => {
    alive = false;
  };
}
