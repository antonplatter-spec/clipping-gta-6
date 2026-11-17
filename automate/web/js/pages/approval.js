// Freigabe-Queue: review Claude's picks, edit hooks/titles/text, approve → render, reject; history tabs.
import * as api from '../api.js';
import {
  html, useState, useEffect, useMemo, useRef, useData, useTitle, useNow, fmt, cx, toast, hookText, plainHook,
  confirmDialog, setQuery, hostOf,
} from '../lib.js';
import {
  PageHeader, Button, IconButton, Badge, Icon, Tabs, Loading, ErrorState, Empty, TextField, TextareaField, AutoText,
  SelectField, TagInput, Callout, Modal, PickStatusBadge, Pager,
} from '../ui.js';
import {
  CLIP_TYPES, HOOK_STYLES, CAPTION_STYLES, readCheck, rankingFor, rowType, hookTypeLabel, factorTone,
} from '../labels.js';
import { overview, loadProfiles } from '../store.js';

const TABS = [
  { id: 'offen', status: 'proposed', label: 'Offen', tone: 'yellow' },
  { id: 'freigegeben', status: 'approved,rendering', label: 'Freigegeben' },
  { id: 'gerendert', status: 'rendered', label: 'Gerendert' },
  { id: 'abgelehnt', status: 'rejected,failed', label: 'Abgelehnt' },
];
const LIMIT = 20;

