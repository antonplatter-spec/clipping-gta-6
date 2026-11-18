// Freigabe: the layout preview still of a pick and the editors for its caption, hook and sound keys, effects and
// music. The pick's own keys win over the defaults in Einstellungen → Untertitel & Sound; a key left out ("Standard")
// uses that default. Times are shown in seconds of the finished clip; single clips store them in source seconds.
import * as api from '../api.js';
import { html, useState, useEffect, useRef, useId, cx } from '../lib.js';
import { Button, IconButton, Icon, Badge, Spinner, Segmented, Modal, describedBy } from '../ui.js';
import {
  STYLE_CHOICES, STYLE_LABELS, LOOKS, styleValueLabel, SOUND_ROLES, MOODS, FX_TYPES, FLASH_COLORS,
} from '../labels.js';

export const STYLE_KEYS = ['caption_position', 'caption_size', 'caption_look', 'hook_size', 'hook_seconds', 'sound_level'];

/** seconds → "0:04,5" */
export function clockD(sec) {
  if (sec == null || Number.isNaN(sec)) return '–';
  const t = Math.max(0, Math.round(sec * 10) / 10);
  const m = Math.floor(t / 60);
  const s = (t - m * 60).toFixed(1).replace('.', ',');
  return `${m}:${s.padStart(4, '0')}`;
}
const r2 = (x) => Math.round(x * 100) / 100;

// ---------------------------------------------------------------------------------------------------------------
// Draft <-> pick data
// ---------------------------------------------------------------------------------------------------------------

const modeOf = (v) => (v == null ? 'auto' : Array.isArray(v) && v.length ? 'custom' : 'none');

export function styleDraftOf(d) {
  return {
    style: Object.fromEntries(STYLE_KEYS.map((k) => [k, d[k] ?? ''])),
    fxMode: modeOf(d.fx), fx: Array.isArray(d.fx) ? d.fx.map((f) => ({ ...f })) : [],
    musicMode: modeOf(d.music), music: Array.isArray(d.music) ? d.music.map((m) => ({ ...m })) : [],
  };
}

const listValue = (mode, list) => (mode === 'auto' ? null : mode === 'none' ? [] : list);

/** The PATCH fields for what changed (null = drop the key: back to the default / automatic). */
export function styleChangesOf(draft, base) {
  const out = {};
  for (const k of STYLE_KEYS) {
    if (draft.style[k] !== base.style[k]) out[k] = draft.style[k] === '' ? null : draft.style[k];
  }
  const fx = listValue(draft.fxMode, draft.fx);
  if (JSON.stringify(fx) !== JSON.stringify(listValue(base.fxMode, base.fx))) out.fx = fx;
  const music = listValue(draft.musicMode, draft.music);
  if (JSON.stringify(music) !== JSON.stringify(listValue(base.musicMode, base.music))) out.music = music;
  return out;
}

/** Whether the saved preview no longer shows the draft (layout keys or start/end changed). */
export function layoutChanged(draft, base) {
  return STYLE_KEYS.some((k) => k !== 'sound_level' && draft.style[k] !== base.style[k])
    || draft.caption_style !== base.caption_style || draft.hook_style !== base.hook_style
    || draft.start !== base.start || draft.end !== base.end
    || JSON.stringify(draft.pairs) !== JSON.stringify(base.pairs);
}

// ---------------------------------------------------------------------------------------------------------------
// Layout preview
// ---------------------------------------------------------------------------------------------------------------

const MAX_PARALLEL = 2;  // the server draws at most two at once; don't queue ten requests behind them
const queue = { running: 0, waiting: [] };
function inQueue(fn) {
  return new Promise((resolve, reject) => {
    const start = () => {
      queue.running += 1;
      fn().then(resolve, reject).finally(() => {
        queue.running -= 1;
        const next = queue.waiting.shift();
        if (next) next();
      });
    };
    if (queue.running < MAX_PARALLEL) start(); else queue.waiting.push(start);
  });
}

function useVisible(ref, rootMargin = '200px') {
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    if (visible || !ref.current) return undefined;
    if (!('IntersectionObserver' in window)) { setVisible(true); return undefined; }
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) { setVisible(true); io.disconnect(); }
    }, { rootMargin });
    io.observe(ref.current);
    return () => io.disconnect();
  }, [visible]);
  return visible;
}

