// Shared, accessible UI components (Preact + htm). Pages compose these; styling lives in css/app.css.
import {
  html, useState, useEffect, useRef, useId, useStore, cx, fmt, copyText, toast, toastStore, dismissToast,
  dialogStore, errorText, useNow,
} from './lib.js';
import ICONS from '../vendor/lucide-icons.js';
import { JOB_STATUS, JOB_STATUS_RENDERING, PICK_STATUS } from './labels.js';

// ---------------------------------------------------------------------------------------------------------------
// Icons, buttons, badges
// ---------------------------------------------------------------------------------------------------------------

export function Icon({ name, size = 18, label, class: klass, ...rest }) {
  const nodes = ICONS[name];
  if (!nodes) return null;
  return html`<svg class=${cx('icon', klass)} width=${size} height=${size} viewBox="0 0 24 24" fill="none"
    stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
    aria-hidden=${label ? undefined : 'true'} role=${label ? 'img' : undefined} aria-label=${label} focusable="false" ...${rest}>
    ${nodes.map(([tag, attrs]) => html`<${tag} ...${attrs} />`)}
  </svg>`;
}

export function Spinner({ size = 16, label }) {
  return html`<span class="spinner" style=${`--s:${size}px`} role=${label ? 'status' : undefined}
    aria-label=${label} aria-hidden=${label ? undefined : 'true'}></span>`;
}

/**
 * variant: primary | secondary | ghost | danger | approve | claude ; size: sm | md
 * `busy` disables and shows a spinner; `href` renders a link styled as a button.
 */
export function Button({
  variant = 'secondary', size = 'md', icon, iconRight, busy = false, disabled, type = 'button', href, children,
  class: klass, block, ...rest
}) {
  const cls = cx('btn', `btn-${variant}`, size === 'sm' && 'btn-sm', block && 'btn-block', busy && 'is-busy', klass);
  const inner = html`${busy ? html`<${Spinner} size=${size === 'sm' ? 13 : 15} />` : icon && html`<${Icon} name=${icon} size=${size === 'sm' ? 15 : 17} />`}
    ${children != null && children !== false && html`<span class="btn-label">${children}</span>`}
    ${iconRight && html`<${Icon} name=${iconRight} size=${size === 'sm' ? 15 : 17} />`}`;
  if (href) return html`<a class=${cls} href=${href} ...${rest}>${inner}</a>`;
  return html`<button type=${type} class=${cls} disabled=${disabled || busy} aria-busy=${busy ? 'true' : undefined} ...${rest}>${inner}</button>`;
}

export function IconButton({ icon, label, variant = 'ghost', size = 'md', busy, ...rest }) {
  return html`<${Button} variant=${variant} size=${size} icon=${icon} busy=${busy} aria-label=${label} title=${label}
    class="btn-icon" ...${rest} />`;
}

/** tone: neutral | muted | green | yellow | orange | red | running | blue */
export function Badge({ tone = 'neutral', dot, icon, children, class: klass, title }) {
  return html`<span class=${cx('badge', `tone-${tone}`, klass)} title=${title}>
    ${dot && html`<span class="badge-dot" aria-hidden="true"></span>`}
    ${icon && html`<${Icon} name=${icon} size=${13} />`}${children}</span>`;
}

export function JobStatusBadge({ status, stage }) {
  // a source past its approval waits for its own renders, not for the owner (auto approval, or all picks decided)
  const s = status === 'waiting_approval' && stage === 'render' ? JOB_STATUS_RENDERING
    : JOB_STATUS[status] || { label: status, tone: 'neutral' };
  return html`<${Badge} tone=${s.tone} dot=${status === 'running'}>${s.label}<//>`;
}

export function PickStatusBadge({ status }) {
  const s = PICK_STATUS[status] || { label: status, tone: 'neutral' };
  return html`<${Badge} tone=${s.tone} dot=${status === 'rendering'}>${s.label}<//>`;
}

/** Relative time with the absolute one as tooltip; re-renders every 30 s. */
export function Rel({ ts, prefix }) {
  const now = useNow(30000);
  if (!ts) return html`<span class="muted">–</span>`;
  const abs = new Date(ts * 1000).toLocaleString('de-DE');
  return html`<time datetime=${new Date(ts * 1000).toISOString()} title=${abs}>${prefix}${fmt.relative(ts, now)}</time>`;
}

