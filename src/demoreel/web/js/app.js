// Bootstrap: brand, session (visitor or organizer), header and footer, hash router, embedding.
//
// Routes (all deep-linkable):
//   #/                         home: the featured plays of the last night
//   #/plays?kind=              every play that has a video
//   #/play/<id>[/edit]         one play (?view=<variant> picks the video)
//   #/matches                  every match, by night
//   #/match/<id>[/round/<n>]   one match (also /reel/<render id>)
//   #/players, #/player/<steamid>
//   #/zoeira?night=&kind=      the fails ("zoeira")
//   #/chat?night=              the chat rankings: podiums and every player's numbers
//   #/rivalidades?night=&sort=&player=   duels between two players
//   #/night/<YYYY-MM-DD>       one night: its trophies and matches
//   #/import                   organizer: import from the community site, or upload a .dem
//
// `?embed=1` runs the app inside the community's own page: no header or footer of ours, a compact menu in
// the content, the organizer's tools never shown, and messages to the parent page so it can size the frame
// and mirror the route (see docs/embedding.md).

import { el, fill, icon, safeUrl } from './dom.js';
import { t, locale, locales, setLocale } from './i18n.js';
import { api, apiInfo, hasApiKey, isAuthRequired, onAuthRequired, requestApiKey, resetApiBase, serverUrl, setKeyPrompter } from './api.js';
import { applyCachedBrand, brand, loadBrand } from './brand.js';
import { data } from './data.js';
import { isEmbed, isOrganizer, loadSession, onSessionChange, session, setVisitorPreview, signIn, signOut, signedIn } from './session.js';
import { linkButton, markSection, promptApiKey, promptOrganizerKey, setPageTitle, toast, toastError } from './ui.js';
import { mountHome } from './views/home.js';
import { mountMatches, mountPlayer, mountPlayers, mountPlays } from './views/lists.js';
import { mountMatch } from './views/match.js';
import { mountPlay } from './views/play.js';
import { mountImport, queueFiles } from './views/import.js';
import { mountChat, mountNight, mountRivalries, mountZoeira } from './views/banter.js';

const brandOpts = { chrome: !isEmbed };
if (isEmbed) document.documentElement.dataset.embed = '1';
applyCachedBrand(brandOpts);
setKeyPrompter(promptApiKey);

const main = document.getElementById('main');
const header = document.getElementById('top');
const footer = document.getElementById('foot');
const embedNav = document.getElementById('embed-nav');

// ------------------------------------------------------------------------------------------- router

function parseRoute() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, qs = ''] = raw.split('?');
  const parts = path.split('/').filter(Boolean).map((p) => {
    try {
      return decodeURIComponent(p);
    } catch {
      return p;
    }
  });
  const query = new URLSearchParams(qs);
  const [head, id, sub, arg] = parts;
  if (!head) return { name: 'home', key: 'home', query };
  if (head === 'plays') {
    // A player's plays have one home: the player page.
    if (query.get('player')) return { name: 'player', key: `player:${query.get('player')}`, id: query.get('player'), query, redirect: `#/player/${encodeURIComponent(query.get('player'))}` };
    return { name: 'plays', key: `plays?${query}`, query };
  }
  if (head === 'play' && id) return { name: 'play', key: `play:${id}`, id, sub, arg, query };
  if (head === 'matches' && !id) return { name: 'matches', key: 'matches', query };
  if ((head === 'match' || head === 'matches') && id) return { name: 'match', key: `match:${id}`, id, sub, arg, query };
  if (head === 'players') return { name: 'players', key: 'players', query };
  if (head === 'player' && id) return { name: 'player', key: `player:${id}`, id, query };
  if (head === 'import') return { name: 'import', key: 'import', query };
  if (head === 'zoeira' || head === 'fails') return { name: 'zoeira', key: `zoeira?${query}`, query };
  if (head === 'chat') return { name: 'chat', key: `chat?${query}`, query };
  if (head === 'rivalidades' || head === 'rivalries') return { name: 'rivals', key: `rivals?${query}`, query };
  if (head === 'night' || head === 'nights' || head === 'noite') return { name: 'night', key: `night:${id || ''}`, id: id || '', query };
  return { name: 'missing', key: 'missing', query };
}

function mountMissing(root) {
  fill(root, el('header', { class: 'page-head' }, el('h1', null, t('missing.title')), el('p', { class: 'lede' }, t('missing.body'))),
    linkButton(t('missing.home'), '#/', { kind: 'primary' }));
}

