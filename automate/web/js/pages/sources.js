// Quellen: per profile the YouTube creators and the live streams (add, pause, priority, delete, live status).
import * as api from '../api.js';
import { html, useState, useEffect, useData, useTitle, cx, toast, setQuery, confirmDialog, hostOf } from '../lib.js';
import {
  PageHeader, Card, Button, IconButton, Badge, Loading, ErrorState, Empty, TextField, SelectField, Toggle, Rel,
  Segmented, FormError,
} from '../ui.js';
import { PlatformIcon } from '../parts.js';
import { loadProfiles, invalidateProfiles, overview } from '../store.js';

export function SourcesPage({ route }) {
  useTitle('Quellen');
  const profiles = useData(() => loadProfiles(true), []);
  const list = profiles.data || [];
  const name = route.query.profile || (list.find((p) => p.enabled) || list[0] || {}).name || '';
  const current = list.find((p) => p.name === name);
  const [busy, setBusy] = useState(false);
  const toggleProfile = async (enabled) => {
    setBusy(true);
    try {
      const upd = await api.updateProfile(name, { enabled });
      invalidateProfiles();
      profiles.setData((ps) => ps.map((p) => (p.name === upd.name ? { ...p, ...upd } : p)));
      toast.success(`Profil ${name} ${enabled ? 'aktiviert' : 'deaktiviert'}.`);
      overview.refresh();
    } catch (err) { toast.fail(err); } finally { setBusy(false); }
  };
  return html`<div class="page">
    <${PageHeader} title="Quellen" subtitle="Wessen Uploads gescannt und wessen Streams live aufgenommen werden. Jedes Video wird zusätzlich auf GTA 6 geprüft." />
    ${profiles.error && !profiles.data ? html`<${ErrorState} error=${profiles.error} onRetry=${profiles.reload} />`
      : !profiles.data ? html`<div class="card"><${Loading} lines=${4} /></div>` : html`
      <div class="card row-between">
        <div class="row">
          <span class="field-label">Profil</span>
          ${list.length <= 4
            ? html`<${Segmented} label="Profil" value=${name} onChange=${(v) => setQuery({ profile: v })}
                options=${list.map((p) => ({ value: p.name, label: p.name }))} />`
            : html`<${SelectField} label="Profil" hideLabel value=${name} onChange=${(v) => setQuery({ profile: v })}
                options=${list.map((p) => ({ value: p.name, label: p.name }))} />`}
        </div>
        ${current && html`<${Toggle} checked=${current.enabled} busy=${busy} onChange=${toggleProfile} label=${current.enabled ? 'Profil aktiv' : 'Profil inaktiv'}
          description=${current.enabled ? 'Wird bei Scans und von der Live-Aufnahme berücksichtigt.' : 'Wird nicht gescannt und nicht aufgenommen.'} />`}
      </div>
      ${name && html`<div class="grid grid-2" key=${name}>
        <${Creators} profile=${name} />
        <${Streams} profile=${name} />
      </div>`}`}
  </div>`;
}

function Priority({ value, onSave, label }) {
  const [v, setV] = useState(String(value));
  const [busy, setBusy] = useState(false);
  useEffect(() => setV(String(value)), [value]);
  const commit = async () => {
    const n = Number(v);
    if (!Number.isFinite(n) || n === value) { setV(String(value)); return; }
    setBusy(true);
    try { await onSave(Math.round(n)); } finally { setBusy(false); }
  };
  return html`<label class="row" style="gap:6px" title="Priorität: kleiner = zuerst">
    <span class="muted small">Priorität</span>
    <input class="input input-sm input-num" type="number" min="0" max="999" step="1" value=${v} aria-label=${label}
      disabled=${busy} onInput=${(e) => setV(e.currentTarget.value)} onBlur=${commit}
      onKeyDown=${(e) => { if (e.key === 'Enter') e.currentTarget.blur(); }} style="width:68px" />
  </label>`;
}

function useRows(fetcher, profile) {
  const data = useData(() => fetcher({ profile }).then(api.itemsOf), [profile]);
  const replace = (row) => data.setData((rows) => rows.map((r) => (r.id === row.id ? row : r)));
  const remove = (id) => data.setData((rows) => rows.filter((r) => r.id !== id));
  const add = (row) => data.setData((rows) => [...(rows || []), row]);
  return { ...data, replace, remove, add };
}

const sortRows = (rows) => [...rows].sort((a, b) => (a.paused - b.paused) || (a.priority - b.priority) || a.name.localeCompare(b.name));

