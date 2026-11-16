// Pieces for what happens across matches: the nights of the mix, duels between two players, trophies,
// what was said in the chat. All of it is banter between friends, and it is written that way.

import { el, fill } from './dom.js';
import { t, locale, joinMeta, num, scoreSeparator } from './i18n.js';
import { hrefPlayer } from './plays.js';

const en = () => locale === 'en';

// ------------------------------------------------------------------------------------------- nights

/** "2026-11-13" -> {label: "Sexta, 13 de novembro", short: "sex 13/11"}. The date is the night, not the clock. */
export function nightName(date) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(date || ''));
  if (!m) return { label: String(date || ''), short: String(date || '') };
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), 12);
  let weekday = new Intl.DateTimeFormat(locale, { weekday: 'long' }).format(d).replace(/-feira$/, '');
  const day = new Intl.DateTimeFormat(locale, { day: 'numeric', month: 'long' }).format(d);
  const shortDay = new Intl.DateTimeFormat(locale, { day: '2-digit', month: '2-digit' }).format(d);
  const shortWeek = new Intl.DateTimeFormat(locale, { weekday: 'short' }).format(d).replace('.', '');
  weekday = weekday.charAt(0).toUpperCase() + weekday.slice(1);
  return { label: `${weekday}, ${day}`, short: `${shortWeek} ${shortDay}` };
}

/** A night's name in the middle of a sentence: Portuguese writes weekdays and months in lower case, English does not. */
export const midSentence = (label) => (en() ? String(label || '') : String(label || '').toLowerCase());

/**
 * "Temporada" or one night: a row of links, the season first, then the nights, newest first.
 * `hrefFor(date | '')` builds each link; `current` is the chosen date ('' = the whole season).
 */
export function scopeNav(nights, current, hrefFor) {
  const link = (date, label, title) => el('a', { href: hrefFor(date), 'aria-current': date === current ? 'true' : null, title: title || null }, label);
  return el('nav', { class: 'kinds scope', 'aria-label': t('scope.label') },
    link('', t('scope.season')),
    (nights || []).slice(0, 8).map((n) => link(n.date, nightName(n.date).short, nightName(n.date).label)));
}

// ------------------------------------------------------------------------------------------- duels

/**
 * One rivalry as a scoreline between two people: "trovoadinha 55 : 38 biruta", with what is behind it.
 * `pair`: `{a, b, a_kills, b_kills, duels, matches, a_openings, b_openings, a_humiliations, b_humiliations}`.
 */
export function duelRow(pair) {
  const aWon = pair.a_kills > pair.b_kills;
  const bWon = pair.b_kills > pair.a_kills;
  const who = (p, won, side) => el('a', { class: `duel-name duel-${side}${won ? ' is-winner' : ''}`, href: hrefPlayer(p.steamid) }, p.name);
  const knives = (pair.a_humiliations || 0) + (pair.b_humiliations || 0);
  return el('li', { class: 'duel' },
    who(pair.a, aWon, 'a'),
    el('span', { class: 'duel-score', role: 'img', 'aria-label': t('duel.aria', { a: pair.a.name, ka: pair.a_kills, b: pair.b.name, kb: pair.b_kills }) },
      el('span', { class: `score${aWon ? ' is-winner' : ''}`, 'aria-hidden': 'true' }, String(pair.a_kills)),
      el('span', { class: 'scorebug-sep', 'aria-hidden': 'true' }, scoreSeparator()),
      el('span', { class: `score${bWon ? ' is-winner' : ''}`, 'aria-hidden': 'true' }, String(pair.b_kills))),
    who(pair.b, bWon, 'b'),
    el('span', { class: 'duel-facts' }, joinMeta(
      Number.isFinite(pair.matches) ? t('duel.matches', { n: pair.matches }) : null,
      Number.isFinite(pair.a_openings) && Number.isFinite(pair.b_openings) ? t('duel.openings', { a: pair.a_openings, b: pair.b_openings }) : null,
      knives ? t('duel.knives', { n: knives }) : null)));
}

/** A player's nemesis or favourite victim, from his own side: "tonho 27 : 40 Juca Faisca". */
export function duelFromSelf(self, other) {
  return duelRow({ a: self, b: other.player, a_kills: other.kills, b_kills: other.deaths, matches: other.matches });
}

// ------------------------------------------------------------------------------------------- trophies

const trophyValue = (value) => {
  if (!Number.isFinite(value)) return '';
  if (Number.isInteger(value)) return num(value);
  return num(value, Math.abs(value) < 10 ? 2 : 1);
};

/**
 * Trophies of a match or a night, as type: the title, who took it, the number, one line of detail and who
 * came next. The API writes titles and details in pt and en.
 */
export function trophyGrid(trophies) {
  return el('div', { class: 'trophies' }, (trophies || []).filter((x) => x && x.player).map((x) => {
    const next = (x.runners_up || []).filter((r) => r && r.player).slice(0, 2);
    return el('article', { class: 'trophy' },
      el('h3', { class: 'trophy-title' }, en() ? x.title_en || x.title_pt : x.title_pt || x.title_en),
      el('p', { class: 'trophy-what' }, en() ? x.description_en || '' : x.description_pt || ''),
      el('p', { class: 'trophy-who' },
        el('a', { href: hrefPlayer(x.player.steamid) }, x.player.name),
        el('span', { class: 'trophy-value' }, trophyValue(x.value))),
      el('p', { class: 'trophy-detail' }, en() ? x.detail_en || x.detail || '' : x.detail || x.detail_en || ''),
      next.length ? el('p', { class: 'trophy-next' }, t('trophy.next'), ' ',
        next.map((r, i) => [i ? ', ' : null, el('a', { href: hrefPlayer(r.player.steamid) }, r.player.name), ` ${trophyValue(r.value)}`])) : null);
  }));
}

