"use strict";

const STATE_KEY = "medhuntWatcherState";
const BACKEND_KEY = "medhuntWatcherBackendUrl";
const ALARM_NAME = "medhuntWatcherCycle";
const DEFAULT_BACKEND = "http://127.0.0.1:8091";
const LOCAL_API_TOKEN = "__MEDHUNT_LOCAL_API_TOKEN__";
const RUN_LEASE_MS = 15 * 60 * 1000;
const UPDATE_TOLERANCE_MS = 15 * 60 * 1000;
let activeCycle = null;

const defaultState = () => ({
  enabled: false,
  running: false,
  query: "",
  location: "",
  intervalMinutes: 2,
  tabId: 0,
  searchUrl: "",
  watchKey: "",
  checkpoints: {},
  baselineEstablished: false,
  pendingNotifications: [],
  emailAlertStatus: "",
  visibleCount: 0,
  changedCount: 0,
  foundCount: 0,
  progressDone: 0,
  progressTotal: 0,
  lastRunAt: 0,
  nextRunAt: 0,
  runStartedAt: 0,
  detail: "",
  lastError: "",
  activity: [],
});

async function readState() {
  const stored = await chrome.storage.local.get([STATE_KEY]);
  const saved = stored[STATE_KEY] || {};
  const state = { ...defaultState(), ...saved };
  if (typeof saved.baselineEstablished !== "boolean") {
    state.baselineEstablished = Object.keys(state.checkpoints || {}).length > 0;
  }
  if (!Array.isArray(state.pendingNotifications)) state.pendingNotifications = [];
  return state;
}

async function writeState(patch) {
  const current = await readState();
  const next = { ...current, ...patch };
  await chrome.storage.local.set({ [STATE_KEY]: next });
  return next;
}

function activityItem(title, detail = "") {
  return { title: String(title), detail: String(detail), at: Date.now() };
}

async function addActivity(title, detail = "", patch = {}) {
  const state = await readState();
  return writeState({
    ...patch,
    activity: [activityItem(title, detail), ...(state.activity || [])].slice(0, 50),
  });
}

function indeedUrl(value) {
  try {
    const host = new URL(value).hostname.toLowerCase();
    return host === "indeed.com" || host.endsWith(".indeed.com");
  } catch {
    return false;
  }
}

const INDEED_TRANSIENT_SEARCH_PARAMS = new Set([
  "candidate", "candidate_id", "candidateid", "drawer", "modal", "profile_id",
  "profileid", "resume_id", "resumeid", "selected_candidate", "selectedcandidate",
]);

function canonicalIndeedSearchUrl(value) {
  try {
    const url = new URL(value);
    url.hash = "";
    for (const key of Array.from(url.searchParams.keys())) {
      if (INDEED_TRANSIENT_SEARCH_PARAMS.has(key.toLowerCase())) url.searchParams.delete(key);
    }
    url.searchParams.sort();
    return url.href;
  } catch {
    return String(value || "");
  }
}

function sameIndeedSearchContext(firstValue, secondValue) {
  if (!firstValue || !secondValue) return false;
  return canonicalIndeedSearchUrl(firstValue) === canonicalIndeedSearchUrl(secondValue);
}

function stableHash(value) {
  let result = 5381;
  for (const character of String(value || "")) {
    result = (((result << 5) + result) + character.charCodeAt(0)) >>> 0;
  }
  return result.toString(36);
}

function canonicalArray(values) {
  return Array.from(new Set((Array.isArray(values) ? values : [])
    .map((value) => String(value || "").replace(/\s+/g, " ").trim().toLowerCase())
    .filter(Boolean))).sort();
}

function profileFingerprint(profile) {
  return stableHash(JSON.stringify({
    id: String(profile.source_id || "").trim().toLowerCase(),
    name: String(profile.name || "").replace(/\s+/g, " ").trim().toLowerCase(),
    location: String(profile.location || "").replace(/\s+/g, " ").trim().toLowerCase(),
    headline: String(profile.headline || "").replace(/\s+/g, " ").trim().toLowerCase(),
    roles: canonicalArray(profile.roles),
    employers: canonicalArray(profile.employers),
    schools: canonicalArray(profile.schools),
  }));
}

