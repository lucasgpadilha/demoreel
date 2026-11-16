// The video player. Its scrubber is the play tape: the clip's segments end to end, a mark at every kill.

import { el, fill, icon, reducedMotion, safeUrl } from './dom.js';
import { t, clock } from './i18n.js';
import { renderFormat, videoLabel } from './plays.js';

const LS_MUTED = 'demoreel.muted';

function savedMuted() {
  try {
    return localStorage.getItem(LS_MUTED) === '1';
  } catch {
    return false;
  }
}

/**
 * `createPlayer({autoplay, muted, onEnded, onTime})` -> `{el, video, load, seek, toggle, destroy}`.
 * `load({render, tl})`: `tl` is the play timeline (plays.js) or null when it does not describe this video.
 * `onTime(seconds, duration)` fires while playing and on every seek.
 */
export function createPlayer({ autoplay = false, muted, onEnded, onTime } = {}) {
  let tl = null;
  let raf = 0;
  let destroyed = false;
  let fallbackDuration = 0;
  let userSetSound = false;

  const video = el('video', { playsInline: true, preload: 'metadata', tabindex: '-1' });
  video.muted = muted ?? savedMuted();
  const tag = el('span', { class: 'vtag' });
  const bigPlay = el('button', { type: 'button', class: 'player-big', 'aria-label': t('player.play') }, icon('play', 34));
  const message = el('p', { class: 'player-msg', hidden: true });
  const screen = el('div', { class: 'player-screen' }, video, tag, bigPlay, message);

  const playBtn = el('button', { type: 'button', class: 'btn btn-icon', 'aria-label': t('player.play') }, icon('play', 20));
  const time = el('span', { class: 'player-time' }, '0:00');
  const track = el('div', { class: 'scrub-track' });
  const fillBar = el('span', { class: 'scrub-fill' });
  const head = el('span', { class: 'scrub-head' });
  const scrub = el('div', {
    class: 'scrub', role: 'slider', tabindex: '0', 'aria-label': t('player.seek'), 'aria-valuemin': '0', 'aria-valuemax': '0', 'aria-valuenow': '0',
  }, track);
  const muteBtn = el('button', { type: 'button', class: 'btn btn-icon' });
  const fullBtn = el('button', { type: 'button', class: 'btn btn-icon', 'aria-label': t('player.fullscreen'), title: t('player.fullscreen') }, icon('fullscreen', 20));
  const bar = el('div', { class: 'player-bar' }, playBtn, time, scrub, muteBtn, fullBtn);
  const root = el('div', { class: 'player', dataset: { state: 'paused' } }, screen, bar);

  const duration = () => (Number.isFinite(video.duration) && video.duration > 0 ? video.duration : fallbackDuration);

  function drawTrack() {
    const total = duration() || (tl && tl.duration) || 0;
    const parts = [];
    if (tl && tl.duration > 0) {
      for (const s of tl.segments) {
        parts.push(el('span', { class: `tape-seg${s.replay ? ' is-replay' : ''}`, style: { left: `${(s.start / tl.duration) * 100}%`, width: `${((s.end - s.start) / tl.duration) * 100}%` } }));
      }
    } else {
      parts.push(el('span', { class: 'tape-seg', style: { left: '0', width: '100%' } }));
    }
    parts.push(fillBar);
    if (tl && tl.duration > 0) {
      for (const k of tl.kills) parts.push(el('span', { class: 'tape-kill', style: { left: `${(k.time / tl.duration) * 100}%` } }));
    }
    parts.push(head);
    fill(track, parts);
    scrub.setAttribute('aria-valuemax', String(Math.round(total)));
  }

  function paint() {
    const total = duration();
    const now = Math.min(video.currentTime || 0, total || Infinity);
    const pct = total > 0 ? (now / total) * 100 : 0;
    fillBar.style.width = `${pct}%`;
    head.style.left = `${pct}%`;
    time.textContent = total > 0 ? `${clock(now)} / ${clock(total)}` : clock(now);
    scrub.setAttribute('aria-valuenow', String(Math.round(now)));
    scrub.setAttribute('aria-valuetext', total > 0 ? t('player.position', { now: clock(now), total: clock(total) }) : clock(now));
    if (onTime) onTime(now, total);
  }

  function loop() {
    paint();
    if (!destroyed && !video.paused && !video.ended) raf = requestAnimationFrame(loop);
  }

  function setPlaying(playing) {
    root.dataset.state = playing ? 'playing' : 'paused';
    const label = playing ? t('player.pause') : t('player.play');
    fill(playBtn, icon(playing ? 'pause' : 'play', 20));
    playBtn.setAttribute('aria-label', label);
    playBtn.title = label;
    bigPlay.hidden = playing;
    cancelAnimationFrame(raf);
    if (playing) raf = requestAnimationFrame(loop);
    else paint();
  }

  function setMuteUi() {
    const label = video.muted ? t('player.unmute') : t('player.mute');
    fill(muteBtn, icon(video.muted ? 'muted' : 'volume', 20));
    muteBtn.setAttribute('aria-label', label);
    muteBtn.title = label;
  }

  function toggle() {
    if (video.paused || video.ended) video.play().catch(() => {});
    else video.pause();
  }

  function seek(seconds) {
    const total = duration();
    if (!total) return;
    video.currentTime = Math.min(Math.max(0, seconds), Math.max(0, total - 0.05));
    paint();
  }

  function seekFromPointer(e) {
    const rect = track.getBoundingClientRect();
    if (rect.width <= 0) return;
    seek(((e.clientX - rect.left) / rect.width) * duration());
  }

  video.addEventListener('play', () => setPlaying(true));
  video.addEventListener('pause', () => setPlaying(false));
  video.addEventListener('ended', () => {
    setPlaying(false);
    if (onEnded) onEnded();
  });
  video.addEventListener('loadedmetadata', () => {
    drawTrack();
    paint();
  });
  video.addEventListener('timeupdate', () => {
    if (video.paused) paint();
  });
  video.addEventListener('error', () => {
    if (!video.getAttribute('src')) return;
    message.textContent = t('player.error');
    message.hidden = false;
    bigPlay.hidden = true;
  });
  video.addEventListener('click', toggle);
  bigPlay.addEventListener('click', toggle);
  playBtn.addEventListener('click', toggle);
  muteBtn.addEventListener('click', () => {
    video.muted = !video.muted;
    userSetSound = true;
    try {
      localStorage.setItem(LS_MUTED, video.muted ? '1' : '0');
    } catch { /* not persisted */ }
    setMuteUi();
  });
  fullBtn.addEventListener('click', () => {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else if (root.requestFullscreen) root.requestFullscreen().catch(() => {});
    else if (video.webkitEnterFullscreen) video.webkitEnterFullscreen();
  });

  scrub.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    scrub.setPointerCapture(e.pointerId);
    seekFromPointer(e);
  });
  scrub.addEventListener('pointermove', (e) => {
    if (scrub.hasPointerCapture(e.pointerId)) seekFromPointer(e);
  });
  scrub.addEventListener('keydown', (e) => {
    const step = e.shiftKey ? 5 : 1;
    if (e.key === 'ArrowRight' || e.key === 'ArrowUp') seek(video.currentTime + step);
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') seek(video.currentTime - step);
    else if (e.key === 'Home') seek(0);
    else if (e.key === 'End') seek(duration());
    else return;
    e.preventDefault();
    e.stopPropagation();
  });
  root.addEventListener('keydown', (e) => {
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    const target = e.target;
    const typing = target instanceof HTMLElement && (target.tagName === 'INPUT' || target.tagName === 'SELECT' || target.tagName === 'TEXTAREA');
    if (typing) return;
    const onButton = target instanceof HTMLElement && target.tagName === 'BUTTON';
    if (e.key === 'k' || (e.key === ' ' && !onButton)) toggle();
    else if (e.key === 'm') muteBtn.click();
    else if (e.key === 'f') fullBtn.click();
    else if (e.key === 'ArrowRight') seek(video.currentTime + 2);
    else if (e.key === 'ArrowLeft') seek(video.currentTime - 2);
    else return;
    e.preventDefault();
  });

  setMuteUi();
  drawTrack();

  return {
    el: root,
    video,
    seek,
    toggle,
    /** Jump to a moment and play from there (kill-feed lines use this). */
    playFrom(seconds) {
      seek(seconds);
      video.play().catch(() => {});
    },
    load({ render, timeline: nextTl = null, play = autoplay }) {
      tl = nextTl;
      fallbackDuration = Number.isFinite(render.duration_s) ? render.duration_s : (tl ? tl.duration : 0);
      message.hidden = true;
      bigPlay.hidden = false;
      tag.textContent = videoLabel(render);
      // A vertical video gets a vertical player: the frame takes the video's own shape.
      const format = renderFormat(render);
      root.dataset.format = format;
      screen.style.aspectRatio = format.replace(':', ' / ');
      tag.className = `vtag vtag-${render.renderer === 'ingame' ? 'ingame' : 'radar'}`;
      const poster = safeUrl(render.thumbnail_url);
      if (poster) video.setAttribute('poster', poster);
      else video.removeAttribute('poster');
      const src = safeUrl(render.video_url);
      if (src) video.src = src;
      video.load();
      drawTrack();
      paint();
      if (play && !reducedMotion()) {
        if (autoplay && !userSetSound) video.muted = true; // unprompted playback is only allowed without sound
        setMuteUi();
        video.play().catch(() => {});
      }
    },
    setTimeline(nextTl) {
      tl = nextTl;
      drawTrack();
      paint();
    },
    destroy() {
      destroyed = true;
      cancelAnimationFrame(raf);
      video.pause();
      video.removeAttribute('src');
      video.load();
    },
  };
}
