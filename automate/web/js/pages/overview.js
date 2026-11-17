// Übersicht: counters, automation switches, direct URL clipping, live recorders, alerts, costs, readiness, recent jobs.
import * as api from '../api.js';
import { html, useState, useData, useTitle, useAction, fmt, cx, toast, hookText, hostOf, navigate, useStore } from '../lib.js';
import {
  PageHeader, Card, Button, Toggle, TextField, SelectField, Badge, Icon, Callout, Loading, ErrorState, Empty,
  JobStatusBadge, Rel, Meter, Skeleton,
} from '../ui.js';
import { overview, loadProfiles, sessionStore } from '../store.js';
import { ALERT_LEVEL, JOB_KIND, JOB_STAGE, CLIP_TYPES } from '../labels.js';
import { PlatformIcon } from '../parts.js';

export function OverviewPage() {
  useTitle('Übersicht');
  const ov = overview.use(5000);
  const d = ov.data;
  const [scan, scanning] = useAction(async () => {
    const r = await api.scanNow({});
    toast.success(r.message || 'Suche gestartet.');
    overview.refresh();
  });
  const scanRunning = d && d.automation && d.automation.scan_running;
  return html`<div class="page">
    <${PageHeader} title="Übersicht" subtitle="Der Server clippt rund um die Uhr – hier steuerst du alles."
      actions=${html`<${Button} variant="primary" icon=${scanRunning ? undefined : 'scan-search'} busy=${scanning || scanRunning}
        onClick=${scan}>${scanRunning ? 'Suche läuft …' : 'Jetzt nach neuen Videos suchen'}<//>`} />
    ${ov.error && !d ? html`<${ErrorState} error=${ov.error} onRetry=${ov.reload} />` : !d ? html`<${OverviewSkeleton} />` : html`
      <${Alerts} alerts=${d.alerts || []} />
      <${Tiles} d=${d} />
      <div class="grid grid-main">
        <div class="stack-lg">
          <${AutomationCard} d=${d} />
          <${UrlCard} />
          <${RecentJobs} jobs=${d.recent_jobs || []} />
        </div>
        <div class="stack-lg">
          <${ReadyCard} d=${d} />
          <${PendingPicks} count=${d.approvals_pending} />
          <${LiveCard} live=${d.live} enabled=${d.automation && d.automation.live_enabled} />
        </div>
      </div>`}
  </div>`;
}

function OverviewSkeleton() {
  return html`<div class="tiles">${[1, 2, 3, 4, 5, 6].map((i) => html`<div class="tile" key=${i}><${Skeleton} lines=${2} /></div>`)}</div>
    <div class="grid grid-main"><div class="card"><${Loading} lines=${6} /></div><div class="card"><${Loading} lines=${6} /></div></div>`;
}

// ---------------------------------------------------------------------------------------------------------------

function Alerts({ alerts }) {
  const [busy, setBusy] = useState(null);
  const [showAcked, setShowAcked] = useState(false);
  const open = alerts.filter((a) => !a.acknowledged);
  const acked = alerts.filter((a) => a.acknowledged);
  const ack = async (a) => {
    setBusy(a.id);
    try { await api.ackAlert(a.id); await overview.refresh(); } catch (err) { toast.fail(err); } finally { setBusy(null); }
  };
  if (!alerts.length) return null;
  const list = showAcked ? [...open, ...acked] : open;
  return html`<section class="alert-list" aria-label="Health-Alarme">
    ${list.map((a) => {
      const lv = ALERT_LEVEL[a.level] || ALERT_LEVEL.warning;
      const link = alertLink(a);
      return html`<${Callout} key=${a.id} tone=${a.level === 'error' ? 'danger' : a.level === 'info' ? 'info' : 'warning'}
        class=${a.acknowledged ? 'is-acked' : ''} title=${a.message}
        actions=${html`${link && html`<${Button} size="sm" href=${link.href}>${link.label}<//>`}
          ${!a.acknowledged && html`<${Button} size="sm" variant="ghost" icon="check" busy=${busy === a.id} onClick=${() => ack(a)}>Bestätigen<//>`}`}>
        <span class="small">${lv.label} · aufgetreten <${Rel} ts=${a.first_seen} />${a.last_seen && a.last_seen - a.first_seen > 60 ? html` · zuletzt gesehen <${Rel} ts=${a.last_seen} />` : ''}${a.acknowledged ? ' · bestätigt' : ''}</span>
      <//>`;
    })}
    ${acked.length > 0 && html`<button type="button" class="linklike small" style="align-self:flex-start" onClick=${() => setShowAcked(!showAcked)}>
      ${showAcked ? 'Bestätigte Alarme ausblenden' : `${acked.length} bestätigte${acked.length === 1 ? 'r' : ''} Alarm${acked.length === 1 ? '' : 'e'} anzeigen`}</button>`}
  </section>`;
}

// Every alert key the backend raises (health.py, pipeline.py, youtube.py, api/auth.py) and where to fix it.
export function alertLink(a) {
  const k = a.key || '';
  if (k.startsWith('key.')) return { href: '#/kosten', label: 'Schlüssel eintragen' };
  if (k === 'anthropic.billing') return { href: '#/kosten', label: 'Kosten & Schlüssel' };
  if (k.startsWith('budget.')) return { href: '#/kosten', label: 'Tageslimit' };
  if (k.startsWith('tools.')) return { href: '#/system', label: 'Systemcheck' };
  if (k.startsWith('job.failed.')) return { href: `#/jobs?job=${k.split('.').pop()}`, label: 'Job ansehen' };
  if (k === 'live.service' || k === 'live.analysis_failed') return { href: '#/system', label: 'Systemcheck' };
  if (k.startsWith('live.')) return { href: '#/quellen', label: 'Streams' };
  if (k.startsWith('scan.failed.')) return { href: '#/system', label: 'Systemcheck' };
  if (k.startsWith('disk.')) return { href: '#/system', label: 'Speicher' };
  if (k.startsWith('youtube.')) return { href: '#/konten', label: 'Konten' };
  if (k.startsWith('pick.footage.')) return { href: `#/freigabe?pick=${k.split('.').pop()}`, label: 'Zur Freigabe' };
  if (k.startsWith('prices.')) return { href: '#/einstellungen/claude', label: 'Preise' };
  if (k === 'schedule.old' || k === 'service.restarted') return { href: '#/system', label: 'System' };
  if (k === 'login.recovery_code') return { href: '#/system', label: 'Passwort & 2FA' };
  return null;
}