export function ApprovalPage({ route }) {
  useTitle('Freigabe');
  const tab = TABS.find((t) => t.id === route.query.tab) || TABS[0];
  const profile = route.query.profile || '';
  const focusId = Number(route.query.pick) || null;
  const [offset, setOffset] = useState(0);
  useEffect(() => setOffset(0), [tab.id, profile]);
  const isQueue = tab.id === 'offen';
  const list = useData(() => api.listPicks({ status: tab.status, profile, limit: LIMIT, offset }), [tab.id, profile, offset],
    { interval: isQueue ? 0 : 15000 });
  // The queue itself is never re-rendered under the owner's hands; a light poll only announces new picks.
  const probe = useData(() => api.listPicks({ status: 'proposed', profile, limit: 1 }), [profile], { interval: 15000 });
  const profiles = useData(() => loadProfiles(), []);
  const [selected, setSelected] = useState(new Set());
  const [dirty, setDirty] = useState(new Set());
  const [bulkBusy, setBulkBusy] = useState(null);
  useEffect(() => { setSelected(new Set()); }, [tab.id, profile, offset]);

  const items = list.data ? list.data.items || [] : [];
  const counts = (probe.data && probe.data.counts) || (list.data && list.data.counts) || {};
  const tabCount = (t) => t.status.split(',').reduce((n, s) => n + (counts[s] || 0), 0);
  const newCount = isQueue && probe.data && list.data ? Math.max(0, probe.data.total - list.data.total) : 0;

  const removeLocal = (ids) => {
    list.setData((d) => d && { ...d, items: d.items.filter((p) => !ids.includes(p.id)), total: Math.max(0, d.total - ids.length) });
    setSelected((s) => { const n = new Set(s); ids.forEach((i) => n.delete(i)); return n; });
    probe.reload();
    overview.refresh();
  };
  const replaceLocal = (pick) => list.setData((d) => d && { ...d, items: d.items.map((p) => (p.id === pick.id ? pick : p)) });
  const markDirty = (id, isDirty) => setDirty((s) => {
    if (s.has(id) === isDirty) return s;
    const n = new Set(s); if (isDirty) n.add(id); else n.delete(id); return n;
  });

  const bulk = async (action) => {
    const ids = [...selected];
    if (!ids.length) return;
    let reason;
    const unsaved = ids.filter((i) => dirty.has(i)).length;
    if (action === 'reject') {
      reason = await confirmDialog({
        title: `${fmt.plural(ids.length, 'Pick', 'Picks')} ablehnen?`, confirmLabel: 'Ablehnen', danger: true,
        message: 'Abgelehnte Picks werden nicht gerendert. Du findest sie unter „Abgelehnt“.',
        input: { label: 'Grund', optional: true, placeholder: 'z. B. schwacher Moment, schon gepostet …' },
      });
      if (reason === false) return;
    } else if (unsaved) {
      const ok = await confirmDialog({
        title: 'Ungespeicherte Änderungen', confirmLabel: 'Trotzdem freigeben',
        message: `${fmt.plural(unsaved, 'ausgewählter Pick hat', 'ausgewählte Picks haben')} ungespeicherte Änderungen. Freigegeben wird der gespeicherte Stand.`,
      });
      if (!ok) return;
    }
    setBulkBusy(action);
    try {
      const res = await api.bulkPicks({ ids, action, reason: reason || undefined });
      const results = res.results || [];
      const ok = results.filter((r) => r.ok).map((r) => r.id);
      const bad = results.filter((r) => !r.ok);
      if (ok.length) toast.success(action === 'approve' ? `${fmt.plural(ok.length, 'Pick', 'Picks')} freigegeben – Render-Jobs angelegt.` : `${fmt.plural(ok.length, 'Pick', 'Picks')} abgelehnt.`);
      bad.forEach((r) => toast.error(`Pick #${r.id}: ${(r.error && r.error.message) || 'fehlgeschlagen'}`));
      removeLocal(ok);
    } catch (err) {
      toast.fail(err);
    } finally {
      setBulkBusy(null);
    }
  };

  const enabledProfiles = (profiles.data || []);
  const allSelected = items.length > 0 && items.every((p) => selected.has(p.id));
  return html`<div class="page">
    <${PageHeader} title="Freigabe" subtitle="Claudes Vorschläge prüfen: Hooks und Titel anpassen, freigeben → Render, oder ablehnen."
      actions=${enabledProfiles.length > 1 && html`<${SelectField} label="Profil" hideLabel value=${profile} onChange=${(v) => setQuery({ profile: v })}
        placeholder="Alle Profile" options=${enabledProfiles.map((p) => ({ value: p.name, label: p.name }))} />`} />
    <${Tabs} label="Status" value=${tab.id} onChange=${(id) => setQuery({ tab: id === 'offen' ? '' : id, pick: '' })}
      tabs=${TABS.map((t) => ({ id: t.id, label: t.label, count: counts && Object.keys(counts).length ? tabCount(t) : null, tone: t.id === 'offen' && tabCount(t) ? 'yellow' : undefined }))} />
    ${newCount > 0 && html`<div class="new-banner"><${Button} icon="refresh-cw" onClick=${() => list.reload()}>
      ${fmt.plural(newCount, 'neuer Pick', 'neue Picks')} – anzeigen<//></div>`}
    ${isQueue && items.length > 0 && html`<div class="bulkbar" role="region" aria-label="Mehrfachauswahl">
      <label class="checkbox"><input type="checkbox" checked=${allSelected}
        onChange=${(e) => setSelected(e.currentTarget.checked ? new Set(items.map((p) => p.id)) : new Set())} />
        ${selected.size ? `${selected.size} ausgewählt` : 'Alle auswählen'}</label>
      <span class="grow"></span>
      <${Button} variant="approve" icon="check" disabled=${!selected.size || !!bulkBusy} busy=${bulkBusy === 'approve'} onClick=${() => bulk('approve')}>Freigeben${selected.size ? ` (${selected.size})` : ''}<//>
      <${Button} variant="danger" icon="x" disabled=${!selected.size || !!bulkBusy} busy=${bulkBusy === 'reject'} onClick=${() => bulk('reject')}>Ablehnen${selected.size ? ` (${selected.size})` : ''}<//>
    </div>`}
    ${list.error && !list.data ? html`<${ErrorState} error=${list.error} onRetry=${list.reload} />`
      : !list.data ? html`<div class="card"><${Loading} lines=${8} label="Lade Picks …" /></div>`
      : !items.length ? html`<div class="card"><${EmptyFor} tab=${tab.id} /></div>`
      : html`<div class="stack-lg">
          ${items.map((p) => (isQueue
            ? html`<${PickEditor} key=${p.id} pick=${p} focus=${focusId === p.id} selected=${selected.has(p.id)}
                onSelect=${(on) => setSelected((s) => { const n = new Set(s); if (on) n.add(p.id); else n.delete(p.id); return n; })}
                onDone=${(id) => removeLocal([id])} onSaved=${replaceLocal} onDirty=${(d) => markDirty(p.id, d)} />`
            : html`<${PickHistory} key=${p.id} pick=${p} />`))}
          <${Pager} total=${list.data.total} limit=${LIMIT} offset=${offset} onChange=${(o) => { setOffset(o); window.scrollTo(0, 0); }} />
        </div>`}
  </div>`;
}

function EmptyFor({ tab }) {
  if (tab === 'offen') {
    return html`<${Empty} icon="badge-check" title="Keine offenen Picks"
      action=${html`<${Button} variant="primary" href="#/uebersicht" icon="scan-search">Zur Übersicht<//>`}>
      Neue Vorschläge erscheinen hier nach dem nächsten Scan. Starte einen Scan auf der Übersicht oder clippe eine Video-URL direkt.<//>`;
  }
  const t = { freigegeben: 'Nichts in Arbeit', gerendert: 'Noch nichts gerendert', abgelehnt: 'Nichts abgelehnt' }[tab];
  const x = { freigegeben: 'Freigegebene Picks warten hier, bis ihr Render-Job fertig ist.', gerendert: 'Gerenderte Picks findest du auch unter Videos.', abgelehnt: 'Abgelehnte Picks bleiben hier zum Nachlesen.' }[tab];
  return html`<${Empty} icon="list-checks" title=${t}>${x}<//>`;
}