// ------------------------------------------------------------------------------------------- chat

// Crying and swearing read one way, laughing and praise another; plain talk is neutral.
const MOOD = { whine: 'sour', toxic: 'sour', laugh: 'sweet', praise: 'sweet', nice: 'sweet' };

/** The lines of all-chat that reacted to a play: who said what, and from which side of the kill. */
export function chatLines(chat) {
  const lines = (chat || []).filter((c) => c && c.text);
  if (!lines.length) return null;
  return el('ul', { class: 'chatlines' }, lines.map((c) => el('li', { class: `chatline is-${MOOD[c.kind] || 'plain'}` },
    el('span', { class: 'chat-who' },
      c.steamid ? el('a', { href: hrefPlayer(c.steamid) }, c.name || '') : c.name || '',
      c.relation === 'victim' || c.relation === 'killer' ? el('span', { class: 'chat-side' }, t(`chat.${c.relation}`)) : null),
    el('q', { class: 'chat-text' }, c.text))));
}

/**
 * Chat rankings: one podium per habit (foul mouth, cry-baby, chatterbox, giggles...), each with its top
 * three, how many times, and a line or two they actually wrote (the API already masks what needs masking).
 * "Tagarela" counts every message, so it shows the number of messages and quotes nothing.
 */
export function chatPodiums(ranks) {
  const podiums = ((ranks && ranks.podiums) || []).filter((p) => p && (p.entries || []).length);
  if (!podiums.length) return null;
  return el('div', { class: 'podiums' }, podiums.map((p) => {
    const talker = p.key === 'talker';
    return el('section', { class: 'podium' },
      el('h3', null, en() ? p.title_en || p.title_pt : p.title_pt || p.title_en),
      el('ol', { class: 'podium-list' }, p.entries.slice(0, 3).map((e, i) => el('li', null,
        el('span', { class: 'podium-rank', 'aria-hidden': 'true' }, String(i + 1)),
        el('span', { class: 'podium-who' },
          el('a', { href: hrefPlayer(e.player.steamid) }, e.player.name),
          el('span', { class: 'podium-count' }, t(talker ? 'chat.messages' : 'chat.times', { n: e.count }))),
        talker ? null : (e.examples || []).slice(0, i === 0 ? 2 : 1).map((x) => el('q', { class: 'chat-text' }, x.text))))));
  }));
}

// The columns of the "Todo mundo" table after the name, in the order of the podiums. `key` is the field on a
// row of `players`; the last one is not a count but a name (who the player cries about) with its count.
const CHAT_COLUMNS = [
  { key: 'messages', label: 'chat.col.messages' },
  { key: 'toxic', label: 'chat.col.toxic' },
  { key: 'whine', label: 'chat.col.whine' },
  { key: 'death_cries', label: 'chat.col.deathCries' },
  { key: 'laugh', label: 'chat.col.laugh' },
  { key: 'nice', label: 'chat.col.nice' },
  { key: 'shouting', label: 'chat.col.shouting' },
  { key: 'cried_about_count', label: 'chat.col.criedAbout', text: true },
];

/**
 * Everyone who typed, one row each, with every count the podiums are made of. A click on a column header
 * sorts by it (most first; names A to Z), a second click reverses. The table scrolls sideways inside its
 * own box on a narrow screen, with the names held in place.
 */
export function chatTable(players) {
  const rows = (players || []).filter((p) => p && p.player);
  let sortKey = 'messages';
  let dir = -1;
  const host = el('div', { class: 'table-wrap chat-table-wrap', role: 'region', tabindex: '0', 'aria-label': t('chat.tableLabel') });
  const count = (p, key) => Number(p[key]) || 0;
  const byName = (a, b) => String(a.player.name || '').localeCompare(String(b.player.name || ''), locale, { sensitivity: 'base' });
  const compare = (a, b) => (sortKey === 'player' ? byName(a, b) * dir
    : (count(a, sortKey) - count(b, sortKey)) * dir || count(b, 'messages') - count(a, 'messages') || byName(a, b));
  const head = (key, label, numeric) => el('th', { scope: 'col', class: numeric ? 'num' : null, 'aria-sort': sortKey === key ? (dir < 0 ? 'descending' : 'ascending') : null },
    el('button', { type: 'button', class: 'th-sort', title: t('chat.sortBy', { column: t(label) }), onclick: () => {
      if (sortKey === key) dir = -dir;
      else {
        sortKey = key;
        dir = key === 'player' ? 1 : -1;
      }
      draw();
      const again = host.querySelector('th[aria-sort] .th-sort'); // the table was rebuilt: keep the keyboard where it was
      if (again) again.focus();
    } }, t(label)));
  function cell(p, col) {
    if (!col.text) return el('td', { class: `num${count(p, col.key) ? '' : ' is-zero'}` }, num(count(p, col.key)));
    const about = p.cried_about && count(p, col.key) ? p.cried_about : null;
    return el('td', null, about ? [el('a', { href: hrefPlayer(about.steamid) }, about.name), ' ', el('span', { class: 'chat-about-count' }, t('chat.times', { n: count(p, col.key) }))] : null);
  }
  function draw() {
    fill(host, el('table', { class: 'table chat-table' },
      el('thead', null, el('tr', null, head('player', 'chat.col.player', false), CHAT_COLUMNS.map((col) => head(col.key, col.label, !col.text)))),
      el('tbody', null, [...rows].sort(compare).map((p) => el('tr', null,
        el('th', { scope: 'row' }, el('a', { href: hrefPlayer(p.player.steamid) }, p.player.name)),
        CHAT_COLUMNS.map((col) => cell(p, col)))))));
  }
  draw();
  return host;
}
