// HTTP client, upload helper and job watcher.
//
// The API base defaults to the origin that serves this page. For development against another origin,
// open the page with `?api=http://127.0.0.1:8077` (persisted in localStorage; `?api=reset` clears it).
// A server that is not on this computer is used only after the person confirms it.

import { t } from './i18n.js';

const LS_BASE = 'demoreel.apiBase';
const LS_KEY = 'demoreel.apiKey';

function lsGet(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function lsSet(key, value) {
  try {
    if (value == null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* storage unavailable (private mode, blocked): keep working without persistence */
  }
}

function sameOriginBase() {
  try {
    return new URL('.', document.baseURI).href.replace(/\/+$/, '');
  } catch {
    return '';
  }
}

const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]']);

/** True for an API on this computer or on the server that serves this page: a link may point there freely. */
export function isLocalApi(url, pageHost = location.host) {
  return LOCAL_HOSTS.has(url.hostname) || url.hostname.endsWith('.localhost') || url.host === pageHost;
}

function resolveBase() {
  const own = sameOriginBase();
  try {
    const params = new URLSearchParams(location.search);
    if (params.has('api')) {
      const raw = (params.get('api') || '').trim();
      if (!raw || raw === 'reset') {
        lsSet(LS_BASE, null);
      } else {
        const u = new URL(raw);
        // Another server would see everything done here, the organiser's key included: a link from
        // someone else must not switch it silently (the choice is remembered).
        const ok = (u.protocol === 'http:' || u.protocol === 'https:')
          && (isLocalApi(u) || window.confirm(t('api.otherServer', { base: u.origin })));
        if (ok) lsSet(LS_BASE, u.href.replace(/\/+$/, ''));
      }
    }
  } catch {
    /* malformed override, or no dialog allowed (sandboxed frame): ignore */
  }
  const override = lsGet(LS_BASE);
  if (override && override !== own) return { base: override, overridden: true };
  return { base: own, overridden: false };
}

const resolved = resolveBase();

/** Where the API lives and whether that is a development override. */
export const apiInfo = Object.freeze({ base: resolved.base, overridden: resolved.overridden });

export function resetApiBase() {
  lsSet(LS_BASE, null);
  const url = new URL(location.href);
  url.searchParams.delete('api');
  location.href = url.href;
}

/** Absolute URL of a server page that is not part of the JSON API (`/docs`, `/mcp`, ...). */
export function serverUrl(path) {
  return `${apiInfo.base}${path}`;
}

// ------------------------------------------------------------------------------------------- API key

// The key is stored per API base, so a key saved for this server is never sent to an overridden base.
const keyStorageName = apiInfo.overridden ? `${LS_KEY}@${apiInfo.base}` : LS_KEY;
let apiKey = lsGet(keyStorageName) || '';
let keyPrompter = null;
let keyPromptPending = null;
let keyPromptDeclined = false;
let authRequired = false;
const authListeners = new Set();

export function hasApiKey() {
  return Boolean(apiKey);
}

export function isAuthRequired() {
  return authRequired;
}

export function setApiKey(value) {
  apiKey = (value || '').trim();
  lsSet(keyStorageName, apiKey || null);
}

// The organizer's key. Servers with DEMOREEL_ADMIN_KEY let anyone read and only this key change things
// (403 otherwise). Stored per API base, like the API key, and sent as a Bearer token.
const LS_ADMIN = 'demoreel.adminKey';
const adminStorageName = apiInfo.overridden ? `${LS_ADMIN}@${apiInfo.base}` : LS_ADMIN;
let adminKey = lsGet(adminStorageName) || '';
let forbiddenHandler = null;

export const hasAdminKey = () => Boolean(adminKey);

export function setAdminKey(value) {
  adminKey = (value || '').trim();
  lsSet(adminStorageName, adminKey || null);
}

/** Called when the server refuses a change (403): the session is not, or no longer, the organizer's. */
export function onForbidden(fn) {
  forbiddenHandler = fn;
}

function authHeaders(headers) {
  if (adminKey) headers.Authorization = `Bearer ${adminKey}`; // also accepted where an API key is asked for
  else if (apiKey) headers['X-API-Key'] = apiKey;
  return headers;
}

/** The address to share for a play: a real page with a preview (Open Graph) that then opens the app. */
export function shareUrl(highlightId, view = '') {
  const page = `${apiInfo.base}/p/${encodeURIComponent(highlightId)}`;
  return view ? `${page}?view=${encodeURIComponent(view)}` : page; // the variant on screen (e.g. the vertical)
}

/** Register the function that asks the user for the key. It must resolve to a string or null. */
export function setKeyPrompter(fn) {
  keyPrompter = fn;
}

export function onAuthRequired(fn) {
  authListeners.add(fn);
  return () => authListeners.delete(fn);
}

function markAuthRequired() {
  if (authRequired) return;
  authRequired = true;
  for (const fn of authListeners) {
    try {
      fn();
    } catch (err) {
      console.error(err);
    }
  }
}

/**
 * Ask for the API key. Concurrent callers share one prompt; after the user dismisses it we do not
 * ask again until `force` is passed (the header button does that).
 */
export function requestApiKey({ force = false } = {}) {
  if (force) keyPromptDeclined = false;
  if (keyPromptDeclined || !keyPrompter) return Promise.resolve(null);
  if (!keyPromptPending) {
    keyPromptPending = Promise.resolve()
      .then(() => keyPrompter())
      .then((value) => {
        const key = (value || '').trim();
        if (key) setApiKey(key);
        else keyPromptDeclined = true;
        return key || null;
      })
      .catch(() => null)
      .finally(() => {
        keyPromptPending = null;
      });
  }
  return keyPromptPending;
}

// ------------------------------------------------------------------------------------------- requests

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

/** Human readable message for any thrown value. */
export function errorMessage(err) {
  if (!err) return t('err.unknown');
  if (err instanceof ApiError) return err.detail;
  return err.message || String(err);
}

function detailFromBody(text, status, statusText) {
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  const detail = data && typeof data === 'object' ? data.detail : null;
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    // FastAPI validation errors: [{loc: [...], msg: "...", type: "..."}]
    return detail
      .map((d) => {
        if (!d || typeof d !== 'object') return String(d);
        const loc = Array.isArray(d.loc) ? d.loc.filter((x) => x !== 'body' && x !== 'query').join('.') : '';
        return loc ? `${loc}: ${d.msg}` : String(d.msg);
      })
      .join('; ');
  }
  return t('err.http', { status: `${status}${statusText ? ` ${statusText}` : ''}` });
}

