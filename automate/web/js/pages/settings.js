// Einstellungen: schema-driven forms (GET /api/settings) plus dedicated editors for the watchlist, profile config,
// research/prompt and assets.
import * as api from '../api.js';
import { html, useState, useEffect, useMemo, useData, useTitle, fmt, cx, toast, confirmDialog, useId } from '../lib.js';
import {
  PageHeader, Card, Button, IconButton, Badge, Icon, Loading, ErrorState, Empty, TextareaField,
  Toggle, Segmented, TagInput, Callout, describedBy, CopyButton,
} from '../ui.js';
import { overview, loadProfiles } from '../store.js';

const SECTIONS = [
  { id: 'zeitplan', label: 'Zeitplan & Automatik', icon: 'calendar-clock', group: 'automation' },
  { id: 'clips', label: 'Clips', icon: 'clapperboard', group: 'clips' },
  { id: 'freigabe', label: 'Freigabe', icon: 'badge-check', group: 'approval' },
  { id: 'claude', label: 'Claude', icon: 'sparkles', group: 'claude' },
  { id: 'analyse', label: 'Analyse', icon: 'activity' },
  { id: 'profil', label: 'Profil', icon: 'folder' },
  { id: 'recherche', label: 'Recherche & Prompt', icon: 'file-text' },
  { id: 'assets', label: 'Assets', icon: 'music' },
];
const KNOWN_GROUPS = new Set(SECTIONS.map((s) => s.group).filter(Boolean));

const INTRO = {
  zeitplan: 'Wann gesucht, aufgenommen und geprüft wird. Änderungen gelten ab dem nächsten Takt, ohne Neustart.',
  clips: 'Was pro Lauf entsteht: Voiceover, wie viele Videos und wie viele Hook-Varianten.',
  freigabe: 'Ob Claudes Picks erst in die Freigabe-Queue kommen oder direkt gerendert werden.',
  claude: 'Modell, Denktiefe und Grenzen für den Claude-API-Agenten (Schritt ③ und ⑥).',
  analyse: 'Was in einem Video als Highlight zählt (watchlist.yaml). Gespeichert wird nur, was du vom Repo-Standard abweichst.',
  profil: 'Themenfilter, Stichwörter, Hashtags und Suchen eines Profils (profiles/<name>/profile.yaml).',
  recherche: 'Das Playbook ist Claudes System-Prompt für Schritt ③. Du kannst es ersetzen oder ergänzen.',
  assets: 'Schriften und Musik, die der Render verwenden darf.',
  weitere: 'Einstellungen, für die es (noch) keine eigene Seite gibt.',
};

export function SettingsPage({ route }) {
  useTitle('Einstellungen');
  const settings = useData(() => api.getSettings(), []);
  const extraGroups = useMemo(() => {
    const schema = (settings.data && settings.data.schema) || {};
    return [...new Set(Object.values(schema).filter((s) => !s.hidden && !KNOWN_GROUPS.has(s.group)).map((s) => s.group))];
  }, [settings.data]);
  const sections = extraGroups.length ? [...SECTIONS, { id: 'weitere', label: 'Weitere', icon: 'settings' }] : SECTIONS;
  const section = sections.find((s) => s.id === route.parts[1]) || sections[0];
  const merge = (values) => settings.setData((d) => ({ ...d, values: { ...d.values, ...values } }));

  let body;
  if (section.group || section.id === 'weitere') {
    if (settings.error && !settings.data) body = html`<${ErrorState} error=${settings.error} onRetry=${settings.reload} />`;
    else if (!settings.data) body = html`<div class="card"><${Loading} lines=${8} /></div>`;
    else {
      const groups = section.group ? [section.group] : extraGroups;
      body = html`${section.id === 'zeitplan' && html`<${NextScan} />`}
        <${SettingsForm} key=${section.id} groups=${groups} data=${settings.data} onSaved=${merge} radioCards=${section.id === 'freigabe'} />`;
    }
  } else if (section.id === 'analyse') body = html`<${WatchlistEditor} />`;
  else if (section.id === 'profil') body = html`<${ProfileEditor} />`;
  else if (section.id === 'recherche') body = html`<${ResearchEditor} />`;
  else body = html`<${AssetsEditor} />`;

  return html`<div class="page">
    <${PageHeader} title="Einstellungen" />
    <div class="settings-layout">
      <nav class="subnav" aria-label="Einstellungsbereiche">
        ${sections.map((s) => html`<a href=${`#/einstellungen/${s.id}`} aria-current=${s.id === section.id ? 'page' : undefined} key=${s.id}>
          <${Icon} name=${s.icon} size=${16} />${s.label}</a>`)}
      </nav>
      <div class="stack-lg" style="min-width:0">
        <div><h2 style="font-size:17px">${section.label}</h2><p class="muted" style="margin-top:4px">${INTRO[section.id]}</p></div>
        ${body}
      </div>
    </div>
  </div>`;
}

function NextScan() {
  const ov = overview.use(30000).data;
  if (!ov || !ov.automation) return null;
  const a = ov.automation;
  return html`<${Callout} tone=${a.enabled ? 'info' : 'warning'} title=${a.enabled
    ? (a.next_scan_at ? `Nächste Suche: ${fmt.dateTime(a.next_scan_at)}` : 'Automatik an, aber keine Suchzeit geplant')
    : 'Die Automatik ist aus – geplante Suchen laufen erst, wenn du sie einschaltest.'}
    actions=${html`<${Button} size="sm" href="#/uebersicht">Zur Übersicht<//>`} />`;
}

