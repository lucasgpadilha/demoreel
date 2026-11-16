// Home: the featured plays of the last night. The best one is already running, the rest wait in a short
// running order next to it, then the matches of that night. Everything else has its own page (all plays,
// all matches, players), so nothing here repeats them.

import { el, fill, reducedMotion } from '../dom.js';
import { t, joinMeta, mapName, nightOf, scoreText } from '../i18n.js';
import { errorMessage } from '../api.js';
import { data, enrichPlays, loadFeed, loadPlayKills } from '../data.js';
import { curationWord, hrefMatch, hrefPlay, hrefPlayer, isBestOfMatch, playLine, playToken, renderFormat, videoTimeline } from '../plays.js';
import { grade, matchList, playKillFeed, playRow } from '../parts.js';
import { createPlayer } from '../player.js';
import { isOrganizer } from '../session.js';
import { empty, failure, iconButton, linkButton, loading, setPageTitle } from '../ui.js';
import { midSentence } from '../insights.js';

const STAGE_PLAYS = 10; // a short list: the rest is one tap away under "Ver todas"
const NIGHT_MATCHES = 6;
const MIN_NIGHT_PLAYS = 3; // a night with fewer featured videos than this is not produced yet

/** The matches of the most recent night, and the window of time they cover. Null when no match has a date. */
function lastNight(matches) {
  const dated = matches.filter((m) => m.status === 'ready' && m.played_at).sort((a, b) => String(b.played_at).localeCompare(String(a.played_at)));
  if (!dated.length) return null;
  const night = nightOf(dated[0].played_at);
  const ofNight = dated.filter((m) => nightOf(m.played_at).key === night.key);
  const times = ofNight.map((m) => Date.parse(m.played_at));
  return {
    key: night.key,
    label: night.label,
    matches: ofNight,
    since: new Date(Math.min(...times) - 1000).toISOString(),
    until: new Date(Math.max(...times) + 1000).toISOString(),
  };
}

