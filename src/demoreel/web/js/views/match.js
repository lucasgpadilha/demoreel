// Match: the scoreline, the round tape with the plays pinned to it, the round under the cursor, the plays,
// the scoreboard, the reel.

import { el, fill, icon, safeUrl } from '../dom.js';
import { t, clock, joinMeta, list as listWords, mapName, money, num, bytes, scoreSeparator, scoreText } from '../i18n.js';
import { api, ApiError, errorMessage, watchJob } from '../api.js';
import { data, invalidate } from '../data.js';
import { bestRender, byRank, hrefMatch, hrefPlay, hrefPlayer, isFeatured, isGraded } from '../plays.js';
import { trophyGrid } from '../insights.js';
import { halves, killLine, matchWhen, playRow, reasonKind, roundTape, teamFaces, videoTag } from '../parts.js';
import { createPlayer } from '../player.js';
import { followJob, formatField, jobText, rendererField } from '../render.js';
import { isEmbed, isOrganizer } from '../session.js';
import {
  button, confirmDialog, copyText, empty, failure, field, iconButton, linkButton, loading, openDialog, progress, setPageTitle, toast,
  toastError,
} from '../ui.js';
import { posterGrid } from './lists.js';

const TOP_POSTERS = 6;
const ROWS_COLLAPSED = 8;

