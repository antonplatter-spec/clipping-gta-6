// Entry point: session gate (setup wizard / login / app), hash routes and the app shell.
import * as api from './api.js';
import {
  html, render, useEffect, useState, useStore, useRoute, navigate, prefs, cx, fmt, toast, useAction,
} from './lib.js';
import { Icon, IconButton, Button, Toasts, DialogHost, Spinner, ErrorState } from './ui.js';
import { sessionStore, refreshSession, sessionExpired, overview } from './store.js';
import { LoginPage } from './pages/login.js';
import { SetupPage } from './pages/setup.js';
import { OverviewPage } from './pages/overview.js';
import { ApprovalPage } from './pages/approval.js';
import { VideosPage } from './pages/videos.js';
import { JobsPage } from './pages/jobs.js';
import { SourcesPage } from './pages/sources.js';
import { SettingsPage } from './pages/settings.js';
import { CostsPage } from './pages/costs.js';
import { AccountsPage } from './pages/accounts.js';
import { SystemPage } from './pages/system.js';

const NAV = [
  { id: 'uebersicht', label: 'Übersicht', icon: 'layout-dashboard', page: OverviewPage },
  { id: 'freigabe', label: 'Freigabe', icon: 'badge-check', page: ApprovalPage },
  { id: 'videos', label: 'Videos', icon: 'film', page: VideosPage },
  { id: 'jobs', label: 'Jobs', icon: 'list-todo', page: JobsPage },
  { id: 'quellen', label: 'Quellen', icon: 'radio', page: SourcesPage },
  { sep: true },
  { id: 'einstellungen', label: 'Einstellungen', icon: 'sliders-horizontal', page: SettingsPage },
  { id: 'kosten', label: 'Kosten & Schlüssel', icon: 'euro', page: CostsPage },
  { id: 'konten', label: 'Konten', icon: 'users', page: AccountsPage },
  { id: 'system', label: 'System', icon: 'server', page: SystemPage },
];

api.onUnauthorized(() => sessionExpired());

// ---------------------------------------------------------------------------------------------------------------
// Theme toggle: auto → hell → dunkel
// ---------------------------------------------------------------------------------------------------------------

const THEMES = { auto: { label: 'Automatisch', icon: 'sun-moon' }, light: { label: 'Hell', icon: 'sun' }, dark: { label: 'Dunkel', icon: 'moon' } };

