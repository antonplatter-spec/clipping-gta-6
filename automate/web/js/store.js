// App-wide state: the session (GET /api/state) and the overview poll shared by the shell and the Übersicht page.
import * as api from './api.js';
import { createStore, createPoller } from './lib.js';

/** {state: State|null, error: ApiError|null} */
export const sessionStore = createStore({ state: null, error: null });

export async function refreshSession() {
  try {
    const state = await api.getState();
    sessionStore.set({ state, error: null });
    return state;
  } catch (error) {
    sessionStore.set((s) => ({ ...s, error }));
    return null;
  }
}

/** Marks the session as gone (401 from any request); the app then shows the login. */
export function sessionExpired() {
  const s = sessionStore.get().state;
  if (s && s.authenticated) sessionStore.set({ state: { ...s, authenticated: false }, error: null });
  overview.reset();
}

export const overview = createPoller(() => api.getOverview());

/** Profiles are needed by several pages (filters, forms); cached for a minute. */
let profilesCache = { at: 0, promise: null };
export function loadProfiles(force = false) {
  if (force || !profilesCache.promise || Date.now() - profilesCache.at > 60000) {
    profilesCache = { at: Date.now(), promise: api.listProfiles().then(api.itemsOf).catch((e) => { profilesCache.at = 0; throw e; }) };
  }
  return profilesCache.promise;
}
export function invalidateProfiles() { profilesCache = { at: 0, promise: null }; }

/** The music library (sounds by role for the effects editor); cached for a minute, dropped after an upload/delete. */
let soundsCache = { at: 0, promise: null };
export function loadSounds(force = false) {
  if (force || !soundsCache.promise || Date.now() - soundsCache.at > 60000) {
    soundsCache = { at: Date.now(), promise: api.getAssets().then((d) => d.music || []).catch((e) => { soundsCache.at = 0; throw e; }) };
  }
  return soundsCache.promise;
}
export function invalidateSounds() { soundsCache = { at: 0, promise: null }; }

/** The caption, hook and sound defaults from Einstellungen (watchlist.yaml + overrides), for "Standard (…)" labels. */
let styleCache = { at: 0, promise: null };
export function loadStyleDefaults(force = false) {
  if (force || !styleCache.promise || Date.now() - styleCache.at > 60000) {
    styleCache = { at: Date.now(), promise: api.getWatchlist().then((d) => d.effective || {}).catch((e) => { styleCache.at = 0; throw e; }) };
  }
  return styleCache.promise;
}
export function invalidateStyleDefaults() { styleCache = { at: 0, promise: null }; }
