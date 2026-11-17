// GhostAP admin page. Plain JavaScript, nothing loaded from outside, and text from the Pi
// is always put on the page as text (never as HTML), so it can't inject anything.
"use strict";

const $ = (id) => document.getElementById(id);
const POLL_MS = 5000;
let timer = null;
let failures = 0;
let view = "status";
let form = null;        // the loaded settings form: { version, controls: [...] }
let lastStatus = null;  // the newest /api/status, for the pages that need to know the mode

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

async function api(path, body) {
  const opts = { credentials: "same-origin", headers: { "X-GhostAP": "1" } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch (e) { /* no body */ }
  return { ok: res.ok, status: res.status, data };
}

function show(which) {
  $("login").hidden = which !== "login";
  $("app").hidden = which !== "app";
  if (which === "login") {
    stopPolling();
    form = null;
    $("settings-body").replaceChildren();
    $("password").value = "";
    $("password").focus();
  }
}

// ---- sign in / out ----
$("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const button = $("login-button");
  button.disabled = true;
  $("login-error").textContent = "";
  try {
    const r = await api("/api/login", { password: $("password").value });
    if (r.ok) { await start(); }
    else { $("login-error").textContent = (r.data && r.data.error) || "Couldn't sign in."; }
  } catch (e) {
    $("login-error").textContent = "Can't reach the Pi.";
  } finally {
    button.disabled = false;
  }
});

$("logout").addEventListener("click", async () => {
  try { await api("/api/logout", {}); } catch (e) { /* signing out anyway */ }
  show("login");
});

// ---- status ----
const MODES = {
  client: { pill: "Online", cls: "ok", title: "Connected to WiFi" },
  ap: { pill: "Own network", cls: "info", title: "Running its own network" },
  disconnected: { pill: "Offline", cls: "bad", title: "Not connected" },
};

function card(title, ...children) {
  const c = el("div", "card");
  c.appendChild(el("h3", null, title));
  children.forEach((x) => c.appendChild(x));
  return c;
}

function rows(pairs) {
  const box = el("div", "rows");
  pairs.forEach(([k, v]) => {
    const r = el("div", "row");
    r.appendChild(el("span", null, k));
    r.appendChild(el("span", null, v));
    box.appendChild(r);
  });
  return box;
}

function render(s) {
  const mode = MODES[s.mode] || MODES.disconnected;
  const pill = $("pill");
  pill.textContent = mode.pill;
  pill.className = "pill " + mode.cls;

  const cards = $("cards");
  cards.replaceChildren();

  if (s.live_test) {
    const left = s.live_test.seconds_left;
    const a = card("Test running",
      el("p", "big", left === null ? "Reverts on its own soon" : "Reverts in about " + left + " s"),
      el("p", "muted", "A test of the fallback network is in progress. It undoes itself unless you keep it."),
      actionButtons([["Keep this", "commit", "primary"], ["Revert now", "abort", "secondary"]]));
    a.className = "card wide alert";
    cards.appendChild(a);
  }

  let line;
  if (s.mode === "client") line = s.wifi ? "Joined: " + s.wifi : "Joined a saved network";
  else if (s.mode === "ap") line = "Network name: " + s.ssid;
  else line = "Not on WiFi, and not running its own network.";
  const conn = [el("p", "big", mode.title), el("p", "muted", line)];
  if (s.mode === "ap") {
    conn.push(el("p", "muted", "Join it, then use " + s.gateway + " to reach the Pi."));
  }
  cards.appendChild(card("Connection", ...conn));

  cards.appendChild(card("This page",
    rows([
      ["Open it at", location.host],
      ["Addresses on " + s.interface, s.addresses.length ? s.addresses.join(", ") : "none"],
    ])));

  cards.appendChild(card("Safety net",
    el("p", "big", s.rescue_armed ? "Armed" : "Idle"),
    el("p", "muted", s.rescue_armed
      ? "A timer is set to put the network back if a change goes wrong."
      : "Nothing is waiting to be undone.")));

  const fo = s.failover;
  const foText = fo.enabled
    ? "If WiFi stays down for " + fo.grace + " s, it switches to its own network."
    : "Off. It only starts its own network at boot.";
  const foCard = [el("p", "big", fo.enabled ? "On" : "Off"), el("p", "muted", foText)];
  if (fo.enabled && fo.countdown_running) {
    foCard.push(el("p", "muted", "WiFi is down: the countdown is running."));
  }
  cards.appendChild(card("Failover", ...foCard));

  cards.appendChild(card("This device",
    rows([["Name", s.hostname], ["WiFi interface", s.interface]])));

  const last = s.apply && s.apply.last;
  if (last) {
    const when = last.at ? " (" + new Date(last.at * 1000).toLocaleString() + ")" : "";
    const lc = card("Last request from this page",
      el("p", last.ok ? "ok-text" : "bad-text", last.message), el("p", "muted small", when.trim()));
    lc.className = "card wide";
    cards.appendChild(lc);
  }

  $("updated").textContent = "Updated " + new Date().toLocaleTimeString();
}