function buildUrl(path, query, { root = false } = {}) {
  let url = `${apiInfo.base}${root ? '' : '/api/v1'}${path}`;
  if (query) {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== '') params.set(k, String(v));
    }
    const qs = params.toString();
    if (qs) url += `?${qs}`;
  }
  return url;
}

async function request(method, path, { body, query, signal, root = false, withTotal = false } = {}) {
  const url = buildUrl(path, query, { root });
  for (let attempt = 0; ; attempt += 1) {
    const headers = authHeaders({ Accept: 'application/json' });
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    let res;
    try {
      res = await fetch(url, {
        method,
        headers,
        signal,
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
    } catch (err) {
      if (err && err.name === 'AbortError') throw err;
      throw new ApiError(0, t('err.unreachable'));
    }
    if (res.status === 401) {
      markAuthRequired();
      if (attempt === 0) {
        const key = await requestApiKey();
        if (key) continue;
      } else {
        setApiKey(''); // the key we just tried is wrong: forget it so the next request asks again
      }
    }
    if (res.status === 204) return null;
    const text = await res.text();
    if (res.status === 403) {
      if (forbiddenHandler) forbiddenHandler();
      throw new ApiError(403, t('err.forbidden'));
    }
    if (!res.ok) throw new ApiError(res.status, detailFromBody(text, res.status, res.statusText));
    try {
      const parsed = text ? JSON.parse(text) : null;
      if (!withTotal) return parsed;
      // Lists that page (`limit` / `offset`) say how many items there are in all in X-Total-Count.
      const total = Number(res.headers.get('X-Total-Count'));
      return { items: parsed, total: Number.isFinite(total) && res.headers.has('X-Total-Count') ? total : null };
    } catch {
      throw new ApiError(res.status, t('err.unreadable'));
    }
  }
}

const enc = encodeURIComponent;
let capabilitiesPromise = null;

export const api = {
  health: () => request('GET', '/health', { root: true }),
  capabilities: () => request('GET', '/capabilities'),
  /** `{admin, admin_key_required}`: whether this browser may change things. */
  session: () => request('GET', '/session'),
  openapi: () => request('GET', '/openapi.json', { root: true }),
  /** Capabilities rarely change: share one request between views (a failure is not cached). */
  capabilitiesCached() {
    if (!capabilitiesPromise) {
      capabilitiesPromise = api.capabilities().catch((err) => {
        capabilitiesPromise = null;
        throw err;
      });
    }
    return capabilitiesPromise;
  },

  listMatches: (query) => request('GET', '/matches', { query }),
  getMatch: (id) => request('GET', `/matches/${enc(id)}`),
  deleteMatch: (id) => request('DELETE', `/matches/${enc(id)}`),
  reanalyze: (id) => request('POST', `/matches/${enc(id)}/reanalyze`),
  /** Grade the plays again and pick the featured ones, without reading the demo again. */
  rescore: (id) => request('POST', `/matches/${enc(id)}/rescore`),
  scoreboard: (id) => request('GET', `/matches/${enc(id)}/scoreboard`),
  players: (id) => request('GET', `/matches/${enc(id)}/players`),
  rounds: (id) => request('GET', `/matches/${enc(id)}/rounds`),
  kills: (id, query) => request('GET', `/matches/${enc(id)}/kills`, { query }),

  // Community endpoints. Servers that predate them answer 404; callers go through data.js, which degrades.
  brand: () => request('GET', '/brand'),
  feed: (query) => request('GET', '/feed', { query }),
  /** The feed with its total: `{items, total}` (total is null on servers that do not send it). */
  feedPage: (query) => request('GET', '/feed', { query, withTotal: true }),

  // Across matches (all accept since, until, night=YYYY-MM-DD, match_id).
  nights: () => request('GET', '/insights/nights'),
  rivalries: (query) => request('GET', '/insights/rivalries', { query }),
  playerProfile: (steamid, query) => request('GET', `/insights/players/${enc(steamid)}`, { query }),
  trophies: (query) => request('GET', '/insights/trophies', { query }),
  chatRanks: (query) => request('GET', '/insights/chat', { query }),
  sources: () => request('GET', '/sources'),
  sourceMatches: (id) => request('GET', `/sources/${enc(id)}/matches`),
  importMatches: (id, remoteIds) => request('POST', `/sources/${enc(id)}/import`, { body: { remote_ids: remoteIds } }),

  highlights: (matchId, query) => request('GET', `/matches/${enc(matchId)}/highlights`, { query }),
  createHighlight: (matchId, body) => request('POST', `/matches/${enc(matchId)}/highlights`, { body }),
  getHighlight: (id) => request('GET', `/highlights/${enc(id)}`),
  patchHighlight: (id, body) => request('PATCH', `/highlights/${enc(id)}`, { body }),
  editHighlight: (id, ops) => request('POST', `/highlights/${enc(id)}/edit`, { body: { ops } }),
  deleteHighlight: (id) => request('DELETE', `/highlights/${enc(id)}`),

  renderHighlight: (id, body) => request('POST', `/highlights/${enc(id)}/renders`, { body }),
  createReel: (matchId, body) => request('POST', `/matches/${enc(matchId)}/reels`, { body }),
  /** Record the match once and render every play in the given renderers and formats. */
  produce: (matchId, body) => request('POST', `/matches/${enc(matchId)}/produce`, { body }),
  renders: (matchId) => request('GET', `/matches/${enc(matchId)}/renders`),
  getRender: (id) => request('GET', `/renders/${enc(id)}`),
  deleteRender: (id) => request('DELETE', `/renders/${enc(id)}`),

  jobs: (query) => request('GET', '/jobs', { query }),
  getJob: (id, wait, signal) => request('GET', `/jobs/${enc(id)}`, { query: wait ? { wait } : undefined, signal }),
  cancelJob: (id) => request('POST', `/jobs/${enc(id)}/cancel`),
};

// ------------------------------------------------------------------------------------------- upload

/**
 * Upload a demo with real progress events. Returns `{promise, abort}`; the promise resolves to the
 * `{match, job, created}` payload. An aborted upload rejects with an error named `AbortError`.
 */
export function uploadDemo(file, { autoRenderTop, renderer, onProgress } = {}) {
  let xhr = null;
  let aborted = false;

  const attempt = (mayRetry) =>
    new Promise((resolve, reject) => {
      xhr = new XMLHttpRequest();
      xhr.open('POST', buildUrl('/matches', { auto_render_top: autoRenderTop, renderer }));
      for (const [name, value] of Object.entries(authHeaders({ Accept: 'application/json' }))) xhr.setRequestHeader(name, value);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) onProgress(e.loaded, e.total);
      };
      xhr.onload = () => {
        const { status, statusText, responseText } = xhr;
        if (status === 401) {
          markAuthRequired();
          if (mayRetry) {
            requestApiKey().then((key) => {
              if (key && !aborted) attempt(false).then(resolve, reject);
              else reject(new ApiError(status, detailFromBody(responseText, status, statusText)));
            });
            return;
          }
        }
        if (status === 403) {
          if (forbiddenHandler) forbiddenHandler();
          reject(new ApiError(403, t('err.forbidden')));
          return;
        }
        if (status >= 200 && status < 300) {
          try {
            resolve(JSON.parse(responseText));
          } catch {
            reject(new ApiError(status, t('err.unreadable')));
          }
        } else {
          reject(new ApiError(status, detailFromBody(responseText, status, statusText)));
        }
      };
      xhr.onerror = () => reject(new ApiError(0, t('err.uploadLost')));
      xhr.onabort = () => {
        const err = new Error(t('err.uploadCanceled'));
        err.name = 'AbortError';
        reject(err);
      };
      const form = new FormData();
      form.append('file', file, file.name);
      xhr.send(form);
    });

  return {
    promise: attempt(true),
    abort() {
      aborted = true;
      if (xhr) xhr.abort();
    },
  };
}