// ---------------------------------------------------------------------------------------------------------------
// Layout pieces
// ---------------------------------------------------------------------------------------------------------------

export function PageHeader({ title, subtitle, actions, children }) {
  return html`<header class="page-header">
    <div class="page-header-text">
      <h1>${title}</h1>
      ${subtitle && html`<p class="page-subtitle">${subtitle}</p>`}
    </div>
    ${actions && html`<div class="page-actions">${actions}</div>`}
    ${children}
  </header>`;
}

export function Card({ title, icon, actions, children, class: klass, tone, id, headingLevel = 2, subtitle }) {
  const H = `h${headingLevel}`;
  return html`<section class=${cx('card', tone && `card-${tone}`, klass)} id=${id} aria-labelledby=${title && id ? `${id}-title` : undefined}>
    ${(title || actions) && html`<div class="card-head">
      <div class="card-title-wrap">
        ${title && html`<${H} class="card-title" id=${id ? `${id}-title` : undefined}>${icon && html`<${Icon} name=${icon} size=${17} />`}${title}<//>`}
        ${subtitle && html`<p class="card-subtitle">${subtitle}</p>`}
      </div>
      ${actions && html`<div class="card-actions">${actions}</div>`}
    </div>`}
    ${children}
  </section>`;
}

export function Skeleton({ lines = 3, height, class: klass }) {
  if (height) return html`<div class=${cx('skeleton', klass)} style=${`height:${height}px`} aria-hidden="true"></div>`;
  return html`<div class=${cx('skeleton-lines', klass)} aria-hidden="true">
    ${Array.from({ length: lines }, (_, i) => html`<div class="skeleton" style=${`width:${[92, 76, 84, 58, 70][i % 5]}%`}></div>`)}
  </div>`;
}

export function Loading({ label = 'Lädt …', lines = 3 }) {
  return html`<div class="loading" role="status" aria-live="polite"><span class="sr-only">${label}</span><${Skeleton} lines=${lines} /></div>`;
}

export function Empty({ icon = 'sparkles', title, children, action }) {
  return html`<div class="empty">
    <div class="empty-icon"><${Icon} name=${icon} size=${26} /></div>
    <p class="empty-title">${title}</p>
    ${children && html`<p class="empty-text">${children}</p>`}
    ${action && html`<div class="empty-action">${action}</div>`}
  </div>`;
}