function Tile({ label, value, sub, tone, href, icon }) {
  const zero = !value;
  const inner = html`<span class="tile-label">${icon && html`<${Icon} name=${icon} size=${15} />`}${label}</span>
    <span class=${cx('tile-value', zero && 'is-zero')}>${typeof value === 'number' ? fmt.num(value) : value}</span>
    ${sub && html`<span class="tile-sub">${sub}</span>`}`;
  return href ? html`<a class=${cx('tile', tone && !zero && `tone-${tone}`)} href=${href}>${inner}</a>` : html`<div class=${cx('tile', tone && `tone-${tone}`)}>${inner}</div>`;
}

function Tiles({ d }) {
  const j = d.jobs || {};
  const c = d.costs || {};
  const limit = c.limit_eur || 0;
  return html`<div class="tiles">
    <${Tile} label="Wartet" value=${j.waiting || 0} href="#/jobs?group=waiting" tone="neutral" icon="clock"
      sub=${j.waiting_approval ? `${j.waiting_approval} auf Freigabe` : 'in der Queue'} />
    <${Tile} label="Läuft" value=${j.running || 0} href="#/jobs?group=running" tone="running" icon="activity" sub="gerade in Arbeit" />
    <${Tile} label="Fertig heute" value=${j.done_today || 0} href="#/jobs?group=done" tone="green" icon="circle-check"
      sub=${`${fmt.num(d.videos_today || 0)} ${d.videos_today === 1 ? 'Video' : 'Videos'} gerendert`} />
    <${Tile} label="Fehler" value=${j.failed || 0} href="#/jobs?group=failed" tone="red" icon="alert-circle" sub=${j.failed ? 'ansehen und wiederholen' : 'alles in Ordnung'} />
    <${Tile} label="Offene Freigaben" value=${d.approvals_pending || 0} href="#/freigabe" tone="yellow" icon="badge-check" sub=${d.approvals_pending ? 'warten auf dich' : 'nichts zu tun'} />
    <a class="tile tone-orange" href="#/kosten">
      <span class="tile-label"><${Icon} name="euro" size=${15} />Claude heute</span>
      <span class="tile-value">${fmt.eur(c.today_eur || 0)}</span>
      ${limit > 0 ? html`<${Meter} value=${c.today_eur || 0} max=${limit} tone="orange" label="Kosten heute im Verhältnis zum Tageslimit" />
        <span class="tile-sub">von ${fmt.eur(limit)} Tageslimit · ${fmt.compact(c.tokens_today || 0)} Tokens</span>`
        : html`<span class="tile-sub">kein Tageslimit · ${fmt.compact(c.tokens_today || 0)} Tokens</span>`}
    </a>
  </div>`;
}

