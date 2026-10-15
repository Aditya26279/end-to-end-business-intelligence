/* Retail BI dashboard front end: vanilla JS + Chart.js, talking to the FastAPI endpoints. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const state = { tab: "overview", meta: null, charts: {}, segment: null, cache: {} };

// ------------------------------------------------------------------ formatting
const fmtMoney = (v, compact = true) => v == null ? "–" : new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", notation: compact ? "compact" : "standard",
  maximumFractionDigits: compact ? 1 : 0,
}).format(v);
const fmtInt = (v) => v == null ? "–" : new Intl.NumberFormat("en-US").format(Math.round(v));
const fmtPct = (v, d = 1) => v == null ? "–" : (v * 100).toFixed(d) + "%";
const fmtNum = (v, d = 2) => v == null ? "–" : Number(v).toFixed(d);
const monthLabel = (iso) => new Date(iso + "T00:00:00").toLocaleDateString("en-US", { month: "short", year: "2-digit" });

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const series = (i) => css(`--s${i}`);

// ------------------------------------------------------------------ data access
function filterParams() {
  const f = new FormData($("#filters"));
  const p = new URLSearchParams();
  for (const k of ["start", "end", "region", "channel", "category", "segment"]) {
    const v = f.get(k);
    if (v) p.set(k, v);
  }
  return p;
}

async function api(path, params) {
  const url = params && [...params].length ? `${path}?${params}` : path;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

// ------------------------------------------------------------------ DOM helpers (text only, never innerHTML for data)
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "style") node.style.cssText = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

/** columns: [{key, label, num?, fmt?, render?}] */
function renderTable(table, columns, rows, { onRowClick, selected } = {}) {
  table.replaceChildren();
  table.append(el("thead", {}, el("tr", {}, columns.map((c) => el("th", { class: c.num ? "num" : "" }, c.label)))));
  const body = el("tbody");
  if (!rows.length) {
    body.append(el("tr", {}, el("td", { colspan: columns.length, class: "empty" }, "No rows for this selection")));
  }
  for (const r of rows) {
    const tr = el("tr", { class: (onRowClick ? "clickable " : "") + (selected && selected(r) ? "selected" : "") },
      columns.map((c) => {
        const content = c.render ? c.render(r) : (c.fmt ? c.fmt(r[c.key]) : r[c.key]);
        return el("td", { class: c.num ? "num" : c.wide ? "wide" : "" }, content);
      }));
    if (onRowClick) tr.addEventListener("click", () => onRowClick(r));
    body.append(tr);
  }
  table.append(body);
}

const barCell = (value, max, text, color = "var(--s1)") =>
  el("span", { class: "bar-cell" },
    el("span", { class: "bar", style: `width:${Math.max(2, 60 * value / (max || 1))}px;background:${color}` }), text);

function statusBadge(ok, text) {
  const color = ok === true ? "var(--good)" : ok === false ? "var(--critical)" : "var(--warning)";
  const icon = ok === true ? "✓" : ok === false ? "✕" : "•";
  return el("span", { class: "status" }, el("span", { class: "dot", style: `background:${color}` }), `${icon} ${text}`);
}

// ------------------------------------------------------------------ chart helpers
function baseOptions(extra = {}) {
  return {
    responsive: true, maintainAspectRatio: false, animation: { duration: 250 },
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: css("--surface"), titleColor: css("--text-2"), bodyColor: css("--text"),
        borderColor: css("--border"), borderWidth: 1, padding: 9, boxPadding: 4, usePointStyle: true,
        bodyFont: { weight: "600" },
      },
    },
    scales: {
      x: { grid: { display: false }, border: { color: css("--axis") }, ticks: { color: css("--muted"), maxRotation: 0, autoSkipPadding: 12 } },
      y: { grid: { color: css("--grid") }, border: { display: false }, ticks: { color: css("--muted") }, beginAtZero: true },
    },
    ...extra,
  };
}

function upsertChart(id, config) {
  const existing = state.charts[id];
  if (existing) existing.destroy();
  state.charts[id] = new Chart(document.getElementById(id), config);
}

