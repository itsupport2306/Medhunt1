"""Offline service-worker smoke test for persistent active-tab synchronization.

Runs background.js against mocked browser APIs in Chrome and Edge. No external
requests, backend calls, downloads, or provider credits are involved.
"""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright


BACKGROUND = Path(__file__).parents[1] / "frontend" / "background.js"
BROWSERS = [
    ("chrome", Path("C:/Program Files/Google/Chrome/Application/chrome.exe")),
    ("edge", Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")),
]


def _exercise(browser_type, executable: Path, source: str) -> dict:
    browser = browser_type.launch(headless=True, executable_path=str(executable))
    page = browser.new_page()
    page.goto("about:blank")
    page.evaluate(
        """source => {
          const event = () => ({
            listeners: [],
            addListener(listener) { this.listeners.push(listener); },
          });
          const state = window.__tabLifecycle = {
            messages: [], session: {}, local: {}, openCount: 0, behaviorCount: 0,
            activeId: 1,
            tabs: {
              1: { id: 1, windowId: 10, active: true, status: 'complete',
                   url: 'https://employers.indeed.com/smartsourcing?query=rn' },
              2: { id: 2, windowId: 10, active: false, status: 'complete',
                   url: 'https://www.linkedin.com/in/jane-doe/' },
              3: { id: 3, windowId: 10, active: false, status: 'complete',
                   url: 'chrome://extensions/' },
            },
          };
          const clone = (value) => JSON.parse(JSON.stringify(value));
          const storageArea = (key) => ({
            async get(names) {
              const output = {};
              for (const name of names || []) {
                if (Object.prototype.hasOwnProperty.call(state[key], name)) {
                  output[name] = state[key][name];
                }
              }
              return output;
            },
            async set(values) { Object.assign(state[key], values || {}); },
            async remove(names) { for (const name of names || []) delete state[key][name]; },
          });
          const runtimeMessage = event();
          window.chrome = {
            sidePanel: {
              async setPanelBehavior() { state.behaviorCount += 1; },
              async open() { state.openCount += 1; },
            },
            runtime: {
              onInstalled: event(), onStartup: event(), onMessage: runtimeMessage,
              async sendMessage(message) { state.messages.push(clone(message)); return { ok: true }; },
            },
            action: { onClicked: event() },
            debugger: {
              async attach() {}, async sendCommand() {}, async detach() {},
            },
            tabs: {
              onActivated: event(), onUpdated: event(), onRemoved: event(),
              async get(tabId) {
                if (!state.tabs[tabId]) throw new Error('No such tab');
                return clone(state.tabs[tabId]);
              },
              async query(query) {
                if (query?.active) return state.tabs[state.activeId]
                  ? [clone(state.tabs[state.activeId])] : [];
                return Object.values(state.tabs).map(clone);
              },
            },
            windows: { WINDOW_ID_NONE: -1, onFocusChanged: event() },
            storage: { session: storageArea('session'), local: storageArea('local') },
            downloads: {
              onCreated: event(), onChanged: event(),
              async search() { return []; }, async download() { return 500; },
            },
          };
          new Function(source)();

          state.setActive = (tabId) => {
            for (const tab of Object.values(state.tabs)) tab.active = Number(tab.id) === Number(tabId);
            state.activeId = Number(tabId);
          };
          state.fire = (target, ...args) => {
            for (const listener of chrome.tabs[target].listeners) listener(...args);
          };
          state.clickAction = async (tabId) => {
            for (const listener of chrome.action.onClicked.listeners) {
              await listener(clone(state.tabs[tabId]));
            }
          };
          state.focus = (windowId) => {
            for (const listener of chrome.windows.onFocusChanged.listeners) listener(windowId);
          };
          state.ask = (message) => new Promise((resolve, reject) => {
            const listener = runtimeMessage.listeners[0];
            let settled = false;
            const done = (value) => { if (!settled) { settled = true; resolve(value); } };
            const pending = listener(message, {}, done);
            if (pending !== true && !settled) resolve(undefined);
            setTimeout(() => { if (!settled) reject(new Error('Timed out')); }, 1000);
          });
        }""",
        source,
    )

    result = page.evaluate(
        """async () => {
          const state = window.__tabLifecycle;
          await state.clickAction(1);

          state.messages = [];
          state.setActive(2);
          state.fire('onActivated', { tabId: 2, windowId: 10 });
          await new Promise((resolve) => setTimeout(resolve, 30));
          const activated = [...state.messages];

          state.messages = [];
          state.tabs[2].url = 'https://www.linkedin.com/search/results/people/?keywords=nurse';
          state.tabs[2].status = 'loading';
          state.fire('onUpdated', 2, { url: state.tabs[2].url, status: 'loading' },
            { ...state.tabs[2] });
          await new Promise((resolve) => setTimeout(resolve, 40));
          state.tabs[2].status = 'complete';
          state.fire('onUpdated', 2, { status: 'complete' }, { ...state.tabs[2] });
          await new Promise((resolve) => setTimeout(resolve, 330));
          const navigated = [...state.messages];

          state.messages = [];
          state.setActive(3);
          state.fire('onActivated', { tabId: 3, windowId: 10 });
          await new Promise((resolve) => setTimeout(resolve, 30));
          const unsupported = [...state.messages];

          state.messages = [];
          state.setActive(1);
          state.fire('onRemoved', 2, { windowId: 10, isWindowClosing: false });
          await new Promise((resolve) => setTimeout(resolve, 130));
          const removed = [...state.messages];

          state.messages = [];
          state.focus(10);
          await new Promise((resolve) => setTimeout(resolve, 30));
          const focused = [...state.messages];
          state.focus(chrome.windows.WINDOW_ID_NONE);
          await new Promise((resolve) => setTimeout(resolve, 20));
          const afterNoWindow = [...state.messages];

          const requested = await state.ask({ type: 'RADIXSOL_GET_ACTIVE_TAB_CONTEXT' });
          return {
            behaviorCount: state.behaviorCount, openCount: state.openCount,
            activated, navigated, unsupported, removed, focused, afterNoWindow, requested,
          };
        }"""
    )
    browser.close()
    return result


def main() -> None:
    source = BACKGROUND.read_text(encoding="utf-8")
    available = [(name, path) for name, path in BROWSERS if path.exists()]
    assert available, "Chrome or Edge is required for the background lifecycle smoke test."
    results = {}
    with sync_playwright() as playwright:
        for name, executable in available:
            result = _exercise(playwright.chromium, executable, source)
            assert result["behaviorCount"] >= 1, result
            assert result["openCount"] == 1, result

            assert len(result["activated"]) == 1, result
            assert result["activated"][0]["tab_id"] == 2, result
            assert result["activated"][0]["platform"] == "linkedin", result
            assert result["activated"][0]["reason"] == "activated", result

            # Debouncing suppresses the intermediate loading URL and publishes
            # one final, complete context.
            assert len(result["navigated"]) == 1, result
            assert result["navigated"][0]["status"] == "complete", result
            assert result["navigated"][0]["reason"] == "loaded", result
            assert "/search/results/people/" in result["navigated"][0]["url"], result

            assert len(result["unsupported"]) == 1, result
            assert result["unsupported"][0]["platform"] == "", result
            assert result["unsupported"][0]["url"] == "chrome://extensions/", result

            assert len(result["removed"]) == 1, result
            assert result["removed"][0]["tab_id"] == 1, result
            assert result["removed"][0]["reason"] == "removed", result
            assert len(result["focused"]) == 1, result
            assert result["focused"][0]["reason"] == "window-focused", result
            assert result["afterNoWindow"] == result["focused"], result
            assert result["requested"]["tab_id"] == 1, result
            assert result["requested"]["platform"] == "indeed", result

            # Switching and navigation never close or reopen the panel.
            assert result["openCount"] == 1, result
            results[name] = {
                "activated": True,
                "navigation_debounced": True,
                "unsupported_broadcast": True,
                "panel_open_count": result["openCount"],
            }
    print({"background_tab_lifecycle": "passed", "browsers": results})


if __name__ == "__main__":
    main()
