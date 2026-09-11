"""Adversarial offline smoke tests for the LinkedIn content adapter.

No backend or enrichment provider is contacted. The fixtures cover universal
search posts, People results, malformed/hidden cards, name normalization,
location false positives, and SPA mutation notifications.
"""
from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


FRONTEND = Path(__file__).parents[1] / "frontend"
CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
BROWSER = Path(os.environ.get("RADIXSOL_BROWSER_EXECUTABLE", str(CHROME)))


def _install_runtime(page: Page) -> None:
    page.evaluate(
        """() => {
          window.__radixsolMessageListener = null;
          window.__radixsolSentMessages = [];
          window.chrome = {
            runtime: {
              lastError: null,
              onMessage: {
                addListener: (listener) => { window.__radixsolMessageListener = listener; }
              },
              sendMessage: (message, callback) => {
                window.__radixsolSentMessages.push(message);
                if (message.type === 'RADIXSOL_TRUSTED_LINKEDIN_CLICK') {
                  const target = document.elementFromPoint(message.x, message.y);
                  if (target) target.click();
                  if (callback) callback({ ok: Boolean(target) });
                  return;
                }
                if (callback) callback({ ok: true });
              }
            }
          };
        }"""
    )


def _message(page: Page, message: dict) -> dict:
    return page.evaluate(
        """(message) => new Promise((resolve, reject) => {
          const listener = window.__radixsolMessageListener;
          if (!listener) return reject(new Error('LinkedIn listener was not installed.'));
          const asyncResponse = listener(message, {}, resolve);
          if (asyncResponse !== true) {
            setTimeout(() => reject(new Error('No synchronous response.')), 1000);
          }
        })""",
        message,
    )


def _load(page: Page, url: str, markup: str) -> None:
    page.goto(url)
    page.set_content(markup)
    _install_runtime(page)
    page.add_script_tag(path=str(FRONTEND / "linkedin-content.js"))


