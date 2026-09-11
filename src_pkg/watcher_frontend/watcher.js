"use strict";

const $ = (selector) => document.querySelector(selector);
let state = {};
let toastTimer = null;

function message(payload) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(payload, (response) => {
      const error = chrome.runtime.lastError;
      if (error) reject(new Error(error.message));
      else resolve(response || {});
    });
  });
}

function showToast(text, tone = "") {
  clearTimeout(toastTimer);
  const toast = $("#toast");
  toast.textContent = text;
  toast.className = `toast show ${tone}`.trim();
  toastTimer = setTimeout(() => { toast.className = "toast"; }, 3500);
}

function when(value, fallback) {
  const time = Number(value);
  if (!time) return fallback;
  return new Date(time).toLocaleString([], { dateStyle: "short", timeStyle: "short" });
}

function render(next = state) {
  state = next || {};
  const enabled = Boolean(state.enabled);
  const running = Boolean(state.running);
  if (state.intervalMinutes) $("#interval").value = String(state.intervalMinutes);
  $("#indicator").className = `indicator ${state.lastError ? "error" : enabled ? "live" : ""}`.trim();
  $("#watchStatus").textContent = running
    ? "Checking Indeed now"
    : enabled ? "Monitoring automatically" : "Not running";
  $("#detail").textContent = state.lastError || state.detail || (
    enabled ? "Waiting for the next scheduled check." : "No search is being monitored."
  );
  $("#visibleCount").textContent = String(state.visibleCount || 0);
  $("#changedCount").textContent = String(state.changedCount || 0);
  $("#retryCount").textContent = String(state.retryCount || 0);
  $("#foundCount").textContent = String(state.foundCount || 0);
  const total = Math.max(1, Number(state.progressTotal) || 1);
  const done = Math.max(0, Number(state.progressDone) || 0);
  $("#progress").style.width = running ? `${Math.min(100, Math.round(done * 100 / total))}%` : "0";
  $("#lastRun").textContent = when(state.lastRunAt, "Never");
  $("#nextRun").textContent = enabled ? when(state.nextRunAt, "Scheduled") : "Not scheduled";
  $("#start").disabled = running;
  $("#stop").disabled = !enabled;
  $("#checkNow").disabled = !enabled || running;

  const activity = Array.isArray(state.activity) ? state.activity : [];
  $("#activity").innerHTML = activity.length
    ? activity.slice(0, 15).map((item) => (
      `<li><strong>${escapeHtml(item.title || "Watcher event")}</strong>` +
      `<span>${escapeHtml(item.detail || "")} · ${escapeHtml(when(item.at, ""))}</span></li>`
    )).join("")
    : '<li class="empty">No activity yet.</li>';
}

function escapeHtml(value) {
  return String(value || "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
}

async function refresh() {
  const response = await message({ type: "MEDHUNT_WATCHER_GET_STATE" });
  render(response.state || {});
  $("#serviceStatus").textContent = response.backendReady
    ? "Service connected"
    : "Local service unavailable";
}

async function start() {
  const intervalMinutes = Number($("#interval").value) || 2;
  $("#start").disabled = true;
  try {
    const response = await message({
      type: "MEDHUNT_WATCHER_START",
      intervalMinutes,
    });
    if (!response.ok) throw new Error(response.error || "Watcher could not start.");
    render(response.state);
    showToast("Watcher started.");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    $("#start").disabled = false;
  }
}

async function command(type, success) {
  try {
    const response = await message({ type });
    if (!response.ok) throw new Error(response.error || "Action failed.");
    render(response.state);
    if (success) showToast(success);
  } catch (error) {
    showToast(error.message, "error");
  }
}

$("#start").addEventListener("click", start);
$("#stop").addEventListener("click", () => command("MEDHUNT_WATCHER_STOP", "Watcher stopped."));
$("#checkNow").addEventListener("click", () => command("MEDHUNT_WATCHER_RUN_NOW", "Check started."));
$("#reset").addEventListener("click", async () => {
  if (!confirm("Clear the stored comparison baseline? Visible candidates will be treated as new on the next check.")) return;
  await command("MEDHUNT_WATCHER_RESET", "Baseline cleared.");
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.medhuntWatcherState?.newValue) {
    render(changes.medhuntWatcherState.newValue);
  }
});

refresh().catch((error) => {
  $("#serviceStatus").textContent = "Service unavailable";
  showToast(error.message, "error");
});
