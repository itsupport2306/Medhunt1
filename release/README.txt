MEDHUNT 3.22.2 - WINDOWS TEAM PACKAGE
=====================================

Share this file with each Windows teammate:
  Medhunt-Setup.exe

They run Setup and then follow the browser guide that opens automatically.
The first launch can take several minutes while the single-file package
extracts its bundled backend and OCR engine; wait for Setup to finish.
Chrome/Edge requires one manual Load unpacked step. Python is not required,
and no separate backend needs to be started: Setup installs the local service,
starts it, and registers it to start with Windows.

Application configuration and contact access are managed by the Radixsol
administrator. Do not send separate configuration files to team members.

Version: 3.22.2
Size: 178,126,728 bytes
SHA-256: f2456c939cd27baa79ad4595c3a6847b4fec921336c5940e8485c399d07023e3

Changes in 3.22.2:
  - Nexus delivery now selects clinical roles from structured platform and
    work-history context instead of mistaking duplicated employer labels for
    job titles;
  - professions and safe fallback specialties resolve from the live Nexus
    tenant catalog, with the tenant's Unknown classification used instead of
    guessing when a role cannot be mapped safely;
  - Nexus receives its required canonical mobile field and US phone format;
  - Setup resets PyInstaller state before launching the backend so installation
    exits cleanly without holding the installer file open; and
  - live end-to-end verification created Candace Robertson with her enriched
    resume and confirmed exact email and phone searches resolve to the same
    Nexus candidate record.

Changes in 3.22.1:
  - every trusted-team installation uses the bundled central Neon candidate
    database and private Cloudflare R2 resume bucket;
  - Setup removes only stale local database/storage overrides left by older
    SQLite-only installations while preserving the per-machine browser token
    and unrelated administrator settings;
  - new candidate/profile writes therefore go to Neon and new resume PDFs go
    to R2 after installation or upgrade; and
  - live commit/read/cleanup verification passed for Neon, and live
    put/head/delete verification passed for Cloudflare R2.

Changes in 3.22.0:
  - scanned and image-only PDF pages are detected automatically and read by a
    local OCR engine bundled with Setup; teammates do not install Tesseract or
    provide another API key;
  - name, city/state/country, role, contact evidence, skills, credentials,
    licenses, work history, education, specialty, work authorization,
    availability, and summary fields are saved as a bounded private structure;
  - the full OCR transcript is not stored;
  - recruiting-platform identity and location remain authoritative; OCR cannot
    overwrite conflicts and resume contact text cannot bypass the existing
    verified-contact gate;
  - only high-confidence, pre-approved missing fields may fill a Nexus profile;
  - OCR has page and time budgets, so unreadable scans do not block storage of
    the original resume; and
  - the installer contains the PDFium renderer, Tesseract runtime, native DLLs,
    and English OCR language model required for portable operation.

Changes in 3.21.0:
  - the browser extension and health response use only generic capability and
    public-record route names; private providers, endpoints, credentials,
    database/storage vendors, and ATS workflow details stay in the backend;
  - administrator-approved Quick Sourcer contacts are eligible for the same
    private resume/database/ATS workflow while DNC suppression remains active;
  - the dedicated Neon candidate database and private Cloudflare R2 resume
    bucket are enabled in the trusted-team backend package;
  - Nexus settings are resolved safely from the existing Nexus application at
    build time without executing that application's Python code;
  - individual and batch Quick Sourcer lookups now queue the newest existing
    resume automatically when delivery requirements become satisfied;
  - each enriched resume shows one latest trusted phone when captured,
    downloaded, or delivered to Nexus; the contact page is refreshed without
    stacking pages, and stale contacts are removed; mobile/wireless preference
    is retained while excluding expired, untrusted,
    disconnected, and do-not-contact values;
  - optional LaborEdge Nexus delivery uses a durable database queue, survives
    service restarts, and keeps every Nexus credential inside the backend;
  - only candidates with a current trusted email and phone are queued;
  - when contacts arrive after a resume was already stored, the newest stored
    resume is queued automatically;
  - Nexus duplicate checks compare trusted email and phone independently,
    reuse a unique compatible candidate, and hold conflicts or ambiguity for
    review instead of risking a wrong attachment;
  - transient and rate-limited deliveries retry with bounded backoff, while
    uncertain create/upload outcomes stop for reconciliation rather than being
    blindly repeated; and
  - Quick Sourcer lookup, public-record review, local caching, DNC suppression,
    private R2 storage, and the tested PDL/Enformion waterfall remain available.

Administrator note: live Neon and R2 write/read/cleanup checks pass. Nexus's
OAuth endpoint currently reports that the Radix API account is locked, so a
Nexus administrator must unlock it before queued Nexus deliveries can succeed.

An uncached external search opens the source site in a real browser, so it
takes 30 to 90 seconds per candidate and the searches cannot run in parallel.
Budget roughly a minute per selected profile the first time.

The active side panel follows supported tabs automatically, skips irrelevant
or unsafe profile records, keeps pending resume events across panel reloads,
and separates retryable failures from genuine no-contact results.

The current EXE is not code-signed. Windows SmartScreen may therefore show an
Unknown publisher warning. Verify the SHA-256 value before running it.
