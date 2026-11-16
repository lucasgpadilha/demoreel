// Locale, dictionary lookup and formatters. pt-BR is the first language; English is used when the browser
// asks for it. `?lang=en` / `?lang=pt-BR` (or the footer switch) overrides and is remembered.

import ptBR from './locales/pt-BR.js';
import en from './locales/en.js';

const DICTS = { 'pt-BR': ptBR, en };
const LS_LANG = 'demoreel.lang';

function stored() {
  try {
    return localStorage.getItem(LS_LANG);
  } catch {
    return null;
  }
}

function normalize(tag) {
  const s = String(tag || '').toLowerCase();
  if (s.startsWith('pt')) return 'pt-BR';
  if (s.startsWith('en')) return 'en';
  return null;
}

function detect() {
  try {
    const param = new URLSearchParams(location.search).get('lang');
    const forced = normalize(param);
    if (forced) {
      try {
        localStorage.setItem(LS_LANG, forced);
      } catch { /* not persisted */ }
      return forced;
    }
  } catch { /* ignore */ }
  const saved = normalize(stored());
  if (saved) return saved;
  for (const tag of navigator.languages || [navigator.language]) {
    const hit = normalize(tag);
    if (hit) return hit;
  }
  return 'pt-BR';
}

export let locale = detect();
document.documentElement.lang = locale;

export const locales = Object.keys(DICTS);

export function setLocale(next) {
  const value = normalize(next);
  if (!value || value === locale) return;
  try {
    localStorage.setItem(LS_LANG, value);
  } catch { /* not persisted */ }
  const url = new URL(location.href);
  url.searchParams.delete('lang');
  location.href = url.href;
  location.reload();
}

// A brand kit may reword the interface in the vocabulary of the community's own site (`ui.strings`):
// `{"pt-BR": {"count.kills": {"one": "{n} abate", "other": "{n} abates"}}}`. Only known keys, only text.
let overrides = {};

export function setStringOverrides(byLocale) {
  overrides = {};
  const mine = byLocale && typeof byLocale === 'object' ? byLocale[locale] : null;
  if (!mine || typeof mine !== 'object') return;
  for (const [key, value] of Object.entries(mine)) {
    if (DICTS['pt-BR'][key] === undefined) continue;
    const plural = value && typeof value === 'object' && typeof value.one === 'string' && typeof value.other === 'string';
    if (typeof value === 'string' || plural) overrides[key] = value;
  }
}

/** `t('match.rounds', {n: 24})`. Plural entries are `{one, other}` objects selected by `vars.n`. */
export function t(key, vars) {
  let entry = overrides[key];
  if (entry === undefined) entry = DICTS[locale][key];
  if (entry === undefined) entry = DICTS['pt-BR'][key];
  if (entry === undefined) {
    console.warn(`i18n: missing key ${key}`);
    return key;
  }
  if (typeof entry === 'object') {
    const n = vars && typeof vars.n === 'number' ? vars.n : 0;
    entry = n === 1 ? entry.one : entry.other;
  }
  if (!vars) return entry;
  return entry.replace(/\{(\w+)\}/g, (m, name) => (vars[name] !== undefined && vars[name] !== null ? String(vars[name]) : m));
}

export const has = (key) => DICTS[locale][key] !== undefined || DICTS['pt-BR'][key] !== undefined;

// ------------------------------------------------------------------------------------------- formatters