function Creators({ profile }) {
  const rows = useRows(api.listCreators, profile);
  const [ref, setRef] = useState('');
  const [name, setName] = useState('');
  const [error, setError] = useState(null);
  const [adding, setAdding] = useState(false);
  const [busyId, setBusyId] = useState(null);
  const add = async (e) => {
    e.preventDefault();
    setAdding(true); setError(null);
    try {
      const c = await api.addCreator({ profile, ref: ref.trim(), name: name.trim() || undefined });
      rows.add(c); setRef(''); setName('');
      toast.success(`${c.name} hinzugefügt.`);
      invalidateProfiles();
    } catch (err) { setError(err); } finally { setAdding(false); }
  };
  const patch = async (c, body) => {
    setBusyId(c.id);
    try { rows.replace(await api.updateCreator(c.id, body)); } catch (err) { toast.fail(err); } finally { setBusyId(null); }
  };
  const del = async (c) => {
    if (!(await confirmDialog({ title: `${c.name} entfernen?`, danger: true, confirmLabel: 'Entfernen', message: 'Neue Uploads dieses Creators werden nicht mehr gescannt. Bereits erstellte Clips bleiben.' }))) return;
    setBusyId(c.id);
    try { await api.deleteCreator(c.id); rows.remove(c.id); toast.success(`${c.name} entfernt.`); invalidateProfiles(); } catch (err) { toast.fail(err); } finally { setBusyId(null); }
  };
  const fields = (error && error.fields) || {};
  const items = sortRows(rows.data || []);
  return html`<${Card} title="YouTube-Creator" icon="circle-play" subtitle=${rows.data ? `${items.filter((c) => !c.paused).length} aktiv · ${items.length} gesamt` : ''}>
    <form class="stack" onSubmit=${add}>
      <${FormError} error=${error && !error.fields ? error : null} />
      <div class="input-group" style="align-items:flex-start;flex-wrap:wrap">
        <${TextField} label="@Handle oder Kanal-URL" value=${ref} onInput=${setRef} placeholder="@KaiCenat" error=${fields.ref}
          autocapitalize="off" spellcheck="false" class="grow" />
        <${TextField} label="Name" optional value=${name} onInput=${setName} placeholder="automatisch" error=${fields.name} />
      </div>
      <div><${Button} type="submit" icon="plus" busy=${adding} disabled=${!ref.trim()}>Creator hinzufügen<//></div>
    </form>
    <hr />
    ${rows.error && !rows.data ? html`<${ErrorState} error=${rows.error} onRetry=${rows.reload} />`
      : !rows.data ? html`<${Loading} lines=${5} />`
      : !items.length ? html`<${Empty} icon="users" title="Noch keine Creator">Füge oben einen YouTube-Kanal hinzu, z. B. <code>@KaiCenat</code>.<//>`
      : html`<div class="list">${items.map((c) => html`<div class=${cx('src-row no-icon', c.paused && 'is-paused')} key=${c.id}>
          <div class="src-main">
            <div class="list-title">${c.name}${c.paused && html`<${Badge} tone="muted">pausiert<//>`}</div>
            <a class="list-sub" href=${c.url} target="_blank" rel="noopener noreferrer">${c.url.replace(/^https?:\/\/(www\.)?/, '')}</a>
          </div>
          <div class="src-side">
            <${Toggle} class="compact" checked=${!c.paused} label=${`${c.name} aktiv`} hideLabel stateText=${c.paused ? 'pausiert' : 'aktiv'} busy=${busyId === c.id}
              onChange=${(on) => patch(c, { paused: !on })} />
            <${IconButton} icon="trash" size="sm" label=${`${c.name} entfernen`} onClick=${() => del(c)} />
          </div>
          <div class="src-controls">
            <${SelectField} label=${`Filter für ${c.name}`} hideLabel value=${c.match || 'any'} onChange=${(v) => patch(c, { match: v })}
              options=${{ any: 'Jeder Upload wird auf GTA 6 geprüft', keywords: 'Nur mit Stichwort im Titel' }} />
            <${Priority} value=${c.priority} label=${`Priorität ${c.name}`} onSave=${(n) => patch(c, { priority: n })} />
          </div>
        </div>`)}</div>`}
  <//>`;
}

