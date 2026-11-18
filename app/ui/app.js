/* OutreachPilot operator console. Plain DOM, no build step, no inline code (strict CSP). */
"use strict";

// ---- small utilities ------------------------------------------------------------------------
const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === false || value === null || value === undefined) continue;
    if (key === "class") el.className = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const store = {
  get(key) { try { return window.localStorage.getItem(key); } catch { return null; } },
  set(key, value) {
    try { value === null ? window.localStorage.removeItem(key) : window.localStorage.setItem(key, value); } catch { /* private mode */ }
  },
};

const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
function ago(iso) {
  if (!iso) return "";
  const seconds = (new Date(iso).getTime() - Date.now()) / 1000;
  const steps = [[60, "second"], [60, "minute"], [24, "hour"], [7, "day"], [4.35, "week"], [12, "month"], [Infinity, "year"]];
  let value = seconds;
  for (const [size, unit] of steps) {
    if (Math.abs(value) < size) return rtf.format(Math.round(value), unit);
    value /= size;
  }
  return "";
}
const fullTime = (iso) => (iso ? new Date(iso).toLocaleString() : "");
const hostOf = (url) => { try { return new URL(url).host; } catch { return ""; } };
const humanize = (s) => String(s || "").replace(/[._]/g, " ").replace(/^\w/, (c) => c.toUpperCase());

// ---- API ------------------------------------------------------------------------------------
class ApiError extends Error {
  constructor(code, message, details, status) {
    super(message);
    this.code = code; this.details = details || {}; this.status = status;
  }
}

async function api(method, path, body) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const key = store.get("op_api_key");
  if (key) headers["X-API-Key"] = key;
  let res;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError("network", "Can't reach the OutreachPilot server. Check that it is running.", {}, 0);
  }
  if (res.status === 204) return null;
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) {
    const err = (data && data.error) || { code: `http_${res.status}`, message: `The server answered ${res.status}.` };
    if (res.status === 401) openKeyDialog();
    throw new ApiError(err.code, err.message, err.details, res.status);
  }
  return data;
}

function explain(err) {
  if (err.code === "request_validation_error" && Array.isArray(err.details.errors)) {
    return err.details.errors.map((e) => `${(e.loc || []).filter((p) => p !== "body").join(".") || "request"}: ${e.msg}`).join("; ");
  }
  return err.message || "Something went wrong.";
}

// ---- vocabulary -----------------------------------------------------------------------------
const STATUS = {
  imported: { label: "Imported", tone: "" },
  validated: { label: "Ready to research", tone: "" },
  researching: { label: "Researching", tone: "live" },
  researched: { label: "Researched", tone: "" },
  generating: { label: "Writing draft", tone: "live" },
  pending_approval: { label: "Needs review", tone: "review" },
  approved: { label: "Approved", tone: "good" },
  rejected: { label: "Rejected", tone: "bad" },
  executing: { label: "Sending", tone: "live" },
  completed: { label: "Sent", tone: "done" },
  failed: { label: "Failed", tone: "bad" },
  blocked: { label: "Blocked", tone: "warn" },
};
const BUSY = new Set(["researching", "generating", "executing"]);
const FILTERS = [
  { id: "review", label: "Needs review", statuses: ["pending_approval"] },
  { id: "todo", label: "To do", statuses: ["imported", "validated", "researched", "rejected", "approved"] },
  { id: "running", label: "Running", statuses: ["researching", "generating", "executing"] },
  { id: "problems", label: "Problems", statuses: ["failed", "blocked"] },
  { id: "sent", label: "Sent", statuses: ["completed"] },
  { id: "all", label: "All", statuses: [] },
];
const EMPTY = {
  review: "Nothing needs review right now.",
  todo: "Nothing is waiting for you.",
  running: "No tasks are running.",
  problems: "No failed or blocked prospects.",
  sent: "No messages sent yet.",
};
const STEPS = ["Imported", "Researched", "Drafted", "Approved", "Sent"];
const STAGE_STEP = { research: 1, generate_message: 2, execute: 4 };
const TASK_LABEL = { research: "Researching the profile", generate_message: "Writing the draft", execute: "Sending the message" };
const FACT_LABEL = {
  name: "Name", job_title: "Job title", company: "Company", headline: "Headline", location: "Location",
  about: "About", recent_activity: "Recent activity", skills: "Skills",
};
const MESSAGE_STATUS = {
  pending_approval: "needs review", approved: "approved", rejected: "rejected", superseded: "replaced", sent: "sent",
};
const EVENTS = {
  "prospect.imported": ["Imported", ""],
  "prospect.validated": ["Checked and ready for research", ""],
  "prospect.updated": ["Details edited", ""],
  "research.requested": ["Research started", ""],
  "research.completed": ["Profile researched", "good"],
  "research.failed": ["Research failed", "bad"],
  "research.blocked": ["The site restricted access during research", "warn"],
  "research.cancelled": ["Research cancelled", "bad"],
  "generate_message.requested": ["Draft requested", ""],
  "message.generated": ["Draft written", "good"],
  "generate_message.failed": ["Draft could not be written", "bad"],
  "generate_message.blocked": ["Draft blocked", "warn"],
  "generate_message.cancelled": ["Draft cancelled", "bad"],
  "message.approved": ["Draft approved", "good"],
  "message.rejected": ["Draft rejected", "bad"],
  "execute.requested": ["Sending started", ""],
  "message.sent": ["Message sent", "good"],
  "execute.failed": ["Sending failed", "bad"],
  "execute.blocked": ["The site restricted access while sending", "warn"],
  "execute.cancelled": ["Sending cancelled", "bad"],
  "workflow.interrupted": ["Interrupted by a server restart", "bad"],
};
const ERROR_TITLE = {
  access_blocked: "The site restricted access",
  robots_disallowed: "robots.txt does not allow this page",
  host_not_allowed: "This site is not on the allow-list",
  automation_timeout: "The page took too long to respond",
  navigation_error: "The page could not be loaded",
  page_not_found: "The profile page does not exist",
  login_failed: "Signing in to the portal failed",
  data_mismatch: "The page belongs to someone else",
  element_not_found: "The page layout was not recognised",
  message_validation_failed: "The model kept producing drafts that failed the fact check",
  llm_unavailable: "The language model is unavailable",
  llm_auth_error: "The language model rejected the API key",
  task_cancelled: "The task was cancelled",
  interrupted: "The server restarted while this was running",
};