const VIEWS = {
  home: mountHome,
  plays: mountPlays,
  play: mountPlay,
  matches: mountMatches,
  match: mountMatch,
  players: mountPlayers,
  player: mountPlayer,
  zoeira: mountZoeira,
  chat: mountChat,
  rivals: mountRivalries,
  night: mountNight,
  // Importing is the organizer's job; a visitor who lands on the link gets a plain explanation.
  import(root) {
    if (isOrganizer()) return mountImport(root);
    fill(root, el('header', { class: 'page-head' }, el('h1', null, t('organizer.onlyTitle')), el('p', { class: 'lede' }, t('organizer.onlyBody'))),
      linkButton(t('missing.home'), '#/', { kind: 'primary' }));
    return null;
  },
  missing: mountMissing,
};

const NAV_OF = {
  home: 'home', plays: 'plays', play: 'plays', matches: 'matches', match: 'matches', night: 'matches', players: 'players', player: 'players',
  zoeira: 'zoeira', chat: 'chat', rivals: 'rivals', import: 'import',
};
// The tab says where you are; pages about one thing (a match, a play, a player) set their own title.
const TITLE_OF = {
  plays: 'nav.plays', matches: 'nav.matches', players: 'nav.players', zoeira: 'nav.banter', chat: 'nav.chat', rivals: 'nav.rivals',
  night: 'night.title',
  import: 'nav.import', missing: 'missing.title',
};
let current = null; // {key, view}

const markNav = (name) => markSection(NAV_OF[name]);

function render() {
  const route = parseRoute();
  if (route.redirect) {
    history.replaceState(null, '', route.redirect);
  }
  if (current && current.key === route.key && current.view && typeof current.view.update === 'function') {
    current.view.update(route);
    tellParentRoute();
    return;
  }
  if (current && current.view) {
    const destroy = typeof current.view === 'function' ? current.view : current.view.destroy;
    try {
      if (destroy) destroy();
    } catch (err) {
      console.error(err);
    }
  }
  setPageTitle(TITLE_OF[route.name] ? t(TITLE_OF[route.name]) : '');
  main.dataset.view = route.name;
  fill(main);
  markNav(route.name);
  const view = VIEWS[route.name](main, route);
  const moved = Boolean(current);
  current = { key: route.key, view };
  if (!isEmbed) window.scrollTo(0, 0);
  if (moved) main.focus({ preventScroll: true });
  tellParentRoute();
}

/** Mount the current route again from scratch (the session changed: other actions are on offer). */
function remount() {
  if (current && current.view) {
    const destroy = typeof current.view === 'function' ? current.view : current.view.destroy;
    try {
      if (destroy) destroy();
    } catch (err) {
      console.error(err);
    }
  }
  current = null;
  render();
}

// ------------------------------------------------------------------------------------------- chrome

function builtInMark() {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 32 32');
  svg.setAttribute('class', 'brand-mark');
  svg.setAttribute('aria-hidden', 'true');
  for (const [x, w, cls] of [[3, 12, 'a'], [17, 5, 'b'], [24, 5, 'a']]) {
    const r = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    r.setAttribute('x', String(x));
    r.setAttribute('y', '12');
    r.setAttribute('width', String(w));
    r.setAttribute('height', '8');
    r.setAttribute('class', `mark-${cls}`);
    svg.appendChild(r);
  }
  for (const x of [9, 19]) {
    const r = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    r.setAttribute('x', String(x));
    r.setAttribute('y', '8');
    r.setAttribute('width', '2');
    r.setAttribute('height', '16');
    r.setAttribute('class', 'mark-k');
    svg.appendChild(r);
  }
  return svg;
}

/** The brand name, with the part the kit marks (a word, or "last-word") in the accent colour. */
function brandName() {
  const name = brand.name;
  let accent = brand.nameAccent;
  if (accent === 'last-word') accent = name.includes(' ') ? name.slice(name.lastIndexOf(' ') + 1) : null;
  const at = accent ? name.toLowerCase().lastIndexOf(accent.toLowerCase()) : -1;
  if (at < 0) return el('span', { class: 'brand-name' }, name);
  return el('span', { class: 'brand-name' }, name.slice(0, at), el('span', { class: 'brand-accent' }, name.slice(at, at + accent.length)), name.slice(at + accent.length));
}

