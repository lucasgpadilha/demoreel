// DOM helpers. Everything that comes from the API reaches the page through text nodes and attributes
// created here, never through innerHTML: player names are hostile strings.

const SVG_NS = 'http://www.w3.org/2000/svg';

const PROP_KEYS = new Set([
  'value', 'checked', 'disabled', 'selected', 'hidden', 'open', 'multiple', 'required', 'readOnly',
  'muted', 'controls', 'autoplay', 'loop', 'playsInline', 'inert', 'htmlFor', 'colSpan', 'draggable',
]);

function append(node, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false || child === true) continue;
    if (Array.isArray(child)) append(node, child);
    else if (typeof child === 'string' || typeof child === 'number') node.appendChild(document.createTextNode(String(child)));
    else node.appendChild(child);
  }
}

/**
 * `el('button', {class: 'btn', onclick: fn, 'aria-label': 'Close'}, 'text', child)`.
 * Strings become text nodes; null / false props and children are skipped.
 */
export function el(tag, props, ...children) {
  const node = document.createElement(tag);
  let value;
  if (props) {
    for (const [key, val] of Object.entries(props)) {
      if (val === null || val === undefined || val === false) {
        if (PROP_KEYS.has(key) && val === false) node[key] = false;
        continue;
      }
      if (key === 'class') node.className = val;
      else if (key === 'dataset') {
        for (const [dk, dv] of Object.entries(val)) if (dv !== null && dv !== undefined) node.dataset[dk] = String(dv);
      } else if (key === 'style') {
        for (const [sk, sv] of Object.entries(val)) if (sv !== null && sv !== undefined) node.style.setProperty(sk, String(sv));
      } else if (key === 'value') value = val; // after children, so <select> finds its <option>
      else if (key.startsWith('on') && typeof val === 'function') node.addEventListener(key.slice(2).toLowerCase(), val);
      else if (PROP_KEYS.has(key)) node[key] = val;
      else node.setAttribute(key, val === true ? '' : String(val));
    }
  }
  append(node, children);
  if (value !== undefined) node.value = String(value);
  return node;
}

/** Replace the children of `node`. */
export function fill(node, ...children) {
  node.replaceChildren();
  append(node, children);
  return node;
}

let counter = 0;
export function uid(prefix = 'id') {
  counter += 1;
  return `${prefix}-${counter}`;
}

// 24 x 24, filled. Static strings written here, never data.
const ICONS = {
  play: 'M8 5v14l11-7z',
  pause: 'M6 19h4V5H6v14zm8-14v14h4V5h-4z',
  prev: 'M6 6h2v12H6zm3.5 6 8.5 6V6z',
  next: 'M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z',
  volume: 'M3 9v6h4l5 5V4L7 9H3zm13.5 3A4.5 4.5 0 0 0 14 7.97v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z',
  muted: 'M16.5 12A4.5 4.5 0 0 0 14 7.97v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51A8.8 8.8 0 0 0 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3 3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06a8.99 8.99 0 0 0 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4 9.91 6.09 12 8.18V4z',
  fullscreen: 'M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z',
  link: 'M3.9 12c0-1.71 1.39-3.1 3.1-3.1h4V7H7a5 5 0 0 0 0 10h4v-1.9H7c-1.71 0-3.1-1.39-3.1-3.1zM8 13h8v-2H8v2zm9-6h-4v1.9h4c1.71 0 3.1 1.39 3.1 3.1s-1.39 3.1-3.1 3.1h-4V17h4a5 5 0 0 0 0-10z',
  download: 'M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z',
  upload: 'M9 16h6v-6h4l-7-7-7 7h4zm-4 2h14v2H5z',
  share: 'M18 16.08c-.76 0-1.44.3-1.96.77L8.91 12.7c.05-.23.09-.46.09-.7s-.04-.47-.09-.7l7.05-4.11A2.99 2.99 0 1 0 15 5c0 .24.04.47.09.7L8.04 9.81a3 3 0 1 0 0 4.38l7.12 4.16c-.05.21-.08.43-.08.65a2.92 2.92 0 1 0 2.92-2.92z',
  edit: 'M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z',
  scissors: 'M9.64 7.64c.23-.5.36-1.05.36-1.64a4 4 0 1 0-4 4c.59 0 1.14-.13 1.64-.36L10 12l-2.36 2.36A3.94 3.94 0 0 0 6 14a4 4 0 1 0 4 4c0-.59-.13-1.14-.36-1.64L12 14l7 7h3v-1L9.64 7.64zM6 8a2 2 0 1 1 0-4 2 2 0 0 1 0 4zm0 12a2 2 0 1 1 0-4 2 2 0 0 1 0 4zm6-7.5a.5.5 0 1 1 0-1 .5.5 0 0 1 0 1zM19 3l-6 6 2 2 7-7V3h-3z',
  trash: 'M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z',
  check: 'M9 16.17 4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z',
  close: 'M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z',
  chevron: 'M16.59 8.59 12 13.17 7.41 8.59 6 10l6 6 6-6z',
  back: 'M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z',
  external: 'M19 19H5V5h7V3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7h-2v7zM14 3v2h3.59l-9.83 9.83 1.41 1.41L19 6.41V10h2V3h-7z',
  film: 'M18 4l2 4h-3l-2-4h-2l2 4h-3l-2-4H8l2 4H7L5 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V4h-4z',
  warning: 'M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z',
  refresh: 'M17.65 6.35A7.96 7.96 0 0 0 12 4a8 8 0 1 0 7.73 10h-2.08A6 6 0 1 1 12 6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z',
  plus: 'M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z',
  clock: 'M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20a8 8 0 1 1 0-16 8 8 0 0 1 0 16zm.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67z',
  bomb: 'M10 8a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13zm3.9-.3 1.9-1.9 1.4 1.4-1.9 1.9zM18.5 1.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z',
  defuse: 'M2 11h7.5v2H2zm12.5 0H22v2h-7.5zM9 4.5l1.8-.9 2.2 4.6-1.8.9zM11.2 14.9l1.8.9-2.2 4.6-1.8-.9z',
};

/** Decorative inline icon (hidden from assistive technology; label the control instead). */
export function icon(name, size = 18) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.setAttribute('class', 'icon');
  const path = document.createElementNS(SVG_NS, 'path');
  path.setAttribute('d', ICONS[name] || '');
  svg.appendChild(path);
  return svg;
}

export const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/** True when `url` is safe to put in href/src: http(s) or a relative path. */
export function safeUrl(url) {
  if (typeof url !== 'string' || !url) return null;
  try {
    const u = new URL(url, document.baseURI);
    return u.protocol === 'http:' || u.protocol === 'https:' ? u.href : null;
  } catch {
    return null;
  }
}
