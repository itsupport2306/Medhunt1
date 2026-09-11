(() => {
  "use strict";

  const ADAPTER_REVISION = "healthcare-directory-v3";
  const ADAPTER_REQUEST = "RADIXSOL_HEALTHCARE_DIRECTORY_V3_REQUEST";
  if (window.__radixsolHealthcareDirectoryAdapterRevision === ADAPTER_REVISION) return;
  window.__radixsolHealthcareDirectoryAdapterRevision = ADAPTER_REVISION;

  const host = location.hostname.toLowerCase();
  const matches = (root) => host === root || host.endsWith(`.${root}`);
  const PLATFORM = matches("npino.com")
    ? { key: "npino", label: "NPI No." }
    : matches("npiprofile.com")
      ? { key: "npiprofile", label: "NPI Profile" }
      : host === "eservices.nysed.gov"
        ? { key: "nysed", label: "NYSED" }
        : host === "health.usnews.com"
          ? { key: "usnews", label: "U.S. News Doctor Finder" }
          : null;
  if (!PLATFORM) return;

  let lastProfiles = [];
  let lastElements = new Map();
  let scanInProgress = false;

  function all(selector, root = document) {
    try { return Array.from(root.querySelectorAll(selector)); } catch { return []; }
  }

  function clean(value, limit = 1000) {
    return String(value ?? "")
      .replace(/\u00a0/g, " ")
      .replace(/[\u0000-\u001f\u007f]+/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, limit);
  }

  function visibleText(element) {
    return clean(element?.innerText || element?.textContent || "", 4000);
  }

  function isVisible(element) {
    if (!element || !element.isConnected || element.hidden || element.getAttribute?.("aria-hidden") === "true") {
      return false;
    }
    const style = getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function unique(values) {
    return [...new Set((values || []).map((value) => clean(value)).filter(Boolean))];
  }

  function absoluteUrl(value) {
    try {
      const url = new URL(value || location.href, location.href);
      url.hash = "";
      return url.href;
    } catch {
      return location.href;
    }
  }

  function cleanProviderName(value) {
    return clean(value, 180)
      .replace(/^(?:dr\.?|doctor)\s+/i, "")
      .replace(/\s*\((?:individual|person)\)\s*$/i, "")
      .replace(/(?:,\s*|\s+)(?:M\.?D\.?|D\.?O\.?|M\.?P\.?H\.?|Ph\.?D\.?|DNP|APRN|NP|PA-C|RN|LPN|DDS|DMD|FACP|FACOG)(?:\s*,?\s*(?:M\.?D\.?|D\.?O\.?|M\.?P\.?H\.?|Ph\.?D\.?|DNP|APRN|NP|PA-C|RN|LPN|DDS|DMD|FACP|FACOG))*\.?$/i, "")
      .replace(/,+$/g, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  const NON_PERSON = /\b(?:associates?|center|clinic|company|corporation|department|group|health(?:care)?|hospital|institute|laborator(?:y|ies)|llc|medical practice|partners?|pharmacy|professional corporation|services?|specialists?\s+(?:of|pc\b)|university)\b/i;
  const NAVIGATION_NAME = /^(?:address|date of licensure|individual|license|licensee|name|npi|npi profile|profession|provider|search|status|view profile)$/i;

  function looksLikePersonName(value) {
    const name = cleanProviderName(value);
    if (!name || name.length < 3 || name.length > 120 || /\d|@|https?:/i.test(name)) return false;
    if (NAVIGATION_NAME.test(name) || NON_PERSON.test(name)) return false;
    const words = name.split(/\s+/).filter(Boolean);
    return words.length >= 2 && words.length <= 10 && words.every((word) => (
      /^[\p{L}\p{M}.'\u2019-]+$/u.test(word)
    ));
  }

  function locationFromAddress(value) {
    const address = clean(value, 500)
      .replace(/^address\s*:?\s*/i, "")
      .replace(/\b(?:phone|fax)\s*:.*$/i, "")
      .trim();
    const commaParts = address.split(",").map((part) => clean(part)).filter(Boolean);
    for (let index = commaParts.length - 1; index >= 1; index -= 1) {
      const state = commaParts[index].match(/^([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?$/i);
      if (state) return `${commaParts[index - 1]}, ${state[1].toUpperCase()}`;
    }
    const match = address.match(/\b([A-Za-z][A-Za-z.'\u2019-]*(?:\s+[A-Za-z][A-Za-z.'\u2019-]*){0,3})\s*,?\s+([A-Z]{2})\s+\d{5}(?:-\d{4})?\b/i);
    return match ? `${clean(match[1])}, ${match[2].toUpperCase()}` : "";
  }

  function npiFrom(value) {
    return clean(value, 2000).match(/\b(\d{10})\b/)?.[1] || "";
  }

  function smallestContainer(element, required) {
    let node = element;
    for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
      const text = visibleText(node);
      if (required.test(text) && text.length <= 3000) return node;
      if (node.matches?.("article, li, tr")) break;
    }
    return element;
  }

  function specialtyFromPage() {
    const taxonomy = location.pathname.match(/\/taxonomy\/code\/([A-Za-z0-9]+)/i)?.[1] || "";
    const headings = all("h1, h2").map(visibleText);
    const heading = headings.find((text) => taxonomy && text.toLowerCase().includes(taxonomy.toLowerCase())) || headings[0] || "";
    return clean(heading
      .replace(new RegExp(taxonomy, "ig"), "")
      .replace(/\b(?:providers?|doctors?|records?|in the u\.?s\.?|nationwide|iowa|pennsylvania)\b.*$/i, "")
      .replace(/^[\s:\-\u2013]+|[\s:\-\u2013]+$/g, ""), 180);
  }

  function profileNotes({ npi = "", license = "", profession = "", specialty = "", address = "", extra = "" }) {
    return unique([
      specialty && `Specialty: ${specialty}`,
      profession && `Profession: ${profession}`,
      npi && `NPI: ${npi}`,
      license && `License: ${license}`,
      address && `Address: ${address}`,
      extra,
    ]).join("\n").slice(0, 12000);
  }

  function jsonLdObjects(root = document) {
    const values = [];
    for (const script of all('script[type="application/ld+json"]', root)) {
      try {
        const pending = [JSON.parse(script.textContent || "null")];
        while (pending.length) {
          const value = pending.shift();
          if (Array.isArray(value)) {
            pending.push(...value);
            continue;
          }
          if (!value || typeof value !== "object") continue;
          values.push(value);
          if (Array.isArray(value["@graph"])) pending.push(...value["@graph"]);
          // U.S. News sometimes nests its Physician schema below a
          // MedicalWebPage instead of publishing it as a top-level object.
          if (value.mainEntity && typeof value.mainEntity === "object") pending.push(value.mainEntity);
        }
      } catch {
        // Ignore unrelated or temporarily incomplete structured-data blocks.
      }
    }
    return values;
  }

  function hasSchemaType(value, expected) {
    const types = Array.isArray(value?.["@type"]) ? value["@type"] : [value?.["@type"]];
    return types.some((type) => clean(type, 80).toLowerCase() === expected.toLowerCase());
  }

  function schemaName(value) {
    if (typeof value === "string") return clean(value, 180);
    return clean(value?.name, 180);
  }

  function postalAddress(value) {
    if (!value || typeof value !== "object") return "";
    const city = clean(value.addressLocality, 120);
    const state = clean(value.addressRegion, 80);
    const postalCode = clean(value.postalCode, 30);
    const cityRegion = [city, state].filter(Boolean).join(", ");
    return [clean(value.streetAddress, 240), [cityRegion, postalCode].filter(Boolean).join(" ")]
      .filter(Boolean)
      .join(", ");
  }

  function exactUsNewsProfilePath() {
    return /^\/(?:doctors|nurse-practitioners)\/[^/?#]+-\d+\/?$/i.test(location.pathname);
  }

  function heading(root, label) {
    const expected = clean(label, 120).toLowerCase();
    return all("h1, h2, h3, h4, dt", root).find((element) => (
      visibleText(element).toLowerCase() === expected
    )) || null;
  }

  function nextElementMatching(element, selector) {
    let node = element?.nextElementSibling || null;
    while (node) {
      if (node.matches?.(selector)) return node;
      if (node.matches?.("h1, h2, h3, h4")) return null;
      const nested = node.querySelector?.(selector);
      if (nested) return nested;
      node = node.nextElementSibling;
    }
    return null;
  }

  function definitionPairs(list) {
    if (!list) return [];
    const pairs = [];
    let term = "";
    for (const child of Array.from(list.children || [])) {
      if (child.matches?.("dt")) {
        term = visibleText(child);
      } else if (term && child.matches?.("dd")) {
        const detail = visibleText(child);
        if (detail) pairs.push({ term, detail });
      }
    }
    return pairs;
  }

  function pairsAfterHeading(root, label) {
    return definitionPairs(nextElementMatching(heading(root, label), "dl"));
  }

  function usNewsProfileDocument(physician = null) {
    if (!exactUsNewsProfilePath()) return null;
    const hero = document.querySelector('[data-test-id^="HeroGraphic"], .hero') || document;
    const rawName = visibleText(hero.querySelector("h1") || document.querySelector("main h1"));
    const name = cleanProviderName(physician?.name || rawName);
    if (!looksLikePersonName(name)) return null;

    const credentials = unique(
      (rawName.match(/\b(?:MD|DO|DNP|APRN|NP|PA-C|RN|DDS|DMD|MPH|PhD)\b/gi) || [])
        .map((value) => value.toUpperCase()),
    );
    const schemaSpecialty = schemaName(physician?.medicalSpecialty);
    const headerSpecialty = visibleText(document.querySelector(
      '[data-tracking-placement="profile_header"][data-tracking-campaign="specialty"]'
    ));
    const specialtyPairs = all("#overview dl").flatMap(definitionPairs);
    const specialties = unique([
      schemaSpecialty,
      headerSpecialty,
      ...specialtyPairs.filter(({ term }) => /^specialty$/i.test(term)).map(({ detail }) => detail),
    ]);
    const subspecialties = unique([
      ...specialtyPairs.filter(({ term }) => /^subspecialt/i.test(term)).map(({ detail }) => detail),
    ]);

    const medicalTraining = pairsAfterHeading(document.querySelector("#experience") || document, "Medical School & Residency");
    const credentialsAndLicenses = pairsAfterHeading(
      document.querySelector("#experience") || document,
      "Certifications & Licensure",
    );
    const education = unique(medicalTraining.map(({ term, detail }) => `${term} — ${detail}`));
    const licenses = unique(credentialsAndLicenses
      .filter(({ term, detail }) => /\bstate medical license\b/i.test(`${term} ${detail}`))
      .map(({ term, detail }) => `${term} — ${detail}`));
    const certifications = unique(credentialsAndLicenses
      .filter(({ term, detail }) => !/\bstate medical license\b/i.test(`${term} ${detail}`))
      .map(({ term, detail }) => `${term} — ${detail}`));

    const schemaAffiliations = Array.isArray(physician?.hospitalAffiliation)
      ? physician.hospitalAffiliation
      : [physician?.hospitalAffiliation];
    const hospitals = unique([
      ...schemaAffiliations.map(schemaName),
      ...all('#hospitals .hospital-name, #hospitals h3, #hospitals h4, #hospitals a[href*="/best-hospitals/"]')
        .map(visibleText),
      visibleText(document.querySelector('a[href="#hospitals"] strong')),
    ]).filter((value) => !/^hospitals?(?:\s*&\s*ascs?)?$/i.test(value));

    const overviewSection = document.querySelector("#overview");
    const overviewHeading = heading(overviewSection || document, "Overview");
    const overview = clean(visibleText(nextElementMatching(overviewHeading, "div, p")), 4000);
    const detailText = visibleText(overviewSection || document);
    const speaks = detailText.match(/\bSpeaks\s+([^\n]+?)(?=\s+Works at\b|$)/i)?.[1] || "";
    const languages = unique(speaks.split(/,|\band\b/i));
    const years = visibleText(hero).match(/\b(\d+\+?)\s+Years? of Experience\b/i)?.[1] || "";
    const schemaAddress = postalAddress(physician?.address);
    const address = schemaAddress || visibleText(document.querySelector('a[href="#location"]'));
    const locationValue = physician?.address && typeof physician.address === "object"
      ? [clean(physician.address.addressLocality, 120), clean(physician.address.addressRegion, 80)]
          .filter(Boolean).join(", ")
      : locationFromAddress(address);
    const npi = npiFrom(
      document.querySelector("[doctor_npi]")?.getAttribute("doctor_npi")
      || document.querySelector("[data-tracking-npi]")?.getAttribute("data-tracking-npi")
      || visibleText(document.querySelector("#experience")),
    );

    // Wait for the experience block to hydrate before creating a document.
    // This avoids saving a second, incomplete PDF while the React page loads.
    if (!education.length && !licenses.length && !certifications.length) return null;
    return {
      kind: "public_professional_profile",
      source_label: "U.S. News Doctor Finder",
      source_url: absoluteUrl(physician?.url || location.href),
      headline: [specialties[0], subspecialties[0]].filter(Boolean).join(" — "),
      credentials,
      summary: overview,
      specialties,
      subspecialties,
      hospitals,
      education,
      licenses,
      certifications,
      languages,
      years_experience: years,
      npi,
      address,
      location: locationValue,
    };
  }

  function usNewsProfiles() {
    const profiles = [];
    const elements = new Map();
    const seen = new Set();
    const cards = all('[data-test-id="DetailCardDoctor"]');
    const roots = cards.length ? cards : [document];

    for (const root of roots) {
      let physicians = jsonLdObjects(root).filter((value) => hasSchemaType(value, "Physician"));
      if (!physicians.length && root === document && exactUsNewsProfilePath()) {
        const heroName = visibleText(document.querySelector('[data-test-id^="HeroGraphic"] h1, .hero h1, main h1'));
        if (looksLikePersonName(cleanProviderName(heroName))) {
          physicians = [{ name: heroName, url: location.href }];
        }
      }
      for (const physician of physicians) {
        const name = cleanProviderName(physician.name);
        if (!looksLikePersonName(name)) continue;

        const sourceUrl = absoluteUrl(physician.url || root.querySelector?.(
          'a[href^="/doctors/"], a[href^="/nurse-practitioners/"]'
        )?.getAttribute("href") || location.href);
        let sourcePath = "";
        try { sourcePath = new URL(sourceUrl).pathname; } catch { /* absoluteUrl already supplied a safe fallback. */ }
        const profileDocument = root === document ? usNewsProfileDocument(physician) : null;
        const npi = npiFrom(
          root.querySelector?.("[doctor_npi]")?.getAttribute("doctor_npi")
          || root.querySelector?.("[data-tracking-npi]")?.getAttribute("data-tracking-npi")
          || profileDocument?.npi,
        );
        const sourceId = npi || sourcePath.replace(/^\/+|\/+$/g, "") || name.toLowerCase();
        if (seen.has(sourceId)) continue;

        const specialty = schemaName(physician.medicalSpecialty) || profileDocument?.specialties?.[0] || "";
        const address = postalAddress(physician.address) || profileDocument?.address || "";
        const locationValue = physician.address && typeof physician.address === "object"
          ? [clean(physician.address.addressLocality, 120), clean(physician.address.addressRegion, 80)]
              .filter(Boolean).join(", ")
          : (profileDocument?.location || locationFromAddress(address));
        const affiliations = Array.isArray(physician.hospitalAffiliation)
          ? physician.hospitalAffiliation
          : [physician.hospitalAffiliation];
        const employers = unique([
          ...affiliations.map(schemaName),
          ...(profileDocument?.hospitals || []),
        ]);
        const profession = /\/nurse-practitioners(?:\/|$)/i.test(sourcePath)
          ? "Nurse Practitioner"
          : "Physician";
        const extra = unique([
          ...employers.map((employer) => `Employer: ${employer}`),
          ...(profileDocument?.subspecialties || []).map((value) => `Subspecialty: ${value}`),
          ...(profileDocument?.education || []).map((value) => `Education: ${value}`),
          ...(profileDocument?.certifications || []).map((value) => `Certification: ${value}`),
          ...(profileDocument?.licenses || []).map((value) => `License: ${value}`),
          ...(profileDocument?.languages || []).map((value) => `Language: ${value}`),
          profileDocument?.years_experience && `Years of experience: ${profileDocument.years_experience}`,
          profileDocument?.summary,
          clean(physician.description, 1200),
        ]).join("\n");

        seen.add(sourceId);
        profiles.push({
          name,
          location: locationValue,
          headline: specialty || profession,
          notes: profileNotes({ npi, profession, specialty, address, extra }),
          roles: [profession],
          employers,
          schools: profileDocument?.education || [],
          licenses: profileDocument?.licenses || [],
          certifications: profileDocument?.certifications || [],
          profile_document: profileDocument || undefined,
          source: PLATFORM.key,
          source_url: sourceUrl,
          source_id: sourceId,
          captured_at: new Date().toISOString(),
        });
        elements.set(sourceId, root === document ? document.documentElement : root);
        if (profiles.length >= 100) return { profiles, elements };
      }
    }
    return { profiles, elements };
  }

  function npinoProfiles() {
    const profiles = [];
    const elements = new Map();
    const seen = new Set();
    for (const link of all('a[href*="/npi/"]')) {
      const href = absoluteUrl(link.getAttribute("href"));
      const npi = href.match(/\/npi\/(\d{10})(?:-|\/|$)/i)?.[1] || npiFrom(link.textContent);
      if (!npi || seen.has(npi)) continue;
      const card = smallestContainer(link, /\bNPI Number\s*:/i);
      const nameLink = card.querySelector?.('h2 a[href*="/npi/"], h3 a[href*="/npi/"]') || link;
      const name = cleanProviderName(nameLink.textContent);
      if (!looksLikePersonName(name)) continue;
      const text = visibleText(card);
      const address = clean(text.match(/\bAddress\s*:\s*(.*?)(?=\s+(?:Phone|Fax)\s*:|$)/i)?.[1] || "", 500);
      const specialty = clean(card.querySelector?.("h3 + p, h2 + p")?.textContent || specialtyFromPage(), 180);
      seen.add(npi);
      profiles.push({
        name,
        location: locationFromAddress(address),
        headline: specialty,
        notes: profileNotes({ npi, specialty, address }),
        roles: specialty ? [specialty] : [],
        employers: [],
        schools: [],
        licenses: [],
        certifications: [],
        source: PLATFORM.key,
        source_url: href,
        source_id: npi,
        captured_at: new Date().toISOString(),
      });
      elements.set(npi, card);
      if (profiles.length >= 100) break;
    }
    return { profiles, elements };
  }

  function candidateName(container, primaryLink, npi) {
    const candidates = [
      primaryLink?.textContent,
      ...all('[itemprop="name"], .provider-name, .name, h2, h3, h4, a[href]', container).map((element) => element.textContent),
      ...all("td", container).map((element) => element.textContent),
    ];
    return candidates
      .map((value) => cleanProviderName(String(value || "").replace(npi, "")))
      .find(looksLikePersonName) || "";
  }

  function npiProfileProfiles() {
    const profiles = [];
    const elements = new Map();
    const seen = new Set();
    const pageSpecialty = specialtyFromPage();
    for (const link of all('a[href*="/npi/"]')) {
      const href = absoluteUrl(link.getAttribute("href"));
      const npi = href.match(/\/npi\/(\d{10})(?:[/?#-]|$)/i)?.[1] || "";
      if (!npi || seen.has(npi)) continue;
      const card = link.closest("tr, article, li") || smallestContainer(link, new RegExp(`\\b${npi}\\b`));
      const cardText = visibleText(card);
      if (/\borganization\b/i.test(cardText) && !/\bindividual\b/i.test(cardText)) continue;
      const name = candidateName(card, link, npi);
      if (!name) continue;
      const location = locationFromAddress(cardText);
      seen.add(npi);
      profiles.push({
        name,
        location,
        headline: pageSpecialty,
        notes: profileNotes({ npi, specialty: pageSpecialty, extra: cardText.slice(0, 1200) }),
        roles: pageSpecialty ? [pageSpecialty] : [],
        employers: [],
        schools: [],
        licenses: [],
        certifications: [],
        source: PLATFORM.key,
        source_url: href,
        source_id: npi,
        captured_at: new Date().toISOString(),
      });
      elements.set(npi, card);
      if (profiles.length >= 100) break;
    }
    return { profiles, elements };
  }

  function nysedProfiles() {
    const profiles = [];
    const elements = new Map();
    const seen = new Set();
    const rows = all("#searchTable tbody tr").filter(isVisible);
    for (const row of rows) {
      const cells = all("td", row).map(visibleText);
      if (cells.length < 4) continue;
      const license = clean(cells[0], 120);
      const name = cleanProviderName(cells[1]);
      const profession = clean(cells[2], 180);
      const address = clean(cells[3], 500);
      const licensedOn = clean(cells[4], 100);
      if (!license || !looksLikePersonName(name)) continue;
      const sourceId = `${profession.toLowerCase()}:${license.toLowerCase()}`;
      if (seen.has(sourceId)) continue;
      seen.add(sourceId);
      profiles.push({
        name,
        location: locationFromAddress(address),
        headline: profession,
        notes: profileNotes({ license, profession, address, extra: licensedOn && `Date of licensure: ${licensedOn}` }),
        roles: profession ? [profession] : [],
        employers: [],
        schools: [],
        licenses: [license],
        certifications: [],
        source: PLATFORM.key,
        source_url: absoluteUrl(location.href),
        source_id: sourceId,
        captured_at: new Date().toISOString(),
      });
      elements.set(sourceId, row);
      if (profiles.length >= 100) break;
    }
    return { profiles, elements };
  }

  function scanSnapshot() {
    const result = PLATFORM.key === "npino"
      ? npinoProfiles()
      : PLATFORM.key === "npiprofile"
        ? npiProfileProfiles()
        : PLATFORM.key === "nysed"
          ? nysedProfiles()
          : usNewsProfiles();
    lastProfiles = result.profiles;
    lastElements = result.elements;
    return {
      ok: true,
      platform: PLATFORM.key,
      platform_label: PLATFORM.label,
      profiles: result.profiles,
      count: result.profiles.length,
      expected_count: result.profiles.length,
      raw: { rows: result.profiles.length },
      page_url: location.href,
    };
  }

  async function progressiveScan() {
    scanInProgress = true;
    try {
      const result = scanSnapshot();
      chrome.runtime.sendMessage({
        type: "RADIXSOL_PLATFORM_SCAN_PROGRESS",
        platform: PLATFORM.key,
        found: result.count,
        total: result.expected_count,
        page_url: location.href,
      }, () => void chrome.runtime.lastError);
      return result;
    } finally {
      scanInProgress = false;
    }
  }

  function openCandidate(index) {
    const profile = lastProfiles[Number(index)];
    if (!profile) return { ok: false, error: "That displayed provider is no longer available." };
    const element = lastElements.get(profile.source_id);
    const link = element?.matches?.("a[href]") ? element : element?.querySelector?.(
      'a[href*="/npi/"], a[href*="/doctors/"], a[href*="/nurse-practitioners/"], a.information'
    );
    if (link) {
      link.scrollIntoView({ block: "center", behavior: "auto" });
      link.click();
      return { ok: true, source_url: profile.source_url };
    }
    return { ok: false, error: `${PLATFORM.label} did not expose a provider-details link.` };
  }

  function captureProfile() {
    const result = scanSnapshot();
    if (result.profiles.length !== 1) {
      return { ok: false, error: `Open one ${PLATFORM.label} provider result, then try Capture again.` };
    }
    return { ok: true, platform: PLATFORM.key, profile: result.profiles[0], page_url: location.href };
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    const messageType = message?.type === ADAPTER_REQUEST ? message.original_type : message?.type;
    const respond = (payload) => sendResponse({ ...payload, adapter_revision: ADAPTER_REVISION });
    if (messageType === "RADIXSOL_PLATFORM_PING") {
      respond({ ok: true, platform: PLATFORM.key, label: PLATFORM.label, url: location.href });
      return false;
    }
    if (messageType === "RADIXSOL_CAPTURE_PLATFORM_PROFILE") {
      respond(captureProfile());
      return false;
    }
    if (messageType === "RADIXSOL_LIST_PLATFORM_CANDIDATES") {
      respond(scanSnapshot());
      return false;
    }
    if (messageType === "RADIXSOL_SCAN_PLATFORM_CANDIDATES") {
      progressiveScan().then(respond).catch((error) => respond({ ok: false, error: String(error?.message || error) }));
      return true;
    }
    if (messageType === "RADIXSOL_OPEN_PLATFORM_CANDIDATE") {
      respond(openCandidate(message.index));
      return false;
    }
    return false;
  });

  let mutationTimer = null;
  let lastSignature = "";
  function detectResultsChange() {
    if (scanInProgress) return;
    clearTimeout(mutationTimer);
    mutationTimer = setTimeout(() => {
      const snapshot = scanSnapshot();
      const signature = snapshot.profiles.map((profile) => (
        `${profile.source_id}:${profile.profile_document ? JSON.stringify(profile.profile_document) : ""}`
      )).join("|");
      if (signature === lastSignature) return;
      const hadResults = Boolean(lastSignature);
      lastSignature = signature;
      if (!signature && !hadResults) return;
      chrome.runtime.sendMessage({
        type: "RADIXSOL_PLATFORM_RESULTS_CHANGED",
        platform: PLATFORM.key,
        count: snapshot.count,
        page_url: location.href,
      }, () => void chrome.runtime.lastError);
    }, 800);
  }

  new MutationObserver(detectResultsChange).observe(document.documentElement, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["class", "hidden", "aria-hidden"],
  });
  addEventListener("popstate", detectResultsChange);
  detectResultsChange();
})();