function hbar(id, labels, values, { money = true } = {}) {
  const opts = baseOptions({ indexAxis: "y" });
  opts.interaction = { mode: "nearest", axis: "y", intersect: false };
  opts.scales.x = { grid: { color: css("--grid") }, border: { display: false }, beginAtZero: true,
    ticks: { color: css("--muted"), callback: (v) => money ? fmtMoney(v) : v } };
  opts.scales.y = { grid: { display: false }, border: { color: css("--axis") }, ticks: { color: css("--text-2") } };
  opts.plugins.tooltip.callbacks = { label: (c) => ` ${money ? fmtMoney(c.raw, false) : fmtNum(c.raw)}` };
  upsertChart(id, {
    type: "bar",
    data: { labels, datasets: [{ data: values, backgroundColor: series(1), hoverBackgroundColor: series(1) + "cc",
      borderRadius: 4, borderSkipped: "start", maxBarThickness: 22, categoryPercentage: 0.8, barPercentage: 0.9 }] },
    options: opts,
  });
}

// ------------------------------------------------------------------ filters
// Calendar math on local Y/M/D only: no UTC conversion (which shifts dates west of Greenwich)
// and no setMonth() overflow (May 31 - 3 months must be Feb 28/29, not "Feb 31" = Mar 3).
const ymd = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
function shiftMonths(d, months) {
  const first = new Date(d.getFullYear(), d.getMonth() + months, 1);
  const lastDay = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate();
  return new Date(first.getFullYear(), first.getMonth(), Math.min(d.getDate(), lastDay));
}
const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);

function setPreset(preset) {
  const f = $("#filters");
  const [y, m, d] = state.meta.max_date.split("-").map(Number);
  const max = new Date(y, m - 1, d);
  let start = null;
  if (preset === "12m") start = addDays(shiftMonths(max, -12), 1);
  if (preset === "ytd") start = new Date(max.getFullYear(), 0, 1);
  if (preset === "q") start = addDays(shiftMonths(max, -3), 1);
  if (preset === "all") { f.start.value = state.meta.min_date; f.end.value = state.meta.max_date; return; }
  if (preset === "custom") return;
  f.start.value = ymd(start) < state.meta.min_date ? state.meta.min_date : ymd(start);
  f.end.value = state.meta.max_date;
}

function fillSelect(select, values) {
  for (const v of values) select.append(el("option", { value: v }, v));
}

function initFilters(meta) {
  const f = $("#filters");
  fillSelect(f.region, meta.regions);
  fillSelect(f.channel, meta.channels);
  fillSelect(f.category, meta.categories);
  fillSelect(f.segment, meta.segments);
  fillSelect($("#r-category"), meta.categories);
  f.start.min = f.end.min = meta.min_date;
  f.start.max = f.end.max = meta.max_date;
  setPreset("12m");
  f.preset.addEventListener("change", () => { setPreset(f.preset.value); refresh(); });
  for (const name of ["start", "end"]) f[name].addEventListener("change", () => { f.preset.value = "custom"; refresh(); });
  for (const name of ["region", "channel", "category", "segment"]) f[name].addEventListener("change", refresh);
  $("#reset").addEventListener("click", () => {
    f.reset(); f.preset.value = "12m"; setPreset("12m"); refresh();
  });
}

// ------------------------------------------------------------------ overview tab
function kpiTile(label, value, cur, prev, { invert = false, pct = false } = {}) {
  let delta = el("div", { class: "delta" }, prev == null ? "no comparison period" : "");
  if (prev != null && cur != null) {
    const change = pct ? cur - prev : (prev ? (cur - prev) / Math.abs(prev) : null);
    if (change != null && isFinite(change)) {
      const good = invert ? change < 0 : change > 0;
      const arrow = change > 0 ? "▲" : change < 0 ? "▼" : "■";
      const text = pct ? `${(change * 100).toFixed(1)} pts` : `${(change * 100).toFixed(1)}%`;
      delta = el("div", { class: `delta ${change === 0 ? "" : good ? "up" : "down"}` }, `${arrow} ${text} vs prior period`);
    }
  }
  return el("div", { class: "card kpi" }, el("div", { class: "label" }, label), el("div", { class: "value" }, value), delta);
}

