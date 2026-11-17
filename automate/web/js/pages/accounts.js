// Konten: social media accounts (records to mark posts against) and the YouTube OAuth connection for uploads.
import * as api from '../api.js';
import { html, useState, useEffect, useData, useTitle, cx, toast, setQuery, confirmDialog } from '../lib.js';
import {
  PageHeader, Card, Button, IconButton, Badge, Loading, ErrorState, Empty, TextField, TextareaField, SelectField,
  Modal, Callout, CopyButton, SecretField, FormError,
} from '../ui.js';
import { PlatformIcon } from '../parts.js';
import { PLATFORMS } from '../labels.js';
import { loadProfiles, overview } from '../store.js';

export function AccountsPage({ route }) {
  useTitle('Konten');
  const accounts = useData(() => api.listAccounts().then(api.itemsOf), []);
  const app = useData(() => api.getYoutubeApp(), []);
  const profiles = useData(() => loadProfiles(), []);
  const [modal, setModal] = useState(null);
  const [busyId, setBusyId] = useState(null);

  // Return from Google's consent screen (the OAuth callback sends the browser to #/konten?youtube=…).
  useEffect(() => {
    const r = route.query.youtube;
    if (!r) return;
    if (r === 'connected') { toast.success('YouTube ist verbunden.'); overview.refresh(); }
    else toast.error(`YouTube-Verbindung fehlgeschlagen: ${route.query.message || 'unbekannter Fehler'}`);
    setQuery({ youtube: '', message: '', account: '' });
  }, [route.query.youtube]);

  const connect = async (accountId) => {
    setBusyId(accountId ? `c${accountId}` : 'new');
    try {
      const res = await api.youtubeConnect(accountId ? { account_id: accountId } : {});
      window.location.assign(res.auth_url);
    } catch (err) { toast.fail(err); setBusyId(null); }
  };
  const disconnect = async (a) => {
    if (!(await confirmDialog({ title: `${a.label} trennen?`, danger: true, confirmLabel: 'Trennen',
      message: 'Das Dashboard vergisst den Zugriff. Hochladen geht erst nach erneutem Verbinden. Bereits hochgeladene Videos bleiben online.' }))) return;
    setBusyId(`d${a.id}`);
    try { const upd = await api.disconnectAccount(a.id); accounts.setData((l) => l.map((x) => (x.id === upd.id ? upd : x))); toast.success('Getrennt.'); overview.refresh(); }
    catch (err) { toast.fail(err); } finally { setBusyId(null); }
  };
  const remove = async (a) => {
    if (!(await confirmDialog({ title: `${a.label} löschen?`, danger: true, confirmLabel: 'Löschen',
      message: 'Das Konto verschwindet aus der Liste. Markierungen „gepostet“ bleiben erhalten, verlieren aber die Kontozuordnung.' }))) return;
    setBusyId(`x${a.id}`);
    try { await api.deleteAccount(a.id); accounts.setData((l) => l.filter((x) => x.id !== a.id)); toast.success('Konto gelöscht.'); overview.refresh(); }
    catch (err) { toast.fail(err); } finally { setBusyId(null); }
  };
  const saved = (a) => accounts.setData((l) => (l.some((x) => x.id === a.id) ? l.map((x) => (x.id === a.id ? a : x)) : [...l, a]));
  const list = accounts.data || [];
  const appReady = app.data && app.data.client_id_set && app.data.client_secret_set;

  return html`<div class="page">
    <${PageHeader} title="Konten" subtitle="Wohin deine Shorts gehen: YouTube zum direkten Hochladen, andere Plattformen zum Markieren, was wo gepostet ist."
      actions=${html`<${Button} variant="primary" icon="plus" onClick=${() => setModal({})}>Konto hinzufügen<//>`} />
    <${Card} title="Social-Media-Konten" icon="users" class="card-flush">
      <div style="padding:0 18px 8px">
        ${accounts.error && !accounts.data ? html`<${ErrorState} error=${accounts.error} onRetry=${accounts.reload} />`
          : !accounts.data ? html`<${Loading} lines=${4} />`
          : !list.length ? html`<${Empty} icon="users" title="Noch keine Konten"
              action=${html`<${Button} icon="plus" onClick=${() => setModal({})}>Konto hinzufügen<//>`}>
              Lege deine Kanäle an (YouTube, TikTok, Instagram …), um Posts zuzuordnen. YouTube kannst du unten verbinden und direkt hochladen.<//>`
          : html`<div class="list">${list.map((a) => html`<div class="list-row" key=${a.id}>
              <${PlatformIcon} platform=${a.platform} />
              <div class="list-main">
                <div class="list-title">${a.label}
                  ${a.platform === 'youtube' && (a.connected ? html`<${Badge} tone="green" icon="check">verbunden<//>` : html`<${Badge} tone="yellow">nicht verbunden<//>`)}
                  ${a.profile && html`<${Badge} tone="muted">${a.profile}<//>`}</div>
                <span class="list-sub">${PLATFORMS[a.platform] || a.platform}${a.handle ? ` · ${a.handle}` : ''}${a.notes ? ` · ${a.notes}` : ''}</span>
              </div>
              <div class="list-actions">
                ${a.platform === 'youtube' && (a.connected
                  ? html`<${Button} size="sm" variant="ghost" busy=${busyId === `d${a.id}`} onClick=${() => disconnect(a)}>Trennen<//>`
                  : html`<${Button} size="sm" icon="link" busy=${busyId === `c${a.id}`} disabled=${!appReady} title=${appReady ? undefined : 'Erst Client-ID und Client-Secret unten eintragen'} onClick=${() => connect(a.id)}>Verbinden<//>`)}
                <${IconButton} icon="pencil" label=${`${a.label} bearbeiten`} onClick=${() => setModal(a)} />
                <${IconButton} icon="trash" label=${`${a.label} löschen`} busy=${busyId === `x${a.id}`} onClick=${() => remove(a)} />
              </div>
            </div>`)}</div>`}
      </div>
    <//>
    <${YoutubeSetup} app=${app} onConnect=${() => connect(null)} connecting=${busyId === 'new'} />
    ${modal && html`<${AccountModal} account=${modal.id ? modal : null} profiles=${profiles.data || []} onClose=${() => setModal(null)} onSaved=${saved} />`}
  </div>`;
}

function AccountModal({ account, profiles, onClose, onSaved }) {
  const [form, setForm] = useState({
    platform: account ? account.platform : 'youtube', label: account ? account.label : '', handle: account ? account.handle || '' : '',
    profile: account ? account.profile || '' : '', notes: account ? account.notes || '' : '',
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }));
  const submit = async (e) => {
    e && e.preventDefault();
    setBusy(true); setError(null);
    const body = { label: form.label.trim(), handle: form.handle.trim() || undefined, profile: form.profile || null, notes: form.notes.trim() || undefined };
    try {
      const a = account ? await api.updateAccount(account.id, body) : await api.createAccount({ platform: form.platform, ...body });
      onSaved(a); toast.success(account ? 'Konto gespeichert.' : 'Konto angelegt.'); onClose();
    } catch (err) { setError(err); } finally { setBusy(false); }
  };
  const f = (error && error.fields) || {};
  return html`<${Modal} title=${account ? 'Konto bearbeiten' : 'Konto hinzufügen'} onClose=${onClose}
    footer=${html`<${Button} variant="ghost" onClick=${onClose}>Abbrechen<//><${Button} variant="primary" busy=${busy} disabled=${!form.label.trim()} onClick=${submit}>${account ? 'Speichern' : 'Anlegen'}<//>`}>
    <form class="stack" onSubmit=${submit}>
      <${FormError} error=${error && !error.fields ? error : null} />
      <${SelectField} label="Plattform" value=${form.platform} onChange=${set('platform')} options=${PLATFORMS} disabled=${!!account} error=${f.platform} />
      <${TextField} label="Bezeichnung" value=${form.label} onInput=${set('label')} placeholder="z. B. Hauptkanal GTA 6" error=${f.label} autofocus />
      <${TextField} label="Handle" optional value=${form.handle} onInput=${set('handle')} placeholder="@meinkanal" error=${f.handle} autocapitalize="off" spellcheck="false" />
      <${SelectField} label="Profil" value=${form.profile} onChange=${set('profile')} placeholder="Alle Profile"
        options=${profiles.map((p) => ({ value: p.name, label: p.name }))} help="Welche Clips hier gepostet werden (nur zur Orientierung)." />
      <${TextareaField} label="Notizen" optional value=${form.notes} onInput=${set('notes')} rows=${2} />
      ${form.platform === 'youtube' && !account && html`<p class="muted small">Tipp: Statt manuell anzulegen kannst du unten „Mit YouTube verbinden“ nutzen – das Konto wird dann automatisch angelegt.</p>`}
    </form>
  <//>`;
}