// ---- state ----------------------------------------------------------------------------------
const state = {
  view: "all",
  q: "",
  items: [],
  total: 0,
  counts: {},
  selectedId: null,
  detail: null,
  messages: [],
  events: [],
  viewVersion: null,
  compare: false,
  rejecting: false,
  pollTimer: null,
  pollTask: null,
};

// ---- top bar --------------------------------------------------------------------------------
async function loadHealth() {
  const env = $("#env");
  try {
    const health = await fetch("/health").then((r) => r.json());
    const model = health.config.llm_mode === "openai" ? health.config.llm_model : "the offline mock model";
    const portal = health.config.demo_portal_url;
    env.replaceChildren(h("span", { class: `dot${health.status === "ok" ? "" : " bad"}` }),
      health.status === "ok" ? `Connected. Drafts are written by ${model}.` : "The database is unavailable.",
      portal ? h("a", {
        class: "portal-link", href: portal, target: "_blank", rel: "noopener noreferrer",
        title: "The fictional website the browser automation signs in to. You don't need to sign in there.",
      }, "Practice website") : null);
  } catch {
    env.replaceChildren(h("span", { class: "dot bad" }), "Can't reach the server.");
  }
}

// ---- list -----------------------------------------------------------------------------------
function filterById(id) { return FILTERS.find((f) => f.id === id) || FILTERS[FILTERS.length - 1]; }

function renderFilters() {
  const root = $("#filters");
  root.replaceChildren(...FILTERS.map((f) => {
    const n = f.statuses.length ? f.statuses.reduce((sum, s) => sum + (state.counts[s] || 0), 0)
      : Object.values(state.counts).reduce((a, b) => a + b, 0);
    return h("button", {
      type: "button", role: "tab", class: `filter ${f.id}`, "aria-selected": String(state.view === f.id),
      onclick: () => { state.view = f.id; syncHash(); renderFilters(); loadList(); },
    }, f.label, n || f.id === "review" ? h("span", { class: "n" }, n) : null);
  }));
}

async function loadStats() {
  try {
    const stats = await api("GET", "/stats");
    state.counts = stats.by_status;
    renderFilters();
  } catch (err) { if (err.status !== 401) toast(explain(err), true); }
}

async function loadList() {
  const params = new URLSearchParams({ sort: "-updated", limit: "100" });
  for (const s of filterById(state.view).statuses) params.append("status", s);
  if (state.q) params.set("q", state.q);
  try {
    const data = await api("GET", `/prospects?${params}`);
    state.items = data.items;
    state.total = data.total;
    renderList();
  } catch (err) { if (err.status !== 401) toast(explain(err), true); }
}

function statusPill(status) {
  const meta = STATUS[status] || { label: humanize(status), tone: "" };
  return h("span", { class: `pill ${meta.tone}` }, meta.label);
}

function renderList() {
  const list = $("#plist");
  if (!state.items.length) {
    const nothingAtAll = !Object.values(state.counts).some(Boolean) && !state.q;
    list.replaceChildren(h("li", { class: "rail-empty" },
      nothingAtAll ? "No prospects yet. Add one, or import a CSV to start."
        : state.q ? `No prospects match "${state.q}".` : EMPTY[state.view] || "No prospects here.",
      nothingAtAll ? h("div", {}, h("button", { type: "button", class: "btn primary", onclick: () => openDialog("add-dialog") }, "Add prospect")) : null));
  } else {
    list.replaceChildren(...state.items.map((p) => h("li", {},
      h("button", {
        type: "button", class: "pitem", "aria-current": String(p.id === state.selectedId),
        onclick: () => select(p.id),
      },
      h("span", { class: "who" }, p.name),
      statusPill(p.status),
      h("span", { class: "what" }, [p.job_title, p.company].filter(Boolean).join(" at ") || hostOf(p.source_url) || "No details"),
      h("span", { class: "when", title: fullTime(p.updated_at) }, ago(p.updated_at))))));
  }
  const shown = state.items.length;
  $("#rail-foot").textContent = state.total > shown ? `Showing ${shown} of ${state.total}` : `${state.total} ${state.total === 1 ? "prospect" : "prospects"}`;
}