async function loadOverview() {
  const p = filterParams();
  const dimParams = (dim) => { const q = new URLSearchParams(p); q.set("dim", dim); return q; };
  const [k, trend, cats, regions, channels, ages, top] = await Promise.all([
    api("/api/kpis", p), api("/api/trend", p),
    api("/api/breakdown", dimParams("category")), api("/api/breakdown", dimParams("sales_region")),
    api("/api/breakdown", dimParams("channel")), api("/api/breakdown", dimParams("age_band")),
    api("/api/top-products", p),
  ]);
  const c = k.current, pr = k.previous || {};
  const has = !!k.previous;
  $("#kpis").replaceChildren(
    kpiTile("Net revenue", fmtMoney(c.revenue), c.revenue, has ? pr.revenue : null),
    kpiTile("Gross profit", fmtMoney(c.profit), c.profit, has ? pr.profit : null),
    kpiTile("Margin", fmtPct(c.margin), c.margin, has ? pr.margin : null, { pct: true }),
    kpiTile("Orders", fmtInt(c.orders), c.orders, has ? pr.orders : null),
    kpiTile("Avg order value", fmtMoney(c.aov, false), c.aov, has ? pr.aov : null),
    kpiTile("Return rate", fmtPct(c.return_rate), c.return_rate, has ? pr.return_rate : null, { invert: true, pct: true }),
  );

  const opts = baseOptions();
  opts.scales.y.ticks.callback = (v) => fmtMoney(v);
  opts.plugins.tooltip.callbacks = { label: (ctx) => ` ${fmtMoney(ctx.raw, false)}  ${ctx.dataset.label}` };
  const line = (label, data, color) => ({
    label, data, borderColor: color, backgroundColor: color, borderWidth: 2, pointRadius: 0,
    pointHoverRadius: 5, pointHoverBorderWidth: 2, pointHoverBorderColor: css("--surface"), tension: 0.25,
  });
  upsertChart("trend-chart", {
    type: "line",
    data: { labels: trend.map((r) => monthLabel(r.month)),
      datasets: [line("Net revenue", trend.map((r) => r.revenue), series(1)), line("Profit", trend.map((r) => r.profit), series(2))] },
    options: opts,
  });
  hbar("cat-chart", cats.map((r) => r.label), cats.map((r) => r.revenue));
  hbar("region-chart", regions.map((r) => r.label), regions.map((r) => r.revenue));
  hbar("channel-chart", channels.map((r) => r.label), channels.map((r) => r.revenue));
  const ageOrder = ["18-24", "25-34", "35-44", "45-54", "55-64", "65+", "Unknown"];
  ages.sort((a, b) => ageOrder.indexOf(a.label) - ageOrder.indexOf(b.label));
  hbar("age-chart", ages.map((r) => r.label), ages.map((r) => r.revenue));

  const maxRev = Math.max(...top.map((r) => r.revenue), 1);
  renderTable($("#top-products"), [
    { key: "product_name", label: "Product" },
    { key: "category", label: "Category", render: (r) => el("span", { class: "tag" }, r.category) },
    { key: "units", label: "Units", num: true, fmt: fmtInt },
    { key: "revenue", label: "Net revenue", num: true, render: (r) => barCell(r.revenue, maxRev, fmtMoney(r.revenue, false)) },
    { key: "margin", label: "Margin", num: true, fmt: (v) => fmtPct(v) },
  ], top);
}

