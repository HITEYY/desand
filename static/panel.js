const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const opt = (value, text, selected) => { const o = el("option", null, text ?? value); o.value = value; o.selected = !!selected; return o; };
const selected = new Set();   // agents checked to receive messages
const rendered = {};          // name -> signature of last render, to keep scroll position stable
let state = null;

async function api(path, body) {
  const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  if (r.status === 401) { location.replace("/login"); return {}; }
  const data = await r.json();
  if (!r.ok) toast(data.error || r.statusText);
  return data;
}
// popover = top layer, so it shows above an open dialog
function toast(msg, ok) { const t = $("#toast"); t.textContent = msg; t.classList.toggle("ok", !!ok);
  if (!t.matches(":popover-open")) t.showPopover();
  clearTimeout(t._h); t._h = setTimeout(() => t.hidePopover(), 4000); }

function row(title, sub, ...actions) {
  const r = el("div", "row"), meta = el("div", "meta");
  meta.append(el("b", null, title), el("small", null, sub));
  r.append(meta, ...actions);
  return r;
}
function button(text, onclick, cls) { const b = el("button", cls || "ghost", text); b.type = "button"; b.onclick = onclick; return b; }
function emptyNote(box, text) { if (!box.children.length) box.append(el("p", "empty", text)); }

$("#logout").onclick = () => api("/api/logout", {}).then(() => location.replace("/login"));

// ---------- chat page ----------

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
  if (a.busy) log.append(el("div", "msg typing", "생각 중…"));
  if (!log.children.length) log.append(el("p", "empty", "아래 입력창에서 메시지를 보내세요."));
  log.scrollTop = log.scrollHeight;
}

function renderRunning() {
  const main = $("#running"), names = state.running.map(a => a.name);
  for (const n of [...selected]) if (!names.includes(n)) selected.delete(n);
  for (const card of [...main.querySelectorAll(".card")]) if (!names.includes(card.dataset.name)) { card.remove(); delete rendered[card.dataset.name]; }
  main.querySelector(":scope > .empty")?.remove();
  if (!state.running.length) main.append(el("p", "empty", "실행 중인 에이전트가 없습니다. 오른쪽 위 “에이전트 시작”을 누르세요."));

  for (const a of state.running) {
    let card = main.querySelector(`.card[data-name="${CSS.escape(a.name)}"]`);
    if (!card) {
      card = el("section", "card"); card.dataset.name = a.name;
      const head = el("header");
      const pick = el("label", "pick"); pick.title = "메시지 받기";
      const cb = el("input"); cb.type = "checkbox"; cb.checked = selected.has(a.name);
      cb.onchange = () => { cb.checked ? selected.add(a.name) : selected.delete(a.name); card.classList.toggle("selected", cb.checked); };
      pick.append(cb);
      const meta = el("div", "meta");
      meta.append(el("b", null, a.name), el("small", null, `${a.workspace} · ${a.provider} / ${a.model}`));
      head.append(pick, el("span", "dot"), meta,
        button("초기화", () => api("/api/reset", { name: a.name }).then(refresh)),
        button("중지", () => api("/api/stop", { name: a.name }).then(refresh), "ghost danger"));
      card.append(head, el("div", "log"));
      card.classList.toggle("selected", selected.has(a.name));
      main.append(card);
    }
    card.querySelector(".dot").classList.toggle("busy", a.busy);
    const sig = JSON.stringify([a.messages.length, a.busy, a.error]);
    if (rendered[a.name] !== sig) { rendered[a.name] = sig; renderLog(card.querySelector(".log"), a); }
  }
}

function openStart() {
  const dlg = $("#start-dialog"), f = dlg.querySelector("form");
  const running = state.running.map(a => a.name);
  const idle = state.available.filter(a => !running.includes(a.name));
  if (!idle.length) return toast("시작할 수 있는 에이전트가 없습니다. 에이전트 메뉴에서 만드세요.");
  f.agent.replaceChildren(...idle.map(a => opt(a.name, `${a.name}${a.custom ? "" : " (파일)"}`)));
  f.workspace.replaceChildren(...state.workspaces.map(w => opt(w.name, null, w.name === "default")));
  const fillModels = () => {
    const a = idle.find(x => x.name === f.agent.value);
    const def = a.model ? `기본값 (${a.provider} / ${a.model})` : `기본값 없음 — 모델을 고르세요`;
    f.model.replaceChildren(opt("", def), ...state.models.map(m => opt(m.name, `${m.name} (${m.provider} / ${m.model})`)));
    f.model.required = !a.model;
  };
  f.agent.onchange = fillModels; fillModels();
  dlg.showModal();
}

