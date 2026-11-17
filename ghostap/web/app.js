// GhostAP admin page. Plain JavaScript, nothing loaded from outside, and text from the Pi
// is always put on the page as text (never as HTML), so it can't inject anything.
"use strict";

const $ = (id) => document.getElementById(id);
const POLL_MS = 5000;
let timer = null;
let failures = 0;

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
      el("p", "muted", "A test of the fallback network is in progress. It undoes itself unless you keep it with: sudo ghostap test --commit"));
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

  $("updated").textContent = "Updated " + new Date().toLocaleTimeString();
}

async function refresh() {
  try {
    const r = await api("/api/status");
    if (r.status === 401) { show("login"); return; }
    if (!r.ok) throw new Error("status " + r.status);
    failures = 0;
    $("banner").hidden = true;
    render(r.data);
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
  timer = setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
}
function stopPolling() { if (timer) { clearInterval(timer); timer = null; } }

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !$("app").hidden) refresh();
});

async function start() {
  show("app");
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