// ------------------------------------------------------------------ customers tab
async function loadCustomers() {
  const p = filterParams();
  const [seg, grid] = await Promise.all([api("/api/segments", p), state.cache.rfm || api("/api/rfm-grid")]);
  state.cache.rfm = grid;
  const prof = seg.profiles;
  if (seg.model) {
    const m = seg.model;
    $("#seg-model-desc").textContent = `K-Means with k=${m.params.k} on log recency, frequency and monetary value ` +
      `(silhouette ${fmtNum(m.metrics.silhouette_by_k[m.params.k], 3)}, ${fmtInt(m.metrics.customers)} customers). ` +
      `“In slice” columns respect the filters above.`;
  }
  const maxSlice = Math.max(...prof.map((r) => r.slice_revenue), 1);
  if (!state.segment || !prof.some((r) => r.segment === state.segment)) state.segment = prof[0]?.segment;
  renderTable($("#seg-table"), [
    { key: "segment", label: "Segment", render: (r) => el("b", {}, r.segment) },
    { key: "customers", label: "Customers", num: true, fmt: fmtInt },
    { key: "avg_recency", label: "Avg days since order", num: true, fmt: fmtInt },
    { key: "avg_frequency", label: "Avg orders", num: true, fmt: (v) => fmtNum(v, 1) },
    { key: "avg_monetary", label: "Avg lifetime value", num: true, fmt: (v) => fmtMoney(v, false) },
    { key: "share_revenue", label: "Revenue share", num: true, fmt: (v) => fmtPct(v) },
    { key: "slice_revenue", label: "Revenue in slice", num: true, render: (r) => barCell(r.slice_revenue, maxSlice, fmtMoney(r.slice_revenue)) },
    { key: "action", label: "Recommended action", wide: true, render: (r) => el("span", { class: "muted" }, r.action) },
  ], prof, { onRowClick: (r) => { state.segment = r.segment; refresh(); }, selected: (r) => r.segment === state.segment });

  const opts = baseOptions({ indexAxis: "y" });
  opts.scales.x = { grid: { color: css("--grid") }, border: { display: false }, beginAtZero: true,
    ticks: { color: css("--muted"), callback: (v) => fmtPct(v, 0) } };
  opts.scales.y = { grid: { display: false }, border: { color: css("--axis") }, ticks: { color: css("--text-2") } };
  opts.plugins.tooltip.callbacks = { label: (c) => ` ${fmtPct(c.raw)}  ${c.dataset.label}` };
  const ds = (label, data, color) => ({ label, data, backgroundColor: color, borderRadius: 4, borderSkipped: "start",
    maxBarThickness: 16, categoryPercentage: 0.75, barPercentage: 0.92, borderColor: css("--surface"), borderWidth: { top: 1, bottom: 1 } });
  upsertChart("seg-chart", {
    type: "bar",
    data: { labels: prof.map((r) => r.segment), datasets: [
      ds("Share of customers", prof.map((r) => r.share_customers), series(1)),
      ds("Share of revenue", prof.map((r) => r.share_revenue), series(2))] },
    options: opts,
  });

  renderHeat(grid);
  $("#seg-cust-title").textContent = `Top customers: ${state.segment}`;
  const custs = await api("/api/segment-customers", new URLSearchParams({ segment: state.segment }));
  renderTable($("#seg-customers"), [
    { key: "customer_id", label: "ID" }, { key: "full_name", label: "Name" }, { key: "region", label: "Region" },
    { key: "recency_days", label: "Days since order", num: true, fmt: fmtInt },
    { key: "frequency", label: "Orders", num: true, fmt: fmtInt },
    { key: "monetary", label: "Lifetime value", num: true, fmt: (v) => fmtMoney(v, false) },
    { key: "rfm_code", label: "RFM", num: true },
  ], custs);
}

function renderHeat(grid) {
  const box = $("#rfm-heat");
  const vals = grid.map((g) => g.avg_monetary);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const ramp = ["--seq-0", "--seq-1", "--seq-2", "--seq-3", "--seq-4"].map(css);
  const lookup = Object.fromEntries(grid.map((g) => [`${g.r_score}-${g.f_score}`, g]));
  const heat = el("div", { class: "heat" });
  heat.append(el("div", { class: "axis" }, "R \\ F"));
  for (let f = 1; f <= 5; f++) heat.append(el("div", { class: "axis" }, `F${f}`));
  const tip = $("#tooltip");
  for (let r = 5; r >= 1; r--) {
    heat.append(el("div", { class: "axis" }, `R${r}`));
    for (let f = 1; f <= 5; f++) {
      const g = lookup[`${r}-${f}`];
      const n = g ? g.customers : 0;
      const step = g ? Math.min(4, Math.floor(((g.avg_monetary - lo) / ((hi - lo) || 1)) * 5)) : 0;
      const cell = el("div", { class: "cell", tabindex: "0",
        style: `background:${ramp[step]};color:${step >= 2 ? "#fff" : "#0b0b0b"}` }, g ? fmtMoney(g.avg_monetary) : "");
      const show = (e) => {
        tip.replaceChildren(el("b", {}, `${g ? fmtMoney(g.avg_monetary, false) : "–"} avg lifetime value`), el("br"),
          `${fmtInt(n)} customers`, el("br"), `Recency score ${r}, frequency score ${f}`);
        tip.style.display = "block";
        const rect = (e.target).getBoundingClientRect();
        tip.style.left = `${Math.min(rect.right + 6, window.innerWidth - 220)}px`;
        tip.style.top = `${rect.top}px`;
      };
      cell.addEventListener("pointerenter", show); cell.addEventListener("focus", show);
      cell.addEventListener("pointerleave", () => (tip.style.display = "none"));
      cell.addEventListener("blur", () => (tip.style.display = "none"));
      heat.append(cell);
    }
  }
  box.replaceChildren(heat, el("p", { class: "desc", style: "margin-top:8px" },
`Darker = higher average lifetime value (${fmtMoney(lo)} – ${fmtMoney(hi)}). Value piles up with frequency; the bottom-right cells are valuable customers going quiet.`));
}