export function ErrorState({ error, onRetry, title = 'Konnte nicht geladen werden' }) {
  return html`<div class="error-state" role="alert">
    <${Icon} name="alert-circle" size=${20} />
    <div><p class="error-title">${title}</p><p>${errorText(error)}</p></div>
    ${onRetry && html`<${Button} size="sm" icon="refresh-cw" onClick=${onRetry}>Erneut versuchen<//>`}
  </div>`;
}

/** tone: info | success | warning | danger | claude */
export function Callout({ tone = 'info', icon, title, children, actions, class: klass }) {
  const icons = { info: 'info', success: 'circle-check', warning: 'alert', danger: 'alert-circle', claude: 'sparkles' };
  return html`<div class=${cx('callout', `callout-${tone}`, klass)} role=${tone === 'danger' ? 'alert' : undefined}>
    <${Icon} name=${icon || icons[tone]} size=${18} class="callout-icon" />
    <div class="callout-body">
      ${title && html`<p class="callout-title">${title}</p>`}
      ${children && html`<div class="callout-text">${children}</div>`}
    </div>
    ${actions && html`<div class="callout-actions">${actions}</div>`}
  </div>`;
}

export function Meter({ value, max, tone = 'neutral', label, warnAt = 0.8 }) {
  const ratio = max > 0 ? Math.min(1, Math.max(0, value / max)) : 0;
  const t = max > 0 && value >= max ? 'red' : max > 0 && ratio >= warnAt ? 'yellow' : tone;
  return html`<div class=${cx('meter', `tone-${t}`)} role="meter" aria-valuemin="0" aria-valuemax=${max || 0}
    aria-valuenow=${value || 0} aria-label=${label}><span style=${`width:${(ratio * 100).toFixed(1)}%`}></span></div>`;
}

export function Tabs({ tabs, value, onChange, label, class: klass }) {
  const refs = useRef([]);
  const onKey = (e, i) => {
    let j = null;
    if (e.key === 'ArrowRight') j = (i + 1) % tabs.length;
    if (e.key === 'ArrowLeft') j = (i - 1 + tabs.length) % tabs.length;
    if (e.key === 'Home') j = 0;
    if (e.key === 'End') j = tabs.length - 1;
    if (j != null) { e.preventDefault(); onChange(tabs[j].id); refs.current[j] && refs.current[j].focus(); }
  };
  return html`<div class=${cx('tabs', klass)} role="tablist" aria-label=${label}>
    ${tabs.map((t, i) => html`<button type="button" role="tab" class=${cx('tab', value === t.id && 'is-active')}
      aria-selected=${value === t.id ? 'true' : 'false'} tabindex=${value === t.id ? 0 : -1}
      ref=${(el) => { refs.current[i] = el; }} onKeyDown=${(e) => onKey(e, i)} onClick=${() => onChange(t.id)}>
      ${t.icon && html`<${Icon} name=${t.icon} size=${16} />`}<span>${t.label}</span>
      ${t.count != null && html`<span class=${cx('tab-count', t.tone && `tone-${t.tone}`)}>${fmt.num(t.count)}</span>`}
    </button>`)}
  </div>`;
}

export function Pager({ total, limit, offset, onChange }) {
  if (!total || total <= limit) return null;
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.ceil(total / limit);
  return html`<nav class="pager" aria-label="Seiten">
    <${Button} size="sm" icon="chevron-left" disabled=${page <= 1} onClick=${() => onChange(Math.max(0, offset - limit))}>Zurück<//>
    <span class="muted">Seite ${page} von ${pages} · ${fmt.num(total)} Einträge</span>
    <${Button} size="sm" iconRight="chevron-right" disabled=${page >= pages} onClick=${() => onChange(offset + limit)}>Weiter<//>
  </nav>`;
}

export function KV({ items }) {
  return html`<dl class="kv">${items.filter(Boolean).map(([k, v]) => html`<div class="kv-row"><dt>${k}</dt><dd>${v}</dd></div>`)}</dl>`;
}

// ---------------------------------------------------------------------------------------------------------------
// Form controls (label + help + error wired with aria-describedby)
// ---------------------------------------------------------------------------------------------------------------

export function Field({ id, label, help, error, optional, children, class: klass, hideLabel, counter }) {
  return html`<div class=${cx('field', error && 'has-error', klass)}>
    ${label && html`<label class=${cx('field-label', hideLabel && 'sr-only')} for=${id}>${label}
      ${optional && html`<span class="field-optional">optional</span>`}</label>`}
    ${children}
    ${(help || counter) && html`<div class="field-help-row">
      ${help && html`<p class="field-help" id=${`${id}-help`}>${help}</p>`}
      ${counter}
    </div>`}
    ${error && html`<p class="field-error" id=${`${id}-err`} role="alert"><${Icon} name="alert-circle" size=${14} />${error}</p>`}
  </div>`;
}

export function describedBy(id, help, error) {
  return [help && `${id}-help`, error && `${id}-err`].filter(Boolean).join(' ') || undefined;
}

export function TextField({
  label, help, error, optional, value, onInput, type = 'text', id: givenId, class: klass, hideLabel, maxLength,
  showCount, inputClass, ...rest
}) {
  const auto = useId();
  const id = givenId || `f${auto}`;
  const counter = showCount && maxLength ? html`<span class=${cx('field-count', (value || '').length > maxLength && 'is-over')}>${(value || '').length}/${maxLength}</span>` : null;
  return html`<${Field} id=${id} label=${label} help=${help} error=${error} optional=${optional} class=${klass} hideLabel=${hideLabel} counter=${counter}>
    <input id=${id} class=${cx('input', inputClass)} type=${type} value=${value ?? ''}
      onInput=${(e) => onInput && onInput(e.currentTarget.value)} aria-invalid=${error ? 'true' : undefined}
      aria-describedby=${describedBy(id, help, error)} ...${rest} />
  <//>`;
}