function AutomationCard({ d }) {
  const a = d.automation || {};
  const [busy, setBusy] = useState(null);
  const set = async (patch, key) => {
    setBusy(key);
    try {
      await api.setAutomation(patch);
      await overview.refresh();
      toast.success(key === 'enabled' ? `Automatik ${patch.enabled ? 'eingeschaltet' : 'ausgeschaltet'}.` : `Live-Aufnahme ${patch.live_enabled ? 'eingeschaltet' : 'ausgeschaltet'}.`);
    } catch (err) { toast.fail(err); } finally { setBusy(null); }
  };
  const last = a.last_scan;
  const summary = last && last.summary ? (last.summary.message || (last.summary.new_jobs != null ? `${fmt.plural(last.summary.new_jobs, 'neues Video', 'neue Videos')}` : '')) : '';
  return html`<${Card} title="Automatik" icon="power" class="automation-card" id="automatik"
    actions=${html`<${Button} size="sm" variant="ghost" icon="calendar-clock" href="#/einstellungen/zeitplan">Zeitplan<//>`}>
    <${Toggle} size="lg" checked=${!!a.enabled} busy=${busy === 'enabled'} onChange=${(v) => set({ enabled: v }, 'enabled')}
      label="Automatik (Scans nach Zeitplan)"
      description=${a.enabled ? 'Sucht nach Zeitplan nach neuen Videos und schlägt Clips vor.' : 'Aus: Es wird nur gesucht, wenn du es startest. Schaltest du sie nach der heutigen Suchzeit ein, startet die verpasste Suche sofort.'} />
    <div class="automation-meta">
      ${a.enabled && html`<span><${Icon} name="clock" size=${13} /> Nächste Suche: ${a.next_scan_at ? html`<b>${fmt.dateTime(a.next_scan_at)}</b> (<${Rel} ts=${a.next_scan_at} />)` : 'kein Zeitpunkt geplant'}</span>`}
      ${a.scan_running && html`<span><${Badge} tone="running" dot>Suche läuft<//></span>`}
      ${last ? html`<span>Letzte Suche <${Rel} ts=${last.at} />: ${last.ok ? '' : html`<b style="color:var(--red)">fehlgeschlagen</b> `}${summary}</span>` : html`<span>Noch keine Suche gelaufen.</span>`}
    </div>
    <div style="height:14px"></div>
    <${Toggle} size="lg" checked=${!!a.live_enabled} busy=${busy === 'live'} onChange=${(v) => set({ live_enabled: v }, 'live')}
      label="Live-Aufnahme" description=${a.live_enabled ? 'Prüft deine Streams alle paar Minuten und nimmt GTA-6-Streams auf. Ausschalten beendet laufende Aufnahmen.' : 'Aus: Live-Streams werden nicht aufgenommen.'} />
    ${a.live_enabled && html`<div class="automation-meta"><span>${d.live && d.live.recorders ? html`<span class="rec">${fmt.plural(d.live.recorders, 'Recorder nimmt', 'Recorder nehmen')} auf</span>` : 'Gerade nimmt kein Recorder auf.'}</span></div>`}
  <//>`;
}

