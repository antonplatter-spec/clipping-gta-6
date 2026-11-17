// Larger building blocks used on more than one page: API key editor, tool check list, 2FA enrolment,
// recovery codes, platform icon.
import * as api from './api.js';
import { html, useState, cx, downloadText, toast, confirmDialog } from './lib.js';
import { Icon, Button, Badge, SecretField, TextField, Callout, CopyButton, Rel, FormError } from './ui.js';
import { TOOL_PURPOSE, PLATFORMS, STREAM_PLATFORMS } from './labels.js';

// ---------------------------------------------------------------------------------------------------------------
// API keys (never rendered: only the masked value from the server)
// ---------------------------------------------------------------------------------------------------------------

export const KEY_HELP = {
  anthropic: {
    title: 'Anthropic (Claude API)',
    required: true,
    why: 'Pflicht für die Automatik: Claude wählt damit die Momente, schreibt Hooks und Titel und übersetzt Dubs.',
    how: html`So bekommst du ihn: <a href="https://platform.claude.com/settings/keys" target="_blank" rel="noopener noreferrer" class="ext">platform.claude.com</a>
      (Claude Console) öffnen → anmelden → <b>API Keys</b> → <b>Create Key</b>. Der Schlüssel beginnt mit <code>sk-ant-</code>.
      Jede Anfrage kostet Geld; das Tageslimit stellst du unter „Kosten & Schlüssel“ ein.`,
    placeholder: 'sk-ant-…',
  },
  fish: {
    title: 'Fish Audio',
    required: false,
    why: 'Optional: KI-Voiceover und Dubs in der geklonten Stimme des Streamers. Ohne Schlüssel nutzt das Voiceover die System-Stimme; Dubs gehen nur mit Schlüssel.',
    how: html`So bekommst du ihn: <a href="https://fish.audio/app/api-keys/" target="_blank" rel="noopener noreferrer" class="ext">fish.audio</a>
      öffnen → anmelden → <b>API Keys</b> → neuen Schlüssel erstellen (die kostenlose Entwickler-Stufe reicht).`,
    placeholder: 'Fish-Audio-API-Schlüssel',
  },
};

