// REST client of the dashboard and the record of what the frontend expects from the backend (spec §7).
// Every network access of the dashboard goes through `request()` below: it sends cookies, adds the CSRF header
// `X-Requested-With: automate`, parses `{error: {code, message}}` into a thrown `ApiError` and sends the browser to
// the login view when a session is missing (401).
//
// General rules (apply to every endpoint below):
// - JSON in and out, prefix /api. Timestamps are unix seconds (number, may be fractional). Days are local
//   "YYYY-MM-DD" strings. Money: `eur`/`usd` numbers (not rounded by the server).
// - Lists are `{items: [...], total: n}` — including GET /api/keys and GET /api/social/accounts (the client also
//   accepts a bare array there, see `itemsOf`). `total` counts all matches ignoring limit/offset.
// - Errors: `{"error": {"code": "snake_case", "message": "German text", "fields"?: {name: "German text"},
//   "check_report"?: CheckReport}}`. `fields` carries per-field messages for 422 validation errors (form field
//   names, or setting keys / dotted config paths); the dashboard shows them next to the field and `message` above
//   the form. 401 = no valid session (and failed login), 403 = missing CSRF header, 404, 409 = state conflict
//   (e.g. approving a pick that is no longer proposed), 422 = validation, 429 = too many login attempts.
// - Mutations that return the changed object return the full object (same shape as the GET of that object), so the
//   dashboard can update in place without refetching. DELETEs return `{ok: true}`.
//
// Additions to the spec (backend please implement; each is also marked "ADDITION" where it is documented):
//  1. Error body may carry `fields` (per-field 422 messages) and, for PATCH /api/picks/{id}, `check_report`.
//  2. GET /api/state: `setup_step` is "account" | "totp" | null. POST /api/setup may be called again while the
//     account's 2FA is not yet confirmed (replaces that unconfirmed account), so a reload mid-setup can recover.
//  3. POST /api/account/totp/confirm returns `{recovery_codes}` (new codes; the old ones stop working).
//  4. GET /api/overview: `automation.scan_running` (bool) and `ready.sources` (number of active creators+streams
//     in enabled profiles). POST /api/automation returns the `automation` block. POST /api/scan starts the scan in
//     the background and returns at once `{ok, message}`.
//  5. GET /api/jobs: `group` also accepts "canceled" (and is optional = all); the response adds
//     `counts: {waiting, running, done, failed, canceled}` (same kind/profile filter, ignoring group). Job.group is
//     one of waiting|running|done|failed|canceled.
//  6. GET /api/picks: `status` accepts a comma-separated list ("approved,rendering"); the response adds
//     `counts: {proposed, approved, rendering, rendered, rejected, failed}` (same profile filter, ignoring status).
//     Pick gains `video_id` (number|null, the rendered video) and `simulate_report` has the shape documented below.
//  7. GET /api/videos: `archived=1` lists archived videos (default 0 = not archived).
//  8. GET /api/streams rows carry the live status where known (`live`, `on_topic`, `recording`, `title`,
//     `checked_at`, all nullable) and `platform`.
//  9. GET /api/settings schema: field format documented in `SettingSchema` (type names, choices as
//     `{value, label, help?}`, `auto`, `unit`, `step`, `hidden`).
// 10. GET /api/social/youtube/app adds `client_id_hint` (masked client id or null) and `redirect_note` (German
//     explanation of the redirect, shown in the YouTube card). PUT accepts either field alone.
//     The OAuth callback finishes by sending the browser to `/#/konten?youtube=connected` or
//     `/#/konten?youtube=error&message=<urlencoded German text>`.
// 11. Tool rows in GET /api/system may carry `required` (bool) and `purpose` (German, what the tool is for).
// 12. GET /api/keys and GET /api/social/accounts use the general list shape `{items, total}` (§7 wrote `[...]`).
//
// Decided here where §7 is silent (the backend implements exactly these; details at each function):
//  - POST /api/login → State. Wrong credentials → 401 `invalid_credentials` (the client does not redirect on it).
//  - Every mutation returns the changed object: retry/cancel/createJob → Job, approve → {pick, job_id},
//    reject → {pick}, bulk → {results}, PATCH video → Video, markPosted → Post, profile/creator/stream/account
//    create+update → the row, PUT key → KeyInfo, disconnect → Account, ack → Alert, PATCH settings → {values}.
//  - Pick.check_report is the per-pick clipper check entry (not the whole {"video N": …} report);
//    Pick.simulate_report is {ranking, keep} (see typedefs). Post.status: posted | uploading | failed.
//  - Image/video GETs (/api/jobs/{id}/sheets/{name}, /api/variants/{id}/file) are loaded by <img>/<video>, which
//    cannot send headers: they need the session cookie only (the client sends X-Requested-With on every fetch;
//    require it on mutating methods only). `?download=1` → Content-Disposition: attachment.
//
// Serving: the backend mounts automate/web/ at / and serves index.html for /. Send *.js as text/javascript (module
// scripts with another MIME type are refused by browsers). The dashboard needs no inline script or style and works
// under `default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; script-src 'self'`
// (the QR code is shown as a data: image).

const BASE = '/api';

/** Thrown for every failed request. `status` 0 = network error (server not reachable). */
export class ApiError extends Error {
  /**
   * @param {number} status HTTP status (0 = no response)
   * @param {string} code snake_case error code from the server
   * @param {string} message German message for the user
   * @param {{fields?: Object<string,string>, check_report?: CheckReport}} [extra]
   */
  constructor(status, code, message, extra = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    /** @type {Object<string,string>|null} per-field messages (422) */
    this.fields = extra.fields || null;
    /** @type {CheckReport|null} check report of a rejected pick edit (422) */
    this.check_report = extra.check_report || null;
  }
}