function UrlCard() {
  const [url, setUrl] = useState('');
  const [section, setSection] = useState('');
  const [profile, setProfile] = useState('');
  const [error, setError] = useState(null);
  const profiles = useData(() => loadProfiles(), []);
  const enabled = (profiles.data || []).filter((p) => p.enabled);
  const [submit, busy] = useAction(async (e) => {
    e.preventDefault();
    setError(null);
    try {
      const job = await api.createJob({ url: url.trim(), section: section.trim() || undefined, profile: profile || undefined });
      setUrl(''); setSection('');
      toast.success(`Job #${job.id} angelegt – das Video wird geladen.`, { action: { href: `#/jobs?job=${job.id}`, label: 'Ansehen' } });
      overview.refresh();
    } catch (err) {
      if (err.fields) setError(err); else toast.fail(err);
    }
  });
  const fields = (error && error.fields) || {};
  return html`<${Card} title="Video-URL direkt clippen" icon="link" subtitle="Ein YouTube-Video oder eine Twitch-VOD sofort analysieren lassen (①–④).">
    <form class="stack" onSubmit=${submit} novalidate>
      <${TextField} label="Video-URL" hideLabel value=${url} onInput=${setUrl} type="url" inputmode="url"
        placeholder="https://www.youtube.com/watch?v=…" error=${fields.url} autocapitalize="off" spellcheck="false" />
      <div class="form-grid">
        <${TextField} label="Abschnitt" optional value=${section} onInput=${setSection} placeholder="0:00:00-1:30:00"
          help="Nur diesen Teil analysieren (bei langen Streams)." error=${fields.section} inputClass="mono" />
        ${enabled.length > 1 ? html`<${SelectField} label="Profil" value=${profile} onChange=${setProfile} placeholder="Standard"
          options=${enabled.map((p) => ({ value: p.name, label: p.name }))} />` : html`<div></div>`}
      </div>
      <div class="form-actions"><${Button} type="submit" variant="primary" icon="scissors" busy=${busy} disabled=${!url.trim()}>Clippen<//></div>
    </form>
  <//>`;
}

