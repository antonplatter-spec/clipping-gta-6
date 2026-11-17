// German labels and semantic tones for the backend's identifiers, plus helpers that read clipper reports.
// Tones follow the whiteboard: orange = Claude API/costs, yellow = decision/needs approval, green = done/data,
// grey = running code, red = error.

export const JOB_STATUS = {
  queued: { label: 'In Warteschlange', tone: 'neutral' },
  running: { label: 'Läuft', tone: 'running' },
  waiting_approval: { label: 'Wartet auf Freigabe', tone: 'yellow' },
  waiting_budget: { label: 'Wartet auf Budget', tone: 'orange' },
  waiting_key: { label: 'Wartet auf API-Schlüssel', tone: 'orange' },
  done: { label: 'Fertig', tone: 'green' },
  failed: { label: 'Fehler', tone: 'red' },
  skipped: { label: 'Übersprungen', tone: 'neutral' },
  canceled: { label: 'Abgebrochen', tone: 'muted' },
};

export const JOB_GROUPS = [
  { id: '', label: 'Alle' },
  { id: 'waiting', label: 'Wartet' },
  { id: 'running', label: 'Läuft' },
  { id: 'done', label: 'Fertig' },
  { id: 'failed', label: 'Fehler' },
  { id: 'canceled', label: 'Abgebrochen' },
];

export const JOB_KIND = {
  source: 'Quellvideo',
  live_chunk: 'Live-Chunk',
  render: 'Render',
  dub: 'Dub',
  upload: 'Upload',
};

export const JOB_STAGE = {
  fetch: '① Laden',
  analyze: '② Analyse',
  select: '③ Claude wählt',
  gate: '④ Qualitäts-Gate',
  approval: 'Freigabe',
  render: '⑤ Render',
  dub: '⑥ Dub',
  upload: 'Upload',
  finished: 'Abgeschlossen',
};

export const PICK_STATUS = {
  proposed: { label: 'Offen', tone: 'yellow' },
  approved: { label: 'Freigegeben', tone: 'neutral' },
  rendering: { label: 'Wird gerendert', tone: 'running' },
  rendered: { label: 'Gerendert', tone: 'green' },
  rejected: { label: 'Abgelehnt', tone: 'muted' },
  failed: { label: 'Fehlgeschlagen', tone: 'red' },
};

export const CLIP_TYPES = {
  reaction: 'Reaktion',
  comedy: 'Comedy',
  news: 'News',
  gameplay: 'Gameplay',
  showcase: 'Showcase',
  scenario: 'Szenario',
};

export const HOOK_STYLES = { bold: 'Fett (Montserrat)', box: 'Meme-Box', label: 'Label', none: 'Ohne Hook-Text' };
export const CAPTION_STYLES = { boxed: 'Box (Bebas)', bold: 'Fett', comic: 'Comic' };

/** clipper hook structure → German label (whiteboard table ④). */
export const HOOK_TYPES = {
  'number promise': 'Zahlen-Hook',
  'shock fact': 'Schock-Fakt',
  'relatable situation': 'Relatable',
  'withheld / negation': 'Vorenthalten/Negation',
  question: 'Frage',
  tease: 'Teaser',
  superlative: 'Superlativ',
  'premise + twist': 'Prämisse + Twist',
  'first-person claim': 'Ich-Aussage',
  'names a streamer': '+ Streamer-Name',
  vague: 'Vage (blockiert)',
  'all caps': 'Alles in Großbuchstaben',
  'announcement wording': 'Ankündigung',
  none: 'Ohne Hook (A/B-Test)',
};
export const hookTypeLabel = (t) => (t ? HOOK_TYPES[t] || t : null);

export const PLATFORMS = {
  youtube: 'YouTube',
  tiktok: 'TikTok',
  instagram: 'Instagram',
  x: 'X',
  facebook: 'Facebook',
  other: 'Andere',
};

export const PRIVACY = { private: 'Privat', unlisted: 'Nicht gelistet', public: 'Öffentlich' };

export const LANGUAGES = {
  en: 'Englisch', de: 'Deutsch', es: 'Spanisch', ru: 'Russisch', fr: 'Französisch', pt: 'Portugiesisch',
  it: 'Italienisch', pl: 'Polnisch', tr: 'Türkisch', nl: 'Niederländisch', ar: 'Arabisch', hi: 'Hindi',
  ja: 'Japanisch', ko: 'Koreanisch', zh: 'Chinesisch',
};
export const langLabel = (code) => LANGUAGES[code] || (code ? code.toUpperCase() : '–');

