/* Bot dashboard front end. Plain JS, no build step. Only reads /api/*. */
"use strict";

const TZ = "Asia/Karachi";
const REFRESH_MS = 30000;
const TABS = [
  ["home", "Home"], ["charts", "Charts"], ["trades", "Trades"], ["agents", "Agents"],
  ["market", "Market"], ["decisions", "Decisions"], ["learning", "Seekhna"],
  ["health", "Situation"], ["emails", "Emails"], ["settings", "Settings"],
];
const state = { tab: "home", period: "day", range: "7d", emailFilter: "all", emailSearch: "", decisionsOnlyChanges: false, charts: {} };
let refreshTimer = null;

// ---------- helpers ----------
const $ = (sel) => document.querySelector(sel);
function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function num(v, d = 2) {
  if (v === null || v === undefined || isNaN(v)) return "–";
  return Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}
function usd(v, d = 2) { return v === null || v === undefined ? "–" : `$${num(v, d)}`; }
function signedUsd(v) {
  if (v === null || v === undefined || isNaN(v)) return "–";
  return `${v >= 0 ? "+" : "−"}$${num(Math.abs(v))}`;
}
function pct(v, d = 2) {
  if (v === null || v === undefined || isNaN(v)) return "–";
  return `${v >= 0 ? "+" : "−"}${num(Math.abs(v), d)}%`;
}
function cls(v) { return v > 0 ? "pos" : v < 0 ? "neg" : ""; }
function when(iso) {
  if (!iso) return "–";
  return new Date(iso).toLocaleString("en-GB", { timeZone: TZ, day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
}
function ago(iso) {
  if (!iso) return "kabhi nahi";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return `${Math.round(s)} sec pehle`;
  if (s < 5400) return `${Math.round(s / 60)} min pehle`;
  if (s < 172800) return `${Math.round(s / 3600)} ghante pehle`;
  return `${Math.round(s / 86400)} din pehle`;
}
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function pill(status, text) { return `<span class="pill ${esc(status)}">${esc(text ?? status)}</span>`; }

async function api(path, opts) {
  const res = await fetch(path, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...opts });
  if (res.status === 401 && !path.endsWith("/login")) { showLogin(); throw new Error("login"); }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

// ---------- login ----------
function showLogin() {
  clearInterval(refreshTimer);
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  setTimeout(() => $("#user").focus(), 50);
}
function showApp() {
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  renderTabs();
  load();
  clearInterval(refreshTimer);
  refreshTimer = setInterval(() => { if (!document.hidden) load(true); }, REFRESH_MS);
}
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("/api/login", { method: "POST", body: JSON.stringify({ user: $("#user").value, password: $("#password").value }) });
    $("#password").value = "";
    showApp();
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});
$("#logout").addEventListener("click", async () => { await api("/api/logout", { method: "POST" }).catch(() => {}); showLogin(); });
$("#refresh").addEventListener("click", () => load());
document.addEventListener("visibilitychange", () => { if (!document.hidden && !$("#app").classList.contains("hidden")) load(true); });

// ---------- tabs ----------
function renderTabs() {
  $("#tabs").innerHTML = TABS.map(([id, label]) => `<button class="tab ${id === state.tab ? "active" : ""}" data-tab="${id}">${label}</button>`).join("");
}
$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("[data-tab]");
  if (!b) return;
  state.tab = b.dataset.tab;
  try { localStorage.setItem("tab", state.tab); } catch (_) {}
  history.replaceState(null, "", `#${state.tab}`);
  renderTabs();
  window.scrollTo(0, 0);
  load();
});

async function load(quiet) {
  const view = $("#view");
  if (!quiet) { destroyCharts(); view.innerHTML = `<div class="empty">Loading…</div>`; }
  try {
    const page = PAGES[state.tab] || PAGES.home;
    const html = await page();
    if (quiet) destroyCharts();
    view.innerHTML = html.html;
    if (html.after) html.after();
    $("#updated").textContent = "Updated " + new Date().toLocaleTimeString("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit" });
  } catch (err) {
    if (err.message !== "login") view.innerHTML = `<div class="card"><b>Data load nahi hua:</b> ${esc(err.message)}</div>`;
  }
}

function setMode(mode) {
  if (!mode) return;
  const b = $("#mode-badge");
  b.textContent = mode.label;
  b.className = `mode-badge ${mode.mode}`;
  b.title = mode.note;
}

