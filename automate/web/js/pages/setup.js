// First-run wizard: Konto → 2FA → Wiederherstellungscodes → Schlüssel → Systemcheck → Quellen → Automatik.
// Account and 2FA are mandatory; every later step can be skipped (and reopened any time at #/einrichtung/…).
import * as api from '../api.js';
import { html, useState, useEffect, navigate, useData, useTitle, cx, fmt, toast, setQuery } from '../lib.js';
import {
  Button, TextField, FormError, Callout, Loading, ErrorState, Toggle, Icon, CopyButton,
} from '../ui.js';
import { KeyEditor, ToolList, TotpEnroll, RecoveryCodes, INSTALL_ALL, PlatformIcon } from '../parts.js';
import { refreshSession, sessionStore, loadProfiles } from '../store.js';
import { AuthFrame } from './login.js';

const STEPS = [
  { id: 'konto', label: 'Konto' },
  { id: '2fa', label: '2FA' },
  { id: 'codes', label: 'Codes' },
  { id: 'schluessel', label: 'Schlüssel' },
  { id: 'system', label: 'Systemcheck' },
  { id: 'quellen', label: 'Quellen' },
  { id: 'automatik', label: 'Automatik' },
];

// Survives re-renders and route changes (not a reload: the QR secret and the codes are never stored).
const pending = { setup: null, username: '', codes: null, token: readToken() };

// The one-time setup code the server prints in its terminal (and puts in the link it opens). Kept for this tab only
// (sessionStorage), so a reload mid-setup doesn't ask for it again; dropped once the setup is done.
const TOKEN_KEY = 'automate.setup_token';
function readToken() {
  try { return sessionStorage.getItem('automate.setup_token') || ''; } catch { return ''; }
}
function keepToken(t) {
  pending.token = t;
  try { if (t) sessionStorage.setItem(TOKEN_KEY, t); else sessionStorage.removeItem(TOKEN_KEY); } catch { /* private mode */ }
}

function Stepper({ current }) {
  const idx = STEPS.findIndex((s) => s.id === current);
  return html`<div><ol class="stepper" aria-label="Fortschritt der Einrichtung">
    ${STEPS.map((s, i) => html`<li class=${cx('step', i < idx && 'is-done', i === idx && 'is-current')}
      aria-current=${i === idx ? 'step' : undefined}>
      <span class="step-bar"></span><span class="step-name">${i + 1}. ${s.label}</span>
      <span class="sr-only">${i < idx ? '(erledigt)' : ''}</span>
    </li>`)}
  </ol><p class="step-caption" aria-hidden="true">Schritt ${idx + 1} von ${STEPS.length}: ${STEPS[idx] && STEPS[idx].label}</p></div>`;
}

function go(step) { navigate(`/einrichtung/${step}`); }
function nextOf(step) { const i = STEPS.findIndex((s) => s.id === step); return STEPS[i + 1] ? STEPS[i + 1].id : null; }

function WizardNav({ step, canSkip = true, nextLabel = 'Weiter', nextDisabled, onNext, busy }) {
  const next = nextOf(step);
  const done = () => navigate('/uebersicht');
  return html`<div class="row-between">
    ${canSkip ? html`<${Button} variant="ghost" onClick=${() => (next ? go(next) : done())}>Überspringen<//>` : html`<span></span>`}
    <${Button} variant="primary" iconRight="chevron-right" busy=${busy} disabled=${nextDisabled}
      onClick=${onNext || (() => (next ? go(next) : done()))}>${nextLabel}<//>
  </div>`;
}

export function SetupPage({ route, state }) {
  useTitle('Einrichtung');
  // the link from the terminal carries the code: take it, then drop it from the address bar (and the history)
  useEffect(() => {
    if (route.query.token) { keepToken(route.query.token.trim()); setQuery({ token: '' }); }
  }, [route.query.token]);
  let step = route.parts[1] || 'konto';
  if (!STEPS.some((s) => s.id === step)) step = 'konto';
  let redirect = null;
  if (state.setup_required) {
    if (step === '2fa' && !pending.setup) redirect = 'konto';
    else if (step !== 'konto' && step !== '2fa') redirect = 'konto';
  } else if (step === 'konto' || step === '2fa') {
    redirect = pending.codes ? 'codes' : 'schluessel';
  } else if (step === 'codes' && !pending.codes) {
    redirect = 'schluessel';
  }
  useEffect(() => { if (redirect) navigate(`/einrichtung/${redirect}`, { replace: true }); }, [redirect]);
  if (redirect) return null;

  const Step = { konto: AccountStep, '2fa': TotpStep, codes: CodesStep, schluessel: KeysStep, system: SystemStep, quellen: SourcesStep, automatik: AutomationStep }[step];
  const titles = {
    konto: ['Willkommen!', 'Lege zuerst dein Konto an. Das Dashboard ist nur für dich – geschützt mit Passwort und 2FA.'],
    '2fa': ['Zwei-Faktor-Anmeldung', 'Zusätzlich zum Passwort brauchst du beim Anmelden einen Code aus deiner Authenticator-App.'],
    codes: ['Wiederherstellungscodes', 'Falls dein Handy einmal weg ist.'],
    schluessel: ['API-Schlüssel', 'Damit Claude die Momente auswählt und (optional) Fish Audio die Voiceovers spricht.'],
    system: ['Systemcheck', 'Diese Programme braucht der Clipper auf diesem Rechner.'],
    quellen: ['Quellen prüfen', 'Diese Creator und Live-Streams wurden aus deinem Repo übernommen.'],
    automatik: ['Automatik einschalten', 'Fast geschafft. Du kannst das auch später auf der Übersicht machen.'],
  }[step];
  return html`<${AuthFrame} wide title=${titles[0]} subtitle=${titles[1]}
    foot=${state.setup_required ? 'Alles bleibt lokal auf diesem PC.' : html`<a href="#/uebersicht">Assistent verlassen und zur Übersicht</a>`}>
    <${Stepper} current=${step} />
    <div class="card stack-lg"><${Step} state=${state} /></div>
  <//>`;
}