export function parseDate(value) {
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** 83.4 -> "1:23" */
export function clock(seconds) {
  if (!Number.isFinite(seconds)) return '';
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

export function num(value, digits = 0) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '';
  return Number(value).toLocaleString(locale, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export const money = (value) => (Number.isFinite(value) ? `$${num(value)}` : '');

export function bytes(n) {
  if (!Number.isFinite(n)) return '';
  if (n < 1024 * 1024) return `${num(n / 1024)} KB`;
  if (n < 1024 * 1024 * 1024) return `${num(n / 1024 / 1024)} MB`;
  return `${num(n / 1024 / 1024 / 1024, 1)} GB`;
}

/** de_anubis -> "Anubis" */
export function mapName(raw) {
  if (!raw) return t('map.unknown');
  const base = String(raw).replace(/^(de|cs|ar|dz|gd)_/, '').replace(/_/g, ' ');
  return base.charAt(0).toUpperCase() + base.slice(1);
}

const NIGHT_CUTOFF_H = 8; // a match that starts before 08:00 belongs to the night before

/** The night a match belongs to: `{key: '2026-11-13', label: 'Sexta, 13 de novembro'}`. */
export function nightOf(value) {
  const d = parseDate(value);
  if (!d) return null;
  const shifted = new Date(d.getTime() - NIGHT_CUTOFF_H * 3600 * 1000);
  const key = `${shifted.getFullYear()}-${String(shifted.getMonth() + 1).padStart(2, '0')}-${String(shifted.getDate()).padStart(2, '0')}`;
  let weekday = new Intl.DateTimeFormat(locale, { weekday: 'long' }).format(shifted).replace(/-feira$/, '');
  weekday = weekday.charAt(0).toUpperCase() + weekday.slice(1);
  const sameYear = shifted.getFullYear() === new Date().getFullYear();
  const day = new Intl.DateTimeFormat(locale, { day: 'numeric', month: 'long', ...(sameYear ? {} : { year: 'numeric' }) }).format(shifted);
  return { key, label: `${weekday}, ${day}`, short: new Intl.DateTimeFormat(locale, { day: '2-digit', month: '2-digit' }).format(shifted) };
}

export function timeOfDay(value) {
  const d = parseDate(value);
  return d ? new Intl.DateTimeFormat(locale, { hour: '2-digit', minute: '2-digit' }).format(d) : '';
}

export function shortDate(value) {
  const d = parseDate(value);
  return d ? new Intl.DateTimeFormat(locale, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }).format(d) : '';
}

// How facts are written. The defaults are the built-in theme's ("13:3", "Anubis, round 8", nights by name);
// a brand kit can switch to the conventions of the community's site ("13 : 3", " \u00b7 ", "14/11 \u00b7 03:40").
const formats = { scoreSep: ':', metaSep: ', ', date: null };

export function setFormats({ scoreSep, metaSep, date } = {}) {
  formats.scoreSep = scoreSep || ':';
  formats.metaSep = metaSep || ', ';
  formats.date = date || null;
}

/** "13:3", or "13 : 3" where the kit says so. */
export const scoreText = (a, b) => `${a ?? ''}${formats.scoreSep}${b ?? ''}`;
/** The separator alone, for scorelines built from separate elements. */
export const scoreSeparator = () => formats.scoreSep.trim() || ':';
/** Short facts side by side: "Anubis, round 8" or "Anubis \u00b7 round 8". */
export const joinMeta = (...parts) => parts.flat().filter((part) => part !== null && part !== undefined && part !== '').join(formats.metaSep);

/** When a match was played: the night by name and the time, or the kit's own pattern (dd MM yyyy HH mm). */
export function whenText(value) {
  const d = parseDate(value);
  if (!d) return '';
  if (!formats.date) {
    const night = nightOf(value);
    return `${night.label}, ${timeOfDay(value)}`;
  }
  const two = (n) => String(n).padStart(2, '0');
  const parts = { dd: two(d.getDate()), MM: two(d.getMonth() + 1), yyyy: String(d.getFullYear()), HH: two(d.getHours()), mm: two(d.getMinutes()) };
  return formats.date.replace(/dd|MM|yyyy|HH|mm/g, (token) => parts[token]);
}

/** "a, b e c" */
export function list(items) {
  try {
    return new Intl.ListFormat(locale, { style: 'long', type: 'conjunction' }).format(items);
  } catch {
    return items.join(', ');
  }
}