// ---- routing --------------------------------------------------------------------------------
function syncHash() {
  const target = `#/${state.view}${state.selectedId ? `/${state.selectedId}` : ""}`;
  if (location.hash !== target) history.replaceState(null, "", target);
}

function readHash() {
  const [, view, id] = location.hash.split("/");
  if (view && FILTERS.some((f) => f.id === view)) state.view = view;
  const n = Number(id);
  return Number.isInteger(n) && n > 0 ? n : null;
}

async function select(id) {
  state.selectedId = id;
  state.viewVersion = null;
  state.compare = false;
  state.rejecting = false;
  syncHash();
  renderList();
  $("#layout").classList.add("showing-detail");
  await loadDetail(id, { focus: true });
}

function clearSelection() {
  stopPolling();
  state.selectedId = null;
  state.detail = null;
  syncHash();
  $("#layout").classList.remove("showing-detail");
  $("#prospect").hidden = true;
  $("#placeholder").hidden = false;
  renderList();
}

// ---- detail ---------------------------------------------------------------------------------
async function loadDetail(id, { focus = false } = {}) {
  try {
    const [detail, messages, events, tasks] = await Promise.all([
      api("GET", `/prospects/${id}`),
      api("GET", `/prospects/${id}/messages`),
      api("GET", `/prospects/${id}/events`),
      api("GET", `/prospects/${id}/tasks`),
    ]);
    if (state.selectedId !== id) return;
    state.detail = detail;
    state.messages = messages;
    state.events = events;
    renderDetail();
    const active = tasks.find((t) => !["succeeded", "failed", "cancelled"].includes(t.status));
    if (BUSY.has(detail.status) && active) startPolling(active);
    else stopPolling();
    if (focus) $("#detail").focus({ preventScroll: true });
  } catch (err) {
    if (err.status === 404) { toast("That prospect no longer exists.", true); clearSelection(); loadList(); loadStats(); return; }
    if (err.status !== 401) toast(explain(err), true);
  }
}

async function refreshAll() {
  await Promise.all([loadStats(), loadList(), state.selectedId ? loadDetail(state.selectedId) : null]);
}

function stepState(detail) {
  const s = detail.status;
  const stage = detail.last_error && detail.last_error.stage;
  const table = {
    imported: [1, 1, "now"], validated: [1, 1, "now"], researching: [1, 1, "live"], researched: [2, 2, "now"],
    generating: [2, 2, "live"], pending_approval: [3, 3, "now"], rejected: [2, 2, "now"], approved: [4, 4, "now"],
    executing: [4, 4, "live"], completed: [5, -1, ""],
  };
  if (s === "failed" || s === "blocked") {
    const at = STAGE_STEP[stage] ?? 1;
    return [at, at, s === "blocked" ? "warn" : "fail"];
  }
  return table[s] || [0, 0, "now"];
}

function renderSteps(detail) {
  const [done, current, kind] = stepState(detail);
  return h("ol", { class: "steps", "aria-label": "Progress" }, STEPS.map((label, i) =>
    h("li", { class: `step${i < done ? " done" : ""}${i === current ? ` ${kind}` : ""}`, "aria-current": i === current ? "step" : null }, label)));
}

function renderHeader(d) {
  return h("header", {},
    h("button", { type: "button", class: "btn quiet small back", onclick: clearSelection }, "Back to list"),
    h("div", { class: "p-head" },
      h("div", {},
        h("h1", { id: "p-name" }, d.name),
        h("p", { class: "sub" }, [d.job_title, d.company].filter(Boolean).join(" at ") || "No title or company",
          d.source_url ? h("a", { href: d.source_url, target: "_blank", rel: "noopener noreferrer" }, "Open profile") : null)),
      h("div", { class: "right" },
        statusPill(d.status),
        h("button", { type: "button", class: "btn quiet small", disabled: BUSY.has(d.status), onclick: deleteProspect }, "Delete"))));
}

function renderNotice(d) {
  if (BUSY.has(d.status)) return h("section", { class: "notice live", id: "task-panel", "aria-live": "polite" }, renderTaskPanel());
  if ((d.status === "failed" || d.status === "blocked") && d.last_error) {
    const e = d.last_error;
    const lines = [];
    if (e.details && e.details.attempts) lines.push(`Attempts: ${e.details.attempts}`);
    if (e.details && e.details.trace) lines.push(`Browser trace saved to ${e.details.trace}`);
    return h("section", { class: `notice ${d.status === "blocked" ? "warn" : "bad"}` },
      h("h3", {}, ERROR_TITLE[e.code] || humanize(e.code)),
      h("p", {}, e.message),
      lines.length ? h("p", { class: "meta" }, lines.join(". ")) : null);
  }
  return null;
}

