// Small interface pieces shared by the views: buttons, toasts, dialogs, progress, copy to clipboard.

import { el, fill, icon, uid } from './dom.js';
import { t } from './i18n.js';
import { brand } from './brand.js';

/** `button('Salvar', {kind: 'primary', icon: 'check', onclick})`. Kinds: primary | quiet | ghost | danger. */
export function button(label, { kind = 'quiet', icon: iconName, small = false, ...props } = {}) {
  return el('button', { type: 'button', class: `btn btn-${kind}${small ? ' btn-small' : ''}`, ...props },
    iconName ? icon(iconName, small ? 16 : 18) : null, label === null || label === undefined ? null : el('span', null, label));
}

/** A button that only shows an icon; `label` is its accessible name and tooltip. */
export function iconButton(iconName, label, props = {}) {
  return el('button', { type: 'button', class: 'btn btn-icon', 'aria-label': label, title: label, ...props }, icon(iconName, 20));
}

export function linkButton(label, href, { kind = 'quiet', icon: iconName, small = false, ...props } = {}) {
  return el('a', { class: `btn btn-${kind}${small ? ' btn-small' : ''}`, href, ...props },
    iconName ? icon(iconName, small ? 16 : 18) : null, el('span', null, label));
}

/** Loading line for a region that is still fetching. */
export function loading(label) {
  return el('p', { class: 'state state-loading', role: 'status' }, label || t('state.loading'));
}

/** What went wrong and a way to try again. */
export function failure(message, onRetry) {
  return el('div', { class: 'state state-error', role: 'alert' },
    el('p', null, message),
    onRetry ? button(t('action.retry'), { icon: 'refresh', small: true, onclick: onRetry }) : null);
}

export function empty(message, ...actions) {
  return el('div', { class: 'state state-empty' }, el('p', null, message), actions.length ? el('div', { class: 'state-actions' }, actions) : null);
}

/** A labelled form control. */
export function field(labelText, control, hint) {
  const id = control.id || uid('f');
  control.id = id;
  return el('div', { class: 'field' }, el('label', { for: id }, labelText), control, hint ? el('p', { class: 'field-hint' }, hint) : null);
}

/**
 * A progress line in the tape's grammar: `const p = progress(); p.set(0.4, 'Renderizando')`.
 * `set(null, text)` shows an indeterminate state.
 */