// ---------- charts ----------
function destroyCharts() { Object.values(state.charts).forEach((c) => c.destroy()); state.charts = {}; }
function baseOptions(yFormat) {
  const grid = cssVar("--grid"), text = cssVar("--text-2");
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { display: false }, tooltip: { backgroundColor: cssVar("--surface"), titleColor: cssVar("--text"), bodyColor: cssVar("--text"), borderColor: cssVar("--border"), borderWidth: 1, padding: 10 } },
    scales: {
      x: { grid: { display: false }, ticks: { color: text, maxRotation: 0, autoSkipPadding: 16 }, border: { color: grid } },
      y: { grid: { color: grid }, ticks: { color: text, callback: yFormat }, border: { display: false } },
    },
  };
}
function timeTicks(range) {
  return (v) => {
    const d = new Date(v);
    const o = range === "24h" ? { hour: "2-digit", minute: "2-digit", hour12: false } : { day: "2-digit", month: "short" };
    return d.toLocaleString("en-GB", { timeZone: TZ, ...o });
  };
}
function equityChart(id, points, small) {
  const el = document.getElementById(id);
  if (!el || !points.length) return;
  const opts = baseOptions((v) => `$${num(v, 0)}`);
  opts.scales.x = { type: "linear", grid: { display: false }, ticks: { color: cssVar("--text-2"), maxRotation: 0, maxTicksLimit: small ? 4 : 7, callback: timeTicks("7d") }, border: { color: cssVar("--grid") } };
  opts.plugins.tooltip.callbacks = { title: (it) => when(new Date(it[0].parsed.x).toISOString()), label: (it) => ` Balance: ${usd(it.parsed.y)}` };
  state.charts[id] = new Chart(el, {
    type: "line",
    data: { datasets: [{ data: points.map((p) => ({ x: new Date(p.t).getTime(), y: p.balance })), borderColor: cssVar("--series-1"), borderWidth: 2, pointRadius: points.length < 30 ? 3 : 0, pointHoverRadius: 5, stepped: "before", fill: false }] },
    options: opts,
  });
}

// ---------- pages ----------
const PAGES = {};