function renderTaskPanel() {
  const t = state.pollTask;
  const d = state.detail;
  const kind = { researching: "research", generating: "generate_message", executing: "execute" }[d.status];
  const children = [h("h3", {}, TASK_LABEL[kind] || "Working")];
  if (t) {
    const retrying = t.status === "retrying";
    children.push(h("p", {}, retrying ? `Attempt ${t.attempts} of ${t.max_attempts} failed; trying again shortly.`
      : t.status === "queued" ? "Waiting for a free worker." : `Attempt ${Math.max(t.attempts, 1)} of ${t.max_attempts}.`));
  }
  children.push(h("div", { class: "progress", "aria-hidden": "true" }, h("span", {})));
  if (t && t.history && t.history.length > 1) {
    children.push(h("ul", { class: "history" }, t.history.slice(-5).map((ev) =>
      h("li", {}, h("span", { class: "t" }, new Date(ev.at).toLocaleTimeString()), ev.message, ev.error ? `: ${ev.error.message}` : ""))));
  }
  children.push(h("div", { class: "actions" }, t ? h("button", { type: "button", class: "btn small", onclick: (e) => cancelTask(e.currentTarget) }, "Cancel task") : null));
  return children;
}

// ---- facts & evidence -------------------------------------------------------------------------
function factsFor(d) {
  const facts = {};
  for (const key of ["name", "job_title", "company"]) if (d[key]) facts[key] = d[key];
  for (const [key, value] of Object.entries(d.research_data || {})) {
    if (["screenshot", "profile_url"].includes(key)) continue;
    if (value === null || value === "" || (Array.isArray(value) && !value.length)) continue;
    facts[key] = value;
  }
  return facts;
}

function evidencePhrases(facts) {
  const phrases = [];
  const add = (text, key) => {
    const t = String(text || "").trim();
    if (t.length >= 3) phrases.push({ text: t, key });
  };
  for (const [key, value] of Object.entries(facts)) {
    if (Array.isArray(value)) value.forEach((v) => add(v, key));
    else if (key === "name") { add(value, key); add(String(value).split(/\s+/)[0], key); }
    else if (key === "location") { add(value, key); String(value).split(",").forEach((part) => add(part, key)); }
    else add(value, key);
  }
  return phrases.sort((a, b) => b.text.length - a.text.length);
}

function markEvidence(text, facts) {
  const found = [];
  const lower = text.toLowerCase();
  for (const { text: phrase, key } of evidencePhrases(facts)) {
    const needle = phrase.toLowerCase();
    let from = 0;
    while (from < lower.length) {
      const at = lower.indexOf(needle, from);
      if (at < 0) break;
      const end = at + needle.length;
      const boundary = (i) => i <= 0 || i >= lower.length || !/[\p{L}\p{N}]/u.test(lower[i]);
      if (boundary(at - 1) && boundary(end) && !found.some((f) => at < f.end && end > f.start)) found.push({ start: at, end, key });
      from = end;
    }
  }
  found.sort((a, b) => a.start - b.start);
  const nodes = [];
  let pos = 0;
  for (const f of found) {
    if (f.start > pos) nodes.push(text.slice(pos, f.start));
    nodes.push(h("mark", {
      class: "ev", tabindex: "0", dataset: { fact: f.key }, title: `From the profile: ${FACT_LABEL[f.key] || humanize(f.key)}`,
    }, text.slice(f.start, f.end)));
    pos = f.end;
  }
  if (pos < text.length) nodes.push(text.slice(pos));
  return nodes;
}

function renderFacts(facts, used) {
  const keys = Object.keys(facts);
  const box = h("aside", { class: "facts", "aria-label": "Facts from the profile" }, h("h3", {}, "Facts from the profile"));
  if (!keys.length) { box.append(h("p", { class: "none" }, "No research yet.")); return box; }
  const dl = h("dl", {});
  for (const key of keys) {
    const value = facts[key];
    let shown;
    if (key === "skills" && Array.isArray(value)) shown = h("div", { class: "tags" }, value.map((v) => h("span", { class: "tag" }, v)));
    else if (Array.isArray(value)) shown = h("ul", {}, value.map((v) => h("li", {}, v)));
    else shown = String(value);
    dl.append(h("div", { class: "fact", dataset: { key } },
      h("dt", {}, used.has(key) ? h("span", { class: "used", title: "Used in this draft" }) : null, FACT_LABEL[key] || humanize(key)),
      h("dd", {}, shown)));
  }
  box.append(dl);
  return box;
}

function linkHover(root) {
  const toggle = (key, on) => root.querySelectorAll(`[data-key="${CSS.escape(key)}"], mark[data-fact="${CSS.escape(key)}"]`)
    .forEach((el) => el.classList.toggle("hot", on));
  root.querySelectorAll("mark.ev").forEach((m) => {
    const key = m.dataset.fact;
    for (const [on, events] of [[true, ["mouseenter", "focus"]], [false, ["mouseleave", "blur"]]]) {
      events.forEach((ev) => m.addEventListener(ev, () => toggle(key, on)));
    }
  });
}

