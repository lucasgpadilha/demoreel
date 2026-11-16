// Brand kit -> CSS custom properties and a few `data-*` switches on <html>. The stylesheet only knows roles
// (--bg, --accent, --btn-size ...); the values come from GET /api/v1/brand.
//
// Two layers:
//   * `colors`, `fonts`, `name`, `logo_url`: the identity, shared with the videos.
//   * `ui`: how the community's own site looks and speaks (uppercase display type, panels, " · " between
//     facts, "13 : 3" ...). Read only here. Everything in it is optional, validated, and one of a closed
//     set of options or a measure: a kit can never inject CSS. Without `ui` the built-in theme stays exactly
//     as docs/design/frontend-design-plan.md describes it.

import { safeUrl } from './dom.js';
import { setFormats, setStringOverrides } from './i18n.js';

const LS_BRAND = 'demoreel.brand';
const HEX = /^#(?:[0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i;
const FONT_NAME = /^[\w .-]{1,60}$/;
const MEASURE = /^(?:0|\d+(?:\.\d+)?(?:px|rem|em))$/;
const PADDING = /^(?:\d+(?:\.\d+)?(?:px|rem|em)|0)(?: (?:\d+(?:\.\d+)?(?:px|rem|em)|0)){0,3}$/;

const COLOR_VARS = {
  background: '--bg',
  surface: '--surface',
  surface_raised: '--raised',
  border: '--line',
  text: '--text',
  muted: '--muted',
  accent: '--accent',
  accent_strong: '--accent-strong',
  positive: '--positive',
  negative: '--negative',
  ct: '--ct',
  t: '--t',
  // web-only roles (usually given in ui.colors)
  on_accent: '--on-accent',
  surface_sunken: '--sunken',
  border_strong: '--line-strong',
  divider: '--divider',
  text_soft: '--text-soft',
  faint: '--faint-text',
  accent_soft: '--accent-soft',
  team_a: '--team-a',
  team_b: '--team-b',
};
// Colour roles a kit may point at by name (header.nav.color: "text_soft").
const COLOR_REFS = { text: 'var(--text)', muted: 'var(--muted)', text_soft: 'var(--text-soft)', accent: 'var(--accent)' };
// Every custom property and data attribute this module may set, so a kit that drops a key gets the default back.
const UI_VARS = [
  '--display-transform', '--display-tracking', '--display-weight', '--display-weight-strong', '--number-weight',
  '--label-transform', '--label-tracking', '--label-size', '--radius', '--page', '--gutter-wide', '--gutter-narrow',
  '--block-gap', '--panel-pad', '--accent-edge', '--top-border', '--top-min-h', '--top-pad', '--logo-size', '--brand-name-size',
  '--nav-size', '--nav-weight', '--nav-gap', '--nav-color', '--nav-active', '--btn-font', '--btn-weight', '--btn-size',
  '--btn-lh', '--btn-tracking', '--btn-transform', '--btn-pad', '--btn2-weight', '--btn2-size', '--btn2-pad',
  '--row-divider', '--row-hover-bar', '--score-winner',
];
const UI_ATTRS = ['surface', 'btnHover', 'nav', 'motion', 'toast', 'kitGutter', 'btnFont'];

export const brand = {
  slug: null, name: 'demoreel', tagline: null, url: null, logo: null, custom: false,
  nameAccent: null, // the part of the name set in the accent colour ("MIX")
  embed: { shareBase: null, parents: [] },
};

