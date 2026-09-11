"""Offline UI smoke test for the panel's public-records action.

The production side-panel JavaScript runs against mocked Chrome APIs and a
mocked local backend, so no external request and no candidate page are
contacted. It covers the three places the action is offered (capture row,
capture review sheet, stored candidate card) and the rendered result sheet.
"""
from __future__ import annotations

import re

from playwright.sync_api import Page, sync_playwright

from sidepanel_lifecycle_smoke import BROWSERS, FRONTEND, MOCK_SCRIPT


PUBLIC_RECORD_RESULT = {
    "status": "found",
    "error": "",
    "cached": False,
    "external_id": 2315,
    "name": "Joseph Michael Antario",
    "source": "familytreenow",
    "emails": ["joseph@example.test"],
    "masked_emails": ["jo************1@example.test"],
    "phones": [
        {
            "value": "(610) 555-0137",
            "type": "Wireless",
            "carrier": "Example Wireless",
            "last_reported": "Jul 2026",
            "primary": True,
        },
        {
            "value": "(610) 555-0188",
            "type": "Landline",
            "carrier": "Example Telecom",
            "last_reported": "Sep 2015",
            "primary": False,
        },
    ],
    "addresses": ["396 Example Dr, Nazareth, PA 18064", "8 N Example Dr, Easton, PA 18042"],
    "current_address": {
        "address": "396 Example Dr, Nazareth, PA 18064",
        "county": "Northampton County",
        "date_range": "Aug 1997 - Jul 2026",
        "property_details": "4 Bed | 4 Bath",
    },
    "age": 67,
    "born": "Jun 1959",
    "also_known_as": ["Joseph Michael Antario", "<img src=x onerror=\"window.__qsXss=true\">"],
    "employment": [{
        "employer": "Everstream Analytics",
        "title": "Senior Pre-Sales Solutions Consultant",
        "industry": "Transportation And Storage",
        "from": "",
        "to": "",
        "location": "",
    }],
    "education": [],
    "relatives": [{"name": "Paula M Antario", "age": 68, "relationship": "Possible Spouse", "deceased": False}],
    "associates": [],
    "businesses": [{"name": "Example Urology, Pc", "address": "175 S Example St, Easton PA"}],
}

# The panel must send the reviewed identity, not the whole captured profile.
_BACKEND_MOCK = """(result) => {
  const state = window.__panelTest;
  state.publicRecordRequests = [];
  state.batchRequests = [];
  const originalFetch = window.fetch;
  window.fetch = async (input, options = {}) => {
    const url = new URL(String(input), 'http://127.0.0.1');
    if (url.pathname === '/health') {
      return new Response(JSON.stringify({
        status: 'ok', mode: 'live', database: 'sqlite',
        records_lookup: { enabled: true, typical_seconds: [30, 90] },
        lookup_behavior: 'sequential',
      }), { status: 200, headers: { 'content-type': 'application/json' } });
    }
    if (url.pathname === '/contact-lookup/batch') {
      const request = JSON.parse(options.body || '{}');
      const ids = request.candidate_ids || [];
      state.batchRequests.push(ids);
      return new Response(JSON.stringify({
        status: 'ok',
        results: Object.fromEntries(ids.map((id) => [String(id), {
          status: 'found',
          emails: ['found@example.test'],
          phones: ['(610) 555-0137', '(610) 555-0188', '(610) 555-0199', '(610) 555-0200'],
          phone_contacts: [
            { value: '(610) 555-0137', kind: 'mobile' },
            { value: '(610) 555-0188', kind: 'other' },
            { value: '(610) 555-0199', kind: 'other' },
            { value: '(610) 555-0200', kind: 'other' },
          ],
          resume_required: false,
          location_match: null,
        }])),
      }), { status: 200, headers: { 'content-type': 'application/json' } });
    }
    if (url.pathname === '/records/find') {
      const request = JSON.parse(options.body || '{}');
      state.publicRecordRequests.push(request);
      const payload = request.name === 'Missing Person'
        ? { status: 'not_found', error: '' }
        : { ...result, cached: Boolean(request.refresh) === false && state.publicRecordRequests.length > 1 };
      return new Response(JSON.stringify(payload), {
        status: 200, headers: { 'content-type': 'application/json' },
      });
    }
    return originalFetch(input, options);
  };
}"""


def _load_panel(page: Page) -> None:
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")
    index = re.sub(r"\s*<script\s+src=[^>]+></script>", "", index, flags=re.I)
    page.set_content(index)
    page.add_style_tag(path=str(FRONTEND / "styles.css"))
    page.evaluate(MOCK_SCRIPT)
    page.evaluate(_BACKEND_MOCK, PUBLIC_RECORD_RESULT)
    page.add_script_tag(path=str(FRONTEND / "profile-quality.js"))
    source = (FRONTEND / "app.js").read_text(encoding="utf-8")
    extension_check = (
        'const IS_EXTENSION = ["chrome-extension:", "moz-extension:"]'
        ".includes(location.protocol);"
    )
    assert extension_check in source
    page.add_script_tag(content=source.replace(extension_check, "const IS_EXTENSION = true;", 1))
    page.wait_for_selector(".capture-row")