// ---- word diff ------------------------------------------------------------------------------
function wordDiff(before, after) {
  const a = before.split(/(\s+)/);
  const b = after.split(/(\s+)/);
  const n = a.length; const m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) {
    dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  }
  const out = [];
  let i = 0; let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push(a[i]); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push(h("del", {}, a[i])); i++; }
    else { out.push(h("ins", {}, b[j])); j++; }
  }
  while (i < n) out.push(h("del", {}, a[i++]));
  while (j < m) out.push(h("ins", {}, b[j++]));
  return out;
}

// ---- draft & actions ------------------------------------------------------------------------
function currentMessage() {
  if (!state.messages.length) return null;
  return state.messages.find((m) => m.id === state.viewVersion) || state.messages[0];
}

function renderLetter(msg, facts) {
  const latest = state.messages[0];
  const previous = state.messages.find((m) => m.version === msg.version - 1);
  const body = state.compare && previous
    ? h("div", { class: "body diff" }, wordDiff(previous.body, msg.body))
    : h("div", { class: "body" }, markEvidence(msg.body, facts));
  const stamps = [];
  if (msg.status === "approved") stamps.push(h("span", { class: "stamp good" }, `Approved by ${msg.reviewer}`));
  if (msg.status === "rejected") stamps.push(h("span", { class: "stamp bad" }, `Rejected by ${msg.reviewer}`));
  if (msg.status === "sent") stamps.push(h("span", { class: "stamp done" }, `Sent ${ago(msg.sent_at)}`));
  return h("div", { class: "letter" },
    msg.id !== latest.id ? h("p", { class: "notice warn" }, `This is version ${msg.version}, which has been ${MESSAGE_STATUS[msg.status] || msg.status}. `,
      h("button", { type: "button", class: "btn link", onclick: () => { state.viewVersion = null; state.compare = false; renderDetail(); } }, "Show the latest version")) : null,
    h("p", { class: "subject" }, h("span", { class: "k" }, "Subject"), msg.subject),
    body,
    msg.status === "rejected" && msg.review_reason ? h("p", { class: "notice bad" }, `Reason: ${msg.review_reason}`) : null,
    h("div", { class: "foot" },
      stamps,
      state.compare ? h("span", {}, `Changes since version ${msg.version - 1}`)
        : h("span", { class: "legend" }, h("mark", { class: "ev" }, "Highlighted"), " text comes from the researched profile."),
      h("span", {}, `Version ${msg.version}, ${humanize(msg.tone)} tone, ${msg.generation_attempts === 1 ? "passed the fact check first time" : `passed the fact check after ${msg.generation_attempts} tries`}`),
      previous ? h("label", {}, h("input", { type: "checkbox", checked: state.compare, onchange: (e) => { state.compare = e.target.checked; renderDetail(); } }), ` Show changes from version ${previous.version}`) : null));
}

function toneSelect(id) {
  return h("div", { class: "inline-field" },
    h("label", { for: id }, "Tone"),
    h("select", { id }, ["professional", "friendly", "concise"].map((t) => h("option", { value: t }, humanize(t)))));
}

function renderActions(d, { dock = false } = {}) {
  const allowed = new Set(d.allowed_next_statuses || []);
  const latest = state.messages[0];
  const stage = d.last_error && d.last_error.stage;
  const bar = h("div", { class: dock ? "actions dock" : "actions", role: "group", "aria-label": "Actions" });
  if (BUSY.has(d.status) || d.status === "completed") return null;

  if (d.status === "pending_approval") {
    bar.append(
      h("div", { class: "inline-field" }, h("label", { for: "reviewer" }, "Your name"),
        h("input", { id: "reviewer", value: store.get("op_reviewer") || "", maxlength: "120", autocomplete: "name", onchange: (e) => store.set("op_reviewer", e.target.value.trim()) })),
      h("button", { type: "button", class: "btn approve", onclick: (e) => decide("approve", e.currentTarget) }, "Approve"),
      h("button", { type: "button", class: "btn", "aria-expanded": String(state.rejecting), onclick: () => { state.rejecting = !state.rejecting; renderDetail(); if (state.rejecting) $("#reject-reason").focus(); } }, "Reject"),
      h("span", { class: "grow" }),
      toneSelect("tone"),
      h("button", { type: "button", class: "btn quiet", onclick: (e) => generate(e.currentTarget) }, "Rewrite"));
    if (state.rejecting) {
      bar.append(h("div", { class: "reject-box" },
        h("label", { for: "reject-reason" }, "Why are you rejecting this draft? The reason is saved with it."),
        h("textarea", { id: "reject-reason", rows: "2", maxlength: "1000" }),
        h("div", { class: "row" },
          h("button", { type: "button", class: "btn quiet", onclick: () => { state.rejecting = false; renderDetail(); } }, "Keep reviewing"),
          h("button", { type: "button", class: "btn danger", onclick: (e) => decide("reject", e.currentTarget) }, "Reject draft"))));
    }
    return bar;
  }

  const canSend = allowed.has("executing") && latest && latest.status === "approved";
  if (canSend) {
    bar.append(h("button", { type: "button", class: "btn primary", onclick: (e) => execute(e.currentTarget) },
      stage === "execute" ? "Try sending again" : "Send message"));
  }
  if (allowed.has("generating") && d.research_data) {
    if (!canSend) {
      bar.append(toneSelect("tone"),
        h("div", { class: "inline-field instructions" }, h("label", { for: "extra" }, "Instructions for the writer (optional)"),
          h("input", { id: "extra", maxlength: "500", placeholder: "For example: keep it under 80 words" })));
    }
    bar.append(h("button", { type: "button", class: canSend ? "btn quiet" : "btn primary", onclick: (e) => generate(e.currentTarget) },
      latest ? (canSend ? "Rewrite instead" : "Write a new draft") : "Write draft"));
  }
  if (allowed.has("researching") && d.source_url) {
    const first = !d.research_data;
    bar.append(h("button", { type: "button", class: first ? "btn primary" : "btn quiet", onclick: (e) => research(e.currentTarget) },
      first ? (d.status === "validated" ? "Research profile" : "Try research again") : "Research again"));
  }
  if (allowed.has("researching") && !d.source_url) {
    bar.append(h("p", { class: "hint" }, "Add a profile URL to research this prospect."));
  }
  return bar.childNodes.length ? bar : null;
}

