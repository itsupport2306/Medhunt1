"""Offline scheduler and page-adapter smoke tests for Medhunt Watcher.

Chrome APIs, Indeed cards, and backend responses are mocked. No provider
network or credits are used. The source is executed in both Chrome and Edge.
"""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright


PROJECT = Path(__file__).parents[2]
WATCHER = PROJECT / "src_pkg" / "watcher_frontend"
BROWSERS = (
    ("Chrome", Path("C:/Program Files/Google/Chrome/Application/chrome.exe")),
    ("Edge", Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")),
)


BACKGROUND_MOCK = r"""() => {
  const event = () => ({ listeners: [], addListener(fn) { this.listeners.push(fn); } });
  const runtimeEvent = event();
  const local = {};
  const mock = window.__watcherMock = {
    version: 1, imports: [], lookups: [], notifications: [], local, lookupDelay: 0,
    notificationFailures: 0,
    // Indeed can render and respond before Chrome changes the tab status to
    // complete. The watcher must use content-script readiness in this state.
    tabStatus: 'loading', contentReady: false, injections: [],
    profile(name, location, id, role, marker) {
      return {
        name, location, source: 'indeed', source_id: id,
        source_url: `https://employers.indeed.com/smartsourcing?candidateId=${id}`,
        headline: role, roles: [role], employers: ['General Hospital'], schools: [],
        notes: `Role: ${role}\nEmployer: General Hospital\n${marker}`,
        result_index: id === 'alice' ? 0 : id === 'bob' ? 1 : 2,
      };
    },
    profiles() {
      const aliceRole = this.version === 1 ? 'Registered Nurse' : 'ICU Registered Nurse';
      const profiles = [
        this.profile('Alice Morgan', 'Columbus, OH', 'alice', aliceRole, 'Resume updated just now'),
        this.profile('Bob Taylor', 'Cleveland, OH', 'bob', 'PCU Nurse', 'Resume updated 2 hours ago'),
      ];
      if (this.version > 1) profiles.push(
        this.profile('New Person', 'Akron, OH', 'new-person', 'Telemetry Nurse', 'Resume updated just now')
      );
      return profiles;
    },
  };
  const copy = value => JSON.parse(JSON.stringify(value));
  window.chrome = {
    runtime: {
      lastError: null,
      onInstalled: event(), onStartup: event(), onMessage: runtimeEvent,
    },
    action: { onClicked: event() },
    scripting: {
      async executeScript(details) {
        mock.injections.push(details);
        mock.contentReady = true;
      },
    },
    alarms: {
      onAlarm: event(),
      async clear() { return true; },
      async create() {},
    },
    sidePanel: { async setPanelBehavior() {}, async open() {} },
    debugger: { async attach() {}, async sendCommand() {}, async detach() {} },
    storage: {
      local: {
        async get(keys) {
          const names = Array.isArray(keys) ? keys : [keys];
          return Object.fromEntries(names.filter(name => name in local).map(name => [name, copy(local[name])]));
        },
        async set(values) { Object.assign(local, copy(values)); },
      },
    },
    tabs: {
      async query() { return [{ id: 7, active: true, status: mock.tabStatus, url: 'https://employers.indeed.com/smartsourcing' }]; },
      async get(id) { return { id, active: true, status: mock.tabStatus, url: local.medhuntWatcherState?.searchUrl || 'https://employers.indeed.com/smartsourcing' }; },
      async update(id, patch) { return { id, active: false, status: mock.tabStatus, url: patch.url }; },
      async create(patch) { return { id: 8, active: false, status: mock.tabStatus, url: patch.url }; },
      sendMessage(id, message, callback) {
        if (!mock.contentReady) {
          window.chrome.runtime.lastError = { message: 'Could not establish connection. Receiving end does not exist.' };
          callback();
          window.chrome.runtime.lastError = null;
          return;
        }
        if (message.type === 'MEDHUNT_WATCHER_PING') callback({ ok: true });
        else if (message.type === 'MEDHUNT_WATCHER_CAPTURE_SEARCH') callback({
          ok: true, pageUrl: 'https://employers.indeed.com/smartsourcing?candidateId=alice&q=rn&sort=recent#profile',
          query: '(RN OR "Registered Nurse")', location: 'Ohio', sort: 'most_recent', resultCount: 2
        });
        else if (message.type === 'MEDHUNT_WATCHER_ENSURE_RECENT') callback({
          ok: true, pageUrl: 'https://employers.indeed.com/smartsourcing?q=rn&sort=recent'
        });
        else if (message.type === 'RADIXSOL_SCAN_INDEED_CANDIDATES') callback({
          ok: true, profiles: mock.profiles(), page_url: 'https://employers.indeed.com/smartsourcing?q=rn&sort=recent'
        });
        else callback({ ok: false, error: `Unexpected tab message ${message.type}` });
      },
    },
  };
  window.fetch = async (url, options = {}) => {
    const path = new URL(url).pathname;
    let payload;
    if (path === '/health') payload = { ok: true };
    else if (path === '/candidates/import/batch') {
      const request = JSON.parse(options.body || '{}');
      mock.imports.push(request.profiles);
      payload = {
        saved: request.profiles.length, imported: request.profiles.length, existing: 0,
        results: request.profiles.map((profile, index) => ({ id: 101 + index, imported: true, candidate: {} })),
      };
    } else if (path === '/contact-lookup/batch') {
      const request = JSON.parse(options.body || '{}');
      mock.lookups.push(request.candidate_ids);
      if (mock.lookupDelay) await new Promise(resolve => setTimeout(resolve, mock.lookupDelay));
      payload = { results: Object.fromEntries(request.candidate_ids.map((id) => [String(id), {
        status: id === 101 ? 'found' : 'not_found',
        emails: id === 101 ? ['alice@example.test'] : [],
        phones: id === 101 ? ['+16145550100'] : [],
        phone_contacts: id === 101 ? [{ value: '+16145550100', kind: 'mobile' }] : [],
        resume_required: false,
      }])) };
    } else if (path === '/watcher/resume-notifications') {
      const request = JSON.parse(options.body || '{}');
      mock.notifications.push(request);
      if (mock.notificationFailures > 0) {
        mock.notificationFailures -= 1;
        payload = { status: 'failed', sent: 0, deduplicated: 0, failed: 1, configured: true };
      } else {
        payload = { status: 'sent', sent: 1, deduplicated: 0, failed: 0, configured: true };
      }
    } else throw new Error(`Unexpected API path ${path}`);
    return { ok: true, headers: { get: () => 'application/json' }, async json() { return payload; } };
  };
  window.sendWatcher = payload => new Promise((resolve, reject) => {
    const listener = runtimeEvent.listeners.find(fn => {
      let used = false;
      try { used = fn(payload, { tab: { id: 7 } }, resolve) === true; } catch (error) { reject(error); }
      return used;
    });
    if (!listener) reject(new Error(`No listener accepted ${payload.type}`));
  });
}"""


CONTENT_MOCK = r"""() => {
  const runtime = { listeners: [], addListener(fn) { this.listeners.push(fn); } };
  window.chrome = { runtime: { onMessage: runtime } };
  window.sendContent = payload => new Promise((resolve, reject) => {
    let accepted = false;
    for (const listener of runtime.listeners) {
      try {
        if (listener(payload, {}, resolve) === true) { accepted = true; break; }
      } catch (error) { reject(error); return; }
    }
    if (!accepted) reject(new Error(`No content listener accepted ${payload.type}`));
  });
}"""


CONTENT_HTML = """<!doctype html><html><body>
  <input aria-label="Job title, keywords, or Boolean search" id="query">
  <input aria-label="City, state, or ZIP code" id="location">
  <button id="find">Find</button>
  <button id="sort">Sort by: Relevance</button>
  <div id="menu" hidden><button id="recent">Most recent</button></div>
  <main id="results"></main>
  <script>
    document.querySelector('#find').onclick = () => {
      document.querySelector('#results').innerHTML = `<article data-cauto-id="MATCH_CARD_BASE-alice">
        <a data-cauto-id="candidate-name">Alice Morgan</a><div>Columbus, OH</div>
      </article>`;
    };
    document.querySelector('#sort').onclick = () => { document.querySelector('#menu').hidden = false; };
    document.querySelector('#recent').onclick = () => {
      document.querySelector('#sort').textContent = 'Sort by: Most recent';
      document.querySelector('#menu').hidden = true;
    };
  </script>
</body></html>"""


def wait_for_background_idle(page, imports: int, changed: int):
    page.wait_for_function(
        """expected => {
          const state = window.__watcherMock.local.medhuntWatcherState || {};
          return state.running === false && window.__watcherMock.imports.length === expected.imports &&
            state.changedCount === expected.changed;
        }""",
        arg={"imports": imports, "changed": changed},
        timeout=30_000,
    )


def test_browser(playwright, name: str, executable: Path) -> None:
    browser = playwright.chromium.launch(headless=True, executable_path=str(executable))
    context = browser.new_context()

    background = context.new_page()
    background.evaluate(BACKGROUND_MOCK)
    background.add_script_tag(content=(WATCHER / "watcher-background.js").read_text(encoding="utf-8"))
    relative_marker_action = background.evaluate("""() => {
      const profile = __watcherMock.profile(
        'Bob Taylor', 'Cleveland, OH', 'bob', 'PCU Nurse', 'Resume updated 2 hours ago'
      );
      const observedAt = Date.now();
      return classifyProfile(profile, {
        fingerprint: profileFingerprint(profile),
        resumeMarker: 'Resume updated 2 hours ago',
        resumeEstimate: observedAt - 24 * 60 * 60 * 1000,
        retry: false,
      }, observedAt).action;
    }""")
    assert relative_marker_action == "unchanged", "an identical relative marker must not drift into an update"
    started = background.evaluate("""() => sendWatcher({
      type: 'MEDHUNT_WATCHER_START', intervalMinutes: 2
    })""")
    assert started["ok"] is True
    state = started["state"]
    assert state["visibleCount"] == 2 and state["changedCount"] == 2 and state["foundCount"] == 1
    assert state["query"] == '(RN OR "Registered Nurse")' and state["location"] == "Ohio"
    assert state["searchUrl"] == "https://employers.indeed.com/smartsourcing?q=rn&sort=recent"
    assert background.evaluate("__watcherMock.tabStatus") == "loading"
    assert len(background.evaluate("__watcherMock.injections")) == 2
    assert len(background.evaluate("__watcherMock.imports")) == 1
    assert len(background.evaluate("__watcherMock.notifications")) == 0, "initial baseline must not send an alert"

    no_change = background.evaluate("() => runCycle()")
    assert no_change["changedCount"] == 0 and no_change["retryCount"] == 0
    assert len(background.evaluate("__watcherMock.imports")) == 1
    assert background.evaluate("__watcherMock.lookups") == [[101], [102]]
    assert len(background.evaluate("__watcherMock.notifications")) == 0

    background.evaluate("__watcherMock.local.medhuntWatcherState.checkpoints.bob.retry = true")
    retried = background.evaluate("() => runCycle()")
    assert retried["changedCount"] == 0 and retried["retryCount"] == 1
    assert len(background.evaluate("__watcherMock.imports")) == 2
    assert len(background.evaluate("__watcherMock.notifications")) == 0, "retries must not send change alerts"

    background.evaluate("__watcherMock.version = 2")
    background.evaluate("__watcherMock.lookupDelay = 250")
    background.evaluate("__watcherMock.notificationFailures = 1")
    background.evaluate("Promise.all([sendWatcher({type: 'MEDHUNT_WATCHER_RUN_NOW'}), sendWatcher({type: 'MEDHUNT_WATCHER_RUN_NOW'})])")
    wait_for_background_idle(background, 3, 2)
    assert len(background.evaluate("__watcherMock.imports[2]")) == 2
    assert background.evaluate("__watcherMock.lookups.slice(3)") == [[101], [102]]
    assert background.evaluate("__watcherMock.lookups.every(ids => ids.length === 1)") is True
    assert len(background.evaluate("__watcherMock.imports")) == 3, "concurrent triggers must share one cycle"
    notifications = background.evaluate("__watcherMock.notifications")
    assert len(notifications) == 1
    assert {profile["name"] for profile in notifications[0]["profiles"]} == {"Alice Morgan", "New Person"}
    assert len(background.evaluate("__watcherMock.local.medhuntWatcherState.pendingNotifications")) == 1

    background.evaluate("sendWatcher({type: 'MEDHUNT_WATCHER_RUN_NOW'})")
    wait_for_background_idle(background, 3, 0)
    notifications = background.evaluate("__watcherMock.notifications")
    assert len(notifications) == 2
    assert notifications[0]["event_id"] == notifications[1]["event_id"]
    assert background.evaluate("__watcherMock.local.medhuntWatcherState.pendingNotifications.length") == 0

    content = context.new_page()
    content.set_content(CONTENT_HTML)
    content.evaluate(CONTENT_MOCK)
    content.add_script_tag(content=(WATCHER / "watcher-content.js").read_text(encoding="utf-8"))
    unprepared = content.evaluate("() => sendContent({type: 'MEDHUNT_WATCHER_CAPTURE_SEARCH'})")
    assert unprepared["ok"] is False and "Most recent" in unprepared["error"]

    content.locator("#query").fill('(RN OR "Registered Nurse")')
    content.locator("#location").fill("Ohio")
    content.locator("#find").click()
    content.locator("#sort").click()
    content.locator("#recent").click()
    captured = content.evaluate("() => sendContent({type: 'MEDHUNT_WATCHER_CAPTURE_SEARCH'})")
    assert captured["ok"] is True and captured["sort"] == "most_recent"
    assert captured["query"] == '(RN OR "Registered Nurse")' and captured["location"] == "Ohio"
    assert "Most recent" in content.locator("#sort").inner_text()
    assert content.locator("[data-cauto-id^='MATCH_CARD_BASE-']").count() == 1

    content.evaluate("document.querySelector('#sort').textContent = 'Sort by: Relevance'")
    ensured = content.evaluate("() => sendContent({type: 'MEDHUNT_WATCHER_ENSURE_RECENT'})")
    assert ensured["ok"] is True and "Most recent" in content.locator("#sort").inner_text()
    context.close()
    browser.close()
    print(f"Medhunt Watcher smoke test passed in {name}")


def main() -> None:
    missing = [name for name, executable in BROWSERS if not executable.exists()]
    if missing:
        raise RuntimeError(f"Missing browser executables: {', '.join(missing)}")
    with sync_playwright() as playwright:
        for name, executable in BROWSERS:
            test_browser(playwright, name, executable)


if __name__ == "__main__":
    main()