async function refresh() {
  try {
    const r = await api("/api/status");
    if (r.status === 401) { show("login"); return; }
    if (!r.ok) throw new Error("status " + r.status);
    failures = 0;
    $("banner").hidden = true;
    lastStatus = r.data;
    render(r.data);
    renderApply();
  } catch (e) {
    failures += 1;
    if (failures >= 2) {
      $("banner").textContent =
        "Lost contact with the Pi. This happens when it changes networks. Retrying...";
      $("banner").hidden = false;
    }
  }
}

function startPolling() {
  stopPolling();
  timer = setInterval(() => { if (!document.hidden && view === "status") refresh(); }, POLL_MS);
}
function stopPolling() { if (timer) { clearInterval(timer); timer = null; } }

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !$("app").hidden && view === "status") refresh();
});

// ---- trying settings / keeping or reverting a test ----
let applyMsg = null;        // { text, cls } shown under the buttons
let confirming = false;

async function sendApply(action) {
  applyMsg = { text: "Sending...", cls: "" };
  renderApply();
  try {
    const r = await api("/api/apply", { action });
    if (r.status === 401) { show("login"); return; }
    if (!r.ok) {
      applyMsg = { text: (r.data && r.data.error) || "Couldn't send that.", cls: "bad" };
    } else if (action === "test") {
      applyMsg = { text: "Sent. The Pi is switching networks now, so this page will lose contact. "
        + "Join the Pi's network and open it there (steps above).", cls: "ok" };
    } else {
      applyMsg = { text: action === "commit" ? "Keeping it..." : "Reverting...", cls: "ok" };
      setTimeout(refresh, 2500);
    }
  } catch (e) {
    applyMsg = { text: "Can't reach the Pi. If it switched networks, follow the steps above.", cls: "bad" };
  }
  confirming = false;
  renderApply();
}

function actionButtons(list) {
  const box = el("div", "actions");
  list.forEach(([label, action, cls]) => {
    const b = el("button", cls, label);
    b.type = "button";
    b.addEventListener("click", () => sendApply(action));
    box.appendChild(b);
  });
  return box;
}

function renderApply() {
  const box = $("apply-card");
  box.replaceChildren();
  box.appendChild(el("h3", null, "Try the saved settings"));
  const s = lastStatus;
  const say = (text) => box.appendChild(el("p", "muted", text));
  if (!s || !s.apply) { say("Loading..."); return; }
  if (!s.apply.available) {
    say("Trying settings from this page needs a small helper that isn't installed yet. "
      + "Run sudo python3 install.py again in the ghost-ap folder.");
    return;
  }
  if (s.live_test) {
    say("A test is already running. Keep it or revert it from the Status page.");
    box.appendChild(actionButtons([["Keep this", "commit", "primary"], ["Revert now", "abort", "secondary"]]));
    return;
  }
  if (s.mode === "ap") {
    say("The Pi is already running its own network. Saved settings are used the next time it "
      + "starts that network (for example after a reboot).");
    return;
  }
  say("Saving only changes the file. To see it work, the Pi can raise its own network with these "
    + "settings. It undoes itself after about " + s.live_timeout + " seconds unless you tap Keep.");
  if (s.apply.pending) { say("A request is already being carried out..."); return; }
  const dirty = form && form.controls.some((c) => c.dirty());
  if (!confirming) {
    const b = el("button", "secondary", "Try the saved settings...");
    b.type = "button"; b.disabled = !!dirty;
    b.addEventListener("click", () => { confirming = true; renderApply(); });
    box.appendChild(b);
    if (dirty) say("Save your changes first.");
  } else {
    const steps = el("ol", "steps");
    [s.mode === "client" ? "The Pi leaves its WiFi, so this page will stop responding. That's expected."
                         : "The Pi raises its own network.",
     "On your phone, join the WiFi network \"" + s.ssid + "\".",
     "Open http://" + s.gateway + ":" + s.gui_port + " and sign in again.",
     "Tap Keep. If you do nothing, it reverts by itself and the Pi rejoins its WiFi."
    ].forEach((t) => steps.appendChild(el("li", null, t)));
    box.appendChild(steps);
    const row = el("div", "actions");
    const go = el("button", "primary", "Yes, try it now"); go.type = "button";
    go.addEventListener("click", () => sendApply("test"));
    const no = el("button", "secondary", "Cancel"); no.type = "button";
    no.addEventListener("click", () => { confirming = false; renderApply(); });
    row.appendChild(go); row.appendChild(no);
    box.appendChild(row);
  }
  if (applyMsg) box.appendChild(el("p", "form-msg " + applyMsg.cls, applyMsg.text));
}

