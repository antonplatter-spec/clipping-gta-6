// Small runtime shared by every page: view library, router, German formatting, polling, toasts, clipboard.
import { h, render, Fragment, createContext, createPortal } from '../vendor/preact.js';
import {
  useState, useEffect, useLayoutEffect, useRef, useMemo, useCallback, useContext, useReducer, useId,
} from '../vendor/preact-hooks.js';
import htm from '../vendor/htm.js';

export const html = htm.bind(h);
export {
  h, render, Fragment, createContext, createPortal,
  useState, useEffect, useLayoutEffect, useRef, useMemo, useCallback, useContext, useReducer, useId,
};

// ---------------------------------------------------------------------------------------------------------------
// Tiny store: module-level state with subscribers (toasts, session, live overview)
// ---------------------------------------------------------------------------------------------------------------

export function createStore(initial) {
  let state = initial;
  const subs = new Set();
  return {
    get: () => state,
    set(next) {
      state = typeof next === 'function' ? next(state) : next;
      subs.forEach((fn) => fn(state));
    },
    subscribe(fn) { subs.add(fn); return () => subs.delete(fn); },
  };
}

export function useStore(store) {
  const [value, setValue] = useState(store.get());
  useEffect(() => {
    setValue(store.get());
    return store.subscribe(setValue);
  }, [store]);
  return value;
}

// ---------------------------------------------------------------------------------------------------------------
// Hash router: #/seite/unterseite?x=1
// ---------------------------------------------------------------------------------------------------------------