function drawHeader() {
  if (isEmbed) {
    // The host page has its own header. Ours shrinks to a row of links at the top of the content.
    embedNav.hidden = false;
    embedNav.setAttribute('aria-label', t('nav.label'));
    fill(embedNav,
      el('a', { href: '#/', dataset: { nav: 'home' } }, t('nav.featured')),
      el('a', { href: '#/plays', dataset: { nav: 'plays' } }, t('nav.plays')),
      el('a', { href: '#/matches', dataset: { nav: 'matches' } }, t('nav.matches')),
      el('a', { href: '#/players', dataset: { nav: 'players' } }, t('nav.players')),
      el('a', { href: '#/zoeira', dataset: { nav: 'zoeira' } }, t('nav.banter')),
      el('a', { href: '#/chat', dataset: { nav: 'chat' } }, t('nav.chat')),
      el('a', { href: '#/rivalidades', dataset: { nav: 'rivals' } }, t('nav.rivals')));
    fill(header);
    return;
  }
  const logo = brand.logo ? el('img', { class: 'brand-logo', src: brand.logo, alt: '', width: '48', height: '48' }) : builtInMark();
  fill(header,
    el('a', { class: 'brand', href: '#/', 'aria-label': t('nav.home', { name: brand.name }) }, logo, brandName()),
    el('nav', { class: 'nav', 'aria-label': t('nav.label') },
      el('a', { href: '#/plays', dataset: { nav: 'plays' } }, t('nav.plays')),
      el('a', { href: '#/matches', dataset: { nav: 'matches' } }, t('nav.matches')),
      el('a', { href: '#/players', dataset: { nav: 'players' } }, t('nav.players')),
      el('a', { href: '#/zoeira', dataset: { nav: 'zoeira' } }, t('nav.banter')),
      el('a', { href: '#/chat', dataset: { nav: 'chat' } }, t('nav.chat')),
      el('a', { href: '#/rivalidades', dataset: { nav: 'rivals' } }, t('nav.rivals'))),
    isOrganizer()
      ? el('a', { class: 'btn btn-quiet nav-import', href: '#/import', dataset: { nav: 'import' } }, icon('upload', 18), el('span', null, t('nav.import')))
      : null);
}

async function askOrganizerKey() {
  const key = await promptOrganizerKey();
  if (!key) return;
  if (await signIn(key)) toast(t('organizer.in'));
  else toastError(t('organizer.wrongKey'));
}

