const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const selected = new Set();   // agents checked to receive messages
const rendered = {};          // name -> signature of last render, to keep scroll position stable

async function api(path, body) {
  const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  const data = await r.json();
  if (!r.ok) toast(data.error || r.statusText);
  return data;
}
// popover = top layer, so it shows above the open start dialog
function toast(msg) { const t = $("#toast"); t.textContent = msg; if (!t.matches(":popover-open")) t.showPopover();
  clearTimeout(t._h); t._h = setTimeout(() => t.hidePopover(), 4000); }

function renderLog(log, a) {
  log.replaceChildren();
  for (const m of a.messages) {
    if (m.role === "system") continue;
    if (m.role === "user") log.append(el("div", "msg user", m.content));
    else if (m.role === "tool") log.append(el("div", "msg tool", "→ " + String(m.content).slice(0, 400)));
    else {
      if (m.content) log.append(el("div", "msg", m.content));
      for (const c of m.tool_calls || []) log.append(el("div", "msg call", `⚙ ${c.function.name}(${c.function.arguments})`));
    }
  }
  if (a.error) log.append(el("div", "msg err", "error: " + a.error));
  if (a.busy) log.append(el("div", "msg call", "…"));
  log.scrollTop = log.scrollHeight;
}

function renderKeys(keys) {
  const box = $("#keys");
  for (const [k, set] of Object.entries(keys)) {
    let row = box.querySelector(`.key[data-key="${k}"]`);
    if (!row) {  // build once, so polling doesn't wipe what's being typed
      row = el("form", "key"); row.dataset.key = k;
      const label = el("label"); label.append(el("span", "dot"), el("span", null, k));
      const input = el("input"); input.type = "password"; input.autocomplete = "off"; input.setAttribute("aria-label", k);
      const save = el("button", null, "Save");
      row.onsubmit = async (e) => {
        e.preventDefault();
        const r = await api("/api/key", { env: k, value: input.value });
        if (r.ok) input.value = "";
        refresh();
      };
      row.append(label, input, save); box.append(row);
    }
    row.querySelector(".dot").classList.toggle("off", !set);
    row.querySelector("input").placeholder = set ? "설정됨 (비우고 Save = 삭제)" : "키 입력";
  }
}

async function loadModels(provider) {
  const list = $(`#models-${CSS.escape(provider)}`);
  if (list.dataset.loaded) return;
  list.dataset.loaded = "1";
  for (const id of await api(`/api/models?provider=${encodeURIComponent(provider)}`)) list.append(el("option", null)).value = id;
}

const dlg = $("#start-dialog");
let starting = null;  // agent info the dialog is starting

function openStart(a) {
  starting = a;
  const f = dlg.querySelector("form");
  f.querySelector("h3").textContent = `${a.name} · ${a.provider}`;
  if (!$(`#models-${CSS.escape(a.provider)}`)) { const dl = el("datalist"); dl.id = `models-${a.provider}`; document.body.append(dl); }
  const m = f.model;
  m.setAttribute("list", `models-${a.provider}`); m.value = "";
  m.placeholder = a.model || "모델 입력 (필수)"; m.required = !a.model;
  m.onfocus = () => loadModels(a.provider);
  dlg.showModal();
}

function renderAvailable(state, runningNames) {
  const av = $("#available");
  av.querySelectorAll(":scope > :not([data-name])").forEach(e => e.remove());  // server-rendered placeholders
  $("#workspaces").replaceChildren(...state.workspaces.map(w => { const o = el("option"); o.value = w; return o; }));
  for (const a of state.available) {
    let row = av.querySelector(`[data-name="${CSS.escape(a.name)}"]`);
    if (!row) {
      row = el("button", "avail"); row.dataset.name = a.name;
      row.append(el("span", null, a.name), el("small", null, a.provider));
      av.append(row);
    }
    row.onclick = () => openStart(a);  // latest info (default model may change)
    const on = runningNames.includes(a.name);
    row.classList.toggle("on", on);
    row.disabled = on;
  }
}

function render(state) {
  if ($("#keys")) renderKeys(state.keys);
  if (!$("#running")) return;  // settings page
  const runningNames = state.running.map(a => a.name);
  renderAvailable(state, runningNames);

  const main = $("#running");
  for (const n of [...selected]) if (!runningNames.includes(n)) selected.delete(n);
  for (const card of [...main.querySelectorAll(".card")]) if (!runningNames.includes(card.dataset.name)) { card.remove(); delete rendered[card.dataset.name]; }
  main.querySelector(".empty")?.remove();
  if (!state.running.length) main.append(el("p", "empty", "왼쪽에서 에이전트를 시작하세요."));

  for (const a of state.running) {
    let card = main.querySelector(`.card[data-name="${CSS.escape(a.name)}"]`);
    if (!card) {
      card = el("section", "card"); card.dataset.name = a.name;
      const head = el("header");
      const cb = el("input"); cb.type = "checkbox"; cb.checked = selected.has(a.name); cb.title = "메시지 받기";
      cb.onchange = () => cb.checked ? selected.add(a.name) : selected.delete(a.name);
      const meta = el("div", "meta"); meta.append(el("b", null, a.name), el("small", null, `${a.workspace} · ${a.provider} / ${a.model}`));
      const reset = el("button", null, "Reset"); reset.onclick = () => api("/api/reset", { name: a.name }).then(refresh);
      const stop = el("button", null, "Stop"); stop.onclick = () => api("/api/stop", { name: a.name }).then(refresh);
      head.append(cb, el("span", "dot"), meta, reset, stop);
      card.append(head, el("div", "log"));
      main.append(card);
    }
    card.querySelector(".dot").classList.toggle("busy", a.busy);
    const sig = JSON.stringify([a.messages.length, a.busy, a.error]);
    if (rendered[a.name] !== sig) { rendered[a.name] = sig; renderLog(card.querySelector(".log"), a); }
  }
}

async function refresh() { try { render(await api("/api/state")); } catch (e) { /* server restarting */ } }

async function send() {
  const text = $("#input").value.trim();
  if (!text) return;
  if (!selected.size) return toast("메시지를 받을 에이전트를 체크하세요.");
  const r = await api("/api/send", { names: [...selected], text });
  if (r.ok) $("#input").value = "";
  refresh();
}
if ($("#running")) {
  $("#send").onclick = send;
  $("#input").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
  dlg.querySelector("[value=cancel]").onclick = () => dlg.close();
  dlg.querySelector("form").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    const r = await api("/api/start", { name: starting.name, workspace: f.workspace.value, model: f.model.value });
    if (r.ok) { selected.add(starting.name); dlg.close(); }
    refresh();
  };
}

refresh(); setInterval(refresh, 1000);
