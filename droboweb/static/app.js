"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---------- formatting ----------
  const dec = (n) => {
    if (n == null) return "—";
    const u = ["B", "KB", "MB", "GB", "TB", "PB"]; let i = 0;
    while (Math.abs(n) >= 1000 && i < u.length - 1) { n /= 1000; i++; }
    return (i ? n.toFixed(n >= 100 ? 0 : n >= 10 ? 1 : 2) : n) + " " + u[i];
  };
  const bin = (n) => {
    if (n == null) return "—";
    const u = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]; let i = 0;
    while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (Number.isInteger(n) ? n : n.toFixed(1)) + " " + u[i];
  };
  const ago = (ts) => {
    if (!ts) return "never";
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 5) return "just now";
    if (s < 60) return Math.round(s) + "s ago";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    if (s < 86400) return Math.round(s / 3600) + "h ago";
    return Math.round(s / 86400) + "d ago";
  };
  const when = (ts) => new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });

  const LED_CLASS = { green: "green", yellow: "yellow", red: "red", "red-green": "red flash-rg", "red-yellow": "red flash-ry", "red-off": "red flash" };
  const LED_TEXT = { green: "OK", yellow: "Replace soon / nearly full", red: "Failed — replace this drive", "red-green": "Rebuilding — do not remove", "red-yellow": "Warning — check unit", "red-off": "Failed", off: "Off", empty: "Empty" };
  const PALETTE = ["#2f8cff", "#22b866", "#e0a800", "#e5484d", "#9b6bff", "#14b8c4", "#ff7a45", "#8f9bab"];

  // ---------- theme ----------
  const root = document.documentElement;
  try { const t = localStorage.getItem("droboweb-theme"); if (t) root.dataset.theme = t; } catch (_) {}
  $("theme-btn").onclick = () => {
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("droboweb-theme", root.dataset.theme); } catch (_) {}
    if (lastHistory) drawHistory(lastHistory);
  };

  // ---------- render: status ----------
  let relayoutMax = 0;

  function renderBays(s) {
    const bays = s.bays || [];
    const present = bays.filter((b) => b.present);
    $("bay-summary").textContent = `${present.length} of ${bays.length || s.slot_count} bays · ${dec(present.reduce((a, b) => a + b.physical_bytes, 0))} raw`;
    $("chassis").innerHTML = bays.map((b) => {
      const n = b.slot + 1;
      if (!b.present) return `<div class="bay empty"><span class="num">${n}</span><span class="model">Empty</span><span class="meta"></span><i class="led" title="Empty"></i></div>`;
      const temp = b.temperature_c == null ? "" : `<span class="${b.temperature_c >= (thresholds.temp_warn_c || 50) ? "hot" : "cool"}">${b.temperature_c} °C</span>`;
      return `<div class="bay" title="${esc(LED_TEXT[b.led] || b.led)}">
        <span class="num">${n}</span>
        <span class="model">${esc(b.model)}</span>
        <span class="meta"><span>${dec(b.physical_bytes)}</span>${temp}</span>
        <i class="led ${LED_CLASS[b.led] || ""}" aria-label="${esc(LED_TEXT[b.led] || b.led)}"></i>
      </div>`;
    }).join("");
    const pct = s.total_bytes ? s.used_bytes / s.total_bytes : 0;
    const lit = Math.min(10, Math.ceil(pct * 10 - 1e-9));
    $("caplights").innerHTML = Array.from({ length: 10 }, (_, i) => `<i class="${i < lit ? "on" : ""}"></i>`).join("");
  }

  function renderCapacity(s) {
    const pct = s.used_pct ?? 0;
    const bar = $("cap-used");
    bar.style.width = pct + "%";
    bar.className = pct >= 95 ? "crit" : pct >= (thresholds.capacity_warn_pct || 85) ? "warn" : "";
    $("cap-pct").textContent = pct + "% used";
    $("cap-used-v").textContent = dec(s.used_bytes);
    $("cap-free-v").textContent = dec(s.free_bytes);
    $("cap-total-v").textContent = dec(s.total_bytes);
    $("cap-unprot-v").textContent = dec(s.unprotected_bytes);
  }

  function renderStatus(s) {
    const lines = [];
    const sev = { ok: "ok", warning: "warning", critical: "critical", rebuilding: "warning" }[s.health] || "info";
    const headline = {
      ok: ["Data protected", "All drives healthy. The unit can survive a drive failure."],
      rebuilding: ["Re-protecting data", "The Drobo is rebuilding redundancy. Don't remove any drives."],
      warning: ["Needs attention", "Check the drive lights and the flags below."],
      critical: ["Critical", "A drive has failed or data is at risk. Check the red bay."],
    }[s.health] || ["Unknown", ""];
    lines.push(`<div class="status-line ${sev}"><span class="dot"></span><div><b>${headline[0]}</b><span class="muted">${headline[1]}</span></div></div>`);
    const benign = ["Data protection in progress", "Format in progress", "New firmware installed", "New volume available after reboot"];
    for (const st of s.status || []) {
      if (st === "Data protection in progress") continue;
      const cls = /Red|Bad|Too many/.test(st) ? "critical" : benign.includes(st) || st.startsWith("Unknown") ? "info" : "warning";
      lines.push(`<div class="status-line ${cls}"><span class="dot"></span><div><b>${esc(st)}</b></div></div>`);
    }
    if (s.relayout_remaining > 0) {
      relayoutMax = Math.max(relayoutMax, s.relayout_remaining);
      const done = relayoutMax ? 100 * (1 - s.relayout_remaining / relayoutMax) : 0;
      lines.push(`<div class="status-line warning"><span class="dot"></span><div style="flex:1"><b>Data protection in progress</b><span class="muted">${s.relayout_remaining.toLocaleString()} blocks left${done > 0.5 ? ` · ${done.toFixed(1)}% since this page opened` : ""}</span><div class="progress"><div style="width:${done}%"></div></div></div></div>`);
    } else relayoutMax = 0;
    const empty = (s.bays || []).filter((b) => !b.present).length;
    if (empty) lines.push(`<div class="status-line info"><span class="dot"></span><div><b>${empty} empty bay${empty > 1 ? "s" : ""}</b><span class="muted">Add drives to grow capacity — no reformat needed.</span></div></div>`);
    for (const e of s.errors || []) lines.push(`<div class="status-line info"><span class="dot"></span><div><b>Partial read</b><span class="muted">${esc(e)}</span></div></div>`);
    $("status-body").innerHTML = lines.join("");
  }

  function renderVolumes(s) {
    const vols = s.volumes || [];
    $("vol-summary").textContent = vols.length ? `${vols.length} volume${vols.length > 1 ? "s" : ""} · ${bin(vols.reduce((a, v) => a + v.size_bytes, 0))} provisioned` : "";
    $("vol-body").innerHTML = vols.length ? vols.map((v) => {
      const use = v.used_bytes == null
        ? `<span class="muted small" title="The host using this volume hasn't reported usage to the Drobo">not reported</span>`
        : `<div class="usebar"><div class="bar"><div style="width:${Math.min(100, 100 * v.used_bytes / v.size_bytes)}%"></div></div><span>${dec(v.used_bytes)}</span></div>`;
      return `<tr><td class="muted">${v.index}</td><td><b>${esc(v.name || "(unnamed)")}</b></td><td class="num">${bin(v.size_bytes)}</td><td>${use}</td><td class="iqn" title="${esc(v.iqn)}">${esc(v.iqn)}</td></tr>`;
    }).join("") : `<tr><td colspan="5" class="muted">No volumes reported.</td></tr>`;
  }

  function renderPorts(s) {
    const ports = s.ports || [];
    $("ports").innerHTML = ports.length ? ports.map((p) => `<div class="port"><span class="p">${p.port}</span><span class="ip">${esc(p.ip)}/${p.prefix}</span>
      <span class="d">gateway ${esc(p.gateway)} · MTU ${p.mtu}${p.mtu >= 9000 ? " (jumbo)" : ""}</span></div>`).join("") : `<p class="muted">Not reported.</p>`;
  }

  function renderUnit(s) {
    // the unit keeps local time; tz_offset is hours west of UTC
    const drift = s.clock_utc ? Math.round(s.clock_utc + (s.tz_offset || 0) * 3600 - s.taken_at) : null;
    const rows = [
      ["Model", "Drobo B800i"],
      ["Serial", s.serial || "—", true],
      ["Firmware", s.firmware || "—"],
      ["Built", s.firmware_build_date || "—"],
      ["Protocol", s.protocol || "—"],
      ["Clock", drift == null ? "—" : Math.abs(drift) < 120 ? `in sync (UTC−${s.tz_offset})` : `${drift > 0 ? "+" : ""}${Math.round(drift / 60)} min off`],
      ["Max volume", bin(s.max_volume_bytes)],
      ["Platform", s.arch || "—"],
    ];
    $("unit-kv").innerHTML = rows.map(([k, v, mono]) => `<div><dt>${k}</dt><dd class="${mono ? "mono" : ""}">${esc(v)}</dd></div>`).join("");
  }

  let thresholds = {};
  function render(data) {
    const m = data.monitor, s = data.snapshot;
    thresholds = m.thresholds || {};
    $("ver").textContent = "v" + data.version;
    $("mode-badge").hidden = m.mode !== "demo";
    const banner = $("banner");
    if (!m.reachable) {
      banner.hidden = false;
      banner.className = "banner";
      banner.innerHTML = `<b>Can't reach the Drobo.</b> ${esc(m.last_error || "Waiting for the first poll…")}${s ? ` Showing the last data, from ${esc(ago(s.taken_at))}.` : ""}`;
    } else banner.hidden = true;
    const pill = $("health-pill");
    if (!s) { pill.textContent = m.reachable ? "—" : "Offline"; pill.className = "pill offline"; return; }
    const health = m.reachable ? s.health : "offline";
    pill.textContent = { ok: "Healthy", warning: "Warning", critical: "Critical", rebuilding: "Rebuilding", offline: "Offline" }[health];
    pill.className = "pill " + health;
    document.title = `${health === "ok" ? "" : "⚠ "}${s.name || "Drobo"} · Drobo Web`;
    $("unit-name").textContent = s.name || "Drobo";
    $("unit-sub").textContent = `B800i · firmware ${s.firmware} · ${s.source}`;
    $("updated").textContent = "updated " + ago(m.last_ok);
    renderBays(s); renderCapacity(s); renderStatus(s); renderVolumes(s); renderPorts(s); renderUnit(s);
  }

  function renderEvents(list) {
    $("events").innerHTML = list.length ? list.slice(0, 50).map((e) => `<li class="${esc(e.severity)}"><span class="dot"></span><div>${esc(e.message)}<span class="t">${when(e.ts)}</span></div></li>`).join("")
      : `<li><span></span><span class="muted">Nothing yet.</span></li>`;
  }

  // ---------- charts ----------
  function cssVar(n) { return getComputedStyle(root).getPropertyValue(n).trim(); }

  function lineChart(el, series, { yfmt = "auto", unit = "", yPad = 0.1, minSpan = 1 } = {}) {
    const W = el.clientWidth || 400, H = el.clientHeight || 190, L = 48, R = 8, T = 8, B = 22;
    const pts = series.flatMap((s) => s.points);
    if (pts.length < 2) { el.innerHTML = `<svg viewBox="0 0 ${W} ${H}"><text class="empty" x="${W / 2}" y="${H / 2}" text-anchor="middle">Collecting data — a point is stored every minute.</text></svg>`; return; }
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
    let x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
    if (y1 - y0 < minSpan) { const c = (y0 + y1) / 2; y0 = c - minSpan / 2; y1 = c + minSpan / 2; }
    const pad = (y1 - y0) * yPad; y0 -= pad; y1 += pad;
    if (x1 === x0) x1 = x0 + 1;
    const X = (x) => L + (x - x0) / (x1 - x0) * (W - L - R), Y = (y) => T + (1 - (y - y0) / (y1 - y0)) * (H - T - B);
    let g = "";
    if (yfmt === "auto") {
      const step = (y1 - y0) / 4, d = Math.max(0, Math.min(4, Math.ceil(-Math.log10(step || 1))));
      yfmt = (v) => v.toFixed(d) + (unit || "");
    }
    for (let i = 0; i <= 4; i++) {
      const v = y0 + (y1 - y0) * i / 4, y = Y(v);
      g += `<line x1="${L}" x2="${W - R}" y1="${y}" y2="${y}"/>`;
      g += `<text x="${L - 6}" y="${y + 3.5}" text-anchor="end">${yfmt(v)}</text>`;
    }
    const span = x1 - x0, ticks = W < 420 ? 2 : 4;
    let xt = "";
    for (let i = 0; i <= ticks; i++) {
      const t = x0 + span * i / ticks, d = new Date(t * 1000);
      const lab = span > 2 * 86400 ? d.toLocaleDateString([], { month: "short", day: "numeric" }) : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      xt += `<text x="${X(t)}" y="${H - 5}" text-anchor="${i === 0 ? "start" : i === ticks ? "end" : "middle"}">${lab}</text>`;
    }
    const paths = series.map((s) => {
      if (!s.points.length) return "";
      const d = s.points.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("");
      const area = s.fill ? `<path d="${d}L${X(s.points.at(-1)[0])},${Y(y0)}L${X(s.points[0][0])},${Y(y0)}Z" fill="${s.color}" opacity=".12"/>` : "";
      return `${area}<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    }).join("");
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><g class="grid">${g.replace(/<text[^>]*>[^<]*<\/text>/g, "")}</g><g class="axis">${g.match(/<text[^>]*>[^<]*<\/text>/g).join("")}${xt}</g>${paths}</svg>`;
  }

  let lastHistory = null;
  function drawHistory(h) {
    lastHistory = h;
    const pts = h.points || [];
    lineChart($("chart-cap"), [{ color: cssVar("--blue"), fill: true, points: pts.map((p) => [p.ts, p.used / 1e12]) }],
      { unit: " TB", minSpan: 0.05 });
    const slots = [...new Set(pts.flatMap((p) => Object.keys(p.temps)))].sort((a, b) => a - b);
    const series = slots.map((sl, i) => ({ name: `Bay ${+sl + 1}`, color: PALETTE[i % PALETTE.length], points: pts.filter((p) => p.temps[sl] != null).map((p) => [p.ts, p.temps[sl]]) }));
    lineChart($("chart-temp"), series, { unit: "°", minSpan: 6 });
    $("temp-legend").innerHTML = series.map((s) => `<span><i style="background:${s.color}"></i>${s.name}</span>`).join("");
  }

  // ---------- data loop ----------
  let hours = 24;
  try { hours = +localStorage.getItem("droboweb-hours") || 24; } catch (_) {}
  for (const b of $("range").querySelectorAll("button")) {
    b.classList.toggle("on", +b.dataset.h === hours);
    b.onclick = () => {
      hours = +b.dataset.h;
      try { localStorage.setItem("droboweb-hours", hours); } catch (_) {}
      for (const o of $("range").querySelectorAll("button")) o.classList.toggle("on", o === b);
      loadHistory();
    };
  }

  async function j(url) { const r = await fetch(url, { cache: "no-store" }); if (!r.ok) throw new Error(r.status); return r.json(); }
  async function loadStatus() {
    try { render(await j("api/status")); }
    catch (e) { const b = $("banner"); b.hidden = false; b.className = "banner"; b.textContent = "Can't reach the drobo-web server (" + e.message + ")."; }
  }
  async function loadHistory() { try { drawHistory(await j(`api/history?hours=${hours}`)); } catch (_) {} }
  async function loadEvents() { try { renderEvents((await j("api/events?limit=50")).events); } catch (_) {} }

  loadStatus(); loadHistory(); loadEvents();
  setInterval(loadStatus, 5000);
  setInterval(loadEvents, 10000);
  setInterval(loadHistory, 60000);
  let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => lastHistory && drawHistory(lastHistory), 150); });
})();
