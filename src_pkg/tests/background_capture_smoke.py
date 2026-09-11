"""Offline regression test for the LinkedIn guided-PDF download guard.

No provider or backend is contacted. The extension service worker runs against
small mocked Chrome APIs so cross-tab and wrong-profile downloads can be tested.
"""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright


BACKGROUND = Path(__file__).parents[1] / "frontend" / "background.js"
CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")


def main():
    source = BACKGROUND.read_text(encoding="utf-8")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(CHROME))
        page = browser.new_page()
        page.goto("about:blank")
        page.evaluate(
            """source => {
              const event = () => ({
                listeners: [],
                addListener(listener) { this.listeners.push(listener); }
              });
              const state = window.__captureState = {
                session: {}, local: {}, messages: [], downloads: {},
                tabs: { 7: { id: 7, url: 'https://www.linkedin.com/in/jane-doe/' } }
              };
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
                async set(values) { Object.assign(state[key], values); },
                async remove(names) {
                  for (const name of names || []) delete state[key][name];
                }
              });
              window.chrome = {
                sidePanel: {
                  async setPanelBehavior() {}, async open() {}
                },
                runtime: {
                  onInstalled: event(), onStartup: event(), onMessage: event(),
                  async sendMessage(message) { state.messages.push(message); return { ok: true }; }
                },
                action: { onClicked: event() },
                debugger: {
                  async attach() {}, async sendCommand() {}, async detach() {}
                },
                tabs: {
                  async get(tabId) {
                    if (!state.tabs[tabId]) throw new Error('No such tab');
                    return state.tabs[tabId];
                  }
                },
                storage: { session: storageArea('session'), local: storageArea('local') },
                downloads: {
                  onCreated: event(), onChanged: event(),
                  async search(query) {
                    return state.downloads[query.id] ? [state.downloads[query.id]] : [];
                  },
                  async download() { return 100; }
                }
              };
              new Function(source)();
              state.arm = (message) => new Promise((resolve) => {
                const listener = chrome.runtime.onMessage.listeners[0];
                const pending = listener(message, {}, resolve);
                if (pending !== true) resolve({ ok: false, error: 'Listener did not remain open.' });
              });
              state.created = async (download) => {
                state.downloads[download.id] = download;
                for (const listener of chrome.downloads.onCreated.listeners) listener(download);
                await new Promise((resolve) => setTimeout(resolve, 20));
              };
              state.changed = async (id, status = 'complete') => {
                const delta = { id, state: { current: status } };
                for (const listener of chrome.downloads.onChanged.listeners) listener(delta);
                await new Promise((resolve) => setTimeout(resolve, 20));
              };
              state.createdAndChanged = async (download, status = 'complete') => {
                state.downloads[download.id] = download;
                for (const listener of chrome.downloads.onCreated.listeners) listener(download);
                const delta = { id: download.id, state: { current: status } };
                for (const listener of chrome.downloads.onChanged.listeners) listener(delta);
                await new Promise((resolve) => setTimeout(resolve, 30));
              };
            }""",
            source,
        )

        result = page.evaluate(
            """async () => {
              const state = window.__captureState;
              const armMessage = {
                type: 'RADIXSOL_ARM_LINKEDIN_PDF_CAPTURE', tabId: 7,
                candidateId: 41, name: 'Jane Doe',
                sourceUrl: 'https://www.linkedin.com/in/jane-doe/'
              };

              state.tabs[7].url = 'https://www.linkedin.com/in/not-jane/';
              const wrongArm = await state.arm(armMessage);
              state.tabs[7].url = 'https://www.linkedin.com/in/jane-doe/?trk=public';
              const exactArm = await state.arm(armMessage);

              await state.created({
                id: 1, tabId: 8, mime: 'application/pdf', filename: 'other.pdf',
                url: 'https://www.linkedin.com/ambry/download.pdf',
                referrer: 'https://www.linkedin.com/in/other-person/'
              });
              const afterOtherTab = state.session.radixsolLinkedinPdfCapture?.downloadId || 0;

              await state.created({
                id: 2, tabId: 7, mime: 'application/pdf', filename: 'jane.pdf',
                url: 'https://www.linkedin.com/ambry/download.pdf',
                referrer: 'https://www.linkedin.com/in/jane-doe/'
              });
              const afterExactTab = state.session.radixsolLinkedinPdfCapture?.downloadId || 0;
              state.tabs[7].url = 'https://www.linkedin.com/in/wrong-at-completion/';
              await state.changed(2);
              const completionMessages = [...state.messages];

              state.tabs[7].url = 'https://www.linkedin.com/in/jane-doe/';
              await state.arm(armMessage);
              await state.created({
                id: 3, mime: 'application/pdf', filename: 'external.pdf',
                url: 'https://example.com/external.pdf'
              });
              const afterMissingTabExternal = state.session.radixsolLinkedinPdfCapture?.downloadId || 0;

              await state.created({
                id: 4, mime: 'application/pdf', filename: 'jane-no-tab.pdf',
                url: 'https://media.licdn.com/profile.pdf',
                referrer: 'https://www.linkedin.com/in/jane-doe/'
              });
              const afterMissingTabLinkedin = state.session.radixsolLinkedinPdfCapture?.downloadId || 0;
              await state.changed(4);

              await state.arm(armMessage);
              await state.createdAndChanged({
                id: 5, tabId: 7, mime: 'application/pdf', filename: 'jane-instant.pdf',
                url: 'https://media.licdn.com/jane-instant.pdf',
                referrer: 'https://www.linkedin.com/in/jane-doe/'
              });

              return {
                wrongArm, exactArm, afterOtherTab, afterExactTab,
                afterMissingTabExternal, afterMissingTabLinkedin,
                messages: state.messages, completionMessages
              };
            }"""
        )
        browser.close()

    assert result["wrongArm"]["ok"] is False, result
    assert "exact LinkedIn profile" in result["wrongArm"]["error"], result
    assert result["exactArm"]["ok"] is True, result
    assert result["afterOtherTab"] == 0, result
    assert result["afterExactTab"] == 2, result
    assert not any(
        message.get("type") == "RADIXSOL_RESUME_DOWNLOADED"
        for message in result["completionMessages"]
    ), result
    assert any(
        message.get("type") == "RADIXSOL_LINKEDIN_PDF_CAPTURE_FAILED"
        for message in result["completionMessages"]
    ), result
    assert result["afterMissingTabExternal"] == 0, result
    assert result["afterMissingTabLinkedin"] == 4, result
    assert any(
        message.get("type") == "RADIXSOL_RESUME_DOWNLOADED"
        and message.get("candidateId") == 41
        for message in result["messages"]
    ), result
    assert any(
        message.get("type") == "RADIXSOL_RESUME_DOWNLOADED"
        and message.get("path", "").endswith("jane-instant.pdf")
        for message in result["messages"]
    ), result
    print("LinkedIn guided-PDF capture guard smoke test passed.")


if __name__ == "__main__":
    main()