function resumeMarker(profile) {
  return String(profile.notes || "").split(/\r?\n/)
    .map((line) => line.replace(/\s+/g, " ").trim())
    .find((line) => /\bresume\s+(?:last\s+)?updated\b/i.test(line)) || "";
}

function resumeEstimate(marker, observedAt = Date.now()) {
  const value = String(marker || "").toLowerCase();
  if (!value) return 0;
  if (/just now|moments? ago/.test(value)) return observedAt;
  const relative = value.match(/\b(\d+)\s*(minute|hour|day|week|month)s?\s+ago\b/);
  if (relative) {
    const units = { minute: 60e3, hour: 3600e3, day: 86400e3, week: 604800e3, month: 2592e6 };
    return observedAt - Number(relative[1]) * units[relative[2]];
  }
  if (/\btoday\b/.test(value)) {
    const day = new Date(observedAt);
    day.setHours(0, 0, 0, 0);
    return day.getTime();
  }
  if (/\byesterday\b/.test(value)) {
    const day = new Date(observedAt);
    day.setHours(0, 0, 0, 0);
    return day.getTime() - 86400e3;
  }
  const dateText = marker.replace(/^.*?updated\s*/i, "").trim();
  const absolute = Date.parse(dateText);
  return Number.isFinite(absolute) ? absolute : 0;
}

function sourceKey(profile) {
  return String(profile.source_id || "").trim() ||
    `fallback:${stableHash(`${profile.name || ""}|${profile.location || ""}`)}`;
}

function classifyProfile(profile, checkpoint, observedAt) {
  const fingerprint = profileFingerprint(profile);
  const marker = resumeMarker(profile);
  const estimate = resumeEstimate(marker, observedAt);
  if (!checkpoint) return { action: "new", fingerprint, marker, estimate };
  if (checkpoint.retry === true) return { action: "retry", fingerprint, marker, estimate };
  if (checkpoint.fingerprint !== fingerprint) {
    return { action: "updated", fingerprint, marker, estimate };
  }
  if (estimate && !checkpoint.resumeEstimate) {
    return { action: "updated", fingerprint, marker, estimate };
  }
  if (estimate && checkpoint.resumeEstimate && estimate > checkpoint.resumeEstimate + UPDATE_TOLERANCE_MS) {
    return { action: "updated", fingerprint, marker, estimate };
  }
  return { action: "unchanged", fingerprint, marker, estimate };
}

async function api(path, options = {}, timeout = 120000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const headers = new Headers(options.headers || {});
    if (LOCAL_API_TOKEN && !LOCAL_API_TOKEN.startsWith("__MEDHUNT_")) {
      headers.set("X-Medhunt-Token", LOCAL_API_TOKEN);
    }
    const configured = await chrome.storage.local.get([BACKEND_KEY]);
    const backend = String(configured[BACKEND_KEY] || DEFAULT_BACKEND).replace(/\/+$/, "");
    const response = await fetch(`${backend}${path}`, { ...options, headers, signal: controller.signal });
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : await response.text();
    if (!response.ok) {
      const detail = payload && typeof payload === "object" ? payload.detail : payload;
      throw new Error(detail || `Backend returned ${response.status}.`);
    }
    return payload;
  } catch (error) {
    if (error?.name === "AbortError") throw new Error("The Medhunt backend request timed out.");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

async function backendReady() {
  try {
    await api("/health", {}, 5000);
    return true;
  } catch {
    return false;
  }
}

function tabMessage(tabId, payload, timeout = 120000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Indeed did not finish the requested action.")), timeout);
    chrome.tabs.sendMessage(tabId, payload, (response) => {
      clearTimeout(timer);
      const error = chrome.runtime.lastError;
      if (error) reject(new Error(error.message));
      else resolve(response || {});
    });
  });
}

async function injectIndeedScripts(tabId) {
  if (!chrome.scripting?.executeScript) return false;
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["inject.js"],
      world: "MAIN",
    });
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["indeed-content.js", "watcher-content.js"],
    });
    return true;
  } catch {
    return false;
  }
}

