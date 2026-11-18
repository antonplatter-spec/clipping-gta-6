// Konten: social media accounts (records to mark posts against), the YouTube OAuth connection and the Instagram
// connection (paste a token, or OAuth behind an HTTPS address) for direct uploads.
import * as api from '../api.js';
import { html, useState, useEffect, useData, useTitle, toast, setQuery, confirmDialog, fmt } from '../lib.js';
import {
  PageHeader, Card, Button, IconButton, Badge, Loading, ErrorState, Empty, TextField, TextareaField, SelectField,
  Modal, Callout, CopyButton, SecretField, FormError, Tabs, Icon,
} from '../ui.js';
import { PlatformIcon } from '../parts.js';
import { PLATFORMS } from '../labels.js';
import { loadProfiles, overview } from '../store.js';

const CONNECTABLE = new Set(['youtube', 'instagram']);

export function AccountsPage({ route }) {
  useTitle('Konten');
  const accounts = useData(() => api.listAccounts().then(api.itemsOf), []);
  const app = useData(() => api.getYoutubeApp(), []);
  const igApp = useData(() => api.getInstagramApp(), []);
  const profiles = useData(() => loadProfiles(), []);
  const [modal, setModal] = useState(null);
  const [igModal, setIgModal] = useState(null);
  const [busyId, setBusyId] = useState(null);

  // Return from Google's / Instagram's consent screen (the OAuth callbacks send the browser to #/konten?<platform>=…).
  useEffect(() => {
    const yt = route.query.youtube;
    const ig = route.query.instagram;
    if (!yt && !ig) return;
    const name = yt ? 'YouTube' : 'Instagram';
    if ((yt || ig) === 'connected') { toast.success(`${name} ist verbunden.`); overview.refresh(); accounts.reload(); }
    else toast.error(`${name}-Verbindung fehlgeschlagen: ${route.query.message || 'unbekannter Fehler'}`);
    setQuery({ youtube: '', instagram: '', message: '', account: '' });
  }, [route.query.youtube, route.query.instagram]);

  const connect = async (accountId) => {
    setBusyId(accountId ? `c${accountId}` : 'new');
    try {
      const res = await api.youtubeConnect(accountId ? { account_id: accountId } : {});
      window.location.assign(res.auth_url);
    } catch (err) { toast.fail(err); setBusyId(null); }
  };
  const disconnect = async (a) => {
    const where = a.platform === 'instagram' ? 'Posten' : 'Hochladen';
    if (!(await confirmDialog({ title: `${a.label} trennen?`, danger: true, confirmLabel: 'Trennen',
      message: `Das Dashboard vergisst den Zugriff. ${where} geht erst nach erneutem Verbinden. Bereits veröffentlichte Videos bleiben online.` }))) return;
    setBusyId(`d${a.id}`);
    try { const upd = await api.disconnectAccount(a.id); accounts.setData((l) => l.map((x) => (x.id === upd.id ? upd : x))); toast.success('Getrennt.'); overview.refresh(); }
    catch (err) { toast.fail(err); } finally { setBusyId(null); }
  };
  const check = async (a) => {
    setBusyId(`k${a.id}`);
    try { const upd = await api.instagramCheck(a.id); saved(upd); toast.success(`Die Verbindung zu ${a.label} funktioniert.`); }
    catch (err) { toast.fail(err); accounts.reload(); overview.refresh(); } finally { setBusyId(null); }
  };
  const remove = async (a) => {
    if (!(await confirmDialog({ title: `${a.label} löschen?`, danger: true, confirmLabel: 'Löschen',
      message: 'Das Konto verschwindet aus der Liste. Markierungen „gepostet“ bleiben erhalten, verlieren aber die Kontozuordnung.' }))) return;
    setBusyId(`x${a.id}`);
    try { await api.deleteAccount(a.id); accounts.setData((l) => l.filter((x) => x.id !== a.id)); toast.success('Konto gelöscht.'); overview.refresh(); }
    catch (err) { toast.fail(err); } finally { setBusyId(null); }
  };
  const saved = (a) => accounts.setData((l) => (l.some((x) => x.id === a.id) ? l.map((x) => (x.id === a.id ? a : x)) : [...l, a]));
  const igConnected = (rows) => { rows.forEach(saved); overview.refresh(); };
  const list = accounts.data || [];
  const appReady = app.data && app.data.client_id_set && app.data.client_secret_set;

  return html`<div class="page">
    <${PageHeader} title="Konten" subtitle="Wohin deine Shorts gehen: YouTube und Instagram zum direkten Hochladen, andere Plattformen zum Markieren, was wo gepostet ist."
      actions=${html`<${Button} variant="primary" icon="plus" onClick=${() => setModal({})}>Konto hinzufügen<//>`} />
    <${Card} title="Social-Media-Konten" icon="users" class="card-flush">
      <div style="padding:0 18px 8px">
        ${accounts.error && !accounts.data ? html`<${ErrorState} error=${accounts.error} onRetry=${accounts.reload} />`
          : !accounts.data ? html`<${Loading} lines=${4} />`
          : !list.length ? html`<${Empty} icon="users" title="Noch keine Konten"
              action=${html`<${Button} icon="plus" onClick=${() => setModal({})}>Konto hinzufügen<//>`}>
              Lege deine Kanäle an (YouTube, TikTok, Instagram …), um Posts zuzuordnen. YouTube und Instagram kannst du unten verbinden und direkt hochladen.<//>`
          : html`<div class="list">${list.map((a) => html`<div class="list-row" key=${a.id}>
              <${PlatformIcon} platform=${a.platform} />
              <div class="list-main">
                <div class="list-title">${a.label}
                  ${CONNECTABLE.has(a.platform) && (a.connected ? html`<${Badge} tone="green" icon="check">verbunden<//>` : html`<${Badge} tone="yellow">nicht verbunden<//>`)}
                  ${a.profile && html`<${Badge} tone="muted">${a.profile}<//>`}</div>
                <span class="list-sub">${PLATFORMS[a.platform] || a.platform}${a.handle && a.handle !== a.label ? ` · ${a.handle}` : ''}${a.notes ? ` · ${a.notes}` : ''}</span>
                ${a.connection && html`<span class="list-sub">${connectionText(a.connection)}</span>`}
                ${a.connection && a.connection.refresh_error && html`<p class="issue issue-note"><${Icon} name="alert" size=${13} />Letzte Prüfung/Erneuerung fehlgeschlagen (das Token bleibt gespeichert): ${a.connection.refresh_error}</p>`}
              </div>
              <div class="list-actions">
                ${a.platform === 'youtube' && (a.connected
                  ? html`<${Button} size="sm" variant="ghost" busy=${busyId === `d${a.id}`} onClick=${() => disconnect(a)}>Trennen<//>`
                  : html`<${Button} size="sm" icon="link" busy=${busyId === `c${a.id}`} disabled=${!appReady} title=${appReady ? undefined : 'Erst Client-ID und Client-Secret unten eintragen'} onClick=${() => connect(a.id)}>Verbinden<//>`)}
                ${a.platform === 'instagram' && (a.connected
                  ? html`<${Button} size="sm" variant="ghost" icon="refresh-cw" busy=${busyId === `k${a.id}`} onClick=${() => check(a)}>Prüfen<//>
                      <${Button} size="sm" variant="ghost" busy=${busyId === `d${a.id}`} onClick=${() => disconnect(a)}>Trennen<//>`
                  : html`<${Button} size="sm" icon="link" onClick=${() => setIgModal(a)}>Verbinden<//>`)}
                <${IconButton} icon="pencil" label=${`${a.label} bearbeiten`} onClick=${() => setModal(a)} />
                <${IconButton} icon="trash" label=${`${a.label} löschen`} busy=${busyId === `x${a.id}`} onClick=${() => remove(a)} />
              </div>
            </div>`)}</div>`}
      </div>
    <//>
    <${YoutubeSetup} app=${app} onConnect=${() => connect(null)} connecting=${busyId === 'new'} />
    <${InstagramSetup} app=${igApp} onConnected=${igConnected} />
    ${modal && html`<${AccountModal} account=${modal.id ? modal : null} profiles=${profiles.data || []} onClose=${() => setModal(null)} onSaved=${saved} />`}
    ${igModal && html`<${InstagramTokenModal} account=${igModal} onClose=${() => setIgModal(null)} onConnected=${igConnected} />`}
  </div>`;
}

/** "Token gültig bis 17.01.2027 (wird automatisch erneuert)" / "über die Facebook-Seite „…“, ohne Ablaufdatum" */
function connectionText(c) {
  if (c.kind === 'facebook') {
    return `Über die Facebook-Seite „${c.page_name || '?'}“ · Token ohne Ablaufdatum${c.checked_at ? ` · zuletzt geprüft ${fmt.dateTime(c.checked_at)}` : ''}`;
  }
  const until = c.expires_at ? `Token gültig bis ${c.expires_estimated ? 'spätestens ' : ''}${fmt.date(c.expires_at)}` : 'Token ohne bekanntes Ablaufdatum';
  return `${until} · wird automatisch erneuert${c.refreshed_at ? ` (zuletzt ${fmt.dateTime(c.refreshed_at)})` : ''}`;
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
      ${form.platform === 'instagram' && !account && html`<p class="muted small">Tipp: Statt manuell anzulegen kannst du unten bei „Instagram verbinden“ ein Token einfügen – das Konto wird dann automatisch angelegt.</p>`}
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
        ${d.redirect_note ? html`<${Callout} tone=${local ? 'info' : 'warning'} title=${local ? 'So läuft das Verbinden' : 'Am PC selbst verbinden'}>${d.redirect_note}<//>`
          : !local && html`<${Callout} tone="warning" title="Am PC selbst verbinden">
          Google schickt dich nach dem Erlauben zu <code>${d.redirect_uri}</code> zurück. Das klappt nur im Browser auf dem PC, auf dem der Clipper läuft – nicht vom Handy.<//>`}
        <${Button} variant="primary" icon="circle-play" busy=${connecting} disabled=${!ready} onClick=${onConnect}>Mit YouTube verbinden<//>
        <p class="muted small">Zugriff: Videos hochladen und Kanal lesen. Tipp: Im Testmodus laufen Googles Zugangs-Tokens nach 7 Tagen ab. Stell die App im Zustimmungsbildschirm auf <b>In Produktion</b>, damit die Verbindung bleibt.</p>
      </div>
    </div>
  <//>`;
}

