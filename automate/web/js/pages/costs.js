// Kosten & Schlüssel: Claude spend today vs. daily limit, history per day (SVG bars), by purpose, recent requests,
// and the API keys (Anthropic, Fish Audio).
import * as api from '../api.js';
import { html, useState, useEffect, useRef, useData, useTitle, fmt, cx, toast, setQuery } from '../lib.js';
import {
  PageHeader, Card, Button, Loading, ErrorState, Empty, TextField, Segmented, Meter, Icon, FormError, Rel,
} from '../ui.js';
import { KeyEditor } from '../parts.js';
import { USAGE_PURPOSE } from '../labels.js';
import { overview } from '../store.js';

export function CostsPage({ route }) {
  useTitle('Kosten & Schlüssel');
  const days = route.query.tage === '14' ? 14 : 30;
  const costs = useData(() => api.getCosts({ days }), [days], { interval: 30000 });
  const keys = useData(() => api.listKeys().then(api.itemsOf), []);
  const c = costs.data;
  const byName = Object.fromEntries((keys.data || []).map((k) => [k.name, k]));
  const updateKey = (k) => { keys.setData((list) => (list || []).map((x) => (x.name === k.name ? { ...x, ...k } : x))); overview.refresh(); };
  return html`<div class="page">
    <${PageHeader} title="Kosten & Schlüssel" subtitle="Was die Claude API kostet, wo die Grenze liegt, und die API-Schlüssel." />
    ${costs.error && !c ? html`<${ErrorState} error=${costs.error} onRetry=${costs.reload} />`
      : !c ? html`<div class="card"><${Loading} lines=${8} /></div>` : html`
      <${CostTiles} c=${c} />
      <div class="grid grid-main">
        <${Card} title="Verlauf pro Tag" icon="activity" subtitle="Claude-Kosten in Euro"
          actions=${html`<${Segmented} size="sm" label="Zeitraum" value=${String(days)} onChange=${(v) => setQuery({ tage: v === '30' ? '' : v })}
            options=${[{ value: '14', label: '14 Tage' }, { value: '30', label: '30 Tage' }]} />`}>
          <${DayChart} days=${c.days || []} limit=${c.limit_eur} />
        <//>
        <${LimitCard} c=${c} onSaved=${costs.reload} />
      </div>
      <div class="grid grid-2">
        <${ByPurpose} rows=${c.by_purpose || []} days=${days} />
        <${Recent} rows=${c.recent || []} />
      </div>`}
    <${Card} title="API-Schlüssel" icon="key-round" subtitle="Werden nur auf diesem PC gespeichert und nie angezeigt – nur maskiert.">
      ${keys.error && !keys.data ? html`<${ErrorState} error=${keys.error} onRetry=${keys.reload} />`
        : !keys.data ? html`<${Loading} lines=${6} />`
        : html`<div class="grid grid-2" style="gap:28px">
            <${KeyEditor} name="anthropic" info=${byName.anthropic} onChange=${updateKey} />
            <${KeyEditor} name="fish" info=${byName.fish} onChange=${updateKey} />
          </div>`}
    <//>
  </div>`;
}

function CostTiles({ c }) {
  const t = c.today || {};
  const total = (c.days || []).reduce((s, d) => s + (d.eur || 0), 0);
  const tokens = (t.input_tokens || 0) + (t.output_tokens || 0) + (t.cache_read_tokens || 0) + (t.cache_creation_tokens || 0);
  return html`<div class="tiles">
    <div class="tile tone-orange">
      <span class="tile-label"><${Icon} name="euro" size=${15} />Heute</span>
      <span class="tile-value">${fmt.eur(t.eur || 0)}</span>
      ${c.limit_eur > 0 ? html`<${Meter} value=${t.eur || 0} max=${c.limit_eur} tone="orange" label="Heute im Verhältnis zum Tageslimit" />
        <span class="tile-sub">${Math.round(((t.eur || 0) / c.limit_eur) * 100)} % von ${fmt.eur(c.limit_eur)} · ${fmt.usd(t.usd || 0)}</span>`
        : html`<span class="tile-sub">kein Tageslimit · ${fmt.usd(t.usd || 0)}</span>`}
    </div>
    <div class="tile"><span class="tile-label"><${Icon} name="send" size=${15} />Anfragen heute</span>
      <span class="tile-value">${fmt.num(t.requests || 0)}</span><span class="tile-sub">an die Claude API</span></div>
    <div class="tile"><span class="tile-label"><${Icon} name="hash" size=${15} />Tokens heute</span>
      <span class="tile-value">${fmt.compact(tokens)}</span>
      <span class="tile-sub">${fmt.compact(t.input_tokens || 0)} rein · ${fmt.compact(t.output_tokens || 0)} raus · ${fmt.compact(t.cache_read_tokens || 0)} aus dem Cache</span></div>
    <div class="tile"><span class="tile-label"><${Icon} name="calendar-clock" size=${15} />${(c.days || []).length} Tage gesamt</span>
      <span class="tile-value">${fmt.eur(total)}</span><span class="tile-sub">Ø ${fmt.eur(total / Math.max(1, (c.days || []).length))} pro Tag</span></div>
  </div>`;
}