async function waitForTab(tabId, timeout = 45000, expectedUrl = "") {
  const deadline = Date.now() + timeout;
  let injectionAttempted = false;
  while (Date.now() < deadline) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (tab && indeedUrl(tab.url || "")) {
      try {
        const ping = await tabMessage(tabId, { type: "MEDHUNT_WATCHER_PING" }, 3000);
        const pageUrl = ping?.url || tab.url || "";
        if (
          ping?.ok &&
          indeedUrl(pageUrl) &&
          (!expectedUrl || sameIndeedSearchContext(pageUrl, expectedUrl))
        ) {
          return { ...tab, url: pageUrl };
        }
      } catch {
        if (!injectionAttempted) {
          injectionAttempted = true;
          await injectIndeedScripts(tabId);
        }
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error("Indeed did not finish loading.");
}

async function getWatchTab(state, requireOpenIndeed = false) {
  if (state.tabId) {
    const existing = await chrome.tabs.get(Number(state.tabId)).catch(() => null);
    if (existing && indeedUrl(existing.url || "")) return existing;
  }
  const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (active && indeedUrl(active.url || "")) return active;
  const candidates = await chrome.tabs.query({ url: ["*://*.indeed.com/*"] });
  if (candidates.length) return candidates[0];
  if (requireOpenIndeed) {
    throw new Error("Open Indeed Smart Sourcing in this browser once, then start the watcher.");
  }
  const created = await chrome.tabs.create({
    url: state.searchUrl || "https://www.indeed.com/candidates/",
    active: false,
  });
  return created;
}

function bounded(value, limit) {
  return String(value || "").replace(/\u0000/g, "").trim().slice(0, limit);
}

function boundedList(values, limit, chars = 240) {
  return (Array.isArray(values) ? values : []).slice(0, limit).map((value) => bounded(value, chars)).filter(Boolean);
}

function importProfile(profile) {
  return {
    name: bounded(profile.name, 200),
    location: bounded(profile.location, 500),
    headline: bounded(profile.headline, 500),
    roles: boundedList(profile.roles, 20),
    employers: boundedList(profile.employers, 20),
    schools: boundedList(profile.schools, 20),
    notes: bounded(profile.notes, 20000),
    source: "indeed",
    source_url: bounded(profile.source_url, 2000),
    source_id: bounded(profile.source_id, 500),
  };
}

function runId() {
  return globalThis.crypto?.randomUUID
    ? globalThis.crypto.randomUUID().replaceAll("-", "")
    : `watch_${Date.now()}_${Math.random().toString(36).slice(2)}`;
}

async function schedule(state) {
  await chrome.alarms.clear(ALARM_NAME);
  if (!state.enabled) return;
  const periodInMinutes = Math.max(1, Number(state.intervalMinutes) || 2);
  await chrome.alarms.create(ALARM_NAME, { delayInMinutes: periodInMinutes, periodInMinutes });
  await writeState({ nextRunAt: Date.now() + periodInMinutes * 60e3 });
}

async function processResume(tabId, profile, candidateId) {
  const captured = await tabMessage(tabId, {
    type: "RADIXSOL_DOWNLOAD_INDEED_RESUME",
    index: Number(profile.result_index) || 0,
    expectedName: profile.name,
  }, 120000);
  if (!captured?.ok || !captured.base64) {
    throw new Error(captured?.error || "Indeed did not return a resume PDF.");
  }
  return api(`/candidates/${Number(candidateId)}/resume/from-browser`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      content_base64: captured.base64,
      filename: bounded(captured.filename || `${profile.name}-resume.pdf`, 255),
    }),
  }, 150000);
}

function resumeNotificationEvent(state, changed, observedAt) {
  const alertItems = changed.filter((item) => ["new", "updated"].includes(item.classification.action));
  if (!state.baselineEstablished || !alertItems.length) return null;
  const signature = alertItems.map((item) => [
    item.key,
    item.previousFingerprint || "none",
    item.classification.fingerprint,
    item.classification.action,
  ].join(":")).sort().join("|");
  return {
    event_id: `indeed.${state.watchKey || "watch"}.${stableHash(signature)}`,
    watch_key: state.watchKey || "",
    query: bounded(state.query, 2000),
    location: bounded(state.location, 300),
    detected_at: new Date(observedAt).toISOString(),
    profiles: alertItems.map((item) => ({
      name: bounded(item.profile.name, 200),
      location: bounded(item.profile.location, 500),
      headline: bounded(item.profile.headline, 500),
      source_url: bounded(item.profile.source_url, 2000),
      source_id: bounded(item.profile.source_id, 500),
      resume_marker: bounded(item.classification.marker, 500),
      change: item.classification.action,
    })),
  };
}