PAGES.home = async () => {
  const s = await api("/api/summary");
  setMode(s.mode);
  const p = s.position;
  const ld = s.last_decision;
  const agentRows = s.agents.map((a) => `
    <div class="row"><div class="main"><div class="title">${esc(a.agent)}</div><div class="meta">${esc(a.doing)}</div></div>
    <div class="side">${pill(a.status)}<div class="meta">${esc(ago(a.last_run))}</div></div></div>`).join("");
  let posHtml;
  if (p.open) {
    const lo = Math.min(p.stop_price, p.current_price ?? p.entry_price) * 0.997;
    const hi = Math.max(p.entry_price * 1.01, p.current_price ?? p.entry_price) * 1.003;
    const at = (v) => `${((v - lo) / (hi - lo)) * 100}%`;
    posHtml = `
      <div class="kv">
        <div>Entry price</div><div>${usd(p.entry_price)}</div>
        <div>Current price</div><div>${usd(p.current_price)} <span class="${cls(p.move_percent)}">(${pct(p.move_percent)})</span></div>
        <div>Stop-loss</div><div>${usd(p.stop_price)} (−${num(p.stop_percent, 1)}%)</div>
        <div>Stop tak faasla</div><div>${pct(p.distance_to_stop_percent)}</div>
        <div>Amount</div><div>${num(p.quantity, 6)} (${usd(p.value_at_entry)})</div>
        <div>Abhi ka P/L (fees ke baad)</div><div class="${cls(p.unrealized_pnl)}">${signedUsd(p.unrealized_pnl)}</div>
        <div>Kab khuli</div><div>${when(p.entry_time)}</div>
      </div>
      <div class="posbar">
        <div class="mark" style="left:${at(p.stop_price)};background:var(--bad-mark)"><span>Stop</span></div>
        <div class="mark top" style="left:${at(p.entry_price)};background:var(--muted)"><span>Entry</span></div>
        ${p.current_price ? `<div class="mark" style="left:${at(p.current_price)};background:var(--series-1)"><span>Ab</span></div>` : ""}
      </div>`;
  } else {
    posHtml = `<div class="notice">Koi position khuli nahi. ${p.wait_for_flat ? "Stop-loss ke baad bot signal ke flat hone ka intezar kar raha hai." : "Bot buy signal ka intezar kar raha hai."}</div>`;
  }
  const st = s.stats;
  return {
    html: `
    <div class="grid">
      <div class="card span-12 notice" style="border:0">${esc(s.mode.note)} Starting balance ${usd(s.starting_balance, 0)}.</div>
      <div class="card stat span-3"><div class="label">Kitne lagaye</div><div class="value">${usd(s.starting_balance, 0)}</div><div class="delta sub">Paper balance</div></div>
      <div class="card stat span-3"><div class="label">Ab kitne hain</div><div class="value">${usd(s.equity)}</div><div class="delta sub">Khuli position samet</div></div>
      <div class="card stat span-3"><div class="label">Kul profit / loss</div><div class="value ${cls(s.total_pnl)}">${signedUsd(s.total_pnl)}</div><div class="delta ${cls(s.total_pnl)}">${pct(s.total_pnl_percent)}</div></div>
      <div class="card stat span-3"><div class="label">${esc(s.symbol)} price</div><div class="value">${usd(s.price, 0)}</div><div class="delta ${cls(s.change_24h)}">${pct(s.change_24h)} (24h)</div></div>
      <div class="card stat span-4 tri"><div class="label">Aaj</div><div class="value ${cls(s.periods.today)}">${signedUsd(s.periods.today)}</div></div>
      <div class="card stat span-4 tri"><div class="label">Is hafte</div><div class="value ${cls(s.periods.week)}">${signedUsd(s.periods.week)}</div></div>
      <div class="card stat span-4 tri"><div class="label">Is mahine</div><div class="value ${cls(s.periods.month)}">${signedUsd(s.periods.month)}</div></div>
      <div class="card span-6"><h2>Khuli position</h2>${posHtml}</div>
      <div class="card span-6"><h2>Agents</h2><div class="list">${agentRows}</div></div>
      <div class="card span-6"><h2>Aakhri faisla</h2>${ld ? `
        <div class="title">${ld.position === 1 ? "LONG: buy / hold" : "FLAT: bahar raho"} ${ld.anomaly ? pill("bad", "Alert") : ""}</div>
        <div class="sub">${esc(when(ld.time))} · price ${usd(ld.price, 0)}</div><p>${esc(ld.reason)}</p>` : `<div class="empty">Abhi koi faisla record nahi hua.</div>`}</div>
      <div class="card span-6"><h2>Trades ka khulasa</h2>
        <div class="kv">
          <div>Band trades</div><div>${st.trades} (${st.wins} profit, ${st.losses} loss)</div>
          <div>Win rate</div><div>${st.win_rate === null ? "–" : num(st.win_rate, 0) + "%"}</div>
          <div>Fees ab tak</div><div>${usd(st.fees_total)}</div>
          <div>Sab se bara girao</div><div class="neg">${pct(s.max_drawdown_percent)}</div>
        </div></div>
    </div>`,
  };
};