// ---------------------------------------------------------------------------------------------------------------
// Instagram
// ---------------------------------------------------------------------------------------------------------------

const ext = (href, text) => html`<a href=${href} target="_blank" rel="noopener noreferrer" class="ext">${text}</a>`;

/** Saves any of the given secret fields (write-only: the inputs are emptied, only "gespeichert" stays visible). */
function AppIdsForm({ app, fields, onSaved }) {
  const [values, setValues] = useState({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const d = app.data;
  const save = async (e) => {
    e.preventDefault();
    setBusy(true); setError(null);
    const body = {};
    for (const f of fields) if ((values[f.key] || '').trim()) body[f.key] = values[f.key].trim();
    try { app.setData(await api.putInstagramApp(body)); setValues({}); toast.success('Gespeichert.'); if (onSaved) onSaved(); }
    catch (err) { setError(err); } finally { setBusy(false); }
  };
  const errs = (error && error.fields) || {};
  const any = fields.some((f) => (values[f.key] || '').trim());
  return html`<form class="stack" onSubmit=${save}>
    <${FormError} error=${error && !error.fields ? error : null} />
    ${fields.map((f) => (f.secret
      ? html`<${SecretField} key=${f.key} label=${f.label} value=${values[f.key] || ''} onInput=${(v) => setValues((x) => ({ ...x, [f.key]: v }))}
          placeholder=${d[`${f.key}_set`] ? 'gespeichert – zum Ändern neu eintragen' : f.placeholder} error=${errs[f.key]} />`
      : html`<${TextField} key=${f.key} label=${f.label} value=${values[f.key] || ''} onInput=${(v) => setValues((x) => ({ ...x, [f.key]: v }))}
          placeholder=${d[`${f.key}_set`] ? (d[`${f.key}_hint`] || 'gespeichert – zum Ändern neu eintragen') : f.placeholder}
          error=${errs[f.key]} inputmode="numeric" autocapitalize="off" spellcheck="false" inputClass="mono" autocomplete="off" />`))}
    <div class="form-actions"><${Button} type="submit" busy=${busy} disabled=${!any}>Speichern<//></div>
  </form>`;
}

/** The write-only token field (paste path); `account` connects an existing Instagram account row. */
function TokenForm({ account, onConnected }) {
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const submit = async (e) => {
    e && e.preventDefault();
    setBusy(true); setError(null);
    try {
      const res = await api.instagramToken({ access_token: token.trim(), ...(account ? { account_id: account.id } : {}) });
      const rows = api.itemsOf(res);
      setToken('');
      toast.success(rows.length === 1 ? `${rows[0].label} ist verbunden.` : `${rows.length} Instagram-Konten verbunden: ${rows.map((r) => r.label).join(', ')}.`);
      onConnected(rows);
    } catch (err) { setError(err); } finally { setBusy(false); }
  };
  const f = (error && error.fields) || {};
  return html`<form class="stack" onSubmit=${submit}>
    <${FormError} error=${error && !f.access_token ? error : null} />
    <${SecretField} label="Zugriffstoken" value=${token} onInput=${setToken} placeholder="IGAA… oder EAA…" error=${f.access_token}
      help="Wird bei Meta geprüft und nur gespeichert, nie wieder angezeigt. Der Server erkennt, ob es ein Instagram-Token (Weg B) oder ein Facebook-Token (Weg A) ist." />
    <div class="form-actions"><${Button} type="submit" variant="primary" icon="link" busy=${busy} disabled=${!token.trim()}>Token prüfen und verbinden<//></div>
  </form>`;
}

function InstagramTokenModal({ account, onClose, onConnected }) {
  return html`<${Modal} title=${`${account.label} verbinden`} onClose=${onClose} size="sm">
    <p class="text-2">Füge ein Token dieses Instagram-Kontos ein. Wie du es bekommst, steht unten auf dieser Seite unter <b>Instagram verbinden</b>.</p>
    <${TokenForm} account=${account} onConnected=${(rows) => { onConnected(rows); onClose(); }} />
  <//>`;
}

function InstagramSetup({ app, onConnected }) {
  const [tab, setTab] = useState('a');
  const [oauthBusy, setOauthBusy] = useState(false);
  if (app.error && !app.data) return html`<${Card} title="Instagram verbinden" icon="clapperboard"><${ErrorState} error=${app.error} onRetry=${app.reload} /><//>`;
  if (!app.data) return html`<${Card} title="Instagram verbinden" icon="clapperboard"><${Loading} lines=${6} /><//>`;
  const d = app.data;
  const metaReady = d.meta_app_id_set && d.meta_app_secret_set;
  const oauthReady = d.oauth_available && d.app_id_set && d.app_secret_set;
  const code = (list) => list.map((p, i) => html`${i ? ', ' : ''}<code>${p}</code>`);
  const oauth = async () => {
    setOauthBusy(true);
    try { const res = await api.instagramConnect({}); window.location.assign(res.auth_url); }
    catch (err) { toast.fail(err); setOauthBusy(false); }
  };
  return html`<${Card} title="Instagram verbinden" icon="clapperboard"
    subtitle="Reels direkt aus dem Dashboard posten – über Metas offizielle Schnittstelle, ohne Passwort. Einmalig: eine eigene Meta-App anlegen (kostenlos, etwa 15 Minuten)."
    actions=${metaReady ? html`<${Badge} tone="green" icon="check">Meta-App eingetragen<//>` : null}>
    <div class="stack">
      <${Callout} tone="warning" title="Noch nicht mit einem echten Instagram-Konto ausprobiert">
        Gebaut nach Metas Unterlagen (Graph API ${d.graph_version}), getestet nur gegen eine Nachbildung. Metas Menünamen ändern sich oft – wo „laut Meta“ steht, bitte dort nachsehen. Klappt etwas nicht, steht Metas Antwort beim Upload-Job unter <a href="#/jobs?kind=upload">Jobs</a>.<//>
      <p class="text-2">Voraussetzung für beide Wege: Dein Instagram-Konto ist ein <b>professionelles Konto</b> (Creator oder Business; in der Instagram-App unter <b>Einstellungen → Kontotyp und Tools</b>, kostenlos). Private Konten können über die API nicht posten.</p>
      <${Tabs} label="Verbindungsweg" value=${tab} onChange=${setTab} class="tabs-wrap"
        tabs=${[{ id: 'a', label: 'Weg A: mit Facebook-Seite (empfohlen)' }, { id: 'b', label: 'Weg B: nur Instagram' }]} />
      ${tab === 'a' ? html`<div class="grid grid-2" style="gap:28px">
        <ol class="steps">
          <li><div>Verknüpfe dein Instagram-Konto mit einer <b>Facebook-Seite</b> (eine Seite kannst du kostenlos anlegen): in der Instagram-App über die <b>Kontenübersicht</b> oder auf Facebook in den Einstellungen der Seite unter <b>Verknüpfte Konten</b> (laut Meta, bitte dort prüfen).</div></li>
          <li><div>Auf ${ext('https://developers.facebook.com/apps', 'developers.facebook.com/apps')} (mit deinem Facebook-Konto) <b>App erstellen</b>, als Typ <b>Business</b> bzw. den Anwendungsfall für Instagram-Inhalte wählen; Name z. B. „GTA Clipper“.</div></li>
          <li><div>In der App das Produkt <b>Instagram</b> hinzufügen und <b>API-Einrichtung mit Facebook-Login</b> wählen. Die App darf im <b>Entwicklungsmodus</b> bleiben: Für dein eigenes Konto (du bist Admin der App) braucht es laut Meta keine App-Prüfung.</div></li>
          <li><div>Unter <b>App-Einstellungen → Allgemeines</b> die <b>App-ID</b> und den <b>App-Geheimcode</b> kopieren und rechts eintragen. Damit macht der Server aus deinem Token eines, das nicht abläuft.</div></li>
          <li><div>Im ${ext('https://developers.facebook.com/tools/explorer/', 'Graph API Explorer')} oben deine App und <b>Benutzer-Token</b> wählen, diese Berechtigungen hinzufügen: ${code(d.fb_permissions)} (gehört die Seite zu einem Business-Portfolio, zusätzlich <code>business_management</code>). Dann <b>Generate Access Token</b> und im Meta-Fenster deine Seite und dein Instagram-Konto auswählen.</div></li>
          <li><div>Das Token (beginnt mit <code>EAA</code>) kopieren und unten einfügen.</div></li>
        </ol>
        <div class="stack">
          <${AppIdsForm} app=${app} fields=${[{ key: 'meta_app_id', label: 'App-ID', placeholder: '123456789012345' },
            { key: 'meta_app_secret', label: 'App-Geheimcode', placeholder: '32 Zeichen', secret: true }]} />
          <p class="muted small">Laut Metas Anleitung ist das der Weg, auf dem ein Video direkt vom eigenen PC hochgeladen werden darf (ohne öffentliche Adresse).</p>
        </div>
      </div>` : html`<div class="stack">
        <ol class="steps">
          <li><div>Auf ${ext('https://developers.facebook.com/apps', 'developers.facebook.com/apps')} <b>App erstellen</b> mit dem Anwendungsfall für Instagram bzw. dem Produkt <b>Instagram → API-Einrichtung mit Instagram-Login</b> (laut Meta, bitte dort prüfen). Eine Facebook-Seite brauchst du dafür nicht.</div></li>
          <li><div>Unter <b>App-Rollen → Rollen</b> dein Instagram-Konto als <b>Instagram-Tester</b> einladen und die Einladung in Instagram annehmen (unter <b>Apps und Websites → Tester-Einladungen</b>; laut Meta).</div></li>
          <li><div>Bei <b>API-Einrichtung mit Instagram-Login → Zugriffstoken generieren</b> dein Konto hinzufügen, anmelden und das Token (beginnt mit <code>IG</code>) kopieren und unten einfügen. Es gilt 60 Tage; der Server erneuert es automatisch jede Woche.</div></li>
        </ol>
        <${Callout} tone="warning" title="Wichtig zu Weg B">Laut Metas Anleitung dürfen nur Apps mit Facebook-Login eine Videodatei direkt hochladen (Weg A). Mit einem Instagram-Login-Token versucht es der Server trotzdem – lehnt Meta ab, steht die Antwort beim Upload-Job, und Weg A ist die Lösung.<//>
        <details class="details"><summary><${Icon} name="chevron-right" size=${16} />Statt Token: mit Instagram anmelden (nur mit eigener HTTPS-Adresse)</summary>
          <div class="details-body">
            <${Callout} tone="info" title=${d.oauth_available ? 'So läuft die Anmeldung' : 'Hier nicht verfügbar'}>${d.oauth_note}<//>
            ${d.oauth_available && html`<div class="stack-sm"><span class="field-label">OAuth-Weiterleitungs-URI (in der Meta-App unter „Einstellungen für Business Login“ eintragen)</span>
              <div class="uri-box"><code>${d.redirect_uri}</code><${CopyButton} text=${d.redirect_uri} iconOnly label="Weiterleitungs-URI kopieren" /></div></div>`}
            ${d.oauth_available && html`<${AppIdsForm} app=${app} fields=${[{ key: 'app_id', label: 'Instagram-App-ID', placeholder: '123456789012345' },
              { key: 'app_secret', label: 'Instagram-App-Geheimcode', placeholder: '32 Zeichen', secret: true }]} />`}
            ${d.oauth_available && html`<div><${Button} icon="link" busy=${oauthBusy} disabled=${!oauthReady} onClick=${oauth}>Mit Instagram anmelden<//></div>`}
          </div>
        </details>
      </div>`}
      <hr style="margin:4px 0" />
      <h3>Token einfügen</h3>
      <${TokenForm} onConnected=${onConnected} />
      <p class="muted small">Posten: <a href="#/videos">Videos</a> → Variante → <b>Hochladen</b> → Instagram-Konto wählen. Instagram verarbeitet das Video danach einige Minuten, dann erscheint der Reel und das Dashboard verlinkt ihn.</p>
    </div>
  <//>`;
}
