// Login: username, password and a 2FA code (or one recovery code).
import * as api from '../api.js';
import { html, useState, useTitle } from '../lib.js';
import { Button, TextField, FormError, Callout } from '../ui.js';
import { sessionStore } from '../store.js';

export function AuthFrame({ title, subtitle, children, wide, foot }) {
  return html`<main class="auth"><div class=${`auth-card${wide ? ' wide' : ''}`}>
    <div class="auth-brand"><span class="brand-mark" aria-hidden="true">GC</span>
      <span><span class="brand-name">GTA Clipper</span> <span class="brand-sub">· Automatik</span></span></div>
    ${title && html`<div class="auth-title"><h1>${title}</h1>${subtitle && html`<p>${subtitle}</p>`}</div>`}
    ${children}
    ${foot && html`<p class="auth-foot">${foot}</p>`}
  </div></main>`;
}

export function LoginPage({ route }) {
  useTitle('Anmelden');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [recovery, setRecovery] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const fields = (error && error.fields) || {};
  const expired = route.query.next && !error;

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const state = await api.login({ username: username.trim(), password, code: code.replace(/\s+/g, '') });
      sessionStore.set({ state: { ...state, authenticated: true }, error: null });
    } catch (err) {
      setError(err);
      setCode('');
    } finally {
      setBusy(false);
    }
  };

  return html`<${AuthFrame} title="Anmelden" subtitle="Dein privates Dashboard. Nur du hast Zugang (Passwort + 2FA)."
    foot="Passwort und 2FA verloren? Im Terminal: uv run python -m automate reset-login">
    <form class="card stack" onSubmit=${submit} novalidate>
      ${expired && html`<${Callout} tone="info" title="Bitte melde dich (erneut) an." />`}
      <${FormError} error=${error && !error.fields ? error : null} />
      <${TextField} label="Benutzername" value=${username} onInput=${setUsername} autocomplete="username"
        autocapitalize="off" spellcheck="false" required error=${fields.username} autofocus />
      <${TextField} label="Passwort" type="password" value=${password} onInput=${setPassword} autocomplete="current-password"
        required error=${fields.password} />
      ${recovery
        ? html`<${TextField} label="Wiederherstellungscode" value=${code} onInput=${setCode} autocomplete="off"
            autocapitalize="off" spellcheck="false" inputClass="mono" error=${fields.code}
            help="Jeder Wiederherstellungscode funktioniert nur einmal." />`
        : html`<${TextField} label="2FA-Code" value=${code} onInput=${(v) => setCode(v.replace(/[^\d ]/g, ''))}
            inputmode="numeric" autocomplete="one-time-code" maxLength=${7} inputClass="mono" error=${fields.code}
            help="6-stelliger Code aus deiner Authenticator-App." />`}
      <button type="button" class="linklike small" style="align-self:flex-start" onClick=${() => { setRecovery(!recovery); setCode(''); }}>
        ${recovery ? 'Stattdessen 2FA-Code eingeben' : 'Kein Zugriff auf die App? Wiederherstellungscode verwenden'}
      </button>
      <${Button} type="submit" variant="primary" busy=${busy} block disabled=${!username || !password || !code}>Anmelden<//>
    </form>
  <//>`;
}