export function mountMatch(root, route) {
  let alive = true;
  let pollTimer = 0;
  let reelPlayer = null;
  let selectRound = () => {};
  let productionResult = null; // {text, failed}: how the last production run ended, kept across the redraw
  const id = route.id;
  const cleanups = [];

  fill(root, loading());

  async function load() {
    clearTimeout(pollTimer);
    let m;
    try {
      m = await data.match(id);
    } catch (err) {
      if (alive) fill(root, failure(errorMessage(err), load), linkButton(t('nav.matches'), '#/matches', { icon: 'back' }));
      return;
    }
    if (!alive) return;
    setPageTitle(`${mapName(m.map)} ${scoreText(m.team_a.score ?? '', m.team_b.score ?? '')}`);
    if (m.status !== 'ready') {
      drawPending(m);
      return;
    }
    try {
      const [rounds, highlights, board, renders, caps] = await Promise.all([
        data.rounds(id), data.highlights(id), data.scoreboard(id), data.renders(id).catch(() => []), data.capabilities().catch(() => null),
      ]);
      if (alive) drawReady(m, rounds, highlights, board, renders, caps);
    } catch (err) {
      if (alive) fill(root, failure(errorMessage(err), load));
    }
  }

  // ----------------------------------------------------------------------------------------- not ready

  function drawPending(m) {
    const line = progress(t(`match.status.${m.status}`));
    const head = el('header', { class: 'page-head' },
      el('h1', null, m.map ? mapName(m.map) : m.filename),
      el('p', { class: 'lede' }, m.status === 'failed' ? t('match.failedLede') : t('match.pendingLede')));
    if (m.status === 'failed') {
      fill(root, head, el('div', { class: 'state state-error', role: 'alert' }, el('p', null, m.error || t('err.unknown'))),
        isOrganizer() ? el('div', { class: 'actions' },
          button(t('match.reanalyze'), { kind: 'primary', icon: 'refresh', onclick: () => reanalyze() }),
          button(t('match.delete'), { kind: 'danger', icon: 'trash', onclick: () => removeMatch(m) })) : null);
      return;
    }
    line.set(null);
    fill(root, head, line.el);
    api.jobs({ match_id: id, limit: 5 }).then((jobs) => {
      const job = jobs.find((j) => !j.done && j.type !== 'render');
      if (!job || !alive) return;
      const w = watchJob(job, (j) => line.set(j.status === 'running' ? j.progress : null, jobText(j, 'analyze')));
      cleanups.push(w.unsubscribe);
      w.promise.then(() => {
        invalidate('match');
        if (alive) load();
      });
    }).catch(() => {});
    pollTimer = setTimeout(() => {
      invalidate(`match:${id}`);
      if (alive) load();
    }, 5000);
  }

  async function reanalyze() {
    try {
      await api.reanalyze(id);
      invalidate('match');
      invalidate(`hl:${id}`);
      invalidate(`rounds:${id}`);
      invalidate(`sb:${id}`);
      invalidate(`players:${id}`);
      load();
    } catch (err) {
      toastError(errorMessage(err));
    }
  }

  async function removeMatch(m) {
    const ok = await confirmDialog({ title: t('match.delete'), message: t('match.deleteConfirm', { map: mapName(m.map) }), confirmLabel: t('match.delete'), danger: true });
    if (!ok) return;
    try {
      await api.deleteMatch(id);
      invalidate('');
      toast(t('match.deleted'));
      location.hash = '#/matches';
    } catch (err) {
      toastError(errorMessage(err));
    }
  }

  // ----------------------------------------------------------------------------------------- ready

  function drawReady(m, rounds, highlights, board, renders, caps) {
    const teams = { team_a: m.team_a.name || t('team.a'), team_b: m.team_b.name || t('team.b') };
    const aWon = m.team_a.score > m.team_b.score;
    const bWon = m.team_b.score > m.team_a.score;
    const source = safeUrl(m.page_url);

    // "Time A" says little: the best players of each side go under the name.
    const side = (key, won, right) => el('span', { class: `scorebug-side${right ? ' is-right' : ''}` },
      el('span', { class: `scorebug-team${won ? ' is-winner' : ''}` }, teams[key]),
      el('span', { class: 'scorebug-faces' }, teamFaces(board, key)));
    const head = el('header', { class: 'match-head' },
      el('p', { class: 'match-when' }, joinMeta(mapName(m.map), matchWhen(m)),
        // Inside the community's page the link leaves the frame: it goes to that very site.
        source ? el('a', { class: 'more', href: source, target: isEmbed ? '_top' : '_blank', rel: 'noopener' }, t('match.source'), icon('external', 14)) : null),
      el('h1', { class: 'scorebug', 'aria-label': t('match.aria', { map: mapName(m.map), a: teams.team_a, sa: m.team_a.score, b: teams.team_b, sb: m.team_b.score }) },
        side('team_a', aWon, false),
        el('span', { class: `score score-xl${aWon ? ' is-winner' : ''}` }, String(m.team_a.score)),
        el('span', { class: 'scorebug-sep', 'aria-hidden': 'true' }, scoreSeparator()),
        el('span', { class: `score score-xl${bWon ? ' is-winner' : ''}` }, String(m.team_b.score)),
        side('team_b', bWon, true)),
      el('p', { class: 'match-story' }, story(m, rounds, teams)));

    // --- the tape
    const playsByRound = new Map();
    for (const h of highlights) if (h.round) playsByRound.set(h.round, (playsByRound.get(h.round) || 0) + 1);
    const panel = el('div', { class: 'round-panel' });
    let current = null;
    const tape = roundTape(rounds, { teams, playsByRound, onSelect: (n) => selectRound(current === n ? null : n, true) });
    selectRound = (n, fromClick) => {
      current = n && rounds.some((r) => r.number === n) ? n : null;
      tape.select(current);
      if (fromClick) history.replaceState(null, '', current ? `${hrefMatch(id)}/round/${current}` : hrefMatch(id));
      if (!current) {
        fill(panel, el('p', { class: 'note' }, t('match.pickRound')));
        return;
      }
      fill(panel, roundPanel(rounds.find((r) => r.number === current), rounds));
    };

    function roundPanel(r, all) {
      const kind = reasonKind(r.reason);
      const winner = teams[r.winner_team];
      const side = r.winner_side === 'CT' ? t('side.ct') : t('side.t');
      const facts = [];
      if (r.ct_buy || r.t_buy) {
        facts.push(t('round.economy', {
          ct: t(`buy.${r.ct_buy || 'unknown'}`), ctv: money(r.ct_equip_value), tr: t(`buy.${r.t_buy || 'unknown'}`), trv: money(r.t_equip_value),
        }));
      }
      if (r.bomb_planted && r.planter) facts.push(t('round.plant', { who: r.planter.name, site: r.bomb_site || '?' }));
      if (r.defuser) facts.push(t('round.defuse', { who: r.defuser.name }));
      for (const c of r.clutches || []) facts.push(t(c.won ? 'round.clutchWon' : 'round.clutchLost', { who: c.player.name, versus: c.versus }));
      const kills = (r.kills || []).filter((k) => !k.after_round_end);
      const roundPlays = highlights.filter((h) => h.round === r.number).map((h) => ({ h, m, r: bestRender(h.renders) }));
      const killers = [...new Map(kills.filter((k) => k.attacker && !k.is_teamkill && !k.is_suicide).map((k) => [k.attacker.steamid, k.attacker.name])).entries()];
      const who = el('select', { class: 'input' }, killers.map(([steamid, name]) => el('option', { value: steamid }, name)));
      const idx = all.indexOf(r);
      const cut = button(t('round.cut'), { icon: 'scissors', small: true, onclick: async () => {
        cut.disabled = true;
        try {
          const h = await api.createHighlight(id, { round: r.number, player_steamid: who.value });
          invalidate(`hl:${id}`);
          location.hash = hrefPlay(h);
        } catch (err) {
          toastError(errorMessage(err));
          cut.disabled = false;
        }
      } });
      return [
        el('div', { class: 'round-head' },
          el('h2', null, t('round.title', { n: r.number })),
          el('div', { class: 'round-nav' },
            iconButton('prev', t('round.prev'), { disabled: idx <= 0, onclick: () => selectRound(all[idx - 1].number, true) }),
            iconButton('next', t('round.next'), { disabled: idx >= all.length - 1, onclick: () => selectRound(all[idx + 1].number, true) }))),
        el('p', { class: 'round-result' },
          t('round.result', { winner, side, how: t(`reason.${kind}`), a: r.score_team_a, b: r.score_team_b, time: clock(r.duration_s) })),
        facts.length ? el('ul', { class: 'round-facts' }, facts.map((f) => el('li', null, f))) : null,
        el('div', { class: 'round-cols' },
          el('div', null,
            el('h3', null, t('round.feed')),
            kills.length ? el('ol', { class: 'kills' }, kills.map((k) => killLine(k, { time: clock(k.clock_s), context: true, linkPlayers: true }))) : el('p', { class: 'note' }, t('round.noKills'))),
          el('div', null,
            el('h3', null, t('round.plays')),
            roundPlays.length ? el('ol', { class: 'rows' }, roundPlays.map((p) => playRow(p, { context: 'match' }))) : el('p', { class: 'note' }, t('round.noPlays')),
            killers.length && isOrganizer() ? el('div', { class: 'cut' }, field(t('round.cutLabel'), who), cut) : null)),
      ];
    }

    const tapeSection = el('section', { class: 'section tape-section' },
      el('div', { class: 'rtape-wrap' },
        el('div', { class: 'rtape-teams', 'aria-hidden': 'true' }, el('span', null, teams.team_a), el('span', null, teams.team_b)),
        tape.el),
      el('p', { class: 'legend' },
        el('span', { class: 'legend-item' }, el('span', { class: 'swatch is-ct' }), t('legend.ct')),
        el('span', { class: 'legend-item' }, el('span', { class: 'swatch is-t' }), t('legend.t')),
        el('span', { class: 'legend-item' }, el('span', { class: 'swatch is-pin' }), t('legend.play')),
        el('span', { class: 'legend-item' }, icon('bomb', 13), t('legend.bomb')),
        el('span', { class: 'legend-item' }, icon('defuse', 13), t('legend.defuse')),
        el('span', { class: 'legend-item' }, el('span', { class: 'swatch is-eco' }), t('legend.eco'))),
      panel);

    // --- plays: the featured ones ("destaques", which get POV videos) apart from the others (2D replays)
    const all = highlights.map((h) => ({ h, m, r: bestRender(h.renders) }));
    // Matches analyzed before curation have no tiers and nothing featured: they keep one plain list.
    const curated = highlights.some((h) => isFeatured(h) || isGraded(h));
    const featured = all.filter((p) => isFeatured(p.h)).sort((a, b) => byRank(a.h, b.h));
    const others = curated ? all.filter((p) => !isFeatured(p.h)) : all;
    const withVideo = all.filter((p) => p.r).slice(0, TOP_POSTERS);
    const rowsHost = el('ol', { class: 'rows' });
    let expanded = false;
    const moreBtn = button('', { small: true, onclick: () => {
      expanded = !expanded;
      drawRows();
    } });
    function drawRows() {
      const shown = expanded ? others : others.slice(0, ROWS_COLLAPSED);
      const action = (p) => (!isOrganizer() ? null : curated ? promote(p) : (p.r ? null : renderNow(p)));
      fill(rowsHost, shown.map((p) => playRow(p, { context: 'match', trailing: action(p) })));
      moreBtn.hidden = others.length <= ROWS_COLLAPSED;
      moreBtn.firstChild.textContent = expanded ? t('match.fewerPlays') : t('match.allPlays', { n: others.length });
    }
    // Promoting a play is the organizer saying "this one deserves a POV": the video comes with the next
    // production run, not at once.
    function promote(p) {
      const go = button(t('curation.promote'), { small: true, kind: 'ghost', title: t('curation.promoteHint'), onclick: async () => {
        go.disabled = true;
        try {
          await api.patchHighlight(p.h.id, { featured: true });
          invalidate(`hl:${id}`);
          toast(t('curation.promoted'));
          if (alive) load();
        } catch (err) {
          toastError(errorMessage(err));
          go.disabled = false;
        }
      } });
      return el('span', { class: 'row-action' }, go);
    }
    function renderNow(p) {
      const slot = el('span', { class: 'row-action' });
      const go = button(t('video.make'), { small: true, icon: 'film', onclick: async () => {
        const line = progress(t('job.queued'));
        fill(slot, line.el);
        try {
          const res = await api.renderHighlight(p.h.id, {});
          const job = await followJob(res.job, line);
          if (job.status !== 'succeeded') throw new Error(jobText(job));
          invalidate(`hl:${id}`);
          invalidate(`renders:${id}`);
          if (alive) load();
        } catch (err) {
          toastError(errorMessage(err));
          if (alive) fill(slot, go);
        }
      } });
      slot.appendChild(go);
      return slot;
    }
    drawRows();

    // --- every video of the match in one go (organizer)
    const produceHost = el('div', { class: 'produce' });
    if (productionResult) {
      fill(produceHost, el('p', { class: productionResult.failed ? 'state state-error' : 'note', role: productionResult.failed ? 'alert' : null }, productionResult.text));
    }
    const produceBtn = button(t('produce.action'), { icon: 'film', hidden: true, onclick: askProduce });
    if (isOrganizer()) {
      data.canProduce().then((ok) => {
        if (alive && ok && all.length) produceBtn.hidden = false;
      });
    }

    async function askProduce() {
      const d = openDialog({ title: t('produce.action') });
      const pick = (label, hint, checked = true) => {
        const input = el('input', { type: 'checkbox', checked });
        return { input, node: el('label', { class: 'choice' }, input, el('span', { class: 'choice-text' }, el('span', { class: 'choice-label' }, label), hint ? el('span', { class: 'choice-hint' }, hint) : null)) };
      };
      const pov = pick(t('video.ingame'), t('produce.povHint'));
      const radar = pick(t('video.radar'), t('produce.radarHint'));
      const wide = pick(t('format.16x9'));
      const tall = pick(t('format.9x16'), t('format.9x16Hint'));
      const everyone = pick(t('produce.povAll', { n: all.length }), t('produce.povAllHint'), false);
      fill(d.body,
        el('p', null, featured.length ? t('produce.rule', { n: featured.length, total: all.length }) : t('produce.ruleNone', { total: all.length })),
        el('p', { class: 'note' }, t('produce.cost')),
        el('details', { class: 'advanced' },
          el('summary', null, t('produce.advanced')),
          everyone.node,
          el('fieldset', { class: 'choices' }, el('legend', null, t('render.kind')), pov.node, radar.node),
          el('fieldset', { class: 'choices' }, el('legend', null, t('render.format')), wide.node, tall.node)));
      fill(d.footer, button(t('action.cancel'), { onclick: () => d.close(null) }),
        button(t('produce.start'), { kind: 'primary', icon: 'film', onclick: () => {
          const renderers = [pov.input.checked ? 'ingame' : null, radar.input.checked ? 'radar' : null].filter(Boolean);
          const formats = [wide.input.checked ? '16:9' : null, tall.input.checked ? '9:16' : null].filter(Boolean);
          if (renderers.length && formats.length) d.close({ renderers, formats, pov: everyone.input.checked ? 'all' : 'featured' });
        } }));
      const choice = await d.closed;
      if (!choice || !alive) return;
      produceBtn.disabled = true;
      productionResult = null;
      try {
        const res = await api.produce(id, choice);
        const later = res.pending_after_capture || 0;
        const summary = [
          res.featured ? t('produce.povFor', { n: res.featured }) : null,
          t('produce.queued', { n: res.render_jobs || 0 }),
          later ? t('produce.pending', { n: later }) : null,
          res.skipped ? t('produce.skipped', { n: res.skipped }) : null,
          res.detail || null,
        ].filter(Boolean).join(' ');
        if (!res.capture_job && !res.render_jobs) {
          fill(produceHost, el('p', { class: 'note' }, summary));
          produceBtn.disabled = false;
          return;
        }
        watchProduction(summary, (res.render_jobs || 0) + later);
      } catch (err) {
        produceBtn.disabled = false;
        if (err instanceof ApiError && err.status === 404) produceBtn.hidden = true; // this server cannot do it
        toastError(errorMessage(err));
      }
    }

    // Progress of a production run, read from the jobs of the match: first the recording, then the renders.
    function watchProduction(summary, expected = 0) {
      const capture = progress(t('produce.capturing'));
      const renders = progress(t('produce.rendering', { done: 0, total: 0 }));
      capture.el.hidden = true;
      renders.el.hidden = true;
      fill(produceHost, el('p', { class: 'note' }, summary || t('produce.resumed')), capture.el, renders.el);
      produceBtn.disabled = true;
      const seen = new Map(); // render job id -> last known status
      let firstError = '';
      let timer = 0;
      let idle = 0;
      const tick = async () => {
        if (!alive) return;
        let jobs;
        try {
          jobs = await api.jobs({ match_id: id, limit: 200 });
        } catch {
          timer = setTimeout(tick, 5000);
          return;
        }
        if (!alive) return;
        const cap = jobs.find((j) => j.type === 'capture_match' && !j.done);
        for (const j of jobs) {
          if (j.type === 'render' && (!j.done || seen.has(j.id))) seen.set(j.id, j.status);
          if (seen.has(j.id) && j.status === 'failed' && j.error && !firstError) firstError = j.error;
        }
        const statuses = [...seen.values()];
        const total = Math.max(statuses.length, expected); // renders that wait for the recording are not jobs yet
        const done = statuses.filter((s) => s === 'succeeded').length;
        const failed = statuses.filter((s) => s === 'failed' || s === 'canceled').length;
        const active = statuses.length - done - failed;
        capture.el.hidden = !cap;
        if (cap) capture.set(cap.status === 'running' ? cap.progress : null, t(cap.status === 'running' ? 'produce.capturing' : 'produce.captureQueued'));
        renders.el.hidden = !total;
        if (total) renders.set((done + failed) / total, t('produce.rendering', { done, total }) + (failed ? ` ${t('produce.failed', { n: failed })}` : ''));
        if (!cap && !active) {
          idle += 1;
          // Quiet with everything accounted for: the run is over. If renders that were promised never show
          // up (the recording failed), stop waiting after a minute.
          if ((idle >= 2 && statuses.length >= expected) || idle >= 20) {
            invalidate(`hl:${id}`);
            invalidate(`renders:${id}`);
            // Say how it ended, and keep saying it after the page redraws: a run that failed must not just vanish.
            const text = [t('produce.done', { n: done }), failed ? t('produce.failed', { n: failed }) : null, failed && firstError ? t('produce.serverSays', { detail: firstError }) : null].filter(Boolean).join(' ');
            productionResult = statuses.length ? { text, failed: failed > 0 } : null;
            load();
            return;
          }
        } else idle = 0;
        timer = setTimeout(tick, 3000);
      };
      cleanups.push(() => clearTimeout(timer));
      tick();
    }

    // Coming back to the page while a run is going: pick the progress up again.
    data.canProduce().then(async (ok) => {
      if (!ok || !alive || !isOrganizer()) return;
      const jobs = await api.jobs({ match_id: id, limit: 200 }).catch(() => []);
      const active = jobs.filter((j) => !j.done);
      if (alive && (active.some((j) => j.type === 'capture_match') || active.filter((j) => j.type === 'render').length >= 2)) watchProduction('');
    });

    // Curated match: "Destaques" as posters in rank order with the team named, then the other plays as a
    // compact list. Older match: the plain list it always had.
    const playsSection = curated
      ? [
        el('section', { class: 'section' },
          el('div', { class: 'section-head' }, el('h2', null, t('match.featuredTitle')), produceBtn),
          el('p', { class: 'note section-note' }, t('match.featuredNote')),
          produceHost,
          featured.length
            ? posterGrid(featured, { context: 'match', showFeatured: false })
            : el('p', { class: 'note' }, t(isOrganizer() ? 'match.noFeatured' : 'match.noFeaturedVisitor'))),
        others.length ? el('section', { class: 'section' },
          el('div', { class: 'section-head' }, el('h2', null, t('match.othersTitle', { n: others.length }))),
          el('p', { class: 'note section-note' }, t(isOrganizer() ? 'match.othersNote' : 'match.othersNoteVisitor')),
          rowsHost, moreBtn) : null,
      ]
      : el('section', { class: 'section' },
        el('div', { class: 'section-head' }, el('h2', null, t('match.playsTitle')), produceBtn),
        produceHost,
        all.length
          ? [isOrganizer() ? el('p', { class: 'note section-note' }, t('match.notCurated')) : null,
            withVideo.length ? posterGrid(withVideo) : el('p', { class: 'note' }, t('match.noVideosYet')),
            el('h3', { class: 'sub' }, t('match.everyPlay', { n: all.length })), rowsHost, moreBtn]
          : empty(t('match.noPlays')));

    // --- zoeira: the fails of the match, apart from the plays (they are banter, not part of the ranking)
    const banterSection = el('section', { class: 'section', hidden: true });
    data.fails(id).then((fails) => {
      if (!alive || !fails.length) return;
      const items = fails.map((h) => ({ h, m, r: bestRender(h.renders) }));
      const top = items.filter((p) => isFeatured(p.h));
      const rest = items.filter((p) => !isFeatured(p.h));
      const list = el('ol', { class: 'rows' });
      const FIRST = 5;
      const row = (p) => playRow(p, { context: 'match', trailing: isOrganizer() ? promote(p) : null });
      for (const p of rest.slice(0, FIRST)) list.appendChild(row(p));
      const all = rest.length > FIRST ? button(t('match.allPlays', { n: rest.length }), { small: true, onclick: () => {
        for (const p of rest.slice(FIRST)) list.appendChild(row(p));
        all.remove();
      } }) : null;
      banterSection.hidden = false;
      fill(banterSection,
        el('div', { class: 'section-head' }, el('h2', null, t('nav.banter')), el('a', { class: 'more', href: '#/zoeira' }, t('match.allBanter'))),
        el('p', { class: 'note section-note' }, t('match.banterNote')),
        top.length ? posterGrid(top, { context: 'match' }) : null,
        rest.length ? [top.length ? el('h3', { class: 'sub' }, t('match.banterRest', { n: rest.length })) : null, list, all] : null);
    }).catch(() => {});

    // --- trophies of the match
    const trophySection = el('section', { class: 'section', hidden: true });
    data.trophies({ match_id: id }).then((res) => {
      const trophies = (res && res.trophies) || [];
      if (!alive || !trophies.length) return;
      trophySection.hidden = false;
      fill(trophySection, el('div', { class: 'section-head' }, el('h2', null, t('match.trophies'))), trophyGrid(trophies));
    }).catch(() => {});

    // --- scoreboard, reel, manage
    const reelSection = el('section', { class: 'section' });
    drawReels(reelSection, m, highlights, renders, caps, board);

    fill(root, head, tapeSection, playsSection, banterSection, trophySection,
      el('section', { class: 'section' }, el('div', { class: 'section-head' }, el('h2', null, t('sb.title'))), scoreboard(board, id)),
      reelSection,
      !isOrganizer() ? null : el('section', { class: 'section manage' },
        el('h2', null, t('match.manage')),
        el('p', { class: 'note' }, t('match.file', { name: m.filename, size: bytes(m.size_bytes) })),
        el('div', { class: 'actions' },
          button(t('match.rescore'), { small: true, onclick: async () => {
            if (!(await confirmDialog({ title: t('match.rescore'), message: t('match.rescoreConfirm'), confirmLabel: t('match.rescore') }))) return;
            try {
              const res = await api.rescore(id);
              invalidate(`hl:${id}`);
              toast(t('match.rescored', { n: (res && res.featured) || 0 }));
              if (alive) load();
            } catch (err) {
              toastError(errorMessage(err));
            }
          } }),
          button(t('match.reanalyze'), { icon: 'refresh', small: true, onclick: async () => {
            if (await confirmDialog({ title: t('match.reanalyze'), message: t('match.reanalyzeConfirm'), confirmLabel: t('match.reanalyze') })) reanalyze();
          } }),
          button(t('match.delete'), { kind: 'danger', icon: 'trash', small: true, onclick: () => removeMatch(m) }))));

    selectRound(route.sub === 'round' ? Number(route.arg) : null, false);
    if (route.sub === 'round' && current) requestAnimationFrame(() => panel.scrollIntoView({ block: 'nearest' }));
  }

  // ----------------------------------------------------------------------------------------- reels

  function drawReels(section, m, highlights, renders, caps, board) {
    if (reelPlayer) reelPlayer.destroy();
    reelPlayer = null;
    const reels = renders.filter((r) => r.kind === 'reel');
    const ready = reels.filter((r) => r.status === 'ready' && r.video_url);
    const running = reels.filter((r) => r.status === 'queued' || r.status === 'running');
    const screen = el('div', { class: 'reel-screen' });
    const listHost = el('ul', { class: 'reel-list' });

    function showReel(r, play) {
      if (!reelPlayer) {
        reelPlayer = createPlayer({});
        fill(screen, reelPlayer.el);
      }
      reelPlayer.load({ render: r, timeline: null, play });
      for (const li of listHost.children) li.classList.toggle('is-current', li.dataset.id === r.id);
    }

    fill(listHost, ready.map((r) => el('li', { dataset: { id: r.id } },
      el('button', { type: 'button', class: 'reel-pick', onclick: () => showReel(r, true) },
        el('span', { class: 'reel-title' }, r.title || t('reel.untitled')),
        videoTag(r),
        el('span', { class: 'row-time' }, clock(r.duration_s)),
        r.stale ? el('span', { class: 'stale-flag' }, t('video.staleShort')) : null),
      iconButton('link', t('action.copyVideoLink'), { onclick: async () => toast((await copyText(r.video_url)) ? t('action.copied') : t('action.copyFailed')) }),
      el('a', { class: 'btn btn-icon', href: safeUrl(r.video_url), download: '', 'aria-label': t('action.download'), title: t('action.download') }, icon('download', 20)),
      !isOrganizer() ? null : iconButton('trash', t('video.delete'), { onclick: async () => {
        if (!(await confirmDialog({ title: t('video.delete'), message: t('video.deleteConfirm'), confirmLabel: t('video.delete'), danger: true }))) return;
        try {
          await api.deleteRender(r.id);
          invalidate(`renders:${m.id}`);
          if (alive) load();
        } catch (err) {
          toastError(errorMessage(err));
        }
      } }))));

    // creation form
    const top = el('input', { class: 'input input-num', type: 'number', min: '2', max: '30', value: String(Math.min(5, Math.max(2, highlights.length))) });
    const who = el('select', { class: 'input' }, el('option', { value: '' }, t('reel.everyone')),
      (board.teams || []).flatMap((team) => (team.players || []).map((p) => el('option', { value: p.steamid }, p.name))));
    const kind = rendererField(caps);
    const shape = formatField();
    const status = el('div', { class: 'reel-status' });
    const make = button(t('reel.make'), { kind: 'primary', icon: 'film', onclick: async () => {
      make.disabled = true;
      const line = progress(t('job.queued'));
      fill(status, line.el);
      try {
        const vars = { map: mapName(m.map), a: m.team_a.score, b: m.team_b.score, player: who.value ? who.selectedOptions[0].textContent : '' };
        const res = await api.createReel(m.id, {
          top: Number(top.value) || 5, player_steamid: who.value || null, renderer: kind.value(), options: { format: shape.value() },
          title: t(who.value ? 'reel.titlePlayer' : 'reel.titleAll', vars),
        });
        fill(status, jobRow(line, res.render.id));
        const job = await followJob(res.job, line);
        invalidate(`renders:${m.id}`);
        if (job.status === 'canceled') {
          if (alive) load();
          return;
        }
        if (job.status !== 'succeeded') throw new Error(jobText(job));
        toast(t('reel.done'));
        if (alive) load();
      } catch (err) {
        if (alive) fill(status, el('p', { class: 'state state-error', role: 'alert' }, errorMessage(err)));
        make.disabled = false;
      }
    } });
    const form = el('form', { class: 'reel-form', onsubmit: (e) => e.preventDefault() },
      el('div', { class: 'reel-fields' }, field(t('reel.top'), top), field(t('reel.player'), who)),
      kind.el,
      shape.el,
      el('div', { class: 'actions' }, make),
      status);

    // A render in progress: its progress line and a way to stop it.
    function jobRow(line, renderId) {
      return el('div', { class: 'job-row' }, line.el, button(t('action.cancel'), { small: true, kind: 'ghost', onclick: async () => {
        try {
          await api.deleteRender(renderId);
          invalidate(`renders:${m.id}`);
          if (alive) load();
        } catch (err) {
          toastError(errorMessage(err));
        }
      } }));
    }

    for (const r of isOrganizer() ? running : []) {
      const line = progress(t('job.queued'));
      line.set(null);
      status.appendChild(jobRow(line, r.id));
      if (r.job_id) {
        followJob({ id: r.job_id }, line).then(() => {
          invalidate(`renders:${m.id}`);
          if (alive) load();
        });
      }
    }

    // A visitor watches the reels that exist; building one is the organizer's. No reel, no section.
    section.hidden = !isOrganizer() && !ready.length;
    fill(section,
      el('div', { class: 'section-head' }, el('h2', null, t('reel.title'))),
      el('p', { class: 'note section-note' }, t('reel.lede')),
      el('div', { class: `reel${ready.length && isOrganizer() ? '' : ' is-empty'}` },
        ready.length ? el('div', { class: 'reel-watch' }, screen, listHost) : null,
        !isOrganizer() ? null : (highlights.length ? form : el('p', { class: 'note' }, t('match.noPlays')))));
    if (ready.length) {
      const wanted = route.sub === 'reel' ? ready.find((r) => r.id === route.arg) : null;
      showReel(wanted || ready[0], false);
    }
  }

  load();
  return {
    destroy() {
      alive = false;
      clearTimeout(pollTimer);
      for (const fn of cleanups) fn();
      if (reelPlayer) reelPlayer.destroy();
    },
    update(next) {
      if (next.sub === 'round') selectRound(Number(next.arg), false);
      else if (!next.sub) selectRound(null, false);
    },
  };
}