export function parseHash(hash = location.hash) {
  const raw = hash.replace(/^#/, '') || '/';
  const [path, qs = ''] = raw.split('?');
  const parts = path.split('/').filter(Boolean).map(decodeURIComponent);
  return { path: `/${parts.join('/')}`, parts, query: Object.fromEntries(new URLSearchParams(qs)), raw };
}

const routeStore = createStore(parseHash());
window.addEventListener('hashchange', () => routeStore.set(parseHash()));

export function useRoute() { return useStore(routeStore); }

export function navigate(path, { replace = false } = {}) {
  const target = `#${path.startsWith('/') ? path : `/${path}`}`;
  if (replace) {
    history.replaceState(null, '', target);
    routeStore.set(parseHash(target));
  } else if (location.hash !== target) {
    location.hash = target;
  } else {
    routeStore.set(parseHash(target));
  }
}

/** Update the query part of the current route without adding history entries. */
export function setQuery(patch) {
  const r = parseHash();
  const q = { ...r.query, ...patch };
  for (const k of Object.keys(q)) if (q[k] === undefined || q[k] === null || q[k] === '') delete q[k];
  const qs = new URLSearchParams(q).toString();
  navigate(`${r.path}${qs ? `?${qs}` : ''}`, { replace: true });
}

// ---------------------------------------------------------------------------------------------------------------
// German formatting
// ---------------------------------------------------------------------------------------------------------------

const nf = new Intl.NumberFormat('de-DE');
const nf1 = new Intl.NumberFormat('de-DE', { maximumFractionDigits: 1 });
const nf2 = new Intl.NumberFormat('de-DE', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const eurFmt = new Intl.NumberFormat('de-DE', { style: 'currency', currency: 'EUR' });
const eurSmall = new Intl.NumberFormat('de-DE', { style: 'currency', currency: 'EUR', maximumFractionDigits: 4 });
const usdFmt = new Intl.NumberFormat('de-DE', { style: 'currency', currency: 'USD' });

export const fmt = {
  num: (n) => (n == null || Number.isNaN(n) ? '–' : nf.format(n)),
  num1: (n) => (n == null ? '–' : nf1.format(n)),
  num2: (n) => (n == null ? '–' : nf2.format(n)),
  eur: (n) => (n == null ? '–' : (Math.abs(n) > 0 && Math.abs(n) < 0.01 ? eurSmall : eurFmt).format(n)),
  usd: (n) => (n == null ? '–' : usdFmt.format(n)),
  factor: (x) => (x == null ? '–' : `${nf2.format(x)}×`),
  /** 12.345 → "12.345", 1.234.567 → "1,2 Mio." */
  compact(n) {
    if (n == null) return '–';
    if (Math.abs(n) >= 1e6) return `${nf1.format(n / 1e6)} Mio.`;
    if (Math.abs(n) >= 1e4) return `${nf1.format(n / 1e3)} Tsd.`;
    return nf.format(n);
  },
  gb: (n) => (n == null ? '–' : `${nf1.format(n)} GB`),
  /** seconds → "0:42", "12:03", "1:02:03" */
  clock(sec) {
    if (sec == null || Number.isNaN(sec)) return '–';
    const s = Math.max(0, Math.round(sec));
    const hh = Math.floor(s / 3600);
    const mm = Math.floor((s % 3600) / 60);
    const ss = String(s % 60).padStart(2, '0');
    return hh ? `${hh}:${String(mm).padStart(2, '0')}:${ss}` : `${mm}:${ss}`;
  },
  /** seconds → "42 s", "3 Min.", "2 Std. 5 Min." */
  duration(sec) {
    if (sec == null) return '–';
    const s = Math.round(sec);
    if (s < 60) return `${s} s`;
    if (s < 3600) return `${Math.round(s / 60)} Min.`;
    const hh = Math.floor(s / 3600);
    const mm = Math.round((s % 3600) / 60);
    return mm ? `${hh} Std. ${mm} Min.` : `${hh} Std.`;
  },
  date(ts) {
    if (!ts) return '–';
    return new Date(ts * 1000).toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit', year: 'numeric' });
  },
  time(ts) {
    if (!ts) return '–';
    return new Date(ts * 1000).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
  },
  dateTime(ts) {
    if (!ts) return '–';
    const d = new Date(ts * 1000);
    const today = new Date();
    const time = d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
    if (d.toDateString() === today.toDateString()) return `heute ${time}`;
    const y = new Date(today); y.setDate(today.getDate() - 1);
    if (d.toDateString() === y.toDateString()) return `gestern ${time}`;
    const t = new Date(today); t.setDate(today.getDate() + 1);
    if (d.toDateString() === t.toDateString()) return `morgen ${time}`;
    return `${d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit' })} ${time}`;
  },
  /** "vor 3 Min.", "in 2 Std." */
  relative(ts, now = Date.now() / 1000) {
    if (!ts) return '–';
    const diff = ts - now;
    const a = Math.abs(diff);
    let v;
    if (a < 45) return 'gerade eben';
    if (a < 3600) v = `${Math.max(1, Math.round(a / 60))} Min.`;
    else if (a < 86400) v = `${Math.round(a / 3600)} Std.`;
    else v = `${Math.round(a / 86400)} ${Math.round(a / 86400) === 1 ? 'Tag' : 'Tagen'}`;
    return diff < 0 ? `vor ${v}` : `in ${v}`;
  },
  /** "2026-11-17" → "Heute", "Gestern", "Mo., 16. Nov. 2026" */
  day(day) {
    if (!day) return '–';
    const [y, m, d] = day.split('-').map(Number);
    const date = new Date(y, m - 1, d);
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const delta = Math.round((today - date) / 86400000);
    if (delta === 0) return 'Heute';
    if (delta === 1) return 'Gestern';
    return date.toLocaleDateString('de-DE', { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
  },
  dayShort(day) {
    if (!day) return '–';
    const [y, m, d] = day.split('-').map(Number);
    return new Date(y, m - 1, d).toLocaleDateString('de-DE', { day: 'numeric', month: 'numeric' });
  },
  plural: (n, one, many) => `${fmt.num(n)} ${n === 1 ? one : many}`,
};

export function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export function hostOf(url) {
  try { return new URL(url).host.replace(/^www\./, ''); } catch { return url || ''; }
}

/** Hook text with *keyword* markup → array of strings / <mark> nodes. */
export function hookText(text) {
  if (!text) return '';
  const parts = String(text).split(/(\*[^*]+\*)/g).filter(Boolean);
  return parts.map((p) => (p.startsWith('*') && p.endsWith('*') && p.length > 2
    ? html`<mark class="kw">${p.slice(1, -1)}</mark>` : p));
}

export const plainHook = (text) => String(text || '').replace(/\*/g, '').trim();

export function cx(...args) {
  return args.flat().filter(Boolean).join(' ');
}

// ---------------------------------------------------------------------------------------------------------------
// Data loading + polling (pauses while the tab is hidden)
// ---------------------------------------------------------------------------------------------------------------

/**
 * Load data with `fn` and re-load every `interval` ms (0 = once). Returns {data, error, loading, reload, setData}.
 * `deps` restart the loader. Polling stops while the document is hidden and refreshes when it becomes visible.
 */
export function useData(fn, deps = [], { interval = 0, keep = true } = {}) {
  const [state, setState] = useState({ data: undefined, error: null, loading: true });
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const seq = useRef(0);
  const intervalRef = useRef(interval);
  intervalRef.current = interval;

  const load = useCallback(async (quiet = false) => {
    const my = ++seq.current;
    if (!quiet) setState((s) => ({ data: keep ? s.data : undefined, error: null, loading: true }));
    try {
      const data = await fnRef.current();
      if (my === seq.current) setState({ data, error: null, loading: false });
      return data;
    } catch (error) {
      if (my === seq.current) setState((s) => ({ data: s.data, error, loading: false }));
      return undefined;
    }
  }, []);

  useEffect(() => {
    load(false);
    let timer = null;
    const tick = () => {
      clearTimeout(timer);
      if (!intervalRef.current) return;
      timer = setTimeout(async () => {
        if (!document.hidden) await load(true);
        tick();
      }, intervalRef.current);
    };
    tick();
    const onVis = () => { if (!document.hidden && intervalRef.current) { load(true); tick(); } };
    document.addEventListener('visibilitychange', onVis);
    return () => { clearTimeout(timer); seq.current++; document.removeEventListener('visibilitychange', onVis); };
  }, [...deps, interval]);

  const setData = useCallback((upd) => setState((s) => ({
    ...s, data: typeof upd === 'function' ? upd(s.data) : upd,
  })), []);
  return { ...state, reload: () => load(true), setData };
}

/**
 * Poll store shared by several components (e.g. the overview used by the shell badges and the Übersicht page):
 * one request at a time, at the shortest interval any subscriber asked for.
 */
export function createPoller(fetcher) {
  const store = createStore({ data: undefined, error: null, at: 0 });
  const wants = new Map();
  let timer = null;
  let inflight = null;
  const minInterval = () => Math.min(...wants.values());
  async function refresh() {
    if (inflight) return inflight;
    inflight = (async () => {
      try {
        const data = await fetcher();
        store.set({ data, error: null, at: Date.now() });
      } catch (error) {
        store.set((s) => ({ ...s, error }));
      } finally {
        inflight = null;
      }
    })();
    return inflight;
  }
  function schedule() {
    clearTimeout(timer);
    if (!wants.size) return;
    timer = setTimeout(async () => {
      if (!document.hidden) await refresh();
      schedule();
    }, minInterval());
  }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && wants.size) { refresh(); schedule(); }
  });
  function use(interval) {
    const value = useStore(store);
    useEffect(() => {
      const key = {};
      wants.set(key, interval);
      const stale = Date.now() - store.get().at > Math.min(interval, 3000);
      if (stale) refresh();
      schedule();
      return () => { wants.delete(key); schedule(); };
    }, [interval]);
    return { ...value, loading: value.data === undefined && !value.error, reload: refresh };
  }
  return { use, refresh, store, reset: () => store.set({ data: undefined, error: null, at: 0 }) };
}

// ---------------------------------------------------------------------------------------------------------------
// Toasts
// ---------------------------------------------------------------------------------------------------------------

export const toastStore = createStore([]);
let toastSeq = 0;

function pushToast(kind, message, opts = {}) {
  const id = ++toastSeq;
  toastStore.set((list) => [...list.slice(-3), { id, kind, message, action: opts.action }]);
  const ttl = opts.ttl ?? (kind === 'error' ? 8000 : 4000);
  if (ttl) setTimeout(() => dismissToast(id), ttl);
  return id;
}
export function dismissToast(id) { toastStore.set((list) => list.filter((t) => t.id !== id)); }
export const toast = {
  success: (m, o) => pushToast('success', m, o),
  info: (m, o) => pushToast('info', m, o),
  error: (m, o) => pushToast('error', m, o),
  /** Shows an ApiError (or any error) in German. */
  fail: (err, prefix) => pushToast('error', `${prefix ? `${prefix}: ` : ''}${errorText(err)}`),
};

export function errorText(err) {
  if (!err) return 'Unbekannter Fehler.';
  if (typeof err === 'string') return err;
  return err.message || 'Unbekannter Fehler.';
}

// ---------------------------------------------------------------------------------------------------------------
// Dialogs (confirm / prompt) through one host component
// ---------------------------------------------------------------------------------------------------------------

export const dialogStore = createStore(null);

/**
 * @param {{title: string, message?: any, confirmLabel?: string, cancelLabel?: string, danger?: boolean,
 *   input?: {label: string, placeholder?: string, multiline?: boolean, optional?: boolean}}} opts
 * @returns {Promise<false|true|string>} false = cancelled; with `input` the entered text
 */
export function confirmDialog(opts) {
  return new Promise((resolve) => {
    dialogStore.set({ ...opts, resolve: (v) => { dialogStore.set(null); resolve(v); } });
  });
}

// ---------------------------------------------------------------------------------------------------------------
// Clipboard (works without a secure context, e.g. http://192.168.x.x from the phone) and downloads
// ---------------------------------------------------------------------------------------------------------------

export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch { /* fall back below */ }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.cssText = 'position:fixed;top:-1000px;opacity:0';
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove();
  return ok;
}