function YoutubeSetup({ app, onConnect, connecting }) {
  const [clientId, setClientId] = useState('');
  const [secret, setSecret] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  if (app.error && !app.data) return html`<${Card} title="YouTube verbinden" icon="circle-play"><${ErrorState} error=${app.error} onRetry=${app.reload} /><//>`;
  if (!app.data) return html`<${Card} title="YouTube verbinden" icon="circle-play"><${Loading} lines=${6} /><//>`;
  const d = app.data;
  const ready = d.client_id_set && d.client_secret_set;
  const local = ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname);
  const save = async (e) => {
    e.preventDefault();
    setBusy(true); setError(null);
    const body = {};
    if (clientId.trim()) body.client_id = clientId.trim();
    if (secret.trim()) body.client_secret = secret.trim();
    try { app.setData(await api.putYoutubeApp(body)); setClientId(''); setSecret(''); toast.success('Google-OAuth-Client gespeichert.'); }
    catch (err) { setError(err); } finally { setBusy(false); }
  };
  const f = (error && error.fields) || {};
  return html`<${Card} title="YouTube verbinden" icon="circle-play"
    subtitle="Einmalig: einen eigenen Google-OAuth-Client anlegen. Dauert etwa 10 Minuten, kostet nichts."
    actions=${ready ? html`<${Badge} tone="green" icon="check">Client eingerichtet<//>` : html`<${Badge} tone="yellow">noch nicht eingerichtet<//>`}>
    <div class="grid grid-2" style="gap:28px">
      <ol class="steps">
        <li><div>Öffne die <a href="https://console.cloud.google.com/projectcreate" target="_blank" rel="noopener noreferrer" class="ext">Google Cloud Console</a> und lege ein neues Projekt an (Name z. B. „GTA Clipper“).</div></li>
        <li><div>Unter <b>APIs & Dienste → Bibliothek</b> die <a href="https://console.cloud.google.com/apis/library/youtube.googleapis.com" target="_blank" rel="noopener noreferrer" class="ext">YouTube Data API v3</a> suchen und <b>Aktivieren</b>.</div></li>
        <li><div>Unter <b>APIs & Dienste → OAuth-Zustimmungsbildschirm</b> (bzw. „Google Auth Platform“): Nutzertyp <b>Extern</b>, App-Name und deine E-Mail eintragen. Bei <b>Zielgruppe → Testnutzer</b> dein Google-Konto hinzufügen.</div></li>
        <li><div>Unter <b>APIs & Dienste → Anmeldedaten → Anmeldedaten erstellen → OAuth-Client-ID</b> als Typ <b>Webanwendung</b> wählen.</div></li>
        <li><div>Bei <b>Autorisierte Weiterleitungs-URIs</b> genau diese Adresse eintragen:
          <div class="uri-box"><code>${d.redirect_uri}</code><${CopyButton} text=${d.redirect_uri} iconOnly label="Weiterleitungs-URI kopieren" /></div></div></li>
        <li><div><b>Client-ID</b> und <b>Clientschlüssel</b> (Client-Secret) kopieren und rechts eintragen.</div></li>
        <li><div>Auf <b>Mit YouTube verbinden</b> klicken, deinen Kanal wählen und den Zugriff erlauben („Google hat diese App nicht überprüft“ → <b>Weiter</b>: es ist deine eigene App).</div></li>
      </ol>
      <div class="stack">
        <form class="stack" onSubmit=${save}>
          <${FormError} error=${error && !error.fields ? error : null} />
          <${TextField} label="Client-ID" value=${clientId} onInput=${setClientId} placeholder=${d.client_id_set ? (d.client_id_hint || 'gespeichert – zum Ändern neu eintragen') : '1234567890-abc….apps.googleusercontent.com'}
            error=${f.client_id} autocapitalize="off" spellcheck="false" inputClass="mono" autocomplete="off" />
          <${SecretField} label="Client-Secret" value=${secret} onInput=${setSecret} placeholder=${d.client_secret_set ? 'gespeichert – zum Ändern neu eintragen' : 'GOCSPX-…'} error=${f.client_secret} />
          <div class="form-actions"><${Button} type="submit" busy=${busy} disabled=${!clientId.trim() && !secret.trim()}>Speichern<//></div>
        </form>
        <hr style="margin:4px 0" />
        ${!local && html`<${Callout} tone="warning" title="Am PC selbst verbinden">
          Google schickt dich nach dem Erlauben zu <code>${d.redirect_uri}</code> zurück. Das klappt nur im Browser auf dem PC, auf dem der Clipper läuft – nicht vom Handy.<//>`}
        <${Button} variant="primary" icon="circle-play" busy=${connecting} disabled=${!ready} onClick=${onConnect}>Mit YouTube verbinden<//>
        <p class="muted small">Zugriff: Videos hochladen und Kanal lesen. Tipp: Im Testmodus laufen Googles Zugangs-Tokens nach 7 Tagen ab. Stell die App im Zustimmungsbildschirm auf <b>In Produktion</b>, damit die Verbindung bleibt.</p>
      </div>
    </div>
  <//>`;
}