export const USAGE_PURPOSE = {
  select: 'Momente wählen ③',
  fix: 'Korrekturrunden ④',
  dub_translate: 'Dub-Übersetzung ⑥',
  test: 'Schlüsseltest',
};

export const STREAM_PLATFORMS = { twitch: 'Twitch', kick: 'Kick', youtube: 'YouTube', other: 'Stream' };

export const ALERT_LEVEL = {
  error: { label: 'Fehler', tone: 'red', icon: 'alert-circle' },
  warning: { label: 'Warnung', tone: 'yellow', icon: 'alert' },
  info: { label: 'Hinweis', tone: 'neutral', icon: 'info' },
};

/** What each tool is for, when the backend does not say (`purpose`). */
export const TOOL_PURPOSE = {
  ffmpeg: 'Audio/Video schneiden und rendern',
  ffprobe: 'Videos auslesen',
  'yt-dlp': 'Videos und Live-Streams laden',
  'whisper.cpp': 'Transkription (whisper.cpp)',
  'whisper-cli': 'Transkription (whisper.cpp)',
  parakeet: 'Transkription (Parakeet)',
  'onnx-asr': 'Transkription (Parakeet)',
  demucs: 'Stimmen von Spielton trennen',
  whisperkit: 'Sprecher erkennen (nur macOS)',
  'whisperkit-cli': 'Sprecher erkennen (nur macOS)',
  pillow: 'Kontaktbögen und Bilder',
  say: 'System-Stimme für Voiceover (macOS)',
};

// ---------------------------------------------------------------------------------------------------------------
// clipper check / simulate reports
// ---------------------------------------------------------------------------------------------------------------

const FAIL_KEYS = new Set(['variety', 'length', 'labels', 'fx', 'footage']);
export const CHECK_SECTIONS = {
  pairing: 'Hook/Titel-Paar',
  playbook: 'Playbook',
  clip_type: 'Clip-Typ',
  length: 'Länge',
  labels: 'Labels',
  fx: 'Effekte',
  edit: 'Schnitt',
  variety: 'Hook-Vielfalt',
  footage: 'Footage schon verwendet',
};

const asList = (v) => {
  if (v == null || v === '') return [];
  if (Array.isArray(v)) return v.flatMap(asList);
  if (typeof v === 'object') return Object.values(v).flatMap(asList);
  return [String(v)];
};

/**
 * Normalises a CheckReport (also tolerates a full {"video 1": {...}} report) into
 * {fails, notes, hooks: {text: {fails, notes}}, titles: {...}, failCount}. fails/notes: [{section, label, text}].
 */
export function readCheck(report) {
  const out = { fails: [], notes: [], hooks: {}, titles: {}, failCount: 0 };
  if (!report || typeof report !== 'object') return out;
  let entry = report;
  const keys = Object.keys(report);
  if (keys.length && keys.every((k) => /^video \d+$/.test(k))) entry = report[keys[0]] || {};
  for (const [key, value] of Object.entries(entry)) {
    if (key === 'hooks' || key === 'titles') {
      for (const [text, r] of Object.entries(value || {})) {
        const f = asList(r && r.fails);
        const n = asList(r && r.notes);
        out[key][text] = { fails: f, notes: n };
        out.failCount += f.length;
      }
      continue;
    }
    const target = FAIL_KEYS.has(key) ? out.fails : out.notes;
    for (const text of asList(value)) target.push({ section: key, label: CHECK_SECTIONS[key] || key, text });
  }
  out.failCount += out.fails.length;
  return out;
}

/** Find the simulate ranking row for a hook (exact text, then without *markup* and case). */
export function rankingFor(simulate, hook) {
  const rows = (simulate && simulate.ranking) || [];
  const norm = (s) => String(s || '').replace(/\*/g, '').trim().toLowerCase();
  return rows.find((r) => r.hook === hook) || rows.find((r) => norm(r.hook) === norm(hook)) || null;
}

/** Strongest structure of a ranking row: `type`, else the first non-modifier feature. */
export function rowType(row) {
  if (!row) return null;
  if (row.type) return row.type;
  const mods = new Set(['names a streamer', 'vague', 'all caps', 'announcement wording']);
  return (row.why || []).find((w) => !mods.has(w)) || null;
}

export function factorTone(x) {
  if (x == null) return 'muted';
  if (x >= 1.4) return 'green';
  if (x >= 1.0) return 'neutral';
  return 'red';
}
