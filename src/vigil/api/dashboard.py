"""The dashboard, served as one string.

Shows episode counts, the two detectors side by side, hot-path latency against its budget,
and the reconciliation panel that story G1 calls "Must". That panel was deliberately absent
until the Phase 2 harness existed, on the grounds that a panel reading "drift: 0" when
nothing is measuring drift is worse than no panel; it now reads from the harness's own
tables and still refuses to render numbers when no reconciliation run has happened. The
React dashboard is Phase 6.
"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vigil</title>
<style>
  :root {
    color-scheme: light dark;
    --page:           #f9f9f7;
    --surface:        #fcfcfb;
    --text-primary:   #0b0b0b;
    --text-secondary: #52514e;
    --text-muted:     #898781;
    --grid:           #e1e0d9;
    --baseline:       #c3c2b7;
    --border:         rgba(11,11,11,0.10);
    /* Six series, because the live chart draws six channels and alternating two of them
       makes a legend the only way to tell any line from another. Hues are spaced for
       deuteranopia and protanopia (an Okabe-Ito-style ordering: blue, orange, green,
       purple, teal, ochre) rather than picked by eye, and each is darkened for light mode
       and lightened for dark so contrast against the surface holds in both. */
    --series-1:       #2a78d6;
    --series-2:       #eb6834;
    --series-3:       #1a8a4f;
    --series-4:       #8552d6;
    --series-5:       #0a8f93;
    --series-6:       #9c6a12;
    --good:           #0ca30c;
    --warning:        #fab219;
    --critical:       #d03b3b;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --page:           #0d0d0d;
      --surface:        #1a1a19;
      --text-primary:   #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted:     #898781;
      --grid:           #2c2c2a;
      --baseline:       #383835;
      --border:         rgba(255,255,255,0.10);
      --series-1:       #3987e5;
      --series-2:       #d95926;
      --series-3:       #35b06a;
      --series-4:       #a37ae8;
      --series-5:       #23b2b6;
      --series-6:       #c9962f;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 32px 28px 64px;
    background: var(--page); color: var(--text-primary);
    font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  header { max-width: 1180px; margin: 0 auto 24px; }
  h1 { font-size: 20px; font-weight: 600; margin: 0 0 4px; letter-spacing: -0.01em; }
  .sub { color: var(--text-secondary); font-size: 13px; margin: 0; }
  main { max-width: 1180px; margin: 0 auto; display: grid; gap: 20px; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }
  .tile, .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 16px 18px;
  }
  .tile .label { color: var(--text-secondary); font-size: 12px; margin-bottom: 6px; }
  .tile .value { font-size: 30px; font-weight: 600; letter-spacing: -0.02em; }
  .tile .note { color: var(--text-muted); font-size: 12px; margin-top: 4px;
                font-variant-numeric: tabular-nums; }
  .card h2 { font-size: 13px; font-weight: 600; margin: 0 0 2px;
             text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-secondary); }
  .card .hint { color: var(--text-muted); font-size: 12px; margin: 0 0 14px; }
  .legend { display: flex; gap: 18px; flex-wrap: wrap; margin-bottom: 12px; }
  .legend span { display: inline-flex; align-items: center; gap: 7px;
                 color: var(--text-secondary); font-size: 12px; }
  .swatch { width: 10px; height: 10px; border-radius: 3px; flex: none; }
  .bars { display: grid; gap: 12px; }
  .bar-row { display: grid; grid-template-columns: 190px 1fr 90px; align-items: center; gap: 12px; }
  .bar-name { color: var(--text-secondary); font-size: 13px; }
  .bar-track { background: var(--grid); border-radius: 4px; height: 14px; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 4px; min-width: 3px; }
  .bar-value { text-align: right; font-variant-numeric: tabular-nums;
               color: var(--text-primary); font-size: 13px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; font-weight: 500; color: var(--text-muted); font-size: 12px;
       padding: 0 10px 8px 0; border-bottom: 1px solid var(--baseline); white-space: nowrap; }
  td { padding: 9px 10px 9px 0; border-bottom: 1px solid var(--grid);
       font-variant-numeric: tabular-nums; }
  td.chan { font-variant-numeric: normal; color: var(--text-primary); }
  .muted { color: var(--text-muted); font-variant-numeric: normal; }
  .status { display: inline-flex; align-items: center; gap: 6px; font-variant-numeric: normal; }
  .status .glyph { font-weight: 700; }
  .s-real      .glyph { color: var(--critical); }
  .s-attributed .glyph { color: var(--warning); }
  .s-suppressed .glyph { color: var(--text-muted); }
  .g-ok        .glyph { color: var(--good); }
  .g-info      .glyph { color: var(--text-muted); }
  .g-warning   .glyph { color: var(--warning); }
  .g-critical  .glyph { color: var(--critical); }
  .det { display: inline-flex; align-items: center; gap: 7px; font-variant-numeric: normal; }
  .budget-ok { color: var(--good); }
  .budget-over { color: var(--critical); }
  .empty { color: var(--text-muted); padding: 18px 0; }

  /* Live chart. Inline SVG rather than a charting library: the whole page is one string
     served by FastAPI, and pulling in a bundle to draw six polylines would be the heaviest
     dependency in the repository for the least of its work. */
  .chart-head { display: flex; align-items: baseline; justify-content: space-between;
                gap: 16px; flex-wrap: wrap; margin-bottom: 10px; }
  .chart-rate { font-size: 26px; font-weight: 600; letter-spacing: -0.02em;
                font-variant-numeric: tabular-nums; }
  .chart-rate .unit { font-size: 13px; font-weight: 400; color: var(--text-secondary);
                      margin-left: 5px; letter-spacing: 0; }
  .chart-wrap { position: relative; }
  svg.stream { width: 100%; height: 260px; display: block; overflow: visible; }
  svg.stream .grid { stroke: var(--grid); stroke-width: 1; }
  svg.stream .axis { stroke: var(--baseline); stroke-width: 1; }
  svg.stream .tick { fill: var(--text-muted); font-size: 10px; }
  svg.stream .line { fill: none; stroke-width: 1.4; vector-effect: non-scaling-stroke; }
  svg.stream .ep-band { fill: var(--critical); opacity: 0.10; }
  svg.stream .ep-rule { stroke: var(--critical); stroke-width: 1; stroke-dasharray: 3 3; }
  svg.stream .ep-dot { fill: var(--critical); stroke: var(--surface); stroke-width: 1.5; }
  svg.stream .ep-dot.attributed { fill: var(--text-muted); }
  .live-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%;
              background: var(--good); margin-right: 6px; vertical-align: middle; }
  .live-dot.stalled { background: var(--warning); }
  .marker-key { display: flex; gap: 18px; flex-wrap: wrap; color: var(--text-muted);
                font-size: 12px; margin-top: 10px; }
  .marker-key .k { display: inline-flex; align-items: center; gap: 6px; }
  .marker-key .dot { width: 8px; height: 8px; border-radius: 50%;
                     background: var(--critical); display: inline-block; }
  .marker-key .dot.attributed { background: var(--text-muted); }
  footer { max-width: 1180px; margin: 28px auto 0; color: var(--text-muted); font-size: 12px; }
  a { color: var(--series-1); }
</style>
</head>
<body>
<header>
  <h1>Vigil</h1>
  <p class="sub">Context-conditioned anomaly detection on a streaming backbone &middot;
     detection spine, reconciliation, conditioning</p>
</header>

<main>
  <section class="tiles" id="tiles"></section>

  <section class="card">
    <div class="chart-head">
      <div>
        <h2>Live stream</h2>
        <p class="hint" id="chart-hint">Readings arriving now, averaged to one point per
           second per channel. Each channel is scaled to its own range over the window --
           flow and vibration differ by a factor of thirty, so a shared axis would draw one
           line and flatten the rest.</p>
      </div>
      <div style="text-align:right">
        <div class="chart-rate" id="rate">&mdash;</div>
        <div class="hint" style="margin:0" id="rate-note">readings/s</div>
      </div>
    </div>
    <div class="chart-wrap">
      <svg class="stream" id="stream" role="img"
           aria-label="Live sensor readings with detected anomalies marked"></svg>
    </div>
    <div class="legend" id="chart-legend"></div>
    <div class="marker-key">
      <span class="k"><span class="dot"></span>episode raised</span>
      <span class="k"><span class="dot attributed"></span>attributed to a
         context event, not paged</span>
      <span class="k">shaded band spans the episode</span>
    </div>
  </section>

  <section class="card">
    <h2>Detectors</h2>
    <p class="hint">Episodes raised by each detector over the same stream. Scores are on each
       detector's own scale and are not comparable between rows; the matched-alarm-budget
       comparison lives in the benchmark.</p>
    <div class="legend" id="legend"></div>
    <div class="bars" id="bars"></div>
  </section>

  <section class="card">
    <h2>Reconciliation</h2>
    <p class="hint">Two counts derived independently: our own per-channel sequence ledger,
       and an audit of what the broker retained against what was consumed. Agreement between
       them is the only reason to believe either. Absent until a run has happened. Max lag is
       measured against the wall clock, so replaying a recorded topic honestly reports the
       age of the data; loss and lag are graded separately for exactly that reason.</p>
    <div id="reconciliation"><p class="empty">loading&hellip;</p></div>
  </section>

  <section class="card">
    <h2>Episodes</h2>
    <p class="hint">Consecutive flagged windows merged into one incident. "Injected" is
       ground truth from the synthetic source, shown for inspection; no detector reads it.</p>
    <div id="episodes"><p class="empty">loading&hellip;</p></div>
  </section>
</main>

<footer id="footer"></footer>

<script>
const fmt = n => n === null || n === undefined ? "\\u2014" : n.toLocaleString();
// Trailing window the live chart shows, how often it polls, and how long without a new
// event before the stream is called idle. Three polls of silence, so one slow write does
// not flip the indicator.
const STREAM_WINDOW_S = 120;
const STREAM_POLL_MS = 2000;
const STREAM_STALL_MS = 3 * STREAM_POLL_MS;
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

// Status is a glyph plus a word, never colour alone.
const STATUS = {
  real:        { glyph: "\\u25CF", label: "real" },
  attributed:  { glyph: "\\u25D0", label: "attributed" },
  suppressed:  { glyph: "\\u25CB", label: "suppressed" },
};

// Reconciliation grades, glyph plus word for the same reason as status.
const GRADE = {
  ok:       { glyph: "\\u25CB", label: "ok" },
  info:     { glyph: "\\u25CC", label: "info" },
  warning:  { glyph: "\\u25D0", label: "warning" },
  critical: { glyph: "\\u25CF", label: "critical" },
};

function tile(label, value, note) {
  const t = el("div", "tile");
  t.append(el("div", "label", label), el("div", "value", value));
  if (note) t.append(el("div", "note", note));
  return t;
}

async function load() {
  const [health, dets, eps, recon] = await Promise.all([
    fetch("/health").then(r => r.json()),
    fetch("/detectors").then(r => r.json()),
    fetch("/episodes?limit=60").then(r => r.json()),
    fetch("/reconciliation?windows=60").then(r => r.json()),
  ]);

  // --- KPI row ---
  const tiles = document.getElementById("tiles");
  tiles.replaceChildren();
  tiles.append(tile("Episodes recorded", fmt(health.episodes ?? 0)));

  const budget = dets.hot_path_budget_ms_p99;
  const hot = dets.detectors.find(d => d.detector === "zscore");
  if (hot && hot.latency_ms) {
    const p99 = hot.latency_ms.p99;
    const t = tile("Hot-path latency p99", p99.toFixed(2) + " ms",
                   `budget ${budget} ms \\u00b7 ${fmt(hot.latency_ms.n)} windows`);
    t.querySelector(".value").className =
      "value " + (p99 <= budget ? "budget-ok" : "budget-over");
    tiles.append(t);
  }
  const model = dets.detectors.find(d => d.detector !== "zscore");
  if (model && model.latency_ms) {
    tiles.append(tile("Model latency p99", model.latency_ms.p99.toFixed(2) + " ms",
                      `${model.detector} \\u00b7 off critical path`));
  }
  tiles.append(tile("Postgres", health.postgres,
                    `uptime ${Math.round(health.uptime_s)} s`));

  // --- detector bars: identity by swatch AND name, with the value labelled ---
  const legend = document.getElementById("legend");
  const bars = document.getElementById("bars");
  legend.replaceChildren();
  bars.replaceChildren();
  const max = Math.max(1, ...dets.detectors.map(d => d.episodes));
  dets.detectors.forEach((d, i) => {
    const colour = `var(--series-${(i % 2) + 1})`;
    const sw = el("span");
    const chip = el("span", "swatch");
    chip.style.background = colour;
    sw.append(chip, document.createTextNode(d.detector));
    legend.append(sw);

    const row = el("div", "bar-row");
    row.append(el("div", "bar-name", d.detector));
    const track = el("div", "bar-track");
    const fill = el("div", "bar-fill");
    fill.style.width = (100 * d.episodes / max) + "%";
    fill.style.background = colour;
    track.append(fill);
    row.append(track, el("div", "bar-value", fmt(d.episodes) + " ep"));
    row.title = `${d.detector}: ${d.episodes} episodes over ${d.windows} flagged windows,`
              + ` peak score max ${d.max_peak_score}`;
    bars.append(row);
  });
  if (!dets.detectors.length) bars.append(el("p", "empty", "no episodes yet"));

  // --- episode table ---
  const host = document.getElementById("episodes");
  host.replaceChildren();
  if (!eps.episodes.length) {
    host.append(el("p", "empty", "no episodes yet \\u2014 start loadgen.py and detector.py"));
  } else {
    const table = el("table");
    const head = el("tr");
    ["Channel", "Detector", "Peak", "Windows", "Duration", "Status", "Injected", "Seen"]
      .forEach(h => head.append(el("th", null, h)));
    const thead = el("thead");
    thead.append(head);
    table.append(thead);
    const body = el("tbody");
    for (const e of eps.episodes) {
      const tr = el("tr");
      tr.append(el("td", "chan", e.channel));

      const dcell = el("td");
      const dwrap = el("span", "det");
      const dchip = el("span", "swatch");
      const idx = dets.detectors.findIndex(d => d.detector === e.raised_by);
      dchip.style.background = `var(--series-${(Math.max(idx, 0) % 2) + 1})`;
      dwrap.append(dchip, document.createTextNode(e.raised_by));
      dcell.append(dwrap);
      tr.append(dcell);

      tr.append(el("td", null, e.peak_score.toFixed(1)));
      tr.append(el("td", null, String(e.window_count)));
      tr.append(el("td", null, e.duration_s + " s"));

      const s = STATUS[e.status] || { glyph: "?", label: e.status };
      const scell = el("td");
      const swrap = el("span", "status s-" + e.status);
      swrap.append(el("span", "glyph", s.glyph), document.createTextNode(s.label));
      scell.append(swrap);
      tr.append(scell);

      tr.append(el("td", "muted", e.injected_origins.length
        ? e.injected_origins.join(", ") : "\\u2014"));
      tr.append(el("td", "muted", new Date(e.created_at).toLocaleTimeString()));
      body.append(tr);
    }
    table.append(body);
    host.append(table);
  }

  // --- reconciliation ---
  // Absent evidence stays absent. Rendering zeros here would assert a clean pipeline that
  // nothing had measured, which is the one claim this panel exists to avoid making.
  const rec = document.getElementById("reconciliation");
  rec.replaceChildren();
  if (!recon.has_evidence) {
    rec.append(el("p", "empty",
      "no reconciliation run recorded — run reconciler.py, and note that it writes "
      + "nothing under --no-store"));
  } else {
    const run = recon.latest_run;
    const agree = run.broker_available === run.broker_consumed;
    const row = el("div", "tiles");
    const drift = tile("Ledger drift", fmt(run.drift),
                       `${fmt(run.readings)} readings · ${fmt(run.channels)} channels`);
    drift.querySelector(".value").className =
      "value " + (run.drift === 0 ? "budget-ok" : "budget-over");
    row.append(drift);
    const audit = tile("Broker offset drift", fmt(run.offset_drift),
                       `${fmt(run.broker_available)} retained · `
                       + `${fmt(run.broker_consumed)} consumed`);
    audit.querySelector(".value").className =
      "value " + (agree && run.offset_drift === 0 ? "budget-ok" : "budget-over");
    row.append(audit);
    row.append(tile("Missing / duplicate / reordered",
                    `${fmt(run.missing)} / ${fmt(run.duplicates)} / ${fmt(run.regressions)}`,
                    `over ${Math.round(run.duration_s)} s on ${run.topic}`));
    const disturbed = recon.disturbed_windows;
    const shown = Math.min(recon.windows.length, 12);
    row.append(tile("Disturbed windows", fmt(disturbed),
                    `${fmt(recon.windows.length)} windows read, ${shown} listed · `
                    + (disturbed ? "conditioning has something to explain with"
                                 : "every window graded ok")));
    rec.append(row);

    if (recon.windows.length) {
      const table = el("table");
      const head = el("tr");
      ["Window start", "Readings", "Missing", "Duplicates", "Reordered", "Max lag", "Grade"]
        .forEach(h => head.append(el("th", null, h)));
      const thead = el("thead");
      thead.append(head);
      table.append(thead);
      const body = el("tbody");
      for (const w of recon.windows.slice(0, shown)) {
        const tr = el("tr");
        tr.append(el("td", "muted", new Date(w.window_start_ms).toLocaleTimeString()));
        tr.append(el("td", null, fmt(w.readings)));
        tr.append(el("td", null, fmt(w.missing)));
        tr.append(el("td", null, fmt(w.duplicates)));
        tr.append(el("td", null, fmt(w.regressions)));
        tr.append(el("td", null, fmt(w.max_lag_ms) + " ms"));
        const g = GRADE[w.severity] || { glyph: "?", label: w.severity };
        const gcell = el("td");
        const gwrap = el("span", "status g-" + w.severity);
        gwrap.append(el("span", "glyph", g.glyph), document.createTextNode(g.label));
        gcell.append(gwrap);
        tr.append(gcell);
        body.append(tr);
      }
      table.append(body);
      rec.append(table);
    }
  }

  document.getElementById("footer").textContent =
    "Every panel reads stored data; nothing here is computed for display. Refreshed "
    + new Date().toLocaleTimeString() + ".";
}


// ---------------------------------------------------------------------------------------
// Live stream chart.
//
// Drawn as inline SVG with no charting library. The page is one string served by FastAPI,
// and a bundle to draw six polylines would be the heaviest dependency in the repository for
// the least of its work.
//
// Each channel is normalised to its own min/max over the visible window. A shared y-axis
// would be honest about magnitude and useless to look at: flow sits near 144 and vibration
// near 5, so one line would occupy the chart and the other five would be a flat smear along
// the bottom. The hint under the heading says so, because a normalised axis that does not
// announce itself is a way to mislead.
// ---------------------------------------------------------------------------------------
const SVG_NS = "http://www.w3.org/2000/svg";
const CHART = { w: 1100, h: 260, padL: 8, padR: 8, padT: 14, padB: 22 };

function svgEl(tag, attrs) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
}

function clockLabel(ms) {
  const d = new Date(ms);
  const pad = v => String(v).padStart(2, "0");
  return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
}

function drawStream(data) {
  const svg = document.getElementById("stream");
  svg.replaceChildren();
  svg.setAttribute("viewBox", `0 0 ${CHART.w} ${CHART.h}`);
  svg.setAttribute("preserveAspectRatio", "none");

  const legend = document.getElementById("chart-legend");
  legend.replaceChildren();

  const channels = data.channels || [];
  const hasPoints = channels.some(c => c.points.length > 1);
  if (!hasPoints) {
    // No data is reported as no data. A chart drawn from an empty window would be a flat
    // line at zero, which looks like a measured quiet stream rather than an absent one.
    svg.append(svgEl("rect", { x: 0, y: 0, width: CHART.w, height: CHART.h, fill: "none" }));
    const t = svgEl("text", { x: CHART.w / 2, y: CHART.h / 2, "text-anchor": "middle",
                              class: "tick" });
    t.textContent = "no readings in the serving store yet "
                  + "\u2014 start loadgen.py and warehouse.py";
    svg.append(t);
    return;
  }

  const t0 = data.from_ms, t1 = data.to_ms;
  const span = Math.max(1, t1 - t0);
  const plotW = CHART.w - CHART.padL - CHART.padR;
  const plotH = CHART.h - CHART.padT - CHART.padB;
  const x = ms => CHART.padL + plotW * ((ms - t0) / span);

  // Time gridlines, one every fifth of the window.
  for (let i = 0; i <= 5; i++) {
    const ms = t0 + (span * i) / 5;
    const px = x(ms);
    svg.append(svgEl("line", { x1: px, y1: CHART.padT, x2: px, y2: CHART.padT + plotH,
                               class: "grid" }));
    const label = svgEl("text", { x: px, y: CHART.h - 6, class: "tick",
                                  "text-anchor":
                                    i === 0 ? "start" : (i === 5 ? "end" : "middle") });
    label.textContent = clockLabel(ms);
    svg.append(label);
  }
  svg.append(svgEl("line", { x1: CHART.padL, y1: CHART.padT + plotH,
                             x2: CHART.padL + plotW, y2: CHART.padT + plotH, class: "axis" }));

  // Episode bands go behind the lines so a marker never hides the excursion it marks.
  const byChannel = new Map(channels.map((c, i) => [c.channel, i]));
  for (const ep of (data.episodes || [])) {
    const from = Math.max(ep.t_start_ms, t0), to = Math.min(ep.t_end_ms, t1);
    if (to < t0 || from > t1) continue;
    const bx = x(from), bw = Math.max(2, x(to) - bx);
    svg.append(svgEl("rect", { x: bx, y: CHART.padT, width: bw, height: plotH,
                               class: "ep-band" }));
    const onset = ep.onset_ms && ep.onset_ms >= t0 && ep.onset_ms <= t1 ? ep.onset_ms : from;
    svg.append(svgEl("line", { x1: x(onset), y1: CHART.padT, x2: x(onset),
                               y2: CHART.padT + plotH, class: "ep-rule" }));
  }

  // One band per channel, stacked, each normalised to its own range.
  const laneH = plotH / channels.length;
  channels.forEach((c, i) => {
    const colour = `var(--series-${(i % 6) + 1})`;
    const values = c.points.map(p => p.value);
    let lo = Math.min(...values), hi = Math.max(...values);
    // A dead-flat channel is a line, not a divide-by-zero.
    if (hi - lo < 1e-9) { lo -= 0.5; hi += 0.5; }
    const top = CHART.padT + i * laneH + 4;
    const h = laneH - 8;
    const y = v => top + h * (1 - (v - lo) / (hi - lo));

    const d = c.points.map((p, j) =>
      (j ? "L" : "M") + x(p.t_ms).toFixed(1) + " " + y(p.value).toFixed(1));
    const path = svgEl("path", { d: d.join(" "), class: "line", stroke: colour });
    svg.append(path);

    const chip = el("span", "swatch");
    chip.style.background = colour;
    const name = el("span");
    name.append(chip, document.createTextNode(
      `${c.channel} (${lo.toFixed(1)}\\u2013${hi.toFixed(1)})`));
    name.title = `${c.channel}: ${c.points.length} seconds plotted, `
               + `range ${lo.toFixed(3)} to ${hi.toFixed(3)} over this window`;
    legend.append(name);

    // Episode dots sit on this channel's own line, at the value the channel held then.
    for (const ep of (data.episodes || [])) {
      if (ep.channel !== c.channel) continue;
      // Clamp to the visible window rather than skipping. An episode that began before
      // the view still draws its band, and a band with no marker reads as a rendering bug
      // rather than as "this started earlier"; the tooltip carries the true onset time.
      const trueAt = ep.onset_ms || ep.t_start_ms;
      if (ep.t_end_ms < t0 || ep.t_start_ms > t1) continue;
      const at = Math.min(Math.max(trueAt, t0), t1);
      let nearest = c.points[0];
      for (const p of c.points) {
        if (Math.abs(p.t_ms - at) < Math.abs(nearest.t_ms - at)) nearest = p;
      }
      const dot = svgEl("circle", {
        cx: x(at).toFixed(1), cy: y(nearest.value).toFixed(1), r: 4.5,
        class: "ep-dot" + (ep.status === "attributed" ? " attributed" : ""),
      });
      const title = svgEl("title");
      title.textContent = `${ep.channel}: ${ep.raised_by} peak ${ep.peak_score}`
        + (ep.status === "attributed" ? ` \\u2014 attributed to ${ep.attributed_to}` : "")
        + ` \\u2014 ${clockLabel(trueAt)}`;
      dot.append(title);
      svg.append(dot);
    }
  });
}

let lastSeenEvent = null;
let lastSeenAt = 0;

async function tick() {
  let data;
  try {
    data = await fetch("/stream?seconds=" + STREAM_WINDOW_S).then(r => r.json());
  } catch (e) {
    return;   // a failed poll is a skipped frame, not a broken page
  }
  drawStream(data);

  const rate = document.getElementById("rate");
  const note = document.getElementById("rate-note");
  // "Moving" is decided by the newest event time changing between polls, not by the clock.
  // A stopped producer should read as stopped rather than as a live stream at zero.
  const now = Date.now();
  if (data.last_event_ms && data.last_event_ms !== lastSeenEvent) {
    lastSeenEvent = data.last_event_ms;
    lastSeenAt = now;
  }
  const moving = lastSeenAt && (now - lastSeenAt) < STREAM_STALL_MS;
  rate.replaceChildren();
  const dot = el("span", "live-dot" + (moving ? "" : " stalled"));
  rate.append(dot, document.createTextNode(fmt(data.readings_per_s ?? 0)));
  const unit = el("span", "unit", "readings/s");
  rate.append(unit);
  note.textContent = moving
    ? `mean over the last ${data.window_s}s \\u00b7 ${fmt(data.readings)} readings`
    : "stream idle \\u2014 no new readings since the last poll";
}

// The panels read stored aggregates and change slowly; the chart is the live one, so the
// two poll at different rates rather than dragging every panel to the chart's cadence.
load();
setInterval(load, 5000);
tick();
setInterval(tick, STREAM_POLL_MS);
</script>
</body>
</html>
"""
