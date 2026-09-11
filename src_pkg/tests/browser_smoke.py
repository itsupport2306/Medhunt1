"""Browser smoke test for the unpacked Medhunt extension.

The private contact endpoint is intercepted, so this test spends no provider credits.
Prerequisites: fixture server on 127.0.0.1:8765 and either a launched test
browser or Chrome remote debugging on the configured CDP port.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright


SYSTEM_CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")


async def main() -> None:
    cdp_url = os.getenv(
        "MEDHUNT_CDP_URL", os.getenv("RADIXSOL_CDP_URL", "http://127.0.0.1:9225")
    )
    screenshot = Path(__file__).with_name("medhunt_extension_smoke.png")
    result_screenshot = Path(__file__).with_name("medhunt_extension_results_smoke.png")
    launch_browser = os.getenv(
        "MEDHUNT_LAUNCH_BROWSER", os.getenv("RADIXSOL_LAUNCH_BROWSER", "")
    ) == "1"
    backend_url = os.getenv(
        "MEDHUNT_BACKEND_URL", os.getenv("RADIXSOL_BACKEND_URL", "")
    )
    browser_profile = os.getenv(
        "MEDHUNT_BROWSER_PROFILE",
        os.getenv("RADIXSOL_BROWSER_PROFILE", "C:/tmp/medhunt-playwright-extension"),
    )

    async with async_playwright() as playwright:
        if launch_browser:
            extension = Path(__file__).parents[1] / "frontend"
            context = await playwright.chromium.launch_persistent_context(
                browser_profile,
                headless=False,
                executable_path=str(SYSTEM_CHROME) if SYSTEM_CHROME.exists() else None,
                args=[
                    f"--disable-extensions-except={extension}",
                    f"--load-extension={extension}",
                    "--disable-features=DisableLoadExtensionCommandLineSwitch",
                    "--host-resolver-rules=MAP indeed.com 127.0.0.1",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            browser = context.browser
            indeed_page = context.pages[0] if context.pages else await context.new_page()
            await indeed_page.goto("http://indeed.com:8765/indeed_results.html")
            if context.service_workers:
                worker = context.service_workers[0]
            else:
                try:
                    worker = await context.wait_for_event(
                        "serviceworker",
                        predicate=lambda item: item.url.startswith("chrome-extension://"),
                        timeout=10_000,
                    )
                except PlaywrightTimeoutError:
                    # MV3 service workers are lazy; chrome://extensions below
                    # can still discover an installed unpacked extension.
                    worker = None
            targets = []
        else:
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0]
            indeed_page = next(page for page in context.pages if "indeed.com:8765" in page.url)
            with urllib.request.urlopen(f"{cdp_url}/json", timeout=5) as response:
                targets = json.load(response)
            worker = next((
                item for item in targets
                if item.get("type") == "service_worker"
                and item.get("url", "").startswith("chrome-extension://")
            ), None)

        lookup_started = asyncio.Event()
        lookup_release = asyncio.Event()

        async def mock_lookup(route) -> None:
            request = route.request.post_data_json
            candidate_ids = request.get("candidate_ids") or [1]
            assert request.get("confirmed") is True
            assert request.get("run_id")
            lookup_started.set()
            await lookup_release.wait()
            match = {
                "status": "found",
                "emails": ["alex.morgan@example.test"],
                "phones": ["(404) 555-0187"],
                "phone_contacts": [{"value": "(404) 555-0187", "kind": "mobile"}],
                "resume_required": False,
            }
            no_match = {
                "status": "not_found",
                "emails": [],
                "phones": [],
            }
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "results": {
                        str(cid): (match if index == 0 else no_match)
                        for index, cid in enumerate(candidate_ids)
                    },
                }),
            )

        async def mock_health(route) -> None:
            await route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "status": "ok", "mode": "live", "database": "sqlite",
                "pdl": {"configured": True, "enabled": True, "run_credit_limit": 0},
                "enformion": {"configured": True, "enabled": True, "run_credit_limit": 10},
            }))

        async def mock_jobs(route) -> None:
            await route.fulfill(status=200, content_type="application/json", body="[]")

        async def mock_import(route) -> None:
            profiles = route.request.post_data_json.get("profiles") or []
            await route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "saved": len(profiles), "imported": len(profiles), "existing": 0,
                "database": "sqlite",
                "results": [
                    {"id": index + 1, "imported": True, "candidate": {
                        "id": index + 1, "enrich_status": "pending", "verification": {},
                    }}
                    for index, _profile in enumerate(profiles)
                ],
            }))

        await context.route("**/health", mock_health)
        await context.route("**/jobs", mock_jobs)
        await context.route("**/candidates/import/batch", mock_import)
        await context.route("**/contact-lookup/batch", mock_lookup)
        extension_id = os.getenv(
            "MEDHUNT_EXTENSION_ID", os.getenv("RADIXSOL_EXTENSION_ID", "")
        )
        if launch_browser and worker:
            extension_id = worker.url.split("/")[2]
        if not extension_id:
            extensions_page = await context.new_page()
            await extensions_page.goto("chrome://extensions")
            await extensions_page.wait_for_timeout(500)
            installed = await extensions_page.evaluate(
                """() => {
                  const manager = document.querySelector('extensions-manager');
                  const list = manager?.shadowRoot?.querySelector('extensions-item-list');
                  const items = list?.shadowRoot?.querySelectorAll('extensions-item') || [];
                  return Array.from(items).map((item) => ({
                    id: item.id,
                    name: item.shadowRoot?.querySelector('#name')?.textContent?.trim() || ''
                  }));
                }"""
            )
            await extensions_page.close()
            match = next((item for item in installed if "Medhunt" in item.get("name", "")), None)
            extension_id = match["id"] if match else ""
        if not extension_id and isinstance(worker, dict) and "medhunt" in worker.get("title", "").lower():
            extension_id = worker["url"].split("/")[2]
        if not extension_id:
            raise RuntimeError("Medhunt extension is not loaded in the test browser.")
        if backend_url and launch_browser and worker:
            await worker.evaluate(
                "async (url) => chrome.storage.local.set({ medhuntBenchmarkABackendUrl: url })",
                backend_url,
            )

        panel = await context.new_page()
        panel.set_default_timeout(45_000)
        await panel.set_viewport_size({"width": 420, "height": 900})
        await panel.goto(f"chrome-extension://{extension_id}/index.html")
        panel.on("dialog", lambda dialog: asyncio.create_task(dialog.accept()))
        assert await panel.locator(".source-brand-copy strong").inner_text() == "Medhunt"
        assert await panel.locator(".medhunt-mark").count() == 1
        assert await panel.locator(".radixsol-mark").count() == 0
        await indeed_page.bring_to_front()
        await indeed_page.evaluate("""() => {
          history.replaceState({}, '', '/indeed_results.html?candidateId=shared-drawer-id');
          document.querySelectorAll("[data-cauto-id='candidate-name']").forEach((item) => {
            item.removeAttribute('href');
          });
        }""")
        await panel.evaluate("document.querySelector('[data-action=\"refresh-indeed\"]')?.click()")
        await panel.locator(".capture-toolbar strong").wait_for()
        assert "50 profiles ready" in await panel.locator("body").inner_text()
        await panel.locator('[data-action="toggle-all-indeed"]').click()
        await panel.locator(".indeed-select").first.check()
        await panel.locator(".indeed-select").nth(1).check()
        assert "Find contact details" in await panel.locator("body").inner_text()
        await panel.screenshot(path=str(screenshot), full_page=True)

        await panel.evaluate("document.querySelector('[data-action=\"lookup-indeed\"]')?.click()")
        await asyncio.wait_for(lookup_started.wait(), timeout=15)

        # While the private request is deliberately held, the single public
        # progressbar must be determinate and live inside the fixed top header.
        header = panel.locator('[data-testid="source-header"]')
        progress = panel.locator('[data-testid="source-progress"]')
        progressbar = panel.locator('[data-testid="source-progressbar"]')
        assert await header.get_attribute("aria-busy") == "true"
        assert await header.get_attribute("data-progress-kind") == "lookup"
        assert await progress.get_attribute("data-mode") == "determinate"
        assert await progress.get_attribute("aria-hidden") is None
        assert await progressbar.get_attribute("role") == "progressbar"
        assert await progressbar.get_attribute("aria-valuemin") == "0"
        assert await progressbar.get_attribute("aria-valuenow") == "0"
        assert await progressbar.get_attribute("aria-valuemax") == "2"
        assert "0 of 2" in (await progressbar.get_attribute("aria-valuetext") or "")
        assert await panel.locator('[role="progressbar"]').count() == 1
        assert await progressbar.evaluate(
            "element => element.closest('[data-testid=source-header]') !== null"
        )
        assert await panel.locator(".panel-rescan-button").is_disabled()

        lookup_release.set()
        await panel.wait_for_selector(".result-summary")
        result_text = await panel.locator("body").inner_text()
        summary_text = (await panel.locator(".result-summary").inner_text()).replace("\n", " ")
        assert "All 2" in summary_text, result_text
        assert "Ready 1" in summary_text, result_text
        assert "No contact 1" in summary_text, result_text
        assert "people data labs" not in result_text.casefold()
        assert "enformion" not in result_text.casefold()
        inactive_progress = panel.locator('[data-testid="source-progress"]')
        inactive_progressbar = panel.locator('[data-testid="source-progressbar"]')
        assert await inactive_progress.get_attribute("data-mode") == "inactive"
        assert await inactive_progress.get_attribute("aria-hidden") == "true"
        assert await inactive_progressbar.get_attribute("role") is None
        assert await panel.locator(".panel-rescan-button").is_enabled()
        await panel.locator('[data-filter="no_match"]').click()
        assert await panel.locator(".capture-row").count() == 1
        assert "No candidate cards detected" not in await panel.locator("body").inner_text()
        await panel.screenshot(path=str(result_screenshot), full_page=True)
        print({
            "extension_id": extension_id,
            "captured": 50,
            "looked_up": 2,
            "result_summary": summary_text,
            "provider_credits_spent": 0,
        })
        await panel.close()
        if launch_browser:
            await context.close()
        else:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