def _changed_messages(page: Page) -> list[dict]:
    return page.evaluate(
        """() => window.__radixsolSentMessages.filter(
          (message) => message.type === 'RADIXSOL_PLATFORM_RESULTS_CHANGED'
        )"""
    )


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(BROWSER))
        context = browser.new_context()
        context.route("**/*", lambda route: route.fulfill(body="<html><body></body></html>"))

        syntax = context.new_page()
        source = (FRONTEND / "linkedin-content.js").read_text(encoding="utf-8")
        assert syntax.evaluate("source => { new Function(source); return true; }", source)
        syntax.close()

        universal = context.new_page()
        _load(
            universal,
            "https://www.linkedin.com/search/results/all/?keywords=nurse",
            """<!doctype html><html><body><main>
              <article data-view-name="feed-update">
                <a class="update-components-actor__meta-link" href="/in/post-author/">
                  <span>Post Author</span>
                </a>
                <div>ICU Registered Nurse</div><div>Austin, Texas, United States</div>
                <button>Like</button><button>Comment</button><button>Repost</button><button>Send</button>
              </article>
              <ul>
                <li class="reusable-search__result-container">
                  <a class="entity-result__title-text" href="/in/ada-lovelace-rn/">
                    <span>Ada Lovelace (Ada), RN, BSN · 2nd</span>
                  </a>
                  <div class="entity-result__primary-subtitle">ICU Registered Nurse</div>
                  <div class="entity-result__secondary-subtitle">Example Health, Inc.</div>
                </li>
                <li data-view-name="people-search-result"
                    data-entity-urn="urn:li:fsd_profile:grace-hopper">
                  <h3><a href="/in/grace-hopper/">Grace Hopper, PhD</a></h3>
                  <div class="entity-result__primary-subtitle">Engineering Leader</div>
                  <div class="entity-result__secondary-subtitle">Arlington, Virginia, United States</div>
                </li>
                <article>
                  <a href="/in/general-article-author/">General Article Author</a>
                  <div>Registered Nurse</div><div>Boston, Massachusetts, United States</div>
                </article>
              </ul>
            </main></body></html>""",
        )
        result = _message(universal, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"})
        assert result["ok"] is True, result
        assert [profile["name"] for profile in result["profiles"]] == [
            "Ada Lovelace", "Grace Hopper",
        ], result
        assert result["profiles"][0]["location"] == "", result
        assert result["profiles"][0]["roles"] == ["ICU Registered Nurse"], result
        assert result["profiles"][0]["employers"] == [], result
        assert result["profiles"][1]["location"] == "Arlington, Virginia, United States"

        people = context.new_page()
        _load(
            people,
            "https://www.linkedin.com/search/results/people/?keywords=nurse",
            """<!doctype html><html><body><main><div role="list">
              <article>
                <a href="/in/mary-jackson-rn/"><span>Mary Jackson (MJ), RN · 3rd+</span></a>
                <div data-radixsol-field="headline">Registered Nurse/Clinical Coordinator</div>
                <div data-radixsol-field="location">Hampton, Virginia, United States</div>
              </article>
              <article hidden>
                <a href="/in/hidden-person/"><span>Hidden Person</span></a>
                <div data-radixsol-field="headline">Registered Nurse</div>
                <div data-radixsol-field="location">Miami, Florida, United States</div>
              </article>
              <article aria-hidden="true">
                <a href="/in/aria-hidden-person/"><span>Aria Hidden</span></a>
                <div data-radixsol-field="headline">Registered Nurse</div>
                <div data-radixsol-field="location">Denver, Colorado, United States</div>
              </article>
              <article><a href="/in/name-only/">Name Only</a></article>
              <article>
                <a href="/in/anna-nicole-gabiosa/"><span>Anna Nicole Gabiosa BSN RN PCCN · 2nd</span></a>
                <div data-radixsol-field="headline">Registered Nurse</div>
                <div data-radixsol-field="location">Los Angeles, California, United States</div>
              </article>
            </div></main></body></html>""",
        )
        result = _message(people, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"})
        assert result["ok"] is True, result
        assert result["count"] == 2, result
        assert result["profiles"][0]["name"] == "Mary Jackson"
        assert result["profiles"][0]["employers"] == []
        assert result["profiles"][1]["name"] == "Anna Nicole Gabiosa"

        modern_pdf = context.new_page()
        _load(
            modern_pdf,
            "https://www.linkedin.com/in/elana-marsh-bsn-rn-07655a3b/",
            """<!doctype html><html><body><main>
              <div data-view-name="profile-card">
                <h2>Elana Marsh, BSN, RN</h2>
                <p>Registered Nurse at the U.S. Department of Veterans Affairs</p>
                <p>Philadelphia, Pennsylvania, United States</p>
                <button type="button">Message</button>
                <button type="button">Follow</button>
                <button id="modern-more" type="button" aria-label="More" aria-expanded="false">...</button>
              </div>
              <script>
                document.getElementById('modern-more').addEventListener('click', (event) => {
                  event.currentTarget.setAttribute('aria-expanded', 'true');
                  const popover = document.createElement('div');
                  popover.id = 'modern-profile-popover';
                  popover.style.cssText = 'position:fixed;left:180px;top:180px;width:220px;height:60px;background:white';
                  popover.innerHTML = '<div id="modern-save-pdf" role="button" tabindex="0"><span>Save to PDF</span></div>';
                  document.body.appendChild(popover);
                  document.getElementById('modern-save-pdf').addEventListener('click', () => {
                    document.body.dataset.pdfClicked = 'true';
                  });
                });
              </script>
            </main></body></html>""",
        )
        pdf_result = _message(modern_pdf, {"type": "RADIXSOL_AUTO_LINKEDIN_PDF"})
        assert pdf_result["ok"] is True, pdf_result
        assert pdf_result["action"] == "save_to_pdf", pdf_result
        assert modern_pdf.locator("body").get_attribute("data-pdf-clicked") == "true"

        profile = context.new_page()
        _load(
            profile,
            "https://www.linkedin.com/in/elizabeth-cruz-rn/",
            """<!doctype html><html><head><title>Elizabeth Cruz (Liz), BSN, RN | LinkedIn</title></head>
            <body><main><section data-view-name="profile-card">
              <h1>Elizabeth Cruz (Liz), BSN, RN</h1>
              <div class="text-body-medium break-words">Registered Nurse</div>
              <div class="pv-text-details__left-panel mt2">
                <span class="text-body-small inline t-black--light break-words">Virginia Beach, Virginia, United States</span>
                <a id="top-card-text-details-contact-info" href="/in/elizabeth-cruz-rn/overlay/contact-info/">Contact info</a>
              </div>
            </section></main></body></html>""",
        )
        result = _message(profile, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"})
        assert result["profiles"][0]["name"] == "Elizabeth Cruz", result
        assert result["profiles"][0]["roles"] == ["Registered Nurse"], result
        assert result["profiles"][0]["employers"] == [], result

        # Initial state, text mutation, and an empty transition must all reach
        # an already-open side panel without requiring it to be reopened.
        universal.wait_for_timeout(700)
        changed = _changed_messages(universal)
        assert changed and changed[-1]["count"] == 2, changed
        universal.locator(".entity-result__primary-subtitle").first.evaluate(
            "element => { element.firstChild.data = 'Emergency Registered Nurse'; }"
        )
        universal.wait_for_timeout(700)
        changed_after_text = _changed_messages(universal)
        assert len(changed_after_text) > len(changed), changed_after_text
        universal.locator("li.reusable-search__result-container").evaluate(
            "element => element.setAttribute('hidden', '')"
        )
        universal.locator("[data-view-name='people-search-result']").evaluate(
            "element => element.setAttribute('aria-hidden', 'true')"
        )
        universal.wait_for_timeout(700)
        empty = _changed_messages(universal)[-1]
        assert empty["count"] == 0 and empty["empty"] is True, empty

        context.close()
        browser.close()
        print({
            "browser": BROWSER.name,
            "universal_people": 2,
            "people_results": 1,
            "spa_empty_event": True,
        })


if __name__ == "__main__":
    main()
