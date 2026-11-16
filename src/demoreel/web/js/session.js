// Who is looking: a visitor (watches, shares, downloads) or the organizer (imports, produces videos, edits,
// promotes, deletes). The server decides: with an admin key configured, only requests that carry it may
// change anything, and GET /session says which side this browser is on. The interface follows that answer
// and shows the organizer's actions to the organizer only.

import { api, ApiError, hasAdminKey, onForbidden, setAdminKey } from './api.js';

/** `?embed=1`: the app is inside the community's page. No chrome of its own, never the organizer's tools. */
export const isEmbed = (() => {
  try {
    return new URLSearchParams(location.search).get('embed') === '1';
  } catch {
    return false;
  }
})();

const SS_PREVIEW = 'demoreel.visitorPreview';

function previewStored() {
  try {
    return sessionStorage.getItem(SS_PREVIEW) === '1';
  } catch {
    return false;
  }
}

export const session = {
  admin: false, // the server accepts changes from this browser
  keyRequired: false, // the server has an admin key configured
  visitorPreview: previewStored(), // the organizer is looking at the site the way a visitor sees it
};

const listeners = new Set();
export function onSessionChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
function changed() {
  for (const fn of [...listeners]) fn();
}

/** True when the organizer's actions should be on screen. */
export const isOrganizer = () => session.admin && !session.visitorPreview && !isEmbed;
/** True when this browser signed in with a key (as opposed to a server that has no key at all). */
export const signedIn = () => session.admin && session.keyRequired && hasAdminKey();

export async function loadSession() {
  try {
    const s = await api.session();
    session.admin = Boolean(s && s.admin);
    session.keyRequired = Boolean(s && s.admin_key_required);
  } catch (err) {
    // A server that predates /session has no visitor mode: everyone may change things there.
    const old = err instanceof ApiError && (err.status === 404 || err.status === 405);
    session.admin = old;
    session.keyRequired = false;
  }
  return session;
}

/** Try a key. Resolves true when the server accepts it (it is then kept in this browser). */
export async function signIn(key) {
  setAdminKey(key);
  await loadSession();
  if (!session.admin) setAdminKey('');
  changed();
  return session.admin;
}

export async function signOut() {
  setAdminKey('');
  await loadSession();
  changed();
}

export function setVisitorPreview(on) {
  session.visitorPreview = Boolean(on);
  try {
    if (on) sessionStorage.setItem(SS_PREVIEW, '1');
    else sessionStorage.removeItem(SS_PREVIEW);
  } catch { /* not persisted */ }
  changed();
}

// The server refused a change (403): the key is missing, wrong or was revoked. Fall back to visitor.
onForbidden(() => {
  if (!session.admin) return;
  session.admin = false;
  setAdminKey('');
  changed();
});