// ------------------------------------------------------------------------------------------- jobs

// A watched job gets two update channels:
//   * a long-poll (`?wait=25`) that returns the moment the job finishes (at most MAX_LONG_POLLS at a
//     time, so the browser's per-host connection limit is never exhausted), and
//   * progress snapshots, either pushed by a view that already polls the job list (`ingestJob`) or
//     fetched by a 2 s ticker for jobs nobody else refreshed (the long-poll does not report progress).
const LONG_POLL_SECONDS = 25;
const MAX_LONG_POLLS = 2;
const TICK_MS = 2000;
const watched = new Map();
let longPolls = 0;
let ticker = null;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function notify(entry) {
  for (const fn of [...entry.listeners]) {
    try {
      fn(entry.job);
    } catch (err) {
      console.error(err);
    }
  }
}

/** Feed a fresh job snapshot (from any request) to whoever is watching that job. */
export function ingestJob(job) {
  if (!job || !job.id) return;
  const entry = watched.get(job.id);
  if (!entry) return;
  entry.job = job;
  entry.updatedAt = Date.now();
  notify(entry);
  if (job.done) {
    watched.delete(job.id);
    if (!watched.size && ticker) {
      clearInterval(ticker);
      ticker = null;
    }
    entry.resolve(job);
  }
}