// ------------------------------------------------------------------ forecast tab
async function loadForecast() {
  const sel = $("#fc-series");
  const data = await api("/api/forecast", new URLSearchParams({ series: sel.value || "All" }));
  if (!sel.options.length) { fillSelect(sel, data.series); sel.addEventListener("change", refresh); }
  const pts = data.points;
  const labels = pts.map((r) => monthLabel(r.month));
  const lastActual = pts.filter((r) => r.actual != null).at(-1);
  // join the forecast line to the last actual so there is no visual gap
  const fc = pts.map((r) => r.forecast ?? (r === lastActual ? r.actual : null));
  const lower = pts.map((r) => r.lower_95 ?? (r === lastActual ? r.actual : null));
  const upper = pts.map((r) => r.upper_95 ?? (r === lastActual ? r.actual : null));

  const opts = baseOptions();
  opts.scales.y.ticks.callback = (v) => fmtMoney(v);
  opts.plugins.tooltip.filter = (item) => item.raw != null && !item.dataset.isBand;
  opts.plugins.tooltip.callbacks = {
    label: (c) => ` ${fmtMoney(c.raw, false)}  ${c.dataset.label}`,
    afterBody: (items) => {
      const r = pts[items[0].dataIndex];
      return r.forecast != null ? [`95% interval: ${fmtMoney(r.lower_95)} – ${fmtMoney(r.upper_95)}`] : [];
    },
  };
  const common = { pointRadius: 0, pointHoverRadius: 5, tension: 0.2, borderWidth: 2, spanGaps: false };
  upsertChart("fc-chart", {
    type: "line",
    data: { labels, datasets: [
      { label: "upper", data: upper, borderWidth: 0, pointRadius: 0, fill: false, isBand: true },
      { label: "lower", data: lower, borderWidth: 0, pointRadius: 0, fill: "-1", backgroundColor: css("--band"), isBand: true },
      { label: "Model fit", data: pts.map((r) => r.fitted), borderColor: css("--muted"), backgroundColor: css("--muted"), ...common, borderWidth: 1.5 },
      { label: "Actual", data: pts.map((r) => r.actual), borderColor: series(1), backgroundColor: series(1), ...common },
      { label: "Forecast", data: fc, borderColor: series(2), backgroundColor: series(2), ...common, borderDash: [6, 4] },
    ] },
    options: opts,
  });

  const m = data.metrics || {};
  const next = pts.filter((r) => r.forecast != null);
  const total = next.reduce((s, r) => s + r.forecast, 0);
  const better = m.mape_holt_winters != null && m.mape_holt_winters < m.mape_seasonal_naive;
  $("#fc-stats").replaceChildren(
    el("div", {}, el("b", {}, fmtMoney(total)), `forecast, next ${next.length} months`),
    el("div", {}, el("b", {}, `${fmtNum(m.mape_holt_winters, 1)}%`), "backtest MAPE (last 6 months held out)"),
    el("div", {}, el("b", {}, `${fmtNum(m.mape_seasonal_naive, 1)}%`), "seasonal-naive baseline MAPE"),
    el("div", {}, el("b", {}, statusBadge(better, better ? "beats baseline" : "no better than baseline"))),
  );
  renderTable($("#fc-table"), [
    { key: "month", label: "Month", render: (r) => monthLabel(r.month) },
    { key: "forecast", label: "Forecast", num: true, fmt: (v) => fmtMoney(v, false) },
    { key: "lower_95", label: "Lower 95%", num: true, fmt: (v) => fmtMoney(v, false) },
    { key: "upper_95", label: "Upper 95%", num: true, fmt: (v) => fmtMoney(v, false) },
  ], next);
}

