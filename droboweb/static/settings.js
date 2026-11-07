"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---------- theme (same as the dashboard) ----------
  const root = document.documentElement;
  try { const t = localStorage.getItem("droboweb-theme"); if (t) root.dataset.theme = t; } catch (_) {}
  $("theme-btn").onclick = () => {
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("droboweb-theme", root.dataset.theme); } catch (_) {}
  };

  const form = $("form");
  let data = null;          // last GET /api/settings
  let fields = {};          // key -> field description
  const cleared = new Set(); // passwords the user asked to remove

  // ---------- render ----------
  function input(f, disabled) {
    const id = "f-" + f.key, dis = disabled ? " disabled" : "";
    if (f.kind === "bool") {
      return `<label class="switch"><input type="checkbox" id="${id}" name="${f.key}"${f.value ? " checked" : ""}${dis}><span></span></label>`;
    }
    if (f.kind === "choice") {
      return `<select id="${id}" name="${f.key}"${dis}>${f.choices.map((c) => `<option value="${esc(c.value)}"${c.value === f.value ? " selected" : ""}>${esc(c.label)}</option>`).join("")}</select>`;
    }
    const type = { int: "number", float: "number", password: "password", email: "text", url: "url" }[f.kind] || "text";
    const num = f.kind === "int" || f.kind === "float";
    const attrs = num ? ` min="${f.min}" max="${f.max}" step="${f.kind === "int" ? 1 : "any"}" inputmode="decimal"` : "";
    let ph = f.placeholder || "";
    if (f.kind === "password") ph = f.is_set ? "Saved. Type to replace it." : "Not set";
    const field = `<input type="${type}" id="${id}" name="${f.key}" value="${esc(f.value)}" placeholder="${esc(ph)}"${attrs}${dis}${f.kind === "password" ? ' autocomplete="new-password"' : ""} spellcheck="false">`;
    const unit = f.unit ? `<span class="unit">${esc(f.unit)}</span>` : "";
    const clear = f.kind === "password" && f.is_set && !disabled ? `<button type="button" class="link" data-clear="${f.key}">Remove</button>` : "";
    return `<span class="inwrap${num ? " num" : ""}">${field}${unit}${clear}</span>`;
  }

  function row(f, editable) {
    const locked = f.locked ? `<span class="lock">Set by the ${esc(f.locked)}</span>` : "";
    const restart = f.restart ? `<span class="tag">Needs a restart</span>` : "";
    return `<div class="row${f.kind === "bool" ? " bool" : ""}" data-row="${f.key}">
      <label for="f-${f.key}">${esc(f.label)} ${restart}</label>
      <div class="ctl">${input(f, !editable || !!f.locked)}
        ${f.help ? `<p class="help">${esc(f.help)}</p>` : ""}${locked}<p class="err" id="e-${f.key}" hidden></p></div>
    </div>`;
  }

  const TEST = { email: "email", webhook: "webhook" };

  function render() {
    fields = {};
    data.groups.forEach((g) => g.fields.forEach((f) => { fields[f.key] = f; }));
    cleared.clear();
    form.innerHTML = data.groups.map((g) => `
      <section class="card" id="g-${g.id}">
        <div class="card-head"><h2>${esc(g.title)}</h2>${TEST[g.id] && data.editable ? `<button type="button" class="btn small" data-test="${TEST[g.id]}">Send test alert</button>` : ""}</div>
        ${g.description ? `<p class="group-desc">${esc(g.description)}</p>` : ""}
        <p class="test-result" id="t-${g.id}" hidden></p>
        ${g.fields.map((f) => row(f, data.editable)).join("")}
      </section>`).join("");
    $("saved-to").textContent = data.saved_to ? `Saved to ${data.saved_to}` : "Demo mode: changes last until drobo-web stops";
    $("mode-badge").hidden = data.mode !== "demo";
    const b = $("banner");
    b.hidden = data.editable;
    b.textContent = data.reason || "";
    dirty();
  }

  // ---------- state ----------
  function valueOf(f) {
    const el = form.elements[f.key];
    if (!el) return undefined;
    if (f.kind === "bool") return el.checked;
    if (f.kind === "int" || f.kind === "float") return el.value.trim() === "" ? "" : Number(el.value);
    return el.value;
  }

  function changes() {
    const out = {};
    for (const f of Object.values(fields)) {
      if (f.locked) continue;
      if (f.kind === "password") {
        const v = valueOf(f);
        if (v) out[f.key] = v;
        else if (cleared.has(f.key)) out[f.key] = "";
        continue;
      }
      const v = valueOf(f);
      if (v !== undefined && v !== f.value) out[f.key] = v;
    }
    return out;
  }

  function dirty() {
    const n = Object.keys(changes()).length;
    $("savebar").hidden = n === 0;
    $("save-msg").textContent = n === 1 ? "1 unsaved change" : `${n} unsaved changes`;
    document.body.classList.toggle("has-savebar", n > 0);
  }

  function showErrors(errors) {
    form.querySelectorAll(".err").forEach((e) => { e.hidden = true; });
    form.querySelectorAll(".row.bad").forEach((e) => e.classList.remove("bad"));
    let first = null;
    for (const [k, msg] of Object.entries(errors || {})) {
      const e = $("e-" + k);
      if (!e) continue;
      e.textContent = `${fields[k] ? fields[k].label : k} ${msg}.`;
      e.hidden = false;
      e.closest(".row").classList.add("bad");
      first = first || e;
    }
    if (first) first.closest(".row").scrollIntoView({ behavior: "smooth", block: "center" });
  }

  // ---------- api ----------
  async function api(method, url, body) {
    const r = await fetch(url, { method, cache: "no-store", headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
    let j = {};
    try { j = await r.json(); } catch (_) {}
    return { ok: r.ok, status: r.status, j };
  }

  async function load() {
    const r = await api("GET", "api/settings");
    if (!r.ok) { const b = $("banner"); b.hidden = false; b.textContent = `Couldn't load the settings (${r.j.detail || r.status}).`; return; }
    data = r.j;
    render();
  }

  function toast(msg, cls = "") {
    const m = $("save-msg"); $("savebar").hidden = false; m.textContent = msg; m.className = cls;
    setTimeout(() => { m.className = ""; dirty(); }, 4000);
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const values = changes();
    if (!Object.keys(values).length) return;
    $("save").disabled = true;
    const r = await api("PUT", "api/settings", { values });
    $("save").disabled = false;
    if (r.status === 422) { showErrors(r.j.errors); toast("Fix the highlighted settings", "bad"); return; }
    if (!r.ok) { toast(`Not saved: ${r.j.detail || r.status}`, "bad"); return; }
    data = r.j;
    render();
    const restart = (r.j.restart || []).map((k) => fields[k] ? fields[k].label : k);
    toast(restart.length ? `Saved. Restart drobo-web to apply: ${restart.join(", ")}` : "Saved", "good");
  });

  form.addEventListener("input", dirty);
  form.addEventListener("change", dirty);
  form.addEventListener("click", async (e) => {
    const c = e.target.closest("[data-clear]");
    if (c) {
      cleared.add(c.dataset.clear);
      const el = form.elements[c.dataset.clear];
      el.value = ""; el.placeholder = "Will be removed when you save";
      c.remove(); dirty();
      return;
    }
    const t = e.target.closest("[data-test]");
    if (!t) return;
    const group = t.closest("section").id.slice(2), out = $("t-" + group);
    if (Object.keys(changes()).length) {
      out.hidden = false; out.className = "test-result bad"; out.textContent = "Save your changes first, so the test uses them.";
      return;
    }
    t.disabled = true; out.hidden = false; out.className = "test-result"; out.textContent = "Sending…";
    const r = await api("POST", `api/settings/test-alert?channel=${t.dataset.test}`, {});
    t.disabled = false;
    const res = r.ok ? r.j.results[t.dataset.test] : (r.j.detail || `error ${r.status}`);
    if (res === undefined) { out.className = "test-result bad"; out.textContent = "This channel isn't set up yet."; }
    else if (res === "ok") { out.className = "test-result good"; out.textContent = "Sent. Check that it arrived."; }
    else { out.className = "test-result bad"; out.textContent = `Failed: ${res}`; }
  });
  $("revert").onclick = () => { render(); showErrors({}); };
  addEventListener("beforeunload", (e) => { if (data && Object.keys(changes()).length) { e.preventDefault(); e.returnValue = ""; } });

  load();
})();