function drawFooter(health) {
  if (isEmbed) {
    fill(footer);
    return;
  }
  const link = (label, onclick) => el('button', { type: 'button', class: 'linklike', onclick }, label);
  // Organizer entry, discreet: visitors do not need it, the organizer knows where it is.
  const organizer = [];
  if (session.keyRequired && !session.admin) organizer.push(link(t('organizer.enter'), askOrganizerKey));
  if (signedIn()) organizer.push(link(t('organizer.leave'), () => signOut().then(() => toast(t('organizer.out')))));
  if (session.admin) {
    organizer.push(link(session.visitorPreview ? t('organizer.backToTools') : t('organizer.preview'), () => setVisitorPreview(!session.visitorPreview)));
  }
  const keyBtn = isAuthRequired() || hasApiKey()
    ? link(t('foot.key'), () => requestApiKey({ force: true }).then((key) => key && location.reload()))
    : null;
  fill(footer,
    el('div', { class: 'foot-brand' },
      el('p', { class: 'foot-name' }, brand.name),
      brand.tagline ? el('p', null, brand.tagline) : null,
      safeUrl(brand.url) ? el('a', { href: safeUrl(brand.url), target: '_blank', rel: 'noopener' }, brand.url.replace(/^https?:\/\//, '').replace(/\/$/, '')) : null),
    el('div', { class: 'foot-meta' },
      el('p', null, t('foot.made'), ' ', el('a', { href: serverUrl('/docs') }, t('foot.api')), health ? ` ${t('foot.version', { v: health.version })}` : ''),
      el('p', null, t('foot.valve')),
      el('p', { class: 'foot-tools' },
        locales.map((code) => el('button', { type: 'button', class: 'linklike', 'aria-pressed': code === locale ? 'true' : 'false', lang: code, onclick: () => setLocale(code) }, code === 'pt-BR' ? 'Português' : 'English')),
        organizer,
        keyBtn,
        apiInfo.overridden ? link(t('foot.resetApi', { base: apiInfo.base }), resetApiBase) : null)));
}

// ------------------------------------------------------------------------------------------- embedding

// Where messages to the parent page go: its own origin when the kit lists the allowed hosts, anyone otherwise
// (the messages carry a height and a route, nothing private).
function parentOrigin() {
  let from = '';
  try {
    from = document.referrer ? new URL(document.referrer).origin : '';
  } catch { /* no referrer */ }
  const allowed = brand.embed.parents;
  if (!allowed.length) return '*';
  return allowed.includes(from) || from === location.origin ? from : null; // our own origin is always trusted
}

function tellParent(message) {
  if (!isEmbed || window.parent === window) return;
  const origin = parentOrigin();
  if (origin) window.parent.postMessage(message, origin);
}

function tellParentRoute() {
  tellParent({ type: 'demoreel:route', hash: location.hash || '#/' });
}

function setupEmbed() {
  // The frame should be exactly as tall as the content, so the host page scrolls and we never do.
  let last = 0;
  const report = () => {
    const height = Math.ceil(document.documentElement.getBoundingClientRect().height);
    if (height === last) return;
    last = height;
    tellParent({ type: 'demoreel:resize', height });
  };
  new ResizeObserver(report).observe(document.documentElement);
  new ResizeObserver(report).observe(document.body);
  setInterval(report, 1000); // a frame that is not being painted gets no observer callbacks; this still reaches it
  report();
  // The host may say how tall its window is (a tall video is sized against it) and send us to a route.
  window.addEventListener('message', (e) => {
    const allowed = brand.embed.parents;
    if (allowed.length && !allowed.includes(e.origin) && e.origin !== location.origin) return;
    const msg = e.data && typeof e.data === 'object' ? e.data : null;
    if (!msg) return;
    if (msg.type === 'demoreel:viewport' && Number.isFinite(msg.height) && msg.height >= 320 && msg.height <= 4000) {
      document.documentElement.style.setProperty('--viewport-h', `${Math.round(msg.height)}px`);
    } else if (msg.type === 'demoreel:navigate' && typeof msg.hash === 'string' && msg.hash.startsWith('#/')) {
      if (location.hash !== msg.hash) location.hash = msg.hash;
    }
  });
}

// ------------------------------------------------------------------------------------------- drop anywhere

function setupDrop() {
  const veil = el('div', { class: 'veil', hidden: true }, icon('upload', 40), el('p', null, t('import.dropAnywhere')));
  document.body.appendChild(veil);
  let depth = 0;
  const hasFiles = (e) => isOrganizer() && e.dataTransfer && [...e.dataTransfer.types].includes('Files');
  window.addEventListener('dragenter', (e) => {
    if (!hasFiles(e)) return;
    depth += 1;
    veil.hidden = false;
  });
  window.addEventListener('dragleave', (e) => {
    if (!hasFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) veil.hidden = true;
  });
  window.addEventListener('dragover', (e) => {
    if (hasFiles(e)) e.preventDefault();
  });
  window.addEventListener('drop', (e) => {
    depth = 0;
    veil.hidden = true;
    if (!hasFiles(e)) return;
    e.preventDefault();
    queueFiles(e.dataTransfer.files);
    if (parseRoute().name !== 'import') location.hash = '#/import';
  });
}

// ------------------------------------------------------------------------------------------- boot

const skip = document.getElementById('skip');
skip.textContent = t('skip');
skip.addEventListener('click', (e) => {
  e.preventDefault(); // a plain "#main" href would be read as a route
  main.focus();
});

async function boot() {
  // Wait briefly for the brand so a themed instance never paints unthemed; never block on it.
  let health = null;
  let painted = false;
  const brandReady = loadBrand(() => data.brand(), brandOpts);
  const sessionReady = loadSession();
  brandReady.then(() => {
    if (!painted) return; // arrived after the timeout: repaint the chrome with the brand
    drawHeader();
    drawFooter(health);
    markNav(parseRoute().name);
  });
  await Promise.race([Promise.all([brandReady, sessionReady]), new Promise((resolve) => setTimeout(resolve, 1200))]);
  drawHeader();
  drawFooter(null);
  painted = true;
  document.body.removeAttribute('data-booting');
  render();
  window.addEventListener('hashchange', render);
  onAuthRequired(() => drawFooter(health));
  // Visitor or organizer changed (signed in, signed out, key refused, preview): other actions are on offer.
  let organizerNow = isOrganizer();
  const sessionChanged = () => {
    drawHeader();
    drawFooter(health);
    if (isOrganizer() !== organizerNow) {
      organizerNow = isOrganizer();
      remount();
    } else markNav(parseRoute().name);
  };
  onSessionChange(sessionChanged);
  sessionReady.then(sessionChanged); // in case the session answered after the first paint
  if (isEmbed) setupEmbed();
  else setupDrop();
  api.health().then((h) => {
    health = h;
    drawFooter(h);
  }).catch(() => {});
}

boot();