PAGES.charts = async () => {
  const c = await api(`/api/charts?period=${state.period}&range=${state.range}`);
  const seg = (name, cur, items) => `<div class="seg" data-seg="${name}">${items.map(([v, l]) => `<button data-v="${v}" class="${v === cur ? "active" : ""}">${l}</button>`).join("")}</div>`;
  const pnlTotal = c.pnl.reduce((a, b) => a + b.pnl, 0);
  const rows = c.pnl.filter((b) => b.trades).slice().reverse().map((b) => `<tr><td>${esc(b.label)}</td><td class="${cls(b.pnl)}">${signedUsd(b.pnl)}</td><td>${b.trades}</td></tr>`).join("");
  return {
    html: `
    <div class="grid">
      <div class="card span-12">
        <div class="card-head"><h2>Profit / loss</h2>${seg("period", state.period, [["day", "Din"], ["week", "Hafta"], ["month", "Mahina"]])}</div>
        <div class="sub">Band hui trades ka asal profit/loss (fees ke baad). Is dauran kul: <b class="${cls(pnlTotal)}">${signedUsd(pnlTotal)}</b></div>
        <div class="legend" style="margin:6px 0"><span style="--c:var(--good-mark)">Profit</span><span style="--c:var(--bad-mark)">Loss</span></div>
        <div class="chart-box"><canvas id="pnl-chart" aria-label="Profit and loss per ${esc(state.period)}"></canvas></div>
        <details style="margin-top:8px"><summary>Table dekhein</summary><div class="table-wrap"><table><tr><th>Period</th><th>P/L</th><th>Trades</th></tr>${rows || `<tr><td colspan="3" class="empty">Abhi koi band trade nahi.</td></tr>`}</table></div></details>
      </div>
      <div class="card span-6">
        <div class="card-head"><h2>Balance</h2><span class="sub">Sab se bara girao: <b class="neg">${pct(c.max_drawdown_percent)}</b></span></div>
        <div class="chart-box"><canvas id="equity-chart" aria-label="Balance over time"></canvas></div>
      </div>
      <div class="card span-6">
        <div class="card-head"><h2>Price aur trades</h2>${seg("range", state.range, [["24h", "24h"], ["7d", "7 din"], ["30d", "30 din"]])}</div>
        <div class="legend" style="margin-bottom:6px"><span style="--c:var(--series-1)">Price</span><span style="--c:var(--good-mark)">▲ Buy</span><span style="--c:var(--bad-mark)">▼ Sell</span>${c.position.open ? `<span style="--c:var(--bad-mark)">– – Stop-loss</span>` : ""}</div>
        <div class="chart-box"><canvas id="price-chart" aria-label="Price with buy and sell markers"></canvas></div>
      </div>
    </div>`,
    after: () => {
      document.querySelectorAll("[data-seg] button").forEach((b) => b.addEventListener("click", () => {
        state[b.parentElement.dataset.seg] = b.dataset.v; load();
      }));
      // P/L bars
      const good = cssVar("--good-mark"), bad = cssVar("--bad-mark");
      const o = baseOptions((v) => `$${num(v, Math.abs(v) < 20 ? 2 : 0)}`);
      o.plugins.tooltip.callbacks = { label: (it) => ` ${signedUsd(it.parsed.y)} · ${c.pnl[it.dataIndex].trades} trade` };
      state.charts.pnl = new Chart($("#pnl-chart"), {
        type: "bar",
        data: { labels: c.pnl.map((b) => state.period === "month" ? b.label : b.label.slice(5)), datasets: [{ data: c.pnl.map((b) => b.pnl), backgroundColor: c.pnl.map((b) => (b.pnl >= 0 ? good : bad)), borderRadius: 4, borderSkipped: false, maxBarThickness: 28 }] },
        options: o,
      });
      equityChart("equity-chart", c.equity);
      // price + markers
      const po = baseOptions((v) => `$${num(v, 0)}`);
      po.scales.x = { type: "linear", grid: { display: false }, ticks: { color: cssVar("--text-2"), maxRotation: 0, maxTicksLimit: 6, callback: timeTicks(state.range) }, border: { color: cssVar("--grid") } };
      po.interaction = { mode: "nearest", intersect: false, axis: "x" };
      po.plugins.tooltip.callbacks = { title: (it) => when(new Date(it[0].parsed.x).toISOString()), label: (it) => ` ${it.dataset.label}: ${usd(it.parsed.y)}` };
      const xs = c.prices.map((p) => new Date(p.t).getTime());
      const ds = [{ label: "Price", data: c.prices.map((p, i) => ({ x: xs[i], y: p.price })), borderColor: cssVar("--series-1"), borderWidth: 2, pointRadius: 0, pointHoverRadius: 4 }];
      const buys = c.markers.filter((m) => m.side === "buy"), sells = c.markers.filter((m) => m.side === "sell");
      if (buys.length) ds.push({ label: "Buy", type: "scatter", data: buys.map((m) => ({ x: new Date(m.t).getTime(), y: m.price })), pointStyle: "triangle", pointRadius: 8, backgroundColor: good, borderColor: cssVar("--surface"), borderWidth: 2 });
      if (sells.length) ds.push({ label: "Sell", type: "scatter", data: sells.map((m) => ({ x: new Date(m.t).getTime(), y: m.price })), pointStyle: "triangle", rotation: 180, pointRadius: 8, backgroundColor: bad, borderColor: cssVar("--surface"), borderWidth: 2 });
      if (c.position.open && xs.length) ds.push({ label: "Stop-loss", data: [{ x: xs[0], y: c.position.stop_price }, { x: xs[xs.length - 1], y: c.position.stop_price }], borderColor: bad, borderDash: [6, 4], borderWidth: 1.5, pointRadius: 0 });
      state.charts.price = new Chart($("#price-chart"), { type: "line", data: { datasets: ds }, options: po });
    },
  };
};

