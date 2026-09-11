"""Offline edge-case smoke test for the Facebook profile content adapter.

The test uses local DOM fixtures only. It never contacts Facebook, the
Radixsol backend, or an enrichment provider.
"""
from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import sync_playwright


FRONTEND = Path(__file__).parents[1] / "frontend"
CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
BROWSER = Path(os.environ.get("RADIXSOL_BROWSER_EXECUTABLE", str(CHROME)))


def _install_runtime(page):
    page.evaluate(
        """() => {
          window.__radixsolMessageListener = null;
          window.__radixsolRuntimeMessages = [];
          window.chrome = {
            runtime: {
              lastError: null,
              onMessage: {
                addListener: (listener) => { window.__radixsolMessageListener = listener; },
                removeListener: (listener) => {
                  if (window.__radixsolMessageListener === listener) {
                    window.__radixsolMessageListener = null;
                  }
                }
              },
              sendMessage: (message, callback) => {
                window.__radixsolRuntimeMessages.push(message);
                if (callback) callback({ok: true});
              }
            }
          };
        }"""
    )


def _message(page, message):
    return page.evaluate(
        """(message) => new Promise((resolve, reject) => {
          const listener = window.__radixsolMessageListener;
          if (!listener) return reject(new Error('Facebook listener was not installed.'));
          listener(message, {}, resolve);
        })""",
        message,
    )


