// One play: its videos (POV, POV vertical, 2D replay) on one player with the tape, what happened in the
// players' words, the link and the file to share, and the editor for the cut.

import { el, fill, icon, safeUrl } from '../dom.js';
import { t, clock, joinMeta, list as listWords, mapName, num, scoreText, shortDate, whenText } from '../i18n.js';
import { api, errorMessage, shareUrl } from '../api.js';
import { data, invalidate, loadPlayKills } from '../data.js';
import {
  failFacts, hrefMatch, hrefPlay, hrefPlayer, hasAutoTitle, isBestOfMatch, isFail, isFeatured, isGraded, pendingKey, playLine, playToken,
  scoreReasons, sortRenders, tagWords, tickAt, timeline, variantKey, variantLabel, variantParts, variantsOf, videoTimeline,
} from '../plays.js';
import { grade, playKillFeed, playTape } from '../parts.js';
import { chatLines } from '../insights.js';
import { createPlayer } from '../player.js';
import { followJob, jobText } from '../render.js';
import { isOrganizer } from '../session.js';
import { button, confirmDialog, copyText, failure, field, iconButton, linkButton, loading, progress, setPageTitle, toast, toastError, markSection } from '../ui.js';

const SPEEDS = [0.25, 0.5, 1, 1.5, 2, 4];
// The videos a featured play ("destaque") is meant to have, in the order they are offered. A play that is
// not featured stays as a 2D replay by design, so it is offered the 2D only: no dashed POV tab asking to be
// rendered as if something were missing. Whatever already exists is always shown.
const CANONICAL = ['ingame-16x9', 'ingame-9x16', 'radar-16x9'];
const TWO_D_ONLY = ['radar-16x9'];
const ORDER = ['ingame-16x9', 'ingame-9x16', 'radar-16x9', 'radar-9x16'];
// With no ?view= in the link, the play opens on the first of these it has: POV first, 2D as the fallback.
const OPENING = ['ingame-16x9', 'radar-16x9', 'ingame-9x16', 'radar-9x16'];

const slug = (text) => String(text || '').normalize('NFKD').replace(/[^\w]+/g, '-').replace(/^-+|-+$/g, '').toLowerCase() || 'play';