PAGES.trades = async () => {
  const d = await api("/api/trades");
  const st = d.stats;
  const rows = d.trades.map((t) => `
    <div class="row"><div class="main">
      <div class="title">${t.net_pnl > 0 ? "Profit" : "Loss"} · ${esc(t.exit_type)}</div>
      <div class="meta">${when(t.entry_time)} → ${when(t.exit_time)} · ${t.held_hours ?? "–"} ghante · entry ${usd(t.entry_price, 0)} → exit ${usd(t.exit_price, 0)} · fees ${usd(t.fees)}</div>
      ${t.exit_reason ? `<div class="meta">Wajah: ${esc(t.exit_reason)}</div>` : ""}
      ${t.lesson ? `<div class="lesson" style="margin-top:6px"><b>Kyun hua:</b> ${esc(t.lesson.reason)}<br><b>Sabaq:</b> ${esc(t.lesson.lesson)}</div>` : (t.net_pnl < 0 ? `<div class="meta">AI analysis abhi baqi hai (Seekhna tab).</div>` : "")}
    </div><div class="side"><div class="${cls(t.net_pnl)}"><b>${signedUsd(t.net_pnl)}</b></div><div class="meta ${cls(t.pnl_percent)}">${pct(t.pnl_percent)}</div></div></div>`).join("");
  return {
    html: `
    <div class="grid">
      <div class="card stat span-3"><div class="label">Band trades</div><div class="value">${st.trades}</div><div class="delta sub">${st.wins} profit · ${st.losses} loss</div></div>
      <div class="card stat span-3"><div class="label">Win rate</div><div class="value">${st.win_rate === null ? "–" : num(st.win_rate, 0) + "%"}</div><div class="delta sub">Stop-loss lage: ${st.stop_outs}</div></div>
      <div class="card stat span-3"><div class="label">Average profit / loss</div><div class="value" style="font-size:20px"><span class="pos">${signedUsd(st.avg_win)}</span> / <span class="neg">${signedUsd(st.avg_loss)}</span></div><div class="delta sub">Profit factor: ${st.profit_factor === null ? "–" : num(st.profit_factor)}</div></div>
      <div class="card stat span-3"><div class="label">Fees ab tak</div><div class="value">${usd(st.fees_total)}</div><div class="delta sub">Sab se bara loss: ${signedUsd(st.biggest_loss)}</div></div>
      <div class="card span-12"><h2>Har trade</h2><div class="list">${rows || `<div class="empty">Abhi koi trade band nahi hui. Pehli trade tab hogi jab trend upar jaye.</div>`}</div></div>
    </div>`,
  };
};

PAGES.agents = async () => {
  const d = await api("/api/agents");
  const cards = d.agents.map((a) => `
    <div class="card span-6">
      <div class="card-head"><h3>${esc(a.agent)}</h3>${pill(a.status)}</div>
      <div class="sub">${esc(a.role)}</div>
      <p><b>Abhi:</b> ${esc(a.doing)}</p>
      <div class="kv">
        <div>Aakhri run</div><div>${when(a.last_run)} (${esc(ago(a.last_run))})</div>
        <div>Har kitni der baad</div><div>${Math.round(a.interval_seconds / 60) || "<1"} min</div>
        <div>Chal raha hai since</div><div>${when(a.started_at)}</div>
        <div>Runs / errors</div><div>${a.runs ?? "–"} / ${a.errors ?? "–"}</div>
        <div>Restarts</div><div>${a.restarts ?? "–"}</div>
      </div>
      ${a.last_error ? `<details style="margin-top:8px"><summary>Aakhri error (${esc(when(a.last_error_time))})</summary><pre style="white-space:pre-wrap;font-size:12.5px">${esc(a.last_error)}</pre></details>` : ""}
    </div>`).join("");
  return {
    html: `<div class="grid">
      <div class="card span-12 notice" style="border:0"><b>Active</b> = theek chal raha · <b>Error</b> = chal raha magar ghalti aa rahi · <b>Stopped</b> = ruk gaya (server par check karein) · <b>Pending</b> = abhi pehli baar chala nahi</div>
      ${cards}</div>`,
  };
};