export function progress(label) {
  const bar = el('div', { class: 'progress-fill' });
  const text = el('span', { class: 'progress-text' }, label || '');
  const pct = el('span', { class: 'progress-pct' });
  const track = el('div', { class: 'progress-track', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100' }, bar);
  const node = el('div', { class: 'progress' }, el('div', { class: 'progress-head' }, text, pct), track);
  return {
    el: node,
    set(fraction, message) {
      if (message !== undefined && message !== null) text.textContent = message;
      if (fraction === null || fraction === undefined || !Number.isFinite(fraction)) {
        track.removeAttribute('aria-valuenow');
        node.classList.add('is-indeterminate');
        pct.textContent = '';
        return;
      }
      const value = Math.round(Math.min(1, Math.max(0, fraction)) * 100);
      node.classList.remove('is-indeterminate');
      track.setAttribute('aria-valuenow', String(value));
      track.setAttribute('aria-label', text.textContent);
      bar.style.width = `${value}%`;
      pct.textContent = `${value}%`;
    },
  };
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = el('textarea', { class: 'visually-hidden', readOnly: true });
    area.value = text;
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try {
      ok = document.execCommand('copy');
    } catch {
      ok = false;
    }
    area.remove();
    return ok;
  }
}

// ------------------------------------------------------------------------------------------- toasts

export function toast(message, { kind = 'info', timeout } = {}) {
  const host = document.getElementById('toasts');
  if (!host) return;
  const close = iconButton('close', t('action.close'), { onclick: () => node.remove() });
  const node = el('div', { class: `toast toast-${kind}`, role: kind === 'error' ? 'alert' : 'status' }, el('p', null, message), close);
  host.appendChild(node);
  const ms = timeout ?? (kind === 'error' ? 9000 : 4500);
  if (ms > 0) setTimeout(() => node.remove(), ms);
}

export const toastError = (message) => toast(message, { kind: 'error' });

// ------------------------------------------------------------------------------------------- dialogs

/**
 * Modal dialog on the native <dialog> (focus trap and Escape come with it).
 * Returns `{dialog, body, footer, close(value), closed: Promise<value>}`.
 */
export function openDialog({ title, wide = false } = {}) {
  const titleId = uid('dlg');
  const body = el('div', { class: 'dialog-body' });
  const footer = el('div', { class: 'dialog-foot' });
  let done;
  const closed = new Promise((resolve) => {
    done = resolve;
  });
  let result;
  const dialog = el('dialog', { class: `dialog${wide ? ' dialog-wide' : ''}`, 'aria-labelledby': titleId },
    el('div', { class: 'dialog-head' },
      el('h2', { id: titleId }, title || ''),
      iconButton('close', t('action.close'), { onclick: () => dialog.close() })),
    body, footer);
  dialog.addEventListener('close', () => {
    dialog.remove();
    done(result);
  });
  dialog.addEventListener('click', (e) => {
    if (e.target === dialog) dialog.close(); // click on the backdrop
  });
  document.body.appendChild(dialog);
  dialog.showModal();
  return {
    dialog, body, footer, closed,
    close(value) {
      result = value;
      if (dialog.open) dialog.close();
    },
  };
}

export function confirmDialog({ title, message, confirmLabel, danger = false }) {
  const d = openDialog({ title });
  fill(d.body, el('p', null, message));
  const ok = button(confirmLabel || t('action.confirm'), { kind: danger ? 'danger' : 'primary', onclick: () => d.close(true) });
  fill(d.footer, button(t('action.cancel'), { onclick: () => d.close(false) }), ok);
  ok.focus();
  return d.closed.then(Boolean);
}

/** Ask for the server's API key. Resolves to the key or null. */
export function promptApiKey() {
  const d = openDialog({ title: t('key.title') });
  const input = el('input', { type: 'password', autocomplete: 'off', spellcheck: 'false', class: 'input' });
  const form = el('form', { class: 'stack', onsubmit: (e) => {
    e.preventDefault();
    d.close(input.value.trim() || null);
  } }, el('p', null, t('key.body')), field(t('key.label'), input));
  fill(d.body, form);
  fill(d.footer, button(t('action.cancel'), { onclick: () => d.close(null) }),
    button(t('key.save'), { kind: 'primary', onclick: () => form.requestSubmit() }));
  input.focus();
  return d.closed.then((v) => v || null);
}

/** Ask for the organizer's key ("Sou organizador"). Resolves to the key or null. */
export function promptOrganizerKey() {
  const d = openDialog({ title: t('organizer.keyTitle') });
  const input = el('input', { type: 'password', autocomplete: 'off', spellcheck: 'false', class: 'input' });
  const form = el('form', { class: 'stack', onsubmit: (e) => {
    e.preventDefault();
    d.close(input.value.trim() || null);
  } }, el('p', null, t('organizer.keyBody')), field(t('organizer.keyLabel'), input));
  fill(d.body, form);
  fill(d.footer, button(t('action.cancel'), { onclick: () => d.close(null) }),
    button(t('organizer.keySave'), { kind: 'primary', onclick: () => form.requestSubmit() }));
  input.focus();
  return d.closed.then((v) => v || null);
}

/** The tab title: what this page is about, then whose site it is. Left alone when embedded in another page. */
export function setPageTitle(text) {
  if (document.documentElement.dataset.embed) return;
  document.title = text ? `${text} | ${brand.name}` : brand.name;
}

/**
 * Mark which section of the site the page belongs to, in the header and in the embedded tabs (`data-nav`).
 * On a phone the sections are one line that slides sideways: the current one is kept in sight.
 */
export function markSection(key) {
  for (const a of document.querySelectorAll('[data-nav]')) {
    if (a.dataset.nav !== key) {
      a.removeAttribute('aria-current');
      continue;
    }
    a.setAttribute('aria-current', 'page');
    const bar = a.parentElement;
    if (bar && bar.scrollWidth > bar.clientWidth) {
      const left = a.offsetLeft - bar.offsetLeft;
      if (left < bar.scrollLeft || left + a.offsetWidth > bar.scrollLeft + bar.clientWidth) bar.scrollLeft = Math.max(0, left - 24);
    }
  }
}