async function queueResumeNotification(event) {
  if (!event?.event_id) return;
  const state = await readState();
  const pending = Array.isArray(state.pendingNotifications) ? state.pendingNotifications : [];
  if (pending.some((item) => item?.event_id === event.event_id)) return;
  await writeState({
    pendingNotifications: [...pending, event].slice(-20),
    emailAlertStatus: "Email alert queued.",
  });
}

async function flushResumeNotifications() {
  const state = await readState();
  const pending = Array.isArray(state.pendingNotifications) ? state.pendingNotifications : [];
  if (!pending.length) return { status: "none", sent: 0, pending: 0 };
  const remaining = [];
  let sent = 0;
  let disabled = 0;
  for (const event of pending) {
    try {
      const result = await api("/watcher/resume-notifications", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(event),
      }, 30000);
      if (["sent", "deduplicated"].includes(result?.status)) {
        sent += 1;
      } else if (result?.status === "disabled") {
        disabled += 1;
      } else {
        remaining.push(event);
      }
    } catch {
      remaining.push(event);
    }
  }
  const emailAlertStatus = remaining.length
    ? `${remaining.length} email alert${remaining.length === 1 ? "" : "s"} pending retry.`
    : sent
      ? `${sent} email alert${sent === 1 ? "" : "s"} sent.`
      : disabled
        ? "Email alerts are not configured in the Medhunt service."
        : "";
  await writeState({ pendingNotifications: remaining, emailAlertStatus });
  return {
    status: remaining.length ? "pending" : sent ? "sent" : disabled ? "disabled" : "none",
    sent,
    pending: remaining.length,
  };
}