async function send() {
  const text = $("#input").value.trim();
  if (!text) return;
  if (!selected.size) return toast("메시지를 받을 에이전트를 체크하세요.");
  const r = await api("/api/send", { names: [...selected], text });
  if (r.ok) { $("#input").value = ""; autosize(); }
  refresh();
}
function autosize() { const t = $("#input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 200) + "px"; }

if ($("#running")) {
  const dlg = $("#start-dialog");
  $("#open-start").onclick = openStart;
  $("#send").onclick = send;
  $("#input").addEventListener("input", autosize);
  $("#input").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
  dlg.querySelector("[value=cancel]").onclick = () => dlg.close();
  dlg.querySelector("form").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    const r = await api("/api/start", { name: f.agent.value, workspace: f.workspace.value, model: f.model.value });
    if (r.ok) { selected.add(f.agent.value); dlg.close(); }
    refresh();
  };
}

// ---------- agents page ----------

function renderAgents() {
  const f = $("#agent-form");
  const keep = f.model.value;
  f.model.replaceChildren(opt("", state.models.length ? "모델 선택" : "먼저 모델 메뉴에서 모델을 만드세요"),
    ...state.models.map(m => opt(m.name, `${m.name} (${m.provider} / ${m.model})`, m.name === keep)));
  const box = $("#tool-list");
  if (!box.children.length) for (const t of state.tools) {
    const l = el("label", "check"), cb = el("input"); cb.type = "checkbox"; cb.name = "tools"; cb.value = t; cb.defaultChecked = true;
    l.append(cb, el("span", null, t)); box.append(l);
  }
  const list = $("#agent-list"); list.replaceChildren();
  for (const a of state.available) {
    const sub = `${a.custom ? a.preset : "agents/" + a.name + ".py"} · ${a.provider} / ${a.model ?? "모델 미지정"}`;
    list.append(a.custom
      ? row(a.name, sub, button("편집", () => editAgent(a)), button("삭제", async () => {
          if (confirm(`${a.name} 에이전트를 삭제할까요?`)) { await api("/api/agents/delete", { name: a.name }); refresh(); }
        }, "ghost danger"))
      : row(a.name, sub, el("span", "badge", "파일")));
  }
  emptyNote(list, "에이전트가 없습니다.");
}

function editAgent(a) {
  const f = $("#agent-form");
  $("#agent-form-title").textContent = `편집: ${a.name}`;
  f.name.value = a.name; f.model.value = a.preset; f.system.value = a.system || "";
  for (const cb of f.querySelectorAll("[name=tools]")) cb.checked = (a.tools || []).includes(cb.value);
  f.name.focus();
}

if ($("#agent-form")) {
  const f = $("#agent-form");
  f.onreset = () => { $("#agent-form-title").textContent = "새 에이전트"; };
  f.onsubmit = async (e) => {
    e.preventDefault();
    const tools = [...f.querySelectorAll("[name=tools]:checked")].map(cb => cb.value);
    const r = await api("/api/agents", { name: f.name.value, model: f.model.value, system: f.system.value, tools });
    if (r.ok) { toast(`${f.name.value} 저장됨`, true); f.reset(); }
    refresh();
  };
}

// ---------- models page ----------

function renderModels() {
  const f = $("#model-form");
  if (!f.provider.options.length) {
    f.provider.append(...Object.entries(state.providers).map(([n, p]) => opt(n, `${n}${p.default ? ` (기본 ${p.default})` : ""}`)));
    f.provider.dispatchEvent(new Event("change"));
  }
  const list = $("#model-list"); list.replaceChildren();
  for (const m of state.models) {
    const users = state.available.filter(a => a.custom && a.preset === m.name).map(a => a.name);
    list.append(row(m.name, `${m.provider} / ${m.model}${users.length ? ` · 사용: ${users.join(", ")}` : ""}`,
      button("편집", () => { f.name.value = m.name; f.provider.value = m.provider; f.provider.dispatchEvent(new Event("change")); f.model.value = m.model; }),
      button("삭제", async () => { await api("/api/models/delete", { name: m.name }); refresh(); }, "ghost danger")));
  }
  emptyNote(list, "저장한 모델이 없습니다.");
}

if ($("#model-form")) {
  const f = $("#model-form"), dl = $("#provider-models"), cache = {};
  f.provider.onchange = () => {
    const p = state.providers[f.provider.value];
    f.model.placeholder = p.default || "모델 ID";
    dl.replaceChildren();
  };
  f.model.onfocus = async () => {
    const p = f.provider.value;
    cache[p] ??= api(`/api/models?provider=${encodeURIComponent(p)}`);
    const ids = await cache[p];
    if (f.provider.value === p) dl.replaceChildren(...(Array.isArray(ids) ? ids : []).map(id => opt(id)));
  };
  f.onsubmit = async (e) => {
    e.preventDefault();
    const r = await api("/api/models", { name: f.name.value, provider: f.provider.value, model: f.model.value });
    if (r.ok) { toast(`${f.name.value} 저장됨`, true); f.name.value = f.model.value = ""; }
    refresh();
  };
}

// ---------- workspaces page ----------

function renderWorkspaces() {
  const list = $("#workspace-list"); list.replaceChildren();
  for (const w of state.workspaces) {
    const sub = `workspaces/${w.name}/ · 항목 ${w.files}개${w.agents.length ? ` · 실행 중: ${w.agents.join(", ")}` : ""}`;
    list.append(row(w.name, sub, button("삭제", async () => {
      if (!confirm(`${w.name} 워크스페이스와 안의 파일 ${w.files}개를 모두 지울까요? 되돌릴 수 없습니다.`)) return;
      await api("/api/workspaces/delete", { name: w.name }); refresh();
    }, "ghost danger")));
  }
  emptyNote(list, "워크스페이스가 없습니다.");
}

if ($("#workspace-form")) {
  $("#workspace-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target, r = await api("/api/workspaces", { name: f.name.value });
    if (r.ok) { toast(`${f.name.value} 만듦`, true); f.reset(); }
    refresh();
  };
}

// ---------- API keys page ----------

function renderKeys() {
  const box = $("#keys");
  for (const [k, set] of Object.entries(state.keys)) {
    let r = box.querySelector(`[data-key="${k}"]`);
    if (!r) {  // build once, so refreshes don't wipe what's being typed
      const providers = Object.entries(state.providers).filter(([, p]) => p.key === k).map(([n]) => n).join(", ") || "S3 업로드";
      const input = el("input"); input.type = "password"; input.autocomplete = "off"; input.setAttribute("aria-label", k);
      const form = el("form", "inline");
      form.append(input, el("button", "primary", "저장"));
      form.onsubmit = async (e) => {
        e.preventDefault();
        const res = await api("/api/key", { env: k, value: input.value });
        if (res.ok) { input.value = ""; toast(`${k} 저장됨`, true); }
        refresh();
      };
      r = row(k, providers, form); r.dataset.key = k;
      r.querySelector(".meta b").prepend(el("span", "dot"));
      box.append(r);
    }
    r.querySelector(".dot").classList.toggle("off", !set);
    r.querySelector("input").placeholder = set ? "설정됨 · 비우고 저장 = 삭제" : "키 입력";
  }
  $("#s3-target").textContent = state.s3 ? `trajectories/*.jsonl → ${state.s3}` : "S3_BUCKET 을 설정하세요 (S3_PREFIX 기본 trajectories)";
  $("#s3-upload").disabled = !state.s3;
}

if ($("#s3-upload")) {
  $("#s3-upload").onclick = async (e) => {
    e.target.disabled = true;
    const r = await api("/api/s3/upload", {});
    if (r.ok) toast(`${r.count}개 파일 업로드됨 → ${r.target}`, true);
    refresh();
  };
}

// ---------- refresh ----------

async function refresh() {
  try { state = await api("/api/state"); } catch (e) { return; /* server restarting */ }
  if (!state.running) return;
  if ($("#running")) renderRunning();
  if ($("#agent-form")) renderAgents();
  if ($("#model-form")) renderModels();
  if ($("#workspace-form")) renderWorkspaces();
  if ($("#keys")) renderKeys();
}

refresh();
if ($("#running")) setInterval(refresh, 1000);  // management pages refresh after each action instead