// ---- settings ----
const narrow = window.matchMedia("(max-width: 720px)");
const SECTIONS = {
  network: "Network", access_point: "Fallback network (the Pi's own WiFi)",
  dhcp: "Addresses given to devices", recovery: "Safety and recovery",
  testing: "Testing", diagnostics: "Diagnostics", gui: "This admin page",
};

function sameValue(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

// One control per setting. read() gives what the person has set (null = "use the default").
function makeControl(item, value) {
  const id = "set-" + item.section + "-" + item.key;
  const locked = !item.editable;
  const ctl = { item, name: item.section + "." + item.key, locked };
  let input;
  if (item.type === "bool" && !item.nullable) {
    input = el("input"); input.type = "checkbox"; input.checked = value === true;
    ctl.start = input.checked;
    ctl.read = () => input.checked;
  } else if (item.type === "bool") {
    input = el("select");
    [["", "Default (not set)"], ["true", "On"], ["false", "Off"]].forEach(([v, t]) => {
      const o = el("option", null, t); o.value = v; input.appendChild(o);
    });
    input.value = value === true ? "true" : value === false ? "false" : "";
    ctl.start = value === true || value === false ? value : null;
    ctl.read = () => input.value === "" ? null : input.value === "true";
  } else if (item.choices) {
    input = el("select");
    item.choices.forEach((c) => { const o = el("option", null, String(c)); o.value = String(c); input.appendChild(o); });
    input.value = String(value);
    ctl.start = value;
    ctl.read = () => typeof item.choices[0] === "number" ? Number(input.value) : input.value;
  } else if (item.type === "list") {
    input = el("textarea"); input.value = (value || []).join("\n"); input.spellcheck = false;
    ctl.start = value || [];
    ctl.read = () => input.value.split("\n").map((x) => x.trim()).filter((x) => x);
  } else if (item.type === "int") {
    input = el("input"); input.type = "number"; input.step = "1"; input.inputMode = "numeric";
    if (item.minimum !== null) input.min = item.minimum;
    if (item.maximum !== null) input.max = item.maximum;
    input.value = value === null || value === undefined ? "" : String(value);
    ctl.start = value;
    ctl.read = () => {
      const t = input.value.trim();
      if (t === "") return null;
      return /^-?\d+$/.test(t) ? Number(t) : t;      // a non-number goes to the Pi to be refused
    };
  } else {
    input = el("input"); input.type = "text"; input.value = value === null || value === undefined ? "" : String(value);
    input.autocomplete = "off"; input.spellcheck = false;
    ctl.start = value === "" ? null : value;
    ctl.read = () => input.value === "" ? null : input.value;
  }
  input.id = id;
  if (locked) input.disabled = true;
  if (item.default !== null && item.default !== "" && !Array.isArray(item.default)
      && input.tagName === "INPUT" && input.type !== "checkbox") {
    input.placeholder = "default: " + item.default;
  }
  ctl.input = input;
  ctl.dirty = () => !locked && !sameValue(ctl.read(), ctl.start);

  const wrap = el("div", "field" + (item.type === "bool" && !item.nullable ? " is-bool" : "") + (locked ? " locked" : ""));
  const label = el("label", null, labelFor(item.key));
  label.htmlFor = id;
  label.appendChild(el("span", "key", item.key));
  const control = input;
  if (item.type === "bool" && !item.nullable) { wrap.appendChild(input); wrap.appendChild(label); }
  else {
    wrap.appendChild(label);
    if (item.unit) {
      const row = el("div", "input-row"); row.appendChild(control); row.appendChild(el("span", "unit", item.unit));
      wrap.appendChild(row);
    } else wrap.appendChild(control);
  }
  let help = item.description;
  if (item.allowed_words) help += " Allowed: " + item.allowed_words.join(", ") + ".";
  if (item.zero_means_off) help += " 0 turns it off.";
  if (locked) help += " Change this one over SSH.";
  wrap.appendChild(el("p", "help", help));
  const fix = el("p", "fix");
  wrap.appendChild(fix);
  ctl.wrap = wrap; ctl.fix = fix;
  input.addEventListener("input", updateButtons);
  input.addEventListener("change", updateButtons);
  return ctl;
}

function labelFor(key) {
  const t = key.replace(/_/g, " ");
  return t.charAt(0).toUpperCase() + t.slice(1);
}

function applyAdvanced() {
  if (!form) return;
  const on = $("show-advanced").checked;
  form.controls.forEach((c) => { c.wrap.hidden = c.item.advanced && !on && !c.dirty(); });
  form.groups.forEach((g) => { g.parentElement.hidden = !g.querySelector(".field:not([hidden])"); });
}

function updateButtons() {
  if (!form) return;
  const n = form.controls.filter((c) => c.dirty()).length;
  $("settings-save").disabled = n === 0 || form.saving;
  $("settings-undo").disabled = n === 0 || form.saving;
  if (typeof renderApply === "function") renderApply();
  $("settings-save").textContent = n ? "Save " + n + " change" + (n === 1 ? "" : "s") : "Save changes";
  applyAdvanced();
}

function setMessage(text, cls) {
  const m = $("settings-msg");
  m.textContent = text || "";
  m.className = "form-msg" + (cls ? " " + cls : "");
}

function clearFixes() {
  if (!form) return;
  form.controls.forEach((c) => { c.fix.textContent = ""; c.wrap.classList.remove("invalid"); });
}

function buildForm(data) {
  const body = $("settings-body");
  body.replaceChildren();
  const controls = [], groups = [];
  const order = Object.keys(SECTIONS);
  order.forEach((section) => {
    const items = data.schema.filter((i) => i.section === section);
    if (!items.length) return;
    const g = el("div", "group");
    items.forEach((item) => {
      const c = makeControl(item, data.values[section + "." + item.key]);
      controls.push(c);
      g.appendChild(c.wrap);
    });
    groups.push(g);
    // a folding section; on a phone only the first two start open so the page isn't endless
    const d = el("details", "card fold");
    d.open = !narrow.matches || groups.length <= 2;
    d.appendChild(el("summary", null, SECTIONS[section]));
    d.appendChild(g);
    body.appendChild(d);
  });
  // the cards wrap the groups; hide a whole card when only advanced fields are in it
  form = { version: data.version, controls, groups, saving: false };
  form.groups = Array.from(body.querySelectorAll(".group"));
  updateButtons();
}

async function loadSettings() {
  setMessage("Loading...");
  try {
    const r = await api("/api/settings");
    if (r.status === 401) { show("login"); return; }
    if (!r.ok) throw new Error("status " + r.status);
    setMessage("");
    buildForm(r.data);
  } catch (e) {
    setMessage("Couldn't load the settings.", "bad");
  }
}

$("show-advanced").addEventListener("change", applyAdvanced);

$("settings-undo").addEventListener("click", () => loadSettings());

$("settings-view").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (!form || form.saving) return;
  const changes = {};
  form.controls.forEach((c) => { if (c.dirty()) changes[c.name] = c.read(); });
  if (!Object.keys(changes).length) return;
  clearFixes();
  form.saving = true; updateButtons();
  setMessage("Saving...");
  try {
    const r = await api("/api/settings", { version: form.version, changes });
    if (r.status === 401) { show("login"); return; }
    if (r.ok) {
      const n = r.data.saved.length;
      await loadSettings();
      setMessage("Saved " + n + " setting" + (n === 1 ? "" : "s") + ". Not live yet: it applies at the next boot of the fallback network, or try it now with: sudo ghostap test", "ok");
      return;
    }
    const fields = (r.data && r.data.fields) || {};
    form.controls.forEach((c) => {
      if (fields[c.name]) { c.fix.textContent = fields[c.name]; c.wrap.classList.add("invalid"); c.wrap.hidden = false;
        const d = c.wrap.closest("details"); if (d) d.open = true; }
    });
    const general = fields[""] || (r.data && r.data.error) || "Couldn't save.";
    setMessage(r.status === 409 ? general : "Not saved. " + general, "bad");
  } catch (e) {
    setMessage("Can't reach the Pi, so nothing was saved.", "bad");
  } finally {
    if (form) { form.saving = false; updateButtons(); }
  }
});

// ---- which page ----
function route() {
  view = location.hash === "#settings" ? "settings" : "status";
  $("status-view").hidden = view !== "status";
  $("settings-view").hidden = view !== "settings";
  $("pill").hidden = view !== "status";
  $("banner").hidden = view !== "status" || failures < 2;
  $("title").textContent = view === "settings" ? "Settings" : "Status";
  $("nav-status").className = "nav-item" + (view === "status" ? " active" : "");
  $("nav-settings").className = "nav-item" + (view === "settings" ? " active" : "");
  if (view === "status") $("nav-status").setAttribute("aria-current", "page");
  else $("nav-status").removeAttribute("aria-current");
  if (view === "settings") {
    $("nav-settings").setAttribute("aria-current", "page");
    if (!form) loadSettings();
    refresh();
  } else $("nav-settings").removeAttribute("aria-current");
}
window.addEventListener("hashchange", route);

async function start() {
  show("app");
  route();
  await refresh();
  startPolling();
}

(async function init() {
  try {
    const r = await api("/api/session");
    if (r.ok && r.data.authenticated) await start(); else show("login");
  } catch (e) {
    show("login");
    $("login-error").textContent = "Can't reach the Pi.";
  }
})();