// ---------------------------------------------------------------------------------------------------------------
// Bar chart: one series (EUR per day), so no legend; title names it. Bars ≤ 24px, 4px rounded tops, 2px gaps,
// hairline grid, a reference line for the daily limit, values on the latest and the highest bar only.
// Every value is also in the table view below the chart and in the per-bar tooltip (hover or keyboard focus).
// ---------------------------------------------------------------------------------------------------------------

function useWidth(ref) {
  const [w, setW] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    setW(el.clientWidth);
    if (!('ResizeObserver' in window)) return undefined;
    const ro = new ResizeObserver(() => setW(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return w;
}

function niceStep(max) {
  const raw = max / 4;
  const pow = 10 ** Math.floor(Math.log10(raw || 1));
  const n = raw / pow;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * pow;
}

function topRounded(x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + rr}Q${x},${y} ${x + rr},${y}H${x + w - rr}Q${x + w},${y} ${x + w},${y + rr}V${y + h}Z`;
}

function DayChart({ days, limit }) {
  const wrap = useRef(null);
  const width = useWidth(wrap);
  const [hover, setHover] = useState(null);
  if (!days.length) return html`<${Empty} icon="activity" title="Noch keine Kosten">Sobald Claude Momente auswählt, erscheinen die Kosten hier.<//>`;
  const H = 250;
  const pad = { top: 18, right: 8, bottom: 26, left: 46 };
  const plotW = Math.max(0, width - pad.left - pad.right);
  const plotH = H - pad.top - pad.bottom;
  const maxVal = Math.max(...days.map((d) => d.eur || 0), limit || 0, 0.01);
  const step = niceStep(maxVal);
  const top = Math.ceil(maxVal / step) * step;
  const ticks = Array.from({ length: Math.round(top / step) + 1 }, (_, i) => i * step);
  const band = plotW / days.length;
  const barW = Math.max(2, Math.min(24, band - 2));
  const y = (v) => pad.top + plotH - (v / top) * plotH;
  const lastIdx = days.length - 1;
  const maxIdx = days.reduce((m, d, i) => ((d.eur || 0) > (days[m].eur || 0) ? i : m), 0);
  const labelEvery = Math.max(1, Math.ceil(days.length / Math.max(1, Math.floor(plotW / 46))));
  const tipDay = hover != null ? days[hover] : null;
  // A value label that would sit on the limit line moves just above it.
  const valueLabelY = (ly) => (limit > 0 && Math.abs(ly - 4 - y(limit)) < 9 ? y(limit) - 6 : ly);
  return html`<div class="stack-sm">
    <div ref=${wrap} style="position:relative;min-height:${H}px">
      ${width > 0 && html`<svg class="chart" width=${width} height=${H} role="img"
        aria-label=${`Claude-Kosten pro Tag, letzte ${days.length} Tage. Höchster Tag ${fmt.dayShort(days[maxIdx].day)} mit ${fmt.eur(days[maxIdx].eur)}. Details in der Tabelle darunter.`}>
        ${ticks.map((t) => html`<g key=${t}>
          <line class="grid-line" x1=${pad.left} x2=${width - pad.right} y1=${y(t)} y2=${y(t)} />
          <text class="axis-label num" x=${pad.left - 8} y=${y(t) + 4} text-anchor="end">${fmt.num2(t).replace(/,00$/, '')} €</text>
        </g>`)}
        ${days.map((d, i) => {
          const v = d.eur || 0;
          const x = pad.left + i * band + (band - barW) / 2;
          const h = Math.max(v > 0 ? 2 : 0, (v / top) * plotH);
          return html`<g key=${d.day}>
            <rect class="bar-hit" x=${pad.left + i * band} y=${pad.top} width=${band} height=${plotH} tabindex="0" role="img"
              aria-label=${`${fmt.day(d.day)}: ${fmt.eur(v)}, ${fmt.plural(d.requests || 0, 'Anfrage', 'Anfragen')}`}
              onPointerEnter=${() => setHover(i)} onPointerLeave=${() => setHover(null)} onFocus=${() => setHover(i)} onBlur=${() => setHover(null)} />
            ${h > 0 ? html`<path class=${cx('bar', i === lastIdx && 'is-today', hover === i && 'is-hover')} d=${topRounded(x, pad.top + plotH - h, barW, h, 4)} />`
              : html`<rect class="bar is-zero" x=${x} y=${pad.top + plotH - 1} width=${barW} height="1" />`}
            ${(i === lastIdx || i === maxIdx) && v > 0 && html`<text class="axis-label num" x=${x + barW / 2} y=${valueLabelY(pad.top + plotH - h - 5)} text-anchor="middle"
              style="fill:var(--text-2);font-weight:600">${fmt.eur(v)}</text>`}
            ${(i % labelEvery === lastIdx % labelEvery) && html`<text class="axis-label" x=${x + barW / 2} y=${H - 8} text-anchor="middle">${i === lastIdx ? 'heute' : fmt.dayShort(d.day)}</text>`}
          </g>`;
        })}
        <line x1=${pad.left} x2=${width - pad.right} y1=${pad.top + plotH} y2=${pad.top + plotH} class="baseline" />
        ${limit > 0 && html`<g aria-hidden="true">
          <line class="limit-line" x1=${pad.left} x2=${width - pad.right} y1=${y(limit)} y2=${y(limit)} />
          <text class="limit-label" x=${pad.left + 6} y=${y(limit) - 5} text-anchor="start">Tageslimit ${fmt.eur(limit)}</text>
        </g>`}
      </svg>`}
      ${tipDay && html`<div class="chart-tooltip" role="status" style=${`left:${Math.min(Math.max(pad.left + hover * band + band / 2, 80), width - 80)}px;top:${Math.max(4, y(tipDay.eur || 0) - 62)}px`}>
        <strong>${fmt.eur(tipDay.eur || 0)}</strong>
        <span>${fmt.day(tipDay.day)}</span>
        <span>${fmt.plural(tipDay.requests || 0, 'Anfrage', 'Anfragen')} · ${fmt.compact(tipDay.tokens || 0)} Tokens</span>
        ${limit > 0 && (tipDay.eur || 0) >= limit && html`<span>Tageslimit erreicht</span>`}
      </div>`}
    </div>
    <details class="details"><summary><${Icon} name="chevron-right" size=${16} />Als Tabelle anzeigen</summary>
      <div class="details-body table-wrap"><table class="table table-compact">
        <thead><tr><th>Tag</th><th class="th-num">Euro</th><th class="th-num">US-Dollar</th><th class="th-num">Anfragen</th><th class="th-num">Tokens</th></tr></thead>
        <tbody>${[...days].reverse().map((d) => html`<tr key=${d.day}><td>${fmt.day(d.day)}</td><td class="td-num">${fmt.eur(d.eur || 0)}</td>
          <td class="td-num">${fmt.usd(d.usd || 0)}</td><td class="td-num">${fmt.num(d.requests || 0)}</td><td class="td-num">${fmt.num(d.tokens || 0)}</td></tr>`)}</tbody>
      </table></div></details>
  </div>`;
}

function LimitCard({ c, onSaved }) {
  const [limit, setLimit] = useState(String(c.limit_eur ?? ''));
  const [rate, setRate] = useState(String(c.usd_to_eur ?? ''));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => { setLimit(String(c.limit_eur ?? '')); setRate(String(c.usd_to_eur ?? '')); }, [c.limit_eur, c.usd_to_eur]);
  const dirty = Number(limit) !== c.limit_eur || Number(rate) !== c.usd_to_eur;
  const save = async (e) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      await api.updateSettings({ 'claude.daily_limit_eur': Number(limit), 'claude.usd_to_eur': Number(rate) });
      toast.success('Tageslimit und Wechselkurs gespeichert.');
      onSaved(); overview.refresh();
    } catch (err) { setError(err); } finally { setBusy(false); }
  };
  const f = (error && error.fields) || {};
  return html`<${Card} title="Tageslimit" icon="gauge" subtitle="Schützt vor Überraschungen auf der Rechnung.">
    <form class="stack" onSubmit=${save}>
      <${FormError} error=${error && !error.fields ? error : null} />
      <${TextField} label="Tageslimit in Euro" type="number" min="0" step="0.5" value=${limit} onInput=${setLimit} inputClass="input-num"
        error=${f['claude.daily_limit_eur']} help="0 = kein Limit. Ist es erreicht, warten Claude-Schritte bis morgen („Wartet auf Budget“); alles andere läuft weiter." />
      <${TextField} label="Wechselkurs: 1 US-Dollar in Euro" type="number" min="0.01" step="0.01" value=${rate} onInput=${setRate} inputClass="input-num"
        error=${f['claude.usd_to_eur']} help="Anthropic rechnet in US-Dollar ab. Der Kurs wird nur zur Anzeige und fürs Limit genutzt." />
      <div class="form-actions"><${Button} type="submit" variant="primary" busy=${busy} disabled=${!dirty || limit === '' || rate === ''}>Speichern<//></div>
    </form>
    <hr />
    <p class="muted small">Preise pro Modell (USD je 1 Mio. Tokens) stehen unter <a href="#/einstellungen/claude">Einstellungen → Claude</a>.</p>
  <//>`;
}

