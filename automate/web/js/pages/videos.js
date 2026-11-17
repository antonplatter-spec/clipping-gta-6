// Videos: rendered shorts grouped by day, each with its hook variants side by side (preview, copy, post, upload, dub).
import * as api from '../api.js';
import {
  html, useState, useEffect, useRef, useData, useTitle, fmt, cx, toast, hookText, setQuery, hostOf, confirmDialog,
} from '../lib.js';
import {
  PageHeader, Button, IconButton, Badge, Icon, Loading, ErrorState, Empty, TextField, SelectField, Segmented, Modal,
  CopyButton, LazyMount, Pager, Toggle, Spinner, FormError, Callout,
} from '../ui.js';
import { hookTypeLabel, factorTone, PLATFORMS, PRIVACY, LANGUAGES, langLabel } from '../labels.js';
import { loadProfiles } from '../store.js';

const LIMIT = 20;

export function VideosPage({ route }) {
  useTitle('Videos');
  const q = route.query;
  const filters = { q: q.q || '', profile: q.profile || '', day: q.day || '', posted: q.posted || '', lang: q.lang || '', archived: q.archived === '1' ? 1 : 0 };
  const [offset, setOffset] = useState(0);
  const [search, setSearch] = useState(filters.q);
  const fkey = JSON.stringify(filters);
  useEffect(() => setOffset(0), [fkey]);
  useEffect(() => {
    if (search === filters.q) return undefined;
    const t = setTimeout(() => setQuery({ q: search }), 350);
    return () => clearTimeout(t);
  }, [search]);
  const [poll, setPoll] = useState(0);
  const list = useData(() => api.listVideos({ ...filters, posted: filters.posted === '' ? undefined : Number(filters.posted), limit: LIMIT, offset }),
    [fkey, offset], { interval: poll });
  const accounts = useData(() => api.listAccounts().then(api.itemsOf), []);
  const profiles = useData(() => loadProfiles(), []);
  const items = (list.data && list.data.items) || [];
  const uploading = items.some((v) => (v.variants || []).some((x) => (x.posts || []).some((p) => p.status === 'uploading')));
  useEffect(() => setPoll(uploading ? 10000 : 0), [uploading]);

  const replace = (video) => list.setData((d) => d && { ...d, items: d.items.map((v) => (v.id === video.id ? video : v)) });
  const refreshVideo = async (id) => { try { replace(await api.getVideo(id)); } catch (err) { toast.fail(err); } };
  const drop = (id) => list.setData((d) => d && { ...d, items: d.items.filter((v) => v.id !== id), total: d.total - 1 });

  const groups = [];
  for (const v of items) {
    const last = groups[groups.length - 1];
    if (last && last.day === v.day) last.videos.push(v); else groups.push({ day: v.day, videos: [v] });
  }
  const anyFilter = filters.q || filters.profile || filters.day || filters.posted || filters.lang || filters.archived;
  return html`<div class="page">
    <${PageHeader} title="Videos" subtitle="Fertige Shorts: je Hook eine Variante zum A/B-Testen. Titel kopieren, posten, als gepostet markieren." />
    <div class="filters" role="search">
      <${TextField} label="Suche" class="grow" value=${search} onInput=${setSearch} type="search" placeholder="Titel, Hook, Beschreibung …" />
      ${(profiles.data || []).length > 1 && html`<${SelectField} label="Profil" value=${filters.profile} onChange=${(v) => setQuery({ profile: v })}
        placeholder="Alle" options=${profiles.data.map((p) => ({ value: p.name, label: p.name }))} />`}
      <${TextField} label="Tag" type="date" value=${filters.day} onInput=${(v) => setQuery({ day: v })} />
      <${SelectField} label="Sprache" value=${filters.lang} onChange=${(v) => setQuery({ lang: v })} placeholder="Alle" options=${LANGUAGES} />
      <div class="field"><span class="field-label" id="posted-l">Status</span>
        <${Segmented} label="Status" value=${filters.posted} onChange=${(v) => setQuery({ posted: v })}
          options=${[{ value: '', label: 'Alle' }, { value: '0', label: 'Offen' }, { value: '1', label: 'Gepostet' }]} /></div>
      <div class="field" style="justify-content:flex-end;padding-bottom:7px"><${Toggle} checked=${!!filters.archived} label="Archiv zeigen"
        onChange=${(v) => setQuery({ archived: v ? '1' : '' })} /></div>
    </div>
    ${list.error && !list.data ? html`<${ErrorState} error=${list.error} onRetry=${list.reload} />`
      : !list.data ? html`<div class="card"><${Loading} lines=${8} label="Lade Videos …" /></div>`
      : !items.length ? html`<div class="card">${anyFilter
          ? html`<${Empty} icon="search" title="Keine Videos für diese Filter" action=${html`<${Button} onClick=${() => { setSearch(''); setQuery({ q: '', profile: '', day: '', posted: '', lang: '', archived: '' }); }}>Filter zurücksetzen<//>`} />`
          : html`<${Empty} icon="film" title="Noch keine fertigen Videos" action=${html`<${Button} href="#/freigabe" variant="primary" icon="badge-check">Zur Freigabe<//>`}>
              Freigegebene Picks erscheinen hier, sobald ihr Render-Job fertig ist.<//>`}</div>`
      : html`<div class="stack-lg">
          ${groups.map((g) => html`<section class="stack" key=${g.day} aria-labelledby=${`day-${g.day}`}>
            <div class="day-head"><h2 id=${`day-${g.day}`}>${fmt.day(g.day)}</h2><span class="muted">${g.day} · ${fmt.plural(g.videos.length, 'Video', 'Videos')}</span></div>
            ${g.videos.map((v) => html`<${VideoCard} key=${v.id} video=${v} accounts=${accounts.data || []} focus=${Number(q.video) === v.id}
              onChanged=${() => refreshVideo(v.id)} onArchived=${() => drop(v.id)} />`)}
          </section>`)}
          <${Pager} total=${list.data.total} limit=${LIMIT} offset=${offset} onChange=${(o) => { setOffset(o); window.scrollTo(0, 0); }} />
        </div>`}
  </div>`;
}

function VideoCard({ video, accounts, focus, onChanged, onArchived }) {
  const [modal, setModal] = useState(null);
  const [busy, setBusy] = useState(false);
  const ref = useRef(null);
  useEffect(() => { if (focus && ref.current) ref.current.scrollIntoView({ behavior: 'smooth', block: 'start' }); }, [focus]);
  const variants = [...(video.variants || [])].sort((a, b) => a.rank - b.rank);
  const posted = variants.some((x) => (x.posts || []).some((p) => p.status === 'posted'));
  const archive = async () => {
    if (!video.archived) {
      const ok = await confirmDialog({ title: 'Video archivieren?', confirmLabel: 'Archivieren', message: 'Es verschwindet aus der Liste (die Dateien bleiben). Unter „Archiv“ kannst du es zurückholen.' });
      if (!ok) return;
    }
    setBusy(true);
    try {
      await api.updateVideo(video.id, { archived: !video.archived });
      toast.success(video.archived ? 'Video wiederhergestellt.' : 'Video archiviert.');
      onArchived();
    } catch (err) { toast.fail(err); } finally { setBusy(false); }
  };
  return html`<article class=${cx('card video-card', focus && 'is-selected')} ref=${ref} aria-labelledby=${`v-${video.id}`}>
    <header class="video-head">
      <div class="video-head-main">
        <div class="row">
          <${Badge} tone="neutral">Video ${video.number}<//>
          <${Badge} tone="muted">${video.profile}<//>
          ${video.lang && video.lang !== 'en' ? html`<${Badge} tone="blue" icon="languages">${langLabel(video.lang)} (Dub)<//>` : html`<${Badge} tone="muted">${langLabel(video.lang || 'en')}<//>`}
          ${posted ? html`<${Badge} tone="green" icon="check">gepostet<//>` : html`<${Badge} tone="yellow">noch nicht gepostet<//>`}
          ${video.archived && html`<${Badge} tone="muted" icon="archive">archiviert<//>`}
        </div>
        <h3 class="video-title" id=${`v-${video.id}`}>${video.title || `Video ${video.number}`}</h3>
        <div class="muted small row" style="gap:6px">
          ${(video.sources || []).map((s, i) => html`<a href=${s} target="_blank" rel="noopener noreferrer" class="ext">Quelle${video.sources.length > 1 ? ` ${i + 1}` : ''}: ${hostOf(s)}</a>`)}
          <span>· ${fmt.dateTime(video.created_at)}</span>
          ${video.pick_id && html`<span>· Pick #${video.pick_id}</span>`}
        </div>
      </div>
      <div class="row">
        ${(!video.lang || video.lang === 'en') && html`<${Button} size="sm" icon="languages" onClick=${() => setModal({ kind: 'dub' })}>Dub erstellen<//>`}
        <${Button} size="sm" variant="ghost" icon=${video.archived ? 'archive-restore' : 'archive'} busy=${busy} onClick=${archive}>${video.archived ? 'Wiederherstellen' : 'Archivieren'}<//>
      </div>
    </header>
    ${variants.length ? html`<div class="variants" role="list" aria-label="Hook-Varianten">
      ${variants.map((x) => html`<${VariantCard} key=${x.id} v=${x} accounts=${accounts} onPost=${() => setModal({ kind: 'post', v: x })}
        onUpload=${() => setModal({ kind: 'upload', v: x })} onChanged=${onChanged} />`)}
    </div>` : html`<p class="muted" style="padding:16px 18px">Keine Varianten gefunden.</p>`}
    ${modal && modal.kind === 'post' && html`<${PostModal} v=${modal.v} accounts=${accounts} onClose=${() => setModal(null)} onDone=${onChanged} />`}
    ${modal && modal.kind === 'upload' && html`<${UploadModal} v=${modal.v} accounts=${accounts} onClose=${() => setModal(null)} onDone=${onChanged} />`}
    ${modal && modal.kind === 'dub' && html`<${DubModal} video=${video} onClose=${() => setModal(null)} />`}
  </article>`;
}

function VariantCard({ v, accounts, onPost, onUpload, onChanged }) {
  const [descOpen, setDescOpen] = useState(false);
  const [removing, setRemoving] = useState(null);
  const ytConnected = accounts.some((a) => a.platform === 'youtube' && a.connected);
  const noHook = v.arm === 'no-hook' || !v.hook;
  const accountLabel = (id) => { const a = accounts.find((x) => x.id === id); return a ? a.label : null; };
  const removePost = async (p) => {
    const ok = await confirmDialog({ title: 'Markierung entfernen?', confirmLabel: 'Entfernen', danger: true,
      message: p.status === 'posted' && p.platform === 'youtube' && p.external_id ? 'Das Video bleibt auf YouTube online – nur die Markierung im Dashboard wird entfernt.' : 'Die Variante gilt dann wieder als nicht gepostet.' });
    if (!ok) return;
    setRemoving(p.id);
    try { await api.deletePost(p.id); onChanged(); } catch (err) { toast.fail(err); } finally { setRemoving(null); }
  };
  return html`<div class="variant" role="listitem">
    <div class="variant-media">
      <span class="variant-rank"><${Badge} tone="neutral">${noHook ? 'ohne Hook' : `#${v.rank + 1}${v.rank === 0 ? ' · bester Hook' : ''}`}<//></span>
      <${LazyMount} placeholder=${html`<div class="video-ph"><${Icon} name="circle-play" size=${36} /></div>`}>
        <video controls preload="metadata" playsinline src=${v.file_url} aria-label=${`Vorschau: ${v.hook || 'ohne Hook'}`}></video>
      <//>
    </div>
    <div class="variant-hook">${noHook ? html`<span class="muted">Ohne Hook-Text (A/B-Test)</span>` : hookText(v.hook)}</div>
    <div class="row" style="gap:6px">
      ${v.hook_type && html`<${Badge} tone="neutral">${hookTypeLabel(v.hook_type)}<//>`}
      ${v.predicted != null && html`<${Badge} tone=${factorTone(v.predicted)} title="Vorhergesagter Views-Faktor aus dem Swipe-Test">${fmt.factor(v.predicted)}<//>`}
      ${v.duration != null && html`<span class="muted small">${fmt.clock(v.duration)}</span>`}
    </div>
    <div class="variant-block">
      <div class="variant-label">Titel <${CopyButton} text=${v.title} iconOnly label="Titel kopieren" /></div>
      <div class="variant-text">${v.title}</div>
    </div>
    <div class="variant-block">
      <div class="variant-label">Beschreibung <${CopyButton} text=${v.description} iconOnly label="Beschreibung kopieren" /></div>
      <div class=${cx('variant-desc', descOpen && 'is-open')}>${v.description}</div>
      ${((v.description || '').length > 110 || (v.description || '').split('\n').length > 3) && html`<button type="button" class="linklike small" style="align-self:flex-start" onClick=${() => setDescOpen(!descOpen)}>${descOpen ? 'weniger' : 'mehr anzeigen'}</button>`}
    </div>
    ${(v.posts || []).length > 0 && html`<div class="posts" aria-label="Gepostet">
      ${v.posts.map((p) => html`<div class="post" key=${p.id}>
        ${p.status === 'uploading' ? html`<${Badge} tone="running"><${Spinner} size=${10} /> lädt hoch<//>`
          : p.status === 'failed' ? html`<${Badge} tone="red" title=${p.error || ''}>Upload fehlgeschlagen<//>`
          : html`<${Badge} tone="green" icon="check">${PLATFORMS[p.platform] || p.platform}<//>`}
        <span class="truncate small">${p.url ? html`<a href=${p.url} target="_blank" rel="noopener noreferrer">${accountLabel(p.account_id) || hostOf(p.url)}</a>` : accountLabel(p.account_id) || (p.status === 'posted' ? fmt.dateTime(p.posted_at || p.created_at) : '')}</span>
        <${IconButton} icon="x" size="sm" label="Markierung entfernen" busy=${removing === p.id} onClick=${() => removePost(p)} />
      </div>`)}
      ${v.posts.filter((p) => p.status === 'failed' && p.error).map((p) => html`<p class="issue issue-fail small"><${Icon} name="alert-circle" size=${13} />${p.error}</p>`)}
    </div>`}
    <div class="variant-actions">
      <${Button} size="sm" icon="download" href=${`${v.file_url}${v.file_url.includes('?') ? '&' : '?'}download=1`} download>MP4<//>
      <${Button} size="sm" icon="check-check" onClick=${onPost}>Als gepostet markieren<//>
      ${ytConnected && html`<${Button} size="sm" icon="upload" onClick=${onUpload}>Auf YouTube hochladen<//>`}
    </div>
  </div>`;
}

function PostModal({ v, accounts, onClose, onDone }) {
  const [platform, setPlatform] = useState('youtube');
  const [accountId, setAccountId] = useState('');
  const [url, setUrl] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const options = accounts.filter((a) => a.platform === platform);
  useEffect(() => { setAccountId(options.length === 1 ? String(options[0].id) : ''); }, [platform]);
  const submit = async (e) => {
    e && e.preventDefault();
    setBusy(true); setError(null);
    try {
      await api.markPosted(v.id, { platform, account_id: accountId ? Number(accountId) : null, url: url.trim() || undefined, note: note.trim() || undefined });
      toast.success('Als gepostet markiert.');
      onDone(); onClose();
    } catch (err) { setError(err); } finally { setBusy(false); }
  };
  const fields = (error && error.fields) || {};
  return html`<${Modal} title="Als gepostet markieren" onClose=${onClose}
    footer=${html`<${Button} variant="ghost" onClick=${onClose}>Abbrechen<//><${Button} variant="primary" busy=${busy} onClick=${submit}>Markieren<//>`}>
    <p class="text-2">„${v.title}“</p>
    <form class="stack" onSubmit=${submit}>
      <${FormError} error=${error && !error.fields ? error : null} />
      <${SelectField} label="Plattform" value=${platform} onChange=${setPlatform} options=${PLATFORMS} />
      <${SelectField} label="Konto" value=${accountId} onChange=${setAccountId} placeholder=${options.length ? 'Ohne Konto' : 'Kein Konto für diese Plattform'}
        options=${options.map((a) => ({ value: a.id, label: `${a.label}${a.handle ? ` (${a.handle})` : ''}` }))}
        help=${options.length ? null : html`Konten legst du unter <a href="#/konten">Konten</a> an.`} />
      <${TextField} label="Link zum Post" optional type="url" value=${url} onInput=${setUrl} placeholder="https://…" error=${fields.url} />
      <${TextField} label="Notiz" optional value=${note} onInput=${setNote} />
    </form>
  <//>`;
}

