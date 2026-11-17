// System: version, tool checks with install hints, disk, paths; account (password, 2FA) and logout.
import * as api from '../api.js';
import { html, useState, useData, useTitle, useStore, fmt, cx, toast, navigate } from '../lib.js';
import {
  PageHeader, Card, Button, Loading, ErrorState, KV, CopyButton, Callout, TextField, FormError, Modal, Icon, Badge,
} from '../ui.js';
import { ToolList, TotpEnroll, RecoveryCodes, INSTALL_ALL } from '../parts.js';
import { sessionStore, refreshSession, sessionExpired } from '../store.js';

const PATH_LABELS = { root: 'Programmordner', data: 'Daten', database: 'Datenbank', db: 'Datenbank', jobs: 'Jobs (Quellvideos)', exports: 'Exporte (fertige Videos)', logs: 'Logs', runtime: 'Laufzeit-Konfiguration' };

export function SystemPage() {
  useTitle('System');
  const sys = useData(() => api.getSystem(), []);
  const d = sys.data;
  return html`<div class="page">
    <${PageHeader} title="System" subtitle="Was auf diesem PC installiert ist, wie viel Platz frei ist, und dein Login."
      actions=${html`<${Button} icon="wand-sparkles" href="#/einrichtung/schluessel">Einrichtungsassistent<//>`} />
    ${sys.error && !d ? html`<${ErrorState} error=${sys.error} onRetry=${sys.reload} />` : !d ? html`<div class="card"><${Loading} lines=${10} /></div>` : html`
      <div class="grid grid-main">
        <${Card} title="Werkzeuge" icon="wrench" subtitle=${toolSummary(d.tools || [])}
          actions=${html`<${Button} size="sm" icon="refresh-cw" busy=${sys.loading} onClick=${sys.reload}>Erneut prüfen<//>`}>
          ${(d.tools || []).some((t) => !t.ok && t.required !== false) && html`<${Callout} tone="warning" title="Fehlende Werkzeuge installieren" class="small">
            Im Terminal im Ordner des Clippers:
            <div class="uri-box" style="margin-top:8px"><code>${INSTALL_ALL}</code><${CopyButton} text=${INSTALL_ALL} iconOnly label="Befehl kopieren" /></div>
            Danach den Server neu starten und hier „Erneut prüfen“.<//>`}
          <${ToolList} tools=${d.tools} />
          <${AsrModel} model=${d.asr_model} />
          ${d.setup_report && html`<details class="details" style="margin-top:8px"><summary><${Icon} name="chevron-right" size=${16} />Ausgabe von „clipper setup“</summary>
            <div class="details-body"><pre class="playbook" style="max-height:300px">${d.setup_report}</pre></div></details>`}
        <//>
        <div class="stack-lg">
          <${Card} title="Speicherplatz" icon="hard-drive"><${Disk} disk=${d.disk} /><//>
          <${Card} title="Version" icon="info">
            <${KV} items=${[['GTA Clipper', d.version], ['Python', d.python], ['System', d.platform]]} />
          <//>
        </div>
      </div>
      <${Card} title="Pfade" icon="folder" subtitle="Wo die Daten auf diesem PC liegen.">
        <${KV} items=${Object.entries(d.paths || {}).map(([k, v]) => [PATH_LABELS[k] || k,
          html`<span class="row" style="gap:4px;flex-wrap:nowrap"><code style="overflow-wrap:anywhere">${v}</code><${CopyButton} text=${v} iconOnly label=${`Pfad ${PATH_LABELS[k] || k} kopieren`} /></span>`])} />
      <//>`}
    <${AccountCard} />
  </div>`;
}

function AsrModel({ model }) {
  if (!model || model.cached == null) return null;
  return html`<p class=${cx('issue small', model.cached ? '' : 'issue-note')} style="margin-top:8px">
    <${Icon} name=${model.cached ? 'circle-check' : 'info'} size=${14} />
    <span>${model.cached
      ? html`Parakeet-Sprachmodell ist heruntergeladen (<span class="mono">${model.name}</span>).`
      : html`Das Parakeet-Sprachmodell ist noch nicht heruntergeladen: die erste Analyse lädt es (rund 650 MB) von huggingface.co und dauert deshalb länger.`}</span></p>`;
}

function toolSummary(tools) {
  const missing = tools.filter((t) => !t.ok && t.required !== false).length;
  return missing ? `${missing} fehlen · ${tools.filter((t) => t.ok).length} installiert` : `Alle ${tools.filter((t) => t.ok).length} nötigen Werkzeuge installiert`;
}

function Disk({ disk }) {
  if (!disk) return html`<p class="muted">Keine Angaben.</p>`;
  const used = Math.max(0, (disk.total_gb || 0) - (disk.free_gb || 0));
  const low = disk.min_free_gb != null && disk.free_gb < disk.min_free_gb;
  return html`<div class="stack-sm">
    <div class="row-between"><span><b class="num">${fmt.gb(disk.free_gb)}</b> frei</span><span class="muted">von ${fmt.gb(disk.total_gb)}</span></div>
    <div class=${cx('disk-bar', low && 'is-low')} role="meter" aria-valuemin="0" aria-valuemax=${disk.total_gb} aria-valuenow=${used} aria-label="Belegter Speicher">
      <span style=${`width:${disk.total_gb ? (used / disk.total_gb) * 100 : 0}%`}></span></div>
    ${low ? html`<p class="issue issue-fail"><${Icon} name="alert-circle" size=${14} />Unter ${fmt.gb(disk.min_free_gb)} frei: Neue Downloads und Live-Aufnahmen warten. Alte Quellvideos löschen oder die Aufbewahrung verkürzen (Einstellungen → Analyse).</p>`
      : html`<p class="muted small">Alarm unter ${fmt.gb(disk.min_free_gb)} · <code style="overflow-wrap:anywhere">${disk.path}</code></p>`}
  </div>`;
}

function AccountCard() {
  const { state } = useStore(sessionStore);
  const [modal, setModal] = useState(null);
  const [loggingOut, setLoggingOut] = useState(false);
  const logout = async () => {
    setLoggingOut(true);
    try { await api.logout(); } catch { /* the session is gone either way */ }
    sessionExpired();
    await refreshSession();
    navigate('/login', { replace: true });
  };
  return html`<${Card} title="Dein Login" icon="shield-check">
    <div class="row-between">
      <div class="stack-sm">
        <span>Angemeldet als <b>${state && state.username}</b></span>
        <span class="row">${state && state.totp_enabled ? html`<${Badge} tone="green" icon="shield-check">2FA aktiv<//>` : html`<${Badge} tone="yellow">2FA nicht aktiv<//>`}</span>
      </div>
      <div class="row">
        <${Button} icon="lock" onClick=${() => setModal('password')}>Passwort ändern<//>
        <${Button} icon="smartphone" onClick=${() => setModal('totp')}>2FA neu einrichten<//>
        <${Button} variant="ghost" icon="log-out" busy=${loggingOut} onClick=${logout}>Abmelden<//>
      </div>
    </div>
    <p class="muted small" style="margin-top:10px">Ausgesperrt? Im Terminal setzt <code>uv run python -m automate reset-login</code> den Login zurück.</p>
    ${modal === 'password' && html`<${PasswordModal} onClose=${() => setModal(null)} />`}
    ${modal === 'totp' && html`<${TotpModal} onClose=${() => setModal(null)} username=${state && state.username} />`}
  <//>`;
}

function PasswordModal({ onClose }) {
  const [cur, setCur] = useState('');
  const [next, setNext] = useState('');
  const [repeat, setRepeat] = useState('');
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const local = {};
  if (next && next.length < 10) local.new_password = 'Mindestens 10 Zeichen.';
  if (repeat && repeat !== next) local.repeat = 'Stimmt nicht überein.';
  const f = { ...local, ...((error && error.fields) || {}) };
  const submit = async (e) => {
    e && e.preventDefault();
    if (local.new_password || local.repeat) return;
    setBusy(true); setError(null);
    try { await api.changePassword({ current_password: cur, new_password: next, code: code.replace(/\s+/g, '') }); toast.success('Passwort geändert.'); onClose(); }
    catch (err) { setError(err); } finally { setBusy(false); }
  };
  return html`<${Modal} title="Passwort ändern" onClose=${onClose} size="sm"
    footer=${html`<${Button} variant="ghost" onClick=${onClose}>Abbrechen<//><${Button} variant="primary" busy=${busy}
      disabled=${!cur || !next || next !== repeat || next.length < 10 || code.replace(/\s+/g, '').length < 6} onClick=${submit}>Ändern<//>`}>
    <form class="stack" onSubmit=${submit}>
      <${FormError} error=${error && !error.fields ? error : null} />
      <${TextField} label="Aktuelles Passwort" type="password" value=${cur} onInput=${setCur} autocomplete="current-password" error=${f.current_password} autofocus />
      <${TextField} label="Neues Passwort" type="password" value=${next} onInput=${setNext} autocomplete="new-password" error=${f.new_password} help="Mindestens 10 Zeichen." />
      <${TextField} label="Neues Passwort wiederholen" type="password" value=${repeat} onInput=${setRepeat} autocomplete="new-password" error=${f.repeat} />
      <${TextField} label="2FA-Code" value=${code} onInput=${(v) => setCode(v.replace(/[^\d ]/g, ''))} inputmode="numeric" autocomplete="one-time-code" maxLength=${7} inputClass="mono" error=${f.code} />
      <button type="submit" hidden></button>
    </form>
  <//>`;
}

function TotpModal({ onClose, username }) {
  const [password, setPassword] = useState('');
  const [setup, setSetup] = useState(null);
  const [codes, setCodes] = useState(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const start = async (e) => {
    e && e.preventDefault();
    setBusy(true); setError(null);
    try { setSetup(await api.resetTotp({ password })); } catch (err) { setError(err); } finally { setBusy(false); }
  };
  const confirm = async (code) => {
    setBusy(true); setError(null);
    try { const res = await api.confirmTotp({ code }); setCodes(res.recovery_codes || []); refreshSession(); toast.success('2FA neu eingerichtet.'); }
    catch (err) { setError(err); } finally { setBusy(false); }
  };
  const f = (error && error.fields) || {};
  return html`<${Modal} title="2FA neu einrichten" onClose=${codes && !saved ? () => {} : onClose} size="lg"
    footer=${codes ? html`<label class="checkbox"><input type="checkbox" checked=${saved} onChange=${(e) => setSaved(e.currentTarget.checked)} />Codes gesichert</label>
      <${Button} variant="primary" disabled=${!saved} onClick=${onClose}>Fertig<//>` : !setup ? html`<${Button} variant="ghost" onClick=${onClose}>Abbrechen<//>
      <${Button} variant="primary" busy=${busy} disabled=${!password} onClick=${start}>Weiter<//>` : null}>
    ${codes ? html`<${RecoveryCodes} codes=${codes} username=${username} />`
      : setup ? html`<${Callout} tone="info">Bis du den neuen Code bestätigst, gilt die alte App weiter.<//><${TotpEnroll} setup=${setup} onConfirm=${confirm} busy=${busy} error=${error} />`
      : html`<form class="stack" onSubmit=${start}>
          <p class="text-2">Für ein neues Handy oder eine neue Authenticator-App. Danach gelten neue Wiederherstellungscodes.</p>
          <${FormError} error=${error && !error.fields ? error : null} />
          <${TextField} label="Passwort zur Bestätigung" type="password" value=${password} onInput=${setPassword} autocomplete="current-password" error=${f.password} autofocus />
        </form>`}
  <//>`;
}