def _page(context, url, markup):
    page = context.new_page()
    page.goto(url)
    page.set_content(markup)
    _install_runtime(page)
    page.add_script_tag(path=str(FRONTEND / "facebook-content.js"))
    return page


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(BROWSER))
        context = browser.new_context()
        context.route("**/*", lambda route: route.fulfill(body="<html><body></body></html>"))

        syntax = context.new_page()
        source = (FRONTEND / "facebook-content.js").read_text(encoding="utf-8")
        assert syntax.evaluate("source => { new Function(source); return true; }", source)
        syntax.close()

        business = _page(
            context,
            "https://www.facebook.com/northlightmedical",
            """<!doctype html><html><head>
              <meta property="og:type" content="business.business">
              <title>Northlight Medical | Facebook</title></head><body>
              <main role="main" aria-label="Northlight Medical Facebook Page">
                <section><a href="/northlightmedical"><h1>Northlight Medical</h1></a>
                  <div>Page · Medical service</div>
                  <button>Like</button><button>Follow</button><button>Call now</button>
                  <div aria-label="Page transparency">Page transparency</div>
                  <div>Lives in Wrongtown, Ohio</div>
                </section>
              </main></body></html>""",
        )
        business_ping = _message(business, {"type": "RADIXSOL_PLATFORM_PING"})
        business_result = _message(
            business, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"}
        )
        assert business_ping["supported_profile"] is False, business_ping
        assert business_ping["profile_kind"] == "page", business_ping
        assert business_result["ok"] is False, business_result
        assert business_result["error_code"] == "FACEBOOK_PAGE_UNSUPPORTED"
        assert business_result["profiles"] == []

        # Professional-mode personal profiles may expose Follow/followers and
        # no Add Friend button. They remain valid regardless of occupation.
        professional = _page(
            context,
            "https://www.facebook.com/avery.quinn.creates",
            """<!doctype html><html><head><meta property="og:type" content="profile"></head>
              <body><main role="main">
                <section data-pagelet="ProfileCover" aria-label="Profile header">
                  <a href="/avery.quinn.creates"><h1>Dr. Avery Quinn, MBA, CPA (AQ)</h1></a>
                  <div>Professional mode</div><div>12K followers</div>
                  <button>Follow</button><button>Message</button>
                </section>
                <section data-pagelet="ProfileTilesFeed_0"><h2>Intro</h2>
                  <div>Lives in Austin, Texas</div><div>From Tulsa, Oklahoma</div>
                  <div>Works at Northlight Studio</div>
                </section>
                <section aria-label="Posts"><h2>Posts</h2><article>
                  <h2>Wrong Person</h2><div>Lives in Miami, Florida</div>
                  <div>Registered Nurse at Wrong Hospital</div>
                  <div aria-label="Comments">A comment about Boston, Massachusetts</div>
                </article></section>
              </main></body></html>""",
        )
        professional_result = _message(
            professional, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"}
        )
        assert professional_result["ok"] is True, professional_result
        profile = professional_result["profiles"][0]
        assert profile["name"] == "Avery Quinn", professional_result
        assert profile["location"] == "Austin, Texas", professional_result
        assert profile["hometown"] == "Tulsa, Oklahoma", professional_result
        assert profile["employers"] == ["Northlight Studio"], professional_result
        assert profile["alternate_names"] == ["AQ"], professional_result
        assert "Professional descriptor: MBA" in profile["notes"]
        assert "Professional descriptor: CPA" in profile["notes"]
        assert "Wrong Person" not in profile["notes"]
        assert "Miami, Florida" not in profile["notes"]
        assert "Wrong Hospital" not in profile["notes"]

        locked = _page(
            context,
            "https://www.facebook.com/locked.profile",
            """<main role="main"><section aria-label="Profile header">
              <h1>Locked Profile</h1><div>100 friends</div><button>Add friend</button>
              <div role="status">This profile is locked</div>
              <div>Lives in Denver, Colorado</div>
            </section></main>""",
        )
        locked_result = _message(locked, {"type": "RADIXSOL_CAPTURE_PLATFORM_PROFILE"})
        assert locked_result["ok"] is False, locked_result
        assert locked_result["error_code"] == "FACEBOOK_PROFILE_LOCKED"
        assert locked_result["profiles"] == []

        # Initial, character-data, and empty transitions must all be emitted.
        changing = _page(
            context,
            "https://www.facebook.com/jordan.rivers",
            """<!doctype html><html><head><title>Jordan Rivers | Facebook</title></head><body>
              <main id="profile-main" role="main"><section aria-label="Profile header">
              <a href="/jordan.rivers"><h1>Jordan Rivers, BSN, RN (JR)</h1></a>
              <div>80 friends</div><button>Add friend</button>
              <div id="current-city">Lives in Phoenix, Arizona</div>
              <div id="hometown">From Reno, Nevada</div>
              <div id="employer">Works at First Health</div>
            </section><section aria-label="Posts"><article id="post">
              <div>Lives in False City, Texas</div>
            </article></section></main></body></html>""",
        )
        initial_messages = changing.evaluate("() => window.__radixsolRuntimeMessages")
        assert any(
            message.get("initial") is True and message.get("count") == 1
            for message in initial_messages
        ), initial_messages
        baseline_count = len(initial_messages)
        changing.evaluate(
            """() => {
              document.querySelector('#hometown').firstChild.data = 'From Boise, Idaho';
              document.querySelector('#employer').firstChild.data = 'Works at Second Health';
            }"""
        )
        changing.wait_for_timeout(650)
        detail_messages = changing.evaluate("() => window.__radixsolRuntimeMessages")
        assert len(detail_messages) == baseline_count + 1, detail_messages
        refreshed = _message(changing, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"})
        refreshed_profile = refreshed["profiles"][0]
        assert refreshed_profile["hometown"] == "Boise, Idaho", refreshed
        assert refreshed_profile["employers"] == ["Second Health"], refreshed

        # A post-only mutation is observed by the DOM observer but produces no
        # candidate signature change because posts are outside capture scope.
        changing.evaluate(
            "() => { document.querySelector('#post').firstElementChild.textContent = 'Lives in Seattle, Washington'; }"
        )
        changing.wait_for_timeout(650)
        post_messages = changing.evaluate("() => window.__radixsolRuntimeMessages")
        assert len(post_messages) == len(detail_messages), post_messages

        changing.evaluate("() => document.querySelector('#profile-main').remove()")
        changing.wait_for_timeout(650)
        empty_messages = changing.evaluate("() => window.__radixsolRuntimeMessages")
        assert len(empty_messages) == len(post_messages) + 1, empty_messages
        assert empty_messages[-1]["count"] == 0, empty_messages[-1]
        assert empty_messages[-1]["error_code"] == "FACEBOOK_PROFILE_LOADING"

        print({
            "business_page_rejected": True,
            "professional_profile_captured": profile["name"],
            "locked_skipped": True,
            "transition_messages": len(empty_messages),
        })
        context.close()
        browser.close()


if __name__ == "__main__":
    main()