async function performCycle({ useCurrentPage = false } = {}) {
  let state = await readState();
  if (!state.enabled) return state;
  if (state.running && Date.now() - Number(state.runStartedAt || 0) < RUN_LEASE_MS) return state;
  state = await writeState({
    running: true,
    runStartedAt: Date.now(),
    lastError: "",
    detail: "Opening the monitored Indeed search…",
    progressDone: 0,
    progressTotal: 1,
  });

  try {
    if (!await backendReady()) throw new Error("The Medhunt backend is not running.");
    await flushResumeNotifications();
    state = await readState();
    let tab = await getWatchTab(state, useCurrentPage && !state.searchUrl);
    await writeState({ tabId: tab.id });

    let expectedUrl = "";
    if (!useCurrentPage && state.searchUrl) {
      expectedUrl = bounded(canonicalIndeedSearchUrl(state.searchUrl), 2000);
      if (expectedUrl !== state.searchUrl) {
        state = await writeState({ searchUrl: expectedUrl });
      }
      if (!sameIndeedSearchContext(tab.url || "", expectedUrl)) {
        tab = await chrome.tabs.update(tab.id, { url: expectedUrl, active: false });
      }
    }
    await waitForTab(tab.id, 45000, expectedUrl);
    const sorted = await tabMessage(tab.id, { type: "MEDHUNT_WATCHER_ENSURE_RECENT" }, 30000);
    if (!sorted?.ok) throw new Error(sorted?.error || "Indeed's Most recent sort could not be applied.");
    const sortedSearchUrl = sorted.pageUrl
      ? bounded(canonicalIndeedSearchUrl(sorted.pageUrl), 2000)
      : "";
    if (sortedSearchUrl && sortedSearchUrl !== state.searchUrl) {
      state = await writeState({ searchUrl: sortedSearchUrl });
    }
    const scan = await tabMessage(tab.id, { type: "RADIXSOL_SCAN_INDEED_CANDIDATES" }, 120000);
    if (!scan?.ok) throw new Error(scan?.error || "Indeed candidate cards could not be read.");
    const profiles = (Array.isArray(scan.profiles) ? scan.profiles : [])
      .filter((profile) => profile?.name && sourceKey(profile))
      .slice(0, 100);
    if (!profiles.length) throw new Error("No candidate cards were found in the monitored search.");

    state = await readState();
    const observedAt = Date.now();
    const checkpoints = { ...(state.checkpoints || {}) };
    const changed = profiles.map((profile) => {
      const key = sourceKey(profile);
      const checkpoint = checkpoints[key];
      return {
        profile,
        key,
        previousFingerprint: checkpoint?.fingerprint || "",
        classification: classifyProfile(profile, checkpoint, observedAt),
      };
    }).filter((item) => item.classification.action !== "unchanged");

    await writeState({
      visibleCount: profiles.length,
      changedCount: changed.length,
      foundCount: 0,
      progressDone: 0,
      progressTotal: Math.max(1, changed.length),
      detail: changed.length
        ? `Processing ${changed.length} new or updated profile${changed.length === 1 ? "" : "s"}…`
        : "No new or updated profiles were detected.",
    });

    await queueResumeNotification(resumeNotificationEvent(state, changed, observedAt));

    if (!changed.length) {
      const now = Date.now();
      const next = now + Math.max(1, Number(state.intervalMinutes) || 2) * 60e3;
      return addActivity("Check complete", `${profiles.length} recent profiles compared; no changes`, {
        running: false,
        runStartedAt: 0,
        lastRunAt: now,
        nextRunAt: next,
        baselineEstablished: true,
      });
    }

    const imported = await api("/candidates/import/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profiles: changed.map((item) => importProfile(item.profile)), search_url: scan.page_url || state.searchUrl }),
    });
    const saved = Array.isArray(imported.results) ? imported.results : [];
    if (saved.length !== changed.length) throw new Error("The backend returned an incomplete candidate import.");
    saved.forEach((result, index) => { changed[index].candidateId = Number(result.id); });

    const lookup = await api("/contact-lookup/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        candidate_ids: changed.map((item) => item.candidateId),
        run_id: runId(),
        confirmed: true,
      }),
    }, 190000);
    const results = lookup?.results || {};
    let found = 0;
    let failed = 0;
    let resumes = 0;

    for (let index = 0; index < changed.length; index += 1) {
      const item = changed[index];
      const result = results[String(item.candidateId)] || { status: "failed" };
      const retry = result.status === "failed";
      let resumeStored = false;
      let resumeError = "";
      if (result.status === "found") {
        found += 1;
        if (result.resume_required === true) {
          try {
            await processResume(tab.id, item.profile, item.candidateId);
            resumeStored = true;
            resumes += 1;
          } catch (error) {
            resumeError = String(error?.message || error);
          }
        }
      } else if (retry) {
        failed += 1;
      }
      checkpoints[item.key] = {
        fingerprint: item.classification.fingerprint,
        resumeMarker: item.classification.marker,
        resumeEstimate: item.classification.estimate,
        candidateId: item.candidateId,
        lastSeenAt: observedAt,
        lastProcessedAt: Date.now(),
        outcome: result.status || "failed",
        retry: retry || Boolean(resumeError),
        resumeStored,
        resumeError,
      };
      await writeState({
        checkpoints,
        foundCount: found,
        progressDone: index + 1,
        detail: `Processed ${index + 1} of ${changed.length}.`,
      });
    }

    const now = Date.now();
    const interval = Math.max(1, Number(state.intervalMinutes) || 2);
    const emailOutcome = await flushResumeNotifications();
    const emailSummary = emailOutcome.status === "sent"
      ? "; recruiter email sent"
      : emailOutcome.status === "pending"
        ? "; recruiter email pending retry"
        : "";
    const summary = `${changed.length} new/updated; ${found} contacts; ${resumes} resumes` +
      (failed ? `; ${failed} retries scheduled` : "") + emailSummary;
    return addActivity("Automatic check complete", summary, {
      running: false,
      runStartedAt: 0,
      lastRunAt: now,
      nextRunAt: now + interval * 60e3,
      detail: summary,
      lastError: "",
      checkpoints,
      baselineEstablished: true,
    });
  } catch (error) {
    const message = String(error?.message || error);
    const current = await readState();
    const interval = Math.max(1, Number(current.intervalMinutes) || 2);
    return addActivity("Check needs retry", message, {
      running: false,
      runStartedAt: 0,
      lastRunAt: Date.now(),
      nextRunAt: Date.now() + interval * 60e3,
      lastError: message,
      detail: "The watcher will retry automatically.",
    });
  }
}

function runCycle(options = {}) {
  if (activeCycle) return activeCycle;
  activeCycle = performCycle(options).finally(() => { activeCycle = null; });
  return activeCycle;
}