// ------------------------------------------------------------------------------------------- story

/** One or two sentences about how the match went, from the round history. */
function story(m, rounds, teams) {
  // A demo can hold only part of a match. The score is still the real one (it is read from the scoreboard),
  // but halves and streaks computed from the rounds we have would be fiction, so say what is missing instead.
  const played = (m.team_a.score ?? 0) + (m.team_b.score ?? 0);
  if (rounds.length && played > rounds.length) return t('story.incomplete', { n: rounds.length, total: played });
  const parts = [];
  const h = halves(rounds);
  const winnerKey = m.team_a.score > m.team_b.score ? 'team_a' : m.team_b.score > m.team_a.score ? 'team_b' : null;
  if (h.length) {
    const first = h[0];
    parts.push(t('story.half', { a: first.a, b: first.b }));
    if (winnerKey) {
      const winnerAtHalf = winnerKey === 'team_a' ? first.a - first.b : first.b - first.a;
      if (winnerAtHalf < 0) parts.push(t('story.comeback', { team: teams[winnerKey] }));
    }
  }
  if (rounds.some((r) => r.is_overtime)) parts.push(t('story.overtime'));
  const loser = Math.min(m.team_a.score, m.team_b.score);
  if (winnerKey && loser <= 3 && !rounds.some((r) => r.is_overtime)) parts.push(t('story.stomp', { team: teams[winnerKey] }));
  // longest winning streak
  let best = { team: null, n: 0 };
  let run = { team: null, n: 0 };
  for (const r of rounds) {
    run = r.winner_team === run.team ? { team: run.team, n: run.n + 1 } : { team: r.winner_team, n: 1 };
    if (run.n > best.n) best = run;
  }
  if (best.n >= 4) parts.push(t('story.streak', { team: teams[best.team], n: best.n }));
  parts.push(t('story.rounds', { n: rounds.length, time: clock(m.duration_s) }));
  return parts.join(' ');
}