PAGES.market = async () => {
  const m = await api("/api/market");
  const l = m.latest || {};
  const flag = m.anomaly_flag && Date.now() / 1000 - m.anomaly_flag.timestamp < 3600 ? m.anomaly_flag : null;
  const news = m.news.map((n) => `<div class="row"><div class="main"><div class="title">${n.link ? `<a href="${esc(n.link)}" target="_blank" rel="noopener noreferrer">${esc(n.title)}</a>` : esc(n.title)}</div><div class="meta">${esc(n.source)} · ${esc(ago(n.time))}</div></div></div>`).join("");
  const alerts = m.anomalies.map((a) => `<div class="row"><div class="main"><div class="title">${esc(a.reason)} ${a.forced_flat ? pill("bad", "bot flat kiya") : pill("warn", "sirf email")}</div><div class="meta">${esc(when(a.time))}</div><div class="meta" style="white-space:pre-wrap">${esc(a.details)}</div></div></div>`).join("");
  return {
    html: `<div class="grid">
      ${flag ? `<div class="card span-12" style="border-color:var(--bad-mark)"><b>Khatre ka alert active:</b> ${esc(flag.reason)}. Bot is waqt koi buy nahi karega.</div>` : ""}
      <div class="card stat span-3"><div class="label">${esc(m.symbol)}</div><div class="value">${usd(l.price, 0)}</div><div class="delta sub">${esc(ago(l.t))}</div></div>
      <div class="card stat span-3"><div class="label">24h high</div><div class="value">${usd(l.high, 0)}</div></div>
      <div class="card stat span-3"><div class="label">24h low</div><div class="value">${usd(l.low, 0)}</div></div>
      <div class="card stat span-3"><div class="label">24h volume</div><div class="value">${l.volume ? "$" + num(l.volume / 1e6, 0) + "M" : "–"}</div></div>
      <div class="card span-8"><h2>Latest news</h2><div class="list">${news || `<div class="empty">Abhi koi news nahi.</div>`}</div></div>
      <div class="card span-4"><h2>Khatre ke alerts</h2><div class="list">${alerts || `<div class="empty">Koi alert nahi aaya.</div>`}</div></div>
    </div>`,
  };
};

PAGES.decisions = async () => {
  const d = await api("/api/decisions");
  const list = state.decisionsOnlyChanges ? d.decisions.filter((x) => x.changed) : d.decisions;
  const rows = list.slice(0, 200).map((x) => `
    <div class="row"><div class="main">
      <div class="title">${x.position === 1 ? "LONG" : "FLAT"} ${x.changed ? pill("info", "badla") : ""} ${x.anomaly ? pill("bad", "alert") : ""}</div>
      <div class="meta">${esc(when(x.time))} · price ${usd(x.price, 0)} · ${x.headline_count ?? 0} headlines dekhi</div>
      <div style="font-size:14px">${esc(x.reason)}</div>
    </div></div>`).join("");
  return {
    html: `<div class="card">
      <div class="card-head"><h2>Agent ke faislay</h2>
        <div class="seg" id="dec-seg"><button data-v="0" class="${state.decisionsOnlyChanges ? "" : "active"}">Sab</button><button data-v="1" class="${state.decisionsOnlyChanges ? "active" : ""}">Sirf badlav</button></div></div>
      <div class="sub" style="margin-bottom:6px">Decision agent har 5 minute baad faisla karta hai. LONG = buy/hold, FLAT = bahar raho.</div>
      <div class="list">${rows || `<div class="empty">Koi faisla nahi mila.</div>`}</div></div>`,
    after: () => $("#dec-seg").addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      state.decisionsOnlyChanges = b.dataset.v === "1"; load();
    }),
  };
};

