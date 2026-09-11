(() => {
  "use strict";

  if (window.__medhuntWatcherContentLoaded) return;
  window.__medhuntWatcherContentLoaded = true;

  const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

  function visible(element) {
    if (!element || !element.isConnected) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
  }

  function text(element) {
    return String(element?.innerText || element?.textContent || "").replace(/\s+/g, " ").trim();
  }

  function inputs() {
    return Array.from(document.querySelectorAll("input, textarea")).filter(visible);
  }

  function scoreInput(element, type) {
    const descriptor = [
      element.getAttribute("placeholder"),
      element.getAttribute("aria-label"),
      element.getAttribute("name"),
      element.getAttribute("data-testid"),
    ].filter(Boolean).join(" ").toLowerCase();
    if (type === "location") {
      return (/location|city|state|zip|where/.test(descriptor) ? 100 : 0) +
        (element.value && /,|\b[A-Z]{2}\b/.test(element.value) ? 10 : 0);
    }
    return (/job|title|keyword|search|query|what/.test(descriptor) ? 100 : 0) -
      (/location|city|state|zip|where/.test(descriptor) ? 200 : 0) +
      (element.tagName === "TEXTAREA" ? 5 : 0);
  }

  function findInput(type) {
    return inputs().map((element) => ({ element, score: scoreInput(element, type) }))
      .sort((first, second) => second.score - first.score)[0]?.element || null;
  }

  function realClick(element) {
    if (!element) return;
    element.scrollIntoView({ block: "center", inline: "center" });
    const options = { bubbles: true, cancelable: true, view: window };
    try { element.dispatchEvent(new PointerEvent("pointerdown", options)); } catch {}
    try { element.dispatchEvent(new MouseEvent("mousedown", options)); } catch {}
    try { element.dispatchEvent(new PointerEvent("pointerup", options)); } catch {}
    try { element.dispatchEvent(new MouseEvent("mouseup", options)); } catch {}
    element.click();
  }

  function exactControl(pattern, root = document) {
    const selectors = "button, [role='button'], [role='option'], [role='menuitem'], li, a";
    return Array.from(root.querySelectorAll(selectors))
      .filter((element) => visible(element) && pattern.test(text(element)))
      .sort((first, second) => text(first).length - text(second).length)[0] || null;
  }

  async function waitFor(predicate, timeout = 20000, interval = 200) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const value = predicate();
      if (value) return value;
      await sleep(interval);
    }
    return null;
  }

  function resultCount() {
    return document.querySelectorAll("[data-cauto-id^='MATCH_CARD_BASE-'], [data-candidate-id]").length;
  }

  async function applyMostRecentSort() {
    const trigger = await waitFor(() => exactControl(/^Sort by\s*:/i), 20000);
    if (!trigger) throw new Error("Indeed's Sort by control was not found.");
    if (/most recent/i.test(text(trigger))) return;
    realClick(trigger);
    const option = await waitFor(() => exactControl(/^Most recent$/i), 8000);
    if (!option) throw new Error("Indeed's Most recent option was not found.");
    realClick(option);
    await sleep(1400);
    const selected = await waitFor(() => {
      const current = exactControl(/^Sort by\s*:/i);
      return current && /most recent/i.test(text(current)) ? current : null;
    }, 8000);
    if (!selected) throw new Error("Indeed did not apply the Most recent sort.");
  }

  async function capturePreparedSearch() {
    const sort = await waitFor(() => exactControl(/^Sort by\s*:/i), 20000);
    if (!sort) throw new Error("Indeed's Sort by control was not found.");
    if (!/most recent/i.test(text(sort))) {
      throw new Error("Select Sort by: Most recent on Indeed before starting the watcher.");
    }
    const cards = await waitFor(() => resultCount() > 0 ? resultCount() : 0, 10000);
    if (!cards) throw new Error("Click Find on Indeed and wait for candidate cards before starting the watcher.");
    return {
      ok: true,
      pageUrl: globalThis.location.href,
      resultCount: cards,
      query: String(findInput("query")?.value || "").trim(),
      location: String(findInput("location")?.value || "").trim(),
      sort: "most_recent",
    };
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type === "MEDHUNT_WATCHER_PING") {
      sendResponse({ ok: true, url: location.href });
      return false;
    }
    if (message?.type === "MEDHUNT_WATCHER_CAPTURE_SEARCH") {
      capturePreparedSearch()
        .then(sendResponse)
        .catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
      return true;
    }
    if (message?.type === "MEDHUNT_WATCHER_ENSURE_RECENT") {
      applyMostRecentSort()
        .then(() => sendResponse({ ok: true, pageUrl: globalThis.location.href, resultCount: resultCount() }))
        .catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
      return true;
    }
    return false;
  });
})();