export function TextareaField({ label, help, error, optional, value, onInput, id: givenId, rows = 3, class: klass, maxLength, showCount, ...rest }) {
  const auto = useId();
  const id = givenId || `f${auto}`;
  const counter = showCount && maxLength ? html`<span class=${cx('field-count', (value || '').length > maxLength && 'is-over')}>${(value || '').length}/${maxLength}</span>` : null;
  return html`<${Field} id=${id} label=${label} help=${help} error=${error} optional=${optional} class=${klass} counter=${counter}>
    <textarea id=${id} class="input textarea" rows=${rows} value=${value ?? ''}
      onInput=${(e) => onInput && onInput(e.currentTarget.value)} aria-invalid=${error ? 'true' : undefined}
      aria-describedby=${describedBy(id, help, error)} ...${rest}></textarea>
  <//>`;
}

/** Single-line-looking text field that grows with its content (hooks and titles must stay fully visible on a phone). */
export function AutoText({ label, value, onInput, id: givenId, class: klass, inputClass, hideLabel, help, error, ...rest }) {
  const auto = useId();
  const id = givenId || `a${auto}`;
  const ref = useRef(null);
  const fit = () => { const el = ref.current; if (el) { el.style.height = 'auto'; el.style.height = `${el.scrollHeight + 2}px`; } };
  useEffect(fit, [value]);
  useEffect(() => {
    window.addEventListener('resize', fit);
    return () => window.removeEventListener('resize', fit);
  }, []);
  return html`<${Field} id=${id} label=${label} help=${help} error=${error} class=${klass} hideLabel=${hideLabel}>
    <textarea id=${id} ref=${ref} rows="1" class=${cx('input autotext', inputClass)} value=${value ?? ''}
      onInput=${(e) => { onInput && onInput(e.currentTarget.value.replace(/\n/g, ' ')); }}
      onKeyDown=${(e) => { if (e.key === 'Enter') e.preventDefault(); }}
      aria-invalid=${error ? 'true' : undefined} aria-describedby=${describedBy(id, help, error)} ...${rest}></textarea>
  <//>`;
}

/** options: [{value, label}] or {value: label} */
export function SelectField({ label, help, error, value, onChange, options, id: givenId, class: klass, hideLabel, placeholder, ...rest }) {
  const auto = useId();
  const id = givenId || `f${auto}`;
  const opts = Array.isArray(options) ? options : Object.entries(options).map(([v, l]) => ({ value: v, label: l }));
  return html`<${Field} id=${id} label=${label} help=${help} error=${error} class=${klass} hideLabel=${hideLabel}>
    <div class="select-wrap">
      <select id=${id} class="input select" value=${value == null ? '' : String(value)}
        onChange=${(e) => onChange(e.currentTarget.value)} aria-invalid=${error ? 'true' : undefined}
        aria-describedby=${describedBy(id, help, error)} ...${rest}>
        ${placeholder != null && html`<option value="">${placeholder}</option>`}
        ${opts.map((o) => html`<option value=${String(o.value)}>${o.label}</option>`)}
      </select>
      <${Icon} name="chevron-down" size=${16} class="select-caret" />
    </div>
  <//>`;
}

/** A switch (role=switch). `label` is visible; `description` sits under it. */
export function Toggle({ checked, onChange, label, description, disabled, busy, id: givenId, size, class: klass, hideLabel, stateText }) {
  const auto = useId();
  const id = givenId || `t${auto}`;
  return html`<div class=${cx('toggle-row', klass)}>
    <button type="button" id=${id} role="switch" aria-checked=${checked ? 'true' : 'false'}
      class=${cx('switch', checked && 'is-on', size === 'lg' && 'switch-lg')} disabled=${disabled || busy}
      aria-labelledby=${`${id}-l`} aria-describedby=${description ? `${id}-d` : undefined}
      onClick=${() => onChange(!checked)}>
      <span class="switch-thumb">${busy && html`<${Spinner} size=${10} />`}</span>
    </button>
    <div class="toggle-text">
      <span id=${`${id}-l`} class=${cx('toggle-label', hideLabel && 'sr-only')} onClick=${() => !disabled && !busy && onChange(!checked)}>${label}</span>
      ${description && html`<span id=${`${id}-d`} class="toggle-desc">${description}</span>`}
    </div>
    ${stateText && html`<span class="toggle-state" aria-hidden="true">${stateText}</span>`}
  </div>`;
}