function renderVersions() {
  if (state.messages.length < 2) return null;
  const current = currentMessage();
  return h("section", { class: "section" },
    h("h2", {}, "Versions", h("span", { class: "aside" }, "Every draft is kept, newest first")),
    h("ul", { class: "versions" }, state.messages.map((m) => h("li", {},
      h("button", {
        type: "button", class: "version", "aria-pressed": String(m.id === current.id),
        onclick: () => { state.viewVersion = m.id; state.compare = false; renderDetail(); },
      }, h("b", {}, `Version ${m.version}`), h("span", {}, `${humanize(m.tone)}, ${MESSAGE_STATUS[m.status] || m.status}`))))));
}

function eventDetail(ev) {
  const d = ev.detail || {};
  const bits = [];
  if (d.version) bits.push(`version ${d.version}`);
  if (d.tone) bits.push(`${d.tone} tone`);
  if (d.confirmation_id) bits.push(`confirmation ${d.confirmation_id}`);
  if (d.attempts && d.attempts > 1) bits.push(`${d.attempts} attempts`);
  return bits.join(", ");
}

function renderActivity() {
  const items = state.events.filter((ev) => EVENTS[ev.event_type] !== undefined || !ev.event_type.startsWith("task.")).slice().reverse();
  return h("section", { class: "section" },
    h("h2", {}, "Activity"),
    items.length ? h("ol", { class: "timeline" }, items.map((ev) => {
      const [label, tone] = EVENTS[ev.event_type] || [humanize(ev.event_type), ""];
      const d = ev.detail || {};
      const extra = eventDetail(ev);
      return h("li", { class: tone },
        h("div", { class: "what" }, label),
        h("div", { class: "meta", title: fullTime(ev.created_at) }, `${ago(ev.created_at)} by ${ev.actor}${extra ? `, ${extra}` : ""}`),
        d.reason ? h("div", { class: "why" }, `Reason: ${d.reason}`) : null,
        d.error && d.error.message ? h("div", { class: "why" }, d.error.message) : null);
    })) : h("p", { class: "hint" }, "No activity yet."));
}

function renderDetail() {
  const d = state.detail;
  if (!d) return;
  const facts = factsFor(d);
  const msg = currentMessage();
  const root = $("#prospect");
  const sections = [renderHeader(d), renderSteps(d), renderNotice(d)];

  if (msg) {
    const used = new Set(msg.facts_used || []);
    sections.push(h("section", { class: "section", "aria-labelledby": "draft-title" },
      h("h2", { id: "draft-title" }, msg.status === "sent" ? "Sent message" : "Draft",
        h("span", { class: "aside" }, msg.status === "pending_approval" ? "Check it against the facts, then approve or reject" : "")),
      h("div", { class: "desk" }, renderLetter(msg, facts), renderFacts(facts, used)),
      msg.id === state.messages[0].id ? renderActions(d, { dock: true }) : null));
    if (msg.execution_result) {
      const r = msg.execution_result;
      sections.push(h("section", { class: "notice good" },
        h("h3", {}, r.duplicate ? "Already sent earlier; the portal returned the original confirmation" : "Delivered through the portal's message form"),
        h("p", {}, `Confirmation ${r.confirmation_id} for ${r.recipient}.`),
        r.screenshot ? h("p", { class: "meta" }, `Screenshot saved to ${r.screenshot}`) : null));
    }
  } else {
    sections.push(h("section", { class: "section" },
      h("h2", {}, "Profile research", h("span", { class: "aside" }, d.research_data ? `From ${hostOf(d.source_url)}` : "")),
      d.research_data ? h("div", { class: "research-grid" }, renderFacts(facts, new Set())) : null,
      renderActions(d)));
  }
  sections.push(renderVersions());
  if (d.notes) sections.push(h("section", { class: "section" }, h("h2", {}, "Internal notes", h("span", { class: "aside" }, "Never sent to the model")), h("p", { class: "hint" }, d.notes)));
  sections.push(renderActivity());

  root.replaceChildren(...sections.filter(Boolean));
  root.hidden = false;
  $("#placeholder").hidden = true;
  linkHover(root);
}

