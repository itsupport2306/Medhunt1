# HealthBoard login and analytics integration

HealthBoard is the account authority for the public Medhunt extension. Auth0
is not used. A recruiter enters their HealthBoard email, receives a six-digit
code, and exchanges it for a limited extension token.

Configure the Medhunt backend with:

```text
HEALTHBOARD_BASE_URL=https://your-healthboard-domain.example
HEALTHBOARD_AUTH_TIMEOUT=8
HEALTHBOARD_AUTH_CACHE_SECONDS=60
```

For local development use `http://127.0.0.1:8000`. The extension talks to the
hosted Medhunt API, which calls HealthBoard server-to-server. Before publishing,
replace `DEFAULT_BACKEND` in `frontend/app.js` with the HTTPS Medhunt API URL
and add that exact origin to `frontend/manifest.json` host permissions. The
HealthBoard origin does not need a Chrome host permission.

HealthBoard endpoints used by Medhunt:

- `POST /api/extension/auth/request-code`
- `POST /api/extension/auth/verify-code`
- `GET /api/extension/auth/me`
- `POST /api/extension/activity/enrichment`

The code expires after ten minutes, is rate-limited and one-time-use, and is
sent only to an existing active HealthBoard recruiter/admin. Responses do not
reveal whether an email is registered. Successful Medhunt enrichments appear
in both personal sourcing analytics and organization member usage.

## Chrome Web Store package

After Render supplies the final HTTPS origin, build the upload ZIP from the
repository root:

```powershell
python .\packaging\build_chrome_store.py `
  --api-base https://your-medhunt-api.onrender.com `
  --output .\release\medhunt-chrome-store.zip
```

The builder removes localhost permissions, adds only that API origin, minifies
the JavaScript, excludes Watcher, renames legacy internal browser identifiers,
and fails if private provider names or credential-like strings occur in any
shipped text file. Browser code is not obfuscated: Chrome Web Store policy
prohibits concealing extension functionality, and any code executed on a user's
computer is ultimately inspectable. Lookup orchestration, credentials, and
analytics implementation remain in the backend and are not included in the ZIP.
