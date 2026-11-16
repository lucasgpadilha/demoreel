// Asking the server for a video: which renderer, what the server can actually do, and following the job.

import { el, uid } from './dom.js';
import { t } from './i18n.js';
import { watchJob } from './api.js';

/** Job state in the reader's language (the server's own progress messages are English). */
export function jobText(job, kind = 'render') {
  if (!job) return '';
  if (job.status === 'queued') return t('job.queued');
  if (job.status === 'running') return t(`job.running.${kind}`);
  if (job.status === 'succeeded') return t('job.done');
  if (job.status === 'canceled') return t('job.canceled');
  return t('job.failed', { error: job.error || '' });
}

/** Follow a job, mirroring it on a `progress()` line. Resolves with the final job. */
export function followJob(job, line, kind = 'render') {
  return watchJob(job, (j) => {
    line.set(j.status === 'running' ? j.progress : j.status === 'queued' ? null : 1, jobText(j, kind));
  }).promise;
}

function radios(legend, options) {
  const name = uid('choice');
  const inputs = options.map((o) => el('input', { type: 'radio', name, value: o.value, checked: Boolean(o.checked) }));
  return {
    el: el('fieldset', { class: 'choices' }, el('legend', null, legend), options.map((o, i) => el('label', { class: 'choice' }, inputs[i],
      el('span', { class: 'choice-text' }, el('span', { class: 'choice-label' }, o.label), o.hint ? el('span', { class: 'choice-hint' }, o.hint) : null)))),
    value: () => (inputs.find((i) => i.checked) || inputs[0]).value,
  };
}

/**
 * POV or 2D. POV is offered even when /capabilities reports in-game recording as off: the footage of a match
 * is kept on the server, so a POV video often needs no game. The server decides; if it refuses, the caller
 * shows its `detail`. Returns `{el, value()}` with value 'ingame' | 'radar'.
 */
export function rendererField(caps, { legend } = {}) {
  const ingame = ((caps && caps.renderers) || []).find((r) => r.name === 'ingame');
  const recordingOff = ingame && ingame.available === false;
  return radios(legend || t('render.kind'), [
    { value: 'ingame', label: t('video.ingame'), hint: t(recordingOff ? 'render.ingameHintOff' : 'render.ingameHint'), checked: true },
    { value: 'radar', label: t('video.radar'), hint: t('render.radarHint') },
  ]);
}

/** 16:9 or 9:16. Returns `{el, value()}`. */
export function formatField({ legend } = {}) {
  return radios(legend || t('render.format'), [
    { value: '16:9', label: t('format.16x9'), checked: true },
    { value: '9:16', label: t('format.9x16'), hint: t('format.9x16Hint') },
  ]);
}