async function startWatcher(message) {
  const intervalMinutes = Math.max(1, Math.min(1440, Number(message.intervalMinutes) || 2));
  if (!await backendReady()) throw new Error("Start the Medhunt backend before starting the watcher.");
  const current = await readState();
  const tab = await getWatchTab(current, true);
  await waitForTab(tab.id);
  const captured = await tabMessage(tab.id, { type: "MEDHUNT_WATCHER_CAPTURE_SEARCH" }, 30000);
  if (!captured?.ok) {
    await writeState({ enabled: false, lastError: captured?.error || "The prepared Indeed search could not be captured." });
    throw new Error(captured?.error || "The prepared Indeed search could not be captured.");
  }
  const query = bounded(captured.query, 2000);
  const location = bounded(captured.location, 300);
  const searchUrl = bounded(canonicalIndeedSearchUrl(captured.pageUrl || tab.url), 2000);
  const watchKey = stableHash(`${query.toLowerCase()}|${location.toLowerCase()}|${searchUrl}`);
  const state = await writeState({
    enabled: true,
    query,
    location,
    intervalMinutes,
    tabId: tab.id,
    searchUrl,
    watchKey,
    checkpoints: current.watchKey === watchKey ? current.checkpoints : {},
    baselineEstablished: current.watchKey === watchKey
      ? current.baselineEstablished
      : false,
    pendingNotifications: current.pendingNotifications,
    lastError: "",
    detail: "Current Indeed search captured. Starting first check…",
  });
  await schedule(state);
  return runCycle({ useCurrentPage: true });
}

async function stopWatcher() {
  await chrome.alarms.clear(ALARM_NAME);
  return addActivity("Watcher stopped", "Automatic checks are paused", {
    enabled: false,
    nextRunAt: 0,
    detail: "Automatic monitoring is paused.",
  });
}

async function resetWatcher() {
  return addActivity("Baseline reset", "The next check will treat visible profiles as new", {
    checkpoints: {},
    baselineEstablished: false,
    visibleCount: 0,
    changedCount: 0,
    foundCount: 0,
  });
}

async function dispatchTrustedIndeedClick(tabId, x, y) {
  const target = { tabId };
  const point = { x, y, button: "left" };
  let attached = false;
  try {
    await chrome.debugger.attach(target, "1.3");
    attached = true;
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", { type: "mouseMoved", ...point });
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", { type: "mousePressed", ...point, clickCount: 1 });
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", { type: "mouseReleased", ...point, clickCount: 1 });
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String(error?.message || error) };
  } finally {
    if (attached) await chrome.debugger.detach(target).catch(() => {});
  }
}

chrome.runtime.onInstalled.addListener(async () => {
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
  const state = await readState();
  if (state.enabled) await schedule(state);
});

chrome.runtime.onStartup.addListener(async () => {
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
  const state = await readState();
  if (state.enabled) {
    await schedule(state);
    void runCycle();
  }
});

chrome.action.onClicked.addListener(async (tab) => {
  await chrome.sidePanel.open({ windowId: tab.windowId }).catch(() => {});
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== ALARM_NAME) return;
  void runCycle();
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "RADIXSOL_TRUSTED_INDEED_CLICK") {
    dispatchTrustedIndeedClick(sender.tab?.id, Number(message.x), Number(message.y)).then(sendResponse);
    return true;
  }
  if (message?.type === "MEDHUNT_WATCHER_GET_STATE") {
    Promise.all([readState(), backendReady()])
      .then(([state, ready]) => sendResponse({ ok: true, state, backendReady: ready }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message?.type === "MEDHUNT_WATCHER_START") {
    startWatcher(message)
      .then((state) => sendResponse({ ok: true, state }))
      .catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
    return true;
  }
  if (message?.type === "MEDHUNT_WATCHER_STOP") {
    stopWatcher().then((state) => sendResponse({ ok: true, state }));
    return true;
  }
  if (message?.type === "MEDHUNT_WATCHER_RESET") {
    resetWatcher().then((state) => sendResponse({ ok: true, state }));
    return true;
  }
  if (message?.type === "MEDHUNT_WATCHER_RUN_NOW") {
    void runCycle();
    readState().then((state) => sendResponse({ ok: true, state: { ...state, detail: "Manual check requested." } }));
    return true;
  }
  return false;
});

chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