// ------------------------------------------------------------------ market basket tab
let rulesTimer;
function initRules() {
  const go = () => { clearTimeout(rulesTimer); rulesTimer = setTimeout(refresh, 200); };
  $("#r-lift").addEventListener("input", (e) => { $("#r-lift-out").textContent = Number(e.target.value).toFixed(1); go(); });
  $("#r-conf").addEventListener("input", (e) => { $("#r-conf-out").textContent = fmtPct(Number(e.target.value), 0); go(); });
  for (const id of ["#r-level", "#r-category"]) $(id).addEventListener("change", go);
  $("#r-item").addEventListener("input", go);
}

async function loadRules() {
  const level = $("#r-level").value;
  $("#r-category").disabled = level !== "product";
  const p = new URLSearchParams({ level, min_lift: $("#r-lift").value, min_confidence: $("#r-conf").value, limit: 200 });
  if ($("#r-category").value && level === "product") p.set("category", $("#r-category").value);
  if ($("#r-item").value.trim()) p.set("item", $("#r-item").value.trim());
  const { rules, metrics } = await api("/api/rules", p);
  if (metrics) {
    $("#rules-stats").replaceChildren(
      el("div", {}, el("b", {}, fmtInt(metrics.baskets)), "orders analysed"),
      el("div", {}, el("b", {}, fmtInt(metrics.multi_item_baskets)), "multi-item baskets"),
      el("div", {}, el("b", {}, fmtInt(level === "product" ? metrics.product_rules : metrics.subcategory_rules)), `${level} rules mined`),
      el("div", {}, el("b", {}, fmtInt(rules.length)), "match the filters"),
    );
  }
  const top = rules.slice(0, 10);
  const short = (s) => s.length > 22 ? s.slice(0, 21) + "…" : s;
  hbar("rules-chart", top.map((r) => `${short(r.antecedents)} → ${short(r.consequents)}`), top.map((r) => r.lift), { money: false });
  state.charts["rules-chart"].options.plugins.tooltip.callbacks = {
    title: (items) => { const r = top[items[0].dataIndex]; return `${r.antecedents} → ${r.consequents}`; },
    label: (c) => { const r = top[c.dataIndex]; return [` Lift ${fmtNum(r.lift)}`, ` Confidence ${fmtPct(r.confidence)}`, ` ${fmtInt(r.basket_count)} baskets`]; },
  };
  state.charts["rules-chart"].options.scales.y.ticks.font = { size: 10 };
  state.charts["rules-chart"].update();

  const maxLift = Math.max(...rules.map((r) => r.lift), 1);
  renderTable($("#rules-table"), [
    { key: "antecedents", label: "If basket has" },
    { key: "consequents", label: "…then it also has", render: (r) => el("b", {}, r.consequents) },
    { key: "confidence", label: "Confidence", num: true, fmt: (v) => fmtPct(v) },
    { key: "lift", label: "Lift", num: true, render: (r) => barCell(r.lift, maxLift, fmtNum(r.lift, 1)) },
    { key: "support", label: "Support", num: true, fmt: (v) => fmtPct(v, 2) },
    { key: "basket_count", label: "Baskets", num: true, fmt: fmtInt },
  ], rules);
}