// ------------------------------------------------------------------------------------------- scoreboard

const COLUMNS = [
  { key: 'kills', label: 'sb.k', full: 'sb.kFull' },
  { key: 'deaths', label: 'sb.d', full: 'sb.dFull', lowerIsBetter: true },
  { key: 'assists', label: 'sb.a', full: 'sb.aFull', wide: true },
  { key: 'diff', label: 'sb.diff', full: 'sb.diffFull', wide: true, signed: true },
  { key: 'adr', label: 'sb.adr', full: 'sb.adrFull', digits: 1 },
  { key: 'kast', label: 'sb.kast', full: 'sb.kastFull', suffix: '%', wide: true },
  { key: 'hs_pct', label: 'sb.hs', full: 'sb.hsFull', suffix: '%', wide: true },
  { key: 'rating', label: 'sb.rating', full: 'sb.ratingFull', digits: 2, strong: true },
];

function scoreboard(board, matchId) {
  let sortKey = 'rating';
  let dir = -1;
  const open = new Set();
  const host = el('div', { class: 'boards' });
  const everyone = (board.teams || []).flatMap((team) => team.players || []);
  const bestOf = (col) => {
    const values = everyone.map((p) => value(p, col.key));
    return col.lowerIsBetter ? Math.min(...values) : Math.max(...values);
  };
  function value(p, key) {
    return key === 'diff' ? p.kills - p.deaths : Number(p[key]) || 0;
  }
  function cell(p, col) {
    const v = value(p, col.key);
    let text = num(v, col.digits || 0);
    if (col.signed && v > 0) text = `+${text}`;
    if (col.suffix) text += col.suffix;
    const cls = ['num', col.wide ? 'wide' : '', col.strong ? 'strong' : '', v === bestOf(col) ? 'is-best' : '',
      col.signed ? (v > 0 ? 'is-pos' : v < 0 ? 'is-neg' : '') : ''].filter(Boolean).join(' ');
    return el('td', { class: cls }, text);
  }
  function draw() {
    fill(host, (board.teams || []).map((team) => {
      const players = [...(team.players || [])].sort((x, y) => (value(x, sortKey) - value(y, sortKey)) * dir);
      return el('div', { class: `table-wrap board-${team.key === 'team_b' ? 'b' : 'a'}` }, el('table', { class: 'table board' },
        el('caption', null, el('span', { class: `board-team${team.won ? ' is-winner' : ''}` }, team.name)),
        el('thead', null, el('tr', null,
          el('th', { scope: 'col' }, t('sb.player')),
          COLUMNS.map((col) => el('th', { scope: 'col', class: `num${col.wide ? ' wide' : ''}`, 'aria-sort': sortKey === col.key ? (dir < 0 ? 'descending' : 'ascending') : null },
            el('button', { type: 'button', class: 'th-sort', title: t(col.full), onclick: () => {
              if (sortKey === col.key) dir = -dir;
              else {
                sortKey = col.key;
                dir = col.lowerIsBetter ? 1 : -1;
              }
              draw();
            } }, t(col.label)))))),
        el('tbody', null, players.flatMap((p) => {
          const isOpen = open.has(p.steamid);
          const detail = isOpen ? el('tr', { class: 'board-detail' }, el('td', { colSpan: COLUMNS.length + 1 }, playerDetail(matchId, p))) : null;
          return [el('tr', { class: isOpen ? 'is-open' : null },
            el('th', { scope: 'row' },
              el('button', { type: 'button', class: 'board-toggle', 'aria-expanded': isOpen ? 'true' : 'false', 'aria-label': t('sb.detail', { name: p.name }), onclick: () => {
                if (isOpen) open.delete(p.steamid);
                else open.add(p.steamid);
                draw();
              } }, icon('chevron', 16)),
              el('a', { href: hrefPlayer(p.steamid) }, p.name)),
            COLUMNS.map((col) => cell(p, col))), detail];
        }))));
    }));
  }
  draw();
  return host;
}