def _run_browser(browser_type, executable) -> dict:
    browser = browser_type.launch(executable_path=str(executable), headless=True)
    page = browser.new_page(viewport={"width": 420, "height": 820})
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    _load_panel(page)

    # 1. Every scanned profile offers the lookup once the backend reports it.
    rows = page.locator('.capture-row [data-action="public-records"]')
    row_count = rows.count()
    assert row_count >= 1, "no public records action rendered on scanned rows"
    row_name = rows.first.get_attribute("data-qs-name")
    rows.first.click()
    page.wait_for_selector(".qs-sheet")

    # 2. The result sheet renders contacts, detail, and no injected markup.
    page.wait_for_selector(".qs-identity")
    # Section labels and the primary tag are upper-cased by the stylesheet.
    sheet_text = page.locator(".qs-sheet").inner_text().casefold()
    for expected in (
        "joseph michael antario", "(610) 555-0137", "primary", "example wireless",
        "joseph@example.test", "northampton county", "everstream analytics",
        "masked email", "396 example dr",
    ):
        assert expected in sheet_text, (expected, sheet_text)
    assert page.evaluate("() => window.__qsXss === true") is False
    assert page.locator('.qs-sheet [data-action="public-records-refresh"]').count() == 1
    request = page.evaluate("() => window.__panelTest.publicRecordRequests.at(-1)")
    assert request["name"] == row_name, request
    assert request["refresh"] is False, request

    # 3. "Search again" repeats the same identity with a forced refresh.
    page.locator('.qs-sheet [data-action="public-records-refresh"]').click()
    page.wait_for_function(
        "() => window.__panelTest.publicRecordRequests.length === 2"
    )
    refreshed = page.evaluate("() => window.__panelTest.publicRecordRequests.at(-1)")
    assert refreshed["refresh"] is True and refreshed["name"] == row_name, refreshed
    page.locator('.qs-sheet [data-action="close-modal"]').click()

    # 4. The capture review sheet offers the lookup and can be returned to.
    page.evaluate("() => showIndeedImport(indeedCandidates[0])")
    page.wait_for_selector("#importName")
    reviewed_name = page.input_value("#importName")
    page.locator('.sheet [data-action="public-records"]').click()
    page.wait_for_selector(".qs-sheet")
    from_review = page.evaluate("() => window.__panelTest.publicRecordRequests.at(-1)")
    assert from_review["name"] == reviewed_name, from_review
    page.locator('.qs-sheet [data-action="public-records-back"]').click()
    page.wait_for_selector("#importName")
    assert page.input_value("#importName") == reviewed_name
    page.locator('.sheet [data-action="close-modal"]').click()

    # 5. A miss explains itself instead of rendering an empty record.
    page.evaluate(
        """() => showPublicRecordResult({ status: 'not_found' }, { name: 'Missing Person' })"""
    )
    assert "No public record matched" in page.locator(".qs-sheet .qs-empty").inner_text()
    page.evaluate("() => closeModal()")

    # 6. Without backend support the action disappears entirely.
    page.evaluate("() => { backendHealth = { status: 'ok' }; }")
    assert page.evaluate("() => publicRecordButton('Someone', 'Denver, CO', 4)") == ""

    # 7. A public-records selection goes out one candidate per request, so no
    #    single call has to outlast a 30-90 second search multiplied by the
    #    selection size.
    prompts = []
    page.on("dialog", lambda dialog: (prompts.append(dialog.message), dialog.accept()))
    page.evaluate(
        """() => {
          backendHealth = {
            status: 'ok',
            lookup_behavior: 'sequential',
            records_lookup: { enabled: true, typical_seconds: [30, 90] },
          };
          const profiles = Array.from({ length: 7 }, (_, index) => ({
            name: `Candidate ${index + 1} Example`,
            location: 'Denver, Colorado, United States',
            source: 'linkedin',
            source_id: `candidate-${index + 1}`,
            source_url: `https://www.linkedin.com/in/candidate-${index + 1}/`,
            result_index: index,
            _selectionKey: `id:candidate-${index + 1}`,
            _candidateId: 3000 + index,
            _sourceTabId: 2,
          }));
          activeSourcingPlatform = SOURCING_PLATFORMS.linkedin;
          indeedCandidates = profiles;
          indeedLookupState = new Map();
          indeedLookupScope = new Set();
          indeedLookupSummary = null;
          indeedSelected = new Set(profiles.map((profile) => profile._selectionKey));
          indeedScanState = { phase: 'captured', found: 7, total: 7 };
          renderIndeedProfiles();
        }"""
    )
    page.evaluate("() => lookupSelectedIndeedCandidates()")
    page.wait_for_function("() => window.__panelTest.batchRequests.length === 7")
    batches = page.evaluate("() => window.__panelTest.batchRequests")
    assert all(len(ids) == 1 for ids in batches), batches
    assert sorted(ids[0] for ids in batches) == list(range(3000, 3007)), batches
    # The recruiter is told what a 7-profile run actually costs before it starts.
    assert prompts and "7 selected candidates" in prompts[0], prompts
    assert "minutes" in prompts[0], prompts

    # 8. A row shows the first few contacts, not a dozen phone numbers.
    row_values = page.locator(".capture-row").first.locator(".lookup-value")
    assert row_values.count() == 4, row_values.count()
    assert "+1 more" in page.locator(".capture-row").first.inner_text()

    assert not errors, errors
    requests = page.evaluate("() => window.__panelTest.publicRecordRequests.length")
    browser.close()
    return {
        "rows_with_action": row_count,
        "requests": requests,
        "batches": len(batches),
    }


def main() -> None:
    available = [(name, path) for name, path in BROWSERS if path.exists()]
    assert available, "Chrome or Edge is required for the public records UI smoke test."
    output = {}
    with sync_playwright() as playwright:
        for name, executable in available:
            output[name] = _run_browser(playwright.chromium, executable)
    print({"public_records_panel": "passed", "browsers": output})


if __name__ == "__main__":
    main()