function ByPurpose({ rows, days }) {
  const total = rows.reduce((s, r) => s + (r.eur || 0), 0);
  return html`<${Card} title="Nach Zweck" icon="list-checks" subtitle=${`Letzte ${days} Tage`}>
    ${!rows.length ? html`<p class="muted">Noch keine Anfragen.</p>` : html`<div class="table-wrap"><table class="table">
      <thead><tr><th>Zweck</th><th>Anteil</th><th class="th-num">Euro</th><th class="th-num">Anfragen</th></tr></thead>
      <tbody>${rows.map((r) => html`<tr key=${r.purpose}>
        <td>${USAGE_PURPOSE[r.purpose] || r.purpose}</td>
        <td style="width:30%"><div class="share-bar" role="img" aria-label=${`${Math.round(((r.eur || 0) / (total || 1)) * 100)} Prozent`}>
          <span style=${`width:${((r.eur || 0) / (total || 1)) * 100}%`}></span></div></td>
        <td class="td-num">${fmt.eur(r.eur || 0)}</td><td class="td-num">${fmt.num(r.requests || 0)}</td>
      </tr>`)}</tbody></table></div>`}
  <//>`;
}

function Recent({ rows }) {
  return html`<${Card} title="Letzte Anfragen" icon="clock" class="card-flush">
    ${!rows.length ? html`<p class="muted" style="padding:0 18px 16px">Noch keine Anfragen.</p>` : html`<div class="table-wrap" style="max-height:380px;overflow:auto"><table class="table table-compact">
      <thead><tr><th>Zeit</th><th>Zweck</th><th class="th-num">Tokens rein/raus</th><th class="th-num">Cache</th><th class="th-num">Euro</th></tr></thead>
      <tbody>${rows.map((u) => html`<tr key=${u.id}>
        <td class="nowrap"><${Rel} ts=${u.ts} />${u.job_id && html`<div class="small"><a href=${`#/jobs?job=${u.job_id}`}>Job #${u.job_id}</a></div>`}</td>
        <td>${USAGE_PURPOSE[u.purpose] || u.purpose}<div class="muted small">${modelName(u.model)}</div></td>
        <td class="td-num">${fmt.compact(u.input_tokens)} / ${fmt.compact(u.output_tokens)}</td>
        <td class="td-num muted">${fmt.compact(u.cache_read_tokens || 0)}</td>
        <td class="td-num">${fmt.eur(u.cost_eur)}</td>
      </tr>`)}</tbody></table></div>`}
  <//>`;
}

const MODEL_NAMES = { 'claude-sonnet-5-5': 'Claude Sonnet 5.5', 'claude-opus-5-5': 'Claude Opus 5.5' };
export const modelName = (id) => MODEL_NAMES[id] || id || '–';