/**
 * The saved pick as it will render, at `at` seconds of the finished clip (null = the first caption while the hook
 * shows). Loads when scrolled near; a changed pick (after saving) loads a new still.
 */
export function LayoutPreview({ pick, at, onAt, onShown, duration, marks, stale }) {
  const ref = useRef(null);
  const visible = useVisible(ref);
  const hooks = (pick.data && pick.data.hooks) || [];
  const [hook, setHook] = useState(1);
  const [state, setState] = useState({ loading: false, data: null, error: null });
  const [slider, setSlider] = useState(at ?? 0);
  const [zoom, setZoom] = useState(false);
  const dataKey = JSON.stringify(pick.data);
  useEffect(() => { if (at != null) setSlider(at); }, [at]);
  useEffect(() => { if (hook > hooks.length) setHook(1); }, [hooks.length]);
  useEffect(() => {
    if (!visible) return undefined;
    let alive = true;
    let retry = null;
    const run = async (attempt) => {
      setState((s) => ({ ...s, loading: true, error: null }));
      try {
        const res = await inQueue(() => api.getPickPreview(pick.id, { at: at == null ? undefined : at, hook }));
        if (!alive) return;
        if (!res.ok && res.reason === 'busy' && attempt < 6) { retry = setTimeout(() => run(attempt + 1), 2000); return; }
        setState({ loading: false, data: res, error: null });
        if (res.ok && at == null) setSlider(res.at);
        if (res.ok && onShown) onShown(res.at);
      } catch (error) {
        if (alive) setState((s) => ({ loading: false, data: s.data, error }));
      }
    };
    run(0);
    return () => { alive = false; clearTimeout(retry); };
  }, [visible, dataKey, at, hook]);

  const d = state.data;
  const ok = d && d.ok;
  const hookEnd = ok && d.placement && d.placement.hook ? d.placement.hook.seconds : null;
  const allMarks = [...(marks || []), ...(hookEnd ? [{ label: 'Hook blendet aus', t: Math.max(0, hookEnd - 0.3) },
    { label: 'Nach dem Hook', t: Math.min(Math.max(0, duration - 0.2), hookEnd + 0.6) }] : [])].sort((a, b) => a.t - b.t);
  const lines = ok && d.placement && d.placement.text_de ? d.placement.text_de.split(' · ') : [];
  const hookState = !ok ? null : d.no_hook_arm ? 'ohne Hook (A/B-Test)' : d.hook_visible ? 'Hook sichtbar' : 'Hook schon ausgeblendet';
  return html`<div class="preview" ref=${ref}>
    <div class="section-label"><${Icon} name="smartphone" size=${14} />Vorschau</div>
    <div class=${cx('preview-frame', state.loading && 'is-loading')}>
      ${ok ? html`<button type="button" class="preview-btn" onClick=${() => setZoom(true)} aria-label="Vorschau vergrößern">
          <img src=${d.image} alt=${`So sieht der Clip bei ${clockD(d.at)} aus: Spielbild mit Hook und Untertitel`} width="270" height="480" decoding="async" /></button>`
        : d && !d.ok ? html`<div class="preview-ph"><${Icon} name="image" size=${28} /><span>${d.message}</span></div>`
        : state.error ? html`<div class="preview-ph"><${Icon} name="alert-circle" size=${28} /><span>${state.error.message}</span></div>`
        : html`<div class="preview-ph"><${Spinner} size=${22} /><span>Vorschau wird erstellt …</span></div>`}
      ${state.loading && ok && html`<span class="preview-spin"><${Spinner} size=${18} label="Vorschau wird aktualisiert" /></span>`}
      ${stale && html`<span class="preview-stale">Geändert – „Speichern“ zeigt die neue Vorschau</span>`}
    </div>
    ${duration > 0 && html`<div class="preview-scrub">
      <label class="small muted" for=${`scrub-${pick.id}`}>Zeitpunkt im Clip: <b>${clockD(slider)}</b> von ${clockD(duration)}</label>
      <input id=${`scrub-${pick.id}`} type="range" min="0" max=${Math.max(0.1, duration - 0.1).toFixed(1)} step="0.1" value=${slider}
        onInput=${(e) => setSlider(Number(e.currentTarget.value))} onChange=${(e) => onAt(Number(e.currentTarget.value))}
        aria-valuetext=${clockD(slider)} />
      <div class="chips">
        <button type="button" class=${cx('chip-btn', at == null && 'is-active')} onClick=${() => onAt(null)}>Erster Untertitel</button>
        ${allMarks.map((m) => html`<button type="button" key=${`${m.label}${m.t}`} class=${cx('chip-btn', at != null && Math.abs(at - m.t) < 0.05 && 'is-active')}
          onClick=${() => onAt(r2(m.t))}>${m.label} ${clockD(m.t)}</button>`)}
      </div>
    </div>`}
    ${hooks.length > 1 && html`<div class="row" style="gap:6px"><span class="small muted">Hook</span>
      <${Segmented} size="sm" label="Welcher Hook" value=${hook} onChange=${(v) => setHook(Number(v))}
        options=${hooks.map((_, i) => ({ value: i + 1, label: String(i + 1) }))} /></div>`}
    ${ok && html`<div class="preview-facts">
      ${hookState && html`<${Badge} tone=${d.no_hook_arm ? 'muted' : 'neutral'}>${hookState}<//>`}
      ${d.caption ? html`<span class="small">Untertitel jetzt: „${d.caption}“</span>` : html`<span class="small muted">Gerade kein Untertitel</span>`}
      <ul class="preview-lines">${lines.map((t) => html`<li>${t}</li>`)}</ul>
    </div>`}
    ${zoom && ok && html`<${Modal} title=${`Vorschau bei ${clockD(d.at)}`} onClose=${() => setZoom(false)}>
      <img class="zoom-img preview-zoom" src=${d.image} alt=${`Vorschau bei ${clockD(d.at)}`} />
    <//>`}
  </div>`;
}

// ---------------------------------------------------------------------------------------------------------------
// Caption, hook and sound keys
// ---------------------------------------------------------------------------------------------------------------

function StyleSelect({ k, value, onChange, defaults, family, error }) {
  const auto = useId();
  const id = `st${auto}`;
  const choices = STYLE_CHOICES[k] || [];
  const def = defaults ? defaults[k] ?? (k === 'sound_level' ? 'normal' : 'auto') : null;
  let options = choices.filter((c) => c.value !== 'seconds');
  if (k === 'caption_look') {
    options = [...options, ...Object.entries(LOOKS).filter(([, l]) => l.family === family)
      .map(([v, l]) => ({ value: v, label: l.label, help: `Fester Look für diesen Clip: ${l.label}.` }))];
  }
  const isSeconds = k === 'hook_seconds' && typeof value === 'number';
  const selected = isSeconds ? 'seconds' : value;
  const chosen = isSeconds ? choices.find((c) => c.value === 'seconds') : options.find((c) => c.value === value);
  const defChoice = def != null && (choices.find((c) => c.value === def) || (LOOKS[def] && { help: `Fester Look: ${LOOKS[def].label}.` })
    || (typeof def === 'number' && { help: `Der Hook steht ${String(def).replace('.', ',')} s.` }));
  const help = value === '' ? `Wie in den Einstellungen${def != null ? ` (${styleValueLabel(k, def)})` : ''}: ${(defChoice && defChoice.help) || ''}`
    : (chosen && chosen.help) || '';
  return html`<div class=${cx('field', error && 'has-error')}>
    <label class="field-label" for=${id}>${STYLE_LABELS[k]}</label>
    <div class="row" style="gap:6px;flex-wrap:nowrap">
      <div class="select-wrap grow">
        <select id=${id} class="input select" value=${String(selected ?? '')} aria-describedby=${describedBy(id, help, error)}
          onChange=${(e) => { const v = e.currentTarget.value; onChange(v === 'seconds' ? 5 : v); }}>
          <option value="">Standard${def != null ? ` (${styleValueLabel(k, def)})` : ''}</option>
          ${options.map((o) => html`<option value=${o.value}>${o.label}</option>`)}
          ${k === 'hook_seconds' && html`<option value="seconds">Feste Sekunden …</option>`}
        </select>
        <${Icon} name="chevron-down" size=${16} class="select-caret" />
      </div>
      ${isSeconds && html`<input class="input input-num" type="number" min="1" max="30" step="0.5" value=${value} aria-label="Sekunden, die der Hook steht"
        onInput=${(e) => { const n = Number(e.currentTarget.value); if (e.currentTarget.value !== '' && !Number.isNaN(n)) onChange(n); }} />`}
      ${isSeconds && html`<span class="muted small">s</span>`}
    </div>
    <p class="field-help" id=${`${id}-help`}>${help}</p>
    ${error && html`<p class="field-error" id=${`${id}-err`} role="alert"><${Icon} name="alert-circle" size=${14} />${error}</p>`}
  </div>`;
}

export function StyleControls({ style, family, defaults, onChange, errors }) {
  const ctl = (k) => html`<${StyleSelect} key=${k} k=${k} value=${style[k]} defaults=${defaults} family=${family}
    error=${errors && errors[k]} onChange=${(v) => onChange(k, v)} />`;
  return html`<div class="stack">
    <div><h3 class="section-label">Untertitel, Hook & Sound</h3>
      <p class="muted small" style="margin-top:2px">„Standard“ nimmt die Einstellung unter Einstellungen → Untertitel & Sound. Die Vorschau zeigt den gespeicherten Stand.</p></div>
    <div class="form-grid form-grid-3">
      ${['caption_position', 'caption_size', 'caption_look'].map(ctl)}
    </div>
    <div class="form-grid form-grid-3">
      ${['hook_size', 'hook_seconds', 'sound_level'].map(ctl)}
    </div>
  </div>`;
}

// ---------------------------------------------------------------------------------------------------------------
// Effects and music
// ---------------------------------------------------------------------------------------------------------------

const FX_MODES = [
  { value: 'auto', label: 'Automatisch', help: 'Der Render findet die Höhepunkte selbst (Pointe, lautester Sprung, geschriener Satz) und setzt dort Sound und Blitz – so viel, wie die Sound-Stärke erlaubt – dazu Zooms auf geschriene Sätze.' },
  { value: 'none', label: 'Keine', help: 'Keine Effekte und kein automatischer Sound.' },
  { value: 'custom', label: 'Eigene', help: 'Nur die Effekte unten. Automatische Zooms und Sounds entfallen.' },
];
const MUSIC_MODES = [
  { value: 'auto', label: 'Automatisch', help: 'Keine feste Musik. Vor einem stillen Aufbau legt der Render bei Bedarf ein leises Spannungs-Bett.' },
  { value: 'none', label: 'Keine', help: 'Keine Musik – und solange die Effekte automatisch sind, auch kein automatischer Sound.' },
  { value: 'custom', label: 'Eigene', help: 'Musik nur in den Abschnitten unten; sie wird leiser, sobald jemand spricht.' },
];

function SecondsInput({ value, onChange, max, label }) {
  return html`<span class="sec-input"><input class="input input-sm input-num" type="number" min="0" max=${max != null ? max.toFixed(1) : undefined}
    step="0.1" value=${value == null ? '' : r2(value)} aria-label=${label}
    onInput=${(e) => { const v = e.currentTarget.value; if (v !== '' && !Number.isNaN(Number(v))) onChange(Number(v)); }} />
    <span class="muted small">s</span></span>`;
}

function soundOptions(sounds) {
  const roles = Object.entries(SOUND_ROLES).map(([v, r]) => ({ value: v, label: `${r.label} (passend gewählt)` }));
  const own = (sounds || []).filter((t) => t.kind === 'hit' && t.usable && !SOUND_ROLES[t.id])
    .map((t) => ({ value: t.id, label: `${t.title || t.id} · ${(SOUND_ROLES[t.role] || {}).label || t.role}${t.source === 'own' ? ' · eigener' : ''}` }));
  return { roles, own };
}

export function FxEditor({ mode, items, onMode, onItems, offset, duration, cursor, sounds, error }) {
  const opts = soundOptions(sounds);
  const at = (t) => r2(t - offset);
  const rel = cursor != null ? cursor : Math.max(0, duration * 0.6);
  const add = (...fx) => { onMode('custom'); onItems([...items, ...fx.map((f) => ({ t: r2(offset + rel), ...f }))].sort((a, b) => a.t - b.t)); };
  const upd = (i, patch) => onItems(items.map((f, j) => (j === i ? { ...f, ...patch } : f)));
  const setType = (i, type) => {
    const f = items[i];
    const next = { t: f.t, type };
    if (type === 'hit') next.sound = 'impact';
    if (type === 'flash') next.color = 'white';
    onItems(items.map((x, j) => (j === i ? next : x)));
  };
  const help = (FX_MODES.find((m) => m.value === mode) || {}).help;
  return html`<div class="stack-sm fx-editor">
    <div class="row" style="justify-content:space-between"><span class="field-label">Effekte</span>
      <${Segmented} size="sm" label="Effekte" value=${mode} onChange=${onMode} options=${FX_MODES} /></div>
    <p class="field-help">${help}</p>
    ${mode === 'custom' && html`<ul class="beat-list" aria-label="Effekte">
      ${items.map((f, i) => {
        const t = at(f.t);
        const out = t < 0 || t >= duration;
        const tight = f.type === 'hit' && f.sound === 'riser' && t < 0.5;
        return html`<li class=${cx('beat', out && 'has-fail')} key=${i}>
          <span class="beat-label">bei</span>
          <${SecondsInput} value=${t} max=${duration} label=${`Effekt ${i + 1}: Sekunde im Clip`} onChange=${(v) => upd(i, { t: r2(offset + v) })} />
          <div class="select-wrap"><select class="input input-sm select" value=${f.type} aria-label=${`Effekt ${i + 1}: Art`} onChange=${(e) => setType(i, e.currentTarget.value)}>
            ${Object.entries(FX_TYPES).map(([v, l]) => html`<option value=${v}>${l}</option>`)}</select><${Icon} name="chevron-down" size=${14} class="select-caret" /></div>
          ${f.type === 'hit' && html`<div class="select-wrap grow"><select class="input input-sm select" value=${f.sound || 'impact'} aria-label=${`Effekt ${i + 1}: Sound`}
              onChange=${(e) => upd(i, { sound: e.currentTarget.value })}>
            <optgroup label="Nach Rolle">${opts.roles.map((o) => html`<option value=${o.value}>${o.label}</option>`)}</optgroup>
            ${opts.own.length > 0 && html`<optgroup label="Bestimmter Sound">${opts.own.map((o) => html`<option value=${o.value}>${o.label}</option>`)}</optgroup>`}
            ${f.sound && !SOUND_ROLES[f.sound] && !opts.own.some((o) => o.value === f.sound) && html`<option value=${f.sound}>${f.sound} (nicht mehr da)</option>`}
          </select><${Icon} name="chevron-down" size=${14} class="select-caret" /></div>`}
          ${f.type === 'flash' && html`<div class="select-wrap grow"><select class="input input-sm select" value=${f.color || 'lime'} aria-label=${`Effekt ${i + 1}: Farbe`}
              onChange=${(e) => upd(i, { color: e.currentTarget.value })}>
            ${Object.entries(FLASH_COLORS).map(([v, l]) => html`<option value=${v}>${l}</option>`)}
            ${f.color && !FLASH_COLORS[f.color] && html`<option value=${f.color}>${f.color}</option>`}
          </select><${Icon} name="chevron-down" size=${14} class="select-caret" /></div>`}
          ${f.type === 'zoom' && html`<span class="muted small grow">kurzer Ruck ins Bild</span>`}
          <${IconButton} icon="trash" size="sm" label=${`Effekt ${i + 1} entfernen`} onClick=${() => onItems(items.filter((_, j) => j !== i))} />
          ${(out || tight) && html`<p class="issue issue-fail beat-note"><${Icon} name="alert-circle" size=${13} />
            <span>${out ? `Liegt außerhalb des Clips (0–${clockD(duration)}).` : 'Ein Riser braucht davor Platz: mindestens 0,5 s nach Clipbeginn.'}</span></p>`}
        </li>`;
      })}
      ${!items.length && html`<li class="muted small">Noch keine Effekte – unten hinzufügen. Leer gespeichert heißt: keine Effekte.</li>`}
    </ul>`}
    <div class="row" style="gap:6px">
      <${Button} size="sm" icon="zap" onClick=${() => add({ type: 'hit', sound: 'riser' }, { type: 'hit', sound: 'impact' })}
        title="Ein Riser baut auf und endet genau hier, ein Impact schlägt genau hier ein">Epischer Moment bei ${clockD(rel)}<//>
      <${Button} size="sm" icon="plus" onClick=${() => add({ type: 'hit', sound: 'impact' })}>Sound<//>
      <${Button} size="sm" icon="plus" onClick=${() => add({ type: 'flash', color: 'white' })}>Blitz<//>
      <${Button} size="sm" icon="plus" onClick=${() => add({ type: 'zoom' })}>Zoom<//>
    </div>
    <p class="muted small">Zeiten in Sekunden des fertigen Clips; neue Effekte landen beim Zeitpunkt der Vorschau. Sparsam: ein Aufbau pro Clip auf dem echten Höhepunkt, höchstens alle 4 s ein Sound.</p>
    ${error && html`<p class="field-error" role="alert"><${Icon} name="alert-circle" size=${14} />${error}</p>`}
  </div>`;
}

export function MusicEditor({ mode, items, onMode, onItems, offset, duration, cursor, error }) {
  const upd = (i, patch) => onItems(items.map((m, j) => (j === i ? { ...m, ...patch } : m)));
  const add = () => {
    const s = Math.min(Math.max(0, cursor ?? 0), Math.max(0, duration - 3));
    onMode('custom');
    onItems([...items, { s: r2(offset + s), e: r2(offset + Math.min(duration, s + 5)), mood: 'suspense' }]);
  };
  const help = (MUSIC_MODES.find((m) => m.value === mode) || {}).help;
  return html`<div class="stack-sm">
    <div class="row" style="justify-content:space-between"><span class="field-label">Musik</span>
      <${Segmented} size="sm" label="Musik" value=${mode} onChange=${onMode} options=${MUSIC_MODES} /></div>
    <p class="field-help">${help}</p>
    ${mode === 'custom' && html`<ul class="beat-list" aria-label="Musik-Abschnitte">
      ${items.map((m, i) => {
        const a = r2(m.s - offset);
        const b = r2(m.e - offset);
        const bad = a < 0 || b > duration + 0.01 || b - a < 1;
        return html`<li class=${cx('beat', bad && 'has-fail')} key=${i}>
          <span class="beat-label">von</span>
          <${SecondsInput} value=${a} max=${duration} label=${`Musik ${i + 1}: Anfang`} onChange=${(v) => upd(i, { s: r2(offset + v) })} />
          <span class="beat-label">bis</span>
          <${SecondsInput} value=${b} max=${duration} label=${`Musik ${i + 1}: Ende`} onChange=${(v) => upd(i, { e: r2(offset + v) })} />
          <div class="select-wrap grow"><select class="input input-sm select" value=${m.mood} aria-label=${`Musik ${i + 1}: Stimmung`} onChange=${(e) => upd(i, { mood: e.currentTarget.value })}>
            ${Object.entries(MOODS).map(([v, l]) => html`<option value=${v}>${l}</option>`)}</select><${Icon} name="chevron-down" size=${14} class="select-caret" /></div>
          <${IconButton} icon="trash" size="sm" label=${`Musik ${i + 1} entfernen`} onClick=${() => onItems(items.filter((_, j) => j !== i))} />
          ${bad && html`<p class="issue issue-fail beat-note"><${Icon} name="alert-circle" size=${13} /><span>Mindestens 1 s lang und innerhalb des Clips (0–${clockD(duration)}).</span></p>`}
        </li>`;
      })}
      ${!items.length && html`<li class="muted small">Noch kein Abschnitt. Leer gespeichert heißt: keine Musik.</li>`}
    </ul>`}
    <div class="row"><${Button} size="sm" icon="music" onClick=${add}>Musik-Abschnitt<//></div>
    ${error && html`<p class="field-error" role="alert"><${Icon} name="alert-circle" size=${14} />${error}</p>`}
  </div>`;
}