const DEFAULT_MESSAGES = {
  400: 'Die Anfrage war ungültig.',
  401: 'Bitte melde dich an.',
  403: 'Diese Aktion ist nicht erlaubt.',
  404: 'Nicht gefunden.',
  409: 'Der Zustand hat sich inzwischen geändert. Bitte neu laden.',
  413: 'Die Daten sind zu groß.',
  422: 'Bitte prüfe die Eingaben.',
  429: 'Zu viele Versuche. Bitte warte kurz.',
  500: 'Interner Serverfehler. Details stehen im Server-Log (logs/automate/).',
  502: 'Der Server ist gerade nicht erreichbar.',
  503: 'Der Server ist gerade beschäftigt. Bitte gleich noch einmal versuchen.',
};

function defaultMessage(status) {
  return DEFAULT_MESSAGES[status] || (status >= 500 ? DEFAULT_MESSAGES[500] : `Fehler ${status}.`);
}

function query(params) {
  if (!params) return '';
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue;
    q.set(k, typeof v === 'boolean' ? (v ? '1' : '0') : String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : '';
}

let unauthorizedHandler = null;
/** The app shell registers what happens on a 401 (default: go to #/login?next=<current route>). */
export function onUnauthorized(fn) { unauthorizedHandler = fn; }

function handleUnauthorized() {
  if (unauthorizedHandler) return unauthorizedHandler();
  const here = location.hash.replace(/^#/, '') || '/uebersicht';
  if (!here.startsWith('/login') && !here.startsWith('/einrichtung')) {
    location.hash = `#/login?next=${encodeURIComponent(here)}`;
  }
}

/**
 * The one place that talks to the server.
 * @param {string} method
 * @param {string} path path below /api, e.g. "/jobs/3"
 * @param {{body?: any, params?: Object, signal?: AbortSignal, redirect401?: boolean}} [opts]
 */
export async function request(method, path, { body, params, signal, redirect401 = true } = {}) {
  const headers = { 'X-Requested-With': 'automate', Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let res;
  try {
    res = await fetch(BASE + path + query(params), {
      method, headers, signal, credentials: 'same-origin', cache: 'no-store',
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    if (e && e.name === 'AbortError') throw e;
    throw new ApiError(0, 'network_error', 'Keine Verbindung zum Server. Läuft „uv run python -m automate“ noch?');
  }
  let data = null;
  if (res.status !== 204 && (res.headers.get('content-type') || '').includes('json')) {
    try { data = await res.json(); } catch { data = null; }
  }
  if (!res.ok) {
    const err = (data && data.error) || {};
    if (res.status === 401 && redirect401) handleUnauthorized();
    throw new ApiError(res.status, err.code || `http_${res.status}`, err.message || defaultMessage(res.status), err);
  }
  return data;
}

/** Accepts `{items: [...]}` or a bare array. @template T @param {{items: T[]}|T[]} res @returns {T[]} */
export function itemsOf(res) {
  return Array.isArray(res) ? res : (res && res.items) || [];
}

// ---------------------------------------------------------------------------------------------------------------
// Shared shapes
// ---------------------------------------------------------------------------------------------------------------

/** @typedef {{items: any[], total: number}} List */
/** @typedef {{ok: true}} Ok */

// ---------------------------------------------------------------------------------------------------------------
// Session, first run, account
// ---------------------------------------------------------------------------------------------------------------

/**
 * @typedef {Object} State
 * @property {boolean} setup_required  true while no account with confirmed 2FA exists (first run)
 * @property {boolean} authenticated   the request carries a valid session cookie
 * @property {string|null} username
 * @property {boolean} totp_enabled
 * @property {string} version          automate.__version__
 * @property {"account"|"totp"|null} setup_step  ADDITION: "account" = no user yet; "totp" = user created but 2FA
 *   not confirmed (POST /api/setup again restarts the 2FA step); null = setup done
 */

/** GET /api/state — no session needed. @returns {Promise<State>} */
export const getState = () => request('GET', '/state', { redirect401: false });

/**
 * @typedef {Object} TotpSetup
 * @property {string} otpauth_uri  otpauth://totp/GTA%20Clipper:<user>?secret=…&issuer=GTA%20Clipper
 * @property {string} totp_secret  base32, shown for manual entry
 * @property {string} qr_svg       complete <svg> document of the QR code (dark modules on transparent/white)
 */

/**
 * POST /api/setup — first run only. Password ≥ 10 characters (422 with `fields.password` otherwise),
 * username 1–64 characters (`fields.username`).
 * @param {{username: string, password: string}} body
 * @returns {Promise<TotpSetup>}
 */
export const setup = (body) => request('POST', '/setup', { body, redirect401: false });

/**
 * POST /api/setup/confirm — confirms the first TOTP code, finishes the account, sets the session cookie.
 * Wrong code → 422 `{code: "invalid_code", fields: {code}}`.
 * @param {{code: string}} body
 * @returns {Promise<{recovery_codes: string[]}>} 10 one-time codes, shown once
 */
export const setupConfirm = (body) => request('POST', '/setup/confirm', { body, redirect401: false });

/**
 * POST /api/login — `code` is a 6-digit TOTP code or one recovery code. Sets the session cookie.
 * Wrong credentials → 401 `invalid_credentials` (no redirect); too many attempts → 429 `rate_limited`.
 * @param {{username: string, password: string, code: string}} body
 * @returns {Promise<State>}
 */
export const login = (body) => request('POST', '/login', { body, redirect401: false });

/** POST /api/logout — deletes the session. @returns {Promise<Ok>} */
export const logout = () => request('POST', '/logout', { redirect401: false });

/**
 * POST /api/account/password — wrong current password → 422 `fields.current_password`; wrong code → 422
 * `fields.code`; weak new password → 422 `fields.new_password`.
 * @param {{current_password: string, new_password: string, code: string}} body
 * @returns {Promise<Ok>}
 */
export const changePassword = (body) => request('POST', '/account/password', { body });

/**
 * POST /api/account/totp/reset — starts re-enrolling 2FA (the old secret stays valid until confirmed).
 * Wrong password → 422 `fields.password`.
 * @param {{password: string}} body
 * @returns {Promise<TotpSetup>}
 */
export const resetTotp = (body) => request('POST', '/account/totp/reset', { body });

/**
 * POST /api/account/totp/confirm — activates the new secret. ADDITION: returns fresh recovery codes (old ones
 * become invalid). Wrong code → 422 `fields.code`.
 * @param {{code: string}} body
 * @returns {Promise<{recovery_codes: string[]}>}
 */
export const confirmTotp = (body) => request('POST', '/account/totp/confirm', { body });

// ---------------------------------------------------------------------------------------------------------------
// Overview, automation, alerts
// ---------------------------------------------------------------------------------------------------------------

/**
 * @typedef {Object} Alert
 * @property {number} id
 * @property {string} key        e.g. "live.not_recording", "disk.low", "key.anthropic_missing", "job.failed.12"
 * @property {"info"|"warning"|"error"} level
 * @property {string} message    German
 * @property {Object|null} details
 * @property {number} first_seen
 * @property {number} last_seen
 * @property {number|null} resolved_at
 * @property {boolean} acknowledged
 */

/**
 * @typedef {Object} LiveStatus  what the live service last saw for a stream (all null when never checked)
 * @property {boolean|null} live
 * @property {boolean|null} on_topic   title says GTA 6
 * @property {boolean|null} recording  a recorder process is running
 * @property {string|null} title       current stream title
 * @property {number|null} checked_at
 */

/** @typedef {LiveStatus & {id: number, name: string, url: string, profile?: string}} LiveStream */

/**
 * @typedef {Object} Overview
 * @property {{waiting: number, running: number, done_today: number, failed: number, waiting_approval: number}} jobs
 *   waiting = queued + waiting_* (incl. waiting_approval); failed = currently failed (not canceled)
 * @property {{enabled: boolean, live_enabled: boolean, next_scan_at: number|null, scan_running?: boolean,
 *   last_scan: {at: number, ok: boolean, summary: ScanSummary|null}|null}} automation
 *   ADDITION: scan_running = a scan (scheduled or "Jetzt suchen") is in progress
 * @property {{streams: LiveStream[], recorders: number}} live  recorders = running recorder processes
 * @property {Alert[]} alerts           active (unresolved) alerts, newest first
 * @property {{today_eur: number, limit_eur: number, tokens_today: number}} costs  limit_eur 0 = no limit
 * @property {number} approvals_pending picks with status "proposed"
 * @property {number} videos_today
 * @property {{disk_free_gb: number|null, tools_ok: boolean, missing: string[]}} system
 * @property {Job[]} recent_jobs        newest first, about 8
 * @property {{anthropic_key: boolean, fish_key: boolean, youtube_connected: boolean, tools_ok: boolean,
 *   sources: number}} ready           ADDITION: sources = active (non-paused) creators + streams of enabled profiles
 */

/**
 * @typedef {Object} ScanSummary  free-form run summary; the dashboard shows `message`, else counts it knows
 * @property {string} [message]   German one-liner, e.g. "2 neue Videos gefunden, 5 übersprungen"
 * @property {number} [new_jobs]
 * @property {number} [checked]
 * @property {number} [skipped]
 */

/** GET /api/overview @returns {Promise<Overview>} */
export const getOverview = () => request('GET', '/overview');

/**
 * POST /api/automation — switch the scheduler (scans) and/or the live service.
 * @param {{enabled?: boolean, live_enabled?: boolean}} body
 * @returns {Promise<Overview["automation"]>}  ADDITION: the updated automation block
 */
export const setAutomation = (body) => request('POST', '/automation', { body });

/**
 * GET /api/alerts?active=1
 * @param {{active?: boolean}} [params]
 * @returns {Promise<{items: Alert[], total: number}>}
 */
export const listAlerts = (params = { active: true }) => request('GET', '/alerts', { params });

/** POST /api/alerts/{id}/ack @returns {Promise<Alert>} */
export const ackAlert = (id) => request('POST', `/alerts/${id}/ack`);

// ---------------------------------------------------------------------------------------------------------------
// Jobs
// ---------------------------------------------------------------------------------------------------------------

/**
 * @typedef {"source"|"live_chunk"|"render"|"dub"|"upload"} JobKind
 * @typedef {"queued"|"running"|"waiting_approval"|"waiting_budget"|"waiting_key"|"done"|"failed"|"skipped"|"canceled"} JobStatus
 * @typedef {"fetch"|"analyze"|"select"|"gate"|"approval"|"render"|"dub"|"upload"|"finished"} JobStage
 * @typedef {"waiting"|"running"|"done"|"failed"|"canceled"} JobGroup
 */

/**
 * @typedef {Object} Job
 * @property {number} id
 * @property {JobKind} kind
 * @property {string} profile
 * @property {JobStatus} status
 * @property {JobGroup} group   waiting = queued + waiting_*; done = done + skipped; canceled is its own group
 * @property {JobStage|null} stage
 * @property {string|null} progress  one short German status line ("Transkribiere …")
 * @property {string|null} error     German, for failed/skipped jobs (skip reason)
 * @property {string|null} title     source video title (render/dub/upload: the video's title)
 * @property {string|null} channel
 * @property {string|null} source_url
 * @property {string|null} clipper_job  jobs/<id> folder name
 * @property {number} attempts
 * @property {number} created_at
 * @property {number} updated_at
 * @property {number|null} started_at
 * @property {number|null} finished_at
 * @property {number|null} parent_id
 * @property {number|null} run_id
 */

/** @typedef {{id: number, ts: number, level: "debug"|"info"|"warning"|"error", message: string}} JobEvent */

/**
 * @typedef {Job & {events: JobEvent[], picks: Pick[], children: Job[], params?: Object, result?: Object}} JobDetail
 * events: oldest first, the last 200. children: jobs with parent_id = this job (render, dub, upload).
 */

/**
 * GET /api/jobs — newest first.
 * @param {{group?: JobGroup, kind?: JobKind, profile?: string, limit?: number, offset?: number}} [params]
 * @returns {Promise<{items: Job[], total: number, counts: {waiting: number, running: number, done: number,
 *   failed: number, canceled: number}}>}  ADDITION: counts per group (same kind/profile filter)
 */
export const listJobs = (params) => request('GET', '/jobs', { params });

/** GET /api/jobs/{id} @returns {Promise<JobDetail>} */
export const getJob = (id) => request('GET', `/jobs/${id}`);

/**
 * POST /api/jobs — clip one video URL now ("URL aus Dashboard: direkt laden"). Bad URL → 422 `fields.url`.
 * @param {{url: string, profile?: string, section?: string}} body  section "H:MM:SS-H:MM:SS" (optional)
 * @returns {Promise<Job>} the created source job
 */
export const createJob = (body) => request('POST', '/jobs', { body });

/** POST /api/jobs/{id}/retry — failed/canceled/skipped jobs only (else 409). @returns {Promise<Job>} */
export const retryJob = (id) => request('POST', `/jobs/${id}/retry`);

/** POST /api/jobs/{id}/cancel — queued/running/waiting_* jobs only (else 409). @returns {Promise<Job>} */
export const cancelJob = (id) => request('POST', `/jobs/${id}/cancel`);

/**
 * POST /api/scan — run ① (find new videos) now, for one profile or every enabled one. ADDITION: starts in the
 * background and returns immediately; a scan already running → 409 `scan_running`.
 * @param {{profile?: string}} [body]
 * @returns {Promise<{ok: true, message: string}>}  message German ("Suche gestartet …")
 */
export const scanNow = (body = {}) => request('POST', '/scan', { body });

// ---------------------------------------------------------------------------------------------------------------
// Picks (Freigabe-Queue)
// ---------------------------------------------------------------------------------------------------------------

/**
 * @typedef {"proposed"|"approved"|"rendering"|"rendered"|"rejected"|"failed"} PickStatus
 * @typedef {"reaction"|"comedy"|"news"|"gameplay"|"showcase"|"scenario"} ClipType
 */

/**
 * A clipper pick (`.claude/commands/clip.md` step 7) as stored in jobs/<id>/picks.json.
 * @typedef {Object} PickData
 * @property {number} [start]  source seconds (single clip)
 * @property {number} [end]
 * @property {Array<{start: number, end: number, job?: string, layout?: Object}>} [segments]  story cut / multi-streamer
 * @property {ClipType} clip_type
 * @property {{type: "fit"|"split", face?: number[]}} [layout]
 * @property {"bold"|"box"|"label"|"none"} [hook_style]
 * @property {"boxed"|"bold"|"comic"} [caption_style]
 * @property {string[]} hooks   one keyword may be marked *like this*
 * @property {string[]} titles  paired with hooks by index
 * @property {string} [description]
 * @property {string[]} [hashtags]
 * @property {string} [voiceover]
 * @property {Array<{s: number, e: number, text: string}>} [labels]
 * @property {Array<{t: number, type: "flash"|"zoom"|"hit", sound?: string, color?: string}>} [fx]
 * @property {Array<{s: number, e: number, mood: string}>} [music]
 * @property {Object<string,string>} [caption_fixes]
 */

/**
 * `clipper check` result for ONE pick: the value `hooks.check_picks` (+ footage check) produces under "video N".
 * Fails (block approval): hooks/titles `fails`, and the keys variety, length, labels, fx, footage (as in
 * `clipper.hooks.has_fails`). Notes (judgment calls): hooks/titles `notes`, pairing, playbook, clip_type, edit.
 * Messages are clipper's own (English). `{}` = clean.
 * @typedef {Object} CheckReport
 * @property {Object<string, {fails: string[], notes?: string[]}>} [hooks]   keyed by hook text
 * @property {Object<string, {fails: string[], notes?: string[]}>} [titles]  keyed by title text
 * @property {string[]} [pairing]
 * @property {string[]} [playbook]
 * @property {string} [clip_type]
 * @property {string} [length]
 * @property {string[]} [labels]
 * @property {string[]} [fx]
 * @property {string[]} [edit]
 * @property {string} [variety]
 * @property {string[]} [footage]
 */

/**
 * `clipper simulate` ranking for ONE pick (output of `simulate.simulate`, best first).
 * @typedef {Object} SimulateReport
 * @property {Array<{hook: string, x: number, low: number, high: number, wins: number, why: string[],
 *   check: string[], type?: string|null}>} ranking  x = predicted views multiplier; type = the hook's strongest
 *   structure (e.g. "number promise"); why = all features incl. modifiers ("names a streamer")
 * @property {number} [keep]  how many hooks the gate kept
 */

/**
 * @typedef {Object} Pick
 * @property {number} id
 * @property {number} job_id
 * @property {number} idx        1-based position in the job's picks.json
 * @property {string} profile
 * @property {PickStatus} status
 * @property {string|null} reason         Claude's "why it works" (German or English, as Claude wrote it)
 * @property {string|null} click_trigger  the named click trigger
 * @property {PickData} data
 * @property {CheckReport|null} check_report
 * @property {SimulateReport|null} simulate_report
 * @property {string|null} error          render error (status failed) or reject reason (status rejected)
 * @property {number} created_at
 * @property {number|null} decided_at
 * @property {{id: number, title: string|null, channel: string|null, source_url: string|null, kind: JobKind,
 *   clipper_job: string|null}} job
 * @property {number} duration            seconds of the finished clip (all segments)
 * @property {string|null} source_link    source URL at the pick's start time (e.g. YouTube &t=123s)
 * @property {string[]} sheets            contact sheet URLs (/api/jobs/{id}/sheets/{name}), best first
 * @property {number|null} source_expires_at  when cleanup deletes the source video (approve before that)
 * @property {number|null} [video_id]     ADDITION: the rendered video (status rendered)
 */

/**
 * GET /api/picks — oldest proposed first for status=proposed, otherwise newest decided first.
 * @param {{status?: string, profile?: string, limit?: number, offset?: number}} [params]
 *   status: one PickStatus or a comma-separated list (ADDITION)
 * @returns {Promise<{items: Pick[], total: number, counts: Object<PickStatus, number>}>}  ADDITION: counts
 */
export const listPicks = (params) => request('GET', '/picks', { params });

/** GET /api/picks/{id} @returns {Promise<Pick>} */
export const getPick = (id) => request('GET', `/picks/${id}`);

/**
 * PATCH /api/picks/{id} — edit a proposed pick; the server re-runs `check` (and re-ranks the hooks so
 * `simulate_report` matches the new hook texts) and saves only when there are no fails.
 * Fails → 422 `{code: "check_failed", message, check_report}` and nothing is saved.
 * `voiceover: ""` or null removes the voiceover. Not proposed → 409.
 * @param {number} id
 * @param {{hooks?: string[], titles?: string[], description?: string, hashtags?: string[], voiceover?: string|null,
 *   start?: number, end?: number, clip_type?: ClipType, hook_style?: string, caption_style?: string}} changes
 * @returns {Promise<{pick: Pick, check_report: CheckReport}>}
 */
export const updatePick = (id, changes) => request('PATCH', `/picks/${id}`, { body: changes });

/**
 * POST /api/picks/{id}/approve — queues the render job. Pick with fails → 422; not proposed → 409.
 * @returns {Promise<{pick: Pick, job_id: number}>}
 */
export const approvePick = (id) => request('POST', `/picks/${id}/approve`);

/**
 * POST /api/picks/{id}/reject
 * @param {number} id
 * @param {{reason?: string}} [body]
 * @returns {Promise<{pick: Pick}>}
 */
export const rejectPick = (id, body = {}) => request('POST', `/picks/${id}/reject`, { body });

/**
 * POST /api/picks/bulk — every id is handled on its own; one failure does not stop the others.
 * @param {{ids: number[], action: "approve"|"reject", reason?: string}} body
 * @returns {Promise<{results: Array<{id: number, ok: boolean, job_id?: number, error?: {code: string,
 *   message: string}}>}>}
 */
export const bulkPicks = (body) => request('POST', '/picks/bulk', { body });

/** URL of a contact sheet (GET /api/jobs/{id}/sheets/{name}, image/jpeg). Pick.sheets already holds these. */
export const sheetUrl = (jobId, name) => `${BASE}/jobs/${jobId}/sheets/${encodeURIComponent(name)}`;

// ---------------------------------------------------------------------------------------------------------------
// Videos, variants, posts
// ---------------------------------------------------------------------------------------------------------------

/**
 * @typedef {"youtube"|"tiktok"|"instagram"|"x"|"facebook"|"other"} Platform
 */

/**
 * @typedef {Object} Post
 * @property {number} id
 * @property {number} variant_id
 * @property {number|null} account_id
 * @property {Platform} platform
 * @property {"posted"|"uploading"|"failed"} status  uploading = YouTube upload job still running
 * @property {string|null} external_id
 * @property {string|null} url
 * @property {"private"|"unlisted"|"public"|null} privacy
 * @property {string|null} note
 * @property {string|null} error   German, for failed uploads
 * @property {number} created_at
 * @property {number|null} posted_at
 */

/**
 * One MP4 of a video (one per hook), from clipper's upload.json.
 * @typedef {Object} Variant
 * @property {number} id
 * @property {number} rank       0 = best hook
 * @property {string} hook       "" for the no-hook A/B arm
 * @property {string|null} hook_type  e.g. "number promise", "none" for the no-hook arm, null = unknown
 * @property {"hook"|"no-hook"} arm
 * @property {string} title      ≤ 100 characters
 * @property {string} description  full post description (credits + hashtags included)
 * @property {string[]} hashtags
 * @property {number|null} predicted  simulated views multiplier (may be null)
 * @property {number|null} duration   seconds
 * @property {string} file_url   /api/variants/{id}/file (Range support); `?download=1` → attachment
 * @property {Post[]} posts
 */

/**
 * @typedef {Object} Video
 * @property {number} id
 * @property {string} profile
 * @property {string} lang        "en" for originals, dub language otherwise
 * @property {string} day         YYYY-MM-DD (export folder date)
 * @property {number} number      "video N" in that day's folder
 * @property {string|null} title
 * @property {string|null} description
 * @property {string[]} sources   source video URLs (credited)
 * @property {boolean} archived
 * @property {number} created_at
 * @property {number|null} job_id
 * @property {number|null} pick_id
 * @property {Variant[]} variants  by rank
 */

/**
 * GET /api/videos — newest day first, then number descending.
 * @param {{profile?: string, day?: string, posted?: 0|1, lang?: string, q?: string, archived?: 0|1,
 *   limit?: number, offset?: number}} [params]  posted=1: any variant has a post; q searches title, hooks,
 *   description; archived (ADDITION) default 0
 * @returns {Promise<{items: Video[], total: number}>}
 */
export const listVideos = (params) => request('GET', '/videos', { params });

/** GET /api/videos/{id} @returns {Promise<Video>} */
export const getVideo = (id) => request('GET', `/videos/${id}`);

/** PATCH /api/videos/{id} @param {number} id @param {{archived: boolean}} body @returns {Promise<Video>} */
export const updateVideo = (id, body) => request('PATCH', `/videos/${id}`, { body });

/** URL of a variant's MP4 for <video> (or as attachment). */
export const variantFileUrl = (id, { download = false } = {}) =>
  `${BASE}/variants/${id}/file${download ? '?download=1' : ''}`;

/**
 * POST /api/variants/{id}/posts — "als gepostet markieren".
 * @param {number} variantId
 * @param {{platform: Platform, account_id?: number|null, url?: string, note?: string}} body
 * @returns {Promise<Post>}
 */
export const markPosted = (variantId, body) => request('POST', `/variants/${variantId}/posts`, { body });

/** DELETE /api/posts/{id} @returns {Promise<Ok>} */
export const deletePost = (id) => request('DELETE', `/posts/${id}`);

/**
 * POST /api/variants/{id}/upload — YouTube upload as a job (creates a Post with status "uploading").
 * Account not connected → 409 `not_connected`.
 * @param {number} variantId
 * @param {{account_id: number, privacy: "private"|"unlisted"|"public"}} body
 * @returns {Promise<{job_id: number}>}
 */
export const uploadVariant = (variantId, body) => request('POST', `/variants/${variantId}/upload`, { body });

/**
 * POST /api/videos/{id}/dub — Fish key missing → 409 `fish_key_missing`.
 * @param {number} videoId
 * @param {{lang: string}} body  ISO 639-1 code, e.g. "es"
 * @returns {Promise<{job_id: number}>}
 */
export const dubVideo = (videoId, body) => request('POST', `/videos/${videoId}/dub`, { body });

// ---------------------------------------------------------------------------------------------------------------
// Sources: profiles, creators, live streams
// ---------------------------------------------------------------------------------------------------------------

/**
 * @typedef {Object} Profile
 * @property {string} name        folder name in profiles/ (e.g. "gta6", "gta-rp")
 * @property {boolean} enabled    scanned by the scheduler / live service
 * @property {number} creators    count (all, incl. paused)
 * @property {number} streams
 * @property {number|null} updated_at
 */

/** GET /api/profiles @returns {Promise<{items: Profile[], total: number}>} */
export const listProfiles = () => request('GET', '/profiles');

/** PATCH /api/profiles/{name} @param {string} name @param {{enabled: boolean}} body @returns {Promise<Profile>} */
export const updateProfile = (name, body) => request('PATCH', `/profiles/${encodeURIComponent(name)}`, { body });

/**
 * @typedef {Object} Creator
 * @property {number} id
 * @property {string} profile
 * @property {string} name
 * @property {string} url        channel videos URL
 * @property {"any"|"keywords"} match  any = every upload is checked for GTA 6; keywords = title must hit a keyword
 * @property {boolean} paused
 * @property {number} priority   lower = scanned first (default 100)
 * @property {number} created_at
 */

/** GET /api/creators?profile= @param {{profile?: string}} [params] @returns {Promise<{items: Creator[], total: number}>} */
export const listCreators = (params) => request('GET', '/creators', { params });

/**
 * POST /api/creators — ref "@handle" or a channel URL; unknown/invalid → 422 `fields.ref`; duplicate → 409.
 * @param {{profile: string, ref: string, name?: string}} body
 * @returns {Promise<Creator>}
 */
export const addCreator = (body) => request('POST', '/creators', { body });

/**
 * PATCH /api/creators/{id}
 * @param {number} id
 * @param {{name?: string, paused?: boolean, priority?: number, match?: "any"|"keywords"}} body
 * @returns {Promise<Creator>}
 */
export const updateCreator = (id, body) => request('PATCH', `/creators/${id}`, { body });

/** DELETE /api/creators/{id} @returns {Promise<Ok>} */
export const deleteCreator = (id) => request('DELETE', `/creators/${id}`);

/**
 * @typedef {Object} StreamBase
 * @property {number} id
 * @property {string} profile
 * @property {string} name        e.g. "xqc-twitch"
 * @property {string} url
 * @property {"twitch"|"kick"|"youtube"|"other"} platform  ADDITION
 * @property {boolean} topic_only  only record while the title says GTA 6
 * @property {boolean} paused
 * @property {number} priority     lower = recorded first when max_recorders is reached
 * @property {number} created_at
 * @typedef {StreamBase & LiveStatus} Stream  ADDITION: live status fields (null = unknown)
 */

/** GET /api/streams?profile= @param {{profile?: string}} [params] @returns {Promise<{items: Stream[], total: number}>} */
export const listStreams = (params) => request('GET', '/streams', { params });

/**
 * POST /api/streams — ref "twitch:name" | "kick:name" | "youtube:@name" | stream URL; invalid → 422 `fields.ref`.
 * @param {{profile: string, ref: string, topic_only?: boolean}} body
 * @returns {Promise<Stream>}
 */
export const addStream = (body) => request('POST', '/streams', { body });

/**
 * PATCH /api/streams/{id}
 * @param {number} id
 * @param {{paused?: boolean, priority?: number, topic_only?: boolean}} body
 * @returns {Promise<Stream>}
 */
export const updateStream = (id, body) => request('PATCH', `/streams/${id}`, { body });

/** DELETE /api/streams/{id} @returns {Promise<Ok>} */
export const deleteStream = (id) => request('DELETE', `/streams/${id}`);

// ---------------------------------------------------------------------------------------------------------------
// Settings, watchlist + profile config, research, assets
// ---------------------------------------------------------------------------------------------------------------

/**
 * Schema of one setting (the generic forms render from this, so new settings need no frontend change).
 * @typedef {Object} SettingSchema
 * @property {"boolean"|"integer"|"number"|"string"|"text"|"choice"|"string_list"|"time_list"|"json"} type
 *   text = multi-line string; string_list = list of strings (tags; `choices` become suggestions);
 *   time_list = list of "HH:MM"; json = any JSON value (edited as JSON text)
 * @property {string} label       German
 * @property {string} [help]      German, one or two sentences
 * @property {string} group       page section: "automation" (Zeitplan & Automatik), "clips", "approval",
 *   "claude", or any other id (shown under "Weitere"). Suggested mapping of spec §3.2:
 *   automation: automation.*, schedule.*, worker.concurrency · clips: prefs.*, render.*, dub.auto_languages ·
 *   approval: approval.mode · claude: claude.model/effort/max_turns/max_fix_rounds/daily_limit_eur/usd_to_eur/prices
 * @property {Array<{value: any, label: string, help?: string}>} [choices]  for "choice" (and suggestions)
 * @property {number} [min]
 * @property {number} [max]
 * @property {number} [step]
 * @property {boolean} [auto]     the value may also be the string "auto" (prefs.videos_per_run, hooks_per_video)
 * @property {string} [unit]      shown after the input, e.g. "€", "Min."
 * @property {boolean} [hidden]   not shown in the generic forms (edited on a dedicated page: watchlist.overrides,
 *   research.override, claude.extra_instructions, ui.*)
 * @property {boolean} [advanced] shown collapsed under "Erweitert" (e.g. claude.prices)
 * @property {any} default
 */

/**
 * GET /api/settings — `schema` keys in display order.
 * @returns {Promise<{values: Object<string, any>, schema: Object<string, SettingSchema>}>}
 */
export const getSettings = () => request('GET', '/settings');

/**
 * PATCH /api/settings — partial update; invalid → 422 with `fields: {key: message}` and nothing saved.
 * @param {Object<string, any>} values
 * @returns {Promise<{values: Object<string, any>}>} all values after the update
 */
export const updateSettings = (values) => request('PATCH', '/settings', { body: values });

/**
 * Base = the repo YAML, overrides = what the dashboard changed (DB), effective = base merged with overrides
 * (dict values merge one level per key, everything else replaces — as clipper.config.load).
 * @typedef {{base: Object, overrides: Object, effective: Object}} ConfigDoc
 */

/** GET /api/settings/watchlist @returns {Promise<ConfigDoc>} */
export const getWatchlist = () => request('GET', '/settings/watchlist');

/**
 * PUT /api/settings/watchlist — replaces all overrides (`{}` = back to the repo defaults). Invalid → 422 with
 * `fields` keyed by dotted path ("weights.loudness").
 * @param {{overrides: Object}} body
 * @returns {Promise<ConfigDoc>}
 */
export const putWatchlist = (body) => request('PUT', '/settings/watchlist', { body });

/** GET /api/profiles/{name}/config @returns {Promise<ConfigDoc>} */
export const getProfileConfig = (name) => request('GET', `/profiles/${encodeURIComponent(name)}/config`);

/**
 * PUT /api/profiles/{name}/config — replaces the profile's overrides (`{}` = repo profile.yaml). Bad regex in
 * topic_patterns → 422 `fields["topic_patterns.2"]`.
 * @param {string} name
 * @param {{overrides: Object}} body
 * @returns {Promise<ConfigDoc>}
 */
export const putProfileConfig = (name, body) =>
  request('PUT', `/profiles/${encodeURIComponent(name)}/config`, { body });

/**
 * @typedef {Object} Research
 * @property {string} playbook             research/viral-gta-shorts.md (Markdown, read-only)
 * @property {string|null} override        replaces the playbook in the system prompt when set
 * @property {string} extra_instructions   appended after the cached research block
 * @property {number} system_prompt_chars  length of the system prompt Claude gets now
 */

/** GET /api/research @returns {Promise<Research>} */
export const getResearch = () => request('GET', '/research');

/**
 * PUT /api/research — `override: null` goes back to the playbook.
 * @param {{override?: string|null, extra_instructions?: string}} body
 * @returns {Promise<Research>}
 */
export const putResearch = (body) => request('PUT', '/research', { body });

/**
 * @typedef {Object} MusicTrack
 * @property {string} id
 * @property {string} file
 * @property {"music"|"hit"} kind
 * @property {string} mood       suspense | sneaky | epic | sad | funny | any
 * @property {boolean} claimable can get a Content-ID claim
 * @property {boolean} approved
 * @property {boolean} usable    approved and file present (and not blocked as claimable)
 * @property {boolean} local     defined in assets/music/local/library.yaml
 */

/**
 * GET /api/assets
 * @returns {Promise<{fonts: Array<{name: string, file: string, license?: string|null}>, music: MusicTrack[]}>}
 */
export const getAssets = () => request('GET', '/assets');

/** PATCH /api/assets/music/{id} @param {string} id @param {{approved: boolean}} body @returns {Promise<MusicTrack>} */
export const updateMusic = (id, body) => request('PATCH', `/assets/music/${encodeURIComponent(id)}`, { body });

// ---------------------------------------------------------------------------------------------------------------
// Costs and API keys
// ---------------------------------------------------------------------------------------------------------------

/**
 * @typedef {Object} Usage  one Claude API request
 * @property {number} id
 * @property {number} ts
 * @property {string} day
 * @property {number|null} job_id
 * @property {"select"|"fix"|"dub_translate"|"test"|string} purpose
 * @property {string} model
 * @property {number} input_tokens
 * @property {number} output_tokens
 * @property {number} cache_creation_tokens
 * @property {number} cache_read_tokens
 * @property {number} cost_usd
 * @property {number} cost_eur
 * @property {string|null} request_id
 */

/**
 * @typedef {Object} CostsReport
 * @property {{eur: number, usd: number, requests: number, input_tokens: number, output_tokens: number,
 *   cache_read_tokens: number, cache_creation_tokens: number}} today
 * @property {number} limit_eur   claude.daily_limit_eur (0 = no limit)
 * @property {number} usd_to_eur  claude.usd_to_eur
 * @property {Array<{day: string, eur: number, usd: number, requests: number, tokens: number}>} days
 *   one entry per local day of the window, oldest first, days without requests included with zeros
 * @property {Array<{purpose: string, eur: number, usd: number, requests: number, tokens: number}>} by_purpose
 *   over the same window, most expensive first
 * @property {Usage[]} recent     newest first, about 50
 */

/** GET /api/costs?days=30 @param {{days?: number}} [params] @returns {Promise<CostsReport>} */
export const getCosts = (params = { days: 30 }) => request('GET', '/costs', { params });

/**
 * @typedef {Object} KeyInfo  never contains the secret itself
 * @property {"anthropic"|"fish"} name
 * @property {string} label       "Anthropic (Claude API)", "Fish Audio"
 * @property {boolean} set
 * @property {string|null} masked first 4 + "…" + last 4
 * @property {number|null} updated_at
 * @property {{ok: boolean, message: string, at: number}|null} last_test
 */

/** GET /api/keys @returns {Promise<{items: KeyInfo[], total: number}>} */
export const listKeys = () => request('GET', '/keys');

/**
 * PUT /api/keys/{name} — stores the key (not tested). Obviously malformed → 422 `fields.value`.
 * @param {"anthropic"|"fish"} name
 * @param {{value: string}} body
 * @returns {Promise<KeyInfo>}
 */
export const setKey = (name, body) => request('PUT', `/keys/${name}`, { body });

/** DELETE /api/keys/{name} @returns {Promise<Ok>} */
export const deleteKey = (name) => request('DELETE', `/keys/${name}`);

/**
 * POST /api/keys/{name}/test — tests the stored key with a cheap request; result is also kept as `last_test`.
 * No key stored → 409 `key_missing`.
 * @returns {Promise<{ok: boolean, message: string}>}  message German
 */
export const testKey = (name) => request('POST', `/keys/${name}/test`);

// ---------------------------------------------------------------------------------------------------------------
// Social media accounts, YouTube
// ---------------------------------------------------------------------------------------------------------------

/**
 * @typedef {Object} Account  credentials (OAuth tokens) are never returned
 * @property {number} id
 * @property {Platform} platform
 * @property {string} label
 * @property {string|null} handle
 * @property {string|null} profile    which clipper profile posts here (null = any)
 * @property {boolean} connected      YouTube: OAuth tokens present and valid; other platforms: always false
 * @property {string|null} external_id  YouTube channel id
 * @property {string|null} notes
 */

/** GET /api/social/accounts @returns {Promise<{items: Account[], total: number}>} */
export const listAccounts = () => request('GET', '/social/accounts');

/**
 * POST /api/social/accounts
 * @param {{platform: Platform, label: string, handle?: string, profile?: string|null, notes?: string}} body
 * @returns {Promise<Account>}
 */
export const createAccount = (body) => request('POST', '/social/accounts', { body });

/**
 * PATCH /api/social/accounts/{id}
 * @param {number} id
 * @param {{label?: string, handle?: string, profile?: string|null, notes?: string}} body
 * @returns {Promise<Account>}
 */
export const updateAccount = (id, body) => request('PATCH', `/social/accounts/${id}`, { body });

/** DELETE /api/social/accounts/{id} — also forgets its tokens. @returns {Promise<Ok>} */
export const deleteAccount = (id) => request('DELETE', `/social/accounts/${id}`);

/**
 * @typedef {Object} YoutubeApp
 * @property {boolean} client_id_set
 * @property {boolean} client_secret_set
 * @property {string|null} [client_id_hint]  ADDITION: masked client id
 * @property {string} redirect_uri  exactly what goes into the Google OAuth client ("http://localhost:8765/api/social/youtube/callback")
 * @property {string} [redirect_note]  German: where Google sends the browser back and what that means (PC vs. phone)
 */

/** GET /api/social/youtube/app @returns {Promise<YoutubeApp>} */
export const getYoutubeApp = () => request('GET', '/social/youtube/app');

/**
 * PUT /api/social/youtube/app — send one or both; `null` clears that value.
 * @param {{client_id?: string|null, client_secret?: string|null}} body
 * @returns {Promise<YoutubeApp>}
 */
export const putYoutubeApp = (body) => request('PUT', '/social/youtube/app', { body });

/**
 * POST /api/social/youtube/connect — start OAuth; the dashboard navigates to `auth_url` in the same tab. Without
 * `account_id` the callback creates a YouTube account from the channel it got access to. Client not configured →
 * 409 `youtube_app_missing`.
 * @param {{account_id?: number}} [body]
 * @returns {Promise<{auth_url: string}>}
 */
export const youtubeConnect = (body = {}) => request('POST', '/social/youtube/connect', { body });

/** POST /api/social/accounts/{id}/disconnect — revokes/forgets tokens. @returns {Promise<Account>} */
export const disconnectAccount = (id) => request('POST', `/social/accounts/${id}/disconnect`);

// ---------------------------------------------------------------------------------------------------------------
// System
// ---------------------------------------------------------------------------------------------------------------

/**
 * @typedef {Object} ToolCheck
 * @property {string} name        "ffmpeg", "yt-dlp", "whisper.cpp", "parakeet", "demucs", "whisperkit", …
 * @property {boolean} ok
 * @property {string|null} [version]
 * @property {string|null} [hint] German install hint ("sudo apt install ffmpeg")
 * @property {boolean} [required] ADDITION: false = optional (e.g. WhisperKit off macOS)
 * @property {string} [purpose]   ADDITION: German, what it is used for
 */

/**
 * @typedef {Object} SystemReport
 * @property {string} version
 * @property {string} python
 * @property {string} platform
 * @property {Object<string,string>} paths  e.g. {root, data, database, jobs, exports, logs}
 * @property {{path: string, free_gb: number, total_gb: number, min_free_gb: number}} disk
 * @property {ToolCheck[]} tools
 * @property {string|null} setup_report    plain text of `clipper setup`
 */

/** GET /api/system @returns {Promise<SystemReport>} */
export const getSystem = () => request('GET', '/system');