function luminance(hex) {
  let h = hex.slice(1);
  if (h.length === 3) h = [...h].map((c) => c + c).join('');
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** Dark or white text on `hex`, whichever contrasts more (WCAG ratio), so a mid-tone accent never gets 2.7:1. */
function inkOn(hex) {
  const l = luminance(hex);
  const dark = '#14110d';
  return (l + 0.05) / (luminance(dark) + 0.05) >= 1.05 / (l + 0.05) ? dark : '#ffffff';
}

const obj = (v) => (v && typeof v === 'object' && !Array.isArray(v) ? v : {});
const measure = (v) => (typeof v === 'number' && v === 0 ? '0' : typeof v === 'string' && MEASURE.test(v.trim()) ? v.trim() : null);
const weight = (v) => (Number.isInteger(v) && v >= 400 && v <= 900 && v % 100 === 0 ? String(v) : null);
const oneOf = (v, options) => (options.includes(v) ? v : null);

function applyUi(ui, root, html) {
  for (const name of UI_VARS) root.removeProperty(name);
  for (const name of UI_ATTRS) delete html.dataset[name];
  const set = (name, value) => {
    if (value !== null && value !== undefined) root.setProperty(name, value);
  };

  // display type: headings, menu, scores
  const display = obj(ui.display);
  if (display.transform === 'uppercase' || display.uppercase === true) set('--display-transform', 'uppercase');
  set('--display-tracking', measure(display.tracking));
  const strong = weight(display.weight);
  set('--display-weight', strong);
  set('--display-weight-strong', strong);
  set('--number-weight', weight(display.number_weight));

  // small labels: table heads, form labels
  const label = obj(ui.label);
  if (label.transform === 'uppercase' || label.uppercase === true) set('--label-transform', 'uppercase');
  set('--label-tracking', measure(label.tracking));
  set('--label-size', measure(label.size));

  set('--radius', measure(ui.radius));

  const container = obj(ui.container);
  set('--page', measure(container.max_width));
  const wide = measure(container.gutter);
  const narrow = measure(container.gutter_narrow);
  if (wide || narrow) {
    set('--gutter-wide', wide || narrow);
    set('--gutter-narrow', narrow || wide);
    html.dataset.kitGutter = '1';
  }
  set('--block-gap', measure(container.block_gap));

  // surfaces: a flat page (default) or a stack of bordered panels
  const surface = typeof ui.surface === 'string' ? { style: ui.surface } : obj(ui.surface);
  if (surface.style === 'panel' || surface.style === 'panels') html.dataset.surface = 'panel';
  set('--panel-pad', measure(surface.padding));
  set('--accent-edge', measure(surface.accent_edge));

  const header = obj(ui.header);
  if (header.border === true) set('--top-border', '1px');
  set('--top-min-h', measure(header.min_height));
  if (measure(header.min_height)) set('--top-pad', '12px'); // a fixed header height brings its own padding
  set('--logo-size', measure(header.logo_size));
  set('--brand-name-size', measure(header.name_size));
  brand.nameAccent = typeof header.name_accent === 'string' && header.name_accent.length <= 40 ? header.name_accent : null;
  const nav = obj(header.nav);
  set('--nav-size', measure(nav.size));
  set('--nav-weight', weight(nav.weight));
  set('--nav-gap', measure(nav.gap));
  set('--nav-color', COLOR_REFS[nav.color] || null);
  set('--nav-active', COLOR_REFS[nav.active] || null);
  if (nav.indicator === 'grow') html.dataset.nav = 'grow';

  const button = obj(ui.button);
  if (button.font === 'display') {
    set('--btn-font', 'var(--font-display)');
    set('--btn-transform', 'var(--display-transform)');
    html.dataset.btnFont = 'display';
  }
  set('--btn-weight', weight(button.weight));
  set('--btn-size', measure(button.size));
  set('--btn-lh', measure(button.line_height));
  set('--btn-tracking', measure(button.tracking));
  if (typeof button.padding === 'string' && PADDING.test(button.padding.trim())) set('--btn-pad', button.padding.trim());
  if (button.hover === 'lift') html.dataset.btnHover = 'lift';
  const secondary = obj(button.secondary);
  set('--btn2-weight', weight(secondary.weight));
  set('--btn2-size', measure(secondary.size));
  if (typeof secondary.padding === 'string' && PADDING.test(secondary.padding.trim())) set('--btn2-pad', secondary.padding.trim());

  const row = obj(ui.row);
  if (row.divider === true) set('--row-divider', '1px');
  if (row.hover === 'bar') set('--row-hover-bar', measure(row.hover_bar) || '3px');

  const score = obj(ui.score);
  if (score.winner === 'accent') set('--score-winner', 'var(--accent)');

  if (oneOf(ui.motion, ['expressive'])) html.dataset.motion = 'expressive';
  if (oneOf(ui.toast, ['top-right'])) html.dataset.toast = 'top-right';

  // how facts are written
  setFormats({
    scoreSep: typeof score.separator === 'string' && /^ ?[:x×–-] ?$/.test(score.separator) ? score.separator : null,
    metaSep: oneOf(ui.meta_separator, [' · ', ', ', ' | ', ' / ']),
    date: typeof ui.date_format === 'string' && /^[dMyHm/.:\- ·]{1,24}$/.test(ui.date_format) ? ui.date_format : null,
  });
  setStringOverrides(obj(ui.strings));

  const embed = obj(ui.embed);
  brand.embed = {
    shareBase: safeUrl(embed.share_base),
    parents: Array.isArray(embed.parents) ? embed.parents.filter((p) => typeof p === 'string' && /^https?:\/\/[\w.-]+(?::\d+)?$/.test(p)) : [],
  };
}

function apply(kit, { chrome = true } = {}) {
  if (!kit || typeof kit !== 'object') return false;
  const html = document.documentElement;
  const root = html.style;
  const ui = obj(kit.ui);
  // The web may use other tones than the videos (ui.colors) without touching the kit's own palette.
  const colors = { ...obj(kit.colors), ...obj(ui.colors) };
  for (const [name, cssVar] of Object.entries(COLOR_VARS)) {
    const value = colors[name];
    if (typeof value === 'string' && HEX.test(value)) root.setProperty(cssVar, value);
    else root.removeProperty(cssVar);
  }
  const accent = HEX.test(colors.accent || '') ? colors.accent : null;
  if (!HEX.test(colors.on_accent || '')) {
    if (accent) root.setProperty('--on-accent', inkOn(accent));
    else root.removeProperty('--on-accent');
  }

  const fonts = obj(kit.fonts);
  for (const [role, cssVar] of [['display', '--brand-display'], ['body', '--brand-body']]) {
    const name = fonts[role];
    if (typeof name === 'string' && FONT_NAME.test(name)) root.setProperty(cssVar, `"${name}"`);
    else root.removeProperty(cssVar);
  }

  brand.slug = typeof kit.slug === 'string' ? kit.slug : null;
  brand.name = typeof kit.name === 'string' && kit.name.trim() ? kit.name.trim() : 'demoreel';
  brand.tagline = typeof kit.tagline === 'string' ? kit.tagline : null;
  brand.url = safeUrl(kit.url);
  brand.logo = safeUrl(kit.logo_url);
  brand.custom = Boolean(brand.slug || kit.name);
  applyUi(ui, root, html);

  // Embedded in someone else's page, the tab belongs to them: leave title, theme colour and icon alone.
  if (chrome) {
    document.title = brand.name;
    const bg = HEX.test(colors.background || '') ? colors.background : null;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta && bg) meta.setAttribute('content', bg);
    const fav = document.querySelector('link[rel="icon"]');
    if (fav && brand.logo) fav.setAttribute('href', brand.logo);
  }
  return true;
}

/** Paint with the brand remembered from the last visit, so a themed instance never flashes the neutral theme. */
export function applyCachedBrand(opts) {
  try {
    const raw = localStorage.getItem(LS_BRAND);
    if (raw) apply(JSON.parse(raw), opts);
  } catch { /* nothing cached */ }
}

/** `fetchBrand` resolves to the kit, or null / undefined when the server has none. */
export async function loadBrand(fetchBrand, opts) {
  let kit = null;
  try {
    kit = await fetchBrand();
  } catch {
    return brand; // keep whatever is applied; a transient failure must not strip the theme
  }
  if (kit) {
    apply(kit, opts);
    try {
      localStorage.setItem(LS_BRAND, JSON.stringify(kit));
    } catch { /* not persisted */ }
  } else {
    apply({}, opts);
    try {
      localStorage.removeItem(LS_BRAND);
    } catch { /* ignore */ }
  }
  return brand;
}
