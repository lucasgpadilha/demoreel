// The organizer's page: pull the night's matches from the community site, or send a .dem by hand.

import { el, fill, icon, safeUrl } from '../dom.js';
import { t, bytes, mapName, nightOf, timeOfDay } from '../i18n.js';
import { api, errorMessage, uploadDemo, watchJob } from '../api.js';
import { data, invalidate } from '../data.js';
import { hrefMatch } from '../plays.js';
import { jobText } from '../render.js';
import { button, empty, failure, field, linkButton, loading, progress, toast, toastError } from '../ui.js';

const ACCEPT = '.dem,.gz,.bz2,.zst,.zip';
const RECENT_NIGHTS = 3;
const DEMO_FILE = /\.(dem|dem\.gz|dem\.bz2|dem\.zst|zip|gz|bz2|zst)$/i;

// Files dropped anywhere on the site wait here until the import page is on screen.
let pendingFiles = [];
let acceptFiles = null;

export function queueFiles(files) {
  const list = [...files];
  if (acceptFiles) acceptFiles(list);
  else pendingFiles.push(...list);
}

export function mountImport(root) {
  let alive = true;
  let pollTimer = 0;
  const unsubs = [];
  const jobsByRemote = new Map(); // `${source}:${remote}` -> latest JobOut
  const expandedSources = new Set();

  const sourcesHost = el('div', null, loading());
  const uploads = el('ol', { class: 'uploads' });
  const topInput = el('input', { class: 'input input-num', type: 'number', min: '0', max: '20', value: '3' });
  const fileInput = el('input', { type: 'file', accept: ACCEPT, multiple: true, class: 'visually-hidden', id: 'demo-file', onchange: () => {
    addFiles(fileInput.files);
    fileInput.value = '';
  } });
  const drop = el('div', { class: 'drop' },
    icon('upload', 28),
    el('p', { class: 'drop-title' }, t('import.dropTitle')),
    el('p', { class: 'note' }, t('import.dropHint')),
    el('label', { class: 'btn btn-quiet', for: 'demo-file' }, t('import.pick')),
    fileInput);
  // Dropping is handled once, for the whole window, in app.js: it calls queueFiles().

  fill(root,
    el('header', { class: 'page-head' }, el('h1', null, t('import.title')), el('p', { class: 'lede' }, t('import.lede'))),
    sourcesHost,
    el('section', { class: 'section' },
      el('div', { class: 'section-head' }, el('h2', null, t('import.uploadTitle'))),
      el('div', { class: 'upload-grid' },
        drop,
        el('div', { class: 'upload-opts' }, field(t('import.autoTop'), topInput, t('import.autoTopHint')))),
      uploads));

  data.capabilities().then((caps) => {
    if (alive && Number.isFinite(caps.auto_render_top)) topInput.value = String(caps.auto_render_top);
  }).catch(() => {});

  // ----------------------------------------------------------------------------------------- sources

  async function loadSources() {
    clearTimeout(pollTimer);
    let sources;
    try {
      sources = await data.sources();
    } catch (err) {
      if (alive) fill(sourcesHost, failure(errorMessage(err), loadSources));
      return;
    }
    if (!alive) return;
    if (!sources.length) {
      fill(sourcesHost, el('section', { class: 'section' }, el('p', { class: 'note' }, t('import.noSources'))));
      return;
    }
    const lists = await Promise.all(sources.map((s) => data.sourceMatches(s.id).then((items) => ({ items }), (err) => ({ error: errorMessage(err) }))));
    if (!alive) return;
    const keepChecked = new Set([...sourcesHost.querySelectorAll('input[type="checkbox"]:checked')].map((i) => i.value));
    fill(sourcesHost, sources.map((s, i) => sourceSection(s, lists[i], keepChecked)));
    const working = lists.some((l) => (l.items || []).some((r) => r.status === 'queued' || r.status === 'analyzing'));
    if (working) pollTimer = setTimeout(loadSources, 4000);
  }

  function sourceSection(source, { items, error }, keepChecked) {
    const site = safeUrl(source.url);
    const head = el('div', { class: 'section-head' },
      el('h2', null, t('import.from', { name: source.name || source.id })),
      site ? el('a', { class: 'more', href: site, target: '_blank', rel: 'noopener' }, t('import.openSite'), icon('external', 14)) : null);
    if (error) return el('section', { class: 'section' }, head, failure(error, loadSources));
    if (!items.length) return el('section', { class: 'section' }, head, empty(t('import.noRemote')));

    const boxes = [];
    const groups = new Map();
    for (const r of items) {
      const night = nightOf(r.played_at);
      const key = night ? night.key : '';
      if (!groups.has(key)) groups.set(key, { label: night ? night.label : t('match.noDate'), key, rows: [] });
      groups.get(key).rows.push(r);
    }
    const ordered = [...groups.values()].sort((a, b) => b.key.localeCompare(a.key));
    const importBtn = button('', { kind: 'primary', icon: 'download', onclick: () => runImport() });
    const sync = () => {
      const n = boxes.filter((b) => b.checked).length;
      importBtn.disabled = n === 0;
      importBtn.lastChild.textContent = n ? t('import.go', { n }) : t('import.goNone');
    };

    async function runImport() {
      const ids = boxes.filter((b) => b.checked).map((b) => b.value.split(':').slice(1).join(':'));
      if (!ids.length) return;
      importBtn.disabled = true;
      try {
        const res = await api.importMatches(source.id, ids);
        (res.jobs || []).forEach((job, i) => {
          const remote = (job.result && job.result.remote_id) || ids[i];
          const key = `${source.id}:${remote}`;
          jobsByRemote.set(key, job);
          const w = watchJob(job, (j) => {
            jobsByRemote.set(key, j);
            const slot = sourcesHost.querySelector(`[data-job="${CSS.escape(key)}"]`);
            if (slot) paintJob(slot, j);
          });
          unsubs.push(w.unsubscribe);
          w.promise.then(() => {
            invalidate('matches');
            invalidate('hl:');
            if (alive) loadSources();
          });
        });
        toast(t('import.started', { n: ids.length }));
        for (const b of boxes) b.checked = false; // the selection has been sent; do not offer it again
        if (alive) loadSources();
      } catch (err) {
        toastError(errorMessage(err));
        sync();
      }
    }

    const nights = ordered.map((g) => el('div', { class: 'night' },
      el('div', { class: 'night-head' },
        el('h3', { class: 'night-title' }, g.label),
        g.rows.some(selectable) ? button(t('import.selectNight'), { small: true, kind: 'ghost', onclick: () => {
          for (const b of boxes) if (b.dataset.night === g.key && !b.disabled) b.checked = true;
          sync();
        } }) : null),
      el('ul', { class: 'imports' }, g.rows.map((r) => remoteRow(source, r, g.key, boxes, keepChecked, sync)))));
    // Older nights stay folded: the organizer comes for the latest one.
    const older = nights.slice(RECENT_NIGHTS);
    const showOlder = older.length && !expandedSources.has(source.id)
      ? button(t('import.older', { n: older.length }), { small: true, onclick: () => {
        expandedSources.add(source.id);
        for (const node of older) node.hidden = false;
        showOlder.remove();
      } })
      : null;
    if (showOlder) for (const node of older) node.hidden = true;
    sync();
    return el('section', { class: 'section import-source' }, head, nights, showOlder, el('div', { class: 'actions import-go' }, importBtn));
  }

  const selectable = (r) => r.has_demo && (!r.status || r.status === 'failed') && !(r.match_id && r.status === 'ready');

  function paintJob(slot, job) {
    if (!slot.line) {
      slot.line = progress('');
      fill(slot, slot.line.el);
    }
    slot.line.set(job.status === 'running' ? job.progress : job.status === 'queued' ? null : 1, jobText(job, 'import'));
  }

  function remoteRow(source, r, nightKey, boxes, keepChecked, sync) {
    const key = `${source.id}:${r.remote_id}`;
    const can = selectable(r);
    const job = jobsByRemote.get(key);
    const box = el('input', { type: 'checkbox', value: key, disabled: !can || Boolean(job && !job.done), dataset: { night: nightKey }, onchange: sync, 'aria-label': `${mapName(r.map)} ${r.score_a}:${r.score_b}` });
    if (can && keepChecked.has(key)) box.checked = true;
    boxes.push(box);
    const page = safeUrl(r.page_url);
    let state;
    if (job && !job.done) {
      state = el('span', { class: 'imp-state', dataset: { job: key } });
      paintJob(state, job);
    } else if (r.status === 'ready' && r.match_id) {
      state = el('span', { class: 'imp-state' }, el('span', { class: 'imp-done' }, icon('check', 14), t('import.imported')), el('a', { href: hrefMatch(r.match_id) }, t('import.open')));
    } else if (r.status === 'queued' || r.status === 'analyzing') {
      state = el('span', { class: 'imp-state is-working' }, t(`match.status.${r.status}`));
    } else if (r.status === 'failed') {
      state = el('span', { class: 'imp-state is-failed' }, t('import.failed'));
    } else if (!r.has_demo) {
      state = el('span', { class: 'imp-state is-off' }, t('import.noDemo'));
    } else {
      state = el('span', { class: 'imp-state' }, t('import.available'));
    }
    const aWon = r.score_a > r.score_b;
    const bWon = r.score_b > r.score_a;
    return el('li', { class: `imp${can ? '' : ' is-off'}` },
      el('label', { class: 'imp-main' },
        box,
        el('span', { class: 'imp-map' }, mapName(r.map)),
        el('span', { class: 'imp-score' },
          el('span', { class: `score${aWon ? ' is-winner' : ''}` }, String(r.score_a ?? '')),
          el('span', { 'aria-hidden': 'true' }, ':'),
          el('span', { class: `score${bWon ? ' is-winner' : ''}` }, String(r.score_b ?? ''))),
        el('span', { class: 'imp-when' }, timeOfDay(r.played_at))),
      state,
      page ? el('a', { class: 'imp-link', href: page, target: '_blank', rel: 'noopener', 'aria-label': t('import.remotePage', { id: r.remote_id }), title: t('import.remotePage', { id: r.remote_id }) }, `#${r.remote_id}`) : el('span', { class: 'imp-link' }, `#${r.remote_id}`));
  }

  // ----------------------------------------------------------------------------------------- uploads

  let chain = Promise.resolve();

  function addFiles(files) {
    for (const file of files) {
      if (!DEMO_FILE.test(file.name)) {
        toastError(t('import.notDemo', { name: file.name }));
        continue;
      }
      const line = progress(t('import.waiting'));
      line.set(null);
      const tail = el('span', { class: 'upload-tail' });
      const row = el('li', { class: 'upload' }, el('div', { class: 'upload-name' }, el('span', null, file.name), el('span', { class: 'note' }, bytes(file.size))), line.el, tail);
      uploads.appendChild(row);
      chain = chain.then(() => (alive ? sendFile(file, line, tail) : null));
    }
  }

  async function sendFile(file, line, tail) {
    const top = Number(topInput.value);
    const up = uploadDemo(file, {
      autoRenderTop: Number.isFinite(top) ? top : undefined,
      onProgress: (loaded, total) => line.set(total ? loaded / total : null, t('import.uploading')),
    });
    const cancel = button(t('action.cancel'), { small: true, kind: 'ghost', onclick: () => up.abort() });
    fill(tail, cancel);
    try {
      const res = await up.promise;
      fill(tail);
      invalidate('matches');
      if (res.job && !res.job.done) {
        const w = watchJob(res.job, (j) => line.set(j.status === 'running' ? j.progress : null, jobText(j, 'analyze')));
        unsubs.push(w.unsubscribe);
        const done = await w.promise;
        if (done.status !== 'succeeded') throw new Error(jobText(done));
      }
      line.set(1, res.created === false ? t('import.duplicate') : t('import.analyzed'));
      invalidate('matches');
      fill(tail, linkButton(t('import.open'), hrefMatch(res.match.id), { small: true, kind: 'primary' }));
    } catch (err) {
      line.set(0, err && err.name === 'AbortError' ? t('err.uploadCanceled') : errorMessage(err));
      line.el.classList.add('is-failed');
      fill(tail);
    }
  }

  acceptFiles = addFiles;
  if (pendingFiles.length) {
    addFiles(pendingFiles);
    pendingFiles = [];
  }
  loadSources();

  return () => {
    alive = false;
    acceptFiles = null;
    clearTimeout(pollTimer);
    for (const fn of unsubs) fn();
  };
}