/** One key: status (masked), set/replace, test, delete. `onChange(info|null)` after every change. */
export function KeyEditor({ info, name, onChange, compact }) {
  const help = KEY_HELP[name] || { title: name, why: '', how: '' };
  const [editing, setEditing] = useState(!info || !info.set);
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const [test, setTest] = useState(null);
  const lastTest = test || (info && info.last_test);

  const save = async (andTest) => {
    setBusy(andTest ? 'savetest' : 'save');
    setError(null);
    try {
      const next = await api.setKey(name, { value: value.trim() });
      setValue('');
      setEditing(false);
      onChange && onChange(next);
      toast.success(`${help.title}: Schlüssel gespeichert.`);
      if (andTest) await runTest(next);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(null);
    }
  };
  const runTest = async (current) => {
    setBusy('test');
    try {
      const r = await api.testKey(name);
      const t = { ok: r.ok, message: r.message, at: Date.now() / 1000 };
      setTest(t);
      onChange && onChange({ ...(current || info), last_test: t });
      (r.ok ? toast.success : toast.error)(`${help.title}: ${r.message}`);
    } catch (err) {
      toast.fail(err, help.title);
    } finally {
      setBusy(null);
    }
  };
  const remove = async () => {
    const ok = await confirmDialog({
      title: `${help.title}-Schlüssel löschen?`, danger: true, confirmLabel: 'Löschen',
      message: name === 'anthropic' ? 'Ohne diesen Schlüssel pausiert die Automatik bei Schritt ③ (Jobs warten auf den Schlüssel).' : 'Voiceover fällt auf die System-Stimme zurück, Dubs sind nicht mehr möglich.',
    });
    if (!ok) return;
    setBusy('delete');
    try {
      await api.deleteKey(name);
      setTest(null);
      setEditing(true);
      onChange && onChange({ ...(info || { name }), set: false, masked: null, last_test: null });
      toast.success(`${help.title}: Schlüssel gelöscht.`);
    } catch (err) {
      toast.fail(err);
    } finally {
      setBusy(null);
    }
  };

  const fields = (error && error.fields) || {};
  return html`<div class="key-card">
    <div class="row-between">
      <div class="row">
        <h3>${help.title}</h3>
        ${help.required ? html`<${Badge} tone="orange">Pflicht für die Automatik<//>` : html`<${Badge} tone="muted">optional<//>`}
      </div>
      <div class="key-status">
        ${info && info.set
          ? html`<span class="masked" aria-label="Gespeicherter Schlüssel (maskiert)">${info.masked || '••••'}</span>`
          : html`<${Badge} tone=${help.required ? 'yellow' : 'neutral'}>nicht eingetragen<//>`}
      </div>
    </div>
    ${!compact && html`<p class="muted">${help.why}</p>`}
    ${lastTest && html`<p class=${cx('issue', lastTest.ok ? '' : 'issue-fail')} style=${lastTest.ok ? 'color:var(--green)' : ''}>
      <${Icon} name=${lastTest.ok ? 'circle-check' : 'alert-circle'} size=${15} />
      <span>${lastTest.ok ? 'Test erfolgreich' : 'Test fehlgeschlagen'}: ${lastTest.message}
        ${lastTest.at && html` <span class="muted">(<${Rel} ts=${lastTest.at} />)</span>`}</span></p>`}
    ${editing
      ? html`<form class="stack" onSubmit=${(e) => { e.preventDefault(); if (value.trim()) save(true); }}>
          <p class="small text-2">${help.how}</p>
          <${FormError} error=${error && !fields.value ? error : null} />
          <${SecretField} label=${info && info.set ? 'Neuer Schlüssel' : 'Schlüssel'} value=${value} onInput=${setValue}
            placeholder=${help.placeholder} error=${fields.value} />
          <div class="form-actions">
            <${Button} type="submit" variant="primary" icon="key-round" busy=${busy === 'savetest'} disabled=${!value.trim() || !!busy}>Speichern & testen<//>
            <${Button} busy=${busy === 'save'} disabled=${!value.trim() || !!busy} onClick=${() => save(false)}>Nur speichern<//>
            ${info && info.set && html`<${Button} variant="ghost" onClick=${() => { setEditing(false); setValue(''); setError(null); }}>Abbrechen<//>`}
          </div>
        </form>`
      : html`<div class="form-actions">
          <${Button} icon="zap" busy=${busy === 'test'} disabled=${!!busy} onClick=${() => runTest()}>Testen<//>
          <${Button} icon="pencil" disabled=${!!busy} onClick=${() => setEditing(true)}>Ändern<//>
          <${Button} variant="danger" icon="trash" busy=${busy === 'delete'} disabled=${!!busy} onClick=${remove}>Löschen<//>
          ${info && info.updated_at && html`<span class="muted small">gespeichert <${Rel} ts=${info.updated_at} /></span>`}
        </div>`}
  </div>`;
}

// ---------------------------------------------------------------------------------------------------------------
// Tool checks
// ---------------------------------------------------------------------------------------------------------------

export function ToolList({ tools }) {
  if (!tools || !tools.length) return html`<p class="muted">Keine Werkzeug-Infos vom Server.</p>`;
  return html`<ul class="list" style="list-style:none;margin:0;padding:0">
    ${tools.map((t) => html`<li class="list-row" key=${t.name}>
      <span class=${cx('check-icon', t.ok ? 'ok' : t.required === false ? 'optional' : 'todo')} aria-hidden="true">
        <${Icon} name=${t.ok ? 'check' : t.required === false ? 'info' : 'alert'} size=${14} /></span>
      <div class="list-main">
        <div class="list-title"><span class="mono">${t.name}</span>
          ${t.ok ? html`<${Badge} tone="green">${t.version ? (/^\d/.test(String(t.version)) ? `v${t.version}` : t.version) : 'installiert'}<//>`
            : t.required === false ? html`<${Badge} tone="muted">optional · fehlt<//>` : html`<${Badge} tone="yellow">fehlt<//>`}</div>
        <span class="muted small">${t.purpose || TOOL_PURPOSE[t.name] || ''}</span>
        ${!t.ok && t.hint && html`<div class="row small" style="margin-top:4px"><code style="overflow-wrap:anywhere">${t.hint}</code>
          <${CopyButton} text=${t.hint} iconOnly label="Befehl kopieren" /></div>`}
      </div>
    </li>`)}
  </ul>`;
}

export const INSTALL_ALL = 'uv run python -m clipper setup --install';

// ---------------------------------------------------------------------------------------------------------------
// 2FA enrolment (setup wizard and "2FA neu einrichten")
// ---------------------------------------------------------------------------------------------------------------

export function groupSecret(s) {
  return String(s || '').replace(/\s+/g, '').replace(/(.{4})/g, '$1 ').trim();
}

/** QR as <img> from the server's SVG (an <img> never runs scripts), the secret to type, and the code form. */
export function TotpEnroll({ setup, onConfirm, busy, error }) {
  const [code, setCode] = useState('');
  const fields = (error && error.fields) || {};
  const src = setup && setup.qr_svg ? `data:image/svg+xml;charset=utf-8,${encodeURIComponent(setup.qr_svg)}` : null;
  return html`<div class="stack-lg">
    <div class="totp-layout">
      <div class="qr-box">${src ? html`<img src=${src} alt="QR-Code für die Authenticator-App" />` : html`<span class="muted">kein QR-Code</span>`}</div>
      <ol class="steps">
        <li><div>Öffne eine Authenticator-App auf deinem Handy (z. B. Google Authenticator, Aegis, 2FAS, 1Password).</div></li>
        <li><div>Scanne den QR-Code. Geht das nicht, tippe diesen Schlüssel ein:
          <div class="secret" aria-label="Geheimer Schlüssel">${groupSecret(setup && setup.totp_secret)}</div>
          <div><${CopyButton} text=${(setup && setup.totp_secret) || ''} label="Schlüssel kopieren" /></div></div></li>
        <li><div>Gib den 6-stelligen Code ein, den die App jetzt anzeigt.</div></li>
      </ol>
    </div>
    <form class="stack" onSubmit=${(e) => { e.preventDefault(); onConfirm(code.replace(/\s+/g, '')); }}>
      <${FormError} error=${error && !fields.code ? error : null} />
      <${TextField} label="Code aus der App" value=${code} onInput=${(v) => setCode(v.replace(/[^\d ]/g, ''))}
        inputmode="numeric" autocomplete="one-time-code" maxLength=${7} inputClass="code-input" error=${fields.code}
        placeholder="123 456" autofocus />
      <${Button} type="submit" variant="primary" busy=${busy} disabled=${code.replace(/\s+/g, '').length !== 6}>Code bestätigen<//>
    </form>
  </div>`;
}

export function RecoveryCodes({ codes, username }) {
  const text = `GTA Clipper – Wiederherstellungscodes${username ? ` für ${username}` : ''}\n` +
    `Erstellt: ${new Date().toLocaleString('de-DE')}\nJeder Code funktioniert genau einmal.\n\n${codes.join('\n')}\n`;
  return html`<div class="stack">
    <${Callout} tone="warning" title="Nur jetzt sichtbar">
      Mit diesen Codes kommst du rein, wenn dein Handy weg ist. Speichere sie an einem sicheren Ort (Passwort-Manager, ausgedruckt). Jeder Code gilt einmal.
    <//>
    <ul class="codes" aria-label="Wiederherstellungscodes">${codes.map((c) => html`<li>${c}</li>`)}</ul>
    <div class="form-actions">
      <${CopyButton} text=${codes.join('\n')} label="Alle kopieren" variant="secondary" size="md" />
      <${Button} icon="download" onClick=${() => downloadText('gta-clipper-wiederherstellungscodes.txt', text)}>Als Datei herunterladen<//>
    </div>
  </div>`;
}

// ---------------------------------------------------------------------------------------------------------------
// Platform icon (text monogram in brand colour; no brand logos shipped)
// ---------------------------------------------------------------------------------------------------------------

const MONO = { youtube: 'YT', tiktok: 'TT', instagram: 'IG', x: 'X', facebook: 'FB', twitch: 'TW', kick: 'K', other: '·' };
export function PlatformIcon({ platform }) {
  const label = PLATFORMS[platform] || STREAM_PLATFORMS[platform] || platform;
  return html`<span class=${cx('platform-icon', `p-${platform}`)} title=${label} aria-hidden="true">${MONO[platform] || '·'}</span>`;
}