export function mountPlay(root, route) {
  let alive = true;
  let player = null;
  let h = null;
  let m = null;
  let kills = [];
  let roster = [];
  let currentKey = CANONICAL[0]; // the variant on screen, whether or not it has a video yet
  let currentRender = null;
  let feed = null;
  let busy = false;
  let editing = route.sub === 'edit' && isOrganizer(); // editing a cut is the organizer's
  let rendering = null; // {line, key, jobId} while a render job runs
  let refusal = null; // {key, message}: the server's answer when it would not render a variant
  const id = route.id;

  const crumb = el('nav', { class: 'crumb', 'aria-label': t('play.matchNav') });
  const variantsBar = el('div', { class: 'variants', role: 'group', 'aria-label': t('play.videos') });
  const screen = el('div', { class: 'play-screen' });
  const under = el('div', { class: 'play-under' });
  const info = el('aside', { class: 'play-info' });
  const layout = el('div', { class: 'play' }, screen, info, under);
  const editorHost = el('section', { class: 'editor', hidden: true });
  fill(root, loading());

  const tl = () => timeline(h, m && m.tickrate);
  const syncedTl = (render) => videoTimeline(h, render, m && m.tickrate);
  const groups = () => variantsOf(h.renders);
  // Videos on their way (queued or rendering), by the variant they will be.
  const pending = () => {
    const out = new Map();
    for (const r of h.renders || []) {
      if ((r.status === 'queued' || r.status === 'running') && (!out.has(pendingKey(r)) || r.status === 'running')) out.set(pendingKey(r), r);
    }
    return out;
  };
  // Which variants get a tab although they have no video: the ones the organizer may render. A visitor is
  // only shown what exists or is on its way. (A server without curation sends no `featured` at all: every
  // play is offered the three videos there, as before.)
  const offered = () => {
    if (!isOrganizer()) return [];
    return isFeatured(h) || h.featured === undefined ? CANONICAL : TWO_D_ONLY;
  };
  const openingKey = () => {
    const g = groups();
    const p = pending();
    return OPENING.find((key) => g.has(key)) || [...g.keys()][0] || OPENING.find((key) => p.has(key)) || [...p.keys()][0] || offered()[0] || TWO_D_ONLY[0];
  };

  /** Put a variant on screen. `renderId` picks one exact video of it (an older version, a fresh render). */
  function select(key, renderId) {
    const list = groups().get(key) || [];
    currentKey = key;
    currentRender = list.find((r) => r.id === renderId) || list[0] || null;
  }

  function selectFromQuery(query) {
    const exact = sortRenders(h.renders).find((r) => r.id === query.get('video'));
    if (exact) return select(variantKey(exact), exact.id);
    const view = query.get('view');
    return select(view && (groups().has(view) || offered().includes(view) || pending().has(view)) ? view : openingKey());
  }

  // The link names the variant unless it is the one the play opens on anyway.
  const viewQuery = () => (currentKey === openingKey() ? '' : `?view=${currentKey}`);
  const pageHash = () => `${hrefPlay(h)}${editing ? '/edit' : ''}${viewQuery()}`;

  async function load() {
    try {
      h = await api.getHighlight(id);
      m = await data.match(h.match_id);
      kills = isFail(h) ? [] : await loadPlayKills(h).catch(() => []); // a fail is not a list of kills
      data.scoreboard(h.match_id).then((sb) => {
        roster = (sb.teams || []).flatMap((team) => (team.players || []).map((p) => ({ steamid: p.steamid, name: p.name, team: team.name })));
        if (alive && editing) drawEditor();
      }).catch(() => {});
    } catch (err) {
      if (alive) fill(root, failure(errorMessage(err), load), linkButton(t('nav.plays'), '#/plays', { icon: 'back' }));
      return;
    }
    if (!alive) return;
    fill(root, crumb, variantsBar, layout, editorHost);
    if (isFail(h)) markSection('zoeira'); // a fail lives under Zoeira, not under the plays
    selectFromQuery(route.query);
    drawAll(false);
    resumeRunningRender();
  }

  function drawAll(autoplay) {
    setPageTitle(`${playToken(h)} ${(h.player && h.player.name) || ''}`.trim());
    drawCrumb();
    drawVariants();
    drawScreen(autoplay);
    drawUnder();
    drawInfo();
    drawEditor();
  }

  function drawCrumb() {
    const a = m.team_a || {};
    const b = m.team_b || {};
    fill(crumb,
      el('a', { class: 'crumb-match', href: hrefMatch(m.id) },
        icon('back', 16),
        el('span', { class: 'crumb-map' }, mapName(m.map)),
        el('span', { class: 'crumb-score' }, `${a.name || ''} ${scoreText(a.score, b.score)} ${b.name || ''}`)),
      h.round ? el('a', { href: `${hrefMatch(m.id)}/round/${h.round}` }, t('play.round', { round: h.round })) : null);
  }

  // ----------------------------------------------------------------------------------------- variants

  // One tab per video of the play. The little frame in each tab is the video's shape, so "vertical" is seen
  // before it is read. A variant without a video is still a tab: picking it offers to render it.
  function drawVariants() {
    const g = groups();
    const wanted = offered();
    const waiting = pending();
    const keys = [...ORDER.filter((key) => g.has(key) || wanted.includes(key) || waiting.has(key)), ...[...g.keys()].filter((key) => !ORDER.includes(key))];
    fill(variantsBar, keys.map((key) => {
      const r = (g.get(key) || [])[0];
      const parts = variantParts(key);
      const shape = `${parts.format.replace(':', 'x')} shape-${parts.renderer}`;
      const sub = r
        ? [key === 'ingame-9x16' ? t('variant.ready') : null, clock(r.duration_s), r.stale && isOrganizer() ? t('video.staleShort') : null].filter(Boolean).join(', ')
        : (waiting.has(key) ? t(waiting.get(key).status === 'running' ? 'video.rendering' : 'video.queued') : (rendering && rendering.key === key ? t('variant.rendering') : t('variant.missing')));
      const state = r ? '' : (waiting.has(key) || (rendering && rendering.key === key) ? ' is-missing is-pending' : ' is-missing');
      return el('button', {
        type: 'button', class: `variant${state}`, 'aria-pressed': key === currentKey ? 'true' : 'false', onclick: () => pick(key),
      },
        el('span', { class: `shape shape-${shape}`, 'aria-hidden': 'true' }),
        el('span', { class: 'variant-text' }, el('span', { class: 'variant-name' }, variantLabel(key)), el('span', { class: 'variant-sub' }, sub)));
    }));
  }

  function pick(key, renderId) {
    if (key === currentKey && (!renderId || (currentRender && currentRender.id === renderId))) return;
    select(key, renderId);
    history.replaceState(null, '', pageHash());
    drawVariants();
    drawScreen(Boolean(currentRender));
    drawUnder();
    drawInfo();
    if (editing) drawEditor();
  }

  // ----------------------------------------------------------------------------------------- video

  function drawScreen(autoplay) {
    const format = variantParts(currentKey).format;
    layout.classList.toggle('is-vertical', format === '9:16');
    if (!currentRender) {
      if (player) player.destroy();
      player = null;
      const { renderer } = variantParts(currentKey);
      const waiting = pending().get(currentKey) || (rendering && rendering.key === currentKey ? { status: 'running' } : null);
      if (waiting) {
        // In the queue or rendering: nothing is missing and there is nothing to press. It shows up by itself.
        fill(screen, el('div', { class: 'no-video is-pending', dataset: { format } },
          el('p', { class: 'no-video-title' }, t(waiting.status === 'running' ? 'play.renderingNow' : 'play.inQueue', { variant: variantLabel(currentKey) })),
          el('p', { class: 'note' }, t('play.pendingHint'))));
        return;
      }
      fill(screen, el('div', { class: 'no-video', dataset: { format } },
        el('p', { class: 'no-video-title' }, t('play.noVariant', { variant: variantLabel(currentKey) })),
        isOrganizer() ? el('p', { class: 'note' }, t(renderer === 'ingame' ? 'play.noPovHint' : 'play.no2dHint')) : null,
        isOrganizer() ? button(t('video.makeVariant', { variant: variantLabel(currentKey) }), {
          kind: 'primary', icon: 'film', disabled: Boolean(rendering), onclick: () => startRender(currentKey),
        }) : null));
      return;
    }
    if (!player) {
      player = createPlayer({ onTime: (now) => feed && feed.sync(now) });
      fill(screen, player.el);
    }
    player.load({ render: currentRender, timeline: syncedTl(currentRender), play: autoplay });
  }

  function downloadLink(render, label, kind) {
    const name = `${slug(h.player && h.player.name)}-${slug(playToken(h))}-${slug(mapName(m.map))}-${variantKey(render)}.mp4`;
    return el('a', { class: `btn btn-${kind}`, href: safeUrl(render.video_url), download: name }, icon('download', 18), el('span', null, label));
  }

  function drawUnder() {
    const g = groups();
    const list = g.get(currentKey) || [];
    const newest = list[0] || null;
    const parts = [];

    const organizer = isOrganizer();
    if (rendering && organizer) {
      const jobId = rendering.jobId;
      parts.push(el('div', { class: 'job-row' },
        el('div', { class: 'job-what' }, el('p', { class: 'note' }, t('play.renderingVariant', { variant: variantLabel(rendering.key) })), rendering.line.el),
        jobId ? button(t('action.cancel'), { small: true, kind: 'ghost', onclick: () => api.cancelJob(jobId).catch((err) => toastError(errorMessage(err))) }) : null));
    }
    if (refusal && refusal.key === currentKey && !rendering) {
      parts.push(el('div', { class: 'state state-error', role: 'alert' }, el('p', null, t('play.refused', { variant: variantLabel(refusal.key), detail: refusal.message }))));
    }
    if (currentRender && !rendering) {
      if (newest && currentRender !== newest) {
        parts.push(el('div', { class: 'notice', role: 'status' }, el('p', null, t('play.staleOther')),
          button(t('play.seeNewest'), { small: true, onclick: () => pick(currentKey, newest.id) })));
      } else if (currentRender.stale && organizer) {
        // An older cut still plays fine: only who can render it again needs to know.
        parts.push(el('div', { class: 'notice', role: 'status' }, el('p', null, t('play.stale')),
          organizer ? button(t('play.rerender'), { kind: 'primary', icon: 'refresh', small: true, onclick: () => startRender(currentKey) }) : null));
      }
    }

    // Link and file belong to the variant on screen. The vertical POV exists to be posted, so getting its
    // file is never more than one press away.
    // The link is a real page of the server (preview in chats, then the app), not the address in the bar.
    const pageUrl = shareUrl(h.id, currentKey === openingKey() ? '' : currentKey);
    const copy = (kind) => button(t('action.copyLink'), { kind, icon: 'link', onclick: async () => toast((await copyText(pageUrl)) ? t('action.copied') : t('action.copyFailed')) });
    const vertical = (g.get('ingame-9x16') || []).find((r) => !r.stale) || null;
    const watchingVertical = Boolean(currentRender) && variantParts(currentKey).format === '9:16';
    const actions = el('div', { class: 'actions' },
      watchingVertical ? downloadLink(currentRender, t('action.downloadVertical'), 'primary') : copy('primary'),
      watchingVertical ? copy('quiet') : null,
      currentRender && !watchingVertical ? downloadLink(currentRender, t('action.download'), 'quiet') : null,
      vertical && !watchingVertical ? downloadLink(vertical, t('action.downloadVertical'), 'quiet') : null,
      navigator.share ? button(t('action.share'), { icon: 'share', onclick: () => navigator.share({ title: document.title, url: pageUrl }).catch(() => {}) }) : null,
      !organizer ? null : editing
        ? linkButton(t('edit.close'), `${hrefPlay(h)}${viewQuery()}`, { icon: 'close' })
        : linkButton(t('edit.open'), `${hrefPlay(h)}/edit${viewQuery()}`, { icon: 'edit' }));
    parts.push(actions);

    if (currentRender && organizer) {
      const older = list.filter((r) => r !== currentRender);
      parts.push(el('details', { class: 'more-videos' },
        el('summary', null, t('play.manageVideos')),
        el('div', { class: 'make' },
          el('div', { class: 'actions' },
            button(t('video.remakeVariant', { variant: variantLabel(currentKey) }), { small: true, icon: 'refresh', disabled: Boolean(rendering), onclick: () => startRender(currentKey) }),
            !g.has('radar-9x16') ? button(t('video.makeVariant', { variant: variantLabel('radar-9x16') }), { small: true, icon: 'film', disabled: Boolean(rendering), onclick: () => startRender('radar-9x16') }) : null,
            button(t('video.delete'), { kind: 'danger', icon: 'trash', small: true, onclick: () => removeRender(currentRender) })),
          older.length ? el('div', { class: 'versions' },
            el('p', { class: 'note' }, t('play.otherVersions', { variant: variantLabel(currentKey) })),
            el('ul', null, older.map((r) => el('li', null,
              el('button', { type: 'button', class: 'linklike', onclick: () => pick(currentKey, r.id) },
                `${shortDate(r.created_at)}, ${clock(r.duration_s)}${r.stale ? `, ${t('video.staleShort')}` : ''}`),
              iconButton('trash', t('video.delete'), { onclick: () => removeRender(r) }))))) : null)));
    }
    fill(under, parts);
  }

  async function startRender(key) {
    if (rendering) return;
    const { renderer, format } = variantParts(key);
    const line = progress(t('job.queued'));
    rendering = { line, key };
    refusal = null;
    drawVariants();
    drawScreen(false);
    drawUnder();
    try {
      // POV is asked for even when the server reports recording as off: footage of the match may already be
      // stored, and only the server knows. If it cannot, its own explanation is shown.
      const res = await api.renderHighlight(id, { renderer, options: { format } });
      rendering.jobId = res.job.id;
      if (alive) drawUnder();
      await finishRender(res.job, res.render.id);
    } catch (err) {
      rendering = null;
      refusal = { key, message: errorMessage(err) };
      if (alive) {
        drawVariants();
        drawScreen(false);
        drawUnder();
      }
    }
  }

  async function finishRender(job, renderId) {
    const key = rendering.key;
    const done = await followJob(job, rendering.line);
    rendering = null;
    invalidate(`hl:${h.match_id}`);
    invalidate(`renders:${h.match_id}`);
    if (!alive) return;
    if (done.status === 'canceled') toast(jobText(done));
    else if (done.status !== 'succeeded') refusal = { key, message: jobText(done) };
    h = await api.getHighlight(id);
    if (!alive) return;
    const made = sortRenders(h.renders).find((r) => r.id === renderId);
    if (made) select(variantKey(made), made.id);
    else select(currentKey, currentRender && currentRender.id);
    if (done.status === 'succeeded') toast(t('video.ready'));
    history.replaceState(null, '', pageHash());
    drawAll(false);
  }

  function resumeRunningRender() {
    const running = (h.renders || []).find((r) => (r.status === 'queued' || r.status === 'running') && r.job_id);
    if (!running || rendering) return;
    const format = running.format || (running.options && running.options.format) || '16:9';
    const key = `${running.renderer === 'ingame' ? 'ingame' : 'radar'}-${String(format).replace(':', 'x')}`;
    rendering = { line: progress(t('job.queued')), jobId: running.job_id, key };
    rendering.line.set(null);
    drawVariants();
    drawScreen(false);
    drawUnder();
    finishRender({ id: running.job_id }, running.id).catch(() => {
      rendering = null;
    });
  }

  async function removeRender(r) {
    if (!(await confirmDialog({ title: t('video.delete'), message: t('video.deleteConfirm'), confirmLabel: t('video.delete'), danger: true }))) return;
    try {
      await api.deleteRender(r.id);
      invalidate(`hl:${h.match_id}`);
      invalidate(`renders:${h.match_id}`);
      h = await api.getHighlight(id);
      const keep = currentRender && currentRender.id !== r.id ? currentRender.id : null;
      if (groups().has(currentKey)) select(currentKey, keep);
      else select(openingKey());
      history.replaceState(null, '', pageHash());
      if (alive) drawAll(false);
    } catch (err) {
      toastError(errorMessage(err));
    }
  }

  // ----------------------------------------------------------------------------------------- what happened

  function drawInfo() {
    const name = (h.player && h.player.name) || '';
    const weapons = [...new Set(kills.map((k) => k.weapon_name).filter(Boolean))];
    const side = kills[0] && kills[0].attacker ? kills[0].attacker.side : null;
    // Map, score and round are in the trail above the video; here goes only what is new.
    const sideText = side ? t(side === 'CT' ? 'play.asCt' : 'play.asT') : null;
    const context = joinMeta(sideText ? sideText.charAt(0).toUpperCase() + sideText.slice(1) : null, m.played_at ? whenText(m.played_at) : null);
    const line = syncedTl(currentRender);
    feed = kills.length ? playKillFeed(kills, line, player ? (at) => player.playFrom(at) : null) : null;
    const words = tagWords(h);
    const custom = !hasAutoTitle(h);
    const reasons = scoreReasons(h);
    // What the others typed right after: the kill and the crying about it belong together.
    const chat = chatLines(h.chat);
    const chatBlock = chat ? el('div', { class: 'play-chat' }, el('h2', null, t('chat.title')), chat) : null;
    if (isFail(h)) {
      // A fail gets its own telling: who, what happened, the detail that makes it funny. No score to explain.
      const f = failFacts(h);
      const detail = h.kind === 'fail_whiff' && f.meters ? t('fail.detailMeters', { m: num(f.meters) })
        : h.kind === 'fail_team_flash' && f.mate ? t(f.seconds ? 'fail.detailMateBlind' : 'fail.detailMatePov', { mate: f.mate, s: num(f.seconds || 0, 1) })
          : h.kind === 'fail_self_flash' && f.seconds ? t('fail.detailSelfBlind', { s: num(f.seconds, 1) }) : null;
      fill(info,
        el('p', { class: 'token token-xl' }, playToken(h)),
        el('h1', { class: 'play-name' }, h.player && h.player.steamid ? el('a', { href: hrefPlayer(h.player.steamid) }, name) : name || h.title),
        el('p', { class: 'play-line' }, playLine(h)),
        detail ? el('p', { class: 'play-detail' }, detail) : null,
        m.played_at ? el('p', { class: 'play-context' }, whenText(m.played_at)) : null,
        chatBlock,
        failCuration());
      return;
    }
    fill(info,
      el('p', { class: 'token token-xl' }, playToken(h)),
      el('h1', { class: 'play-name' }, h.player && h.player.steamid ? el('a', { href: hrefPlayer(h.player.steamid) }, name) : name || h.title),
      el('p', { class: 'play-line' }, playLine(h, weapons)),
      context ? el('p', { class: 'play-context' }, context) : null,
      feed ? feed.el : null,
      chatBlock,
      // The reasons already say the tags in context; the bare tag sentence is for plays without a breakdown.
      words.length && !reasons.length ? el('p', { class: 'play-tags' }, capitalize(listWords(words))) : null,
      why(reasons),
      custom && h.description ? el('p', { class: 'note' }, h.description) : null,
      el('p', { class: 'note' }, h.version > 1 ? t('play.originEdited', { v: h.version }) : t(h.origin === 'auto' ? 'play.originAuto' : h.origin === 'agent' ? 'play.originAgent' : 'play.originManual')));
  }

  // Fails have no "why this score". The organizer still decides which ones get a POV video.
  function failCuration() {
    if (!isOrganizer() || h.featured === undefined) return null;
    const toggle = button(isFeatured(h) ? t('curation.demote') : t('curation.promote'), {
      small: true, kind: isFeatured(h) ? 'ghost' : 'quiet', onclick: () => setFeatured(!isFeatured(h), toggle),
    });
    return el('div', { class: 'why why-curation' },
      el('p', null, t(isFeatured(h) ? 'fail.isFeatured' : 'fail.notFeatured')),
      el('div', { class: 'actions' }, toggle));
  }

  // "Por que esta nota": the grade, the reasons in the words players use, and whether the play is one of the
  // featured ones (which decides if it gets POV videos), with the one action that changes that.
  function why(reasons) {
    const team = h.team && m[h.team] ? m[h.team].name : null;
    let status;
    if (isBestOfMatch(h)) status = t('curation.isBest');
    else if (isFeatured(h)) status = h.featured_rank ? t(team ? 'curation.isRankedTeam' : 'curation.isRanked', { n: h.featured_rank, team }) : t('curation.isManual');
    else status = t([...groups().keys()].some((key) => key.startsWith('ingame-')) ? 'curation.notFeaturedHasPov' : 'curation.notFeatured');
    const toggle = button(isFeatured(h) ? t('curation.demote') : t('curation.promote'), {
      small: true, kind: isFeatured(h) ? 'ghost' : 'quiet', onclick: () => setFeatured(!isFeatured(h), toggle),
    });
    // Open where there is room beside the video; folded on a phone, where the video and its buttons come first.
    const open = window.matchMedia('(min-width: 1001px)').matches;
    return el('details', { class: 'why', open },
      el('summary', null, el('span', null, t('why.title')), grade(h)),
      isGraded(h)
        ? el('p', { class: 'why-grade' }, grade(h, { big: true }), el('span', null, t(`grade.${h.tier}`)))
        : el('p', { class: 'note' }, t('why.none')),
      reasons.length ? el('ul', { class: 'why-list' }, reasons.map((r) => el('li', { class: `why-${r.dir}` },
        el('span', { class: 'why-sign', 'aria-hidden': 'true' }, r.dir === 'up' ? '+' : '\u2212'),
        el('span', { class: 'visually-hidden' }, t(r.dir === 'up' ? 'why.up' : 'why.down')),
        el('span', null, r.text)))) : null,
      // Only servers with curation can change it.
      h.featured === undefined ? null : el('div', { class: 'why-curation' },
        el('p', null, status, ' ', isFeatured(h) ? t('curation.featuredRule') : null),
        isOrganizer() ? el('div', { class: 'actions' }, toggle) : null,
        isOrganizer() ? el('p', { class: 'note' }, t(isFeatured(h) ? 'curation.demoteHint' : 'curation.promoteHint')) : null));
  }

  async function setFeatured(value, control) {
    control.disabled = true;
    try {
      h = await api.patchHighlight(id, { featured: value });
      invalidate(`hl:${h.match_id}`);
      invalidate('matches');
      toast(t(value ? 'curation.promoted' : 'curation.removed'));
      // The tabs on offer depend on it: a play that left the featured ones no longer asks for a POV.
      if (groups().has(currentKey) || offered().includes(currentKey)) select(currentKey, currentRender && currentRender.id);
      else select(openingKey());
      history.replaceState(null, '', pageHash());
      if (alive) drawAll(false);
    } catch (err) {
      toastError(errorMessage(err));
      control.disabled = false;
    }
  }

  // ----------------------------------------------------------------------------------------- editor

  async function applyOps(ops, okMessage) {
    if (busy) return;
    busy = true;
    editorHost.setAttribute('aria-busy', 'true');
    try {
      h = await api.editHighlight(id, ops);
      invalidate(`hl:${h.match_id}`);
      select(currentKey, currentRender && currentRender.id);
      kills = isFail(h) ? [] : await loadPlayKills(h).catch(() => kills);
      if (okMessage) toast(okMessage);
    } catch (err) {
      toastError(errorMessage(err));
    } finally {
      busy = false;
      editorHost.removeAttribute('aria-busy');
      if (alive) {
        if (player && currentRender) player.setTimeline(syncedTl(currentRender));
        drawVariants();
        drawUnder();
        drawInfo();
        drawEditor();
      }
    }
  }

  function drawEditor() {
    editorHost.hidden = !editing;
    if (!editing) {
      fill(editorHost);
      return;
    }
    const line = tl();
    const rate = m.tickrate || 64;
    const title = el('input', { class: 'input', type: 'text', value: h.title, maxlength: '140' });
    const saveTitle = button(t('edit.saveTitle'), { small: true, onclick: () => {
      const value = title.value.trim();
      if (value && value !== h.title) applyOps([{ op: 'set_meta', title: value }], t('edit.saved'));
    } });

    const nudge = (index, which, delta, label, aria) => button(label, {
      small: true, kind: 'ghost', 'aria-label': aria, title: aria,
      onclick: () => applyOps([{ op: 'trim', segment: index, start_delta_s: which === 'start' ? delta : 0, end_delta_s: which === 'end' ? delta : 0 }]),
    });

    const segs = line.segments.map((s) => {
      const i = s.index;
      const seg = s.seg;
      const killsHere = line.kills.filter((k) => k.segment === i).length;
      const speed = el('select', { class: 'input', onchange: () => applyOps([{ op: 'set_speed', segment: i, speed: Number(speed.value) }]) },
        [...new Set([...SPEEDS, seg.speed || 1])].sort((x, y) => x - y).map((v) => el('option', { value: String(v) }, `${num(v, v % 1 ? 2 : 0).replace(/[.,]?0+$/, '') || '0'}×`)));
      speed.value = String(seg.speed || 1);
      const pov = el('select', { class: 'input', onchange: () => applyOps([{ op: 'set_pov', segment: i, steamid: pov.value || null }]) },
        el('option', { value: '' }, t('edit.freeCam')),
        roster.map((p) => el('option', { value: p.steamid }, `${p.name} (${p.team})`)));
      if (seg.pov_steamid && !roster.some((p) => p.steamid === seg.pov_steamid)) pov.appendChild(el('option', { value: seg.pov_steamid }, seg.pov_steamid));
      pov.value = seg.pov_steamid || '';
      const caption = el('input', { class: 'input', type: 'text', value: seg.caption || '', maxlength: '120', placeholder: t('edit.captionHint'),
        onchange: () => applyOps([{ op: 'set_caption', segment: i, caption: caption.value.trim() || null }]) });

      // "Split here" needs the playhead inside this segment of a video that still matches the cut.
      const synced = syncedTl(currentRender);
      const inVideo = synced ? synced.segments[i] : null;
      const tickHere = () => {
        const at = player && inVideo ? player.video.currentTime : null;
        return at !== null && at > inVideo.start + 0.3 && at < inVideo.end - 0.3 ? tickAt(synced, at, rate) : null;
      };
      const now = player ? player.video.currentTime : 0;
      const here = tickHere();
      const middle = Math.round((seg.start_tick + seg.end_tick) / 2);
      const canSplit = seg.end_tick - seg.start_tick >= rate;
      const split = button(here ? t('edit.splitHere', { time: clock(now) }) : t('edit.splitMiddle'), {
        small: true, icon: 'scissors', disabled: !canSplit,
        onclick: () => applyOps([{ op: 'split_segment', segment: i, at_tick: tickHere() || middle }]),
      });

      return el('li', { class: 'seg' },
        el('div', { class: 'seg-head' },
          // A replay is the same kill again from the victim's eyes, slowed down: named as such, not as one
          // more stretch of the play, and its kills are not counted a second time.
          el('h3', null, s.replay ? t('edit.replay', { name: replayName(seg) }) : t('edit.segment', { n: i + 1 })),
          el('p', { class: 'seg-span' }, t('edit.span', { from: clock(s.start), to: clock(s.end), len: num(s.end - s.start, 1) }), killsHere && !s.replay ? `, ${t('count.kills', { n: killsHere })}` : ''),
          el('div', { class: 'seg-tools' },
            split,
            iconButton('chevron', t('edit.moveUp'), { class: 'btn btn-icon flip', disabled: i === 0, onclick: () => applyOps([{ op: 'move_segment', segment: i, to: i - 1 }]) }),
            iconButton('chevron', t('edit.moveDown'), { disabled: i === line.segments.length - 1, onclick: () => applyOps([{ op: 'move_segment', segment: i, to: i + 1 }]) }),
            iconButton('trash', t('edit.remove'), { disabled: line.segments.length <= 1, onclick: () => applyOps([{ op: 'remove_segment', segment: i }]) }))),
        el('div', { class: 'seg-grid' },
          el('div', { class: 'trim', role: 'group', 'aria-label': t('edit.start') },
            el('span', { class: 'trim-label' }, t('edit.start')),
            nudge(i, 'start', -1, '−1 s', t('edit.startEarlier', { s: '1' })),
            nudge(i, 'start', -0.25, '−¼', t('edit.startEarlier', { s: num(0.25, 2) })),
            nudge(i, 'start', 0.25, '+¼', t('edit.startLater', { s: num(0.25, 2) })),
            nudge(i, 'start', 1, '+1 s', t('edit.startLater', { s: '1' }))),
          el('div', { class: 'trim', role: 'group', 'aria-label': t('edit.end') },
            el('span', { class: 'trim-label' }, t('edit.end')),
            nudge(i, 'end', -1, '−1 s', t('edit.endEarlier', { s: '1' })),
            nudge(i, 'end', -0.25, '−¼', t('edit.endEarlier', { s: num(0.25, 2) })),
            nudge(i, 'end', 0.25, '+¼', t('edit.endLater', { s: num(0.25, 2) })),
            nudge(i, 'end', 1, '+1 s', t('edit.endLater', { s: '1' }))),
          field(t('edit.speed'), speed),
          field(t('edit.pov'), pov),
          field(t('edit.caption'), caption)));
    });

    const stale = currentRender && currentRender.stale;
    fill(editorHost,
      el('div', { class: 'section-head' }, el('h2', null, t('edit.title')), linkButton(t('edit.close'), `${hrefPlay(h)}${viewQuery()}`, { small: true, icon: 'close' })),
      el('p', { class: 'note' }, t('edit.lede')),
      el('div', { class: 'tape-lg-wrap' }, largeTape(line), el('p', { class: 'note' }, t('edit.total', { len: num(line.duration, 1), n: line.segments.length }))),
      el('ol', { class: 'segs' }, segs),
      el('div', { class: 'edit-title' }, field(t('edit.titleField'), title, t('edit.titleHint')), saveTitle),
      el('div', { class: 'actions edit-foot' },
        stale || !currentRender ? button(currentRender ? t('play.rerender') : t('video.makeVariant', { variant: variantLabel(currentKey) }), { kind: 'primary', icon: 'refresh', disabled: Boolean(rendering), onclick: () => startRender(currentKey) }) : null,
        button(t('edit.delete'), { kind: 'danger', icon: 'trash', onclick: removePlay })));
  }

  /** Whose eyes a replay segment shows: the roster knows the name; the caption ("Replay · name") is the fallback. */
  function replayName(seg) {
    const known = roster.find((p) => p.steamid === seg.pov_steamid);
    if (known) return known.name;
    const m2 = /^Replay\s*\S\s*(.+)$/.exec(seg.caption || '');
    return m2 ? m2[1] : t('edit.replayVictim');
  }

  function largeTape(line) {
    const tape = playTape(h, m.tickrate);
    tape.classList.add('tape-lg');
    const labels = el('div', { class: 'tape-labels', 'aria-hidden': 'true' }, line.duration
      ? line.segments.map((s) => el('span', { style: { left: `${(s.start / line.duration) * 100}%` } }, String(s.index + 1)))
      : null);
    return el('div', { class: 'tape-lg-box' }, labels, tape);
  }

  async function removePlay() {
    if (!(await confirmDialog({ title: t('edit.delete'), message: t('edit.deleteConfirm'), confirmLabel: t('edit.delete'), danger: true }))) return;
    try {
      await api.deleteHighlight(id);
      invalidate(`hl:${h.match_id}`);
      invalidate('matches');
      toast(t('edit.deleted'));
      location.hash = hrefMatch(h.match_id);
    } catch (err) {
      toastError(errorMessage(err));
    }
  }

  load();
  return {
    destroy() {
      alive = false;
      if (player) player.destroy();
    },
    update(next) {
      const wasEditing = editing;
      editing = next.sub === 'edit' && isOrganizer();
      if (!h) return;
      const beforeKey = currentKey;
      const beforeRender = currentRender;
      selectFromQuery(next.query);
      if (currentKey !== beforeKey || currentRender !== beforeRender) {
        drawVariants();
        drawScreen(false);
        drawInfo();
      }
      drawUnder();
      drawEditor();
      if (editing && !wasEditing) editorHost.scrollIntoView({ block: 'start', behavior: 'auto' });
    },
  };
}

function capitalize(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}
