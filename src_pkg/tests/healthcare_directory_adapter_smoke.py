"""Offline browser smoke test for Medhunt's public healthcare-directory adapters."""
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
          window.chrome = {
            runtime: {
              lastError: null,
              onMessage: {
                addListener: listener => { window.__radixsolMessageListener = listener; }
              },
              sendMessage: (_message, callback) => { if (callback) callback({ ok: true }); }
            }
          };
        }"""
    )


def _message(page, message):
    return page.evaluate(
        """message => new Promise((resolve, reject) => {
          const listener = window.__radixsolMessageListener;
          if (!listener) return reject(new Error('Content-script listener was not installed.'));
          const asyncResponse = listener(message, {}, resolve);
          if (asyncResponse !== true && asyncResponse !== false) {
            setTimeout(() => reject(new Error('No response.')), 1000);
          }
        })""",
        message,
    )


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(BROWSER))
        context = browser.new_context()
        context.route("**/*", lambda route: route.fulfill(body="<html><body></body></html>"))

        syntax = context.new_page()
        source = (FRONTEND / "healthcare-directory-content.js").read_text(encoding="utf-8")
        assert syntax.evaluate("source => { new Function(source); return true; }", source)
        syntax.close()

        npino = context.new_page()
        npino.goto("https://npino.com/lookup/208800000x-urology/pa/")
        npino.set_content(
            """<main>
              <h1>Urology Doctors in Pennsylvania</h1>
              <div class="provider-card"><div><h3><a href="/npi/1093058315-patrick-theodore-gomella/">Patrick Theodore Gomella, MD, MPH</a></h3>
                <p>Urology</p><p>NPI Number: <a href="/npi/1093058315-patrick-theodore-gomella/">1093058315</a></p>
                <p>Address: 1245 Highland Ave Ste 302, Abington, PA, 19001-3724</p><p>Phone: 215-555-0100</p></div></div>
              <div class="provider-card"><div><h3><a href="/npi/1609865971-urology-specialists/">Urology Specialists Of The Lehigh Valley Pc</a></h3>
                <p>Urology</p><p>NPI Number: 1609865971</p><p>Address: Allentown, PA, 18106</p></div></div>
            </main>"""
        )
        _install_runtime(npino)
        npino.add_script_tag(path=str(FRONTEND / "healthcare-directory-content.js"))
        npino_result = _message(npino, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"})
        assert npino_result["platform"] == "npino", npino_result
        assert npino_result["adapter_revision"] == "healthcare-directory-v3"
        assert npino_result["count"] == 1, npino_result
        assert npino_result["profiles"][0]["name"] == "Patrick Theodore Gomella"
        assert npino_result["profiles"][0]["source_id"] == "1093058315"
        assert npino_result["profiles"][0]["location"] == "Abington, PA"
        assert npino_result["profiles"][0]["roles"] == ["Urology"]

        npi_profile = context.new_page()
        npi_profile.goto("https://npiprofile.com/taxonomy/code/207V00000X/state/ia")
        npi_profile.set_content(
            """<main><h1>207V00000X - Obstetrics &amp; Gynecology Providers in Iowa</h1>
              <table><tbody>
                <tr><td><a href="/npi/1023944683">1023944683</a></td><td>Madeline M. Shaw, MD</td>
                  <td>Individual</td><td>200 Hawkins Dr, Iowa City, IA 52242-1009</td></tr>
                <tr><td><a href="/npi/1999999999">1999999999</a></td><td>Example Women's Health LLC</td>
                  <td>Organization</td><td>Des Moines, IA 50309</td></tr>
              </tbody></table>
            </main>"""
        )
        _install_runtime(npi_profile)
        npi_profile.add_script_tag(path=str(FRONTEND / "healthcare-directory-content.js"))
        npi_profile_result = _message(npi_profile, {"type": "RADIXSOL_SCAN_PLATFORM_CANDIDATES"})
        assert npi_profile_result["platform"] == "npiprofile", npi_profile_result
        assert npi_profile_result["count"] == 1, npi_profile_result
        assert npi_profile_result["profiles"][0]["name"] == "Madeline M. Shaw"
        assert npi_profile_result["profiles"][0]["source_id"] == "1023944683"
        assert npi_profile_result["profiles"][0]["location"] == "Iowa City, IA"
        assert npi_profile_result["profiles"][0]["headline"] == "Obstetrics & Gynecology"

        nysed = context.new_page()
        nysed.goto("https://eservices.nysed.gov/professions/verification-search")
        nysed.set_content(
            """<main><table id="searchTable"><tbody>
              <tr><td><a class="information" href="#">012345</a></td><td>Alexandra Rivera, MD</td>
                <td>Medicine</td><td>Albany, NY 12234</td><td>01/02/2014</td></tr>
              <tr><td><a class="information" href="#">099999</a></td><td>Capital Medical Group LLC</td>
                <td>Medicine</td><td>Albany, NY 12234</td><td>03/04/2015</td></tr>
            </tbody></table></main>"""
        )
        _install_runtime(nysed)
        nysed.add_script_tag(path=str(FRONTEND / "healthcare-directory-content.js"))
        nysed_result = _message(nysed, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"})
        assert nysed_result["platform"] == "nysed", nysed_result
        assert nysed_result["count"] == 1, nysed_result
        assert nysed_result["profiles"][0]["name"] == "Alexandra Rivera"
        assert nysed_result["profiles"][0]["source_id"] == "medicine:012345"
        assert nysed_result["profiles"][0]["location"] == "Albany, NY"
        assert nysed_result["profiles"][0]["licenses"] == ["012345"]
        assert "Date of licensure: 01/02/2014" in nysed_result["profiles"][0]["notes"]

        usnews = context.new_page()
        usnews.goto("https://health.usnews.com/doctors/obstetrician-gynecologists")
        usnews.set_content(
            """<main><ol>
              <li><div data-test-id="DetailCardDoctor">
                <script type="application/ld+json">{
                  "@context":"https://schema.org", "@type":"LocalBusiness",
                  "name":"Dr. Ryan E. Longman MD", "telephone":"1-240-981-4709"
                }</script>
                <script type="application/ld+json">{
                  "@context":"http://schema.org", "@type":"Physician",
                  "name":"Ryan E. Longman",
                  "description":"Dr. Ryan Longman is an obstetrician-gynecologist in New York.",
                  "url":"https://health.usnews.com/doctors/ryan-longman-601896",
                  "address":{"@type":"PostalAddress","addressLocality":"New York","addressRegion":"NY","postalCode":"10038","streetAddress":"156 William St"},
                  "medicalSpecialty":{"@type":"MedicalSpecialty","name":"Obstetrics & Gynecology"},
                  "telephone":"(646) 962-2620",
                  "hospitalAffiliation":{"@type":"Hospital","name":"NewYork-Presbyterian Hospital-Columbia and Cornell"}
                }</script>
                <a href="https://health.usnews.com/doctors/ryan-longman-601896"><h3>Dr. Ryan E. Longman MD</h3></a>
                <button data-tracking-npi="1689794356">Book Appointment</button>
              </div></li>
              <li><div data-test-id="DetailCardDoctor">
                <script type="application/ld+json">{
                  "@context":"http://schema.org", "@type":"Physician",
                  "name":"Julie A. Abbott",
                  "url":"https://health.usnews.com/nurse-practitioners/julie-abbott-1953734",
                  "address":{"@type":"PostalAddress","addressLocality":"Coos Bay","addressRegion":"OR","postalCode":"97420","streetAddress":"1750 Thompson Rd"},
                  "medicalSpecialty":{"@type":"MedicalSpecialty","name":"Women's Health Nurse Practitioner"},
                  "hospitalAffiliation":{"@type":"Hospital","name":"Bay Area Hospital"}
                }</script>
                <a href="/nurse-practitioners/julie-abbott-1953734">Julie Abbott NP</a>
                <span data-tracking-npi="1609881028"></span>
              </div></li>
            </ol></main>"""
        )
        _install_runtime(usnews)
        usnews.add_script_tag(path=str(FRONTEND / "healthcare-directory-content.js"))
        usnews_result = _message(usnews, {"type": "RADIXSOL_SCAN_PLATFORM_CANDIDATES"})
        assert usnews_result["platform"] == "usnews", usnews_result
        assert usnews_result["platform_label"] == "U.S. News Doctor Finder"
        assert usnews_result["count"] == 2, usnews_result
        physician, nurse_practitioner = usnews_result["profiles"]
        assert physician["name"] == "Ryan E. Longman"
        assert physician["source_id"] == "1689794356"
        assert physician["location"] == "New York, NY"
        assert physician["headline"] == "Obstetrics & Gynecology"
        assert physician["roles"] == ["Physician"]
        assert physician["employers"] == ["NewYork-Presbyterian Hospital-Columbia and Cornell"]
        assert "Specialty: Obstetrics & Gynecology" in physician["notes"]
        assert "Profession: Physician" in physician["notes"]
        assert "NPI: 1689794356" in physician["notes"]
        assert "Employer: NewYork-Presbyterian Hospital-Columbia and Cornell" in physician["notes"]
        assert "1-240-981-4709" not in physician["notes"]
        assert "(646) 962-2620" not in physician["notes"]
        opened = _message(usnews, {"type": "RADIXSOL_OPEN_PLATFORM_CANDIDATE", "index": 0})
        assert opened["ok"] is True, opened
        assert opened["source_url"] == "https://health.usnews.com/doctors/ryan-longman-601896", opened
        assert nurse_practitioner["source_id"] == "1609881028"
        assert nurse_practitioner["roles"] == ["Nurse Practitioner"]

        usnews_profile = context.new_page()
        usnews_profile.goto("https://health.usnews.com/doctors/ryan-longman-601896")
        usnews_profile.set_content(
            """<main><div class="hero" data-test-id="HeroGraphic_with headshot">
                <h1>Dr. Ryan E. Longman MD</h1>
                <p data-tracking-placement="profile_header" data-tracking-campaign="specialty">Obstetrics &amp; Gynecology</p>
                <a href="#hospitals"><strong>NewYork-Presbyterian Hospital</strong></a>
                <span>18+ Years of Experience</span>
                <button doctor_npi="1689794356">Save</button>
              </div>
              <section id="overview"><h2>Overview</h2>
                <div>Dr. Ryan Longman is an obstetrician-gynecologist in New York.</div>
                <div>Speaks <strong>English</strong> Works at <strong>NewYork-Presbyterian Hospital</strong></div>
                <dl><dt>Specialty</dt><dd>Obstetrics &amp; Gynecology</dd></dl>
                <dl><dt>Subspecialties</dt><dd>Maternal &amp; Fetal Medicine</dd></dl>
              </section>
              <section id="hospitals"><h2>Hospitals &amp; ASCs</h2>
                <a href="/best-hospitals/area/ny/new-york-presbyterian-123"><h3>NewYork-Presbyterian Hospital</h3></a>
              </section>
              <section id="experience"><h2>Education &amp; Training</h2>
                <h3>Medical School &amp; Residency</h3><dl>
                  <dt>New York Presbyterian Hospital</dt><dd>Residency, Obstetrics and Gynecology, 2003-2007</dd>
                  <dt>Columbia University Vagelos College of Physicians and Surgeons</dt><dd>Medical School</dd>
                </dl>
                <h3>Certifications &amp; Licensure</h3><dl>
                  <dt>American Board of Obstetrics and Gynecology (ABMS)</dt><dd>Certified in Obstetrics &amp; Gynecology</dd>
                  <dt>NY State Medical License</dt><dd>Active through 2028</dd>
                </dl>
                <dl><dt>Provider NPI</dt><dd>1689794356</dd></dl>
              </section>
            </main>"""
        )
        _install_runtime(usnews_profile)
        usnews_profile.add_script_tag(path=str(FRONTEND / "healthcare-directory-content.js"))
        usnews_profile_result = _message(
            usnews_profile, {"type": "RADIXSOL_CAPTURE_PLATFORM_PROFILE"}
        )
        assert usnews_profile_result["ok"] is True, usnews_profile_result
        assert usnews_profile_result["profile"]["source_id"] == "1689794356"
        profile = usnews_profile_result["profile"]
        assert profile["schools"] == [
            "New York Presbyterian Hospital — Residency, Obstetrics and Gynecology, 2003-2007",
            "Columbia University Vagelos College of Physicians and Surgeons — Medical School",
        ]
        assert profile["licenses"] == ["NY State Medical License — Active through 2028"]
        assert profile["certifications"] == [
            "American Board of Obstetrics and Gynecology (ABMS) — Certified in Obstetrics & Gynecology"
        ]
        document_profile = profile["profile_document"]
        assert document_profile["kind"] == "public_professional_profile"
        assert document_profile["credentials"] == ["MD"]
        assert document_profile["subspecialties"] == ["Maternal & Fetal Medicine"]
        assert document_profile["hospitals"] == ["NewYork-Presbyterian Hospital"]
        assert document_profile["languages"] == ["English"]
        assert document_profile["years_experience"] == "18+"
        assert document_profile["npi"] == "1689794356"

        browser.close()
        print({
            "npino": npino_result["count"],
            "npiprofile": npi_profile_result["count"],
            "nysed": nysed_result["count"],
            "usnews": usnews_result["count"],
            "usnews_profile": 1,
        })


if __name__ == "__main__":
    main()