// ---------------------------------------------------------------------------------------------------------------

function AccountStep({ state }) {
  const [token, setToken] = useState(pending.token || '');
  useEffect(() => { if (pending.token) setToken(pending.token); }, [pending.token]);  // a link opened later wins
  const [username, setUsername] = useState(pending.username || '');
  const [password, setPassword] = useState('');
  const [repeat, setRepeat] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [touched, setTouched] = useState(false);
  const local = {};
  if (touched && password && password.length < 10) local.password = 'Mindestens 10 Zeichen.';
  if (touched && repeat && repeat !== password) local.repeat = 'Die Passwörter stimmen nicht überein.';
  const fields = { ...local, ...((error && error.fields) || {}) };

  const submit = async (e) => {
    e.preventDefault();
    setTouched(true);
    if (password.length < 10 || password !== repeat || !username.trim() || !token.trim()) return;
    setBusy(true);
    setError(null);
    try {
      keepToken(token.trim());
      pending.setup = await api.setup({ username: username.trim(), password, token: pending.token });
      pending.username = username.trim();
      go('2fa');
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return html`<form class="stack" onSubmit=${submit} novalidate>
    ${state.setup_step === 'totp' && html`<${Callout} tone="info" title="Die Einrichtung wurde unterbrochen.">
      Das Konto existiert schon, aber 2FA ist noch nicht bestätigt. Gib Benutzername und Passwort noch einmal ein – es gibt dann einen neuen QR-Code.<//>`}
    <${FormError} error=${error && !error.fields ? error : null} />
    <${TextField} label="Einrichtungs-Code" value=${token} onInput=${setToken} autocomplete="off" autocapitalize="characters"
      spellcheck="false" required inputClass="mono" placeholder="XXXX-XXXX-XXXX-XXXX-XXXX" error=${fields.token}
      help=${pending.token && token === pending.token ? 'Aus dem Link übernommen, den der Server beim Start geöffnet hat.'
        : 'Steht im Terminal, in dem „uv run python -m automate“ läuft (Zeile „Einrichtungs-Code“). Nur wer diesen Code hat, kann das Konto anlegen.'} />
    <${TextField} label="Benutzername" value=${username} onInput=${setUsername} autocomplete="username" autocapitalize="off"
      spellcheck="false" required error=${fields.username} autofocus />
    <div class="form-grid">
      <${TextField} label="Passwort" type="password" value=${password} onInput=${setPassword} autocomplete="new-password"
        required error=${fields.password} help="Mindestens 10 Zeichen. Am besten ein Satz oder aus dem Passwort-Manager." />
      <${TextField} label="Passwort wiederholen" type="password" value=${repeat} onInput=${setRepeat} autocomplete="new-password"
        required error=${fields.repeat} onBlur=${() => setTouched(true)} />
    </div>
    <div class="row-between">
      <span class="muted small">Schritt 1 von ${STEPS.length} · Pflicht</span>
      <${Button} type="submit" variant="primary" iconRight="chevron-right" busy=${busy}
        disabled=${!username.trim() || !password || !repeat || !token.trim()}>Konto anlegen<//>
    </div>
  </form>`;
}

function TotpStep() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const confirm = async (code) => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.setupConfirm({ code, token: pending.token });
      pending.codes = res.recovery_codes || [];
      pending.setup = null;
      keepToken('');
      const st = await refreshSession();
      if (st && !st.authenticated) sessionStore.set({ state: { ...st, authenticated: true }, error: null });
      go('codes');
    } catch (err) {
      if (err.code === 'setup_restart' || err.code === 'setup_token_invalid' || err.code === 'setup_token_missing') {
        toast.error(err.message);  // started over elsewhere, or the server restarted with a new code: back to step 1
        pending.setup = null;
        if (err.code !== 'setup_restart') keepToken('');
        go('konto');
        return;
      }
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return html`<${TotpEnroll} setup=${pending.setup} onConfirm=${confirm} busy=${busy} error=${error} />
    <p class="muted small">Schritt 2 von ${STEPS.length} · Pflicht. Ohne bestätigten Code wird das Konto nicht aktiviert.</p>`;
}

function CodesStep({ state }) {
  const [saved, setSaved] = useState(false);
  const codes = pending.codes || [];
  return html`<${RecoveryCodes} codes=${codes} username=${state.username} />
    <label class="checkbox"><input type="checkbox" checked=${saved} onChange=${(e) => setSaved(e.currentTarget.checked)} />
      Ich habe die Codes sicher gespeichert.</label>
    <${WizardNav} step="codes" canSkip=${false} nextDisabled=${!saved} onNext=${() => { pending.codes = null; go('schluessel'); }} />`;
}

function KeysStep() {
  const keys = useData(() => api.listKeys().then(api.itemsOf), []);
  const byName = Object.fromEntries((keys.data || []).map((k) => [k.name, k]));
  const update = (k) => keys.setData((list) => (list || []).map((x) => (x.name === k.name ? { ...x, ...k } : x)));
  if (keys.loading && !keys.data) return html`<${Loading} lines=${5} />`;
  if (keys.error && !keys.data) return html`<${ErrorState} error=${keys.error} onRetry=${keys.reload} />`;
  return html`<${KeyEditor} name="anthropic" info=${byName.anthropic} onChange=${update} headingLevel=${2} />
    <hr style="margin:0" />
    <${KeyEditor} name="fish" info=${byName.fish} onChange=${update} headingLevel=${2} />
    <${WizardNav} step="schluessel" />`;
}

function SystemStep() {
  const sys = useData(() => api.getSystem(), []);
  if (sys.loading && !sys.data) return html`<${Loading} lines=${6} label="Prüfe Werkzeuge …" />`;
  if (sys.error && !sys.data) return html`<${ErrorState} error=${sys.error} onRetry=${sys.reload} />`;
  const tools = sys.data.tools || [];
  const missing = tools.filter((t) => !t.ok && t.required !== false);
  return html`${missing.length
    ? html`<${Callout} tone="warning" title=${`${missing.length} ${missing.length === 1 ? 'Werkzeug fehlt' : 'Werkzeuge fehlen'}`}>
        Installiere sie im Terminal (im Ordner des Clippers) mit:
        <div class="uri-box" style="margin-top:8px"><code>${INSTALL_ALL}</code><${CopyButton} text=${INSTALL_ALL} iconOnly label="Befehl kopieren" /></div>
      <//>`
    : html`<${Callout} tone="success" title="Alles Nötige ist installiert." />`}
    <${ToolList} tools=${tools} />
    <div class="row muted small"><${Icon} name="hard-drive" size=${15} />
      ${sys.data.disk ? `${fmt.gb(sys.data.disk.free_gb)} frei von ${fmt.gb(sys.data.disk.total_gb)}` : 'Speicherplatz unbekannt'}
      <${Button} size="sm" variant="ghost" icon="refresh-cw" busy=${sys.loading} onClick=${sys.reload}>Erneut prüfen<//></div>
    <${WizardNav} step="system" />`;
}

function SourcesStep() {
  const data = useData(async () => {
    const profiles = await loadProfiles(true);
    const enabled = profiles.filter((p) => p.enabled);
    const name = (enabled[0] || profiles[0] || { name: 'gta6' }).name;
    const [creators, streams] = await Promise.all([api.listCreators({ profile: name }), api.listStreams({ profile: name })]);
    return { profiles, name, creators: api.itemsOf(creators), streams: api.itemsOf(streams) };
  }, []);
  const [busyId, setBusyId] = useState(null);
  if (data.loading && !data.data) return html`<${Loading} lines=${6} />`;
  if (data.error && !data.data) return html`<${ErrorState} error=${data.error} onRetry=${data.reload} />`;
  const d = data.data;
  const togglePause = async (kind, item) => {
    setBusyId(`${kind}${item.id}`);
    try {
      const upd = kind === 'c' ? await api.updateCreator(item.id, { paused: !item.paused }) : await api.updateStream(item.id, { paused: !item.paused });
      data.setData((x) => ({ ...x, [kind === 'c' ? 'creators' : 'streams']: x[kind === 'c' ? 'creators' : 'streams'].map((i) => (i.id === upd.id ? upd : i)) }));
    } catch (err) { toast.fail(err); } finally { setBusyId(null); }
  };
  return html`<p class="text-2">Profil <b>${d.name}</b>. Pausierte Quellen werden nicht durchsucht bzw. aufgenommen. Hinzufügen und Prioritäten setzt du später unter <a href="#/quellen">Quellen</a>.</p>
    <div class="grid grid-2">
      <div class="stack-sm">
        <h2>YouTube-Creator <span class="muted">(${d.creators.length})</span></h2>
        ${d.creators.length ? html`<div class="list">${d.creators.map((c) => html`<div class=${cx('list-row', c.paused && 'is-paused')} key=${c.id}>
          <${PlatformIcon} platform="youtube" />
          <div class="list-main"><span class="list-title">${c.name}</span><span class="list-sub">${c.url}</span></div>
          <${Toggle} checked=${!c.paused} hideLabel label=${`${c.name} aktiv`} busy=${busyId === `c${c.id}`} onChange=${() => togglePause('c', c)} />
        </div>`)}</div>` : html`<p class="muted">Noch keine Creator.</p>`}
      </div>
      <div class="stack-sm">
        <h2>Live-Streams <span class="muted">(${d.streams.length})</span></h2>
        ${d.streams.length ? html`<div class="list">${d.streams.map((s) => html`<div class=${cx('list-row', s.paused && 'is-paused')} key=${s.id}>
          <${PlatformIcon} platform=${s.platform || 'other'} />
          <div class="list-main"><span class="list-title">${s.name}</span><span class="list-sub">${s.url}</span></div>
          <${Toggle} checked=${!s.paused} hideLabel label=${`${s.name} aktiv`} busy=${busyId === `s${s.id}`} onChange=${() => togglePause('s', s)} />
        </div>`)}</div>` : html`<p class="muted">Noch keine Live-Streams.</p>`}
      </div>
    </div>
    <${WizardNav} step="quellen" />`;
}

function AutomationStep() {
  const data = useData(async () => {
    const [ov, settings] = await Promise.all([api.getOverview(), api.getSettings()]);
    return { automation: ov.automation, ready: ov.ready, times: (settings.values && settings.values['schedule.scan_times']) || [] };
  }, []);
  const [busy, setBusy] = useState(null);
  if (data.loading && !data.data) return html`<${Loading} lines=${4} />`;
  if (data.error && !data.data) return html`<${ErrorState} error=${data.error} onRetry=${data.reload} />`;
  const d = data.data;
  const set = async (patch, key) => {
    setBusy(key);
    try {
      const auto = await api.setAutomation(patch);
      data.setData((x) => ({ ...x, automation: { ...x.automation, ...auto } }));
    } catch (err) { toast.fail(err); } finally { setBusy(null); }
  };
  const finish = () => { toast.success('Einrichtung abgeschlossen.'); navigate('/uebersicht'); };
  return html`${!d.ready.anthropic_key && html`<${Callout} tone="warning" title="Noch kein Anthropic-Schlüssel">
      Die Automatik findet und analysiert Videos trotzdem, wartet aber vor Schritt ③ auf den Schlüssel. <a href="#/einrichtung/schluessel">Jetzt eintragen</a><//>`}
    <${Toggle} size="lg" checked=${d.automation.enabled} busy=${busy === 'enabled'} onChange=${(v) => set({ enabled: v }, 'enabled')}
      label="Automatik (tägliche Suche)" description=${`Sucht ${d.times.length ? `täglich um ${d.times.join(', ')} Uhr` : 'nach Zeitplan'} nach neuen GTA-6-Videos deiner Creator und schlägt Clips vor. Zeiten änderst du unter Einstellungen.`} />
    <${Toggle} size="lg" checked=${d.automation.live_enabled} busy=${busy === 'live'} onChange=${(v) => set({ live_enabled: v }, 'live')}
      label="Live-Aufnahme" description="Prüft alle paar Minuten, ob deine Streams live GTA 6 spielen, nimmt sie auf und clippt jeden fertigen 30-Minuten-Chunk." />
    <${Callout} tone="info">Neue Picks landen in der <b>Freigabe</b>: Du prüfst Hooks und Titel und gibst sie zum Rendern frei (oder stellst unter Einstellungen → Freigabe auf automatisch).<//>
    <div class="row-between">
      <${Button} variant="ghost" onClick=${() => navigate('/uebersicht')}>Später<//>
      <${Button} variant="primary" icon="check" onClick=${finish}>Fertig – zur Übersicht<//>
    </div>`;
}