function UploadModal({ v, accounts, onClose, onDone }) {
  const yt = accounts.filter((a) => a.platform === 'youtube' && a.connected);
  const [accountId, setAccountId] = useState(yt[0] ? String(yt[0].id) : '');
  const [privacy, setPrivacy] = useState('private');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const submit = async () => {
    setBusy(true); setError(null);
    try {
      const res = await api.uploadVariant(v.id, { account_id: Number(accountId), privacy });
      toast.success(`Upload gestartet (Job #${res.job_id}).`, { action: { href: `#/jobs?job=${res.job_id}`, label: 'Job ansehen' } });
      onDone(); onClose();
    } catch (err) { setError(err); } finally { setBusy(false); }
  };
  return html`<${Modal} title="Auf YouTube hochladen" onClose=${onClose}
    footer=${html`<${Button} variant="ghost" onClick=${onClose}>Abbrechen<//><${Button} variant="primary" icon="upload" busy=${busy} disabled=${!accountId} onClick=${submit}>Hochladen<//>`}>
    <${FormError} error=${error} />
    <div class="stack-sm"><span class="field-label">Titel</span><p class="text-2">${v.title}</p></div>
    <${SelectField} label="YouTube-Konto" value=${accountId} onChange=${setAccountId}
      options=${yt.map((a) => ({ value: a.id, label: `${a.label}${a.handle ? ` (${a.handle})` : ''}` }))} />
    <div class="field"><span class="field-label">Sichtbarkeit</span>
      <${Segmented} label="Sichtbarkeit" value=${privacy} onChange=${setPrivacy} options=${Object.entries(PRIVACY).map(([value, label]) => ({ value, label }))} />
      <p class="field-help">Tipp: erst „Privat“ hochladen, in YouTube Studio prüfen, dann veröffentlichen.</p></div>
    <${Callout} tone="info">Der Upload läuft als Job im Hintergrund. Titel, Beschreibung und Hashtags dieser Variante werden übernommen.<//>
  <//>`;
}