export function mountHome(root) {
  let alive = true;
  let player = null;
  let pollTimer = 0;
  const observers = [];

  const stage = el('section', { class: 'stage' }, loading());
  const nightSection = el('section', { class: 'section', hidden: true });
  fill(root, stage, nightSection);
  setPageTitle('');

  // ----------------------------------------------------------------------------------------- stage

  function buildStage(plays, title) {
    let index = 0;
    let feed = null;
    let pausedByScroll = false;
    const tlOf = (p) => (Array.isArray(p.h.segments) ? videoTimeline(p.h, p.r, p.m.tickrate) : null);

    const token = el('span', { class: 'token token-xl' });
    const name = el('a', { class: 'third-name' });
    const line = el('p', { class: 'third-line' });
    const where = el('a', { class: 'third-where' });
    const curation = el('p', { class: 'third-curation' });
    const feedSlot = el('div', { class: 'third-feed' });
    const open = linkButton(t('home.openPlay'), '#/', { kind: 'primary' });
    const rows = el('ol', { class: 'rows rundown-list' });

    player = createPlayer({
      autoplay: true,
      muted: true,
      onEnded: () => show((index + 1) % plays.length, true),
      onTime: (now) => feed && feed.sync(now),
    });

    function drawRundown() {
      // The play on the stage is described under the video; its line here only marks the place.
      fill(rows, plays.map((p, i) => playRow(p, { nowPlaying: i === index, showFeatured: false })));
    }

    function drawFeed(p) {
      const wanted = p;
      feed = null;
      fill(feedSlot);
      loadPlayKills(p.h).then((kills) => {
        if (!alive || plays[index] !== wanted || !kills.length) return;
        feed = playKillFeed(kills, tlOf(p), (at) => player.playFrom(at));
        fill(feedSlot, feed.el);
      }).catch(() => {});
    }

    function show(i, autoplay) {
      index = i;
      const p = plays[i];
      player.load({ render: p.r, timeline: tlOf(p), play: autoplay });
      token.textContent = playToken(p.h);
      name.textContent = (p.h.player && p.h.player.name) || '';
      if (p.h.player && p.h.player.steamid) name.href = hrefPlayer(p.h.player.steamid);
      line.textContent = playLine(p.h);
      const word = curationWord(p.h);
      fill(curation, word ? el('span', { class: `cap-word${isBestOfMatch(p.h) ? ' is-best' : ''}` }, word) : null, grade(p.h));
      curation.hidden = !curation.childNodes.length;
      const a = p.m.team_a || {};
      const b = p.m.team_b || {};
      where.textContent = joinMeta(mapName(p.m.map), p.h.round ? t('play.round', { round: p.h.round }).toLowerCase() : null,
        `${a.name || ''} ${scoreText(a.score, b.score)} ${b.name || ''}`.trim());
      where.href = hrefMatch(p.m.id);
      open.href = hrefPlay(p.h);
      drawRundown();
      drawFeed(p);
    }

    const step = (delta) => show((index + delta + plays.length) % plays.length, !reducedMotion());
    fill(stage,
      el('div', { class: 'stage-screen' }, player.el),
      el('div', { class: 'third' },
        el('div', { class: 'third-main' }, token, el('div', { class: 'third-text' }, name, line, curation, where)),
        feedSlot,
        el('div', { class: 'third-actions' },
          open,
          plays.length > 1 ? iconButton('prev', t('home.prev'), { onclick: () => step(-1) }) : null,
          plays.length > 1 ? iconButton('next', t('home.next'), { onclick: () => step(1) }) : null)),
      el('div', { class: 'rundown' },
        el('div', { class: 'section-head' }, el('h1', null, title), el('a', { class: 'more', href: '#/plays?kind=featured' }, t('home.allPlays'))),
        rows));
    show(0, true);

    // Do not keep a video running where nobody can see it.
    const io = new IntersectionObserver((entries) => {
      const visible = entries.some((e) => e.isIntersecting);
      if (!visible && !player.video.paused) {
        pausedByScroll = true;
        player.video.pause();
      } else if (visible && pausedByScroll) {
        pausedByScroll = false;
        if (!reducedMotion()) player.video.play().catch(() => {});
      }
    }, { threshold: 0.2 });
    io.observe(player.el);
    observers.push(io);

    // Older servers send a feed without segments: fetch them so the tape can be drawn.
    const shown = () => plays[index].r.id;
    const before = shown();
    enrichPlays(plays).then(() => {
      if (!alive) return;
      if (shown() !== before) show(index, !reducedMotion());
      else {
        player.setTimeline(tlOf(plays[index]));
        drawFeed(plays[index]);
      }
    }).catch(() => {});
  }

  const wide = (list) => list.filter((p) => renderFormat(p.r) === '16:9'); // the stage is a 16:9 screen

  async function load() {
    fill(stage, loading());
    try {
      const matches = await data.matches().catch(() => []);
      if (!alive) return;
      const night = lastNight(matches);
      drawNight(night, matches);

      // The featured plays of the last night; if that night has no videos yet, the best of every night.
      let title = t('home.featured');
      let plays = [];
      if (night) {
        plays = wide((await loadFeed({ limit: STAGE_PLAYS, featured: true, since: night.since, until: night.until })).plays).filter((p) => p.h.featured === true);
        if (plays.length >= MIN_NIGHT_PLAYS) title = t('home.featuredNight', { night: midSentence(night.label.split(',')[0]) });
        else plays = [];
      }
      if (!plays.length) {
        const all = wide((await loadFeed({ limit: STAGE_PLAYS, featured: true })).plays);
        const featured = all.filter((p) => p.h.featured === true);
        // A server without curation ignores the filter: then the list is simply the best plays.
        plays = featured.length ? featured : all;
        if (!featured.length) title = t('home.best');
      }
      if (!alive) return;
      if (plays.length) {
        buildStage(plays.slice(0, STAGE_PLAYS), title);
        return;
      }
      stage.classList.add('is-empty');
      const ready = matches.filter((m) => m.status === 'ready');
      if (ready.length) {
        fill(stage, el('h1', null, t('home.best')), empty(t(isOrganizer() ? 'home.noVideos' : 'home.noVideosVisitor'),
          linkButton(t('home.openLatest'), hrefMatch(ready[0].id), { kind: 'primary' })));
      } else {
        fill(stage, el('h1', null, t('home.best')), empty(t(isOrganizer() ? 'home.noMatches' : 'home.noMatchesVisitor'),
          isOrganizer() ? linkButton(t('nav.import'), '#/import', { kind: 'primary', icon: 'upload' }) : null));
      }
    } catch (err) {
      if (alive) fill(stage, failure(errorMessage(err), load));
    }
  }

  // ----------------------------------------------------------------------------------------- the night

  function drawNight(night, matches) {
    const list = night ? night.matches : matches.slice(0, NIGHT_MATCHES);
    nightSection.hidden = !list.length;
    if (!list.length) return;
    const shown = list.slice(0, NIGHT_MATCHES);
    fill(nightSection,
      el('div', { class: 'section-head' },
        el('h2', null, night ? t('home.nightMatches', { night: midSentence(night.label) }) : t('home.matches')),
        el('a', { class: 'more', href: '#/matches' }, t('home.allMatchesCount', { n: matches.length }))),
      // The rest of the night lives on its own pages; from here, one link each.
      night ? el('p', { class: 'night-links' },
        el('a', { class: 'more', href: `#/night/${night.key}` }, t('home.nightTrophies')),
        el('a', { class: 'more', href: `#/zoeira?night=${night.key}` }, t('home.nightBanter'))) : null,
      matchList(shown, { grouped: false }));
    clearTimeout(pollTimer);
    if (matches.some((m) => m.status === 'queued' || m.status === 'analyzing')) {
      pollTimer = setTimeout(() => data.matches().then((next) => alive && drawNight(lastNight(next), next)).catch(() => {}), 5000);
    }
  }

  load();

  return () => {
    alive = false;
    clearTimeout(pollTimer);
    for (const io of observers) io.disconnect();
    if (player) player.destroy();
  };
}

/** The roster: every name is a way into that player's plays. */
export function rosterList(roster) {
  return el('ul', { class: 'roster' }, roster.map((p) => el('li', null,
    el('a', { href: hrefPlayer(p.steamid) },
      el('span', { class: 'roster-name' }, p.name),
      el('span', { class: 'roster-count' }, t('roster.count', { n: p.plays }))))));
}
