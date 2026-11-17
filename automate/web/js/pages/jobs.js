// Jobs: grouped table with filters; detail drawer with event log (live while running), picks, children, retry/cancel.
import * as api from '../api.js';
import { html, useState, useEffect, useRef, useData, useTitle, fmt, cx, toast, setQuery, hookText, confirmDialog, hostOf } from '../lib.js';
import {
  PageHeader, Button, Badge, Icon, Tabs, Loading, ErrorState, Empty, SelectField, Modal, JobStatusBadge,
  PickStatusBadge, Rel, Pager, KV, Callout, Spinner, CopyButton,
} from '../ui.js';
import { JOB_GROUPS, JOB_KIND, JOB_STAGE } from '../labels.js';
import { overview, loadProfiles } from '../store.js';

const LIMIT = 50;
const ACTIVE = new Set(['queued', 'running', 'waiting_approval', 'waiting_budget', 'waiting_key']);

export function JobsPage({ route }) {
  useTitle('Jobs');
  const q = route.query;
  const group = JOB_GROUPS.some((g) => g.id === q.group) ? q.group : '';
  const kind = q.kind || '';
  const profile = q.profile || '';
  const openId = Number(q.job) || null;
  const [offset, setOffset] = useState(0);
  useEffect(() => setOffset(0), [group, kind, profile]);
  const list = useData(() => api.listJobs({ group: group || undefined, kind: kind || undefined, profile: profile || undefined, limit: LIMIT, offset }),
    [group, kind, profile, offset], { interval: 5000 });
  const profiles = useData(() => loadProfiles(), []);
  const items = (list.data && list.data.items) || [];
  const counts = (list.data && list.data.counts) || null;
  const open = (id) => setQuery({ job: id || '' });

  return html`<div class="page">
    <${PageHeader} title="Jobs" subtitle="Was der Worker gerade tut, was wartet und was schiefging." />
    <${Tabs} label="Gruppe" value=${group} onChange=${(id) => setQuery({ group: id })}
      tabs=${JOB_GROUPS.map((g) => ({ id: g.id, label: g.label,
        count: counts ? (g.id ? counts[g.id] : Object.values(counts).reduce((a, b) => a + b, 0)) : null,
        tone: g.id === 'failed' && counts && counts.failed ? 'red' : undefined }))} />
    <div class="filters">
      <${SelectField} label="Art" value=${kind} onChange=${(v) => setQuery({ kind: v })} placeholder="Alle Arten" options=${JOB_KIND} />
      ${(profiles.data || []).length > 1 && html`<${SelectField} label="Profil" value=${profile} onChange=${(v) => setQuery({ profile: v })}
        placeholder="Alle" options=${profiles.data.map((p) => ({ value: p.name, label: p.name }))} />`}
    </div>
    <div class="card card-flush">
      ${list.error && !list.data ? html`<div style="padding:16px"><${ErrorState} error=${list.error} onRetry=${list.reload} /></div>`
        : !list.data ? html`<div style="padding:16px"><${Loading} lines=${8} label="Lade Jobs …" /></div>`
        : !items.length ? html`<${Empty} icon="list-todo" title=${group || kind ? 'Keine Jobs in dieser Ansicht' : 'Noch keine Jobs'}>
            ${group || kind ? 'Wähle oben eine andere Gruppe oder Art.' : 'Jobs entstehen durch Scans, Live-Chunks, Freigaben und direkt geclippte URLs.'}<//>`
        : html`<div class="table-wrap jobs-table"><table class="table">
            <thead><tr><th>Job</th><th>Art</th><th>Status</th><th>Phase · Fortschritt</th><th class="th-num">Erstellt</th><th class="th-num">Dauer</th></tr></thead>
            <tbody>${items.map((j) => html`<tr class="is-clickable" key=${j.id} onClick=${() => open(j.id)}>
              <td class="td-title"><a href=${`#/jobs?job=${j.id}`} class="job-title" onClick=${(e) => { e.preventDefault(); e.stopPropagation(); open(j.id); }}>${j.title || hostOf(j.source_url) || `Job #${j.id}`}</a>
                <div class="muted small truncate">#${j.id}${j.channel ? ` · ${j.channel}` : ''}${j.profile ? ` · ${j.profile}` : ''}</div></td>
              <td class="nowrap">${JOB_KIND[j.kind] || j.kind}</td>
              <td><${JobStatusBadge} status=${j.status} />${j.attempts > 1 ? html`<div class="muted small">${j.attempts}. Versuch</div>` : ''}</td>
              <td><div class="small">${JOB_STAGE[j.stage] || '–'}</div>
                ${j.status === 'failed' && j.error ? html`<div class="small truncate" style="color:var(--red);max-width:320px">${j.error}</div>`
                  : j.progress && html`<div class="job-progress truncate" style="max-width:320px">${j.status === 'running' && html`<${Spinner} size=${11} />`}${j.progress}</div>`}</td>
              <td class="td-num muted small"><${Rel} ts=${j.created_at} /></td>
              <td class="td-num muted small">${duration(j)}</td>
            </tr>`)}</tbody></table></div>
          <div class="job-cards" style="padding:0 16px">${items.map((j) => html`<button type="button" class="job-card" key=${j.id} onClick=${() => open(j.id)}>
            <div class="row-between"><span class="job-title truncate">${j.title || `Job #${j.id}`}</span><${JobStatusBadge} status=${j.status} /></div>
            <span class="muted small">#${j.id} · ${JOB_KIND[j.kind] || j.kind} · ${JOB_STAGE[j.stage] || '–'} · <${Rel} ts=${j.created_at} /></span>
            ${(j.status === 'failed' && j.error) ? html`<span class="small" style="color:var(--red)">${j.error}</span>` : j.progress && html`<span class="job-progress">${j.progress}</span>`}
          </button>`)}</div>`}
      ${list.data && html`<div style="padding:0 16px 12px"><${Pager} total=${list.data.total} limit=${LIMIT} offset=${offset} onChange=${setOffset} /></div>`}
    </div>
    ${openId && html`<${JobDrawer} id=${openId} onClose=${() => open(null)} onChanged=${() => { list.reload(); overview.refresh(); }} />`}
  </div>`;
}

function duration(j) {
  if (!j.started_at) return '–';
  const end = j.finished_at || (j.status === 'running' ? Date.now() / 1000 : null);
  return end ? fmt.duration(end - j.started_at) : '–';
}

function JobDrawer({ id, onClose, onChanged }) {
  const [poll, setPoll] = useState(2000);
  const job = useData(() => api.getJob(id), [id], { interval: poll });
  const j = job.data;
  useEffect(() => { if (j) setPoll(ACTIVE.has(j.status) ? (j.status === 'running' ? 2000 : 8000) : 0); }, [j && j.status]);
  const [busy, setBusy] = useState(null);
  const act = async (what) => {
    if (what === 'cancel') {
      const ok = await confirmDialog({ title: `Job #${id} abbrechen?`, danger: true, confirmLabel: 'Abbrechen', cancelLabel: 'Weiterlaufen lassen',
        message: 'Laufende Schritte werden gestoppt. Du kannst den Job später erneut starten.' });
      if (!ok) return;
    }
    setBusy(what);
    try {
      const upd = what === 'retry' ? await api.retryJob(id) : await api.cancelJob(id);
      job.setData((x) => ({ ...x, ...upd }));
      setPoll(2000);
      toast.success(what === 'retry' ? `Job #${id} wird erneut versucht.` : `Job #${id} abgebrochen.`);
      onChanged();
    } catch (err) { toast.fail(err); } finally { setBusy(null); }
  };
  const canRetry = j && ['failed', 'canceled', 'skipped'].includes(j.status);
  const canCancel = j && ACTIVE.has(j.status);
  return html`<${Modal} drawer title=${j ? (j.title || `Job #${id}`) : `Job #${id}`} onClose=${onClose}
    footer=${j && html`${canCancel && html`<${Button} variant="danger" icon="ban" busy=${busy === 'cancel'} onClick=${() => act('cancel')}>Abbrechen<//>`}
      ${canRetry && html`<${Button} variant="primary" icon="rotate-ccw" busy=${busy === 'retry'} onClick=${() => act('retry')}>Erneut versuchen<//>`}
      <${Button} variant="ghost" onClick=${onClose}>Schließen<//>`}>
    ${job.error && !j ? html`<${ErrorState} error=${job.error} onRetry=${job.reload} />` : !j ? html`<${Loading} lines=${10} />` : html`
      <div class="row"><${JobStatusBadge} status=${j.status} /><${Badge} tone="muted">${JOB_KIND[j.kind] || j.kind}<//>
        <${Badge} tone="muted">${JOB_STAGE[j.stage] || '–'}<//>${j.profile && html`<${Badge} tone="muted">${j.profile}<//>`}</div>
      ${j.progress && ACTIVE.has(j.status) && html`<p class="job-progress">${j.status === 'running' && html`<${Spinner} size=${12} />`}${j.progress}</p>`}
      ${j.error && html`<${Callout} tone=${j.status === 'skipped' ? 'info' : 'danger'} title=${j.status === 'skipped' ? 'Übersprungen' : 'Fehler'}>${j.error}<//>`}
      ${j.status === 'waiting_key' && html`<${Callout} tone="warning" title="Wartet auf den Anthropic-Schlüssel" actions=${html`<${Button} size="sm" href="#/kosten">Eintragen<//>`}>Sobald der Schlüssel gespeichert ist, geht es automatisch weiter.<//>`}
      ${j.status === 'waiting_budget' && html`<${Callout} tone="warning" title="Tageslimit erreicht" actions=${html`<${Button} size="sm" href="#/kosten">Limit ändern<//>`}>Der Job läuft am nächsten Tag oder nach einer Erhöhung des Limits weiter.<//>`}
      <${KV} items=${[
        j.source_url && ['Quelle', html`<a href=${j.source_url} target="_blank" rel="noopener noreferrer" class="ext">${hostOf(j.source_url)}</a>`],
        j.channel && ['Kanal', j.channel],
        j.clipper_job && ['Ordner', html`<span class="row" style="gap:4px"><code>jobs/${j.clipper_job}</code><${CopyButton} text=${`jobs/${j.clipper_job}`} iconOnly label="Pfad kopieren" /></span>`],
        ['Erstellt', fmt.dateTime(j.created_at)],
        j.started_at && ['Gestartet', fmt.dateTime(j.started_at)],
        j.finished_at && ['Beendet', fmt.dateTime(j.finished_at)],
        ['Dauer', duration(j)],
        ['Versuche', String(j.attempts || 0)],
        j.parent_id && ['Übergeordnet', html`<a href=${`#/jobs?job=${j.parent_id}`}>Job #${j.parent_id}</a>`],
      ]} />
      ${(j.picks || []).length > 0 && html`<section class="stack-sm"><h3>Picks (${j.picks.length})</h3>
        <div class="list">${j.picks.map((p) => html`<div class="list-row" key=${p.id}>
          <div class="list-main"><span class="list-title">${hookText((p.data && p.data.hooks && p.data.hooks[0]) || 'Ohne Hook')}</span>
            <span class="list-sub">Pick ${p.idx} · ${fmt.clock(p.duration)}${p.reason ? ` · ${p.reason}` : ''}</span></div>
          <${PickStatusBadge} status=${p.status} />
          ${p.status === 'proposed' && html`<${Button} size="sm" href=${`#/freigabe?pick=${p.id}`}>Prüfen<//>`}
        </div>`)}</div></section>`}
      ${(j.children || []).length > 0 && html`<section class="stack-sm"><h3>Folge-Jobs</h3>
        <div class="list">${j.children.map((c) => html`<div class="list-row" key=${c.id}>
          <div class="list-main"><a class="list-title" href=${`#/jobs?job=${c.id}`}>#${c.id} · ${JOB_KIND[c.kind] || c.kind}</a>
            <span class="list-sub">${c.progress || c.error || JOB_STAGE[c.stage] || ''}</span></div>
          <${JobStatusBadge} status=${c.status} /></div>`)}</div></section>`}
      <${EventLog} events=${j.events || []} live=${j.status === 'running'} />`}
  <//>`;
}

function EventLog({ events, live }) {
  const ref = useRef(null);
  const stick = useRef(true);
  useEffect(() => {
    const el = ref.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [events.length]);
  const onScroll = () => {
    const el = ref.current;
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };
  return html`<section class="stack-sm">
    <div class="row-between"><h3>Protokoll</h3>
      <span class="muted small">${live ? html`<span class="row" style="gap:6px"><${Spinner} size=${11} />aktualisiert live</span>` : `${events.length} Einträge`}</span></div>
    ${events.length ? html`<div class="log" ref=${ref} onScroll=${onScroll} role="log" aria-live=${live ? 'polite' : 'off'} tabindex="0">
      ${events.map((e) => html`<div class=${cx('log-line', `log-${e.level}`)} key=${e.id}>
        <span class="log-ts">${new Date(e.ts * 1000).toLocaleTimeString('de-DE')}</span><span>${e.message}</span></div>`)}
    </div>` : html`<p class="muted">Noch keine Einträge.</p>`}
  </section>`;
}