/** Segmented control (radio group). options: [{value, label}] */
export function Segmented({ options, value, onChange, label, size, class: klass }) {
  const name = useId();
  return html`<div class=${cx('segmented', size === 'sm' && 'segmented-sm', klass)} role="radiogroup" aria-label=${label}>
    ${options.map((o) => html`<label class=${cx('seg', String(value) === String(o.value) && 'is-active')}>
      <input type="radio" name=${`seg${name}`} value=${String(o.value)} checked=${String(value) === String(o.value)}
        onChange=${() => onChange(o.value)} class="sr-only" />
      <span>${o.label}</span>${o.count != null && html`<span class="seg-count">${o.count}</span>`}
    </label>`)}
  </div>`;
}

/** List of strings as removable chips; Enter or comma adds. */
export function TagInput({ value = [], onChange, placeholder = 'Hinzufügen …', suggestions, id: givenId, label, help, error, mono, transform }) {
  const auto = useId();
  const id = givenId || `tag${auto}`;
  const [draft, setDraft] = useState('');
  const add = (raw) => {
    const parts = String(raw).split(/[,\n]/).map((s) => (transform ? transform(s.trim()) : s.trim())).filter(Boolean);
    const next = [...value];
    for (const p of parts) if (!next.includes(p)) next.push(p);
    if (next.length !== value.length) onChange(next);
    setDraft('');
  };
  const onKey = (e) => {
    if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); if (draft.trim()) add(draft); }
    if (e.key === 'Backspace' && !draft && value.length) onChange(value.slice(0, -1));
  };
  const listId = suggestions && suggestions.length ? `${id}-list` : undefined;
  return html`<${Field} id=${id} label=${label} help=${help} error=${error}>
    <div class=${cx('tags', error && 'has-error')} onClick=${(e) => e.currentTarget.querySelector('input').focus()}>
      ${value.map((t, i) => html`<span class=${cx('tag', mono && 'mono')}>${t}
        <button type="button" class="tag-x" aria-label=${`„${t}“ entfernen`} onClick=${(e) => { e.stopPropagation(); onChange(value.filter((_, j) => j !== i)); }}>
          <${Icon} name="x" size=${12} /></button></span>`)}
      <input id=${id} class="tags-input" value=${draft} placeholder=${value.length ? '' : placeholder} list=${listId}
        onInput=${(e) => setDraft(e.currentTarget.value)} onKeyDown=${onKey}
        onBlur=${() => draft.trim() && add(draft)} aria-describedby=${describedBy(id, help, error)} />
      ${listId && html`<datalist id=${listId}>${suggestions.filter((s) => !value.includes(s)).map((s) => html`<option value=${s} />`)}</datalist>`}
    </div>
  <//>`;
}

export function CopyButton({ text, label = 'Kopieren', size = 'sm', variant = 'ghost', iconOnly, done = 'Kopiert' }) {
  const [ok, setOk] = useState(false);
  const onClick = async () => {
    const success = await copyText(text || '');
    if (success) { setOk(true); setTimeout(() => setOk(false), 1600); } else toast.error('Kopieren nicht möglich. Bitte Text markieren und manuell kopieren.');
  };
  if (iconOnly) return html`<${IconButton} icon=${ok ? 'check' : 'copy'} label=${ok ? done : label} size=${size} onClick=${onClick} />`;
  return html`<${Button} size=${size} variant=${variant} icon=${ok ? 'check' : 'copy'} onClick=${onClick}>${ok ? done : label}<//>`;
}

/** A write-only secret input with show/hide. Never prefilled with a secret. */
export function SecretField({ label, help, error, value, onInput, placeholder, id: givenId, autoComplete = 'off' }) {
  const auto = useId();
  const id = givenId || `s${auto}`;
  const [show, setShow] = useState(false);
  return html`<${Field} id=${id} label=${label} help=${help} error=${error}>
    <div class="input-group">
      <input id=${id} class="input mono" type=${show ? 'text' : 'password'} value=${value} placeholder=${placeholder}
        autocomplete=${autoComplete} spellcheck="false" autocapitalize="off"
        onInput=${(e) => onInput(e.currentTarget.value)} aria-invalid=${error ? 'true' : undefined}
        aria-describedby=${describedBy(id, help, error)} />
      <${IconButton} icon=${show ? 'eye-off' : 'eye'} label=${show ? 'Verbergen' : 'Anzeigen'} onClick=${() => setShow(!show)} />
    </div>
  <//>`;
}

// ---------------------------------------------------------------------------------------------------------------
// Modal + drawer (native <dialog>: focus trap, Esc, inert background)
// ---------------------------------------------------------------------------------------------------------------