function ReadyCard({ d }) {
  const { state } = useStore(sessionStore);
  const r = d.ready || {};
  const a = d.automation || {};
  const items = [
    { name: 'Konto', ok: true, hint: state && state.username ? `angemeldet als ${state.username}` : '' },
    { name: '2FA', ok: !!(state && state.totp_enabled), hint: 'Zwei-Faktor-Anmeldung', href: '#/system', action: 'Einrichten' },
    { name: 'Anthropic-Schlüssel', ok: !!r.anthropic_key, hint: 'Pflicht: Claude wählt die Momente', href: '#/kosten', action: 'Eintragen' },
    { name: 'Fish-Audio-Schlüssel', ok: !!r.fish_key, optional: true, hint: 'optional: KI-Voiceover und Dubs', href: '#/kosten', action: 'Eintragen' },
    { name: 'Werkzeuge', ok: !!r.tools_ok, hint: r.tools_ok ? 'ffmpeg, yt-dlp, Transkription …' : `fehlt: ${(d.system && d.system.missing || []).join(', ') || 'siehe Systemcheck'}`, href: '#/system', action: 'Prüfen' },
    { name: 'Quellen', ok: (r.sources || 0) > 0, hint: r.sources ? `${fmt.num(r.sources)} aktive Creator und Streams` : 'noch keine aktive Quelle', href: '#/quellen', action: 'Hinzufügen' },
    { name: 'YouTube', ok: !!r.youtube_connected, optional: true, hint: 'optional: direkt hochladen', href: '#/konten', action: 'Verbinden' },
    { name: 'Automatik', ok: !!a.enabled, hint: a.enabled ? 'läuft nach Zeitplan' : 'noch aus', scroll: 'automatik', action: 'Einschalten' },
  ];
  const required = items.filter((i) => !i.optional);
  const done = required.filter((i) => i.ok).length;
  const allDone = done === required.length;
  const [open, setOpen] = useState(!allDone);
  return html`<${Card} title="Bereit zum Start" icon="list-checks" tone=${allDone ? undefined : 'yellow'}
    subtitle=${allDone ? 'Alles Nötige ist eingerichtet.' : `${done} von ${required.length} Pflichtpunkten erledigt`}
    actions=${allDone && html`<${Button} size="sm" variant="ghost" iconRight=${open ? 'chevron-down' : 'chevron-right'} aria-expanded=${open ? 'true' : 'false'} onClick=${() => setOpen(!open)}>${open ? 'Ausblenden' : 'Details'}<//>`}>
    ${open && html`<ul class="checklist">${items.map((i) => html`<li class="check-item" key=${i.name}>
      <span class=${cx('check-icon', i.ok ? 'ok' : i.optional ? 'optional' : 'todo')} aria-hidden="true"><${Icon} name=${i.ok ? 'check' : i.optional ? 'minus' : 'alert'} size=${13} /></span>
      <div class="check-text"><div class="check-name">${i.name}${i.optional && html` <span class="muted small">(optional)</span>`}
        <span class="sr-only">${i.ok ? ': erledigt' : ': offen'}</span></div><div class="check-hint">${i.hint}</div></div>
      ${!i.ok && (i.href || i.scroll) && html`<${Button} size="sm" variant=${i.optional ? 'ghost' : 'secondary'} href=${i.href}
        onClick=${i.scroll ? () => { const el = document.getElementById(i.scroll); el.scrollIntoView({ behavior: 'smooth' }); const sw = el.querySelector('[role=switch]'); if (sw) sw.focus({ preventScroll: true }); } : undefined}>${i.action}<//>`}
    </li>`)}</ul>`}
  <//>`;
}

function PendingPicks({ count }) {
  const picks = useData(() => (count ? api.listPicks({ status: 'proposed', limit: 3 }) : Promise.resolve({ items: [], total: 0 })), [count]);
  const items = picks.data ? api.itemsOf(picks.data) : [];
  return html`<${Card} title="Offene Freigaben" icon="badge-check" tone=${count ? 'yellow' : undefined}
    actions=${count > 0 && html`<${Button} size="sm" href="#/freigabe" iconRight="chevron-right">Alle ${count} ansehen<//>`}>
    ${!count ? html`<p class="muted">Keine Picks warten auf dich. Neue Vorschläge erscheinen nach dem nächsten Scan.</p>`
      : picks.loading && !picks.data ? html`<${Skeleton} lines=${3} />`
      : html`<div>${items.map((p) => html`<a class="mini-pick" href=${`#/freigabe?pick=${p.id}`} key=${p.id}>
          <span class="mini-hook">${hookText((p.data && p.data.hooks && p.data.hooks[0]) || 'Ohne Hook')}</span>
          <span class="muted small truncate">${CLIP_TYPES[p.data && p.data.clip_type] || ''} · ${fmt.clock(p.duration)} · ${(p.job && (p.job.channel || p.job.title)) || ''}</span>
        </a>`)}</div>`}
  <//>`;
}

