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
/** waiting_approval at stage render: every pick is decided, the approved ones are rendering */
export const JOB_STATUS_RENDERING = { label: 'Clips werden gerendert', tone: 'running' };

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

// ---------------------------------------------------------------------------------------------------------------
// Captions, hook banner and sound (automate/style.py mirrors the renderer's vocabulary; these are its German words)
// ---------------------------------------------------------------------------------------------------------------

/** [{value, label, help}] per caption/hook/sound key, in the order the controls show them. */
export const STYLE_CHOICES = {
  caption_position: [
    { value: 'auto', label: 'Automatisch', help: 'Pro Clip aus einigen Bildern gewählt: dort, wo am wenigsten Spiel verdeckt wird – nie über Gesicht, App-Buttons oder Bildrand.' },
    { value: 'high', label: 'Oben', help: 'Facecam-Clips: oben im Spielbild direkt unter der Kamera. Vollbild-Clips: über dem Spielbild auf der Unschärfe.' },
    { value: 'seam', label: 'Naht', help: 'Facecam-Clips: genau zwischen Kamera und Spiel (wie bei Kai Cenat). Sitzt dort der Hook, rutschen die Untertitel nach oben ins Spielbild.' },
    { value: 'low', label: 'Unten', help: 'Facecam-Clips: unten im Spielbild über der App-Leiste. Vollbild-Clips: unter dem Spielbild auf der Unschärfe.' },
    { value: 'classic', label: 'Klassisch', help: 'Die alte feste Stelle: mitten im Spielbild (Facecam-Clips) bzw. unter dem Bild.' },
  ],
  caption_size: [
    { value: 'auto', label: 'Automatisch', help: 'Klein, wo die Untertitel auf dem Spiel liegen; mittel auf der Unschärfe oder der Naht.' },
    { value: 'small', label: 'Klein', help: 'Verdeckt am wenigsten.' },
    { value: 'medium', label: 'Mittel', help: 'Etwas größer, gut lesbar auf dem Handy.' },
    { value: 'large', label: 'Groß', help: 'Die alte Größe – nur über ruhigen Bildern.' },
  ],
  caption_look: [
    { value: 'auto', label: 'Automatisch', help: 'Jeder Clip bekommt einen Look aus der Familie seines Untertitel-Stils (derselbe Clip immer denselben), damit nicht jedes Video gleich aussieht.' },
    { value: 'classic', label: 'Klassisch', help: 'Immer der Grundlook des Untertitel-Stils, wie früher.' },
  ],
  hook_size: [
    { value: 'auto', label: 'Automatisch', help: 'Groß über dem Spielbild bei Vollbild-Clips, klein auf der Naht bei Facecam-Clips.' },
    { value: 'small', label: 'Klein', help: 'Verdeckt am wenigsten von Gesicht und Spiel.' },
    { value: 'medium', label: 'Mittel', help: 'Ein Kompromiss.' },
    { value: 'large', label: 'Groß', help: 'Die alte Größe.' },
  ],
  hook_seconds: [
    { value: 'auto', label: 'Automatisch', help: 'Vollbild: ganzer Clip. Facecam-Clips: Reaktion 3 s, Gameplay 4 s, Comedy und Szenario 5 s, News und Showcase ganzer Clip (dort ist der Hook die Titelkarte).' },
    { value: 'whole', label: 'Ganzer Clip', help: 'Der Hook bleibt bis zum Ende stehen.' },
    { value: 'seconds', label: 'Feste Sekunden', help: 'Der Hook steht so viele Sekunden und blendet dann kurz aus.' },
  ],
  sound_level: [
    { value: 'off', label: 'Aus', help: 'Keine automatischen Effekte. Eigene Effekte und Musik eines Clips spielen trotzdem.' },
    { value: 'subtle', label: 'Dezent', help: 'Ein Einschlag (Impact) auf dem stärksten Moment, kein Aufbau.' },
    { value: 'normal', label: 'Normal', help: 'Bis zu 2 Momente: ein Riser baut in den Hauptmoment auf, dazu ein Impact; vor einem stillen Aufbau ein leises Spannungs-Bett.' },
    { value: 'strong', label: 'Stark', help: 'Bis zu 3 Momente, längere Riser, alles etwas lauter.' },
  ],
};
export const STYLE_LABELS = {
  caption_position: 'Untertitel-Position', caption_size: 'Untertitel-Größe', caption_look: 'Untertitel-Look',
  hook_size: 'Hook-Größe', hook_seconds: 'Hook-Anzeigedauer', sound_level: 'Sound-Stärke',
};
/** Caption looks per family (clipper.captions.LOOKS / LOOK_DE). */
export const LOOKS = {
  bold: { family: 'bold', label: 'Montserrat, 3 Wörter, Gelb' },
  'bold-word': { family: 'bold', label: 'Montserrat, ein Wort' },
  'bold-lime': { family: 'bold', label: 'Montserrat, 2 Wörter, Lime' },
  'bold-clean': { family: 'bold', label: 'TikTok Sans, normale Schreibung, Lila' },
  boxed: { family: 'boxed', label: 'Bebas auf Schwarz, 3 Wörter, Gelb' },
  'boxed-word': { family: 'boxed', label: 'Bebas auf Schwarz, ein Wort' },
  'boxed-lime': { family: 'boxed', label: 'Bebas auf Schwarz, 2 Wörter, Lime' },
  comic: { family: 'comic', label: 'Comic, 3 Wörter, Gelb' },
  'comic-word': { family: 'comic', label: 'Comic, ein Wort' },
  'comic-orange': { family: 'comic', label: 'Comic, 2 Wörter, Orange' },
};
/** What brings back the look from before captions adapted (style.CLASSIC). */
export const CLASSIC_STYLE = {
  caption_position: 'classic', caption_size: 'large', caption_look: 'classic', hook_size: 'large', hook_seconds: 'whole',
  sound_level: 'off',
};
export const styleValueLabel = (key, v) => {
  if (key === 'caption_look' && LOOKS[v]) return LOOKS[v].label;
  if (key === 'hook_seconds' && typeof v === 'number') return `${String(v).replace('.', ',')} s`;
  const c = (STYLE_CHOICES[key] || []).find((x) => x.value === v);
  return c ? c.label : v;
};

export const SOUND_ROLES = {
  impact: { label: 'Impact', help: 'Einschlag: beginnt genau auf dem Moment.' },
  riser: { label: 'Riser', help: 'Baut auf und endet genau auf dem Moment (startet bis zu 3 s vorher).' },
  whoosh: { label: 'Whoosh', help: 'Kurzes Rauschen für einen harten Schnitt.' },
  tension: { label: 'Spannung', help: 'Herzschlag-Bett, das zum Moment hin läuft.' },
};
export const MOODS = { suspense: 'Spannung', sneaky: 'Schleichen', epic: 'Episch', sad: 'Traurig', funny: 'Lustig' };
export const FX_TYPES = { hit: 'Sound', flash: 'Blitz', zoom: 'Zoom' };
export const FLASH_COLORS = { white: 'Weiß', red: 'Rot', orange: 'Orange', lime: 'Lime' };

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

const FAIL_KEYS = new Set(['variety', 'length', 'labels', 'fx', 'footage', 'style']);
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
  style: 'Untertitel, Hook & Sound',
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