function playerDetail(matchId, row) {
  const host = el('div', { class: 'detail' }, loading());
  data.players(matchId).then((players) => {
    const p = players.find((x) => x.steamid === row.steamid);
    if (!p) {
      fill(host, el('p', { class: 'note' }, t('sb.noDetail')));
      return;
    }
    const multi = ['5k', '4k', '3k', '2k'].filter((k) => p.multi_kills && p.multi_kills[k]).map((k) => `${p.multi_kills[k]}× ${k.toUpperCase()}`);
    const clutchSizes = Object.entries(p.clutches_by_size || {}).filter(([, n]) => n).map(([size, n]) => `${n}× ${size}`);
    const weapons = Object.entries(p.weapon_kills || {}).sort((a, b) => b[1] - a[1]).slice(0, 4).map(([w, n]) => `${w} ${n}`);
    const side = (key) => {
      const s = p.by_side && p.by_side[key];
      return s ? t('detail.side', { k: s.kills, d: s.deaths, adr: num(s.adr, 1), rating: num(s.rating, 2) }) : null;
    };
    const u = p.utility || {};
    const items = [
      [t('detail.multi'), multi.length ? listWords(multi) : t('detail.none')],
      [t('detail.clutches'), p.clutches_attempted ? t('detail.clutchLine', { won: p.clutches_won, tried: p.clutches_attempted }) + (clutchSizes.length ? ` (${clutchSizes.join(', ')})` : '') : t('detail.none')],
      [t('detail.opening'), t('detail.openingLine', { k: p.opening_kills, d: p.opening_deaths })],
      [t('detail.trades'), t('detail.tradesLine', { k: p.trade_kills, d: p.traded_deaths })],
      [t('detail.weapons'), weapons.length ? weapons.join(', ') : t('detail.none')],
      [t('detail.aim'), t('detail.aimLine', { acc: num(p.accuracy || 0, 1), hs: num(p.headshot_pct), dist: p.avg_kill_distance_m ? num(p.avg_kill_distance_m, 1) : '?' })],
      [t('detail.utility'), t('detail.utilityLine', { flashed: u.enemies_flashed || 0, fa: u.flash_assists || 0, he: u.he_damage || 0, fire: u.fire_damage || 0 })],
      side('CT') ? [t('side.ct'), side('CT')] : null,
      side('T') ? [t('side.t'), side('T')] : null,
    ].filter(Boolean);
    fill(host, el('dl', { class: 'facts' }, items.map(([k, v]) => el('div', null, el('dt', null, k), el('dd', null, v)))));
  }).catch((err) => fill(host, el('p', { class: 'note' }, errorMessage(err))));
  return host;
}