// ---------------------------------------------------------------------------------------------------------------
// Editable card for a proposed pick
// ---------------------------------------------------------------------------------------------------------------

function draftOf(pick) {
  const d = pick.data || {};
  const hooks = d.hooks || [];
  const titles = d.titles || [];
  const n = Math.max(hooks.length, titles.length);
  return {
    pairs: Array.from({ length: n }, (_, i) => ({ hook: hooks[i] || '', title: titles[i] || '' })),
    description: d.description || '',
    hashtags: d.hashtags || [],
    voiceover: d.voiceover || '',
    start: d.start ?? null,
    end: d.end ?? null,
    clip_type: d.clip_type || '',
    hook_style: d.hook_style || '',
    caption_style: d.caption_style || '',
  };
}

const keyOf = (draft) => JSON.stringify(draft);

function changesOf(draft, base) {
  const out = {};
  const hooks = draft.pairs.map((p) => p.hook.trim());
  const titles = draft.pairs.map((p) => p.title.trim());
  if (JSON.stringify(hooks) !== JSON.stringify(base.pairs.map((p) => p.hook))) out.hooks = hooks;
  if (JSON.stringify(titles) !== JSON.stringify(base.pairs.map((p) => p.title))) out.titles = titles;
  for (const k of ['description', 'voiceover', 'clip_type', 'hook_style', 'caption_style']) {
    if (draft[k] !== base[k]) out[k] = k === 'voiceover' ? (draft[k].trim() || null) : draft[k];
  }
  if (JSON.stringify(draft.hashtags) !== JSON.stringify(base.hashtags)) out.hashtags = draft.hashtags;
  for (const k of ['start', 'end']) if (draft[k] !== base[k] && draft[k] != null && !Number.isNaN(draft[k])) out[k] = draft[k];
  return out;
}