export function Modal({ title, onClose, children, footer, size = 'md', drawer = false, class: klass, labelledBy }) {
  const ref = useRef(null);
  const titleId = useId();
  useEffect(() => {
    const d = ref.current;
    if (d && !d.open) d.showModal();
    const onCancel = (e) => { e.preventDefault(); onClose && onClose(); };
    d.addEventListener('cancel', onCancel);
    return () => { d.removeEventListener('cancel', onCancel); if (d.open) d.close(); };
  }, []);
  const onClick = (e) => { if (e.target === ref.current) onClose && onClose(); };
  return html`<dialog ref=${ref} class=${cx('modal', drawer ? 'drawer' : `modal-${size}`, klass)}
    aria-labelledby=${labelledBy || `m${titleId}`} onClick=${onClick}>
    <div class="modal-inner">
      <div class="modal-head">
        <h2 class="modal-title" id=${`m${titleId}`}>${title}</h2>
        <${IconButton} icon="x" label="Schließen" onClick=${onClose} />
      </div>
      <div class="modal-body">${children}</div>
      ${footer && html`<div class="modal-foot">${footer}</div>`}
    </div>
  </dialog>`;
}

export function DialogHost() {
  const d = useStore(dialogStore);
  const [text, setText] = useState('');
  useEffect(() => { setText(''); }, [d]);
  if (!d) return null;
  const close = (v) => d.resolve(v);
  const confirm = (e) => {
    e && e.preventDefault();
    if (d.input) {
      if (!d.input.optional && !text.trim()) return;
      close(text.trim() || '');
    } else close(true);
  };
  return html`<${Modal} title=${d.title} onClose=${() => close(false)} size="sm"
    footer=${html`<${Button} variant="ghost" onClick=${() => close(false)}>${d.cancelLabel || 'Abbrechen'}<//>
      <${Button} variant=${d.danger ? 'danger' : 'primary'} onClick=${confirm} autofocus=${!d.input}>${d.confirmLabel || 'OK'}<//>`}>
    ${d.message && html`<div class="dialog-message">${d.message}</div>`}
    ${d.input && html`<form onSubmit=${confirm}>
      ${d.input.multiline
        ? html`<${TextareaField} label=${d.input.label} optional=${d.input.optional} value=${text} onInput=${setText} placeholder=${d.input.placeholder} autofocus rows=${3} />`
        : html`<${TextField} label=${d.input.label} optional=${d.input.optional} value=${text} onInput=${setText} placeholder=${d.input.placeholder} autofocus />`}
    </form>`}
  <//>`;
}

export function Toasts() {
  const list = useStore(toastStore);
  return html`<div class="toasts" aria-live="polite" aria-relevant="additions">
    ${list.map((t) => html`<div class=${cx('toast', `toast-${t.kind}`)} role=${t.kind === 'error' ? 'alert' : 'status'} key=${t.id}>
      <${Icon} name=${t.kind === 'error' ? 'alert-circle' : t.kind === 'success' ? 'circle-check' : 'info'} size=${18} />
      <span class="toast-msg">${t.message}</span>
      ${t.action && html`<a class="toast-action" href=${t.action.href} onClick=${() => dismissToast(t.id)}>${t.action.label}</a>`}
      <button type="button" class="toast-x" aria-label="Meldung schließen" onClick=${() => dismissToast(t.id)}><${Icon} name="x" size=${14} /></button>
    </div>`)}
  </div>`;
}

/** Shows a form-level error from an ApiError (message) above a form. */
export function FormError({ error }) {
  if (!error) return null;
  return html`<${Callout} tone="danger" title=${errorText(error)} />`;
}

/** Lazily mounts children once scrolled near the viewport (for <video> previews). */
export function LazyMount({ children, placeholder, rootMargin = '300px' }) {
  const ref = useRef(null);
  const [show, setShow] = useState(false);
  useEffect(() => {
    if (show || !ref.current) return undefined;
    if (!('IntersectionObserver' in window)) { setShow(true); return undefined; }
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) { setShow(true); io.disconnect(); }
    }, { rootMargin });
    io.observe(ref.current);
    return () => io.disconnect();
  }, [show]);
  return html`<div ref=${ref} class="lazy">${show ? children : placeholder}</div>`;
}