function Streams({ profile }) {
  const rows = useRows(api.listStreams, profile);
  const [ref, setRef] = useState('');
  const [topicOnly, setTopicOnly] = useState(true);
  const [error, setError] = useState(null);
  const [adding, setAdding] = useState(false);
  const [busyId, setBusyId] = useState(null);
  const add = async (e) => {
    e.preventDefault();
    setAdding(true); setError(null);
    try {
      const s = await api.addStream({ profile, ref: ref.trim(), topic_only: topicOnly });
      rows.add(s); setRef('');
      toast.success(`${s.name} hinzugefügt.`);
      invalidateProfiles();
    } catch (err) { setError(err); } finally { setAdding(false); }
  };
  const patch = async (s, body) => {
    setBusyId(s.id);
    try {
      rows.replace(await api.updateStream(s.id, body));
      if (body.paused && s.recording) toast.success(`${s.name} pausiert – die laufende Aufnahme wird beendet.`);
    } catch (err) { toast.fail(err); } finally { setBusyId(null); }
  };
  const del = async (s) => {
    if (!(await confirmDialog({ title: `${s.name} entfernen?`, danger: true, confirmLabel: 'Entfernen', message: 'Der Stream wird nicht mehr aufgenommen. Eine laufende Aufnahme wird sofort beendet; ihr angefangenes Stück wird gespeichert, aber nicht mehr geclippt.' }))) return;
    setBusyId(s.id);
    try { await api.deleteStream(s.id); rows.remove(s.id); toast.success(`${s.name} entfernt.`); invalidateProfiles(); } catch (err) { toast.fail(err); } finally { setBusyId(null); }
  };
  const fields = (error && error.fields) || {};
  const items = sortRows(rows.data || []);
  return html`<${Card} title="Live-Streams" icon="radio" subtitle=${rows.data ? `${items.filter((s) => s.recording).length} nehmen auf · ${items.filter((s) => !s.paused).length} aktiv` : ''}>
    <form class="stack" onSubmit=${add}>
      <${FormError} error=${error && !error.fields ? error : null} />
      <${TextField} label="Stream" value=${ref} onInput=${setRef} placeholder="twitch:xqc · kick:xqc · youtube:@KaiCenat · URL"
        error=${fields.ref} autocapitalize="off" spellcheck="false" inputClass="mono" />
      <label class="checkbox"><input type="checkbox" checked=${topicOnly} onChange=${(e) => setTopicOnly(e.currentTarget.checked)} />
        Nur aufnehmen, wenn der Stream-Titel GTA 6 nennt</label>
      <div><${Button} type="submit" icon="plus" busy=${adding} disabled=${!ref.trim()}>Stream hinzufügen<//></div>
    </form>
    <hr />
    ${rows.error && !rows.data ? html`<${ErrorState} error=${rows.error} onRetry=${rows.reload} />`
      : !rows.data ? html`<${Loading} lines=${5} />`
      : !items.length ? html`<${Empty} icon="radio" title="Noch keine Live-Streams">Füge oben z. B. <code>twitch:xqc</code> hinzu. Aufgenommen wird nur, wenn die Live-Aufnahme eingeschaltet ist.<//>`
      : html`<div class="list">${items.map((s) => html`<div class=${cx('src-row', s.paused && 'is-paused')} key=${s.id}>
          <${PlatformIcon} platform=${s.platform || 'other'} />
          <div class="src-main">
            <div class="list-title">${s.name}
              ${s.paused ? html`<${Badge} tone="muted">pausiert<//>`
                : s.recording ? html`<span class="rec">nimmt auf</span>`
                : s.live ? html`<${Badge} tone="green">live<//>` : s.live === false ? html`<${Badge} tone="muted">offline<//>` : ''}
              ${s.live && !s.paused && (s.on_topic ? html`<${Badge} tone="green">GTA 6<//>` : html`<${Badge} tone="neutral">anderes Thema<//>`)}
            </div>
            <span class="list-sub" title=${s.title || ''}>${s.title ? `„${s.title}“` : hostOf(s.url)}${s.checked_at ? html` · geprüft <${Rel} ts=${s.checked_at} />` : ' · noch nicht geprüft'}</span>
          </div>
          <div class="src-side">
            <${Toggle} class="compact" checked=${!s.paused} label=${`${s.name} aktiv`} hideLabel stateText=${s.paused ? 'pausiert' : 'aktiv'} busy=${busyId === s.id}
              onChange=${(on) => patch(s, { paused: !on })} />
            <${IconButton} icon="trash" size="sm" label=${`${s.name} entfernen`} onClick=${() => del(s)} />
          </div>
          <div class="src-controls">
            <label class="checkbox small"><input type="checkbox" checked=${!!s.topic_only} disabled=${busyId === `t${s.id}`}
              onChange=${async (e) => { const v = e.currentTarget.checked; setBusyId(`t${s.id}`); try { rows.replace(await api.updateStream(s.id, { topic_only: v })); } catch (err) { toast.fail(err); } finally { setBusyId(null); } }} />
              nur bei GTA-6-Titel aufnehmen</label>
            <${Priority} value=${s.priority} label=${`Priorität ${s.name}`} onSave=${(n) => patch(s, { priority: n })} />
          </div>
        </div>`)}</div>`}
  <//>`;
}