function DubModal({ video, onClose }) {
  const [lang, setLang] = useState('es');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const submit = async () => {
    setBusy(true); setError(null);
    try {
      const res = await api.dubVideo(video.id, { lang });
      toast.success(`Dub (${langLabel(lang)}) gestartet – Job #${res.job_id}.`, { action: { href: `#/jobs?job=${res.job_id}`, label: 'Job ansehen' } });
      onClose();
    } catch (err) { setError(err); } finally { setBusy(false); }
  };
  const langs = Object.entries(LANGUAGES).filter(([code]) => code !== 'en').map(([value, label]) => ({ value, label }));
  return html`<${Modal} title="Dub erstellen" onClose=${onClose} size="sm"
    footer=${html`<${Button} variant="ghost" onClick=${onClose}>Abbrechen<//><${Button} variant="primary" icon="languages" busy=${busy} onClick=${submit}>Dub starten<//>`}>
    <${FormError} error=${error} />
    <p class="text-2">Claude übersetzt die Zeilen, Fish Audio spricht sie in der geklonten Stimme jedes Sprechers. Das fertige Video erscheint hier als eigenes Video mit KI-Dub-Hinweis.</p>
    <${SelectField} label="Sprache" value=${lang} onChange=${setLang} options=${langs} />
    <p class="muted small">Braucht den Fish-Audio-Schlüssel und kostet Claude-Tokens für die Übersetzung.</p>
  <//>`;
}