// ---- polling --------------------------------------------------------------------------------
function startPolling(task) {
  stopPolling();
  state.pollTask = task;
  const tick = async () => {
    try {
      const t = await api("GET", `/tasks/${task.id}`);
      state.pollTask = t;
      if (["succeeded", "failed", "cancelled"].includes(t.status)) {
        stopPolling();
        const verb = { research: "Research", generate_message: "Draft", execute: "Sending" }[t.type] || "Task";
        if (t.status === "succeeded") toast(`${verb} finished.`);
        else if (t.status === "cancelled") toast(`${verb} cancelled.`);
        else toast(`${verb} failed: ${(t.error && t.error.message) || "see details"}`, true);
        await refreshAll();
        return;
      }
      const panel = $("#task-panel");
      if (panel) panel.replaceChildren(...renderTaskPanel());
    } catch (err) {
      if (err.status === 404) { stopPolling(); return; }
    }
    state.pollTimer = window.setTimeout(tick, 1000);
  };
  state.pollTimer = window.setTimeout(tick, 600);
}

function stopPolling() {
  if (state.pollTimer) window.clearTimeout(state.pollTimer);
  state.pollTimer = null;
  state.pollTask = null;
}

// ---- commands -------------------------------------------------------------------------------
async function withBusy(button, fn) {
  if (button) { button.disabled = true; button.classList.add("busy"); }
  try { return await fn(); } finally { if (button && button.isConnected) { button.disabled = false; button.classList.remove("busy"); } }
}

async function startTask(button, path, body, message) {
  const id = state.selectedId;
  await withBusy(button, async () => {
    try {
      const task = await api("POST", path, body);
      toast(message);
      await refreshAll();
      if (state.selectedId === id) startPolling(task);
    } catch (err) { toast(explain(err), true); }
  });
}

const research = (btn) => startTask(btn, `/prospects/${state.selectedId}/research`, undefined, "Research started. The browser is opening the profile.");

function generate(btn) {
  const tone = ($("#tone") || {}).value || "professional";
  const extra = (($("#extra") || {}).value || "").trim();
  const body = { tone };
  if (extra) body.extra_instructions = extra;
  state.viewVersion = null; state.compare = false; state.rejecting = false;
  return startTask(btn, `/prospects/${state.selectedId}/generate-message`, body, "Writing a new draft.");
}

async function decide(decision, btn) {
  const reason = decision === "reject" ? ($("#reject-reason").value || "").trim() : "";
  if (decision === "reject" && !reason) { toast("Add a reason before rejecting.", true); $("#reject-reason").focus(); return; }
  const reviewerInput = $("#reviewer");
  const reviewer = reviewerInput ? reviewerInput.value.trim() : "";
  if (!reviewer) {
    toast("Enter your name so the decision is recorded against it.", true);
    if (reviewerInput) { reviewerInput.setAttribute("aria-invalid", "true"); reviewerInput.focus(); }
    return;
  }
  store.set("op_reviewer", reviewer);
  const body = { decision, reviewer };
  if (reason) body.reason = reason;
  await withBusy(btn, async () => {
    try {
      await api("POST", `/prospects/${state.selectedId}/approve`, body);
      state.rejecting = false;
      toast(decision === "approve" ? "Draft approved. It is ready to send." : "Draft rejected.");
      await refreshAll();
    } catch (err) { toast(explain(err), true); }
  });
}

async function execute(btn) {
  const d = state.detail;
  const ok = await confirmAction("Send this message?", `The browser will fill in and submit the message form for ${d.name} on ${hostOf(d.source_url)}.`, "Send message", "primary");
  if (!ok) return;
  await startTask(btn, `/prospects/${state.selectedId}/execute`, undefined, "Sending. The browser is submitting the message form.");
}

async function cancelTask(btn) {
  if (!state.pollTask) return;
  await withBusy(btn, async () => {
    try { await api("POST", `/tasks/${state.pollTask.id}/cancel`); } catch (err) { toast(explain(err), true); }
  });
}

async function deleteProspect() {
  const d = state.detail;
  const ok = await confirmAction(`Delete ${d.name}?`, "This removes the prospect, every draft and the activity history. It can't be undone.", "Delete prospect", "danger");
  if (!ok) return;
  try {
    await api("DELETE", `/prospects/${d.id}`);
    toast(`${d.name} deleted.`);
    clearSelection();
    await Promise.all([loadStats(), loadList()]);
  } catch (err) { toast(explain(err), true); }
}

// ---- dialogs --------------------------------------------------------------------------------
function openDialog(id) {
  const dialog = document.getElementById(id);
  if (!dialog.open) dialog.showModal();
  const first = dialog.querySelector("input, textarea, select");
  if (first) first.focus();
}