function PickEditor({ pick, focus, selected, onSelect, onDone, onSaved, onDirty }) {
  const base = useMemo(() => draftOf(pick), [pick]);
  const [draft, setDraft] = useState(base);
  const [report, setReport] = useState({ data: pick.check_report, forKey: keyOf(base) });
  const [busy, setBusy] = useState(null);
  const [saveError, setSaveError] = useState(null);
  const ref = useRef(null);
  useEffect(() => { setDraft(base); setReport({ data: pick.check_report, forKey: keyOf(base) }); }, [base]);
  useEffect(() => { if (focus && ref.current) ref.current.scrollIntoView({ behavior: 'smooth', block: 'start' }); }, [focus]);

  const draftKey = keyOf(draft);
  const isDirty = draftKey !== keyOf(base);
  useEffect(() => { onDirty(isDirty); }, [isDirty]);
  useEffect(() => () => onDirty(false), []);
  const check = readCheck(report.data);
  const reportCurrent = report.forKey === draftKey;
  // Footage fails depend on other clips (one rejected or deleted frees it): "Freigeben" checks them again on the
  // server, so they don't block the button; hook/title fails only an edit can fix.
  const footageOnly = check.failCount > 0 && check.fails.length === check.failCount && check.fails.every((f) => f.section === 'footage');
  const blocked = reportCurrent && check.failCount > 0 && !footageOnly;
  const emptyHook = draft.pairs.some((p) => !p.hook.trim());

  const set = (patch) => setDraft((d) => ({ ...d, ...patch }));
  const setPair = (i, patch) => setDraft((d) => ({ ...d, pairs: d.pairs.map((p, j) => (j === i ? { ...p, ...patch } : p)) }));
  const move = (i, dir) => setDraft((d) => {
    const pairs = [...d.pairs];
    const j = i + dir;
    if (j < 0 || j >= pairs.length) return d;
    [pairs[i], pairs[j]] = [pairs[j], pairs[i]];
    return { ...d, pairs };
  });

  const save = async () => {
    const changes = changesOf(draft, base);
    if (!Object.keys(changes).length) return pick;
    setSaveError(null);
    try {
      const res = await api.updatePick(pick.id, changes);
      setReport({ data: res.check_report, forKey: keyOf(draftOf(res.pick)) });
      onSaved(res.pick);
      return res.pick;
    } catch (err) {
      if (err.check_report) setReport({ data: err.check_report, forKey: draftKey });
      setSaveError(err);
      return null;
    }
  };
  const onSave = async () => {
    setBusy('save');
    const res = await save();
    setBusy(null);
    if (res) toast.success('Gespeichert – der Check ist sauber.');
  };
  const approve = async () => {
    setBusy('approve');
    try {
      if (isDirty && !(await save())) return;
      const res = await api.approvePick(pick.id);
      toast.success(`Freigegeben – Render-Job #${res.job_id} angelegt.`, { action: { href: `#/jobs?job=${res.job_id}`, label: 'Job ansehen' } });
      onDone(pick.id);
    } catch (err) {
      if (err.check_report) setReport({ data: err.check_report, forKey: draftKey });
      toast.fail(err, 'Freigabe nicht möglich');
    } finally {
      setBusy(null);
    }
  };
  const reject = async () => {
    const reason = await confirmDialog({
      title: 'Pick ablehnen?', confirmLabel: 'Ablehnen', danger: true,
      message: `„${plainHook(draft.pairs[0] && draft.pairs[0].hook) || 'Dieser Pick'}“ wird nicht gerendert.`,
      input: { label: 'Grund', optional: true, placeholder: 'z. B. Moment zu schwach, schon gepostet …' },
    });
    if (reason === false) return;
    setBusy('reject');
    try {
      await api.rejectPick(pick.id, reason ? { reason } : {});
      toast.success('Pick abgelehnt.');
      onDone(pick.id);
    } catch (err) {
      toast.fail(err);
    } finally {
      setBusy(null);
    }
  };

  const d = pick.data || {};
  const headingId = `pick-${pick.id}-h`;
  return html`<article class=${cx('card pick-card', selected && 'is-selected')} ref=${ref} aria-labelledby=${headingId} id=${`pick-${pick.id}`}>
    <${PickHead} pick=${pick} headingId=${headingId} selectable selected=${selected} onSelect=${onSelect} />
    ${pick.error && html`<div style="padding:0 18px"><${Callout} tone="warning" title="Zurück in der Freigabe">${pick.error}<//></div>`}
    <div class="pick-body">
      <aside class="pick-side">
        <${Sheets} sheets=${pick.sheets} title=${pick.job && pick.job.title} />
        <${Why} pick=${pick} />
        <${FxSummary} data=${d} />
      </aside>
      <div class="pick-main">
        <section class="stack">
          <div><h3 class="section-label">Hooks & Titel</h3><p class="muted small" style="margin-top:2px">Je Hook ein MP4, bester zuerst. Der Titel darunter gehört zu diesem Hook.</p></div>
          <ol class="hook-list" aria-label="Hooks und Titel, bester zuerst">
            ${draft.pairs.map((p, i) => html`<${HookRow} key=${i} i=${i} n=${draft.pairs.length} pair=${p} pick=${pick} check=${reportCurrent ? check : null}
              onChange=${(patch) => setPair(i, patch)} onMove=${(dir) => move(i, dir)}
              onRemove=${() => setDraft((x) => ({ ...x, pairs: x.pairs.filter((_, j) => j !== i) }))} />`)}
          </ol>
          <div class="row">
            <${Button} size="sm" icon="plus" disabled=${draft.pairs.length >= 8} onClick=${() => set({ pairs: [...draft.pairs, { hook: '', title: '' }] })}>Hook hinzufügen<//>
            <span class="muted small">2–6 Wörter, jede Variante mit anderer Struktur. Ein Schlüsselwort mit <code>*Sternchen*</code> markieren.</span>
          </div>
        </section>
        <${CheckSummary} check=${check} current=${reportCurrent} dirty=${isDirty} error=${saveError} footageOnly=${footageOnly} />
        <section class="stack">
          <h3 class="section-label">Text zum Posten</h3>
          <${TextareaField} label="Beschreibung" value=${draft.description} onInput=${(v) => set({ description: v })} rows=${3}
            help="Quellen-Credits und Hashtags hängt der Render automatisch an." />
          <${TagInput} label="Hashtags" value=${draft.hashtags} onChange=${(v) => set({ hashtags: v })} placeholder="#gta6"
            transform=${(t) => (t ? `#${t.replace(/^#+/, '').replace(/\s+/g, '')}` : '')} />
          <${TextField} label="Voiceover" optional value=${draft.voiceover} onInput=${(v) => set({ voiceover: v })}
            placeholder="Leer = kein Voiceover" help="Ein Satz, höchstens 10 Wörter. Wird von Fish Audio (oder der System-Stimme) gesprochen."
            maxLength=${120} showCount />
        </section>
        <details class="details">
          <summary><${Icon} name="chevron-right" size=${16} />Schnitt & Stil</summary>
          <div class="details-body">
            ${d.segments && d.segments.length
              ? html`<div class="stack-sm"><span class="field-label">Segmente (${d.segments.length})</span>
                  <ol class="small" style="margin:0;padding-left:18px">${d.segments.map((s) => html`<li>${fmt.clock(s.start)}–${fmt.clock(s.end)}${s.job ? html` <span class="muted">aus ${s.job}</span>` : ''}</li>`)}</ol>
                  <span class="muted small">Segment-Picks bearbeitest du hier nicht; lehne ab, wenn der Schnitt nicht passt.</span></div>`
              : html`<div class="form-grid">
                  <${TextField} label="Start (Sekunde im Quellvideo)" type="number" step="0.1" min="0" value=${draft.start ?? ''}
                    onInput=${(v) => set({ start: v === '' ? null : Number(v) })} help=${draft.start != null ? `= ${fmt.clock(draft.start)}` : ''} />
                  <${TextField} label="Ende (Sekunde im Quellvideo)" type="number" step="0.1" min="0" value=${draft.end ?? ''}
                    onInput=${(v) => set({ end: v === '' ? null : Number(v) })}
                    help=${draft.start != null && draft.end != null ? `= ${fmt.clock(draft.end)} · Länge ${fmt.num1(draft.end - draft.start)} s (max. 60 s)` : ''}
                    error=${draft.start != null && draft.end != null && (draft.end - draft.start > 60 || draft.end <= draft.start) ? 'Länge muss zwischen 0 und 60 s liegen.' : null} />
                </div>`}
            <div class="form-grid">
              <${SelectField} label="Clip-Typ" value=${draft.clip_type} onChange=${(v) => set({ clip_type: v })} options=${CLIP_TYPES} />
              <${SelectField} label="Hook-Stil" value=${draft.hook_style} onChange=${(v) => set({ hook_style: v })} options=${HOOK_STYLES} placeholder=${draft.hook_style ? undefined : 'Standard'} />
              <${SelectField} label="Untertitel-Stil" value=${draft.caption_style} onChange=${(v) => set({ caption_style: v })} options=${CAPTION_STYLES} placeholder=${draft.caption_style ? undefined : 'Standard'} />
            </div>
          </div>
        </details>
      </div>
    </div>
    <footer class="pick-foot">
      <span class="grow" role="status">${blocked ? html`<span style="color:var(--red)">Der Check hat Fehler – erst beheben, dann freigeben.</span>`
        : reportCurrent && footageOnly ? html`<span style="color:var(--red)">Szenen schon verwendet – „Freigeben“ prüft erneut.</span>`
        : emptyHook ? 'Leere Hooks bitte ausfüllen oder entfernen.'
        : isDirty ? 'Ungespeicherte Änderungen.' : `Vorgeschlagen ${fmt.dateTime(pick.created_at)}`}</span>
      ${isDirty && html`<${Button} variant="ghost" disabled=${!!busy} onClick=${() => { setDraft(base); setSaveError(null); setReport({ data: pick.check_report, forKey: keyOf(base) }); }}>Verwerfen<//>`}
      ${isDirty && html`<${Button} icon="check" busy=${busy === 'save'} disabled=${!!busy || emptyHook} onClick=${onSave}>Speichern<//>`}
      <${Button} variant="danger" icon="x" busy=${busy === 'reject'} disabled=${!!busy} onClick=${reject}>Ablehnen<//>
      <${Button} variant="approve" icon="badge-check" busy=${busy === 'approve'} disabled=${!!busy || blocked || emptyHook} onClick=${approve}>
        ${isDirty ? 'Speichern & freigeben' : 'Freigeben → Render'}<//>
    </footer>
  </article>`;
}

function PickHead({ pick, headingId, selectable, selected, onSelect }) {
  const now = useNow(60000);
  const d = pick.data || {};
  const job = pick.job || {};
  const exp = pick.source_expires_at;
  const left = exp ? exp - now : null;
  return html`<header class="pick-head">
    ${selectable && html`<label class="checkbox" style="margin-top:3px"><input type="checkbox" checked=${selected}
      onChange=${(e) => onSelect(e.currentTarget.checked)} aria-label=${`Pick #${pick.id} auswählen`} /></label>`}
    <div class="pick-head-main">
      <h2 class="pick-source" id=${headingId}>${job.title || 'Unbekannte Quelle'}</h2>
      <div class="pick-meta">
        ${job.channel && html`<span>${job.channel}</span><span aria-hidden="true">·</span>`}
        ${pick.source_link ? html`<a href=${pick.source_link} target="_blank" rel="noopener noreferrer" class="ext">${d.start != null ? `ab ${fmt.clock(d.start)}` : 'Quelle'} auf ${hostOf(pick.source_link)}</a>`
          : job.source_url ? html`<a href=${job.source_url} target="_blank" rel="noopener noreferrer" class="ext">Quelle</a>` : ''}
        <span aria-hidden="true">·</span><span>Job <a href=${`#/jobs?job=${pick.job_id}`}>#${pick.job_id}</a> · Pick ${pick.idx}</span>
        ${job.kind === 'live_chunk' && html`<${Badge} tone="neutral" icon="radio">Live-Chunk<//>`}
      </div>
      <div class="chips">
        <${Badge} tone="neutral" icon="clapperboard">${CLIP_TYPES[d.clip_type] || d.clip_type || 'ohne Typ'}<//>
        <${Badge} tone="neutral" icon="timer">${fmt.clock(pick.duration)}<//>
        ${d.segments && d.segments.length ? html`<${Badge} tone="neutral" icon="scissors">${d.segments.length} Segmente<//>` : ''}
        ${d.voiceover ? html`<${Badge} tone="neutral" icon="mic">Voiceover<//>` : ''}
        ${left != null && (left <= 0
          ? html`<${Badge} tone="red" icon="alert-circle">Quellvideo gelöscht<//>`
          : html`<${Badge} tone=${left < 3600 ? 'red' : left < 6 * 3600 ? 'yellow' : 'muted'} icon="clock" title=${`Quellvideo wird ${fmt.dateTime(exp)} gelöscht`}>Quelle läuft ab ${fmt.relative(exp, now)}<//>`)}
        ${pick.status !== 'proposed' && html`<${PickStatusBadge} status=${pick.status} />`}
      </div>
    </div>
  </header>`;
}

function Sheets({ sheets, title }) {
  const [open, setOpen] = useState(null);
  if (!sheets || !sheets.length) return html`<div class="sheets"><div class="video-ph" style="aspect-ratio:16/9;border-radius:8px">Kein Kontaktbogen</div></div>`;
  return html`<div class="sheets">
    ${sheets.map((src, i) => html`<button type="button" class="sheet-btn" key=${src} onClick=${() => setOpen(i)} aria-label=${`Kontaktbogen ${i + 1} vergrößern`}>
      <img src=${src} alt=${`Kontaktbogen ${i + 1}${title ? ` von „${title}“` : ''}`} loading="lazy" decoding="async" width="320" height="180" /></button>`)}
    ${open != null && html`<${Modal} title=${`Kontaktbogen ${open + 1} von ${sheets.length}`} size="xl" onClose=${() => setOpen(null)}
      footer=${sheets.length > 1 && html`<${Button} icon="chevron-left" disabled=${open === 0} onClick=${() => setOpen(open - 1)}>Vorheriger<//>
        <${Button} iconRight="chevron-right" disabled=${open === sheets.length - 1} onClick=${() => setOpen(open + 1)}>Nächster<//>`}>
      <img class="zoom-img" src=${sheets[open]} alt=${`Kontaktbogen ${open + 1}`} />
    <//>`}
  </div>`;
}

function Why({ pick }) {
  if (!pick.reason && !pick.click_trigger) return null;
  return html`<div class="why">
    <span class="why-label"><${Icon} name="sparkles" size=${14} />Claudes Begründung</span>
    ${pick.reason && html`<p>${pick.reason}</p>`}
    ${pick.click_trigger && html`<p><b>Klick-Auslöser:</b> ${pick.click_trigger}</p>`}
  </div>`;
}

function FxSummary({ data }) {
  const chips = [];
  if (data.layout && data.layout.type) chips.push(data.layout.type === 'split' ? 'Facecam oben (split)' : 'Vollbild (fit)');
  if (Array.isArray(data.fx)) chips.push(data.fx.length ? `${data.fx.length} Effekte` : 'keine Effekte');
  if (Array.isArray(data.music)) chips.push(data.music.length ? `Musik: ${[...new Set(data.music.map((m) => m.mood))].join(', ')}` : 'keine Musik');
  if (data.labels && data.labels.length) chips.push(`${data.labels.length} Labels`);
  if (data.caption_fixes && Object.keys(data.caption_fixes).length) chips.push(`${Object.keys(data.caption_fixes).length} Namensfixes`);
  if (!chips.length) return null;
  return html`<div class="stack-sm"><span class="section-label">Schnitt</span><div class="chips">${chips.map((c) => html`<${Badge} tone="muted">${c}<//>`)}</div></div>`;
}

function HookRow({ i, n, pair, pick, check, onChange, onMove, onRemove }) {
  const row = rankingFor(pick.simulate_report, pair.hook);
  const type = rowType(row);
  const issues = check ? check.hooks[pair.hook] || (check.hooks[pair.hook.trim()]) : null;
  const tIssues = check ? check.titles[pair.title] || check.titles[pair.title.trim()] : null;
  const fails = [...((issues && issues.fails) || []), ...((tIssues && tIssues.fails) || []).map((t) => `Titel: ${t}`)];
  const notes = [...((issues && issues.notes) || []), ...((tIssues && tIssues.notes) || []).map((t) => `Titel: ${t}`)];
  const changed = !row && pair.hook.trim();
  return html`<li class=${cx('hook-item', fails.length && 'has-fail')}>
    <span class="hook-rank" aria-label=${`Rang ${i + 1}`}>${i + 1}</span>
    <div class="hook-fields">
      <${AutoText} label=${`Hook ${i + 1}`} hideLabel value=${pair.hook} onInput=${(v) => onChange({ hook: v })} inputClass="hook-input"
        placeholder="Hook-Text, z. B. Nobody expected the hostages to *SING*" />
      <div class="hook-meta">
        ${type ? html`<${Badge} tone="neutral">${hookTypeLabel(type)}<//>` : changed && pick.simulate_report ? html`<${Badge} tone="muted">neu – Faktor nach dem Speichern<//>` : ''}
        ${row && row.x != null && html`<${Badge} tone=${factorTone(row.x)} title=${row.low != null ? `90 %-Bereich ${fmt.factor(row.low)}–${fmt.factor(row.high)}` : undefined}>
          ${fmt.factor(row.x)} vorhergesagt<//>`}
        ${row && row.wins != null && html`<span class="muted small">gewinnt ${Math.round(row.wins * 100)} % der Swipe-Tests</span>`}
        ${row && (row.why || []).includes('names a streamer') && html`<${Badge} tone="muted">+ Streamer-Name<//>`}
      </div>
      <div class="title-row">
        <${AutoText} label=${`Titel zu Hook ${i + 1}`} hideLabel value=${pair.title} onInput=${(v) => onChange({ title: v })}
          placeholder="Passender Titel (max. 100 Zeichen, mit „GTA 6“)" class="grow" />
        <span class=${cx('field-count', pair.title.length > 100 && 'is-over')} aria-label="Zeichen">${pair.title.length}/100</span>
      </div>
      ${fails.map((f) => html`<p class="issue issue-fail"><${Icon} name="alert-circle" size=${14} /><span>${f}</span></p>`)}
      ${notes.map((f) => html`<p class="issue issue-note"><${Icon} name="info" size=${14} /><span>${f}</span></p>`)}
    </div>
    <div class="hook-tools">
      <${IconButton} icon="arrow-up" size="sm" label="Nach oben" disabled=${i === 0} onClick=${() => onMove(-1)} />
      <${IconButton} icon="arrow-down" size="sm" label="Nach unten" disabled=${i === n - 1} onClick=${() => onMove(1)} />
      <${IconButton} icon="trash" size="sm" label="Hook entfernen" disabled=${n <= 1} onClick=${onRemove} />
    </div>
  </li>`;
}

function CheckSummary({ check, current, dirty, error, footageOnly }) {
  const general = [...check.fails.map((x) => ({ ...x, fail: true })), ...check.notes];
  if (!current && dirty) {
    return html`<div class="check-summary"><p class="issue" style="color:var(--text-2)"><${Icon} name="info" size=${14} />
      <span>Geändert – beim Speichern prüft der Server Hooks und Titel erneut (Check + Swipe-Test).</span></p></div>`;
  }
  if (!general.length && !check.failCount) {
    if (error && !error.check_report) return html`<${Callout} tone="danger" title=${error.message} />`;
    return html`<div class="check-summary"><p class="issue" style="color:var(--green)"><${Icon} name="circle-check" size=${14} /><span>Qualitäts-Check ohne Fehler.</span></p></div>`;
  }
  return html`<div class=${cx('check-summary', check.failCount && 'has-fail')} role=${check.failCount ? 'alert' : undefined}>
    <p class="issue" style=${check.failCount ? 'color:var(--red);font-weight:600' : 'font-weight:600'}>
      <${Icon} name=${check.failCount ? 'alert-circle' : 'info'} size=${14} />
      <span>${footageOnly ? 'Diese Szenen nutzt schon ein anderer Clip'
        : check.failCount ? `${fmt.plural(check.failCount, 'Fehler', 'Fehler')} im Qualitäts-Check (blockiert die Freigabe)` : 'Hinweise aus dem Qualitäts-Check'}${error && error.check_report ? ' – nicht gespeichert.' : ''}</span></p>
    ${general.map((g) => html`<p class=${cx('issue', g.fail ? 'issue-fail' : 'issue-note')}>
      <${Icon} name=${g.fail ? 'alert-circle' : 'info'} size=${14} /><span><span class="issue-label">${g.label}:</span> ${g.text}</span></p>`)}
    ${footageOnly && html`<p class="issue issue-note"><${Icon} name="info" size=${14} /><span>Dieselben Szenen zweimal zu posten bringt keine Reichweite. Lehne einen der beiden Clips ab oder ändere Start/Ende. „Freigeben“ prüft das erneut – ist der andere Clip inzwischen abgelehnt, geht es durch.</span></p>`}
  </div>`;
}

// ---------------------------------------------------------------------------------------------------------------
// History (approved / rendered / rejected)
// ---------------------------------------------------------------------------------------------------------------

function PickHistory({ pick }) {
  const d = pick.data || {};
  const hooks = d.hooks || [];
  return html`<article class="card pick-card" aria-labelledby=${`ph-${pick.id}`}>
    <${PickHead} pick=${pick} headingId=${`ph-${pick.id}`} />
    <div style="padding:14px 18px" class="compact-pick">
      ${pick.sheets && pick.sheets[0] && html`<img src=${pick.sheets[0]} alt="Kontaktbogen" loading="lazy" />`}
      <div class="stack-sm grow">
        <ol class="stack-sm" style="margin:0;padding-left:18px">
          ${hooks.map((h, i) => html`<li><span style="font-weight:600">${hookText(h)}</span>
            ${d.titles && d.titles[i] && html`<div class="muted small">${d.titles[i]}</div>`}</li>`)}
        </ol>
        ${pick.status === 'rejected' && html`<p class="small"><b>Abgelehnt</b> ${pick.decided_at ? fmt.dateTime(pick.decided_at) : ''}${pick.error ? ` – Grund: ${pick.error}` : ''}</p>`}
        ${pick.status === 'failed' && html`<${Callout} tone="danger" title="Render fehlgeschlagen">${pick.error || 'Details im Job-Log.'}<//>`}
        ${(pick.status === 'approved' || pick.status === 'rendering') && html`<p class="small muted">Freigegeben ${pick.decided_at ? fmt.dateTime(pick.decided_at) : ''} · ${pick.status === 'rendering' ? 'wird gerade gerendert' : 'wartet auf den Render-Job'}</p>`}
        <div class="row">
          ${pick.status === 'rendered' && html`<${Button} size="sm" icon="film" href=${pick.video_id ? `#/videos?video=${pick.video_id}` : '#/videos'}>Zum Video<//>`}
          ${(pick.status === 'approved' || pick.status === 'rendering' || pick.status === 'failed') && html`<${Button} size="sm" icon="list-todo" href=${`#/jobs?kind=render`}>Render-Jobs<//>`}
        </div>
      </div>
    </div>
  </article>`;
}
