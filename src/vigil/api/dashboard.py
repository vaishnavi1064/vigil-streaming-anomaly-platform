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
    --series-1:       #2a78d6;
    --series-2:       #eb6834;
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
  ok:       { glyph: "○", label: "ok" },
  info:     { glyph: "◌", label: "info" },
  warning:  { glyph: "◐", label: "warning" },
  critical: { glyph: "●", label: "critical" },
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

load();
setInterval(load, 5000);
</script>
</body>
</html>
"""