function ThemeToggle() {
  const [theme, setTheme] = useState(prefs.get('theme', 'auto'));
  const next = { auto: 'light', light: 'dark', dark: 'auto' }[theme] || 'auto';
  const apply = () => {
    prefs.set('theme', next);
    if (next === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', next);
    setTheme(next);
  };
  const t = THEMES[theme] || THEMES.auto;
  return html`<${IconButton} icon=${t.icon} label=${`Design: ${t.label} (wechseln zu ${THEMES[next].label})`} onClick=${apply} />`;
}

// ---------------------------------------------------------------------------------------------------------------
// Shell
// ---------------------------------------------------------------------------------------------------------------

function Sidebar({ current, open, onNavigate, username, version }) {
  const ov = overview.use(20000).data;
  const [doLogout, busy] = useAction(async () => {
    await api.logout();
    sessionExpired();
    await refreshSession();
    navigate('/login', { replace: true });
  });
  const badges = {
    freigabe: ov && ov.approvals_pending ? { n: ov.approvals_pending, tone: 'yellow', label: 'offene Freigaben' } : null,
    jobs: ov && ov.jobs && ov.jobs.failed ? { n: ov.jobs.failed, tone: 'red', label: 'Jobs mit Fehler' } : null,
  };
  const auto = ov && ov.automation;
  return html`<nav class=${cx('sidebar', open && 'is-open')} aria-label="Hauptnavigation" id="sidebar">
    <a class="brand" href="#/uebersicht" onClick=${onNavigate}>
      <span class="brand-mark" aria-hidden="true">GC</span>
      <span><span class="brand-name">GTA Clipper</span><br /><span class="brand-sub">Automatik</span></span>
    </a>
    <div class="nav">
      ${NAV.map((n, i) => (n.sep ? html`<div class="nav-sep" key=${`sep${i}`}></div>` : html`<a href=${`#/${n.id}`} key=${n.id}
        aria-current=${current === n.id ? 'page' : undefined} onClick=${onNavigate}>
        <${Icon} name=${n.icon} size=${18} /><span>${n.label}</span>
        ${badges[n.id] && html`<span class=${cx('nav-badge', `tone-${badges[n.id].tone}`)} aria-label=${`${badges[n.id].n} ${badges[n.id].label}`}>${badges[n.id].n > 99 ? '99+' : badges[n.id].n}</span>`}
      </a>`))}
    </div>
    ${auto && html`<div class="sidebar-status" aria-label="Status">
      <div class="status-line"><span class=${cx('status-dot', auto.enabled && 'on')}></span>Automatik ${auto.enabled ? 'an' : 'aus'}</div>
      <div class="status-line"><span class=${cx('status-dot', ov.live && ov.live.recorders ? 'rec' : auto.live_enabled && 'on')}></span>
        Live ${auto.live_enabled ? (ov.live && ov.live.recorders ? `· ${ov.live.recorders} nimmt auf` : 'an') : 'aus'}</div>
      ${auto.enabled && auto.next_scan_at && html`<div class="status-line muted"><${Icon} name="clock" size=${14} />Nächste Suche ${fmt.dateTime(auto.next_scan_at)}</div>`}
    </div>`}
    <div class="sidebar-foot">
      <div class="user-line"><${Icon} name="user" size=${16} /><span class="truncate">${username || 'Angemeldet'}</span></div>
      <div class="row">
        <${ThemeToggle} />
        <${Button} variant="ghost" icon="log-out" busy=${busy} onClick=${doLogout}>Abmelden<//>
      </div>
      <div class="version">Version ${version || '–'}</div>
    </div>
  </nav>`;
}

function Shell({ route, state }) {
  const [menuOpen, setMenuOpen] = useState(false);
  const id = route.parts[0] || 'uebersicht';
  const entry = NAV.find((n) => n.id === id);
  useEffect(() => { setMenuOpen(false); }, [route.path]);
  useEffect(() => {
    if (!menuOpen) return undefined;
    const onKey = (e) => { if (e.key === 'Escape') setMenuOpen(false); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [menuOpen]);
  const Page = entry ? entry.page : NotFound;
  return html`<div class="app">
    <a class="skip-link" href="#main" onClick=${(e) => { e.preventDefault(); document.getElementById('main').focus(); }}>Zum Inhalt springen</a>
    <header class="topbar">
      <${IconButton} icon="menu" label="Menü öffnen" aria-expanded=${menuOpen ? 'true' : 'false'} aria-controls="sidebar" onClick=${() => setMenuOpen(true)} />
      <span class="topbar-title">${entry ? entry.label : 'GTA Clipper'}</span>
      <${TopbarBadge} />
    </header>
    ${menuOpen && html`<div class="sidebar-backdrop" onClick=${() => setMenuOpen(false)}></div>`}
    <${Sidebar} current=${id} open=${menuOpen} onNavigate=${() => setMenuOpen(false)} username=${state.username} version=${state.version} />
    <main class="main" id="main" tabindex="-1">
      <${Page} route=${route} key=${id} />
    </main>
  </div>`;
}

function TopbarBadge() {
  const ov = overview.use(20000).data;
  if (!ov || !ov.approvals_pending) return null;
  return html`<a class="btn btn-sm" href="#/freigabe" aria-label=${`${ov.approvals_pending} offene Freigaben`}>
    <${Icon} name="badge-check" size=${15} /><span class="nav-badge tone-yellow" style="margin:0">${ov.approvals_pending}</span></a>`;
}

function NotFound() {
  return html`<div class="page"><div class="card"><p class="empty-title">Seite nicht gefunden</p>
    <p class="muted">Diese Adresse gibt es im Dashboard nicht.</p><p style="margin-top:12px"><a href="#/uebersicht">Zur Übersicht</a></p></div></div>`;
}

// ---------------------------------------------------------------------------------------------------------------
// Session gate
// ---------------------------------------------------------------------------------------------------------------

function Splash({ error, onRetry }) {
  return html`<div class="auth"><div class="auth-card">
    <div class="auth-brand"><span class="brand-mark" aria-hidden="true">GC</span><span class="brand-name">GTA Clipper · Automatik</span></div>
    ${error ? html`<${ErrorState} error=${error} onRetry=${onRetry} title="Server nicht erreichbar" />`
      : html`<div class="row" style="justify-content:center" role="status"><${Spinner} size=${18} /><span class="muted">Verbinde …</span></div>`}
  </div></div>`;
}

function App() {
  const route = useRoute();
  const { state, error } = useStore(sessionStore);
  const top = route.parts[0] || '';

  // Redirects happen in an effect, never during render.
  let redirect = null;
  if (state) {
    if (state.setup_required) {
      if (top !== 'einrichtung') redirect = '/einrichtung/konto';
    } else if (!state.authenticated) {
      if (top !== 'login') {
        const next = route.raw && route.raw !== '/' && top !== 'einrichtung' ? route.raw : '';
        redirect = `/login${next ? `?next=${encodeURIComponent(next)}` : ''}`;
      }
    } else if (top === 'login') {
      const next = route.query.next;
      redirect = next && next.startsWith('/') && !next.startsWith('/login') ? next : '/uebersicht';
    } else if (!top) {
      redirect = '/uebersicht';
    }
  }
  useEffect(() => { if (redirect) navigate(redirect, { replace: true }); }, [redirect]);

  if (!state) return html`<${Splash} error=${error} onRetry=${refreshSession} />`;
  if (redirect) return null;
  if (state.setup_required || top === 'einrichtung') return html`<${SetupPage} route=${route} state=${state} />`;
  if (!state.authenticated) return html`<${LoginPage} route=${route} />`;
  return html`<${Shell} route=${route} state=${state} />`;
}

function Root() {
  return html`<${App} /><${Toasts} /><${DialogHost} />`;
}

// YouTube OAuth callback lands on #/konten?youtube=…; everything else just renders.
refreshSession();
render(html`<${Root} />`, document.getElementById('app'));

// Unexpected script errors become a visible toast instead of a silent broken page.
window.addEventListener('unhandledrejection', (e) => {
  const err = e.reason;
  if (err && err.name === 'ApiError' && err.status === 401) return;
  if (err && err.name === 'AbortError') return;
  toast.fail(err || 'Unbekannter Fehler');
});