function LiveCard({ live, enabled }) {
  const streams = [...((live && live.streams) || [])].sort((a, b) => (b.recording - a.recording) || (b.live - a.live) || a.name.localeCompare(b.name));
  return html`<${Card} title="Live-Recorder" icon="radio" subtitle=${enabled ? `${fmt.num((live && live.recorders) || 0)} nehmen gerade auf` : 'Live-Aufnahme ist aus'}
    actions=${html`<${Button} size="sm" variant="ghost" href="#/quellen" iconRight="chevron-right">Streams<//>`}>
    ${!streams.length ? html`<p class="muted">Keine Live-Streams eingetragen. Füge unter <a href="#/quellen">Quellen</a> z. B. <code>twitch:xqc</code> hinzu.</p>`
      : html`<div class="list">${streams.map((s) => html`<div class="list-row live-row" key=${s.id}>
          <${PlatformIcon} platform=${platformOf(s.url)} />
          <div class="list-main">
            <div class="list-title">${s.name}
              ${s.recording ? html`<span class="rec">nimmt auf</span>` : s.live ? html`<${Badge} tone="green">live<//>` : s.live === false ? html`<${Badge} tone="muted">offline<//>` : html`<${Badge} tone="muted">unbekannt<//>`}
              ${s.live && (s.on_topic ? html`<${Badge} tone="green">GTA 6<//>` : html`<${Badge} tone="neutral">anderes Thema<//>`)}
            </div>
            <span class="list-sub" title=${s.title || ''}>${s.title || hostOf(s.url)}${s.checked_at ? html` · geprüft <${Rel} ts=${s.checked_at} />` : ''}</span>
          </div>
        </div>`)}</div>`}
  <//>`;
}

export function platformOf(url) {
  const h = hostOf(url || '');
  if (h.includes('twitch')) return 'twitch';
  if (h.includes('kick')) return 'kick';
  if (h.includes('youtube') || h.includes('youtu.be')) return 'youtube';
  return 'other';
}

function RecentJobs({ jobs }) {
  return html`<${Card} title="Letzte Jobs" icon="list-todo" class="card-flush"
    actions=${html`<${Button} size="sm" variant="ghost" href="#/jobs" iconRight="chevron-right">Alle Jobs<//>`}>
    ${!jobs.length ? html`<${Empty} icon="list-todo" title="Noch keine Jobs">Starte oben eine Suche oder clippe eine Video-URL direkt.<//>`
      : html`<div class="job-cards" style="padding:0 18px">${jobs.map((j) => html`<button type="button" class="job-card" key=${j.id} onClick=${() => navigate(`/jobs?job=${j.id}`)}>
          <div class="row-between" style="flex-wrap:nowrap"><span class="job-title clamp-2">${j.title || `Job #${j.id}`}</span><${JobStatusBadge} status=${j.status} stage=${j.stage} /></div>
          <span class="muted small">#${j.id} · ${JOB_KIND[j.kind] || j.kind} · ${JOB_STAGE[j.stage] || '–'} · <${Rel} ts=${j.updated_at} /></span>
          ${(j.status === 'failed' && j.error) ? html`<span class="small clamp-2" style="color:var(--red)">${j.error}</span>` : j.progress && html`<span class="job-progress">${j.progress}</span>`}
        </button>`)}</div>
        <div class="table-wrap jobs-table"><table class="table">
          <thead><tr><th>Job</th><th>Status</th><th>Phase · Fortschritt</th></tr></thead>
          <tbody>${jobs.map((j) => html`<tr class="is-clickable" key=${j.id} onClick=${() => navigate(`/jobs?job=${j.id}`)}>
            <td class="td-title"><a href=${`#/jobs?job=${j.id}`} onClick=${(e) => e.stopPropagation()} class="job-title">${j.title || `Job #${j.id}`}</a>
              <div class="muted small">#${j.id} · ${JOB_KIND[j.kind] || j.kind}${j.channel ? ` · ${j.channel}` : ''}</div></td>
            <td class="nowrap"><${JobStatusBadge} status=${j.status} stage=${j.stage} /><div class="muted small"><${Rel} ts=${j.updated_at} /></div></td>
            <td><div class="small">${JOB_STAGE[j.stage] || '–'}</div>${(j.status === 'failed' ? j.error : j.progress) && html`<div class="small clamp-2" style=${j.status === 'failed' ? 'color:var(--red)' : 'color:var(--muted)'}>${j.status === 'failed' ? j.error : j.progress}</div>`}</td>
          </tr>`)}</tbody></table></div>`}
  <//>`;
}