function failWatch(entry, message) {
  ingestJob({ ...entry.job, status: 'failed', done: true, error: message });
}

async function longPoll(entry) {
  entry.longPolling = true;
  longPolls += 1;
  let failures = 0;
  try {
    while (watched.get(entry.job.id) === entry) {
      try {
        ingestJob(await api.getJob(entry.job.id, LONG_POLL_SECONDS));
        failures = 0;
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) {
          failWatch(entry, t('err.jobGone'));
          break;
        }
        failures += 1;
        if (failures >= 8) {
          failWatch(entry, t('err.jobLost', { detail: errorMessage(err) }));
          break;
        }
        await sleep(Math.min(1000 * failures, 5000));
      }
    }
  } finally {
    entry.longPolling = false;
    longPolls -= 1;
    for (const other of watched.values()) {
      if (!other.longPolling && longPolls < MAX_LONG_POLLS) longPoll(other);
    }
  }
}

function tick() {
  const now = Date.now();
  for (const entry of watched.values()) {
    if (entry.fetching || now - entry.updatedAt < TICK_MS - 200) continue;
    entry.fetching = true;
    api
      .getJob(entry.job.id)
      .then(ingestJob, () => {})
      .finally(() => {
        entry.fetching = false;
      });
  }
}

/**
 * Follow a job until it reaches a final state. `job` may be a full JobOut or just `{id}`.
 * `onUpdate` is called with every snapshot (including the final one). Returns
 * `{promise, unsubscribe}`; the promise resolves with the final job (never rejects: a job we
 * lose track of resolves as `failed` with an explanatory `error`).
 */
export function watchJob(job, onUpdate) {
  if (job.done) {
    if (onUpdate) onUpdate(job);
    return { promise: Promise.resolve(job), unsubscribe() {} };
  }
  let entry = watched.get(job.id);
  if (!entry) {
    let resolve;
    const promise = new Promise((r) => {
      resolve = r;
    });
    const known = typeof job.status === 'string';
    entry = {
      job: known ? job : { status: 'queued', progress: 0, message: null, done: false, ...job },
      listeners: new Set(),
      promise,
      resolve,
      updatedAt: known ? Date.now() : 0,
      longPolling: false,
      fetching: false,
    };
    watched.set(job.id, entry);
    if (longPolls < MAX_LONG_POLLS) longPoll(entry);
    if (!ticker) ticker = setInterval(tick, TICK_MS);
    if (!known) tick();
  }
  if (onUpdate) {
    entry.listeners.add(onUpdate);
    onUpdate(entry.job);
  }
  const target = entry;
  return {
    promise: target.promise,
    unsubscribe() {
      if (onUpdate) target.listeners.delete(onUpdate);
    },
  };
}