PAGES.learning = async () => {
  const d = await api("/api/learning");
  const s = d.status || {};
  const statusText = !d.key_set ? "Band: GEMINI_API_KEY set nahi" : (s.note || "Chal raha hai");
  const report = d.reports[0];
  const ruleText = (r) => (r ? `${esc(r.name)} = ${esc(r.value)}` : "");
  const bt = (b) => {
    if (!b) return "";
    if (b.error) return `<div class="meta">Backtest nahi ho saka: ${esc(b.error)}</div>`;
    const v = b.verdict === "better" ? pill("good", "Backtest: behtar") : b.verdict === "worse" ? pill("bad", "Backtest: bura") : pill("warn", "Backtest: farq nahi");
    return `<div style="margin-top:6px">${v}
      <div class="table-wrap"><table><tr><th></th><th>Abhi ka rule</th><th>Naya rule</th></tr>
      <tr><td>Return (2023 se ab)</td><td>${pct(b.base?.oos_return)}</td><td>${pct(b.new?.oos_return)}</td></tr>
      <tr><td>Sab se bara girao</td><td>${pct(b.base?.oos_max_dd)}</td><td>${pct(b.new?.oos_max_dd)}</td></tr>
      <tr><td>Return (2017 se ab)</td><td>${pct(b.base?.full_return)}</td><td>${pct(b.new?.full_return)}</td></tr>
      <tr><td>Trades</td><td>${b.base?.oos_trades ?? "–"}</td><td>${b.new?.oos_trades ?? "–"}</td></tr></table></div></div>`;
  };
  const lessons = d.lessons.map((l) => `
    <div class="row"><div class="main lesson">
      <div class="title">${esc(l.title || l.category || "Sabaq")}</div>
      <div class="meta"><span class="tag">${esc(l.source === "backtest" ? "Purana backtest" : l.source === "market" ? "Market report" : "Live trade")}</span>${esc(when(l.time))}${l.loss_usdt ? ` · loss ${signedUsd(l.loss_usdt)}` : ""}${l.market_type ? ` · market: ${esc(l.market_type)}` : ""}</div>
      ${l.reason ? `<p style="margin:6px 0"><b>Kyun hua:</b> ${esc(l.reason)}</p>` : ""}
      ${l.lesson ? `<p style="margin:6px 0"><b>Sabaq:</b> ${esc(l.lesson)}</p>` : ""}
      ${l.suggested_rule ? `<p style="margin:6px 0"><b>Mashwara:</b> ${ruleText(l.suggested_rule)} ${l.suggested_rule.why ? "· " + esc(l.suggested_rule.why) : ""}</p>${bt(l.backtest)}<div class="meta" style="margin-top:6px">Status: ${esc(l.status === "approved" ? "Approved" : "Aapki approval ka intezar (Approve button agle hisse mein)")}</div>` : ""}
    </div></div>`).join("");
  return {
    html: `<div class="grid">
      <div class="card span-12 notice" style="border:0">Seekhne wala system har loss aur roz ki market ko Gemini AI se parhta hai, sabaq likhta hai, aur naya rule suggest kare to pehle purane data par test karta hai. <b>Bot apne rules khud nahi badalta</b>: koi bhi rule aapki approval ke baad hi lagega.</div>
      <div class="card stat span-4"><div class="label">Status</div><div class="value" style="font-size:18px">${esc(statusText)}</div><div class="delta sub">Aaj Gemini calls: ${s.calls_today ?? 0} / ${s.max_calls ?? "–"}</div></div>
      <div class="card stat span-4"><div class="label">Sabaq ab tak</div><div class="value">${d.lessons.length}</div></div>
      <div class="card stat span-4"><div class="label">Aakhri run</div><div class="value" style="font-size:18px">${esc(ago(s.last_run))}</div><div class="delta sub">${s.last_error ? "Error: " + esc(s.last_error) : ""}</div></div>
      <div class="card span-12"><h2>Aaj ki market report</h2>${report ? `
        <div class="meta">${esc(when(report.time))} · mood: <b>${esc(report.mood)}</b></div>
        <p>${esc(report.summary)}</p>
        ${report.risks?.length ? `<b>Khatre:</b><ul>${report.risks.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
        ${report.watch ? `<p><b>Bot ko kis cheez par nazar rakhni chahiye:</b> ${esc(report.watch)}</p>` : ""}
        ${report.new_keywords?.length ? `<p><b>Naye khatre wale lafz (alert list mein add karne ka mashwara):</b> ${report.new_keywords.map((k) => `<span class="tag">${esc(k)}</span>`).join("")}</p>` : ""}`
        : `<div class="empty">Abhi koi report nahi. Gemini key lagne ke baad roz ek report banegi.</div>`}</div>
      <div class="card span-12"><h2>Seekhi hui baatein</h2><div class="list">${lessons || `<div class="empty">Abhi koi sabaq nahi.</div>`}</div></div>
    </div>`,
  };
};

PAGES.health = async () => {
  const d = await api("/api/health");
  const order = { bad: 0, warn: 1, info: 2, good: 3 };
  const icon = { bad: "Masla", warn: "Dhyan", info: "Info", good: "Theek" };
  const rows = d.checks.slice().sort((a, b) => order[a.level] - order[b.level]).map((c) => `
    <div class="row"><div style="width:70px">${pill(c.level, icon[c.level])}</div><div class="main"><div class="title">${esc(c.title)}</div><div class="meta" style="font-size:13.5px">${esc(c.detail)}</div></div></div>`).join("");
  return { html: `<div class="card"><h2>Project ki situation</h2><div class="list">${rows}</div></div>` };
};

PAGES.emails = async () => {
  const d = await api("/api/emails");
  const q = state.emailSearch.toLowerCase();
  const list = d.emails.filter((e) => (state.emailFilter === "all" || e.status === state.emailFilter) && (!q || `${e.subject} ${e.agent}`.toLowerCase().includes(q)));
  const counts = d.emails.reduce((a, e) => ((a[e.status] = (a[e.status] || 0) + 1), a), {});
  const statusPill = (s) => pill(s === "sent" ? "good" : s === "failed" ? "bad" : "warn", s === "sent" ? "Gayi" : s === "failed" ? "Fail" : "Nahi bheji");
  const rows = list.map((e) => `
    <div class="row"><div class="main"><div class="title">${esc(e.subject)}</div>
      <div class="meta">${esc(when(e.time))} · ${esc(e.agent)} · ${esc((e.to || []).join(", "))}</div>
      ${e.error ? `<div class="meta neg">${esc(e.error)}</div>` : ""}</div>
      <div class="side">${statusPill(e.status)}</div></div>`).join("");
  return {
    html: `<div class="card">
      <div class="card-head"><h2>Emails</h2><span class="sub">Gayi: ${counts.sent || 0} · Fail: ${counts.failed || 0} · Nahi bheji: ${counts.skipped || 0}</span></div>
      <div class="filters">
        <input id="email-search" placeholder="Subject ya agent search karein" value="${esc(state.emailSearch)}">
        <select id="email-filter">${[["all", "Sab"], ["sent", "Gayi"], ["failed", "Fail"], ["skipped", "Nahi bheji"]].map(([v, l]) => `<option value="${v}" ${v === state.emailFilter ? "selected" : ""}>${l}</option>`).join("")}</select>
      </div>
      <div class="list">${rows || `<div class="empty">Koi email nahi mili.</div>`}</div></div>`,
    after: () => {
      $("#email-filter").addEventListener("change", (e) => { state.emailFilter = e.target.value; load(true); });
      let t; $("#email-search").addEventListener("input", (e) => { clearTimeout(t); t = setTimeout(() => { state.emailSearch = e.target.value; load(true).then(() => { const i = $("#email-search"); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }); }, 300); });
    },
  };
};

PAGES.settings = async () => {
  const d = await api("/api/settings");
  setMode(d.mode);
  const groups = d.groups.map((g) => `<div class="card span-6"><h2>${esc(g.title)}</h2><div class="kv">${g.items.map((i) => `<div>${esc(i.key)}</div><div>${esc(i.value)}</div>`).join("")}</div></div>`).join("");
  const logins = (d.logins || []).map((l) => `<tr><td>${esc(when(l.time))}</td><td>${l.ok ? pill("good", "OK") : pill("bad", "Ghalat")}</td><td>${esc(l.user)}</td><td class="wrap">${esc(l.agent)}</td></tr>`).join("");
  return {
    html: `<div class="grid">
      <div class="card span-12 notice" style="border:0">Ye settings sirf dekhne ke liye hain. Badalne ke liye server par <code>sudo nano /etc/crypto-bot.env</code>, phir services restart. Passwords aur keys yahan kabhi nahi dikhte.</div>
      ${groups}
      <div class="card span-12"><h2>Aakhri logins</h2><div class="table-wrap"><table><tr><th>Time</th><th>Result</th><th>User</th><th>Device</th></tr>${logins || `<tr><td colspan="4" class="empty">Koi record nahi.</td></tr>`}</table></div></div>
    </div>`,
  };
};

// ---------- start ----------
(async function start() {
  const fromHash = location.hash.slice(1);
  let saved = null;
  try { saved = localStorage.getItem("tab"); } catch (_) {}
  state.tab = TABS.some(([id]) => id === fromHash) ? fromHash : TABS.some(([id]) => id === saved) ? saved : "home";
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  try {
    const me = await api("/api/me");
    if (me.user) showApp(); else showLogin();
  } catch (_) { showLogin(); }
})();