// ---------------------------------------------------------------------------------------------------------------
// Generic schema-driven form
// ---------------------------------------------------------------------------------------------------------------

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function SettingsForm({ groups, data, onSaved, radioCards }) {
  const entries = Object.entries(data.schema || {}).filter(([, s]) => !s.hidden && groups.includes(s.group));
  const [draft, setDraft] = useState(() => Object.fromEntries(entries.map(([k]) => [k, data.values[k]])));
  const [errors, setErrors] = useState({});
  const [formError, setFormError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [jsonBad, setJsonBad] = useState({});
  const changed = entries.filter(([k]) => !same(draft[k], data.values[k])).map(([k]) => k);
  const save = async () => {
    setBusy(true); setErrors({}); setFormError(null);
    try {
      const res = await api.updateSettings(Object.fromEntries(changed.map((k) => [k, draft[k]])));
      onSaved(res.values || Object.fromEntries(changed.map((k) => [k, draft[k]])));
      toast.success('Einstellungen gespeichert.');
      if (changed.some((k) => k.startsWith('automation.') || k.startsWith('schedule.'))) overview.refresh();
    } catch (err) {
      if (err.fields) setErrors(err.fields);
      setFormError(err);
    } finally { setBusy(false); }
  };
  if (!entries.length) return html`<div class="card"><${Empty} icon="settings" title="Keine Einstellungen in diesem Bereich" /></div>`;
  const basic = entries.filter(([, s]) => !s.advanced);
  const advanced = entries.filter(([, s]) => s.advanced);
  const row = ([k, s]) => html`<${SettingRow} key=${k} k=${k} s=${s} value=${draft[k]} dirty=${changed.includes(k)} error=${errors[k]} radioCards=${radioCards}
    onChange=${(v) => setDraft((d) => ({ ...d, [k]: v }))} onJsonValid=${(ok) => setJsonBad((j) => ({ ...j, [k]: !ok }))}
    onReset=${s.default !== undefined && !same(draft[k], s.default) ? () => setDraft((d) => ({ ...d, [k]: s.default })) : null} />`;
  const invalid = Object.values(jsonBad).some(Boolean);
  return html`<div class="card"><div class="setting-list">${basic.map(row)}</div>
    ${advanced.length > 0 && html`<details class="details" style="margin-top:8px"><summary><${Icon} name="chevron-right" size=${16} />Erweitert</summary>
      <div class="setting-list">${advanced.map(row)}</div></details>`}
  </div>
  ${(changed.length > 0 || formError) && html`<div class="savebar" role="region" aria-label="Ungespeicherte Änderungen">
    <span class="grow">${formError ? html`<span style="color:var(--red)">${formError.message}</span>` : `${fmt.plural(changed.length, 'Änderung', 'Änderungen')} nicht gespeichert`}</span>
    <${Button} variant="ghost" disabled=${busy} onClick=${() => { setDraft(Object.fromEntries(entries.map(([k]) => [k, data.values[k]]))); setErrors({}); setFormError(null); }}>Verwerfen<//>
    <${Button} variant="primary" icon="check" busy=${busy} disabled=${!changed.length || invalid} onClick=${save}>Speichern<//>
  </div>`}`;
}

function SettingRow({ k, s, value, onChange, dirty, error, onReset, radioCards, onJsonValid }) {
  const auto = useId();
  const id = `set${auto}`;
  const wide = s.type === 'text' || s.type === 'json' || (radioCards && s.type === 'choice');
  return html`<div class=${cx('setting', dirty && 'is-dirty', wide && 'is-wide')}>
    <div class="setting-text">
      <label class="setting-label" for=${id} id=${`${id}-l`}>${s.label || k}</label>
      ${s.help && html`<p class="setting-help" id=${`${id}-help`}>${s.help}</p>`}
      <span class="setting-key">${k}</span>
    </div>
    <div class="setting-control">
      <${SettingControl} id=${id} s=${s} value=${value} onChange=${onChange} error=${error} radioCards=${radioCards} onJsonValid=${onJsonValid} />
      ${error && html`<p class="field-error" id=${`${id}-err`} role="alert"><${Icon} name="alert-circle" size=${14} />${error}</p>`}
      ${onReset && html`<button type="button" class="linklike small" style="align-self:flex-start" onClick=${onReset}>Standard: ${showValue(s.default, s)}</button>`}
    </div>
  </div>`;
}

function showValue(v, s) {
  if (v === 'auto') return 'automatisch';
  if (v === true) return 'an';
  if (v === false) return 'aus';
  if (v == null) return '–';
  if (Array.isArray(v)) return v.length ? v.join(', ') : 'leer';
  if (s && s.choices) { const c = s.choices.find((x) => (typeof x === 'object' ? x.value : x) === v); if (c) return typeof c === 'object' ? c.label : c; }
  if (typeof v === 'object') return 'JSON';
  return `${v}${s && s.unit ? ` ${s.unit}` : ''}`;
}

const normChoices = (choices) => (choices || []).map((c) => (typeof c === 'object' ? c : { value: c, label: String(c) }));

function SettingControl({ id, s, value, onChange, error, radioCards, onJsonValid }) {
  const db = describedBy(id, s.help, error);
  const labelled = `${id}-l`;
  switch (s.type) {
    case 'boolean':
      return html`<${Toggle} id=${id} checked=${!!value} onChange=${onChange} label=${s.label} hideLabel stateText=${value ? 'an' : 'aus'} />`;
    case 'integer':
    case 'number': {
      const isAuto = s.auto && value === 'auto';
      const num = html`<div class="row" style="gap:6px">
        <input id=${id} class="input input-num" type="number" min=${s.min} max=${s.max} step=${s.step || (s.type === 'integer' ? 1 : 'any')}
          value=${isAuto ? '' : value ?? ''} disabled=${isAuto} aria-describedby=${db} aria-invalid=${error ? 'true' : undefined}
          onInput=${(e) => { const v = e.currentTarget.value; onChange(v === '' ? null : s.type === 'integer' ? parseInt(v, 10) : parseFloat(v)); }} />
        ${s.unit && html`<span class="muted">${s.unit}</span>`}
        ${(s.min != null || s.max != null) && html`<span class="muted small">${s.min ?? '…'}–${s.max ?? '…'}</span>`}
      </div>`;
      if (!s.auto) return num;
      return html`<div class="stack-sm">
        <${Segmented} size="sm" label=${s.label} value=${isAuto ? 'auto' : 'fixed'}
          onChange=${(m) => onChange(m === 'auto' ? 'auto' : (typeof s.default === 'number' ? s.default : s.min ?? 1))}
          options=${[{ value: 'auto', label: 'Automatisch' }, { value: 'fixed', label: 'Fester Wert' }]} />
        ${!isAuto && num}
      </div>`;
    }
    case 'choice': {
      const choices = normChoices(s.choices);
      if (radioCards || choices.some((c) => c.help)) {
        return html`<div class="radio-cards" role="radiogroup" aria-labelledby=${labelled}>
          ${choices.map((c) => html`<label class="radio-card" key=${String(c.value)}>
            <input type="radio" name=${id} checked=${value === c.value} onChange=${() => onChange(c.value)} />
            <span><span class="radio-card-title">${c.label}</span>${c.help && html`<br /><span class="radio-card-help">${c.help}</span>`}</span>
          </label>`)}</div>`;
      }
      if (choices.length <= 3 && choices.every((c) => String(c.label).length <= 18)) {
        return html`<${Segmented} label=${s.label} value=${value} onChange=${onChange} options=${choices} />`;
      }
      return html`<div class="select-wrap"><select id=${id} class="input select" value=${String(value ?? '')} aria-describedby=${db}
        onChange=${(e) => { const c = choices.find((x) => String(x.value) === e.currentTarget.value); onChange(c ? c.value : e.currentTarget.value); }}>
        ${choices.map((c) => html`<option value=${String(c.value)}>${c.label}</option>`)}</select>
        <${Icon} name="chevron-down" size=${16} class="select-caret" /></div>`;
    }
    case 'text':
      return html`<textarea id=${id} class="input textarea" rows="5" value=${value ?? ''} aria-describedby=${db} onInput=${(e) => onChange(e.currentTarget.value)}></textarea>`;
    case 'string_list':
      return html`<${TagInput} id=${id} value=${Array.isArray(value) ? value : []} onChange=${onChange} suggestions=${normChoices(s.choices).map((c) => String(c.value))} />`;
    case 'time_list':
      return html`<${TimeList} id=${id} value=${Array.isArray(value) ? value : []} onChange=${onChange} />`;
    case 'json':
      return html`<${JsonField} id=${id} value=${value} onChange=${onChange} onValid=${onJsonValid} />`;
    default:
      return html`<input id=${id} class="input" value=${value ?? ''} aria-describedby=${db} onInput=${(e) => onChange(e.currentTarget.value)} />`;
  }
}

function TimeList({ id, value, onChange }) {
  const [add, setAdd] = useState('');
  return html`<div class="stack-sm">
    <div class="time-list">
      ${value.map((t, i) => html`<span class="time-item" key=${i}>
        <input class="input input-sm" type="time" value=${t} aria-label=${`Uhrzeit ${i + 1}`}
          onInput=${(e) => onChange(value.map((x, j) => (j === i ? e.currentTarget.value : x)))} />
        <${IconButton} icon="x" size="sm" label=${`${t} entfernen`} onClick=${() => onChange(value.filter((_, j) => j !== i))} /></span>`)}
      ${!value.length && html`<span class="muted small">Keine Uhrzeit – es wird nicht automatisch gesucht.</span>`}
    </div>
    <div class="row" style="gap:6px">
      <input id=${id} class="input input-sm" type="time" value=${add} onInput=${(e) => setAdd(e.currentTarget.value)} aria-label="Neue Uhrzeit" style="width:120px" />
      <${Button} size="sm" icon="plus" disabled=${!add || value.includes(add)} onClick=${() => { onChange([...value, add].sort()); setAdd(''); }}>Uhrzeit hinzufügen<//>
    </div>
  </div>`;
}

function JsonField({ id, value, onChange, onValid, rows = 8 }) {
  const [text, setText] = useState(() => JSON.stringify(value ?? null, null, 2));
  const [bad, setBad] = useState(null);
  useEffect(() => {
    try { if (!same(JSON.parse(text), value)) setText(JSON.stringify(value ?? null, null, 2)); } catch { /* keep the user's text */ }
  }, [JSON.stringify(value)]);
  return html`<div class="stack-sm">
    <textarea id=${id} class="input textarea mono" rows=${rows} value=${text} spellcheck="false" aria-invalid=${bad ? 'true' : undefined}
      onInput=${(e) => {
        const t = e.currentTarget.value;
        setText(t);
        try { const v = JSON.parse(t); setBad(null); onValid && onValid(true); onChange(v); } catch (err) { setBad('Ungültiges JSON.'); onValid && onValid(false); }
      }}></textarea>
    ${bad && html`<p class="field-error"><${Icon} name="alert-circle" size=${14} />${bad}</p>`}
  </div>`;
}

// ---------------------------------------------------------------------------------------------------------------
// Config documents (watchlist, profile): typed form for known keys + raw JSON overrides
// ---------------------------------------------------------------------------------------------------------------

const getPath = (obj, path) => path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
function setPath(obj, path, v) {
  const keys = path.split('.');
  const out = { ...obj };
  let cur = out;
  keys.slice(0, -1).forEach((k) => { cur[k] = { ...(cur[k] || {}) }; cur = cur[k]; });
  cur[keys[keys.length - 1]] = v;
  return out;
}

/** Overrides = current overrides with every known field set when it differs from base, removed when equal. */
function buildOverrides(prev, form, base, fields) {
  let out = JSON.parse(JSON.stringify(prev || {}));
  const tops = new Set(fields.map((f) => f.path.split('.')[0]));
  for (const top of tops) {
    const nested = fields.filter((f) => f.path.startsWith(`${top}.`));
    // Nested fields (weights.*) are written as the whole dict: the overlay merges dicts one level deep anyway.
    const formTop = nested.length
      ? { ...(base[top] || {}), ...Object.fromEntries(nested.map((f) => [f.path.slice(top.length + 1), getPath(form, f.path)])) }
      : getPath(form, top);
    if (same(formTop, base[top])) delete out[top]; else out = { ...out, [top]: formTop };
  }
  return out;
}

function ConfigEditor({ load, save, fields, sections, deps, extra }) {
  const doc = useData(load, deps);
  const [form, setForm] = useState(null);
  const [json, setJson] = useState(null);
  const [jsonOk, setJsonOk] = useState(true);
  const [errors, setErrors] = useState({});
  const [formError, setFormError] = useState(null);
  const [busy, setBusy] = useState(null);
  useEffect(() => {
    if (!doc.data) return;
    setForm(Object.fromEntries(fields.map((f) => [f.path, getPath(doc.data.effective, f.path)])));
    setJson(doc.data.overrides || {});
  }, [doc.data]);
  if (doc.error && !doc.data) return html`<${ErrorState} error=${doc.error} onRetry=${doc.reload} />`;
  if (!doc.data || !form) return html`<div class="card"><${Loading} lines=${10} /></div>`;
  const base = doc.data.base || {};
  const formObj = fields.reduce((o, f) => setPath(o, f.path, form[f.path]), {});
  const fromForm = buildOverrides(doc.data.overrides, formObj, base, fields);
  const jsonChanged = !same(json, doc.data.overrides || {});
  const formChanged = !same(fromForm, doc.data.overrides || {});
  const next = jsonChanged ? json : fromForm;
  const dirty = jsonChanged || formChanged;

  const put = async (overrides, what) => {
    setBusy(what); setErrors({}); setFormError(null);
    try {
      const res = await save(overrides);
      doc.setData(res);
      toast.success(what === 'reset' ? 'Auf Standard zurückgesetzt.' : 'Gespeichert.');
    } catch (err) {
      if (err.fields) setErrors(err.fields);
      setFormError(err);
    } finally { setBusy(null); }
  };
  const reset = async () => {
    if (!(await confirmDialog({ title: 'Auf Standard zurücksetzen?', confirmLabel: 'Zurücksetzen', danger: true,
      message: 'Alle Abweichungen vom Repo-Standard in diesem Bereich werden gelöscht.' }))) return;
    put({}, 'reset');
  };
  const set = (path, v) => setForm((f) => ({ ...f, [path]: v }));
  const field = (f) => {
    const baseV = getPath(base, f.path);
    const overridden = !same(form[f.path], baseV);
    return html`<${ConfigField} key=${f.path} f=${f} value=${form[f.path]} onChange=${(v) => set(f.path, v)} error=${errors[f.path]}
      overridden=${overridden} baseValue=${baseV} onReset=${() => set(f.path, baseV)} disabled=${jsonChanged} />`;
  };
  const nOverrides = Object.keys(doc.data.overrides || {}).length;
  return html`${extra && extra(form, set, base)}
    ${sections.map((sec) => html`<${Card} title=${sec.title} subtitle=${sec.subtitle} key=${sec.title}>
      ${sec.render ? sec.render({ form, set, base, errors, disabled: jsonChanged }) : html`<div class="setting-list">${fields.filter((f) => f.section === sec.id).map(field)}</div>`}
    <//>`)}
    <${Card} title="Erweitert: Abweichungen als JSON" icon="terminal" subtitle=${nOverrides ? `${fmt.plural(nOverrides, 'Schlüssel weicht', 'Schlüssel weichen')} vom Repo-Standard ab` : 'Keine Abweichungen – es gilt der Repo-Standard.'}
      actions=${html`<${Button} size="sm" variant="danger" icon="rotate-ccw" busy=${busy === 'reset'} disabled=${!nOverrides || !!busy} onClick=${reset}>Auf Standard zurücksetzen<//>`}>
      <details class="details"><summary><${Icon} name="chevron-right" size=${16} />JSON bearbeiten (für Schlüssel ohne eigenes Feld)</summary>
        <div class="details-body">
          <p class="muted small">Nur die Abweichungen. Objekte werden eine Ebene tief mit dem Standard zusammengeführt, alles andere ersetzt. Solange hier etwas geändert ist, sind die Felder oben gesperrt.</p>
          <${JsonField} value=${json} onChange=${setJson} onValid=${setJsonOk} rows=${12} />
          <details class="details"><summary><${Icon} name="chevron-right" size=${16} />Repo-Standard ansehen</summary>
            <pre class="playbook" style="max-height:320px">${JSON.stringify(base, null, 2)}</pre></details>
        </div>
      </details>
    <//>
    ${(dirty || formError) && html`<div class="savebar" role="region" aria-label="Ungespeicherte Änderungen">
      <span class="grow">${formError ? html`<span style="color:var(--red)">${formError.message}</span>` : jsonChanged ? 'JSON geändert – nicht gespeichert' : 'Änderungen nicht gespeichert'}</span>
      <${Button} variant="ghost" disabled=${!!busy} onClick=${() => { doc.setData({ ...doc.data }); setErrors({}); setFormError(null); }}>Verwerfen<//>
      <${Button} variant="primary" icon="check" busy=${busy === 'save'} disabled=${!dirty || !jsonOk || !!busy} onClick=${() => put(next, 'save')}>Speichern<//>
    </div>`}`;
}

function ConfigField({ f, value, onChange, error, overridden, baseValue, onReset, disabled }) {
  const auto = useId();
  const id = `cf${auto}`;
  let control;
  const numInput = (v, set, extraAttrs = {}) => html`<input id=${id} class="input input-num" type="number" min=${f.min} max=${f.max} step=${f.step || 'any'}
    value=${v ?? ''} disabled=${disabled} onInput=${(e) => set(e.currentTarget.value === '' ? null : Number(e.currentTarget.value))} ...${extraAttrs} />`;
  switch (f.kind) {
    case 'bool': control = html`<${Toggle} id=${id} checked=${!!value} onChange=${onChange} label=${f.label} hideLabel stateText=${value ? 'an' : 'aus'} disabled=${disabled} />`; break;
    case 'choice': control = html`<${Segmented} label=${f.label} value=${value} onChange=${onChange} options=${f.choices} />`; break;
    case 'tags': control = html`<${TagInput} id=${id} value=${value || []} onChange=${onChange} mono=${f.mono} transform=${f.transform} placeholder=${f.placeholder} />`; break;
    case 'autonum': {
      const isAuto = value === 'auto';
      control = html`<div class="row" style="gap:8px"><${Segmented} size="sm" label=${f.label} value=${isAuto ? 'auto' : 'fixed'}
          onChange=${(m) => onChange(m === 'auto' ? 'auto' : (typeof baseValue === 'number' ? baseValue : f.fallback ?? f.min ?? 1))}
          options=${[{ value: 'auto', label: 'Auto' }, { value: 'fixed', label: 'Wert' }]} />
        ${!isAuto && numInput(value, onChange)}${!isAuto && f.unit && html`<span class="muted">${f.unit}</span>`}</div>`;
      break;
    }
    case 'nullnum': {
      const off = value == null;
      control = html`<div class="row" style="gap:8px">${!off && numInput(value, onChange)}${!off && f.unit && html`<span class="muted">${f.unit}</span>`}
        <label class="checkbox small"><input type="checkbox" checked=${off} disabled=${disabled} onChange=${(e) => onChange(e.currentTarget.checked ? null : (baseValue ?? f.fallback ?? 1))} />${f.nullLabel}</label></div>`;
      break;
    }
    case 'percent':
      control = html`<div class="row" style="gap:8px"><input id=${id} type="range" min="0" max="1" step="0.01" value=${value ?? 0} disabled=${disabled}
        onInput=${(e) => onChange(Number(e.currentTarget.value))} style="flex:1;accent-color:var(--green)" />
        <span class="num" style="width:44px;text-align:right">${Math.round((value || 0) * 100)} %</span></div>`;
      break;
    case 'kv': control = html`<${KvEditor} value=${value || {}} onChange=${onChange} disabled=${disabled} />`; break;
    default: control = html`<div class="row" style="gap:6px">${numInput(value, onChange)}${f.unit && html`<span class="muted">${f.unit}</span>`}</div>`;
  }
  return html`<div class=${cx('setting', overridden && 'is-dirty', (f.kind === 'tags' || f.kind === 'kv') && f.wide && 'is-wide')}>
    <div class="setting-text"><label class="setting-label" for=${id}>${f.label}</label>
      ${f.help && html`<p class="setting-help">${f.help}</p>`}<span class="setting-key">${f.path}</span></div>
    <div class="setting-control">${control}
      ${error && html`<p class="field-error" role="alert"><${Icon} name="alert-circle" size=${14} />${error}</p>`}
      ${overridden && html`<span class="base-hint">Weicht ab · Standard: ${fmtBase(baseValue)} <button type="button" class="linklike" onClick=${onReset} disabled=${disabled}>zurücksetzen</button></span>`}
    </div>
  </div>`;
}

function fmtBase(v) {
  if (v === undefined) return 'nicht gesetzt';
  if (v === null) return 'aus';
  if (Array.isArray(v)) return v.length > 4 ? `${v.slice(0, 4).join(', ')} … (${v.length})` : v.join(', ') || 'leer';
  if (typeof v === 'object') return `${Object.keys(v).length} Einträge`;
  if (typeof v === 'boolean') return v ? 'an' : 'aus';
  return String(v);
}

function KvEditor({ value, onChange, disabled }) {
  const rows = Object.entries(value);
  const [k, setK] = useState('');
  const [v, setV] = useState('');
  const rename = (oldKey, newKey) => onChange(Object.fromEntries(rows.map(([a, b]) => (a === oldKey ? [newKey, b] : [a, b]))));
  return html`<div class="kv-edit">
    ${rows.map(([a, b]) => html`<div class="kv-edit-row" key=${a}>
      <input class="input input-sm" value=${a} aria-label="Falsch erkannt" disabled=${disabled} onChange=${(e) => rename(a, e.currentTarget.value.trim())} />
      <span class="arrow" aria-hidden="true">→</span>
      <input class="input input-sm" value=${b} aria-label=${`Richtig für ${a}`} disabled=${disabled} onInput=${(e) => onChange({ ...value, [a]: e.currentTarget.value })} />
      <${IconButton} icon="x" size="sm" label=${`${a} entfernen`} disabled=${disabled} onClick=${() => { const n = { ...value }; delete n[a]; onChange(n); }} />
    </div>`)}
    <div class="kv-edit-row">
      <input class="input input-sm" value=${k} placeholder="falsch (z. B. trever)" aria-label="Neues falsches Wort" disabled=${disabled} onInput=${(e) => setK(e.currentTarget.value)} />
      <span class="arrow" aria-hidden="true">→</span>
      <input class="input input-sm" value=${v} placeholder="richtig (Trevor)" aria-label="Richtige Schreibweise" disabled=${disabled} onInput=${(e) => setV(e.currentTarget.value)} />
      <${IconButton} icon="plus" size="sm" label="Hinzufügen" disabled=${disabled || !k.trim() || !v.trim()} onClick=${() => { onChange({ ...value, [k.trim().toLowerCase()]: v.trim() }); setK(''); setV(''); }} />
    </div>
  </div>`;
}

// ---------------------------------------------------------------------------------------------------------------
// Analyse (watchlist.yaml)
// ---------------------------------------------------------------------------------------------------------------

const WEIGHTS = [
  ['weights.loudness', 'Lautheit', 'Spitzen über dem normalen Pegel des Videos'],
  ['weights.jump', 'Sprung', 'Plötzlich lauter: Schreie, Reaktionen'],
  ['weights.heatmap', 'Replay', 'YouTubes „Meistgesehen“-Kurve, wenn vorhanden'],
  ['weights.keywords', 'Hype-Wörter', 'Hype-Wörter und Geräusche wie (screaming)'],
  ['weights.speech_rate', 'Tempo', 'Schnelles Sprechen = Aufregung'],
];

const WATCH_FIELDS = [
  ...WEIGHTS.map(([path, label]) => ({ path, label, section: 'weights', kind: 'num' })),
  { path: 'hype_words', label: 'Hype-Wörter', section: 'words', kind: 'tags', wide: true, placeholder: 'Wort oder Phrase …', help: 'Zählen zum Hype-Signal, wenn sie im Transkript vorkommen.' },
  { path: 'target_seconds', label: 'Ziel-Länge', section: 'length', kind: 'autonum', unit: 's', min: 5, max: 60, fallback: 30, help: 'Auto: Reaktionen ~22 s, Info-Clips ~45 s (aus der Recherche).' },
  { path: 'min_seconds', label: 'Mindestlänge', section: 'length', kind: 'autonum', unit: 's', min: 3, max: 60, fallback: 9 },
  { path: 'max_seconds', label: 'Höchstlänge', section: 'length', kind: 'autonum', unit: 's', min: 5, max: 60, fallback: 58, help: 'Shorts dürfen höchstens 60 s lang sein.' },
  { path: 'max_candidates', label: 'Kandidaten pro Video', section: 'length', kind: 'autonum', min: 3, max: 25, fallback: 10, help: 'Auto: etwa einer pro 3 Minuten (3–25).' },
  { path: 'max_source_minutes', label: 'Längste Quelle', section: 'length', kind: 'num', unit: 'Min.', min: 1, max: 600, help: 'Längere Uploads überspringt die Suche.' },
  { path: 'max_recorders', label: 'Gleichzeitige Recorder', section: 'live', kind: 'num', min: 1, max: 50, help: 'Über alle Profile; Priorität entscheidet, wer aufgenommen wird.' },
  { path: 'live_workers', label: 'Parallele Transkriptionen', section: 'live', kind: 'autonum', min: 1, max: 16, fallback: 2, help: 'Auto: eine pro ~4 CPU-Kerne.' },
  { path: 'transcribe_mode', label: 'Transkription live', section: 'live', kind: 'choice', choices: [{ value: 'auto', label: 'Auto' }, { value: 'full', label: 'Komplett' }, { value: 'hot', label: 'Nur laute 35 %' }], help: 'Auto: nur die lautesten Stellen, wenn viel aufgenommen wird.' },
  { path: 'hot_when_recorders', label: '„Nur laut“ ab Recordern', section: 'live', kind: 'num', min: 1, max: 50 },
  { path: 'live_fast', label: 'Schnelles Modell für Live', section: 'live', kind: 'bool', help: 'Nur bei whisper.cpp: Live-Chunks mit dem schnellen Modell.' },
  { path: 'keep_live_sources_hours', label: 'Live-Video aufbewahren', section: 'keep', kind: 'nullnum', unit: 'Std.', min: 0, nullLabel: 'nie löschen', fallback: 3, help: 'Nach dem Clippen; Zeit für Multi-Streamer-Edits.' },
  { path: 'keep_sources_days', label: 'Andere Quellvideos aufbewahren', section: 'keep', kind: 'nullnum', unit: 'Tage', min: 0, nullLabel: 'nie löschen', fallback: 2, help: 'Transkripte, Picks und Exporte bleiben immer.' },
  { path: 'min_free_gb', label: 'Alarm unter freiem Speicher', section: 'keep', kind: 'num', unit: 'GB', min: 1, help: 'Darunter: Alarm, und neue Downloads und Live-Aufnahmen warten, bis wieder Platz ist.' },
  { path: 'caption_style', label: 'Untertitel-Stil (Standard)', section: 'style', kind: 'choice', choices: [{ value: 'bold', label: 'Fett' }, { value: 'boxed', label: 'Box' }, { value: 'comic', label: 'Comic' }] },
  { path: 'censor_captions', label: 'Schimpfwörter in Untertiteln maskieren', section: 'style', kind: 'bool', help: 'F*CK statt des Worts; der Ton bleibt unverändert.' },
  { path: 'no_hook_share', label: 'Anteil ohne Hook (A/B-Test)', section: 'style', kind: 'percent', help: 'So viele Clips gehen ohne Hook-Text raus, zum Vergleich.' },
];

function Weights({ form, set, base, errors, disabled }) {
  const sum = WEIGHTS.reduce((s, [p]) => s + (Number(form[p]) || 0), 0);
  const ok = Math.abs(sum - 1) < 0.005;
  // whole percents that add up to exactly 100 (largest remainder): plain rounding gave e.g. 36/18/18/18/9 = 99 %
  const normalize = () => {
    if (sum <= 0) return;
    const raw = WEIGHTS.map(([p]) => ((Number(form[p]) || 0) / sum) * 100);
    const pct = raw.map(Math.floor);
    const order = raw.map((r, i) => [r - pct[i], i]).sort((a, b) => b[0] - a[0]);
    for (let k = 0; k < 100 - pct.reduce((a, b) => a + b, 0); k += 1) pct[order[k][1]] += 1;
    WEIGHTS.forEach(([p], i) => set(p, pct[i] / 100));
  };
  return html`<div class="weights">
    ${WEIGHTS.map(([p, label, help]) => html`<div class="weight" key=${p}>
      <label for=${`w-${p}`}><span style="font-weight:600">${label}</span><br /><span class="muted small">${help}</span></label>
      <input id=${`w-${p}`} type="range" min="0" max="1" step="0.05" value=${form[p] ?? 0} disabled=${disabled}
        onInput=${(e) => set(p, Number(e.currentTarget.value))} aria-valuetext=${`${Math.round((form[p] || 0) * 100)} Prozent`} />
      <span class="weight-val">${Math.round((form[p] || 0) * 100)} %</span>
      ${errors[p] && html`<p class="field-error" style="grid-column:1/-1">${errors[p]}</p>`}
    </div>`)}
    <div class="weight-sum" role="status">
      <span style=${ok ? 'color:var(--green)' : 'color:var(--yellow)'}>Summe ${Math.round(sum * 100)} %${ok ? ' ✓' : ' – sollte 100 % sein'}</span>
      ${!ok && html`<${Button} size="sm" onClick=${normalize} disabled=${disabled}>Auf 100 % verteilen<//>`}
      ${!same(WEIGHTS.map(([p]) => form[p]), WEIGHTS.map(([p]) => getPath(base, p))) && html`<${Button} size="sm" variant="ghost" disabled=${disabled}
        onClick=${() => WEIGHTS.forEach(([p]) => set(p, getPath(base, p)))}>Standard (${WEIGHTS.map(([p]) => Math.round((getPath(base, p) || 0) * 100)).join('/')})<//>`}
    </div>
  </div>`;
}

function WatchlistEditor() {
  return html`<${ConfigEditor} deps=${[]} load=${() => api.getWatchlist()} save=${(overrides) => api.putWatchlist({ overrides })} fields=${WATCH_FIELDS}
    sections=${[
      { id: 'weights', title: 'Signal-Gewichte', subtitle: 'Wie stark jedes Signal in den Score pro Sekunde eingeht (Summe 100 %).', render: (p) => html`<${Weights} ...${p} />` },
      { id: 'words', title: 'Hype-Wörter' },
      { id: 'length', title: 'Cliplängen & Kandidaten' },
      { id: 'live', title: 'Live-Limits' },
      { id: 'keep', title: 'Aufbewahrung & Speicher' },
      { id: 'style', title: 'Untertitel & A/B-Test' },
    ]} />`;
}

// ---------------------------------------------------------------------------------------------------------------
// Profil (profiles/<name>/profile.yaml)
// ---------------------------------------------------------------------------------------------------------------

const hashtag = (t) => (t ? `#${t.replace(/^#+/, '').replace(/\s+/g, '')}` : '');
const PROFILE_FIELDS = [
  { path: 'require_topic', label: 'Nur Videos über GTA 6', section: 'topic', kind: 'bool', help: 'Titel (oder Beschreibung/Tags) muss das Thema belegen.' },
  { path: 'topic_patterns', label: 'Themen-Muster', section: 'topic', kind: 'tags', mono: true, wide: true, placeholder: 'Regulärer Ausdruck …', help: 'Ein Treffer im Titel zählt als „über GTA 6“ (reguläre Ausdrücke, Groß/klein egal).' },
  { path: 'exclude_title_patterns', label: 'Ausschluss-Muster', section: 'topic', kind: 'tags', mono: true, wide: true, placeholder: 'Regulärer Ausdruck …', help: 'Titel mit diesen Mustern werden verworfen (z. B. GTA 5, Memes).' },
  { path: 'keywords', label: 'Stichwörter', section: 'topic', kind: 'tags', wide: true, help: 'Für Creator mit Filter „Nur mit Stichwort im Titel“.' },
  { path: 'search', label: 'Zusätzliche YouTube-Suchen', section: 'find', kind: 'tags', wide: true, placeholder: 'Suchbegriff …', help: 'Findet Videos außerhalb deiner Creator-Liste.' },
  { path: 'hashtags', label: 'Hashtags', section: 'post', kind: 'tags', wide: true, transform: hashtag, placeholder: '#gta6', help: 'Standard-Hashtags für jeden Clip dieses Profils.' },
  { path: 'caption_fixes', label: 'Namensfixes', section: 'post', kind: 'kv', wide: true, help: 'Falsch erkannte Namen in Untertiteln korrigieren (ganze Wörter, Groß/klein egal).' },
  { path: 'vocabulary', label: 'Vokabular für die Transkription', section: 'post', kind: 'tags', wide: true, help: 'Namen, die whisper.cpp richtig schreiben soll.' },
];

function ProfileEditor() {
  const profiles = useData(() => loadProfiles(), []);
  const [name, setName] = useState('');
  const list = profiles.data || [];
  const current = name || (list.find((p) => p.enabled) || list[0] || {}).name;
  if (profiles.error && !profiles.data) return html`<${ErrorState} error=${profiles.error} onRetry=${profiles.reload} />`;
  if (!profiles.data) return html`<div class="card"><${Loading} lines=${4} /></div>`;
  if (!current) return html`<div class="card"><${Empty} icon="folder" title="Keine Profile gefunden">Profile sind Ordner in <code>profiles/</code> im Repo.<//></div>`;
  return html`<div class="card row">
      <span class="field-label">Profil</span>
      <${Segmented} label="Profil" value=${current} onChange=${setName} options=${list.map((p) => ({ value: p.name, label: `${p.name}${p.enabled ? '' : ' (inaktiv)'}` }))} />
    </div>
    <${ConfigEditor} key=${current} deps=${[current]} load=${() => api.getProfileConfig(current)}
      save=${(overrides) => api.putProfileConfig(current, { overrides })} fields=${PROFILE_FIELDS}
      sections=${[{ id: 'topic', title: 'Themenfilter' }, { id: 'find', title: 'Suchen' }, { id: 'post', title: 'Posten & Untertitel' }]} />`;
}

// ---------------------------------------------------------------------------------------------------------------
// Recherche & Prompt
// ---------------------------------------------------------------------------------------------------------------

function ResearchEditor() {
  const doc = useData(() => api.getResearch(), []);
  const [override, setOverride] = useState(null);
  const [extra, setExtra] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => { if (doc.data) { setOverride(doc.data.override); setExtra(doc.data.extra_instructions || ''); } }, [doc.data]);
  if (doc.error && !doc.data) return html`<${ErrorState} error=${doc.error} onRetry=${doc.reload} />`;
  if (!doc.data) return html`<div class="card"><${Loading} lines=${10} /></div>`;
  const d = doc.data;
  const dirty = override !== d.override || extra !== (d.extra_instructions || '');
  const save = async () => {
    setBusy(true); setError(null);
    try {
      const res = await api.putResearch({ override, extra_instructions: extra });
      doc.setData(res);
      toast.success('Prompt gespeichert. Gilt ab dem nächsten Claude-Aufruf.');
    } catch (err) { setError(err); } finally { setBusy(false); }
  };
  return html`<${Card} title="Systemprompt" icon="sparkles" tone="orange"
      subtitle=${`${fmt.num(d.system_prompt_chars)} Zeichen (≈ ${fmt.num(Math.round(d.system_prompt_chars / 4))} Tokens). Das Playbook wird per Prompt-Cache zwischengespeichert.`}>
      <p class="text-2">Claude bekommt: Arbeitsregeln (fest) → Playbook (unten, gecacht) → deine Zusatzanweisungen. Änderungen am Playbook machen den Cache einmalig neu.</p>
    <//>
    <${Card} title="Recherche-Playbook" icon="file-text" subtitle="research/viral-gta-shorts.md – die Grundlage für Hooks, Titel und Clip-Typen."
      actions=${html`<${CopyButton} text=${d.playbook} label="Kopieren" />`}>
      <details class="details"><summary><${Icon} name="chevron-right" size=${16} />Playbook lesen (${fmt.num(d.playbook.length)} Zeichen)</summary>
        <div class="details-body"><pre class="playbook">${d.playbook}</pre></div></details>
      <hr />
      <${Toggle} checked=${override != null} onChange=${(on) => setOverride(on ? (d.override ?? d.playbook) : null)}
        label="Eigene Version des Playbooks verwenden" description="Ersetzt das Playbook im Systemprompt. Das Repo bleibt unverändert." />
      ${override != null && html`<div style="margin-top:12px"><${TextareaField} label="Eigenes Playbook" value=${override} onInput=${setOverride} rows=${16} class="mono"
        help="Markdown. Halte die Hook- und Titelregeln drin, sonst schlägt der Qualitäts-Check öfter fehl." /></div>`}
    <//>
    <${Card} title="Zusatzanweisungen" icon="pencil" subtitle="Werden nach dem Playbook angehängt, z. B. „Bevorzuge Story-Cuts“ oder „Keine Voiceovers bei xQc“.">
      <${TextareaField} label="Zusatzanweisungen" value=${extra} onInput=${setExtra} rows=${6} placeholder="Optional. Kurz und konkret." />
    <//>
    ${(dirty || error) && html`<div class="savebar">
      <span class="grow">${error ? html`<span style="color:var(--red)">${error.message}</span>` : 'Änderungen nicht gespeichert'}</span>
      <${Button} variant="ghost" disabled=${busy} onClick=${() => { setOverride(d.override); setExtra(d.extra_instructions || ''); setError(null); }}>Verwerfen<//>
      <${Button} variant="primary" icon="check" busy=${busy} disabled=${!dirty} onClick=${save}>Speichern<//>
    </div>`}`;
}

// ---------------------------------------------------------------------------------------------------------------
// Assets
// ---------------------------------------------------------------------------------------------------------------

const MOODS = { suspense: 'Spannung', sneaky: 'Schleichen', epic: 'Episch', sad: 'Traurig', funny: 'Lustig', any: 'Sound-Hit' };

function AssetsEditor() {
  const doc = useData(() => api.getAssets(), []);
  const [busyId, setBusyId] = useState(null);
  if (doc.error && !doc.data) return html`<${ErrorState} error=${doc.error} onRetry=${doc.reload} />`;
  if (!doc.data) return html`<div class="card"><${Loading} lines=${8} /></div>`;
  const toggle = async (t, approved) => {
    if (approved && t.claimable && !(await confirmDialog({ title: 'Content-ID-gefährdeten Track freigeben?', confirmLabel: 'Freigeben',
      message: 'Dieser Track kann einen Urheberrechts-Claim auslösen. Er spielt nur, wenn das Profil music.allow_claimable erlaubt.' }))) return;
    setBusyId(t.id);
    try {
      const upd = await api.updateMusic(t.id, { approved });
      doc.setData((d) => ({ ...d, music: d.music.map((m) => (m.id === upd.id ? upd : m)) }));
    } catch (err) { toast.fail(err); } finally { setBusyId(null); }
  };
  const music = doc.data.music || [];
  const groups = [['music', 'Musik'], ['hit', 'Sound-Hits']];
  return html`${groups.map(([kind, title]) => {
      const rows = music.filter((m) => m.kind === kind);
      return html`<${Card} key=${kind} title=${title} icon=${kind === 'music' ? 'music' : 'zap'}
        subtitle=${`${rows.filter((m) => m.usable).length} von ${rows.length} nutzbar. Nur freigegebene Tracks spielen im Render.`}>
        ${rows.length ? html`<div class="list">${rows.map((t) => html`<div class="list-row" key=${t.id}>
          <div class="list-main">
            <div class="list-title"><span class="mono">${t.id}</span>
              ${kind === 'music' && html`<${Badge} tone="neutral">${MOODS[t.mood] || t.mood}<//>`}
              ${t.claimable && html`<${Badge} tone="yellow" icon="alert">Content-ID<//>`}
              ${t.local && html`<${Badge} tone="muted">lokal<//>`}
              ${t.approved && !t.usable && html`<${Badge} tone="red">nicht nutzbar<//>`}
            </div>
            <span class="list-sub">${t.file}</span>
          </div>
          <${Toggle} checked=${!!t.approved} label=${`${t.id} freigeben`} hideLabel stateText=${t.approved ? 'freigegeben' : 'gesperrt'} busy=${busyId === t.id} onChange=${(v) => toggle(t, v)} />
        </div>`)}</div>` : html`<p class="muted">Keine Einträge in assets/music/library.yaml.</p>`}
      <//>`;
    })}
    <${Card} title="Schriften" icon="type" subtitle="Gebündelt in assets/fonts (OFL/Apache). Für andere Alphabete wählt der Render automatisch eine passende Schrift.">
      ${(doc.data.fonts || []).length ? html`<div class="list">${doc.data.fonts.map((f) => html`<div class="list-row" key=${f.file}>
        <div class="list-main"><span class="list-title">${f.name}</span><span class="list-sub">${f.file}${f.license ? ` · ${f.license}` : ''}</span></div></div>`)}</div>`
        : html`<p class="muted">Keine Schriften gefunden.</p>`}
      <p class="muted small" style="margin-top:10px">Eigene Musik: Dateien nach <code>assets/music/local/</code> legen und in <code>assets/music/local/library.yaml</code> eintragen; sie erscheinen dann hier zum Freigeben.</p>
    <//>`;
}