// ------------------------------------------------------------------ pipeline tab
async function loadPipeline() {
  const d = await api("/api/pipeline");
  const time = (v) => v ? new Date(v).toLocaleString() : "–";
  const dur = (r) => r.finished_at ? `${((new Date(r.finished_at) - new Date(r.started_at)) / 1000).toFixed(1)}s` : "–";
  renderTable($("#runs-table"), [
    { key: "run_id", label: "#", num: true },
    { key: "step", label: "Step", render: (r) => el("b", {}, r.step) },
    { key: "status", label: "Status", render: (r) => statusBadge(r.status === "success" ? true : r.status === "failed" ? false : null, r.status) },
    { key: "started_at", label: "Started", fmt: time },
    { key: "duration", label: "Duration", num: true, render: dur },
    { key: "rows_in", label: "Rows in", num: true, fmt: fmtInt },
    { key: "rows_out", label: "Rows out", num: true, fmt: fmtInt },
    { key: "rows_rejected", label: "Rejected", num: true, fmt: fmtInt },
    { key: "message", label: "Message", wide: true, render: (r) => el("span", { class: "muted" }, r.message || "") },
  ], d.runs);
  renderTable($("#checks-table"), [
    { key: "check_name", label: "Check" },
    { key: "passed", label: "Result", render: (r) => statusBadge(r.passed, r.passed ? "pass" : "fail") },
    { key: "observed", label: "Observed", num: true, fmt: (v) => fmtNum(v, 2) },
    { key: "detail", label: "Rule", render: (r) => el("span", { class: "muted" }, r.detail) },
  ], d.checks);
  renderTable($("#rejects-table"), [
    { key: "source_system", label: "Source", render: (r) => el("span", { class: "tag" }, r.source_system) },
    { key: "reason", label: "Reason" },
    { key: "rows", label: "Rows", num: true, fmt: fmtInt },
  ], d.rejects);
  renderTable($("#tables-table"), [
    { key: "table_name", label: "Table" },
    { key: "layer", label: "Layer", render: (r) => el("span", { class: "tag" }, r.table_name.split(".")[0]) },
    { key: "rows", label: "Rows", num: true, fmt: fmtInt },
  ], d.tables);
}

// ------------------------------------------------------------------ orchestration
const loaders = { overview: loadOverview, customers: loadCustomers, forecast: loadForecast, basket: loadRules, pipeline: loadPipeline };
const filteredTabs = new Set(["overview", "customers"]);

// Refreshes are serialised: a filter change during a load queues exactly one more load,
// so a slow, stale response can never overwrite a newer one.
let inFlight = null, rerun = false;
async function refresh() {
  if (inFlight) { rerun = true; return inFlight; }
  inFlight = (async () => {
    do {
      rerun = false;
      const panel = document.querySelector(`[data-panel="${state.tab}"]`);
      panel.classList.add("loading");
      panel.querySelectorAll(".load-error").forEach((n) => n.remove());
      try {
        await loaders[state.tab]();
      } catch (err) {
        console.error(err);
        panel.prepend(el("div", { class: "card empty load-error" }, `Could not load data: ${err.message}`));
      } finally {
        panel.classList.remove("loading");
      }
    } while (rerun);
  })();
  try { await inFlight; } finally { inFlight = null; }
}

function selectTab(tab) {
  state.tab = tab;
  for (const b of document.querySelectorAll("nav.tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  for (const s of document.querySelectorAll("[data-panel]")) s.hidden = s.dataset.panel !== tab;
  $("#filters").hidden = !filteredTabs.has(tab);
  refresh();
}

function initTheme() {
  const btn = $("#theme-toggle");
  const saved = (() => { try { return localStorage.getItem("bi-theme"); } catch { return null; } })();
  if (saved) document.documentElement.dataset.theme = saved;
  const label = () => { btn.textContent = document.documentElement.dataset.theme === "dark" ||
    (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches) ? "☀ Light" : "☾ Dark"; };
  label();
  btn.addEventListener("click", () => {
    const dark = btn.textContent.includes("Light");
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("bi-theme", document.documentElement.dataset.theme); } catch { /* storage unavailable */ }
    label();
    refresh(); // charts read colours from CSS variables, so redraw
  });
}

async function main() {
  initTheme();
  try {
    state.meta = await api("/api/meta");
  } catch (err) {
    document.querySelector(".wrap").append(el("div", { class: "card empty" },
      "The warehouse is empty or unreachable. Run: python run_pipeline.py all"));
    return;
  }
  const last = state.meta.last_etl[0];
  $("#etl-status").replaceChildren(
    el("span", { class: "dot", style: `background:${last && last.status === "success" ? "var(--good)" : "var(--warning)"}` }),
    `Data through ${new Date(state.meta.max_date + "T00:00:00").toLocaleDateString("en-US", { dateStyle: "medium" })}`);
  initFilters(state.meta);
  initRules();
  for (const b of document.querySelectorAll("nav.tabs button")) b.addEventListener("click", () => selectTab(b.dataset.tab));
  selectTab("overview");
}

main();