export function downloadText(filename, text) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------------------------------------------------------------------------------------------------------------
// Local preferences (theme, last tabs) — never secrets
// ---------------------------------------------------------------------------------------------------------------

export const prefs = {
  get(key, fallback) {
    try { const v = localStorage.getItem(`automate.${key}`); return v == null ? fallback : JSON.parse(v); } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(`automate.${key}`, JSON.stringify(value)); } catch { /* private mode */ }
  },
};

/** Run an async action with a busy flag; errors become a toast unless `onError` handles them. */
export function useAction(fn, { onError } = {}) {
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);
  const run = useCallback(async (...args) => {
    setBusy(true);
    try {
      return await fn(...args);
    } catch (err) {
      if (onError) onError(err); else toast.fail(err);
      return undefined;
    } finally {
      if (mounted.current) setBusy(false);
    }
  }, [fn, onError]);
  return [run, busy];
}

/** Re-render every `ms` so relative times ("vor 3 Min.") stay fresh. */
export function useNow(ms = 30000) {
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now() / 1000), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

/** Sets document.title for a page. */
export function useTitle(title) {
  useEffect(() => { document.title = title ? `${title} · GTA Clipper` : 'GTA Clipper – Automatik'; }, [title]);
}

export function useMedia(query) {
  const [match, setMatch] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const on = () => setMatch(mq.matches);
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, [query]);
  return match;
}