function openKeyDialog() {
  const dialog = $("#key-dialog");
  if (!dialog.open) { $("#f-key").value = ""; dialog.showModal(); }
}

function confirmAction(title, text, okLabel, okClass) {
  const dialog = $("#confirm-dialog");
  $("#confirm-title").textContent = title;
  $("#confirm-text").textContent = text;
  const ok = $("#confirm-ok");
  ok.textContent = okLabel;
  ok.className = `btn ${okClass}`;
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true }));
}

function setupDialogs() {
  document.querySelectorAll("[data-open]").forEach((b) => b.addEventListener("click", () => openDialog(b.dataset.open)));
  document.querySelectorAll("dialog [data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));

  $("#add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    const error = $("#add-error");
    const button = form.querySelector('button[type="submit"]');
    const payload = {};
    for (const [key, value] of new FormData(form).entries()) if (String(value).trim()) payload[key] = String(value).trim();
    form.querySelectorAll("[aria-invalid]").forEach((el) => el.removeAttribute("aria-invalid"));
    if (!payload.name) { error.textContent = "Enter the prospect's full name."; error.hidden = false; $("#f-name").setAttribute("aria-invalid", "true"); return; }
    if (!payload.source_url && !payload.company) { error.textContent = "Add a profile URL or a company."; error.hidden = false; return; }
    await withBusy(button, async () => {
      try {
        const created = await api("POST", "/prospects", payload);
        error.hidden = true;
        form.reset();
        $("#add-dialog").close();
        toast(`${created.name} added.`);
        state.view = "all";
        await Promise.all([loadStats(), loadList()]);
        await select(created.id);
      } catch (err) {
        if (err.code === "duplicate_prospect" && err.details.existing_id) {
          error.replaceChildren("This prospect already exists. ", h("button", {
            type: "button", class: "btn link", onclick: () => { $("#add-dialog").close(); select(err.details.existing_id); },
          }, "Open the existing record"));
        } else {
          error.textContent = explain(err);
        }
        error.hidden = false;
      }
    });
  });

  $("#csv-file").addEventListener("change", async (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    if (file.size > 1_000_000) { toast("That file is larger than 1 MB. Split it into smaller files.", true); return; }
    $("#csv-text").value = await file.text();
  });

  $("#import-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = $("#csv-text").value;
    const error = $("#import-error");
    const result = $("#import-result");
    if (!text.trim()) { error.textContent = "Choose a CSV file or paste CSV text."; error.hidden = false; return; }
    await withBusy(e.currentTarget.querySelector('button[type="submit"]'), async () => {
      try {
        const report = await api("POST", "/prospects/import", { csv: text });
        error.hidden = true;
        const statusLabel = { created: "Added", duplicate: "Already exists", invalid: "Not valid" };
        result.replaceChildren(
          h("p", { class: "summary" }, `${report.created} added, ${report.duplicates} already existed, ${report.invalid} not valid.`),
          report.rows.length ? h("table", {},
            h("thead", {}, h("tr", {}, h("th", {}, "Line"), h("th", {}, "Name"), h("th", {}, "Result"), h("th", {}, "Details"))),
            h("tbody", {}, report.rows.map((r) => h("tr", {},
              h("td", {}, r.row), h("td", {}, r.name || ""), h("td", {}, statusLabel[r.status]), h("td", {}, r.errors.join("; ")))))) : null);
        result.hidden = false;
        await Promise.all([loadStats(), loadList()]);
      } catch (err) { error.textContent = explain(err); error.hidden = false; }
    });
  });

  $("#key-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const key = $("#f-key").value.trim();
    if (!key) return;
    store.set("op_api_key", key);
    $("#key-dialog").close();
    refreshAll();
  });
}

// ---- toasts ---------------------------------------------------------------------------------
function toast(text, bad = false) {
  const el = h("div", { class: `toast${bad ? " bad" : ""}` }, text);
  const box = $("#toasts");
  box.append(el);
  while (box.children.length > 3) box.firstElementChild.remove();
  window.setTimeout(() => el.remove(), bad ? 7000 : 4000);
}

// ---- boot -----------------------------------------------------------------------------------
async function boot() {
  setupDialogs();
  let timer = null;
  $("#search").addEventListener("input", (e) => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => { state.q = e.target.value.trim(); loadList(); }, 200);
  });
  const fromHash = readHash();
  loadHealth();
  await loadStats();
  if (!location.hash) state.view = state.counts.pending_approval ? "review" : "all";
  renderFilters();
  await loadList();
  if (fromHash) await select(fromHash);
  syncHash();
  window.addEventListener("hashchange", () => {
    const id = readHash();
    renderFilters();
    loadList();
    if (id && id !== state.selectedId) select(id);
    else if (!id && state.selectedId) clearSelection();
  });
  window.setInterval(() => {
    if (document.visibilityState === "visible") { loadStats(); loadList(); }
  }, 15000);
}

document.addEventListener("DOMContentLoaded", boot);
