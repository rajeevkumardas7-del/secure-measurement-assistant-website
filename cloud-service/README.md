# SECURE hosted web framework

Status: framework, not a production portal automation service.

## Boundaries

- `/app/` is the website entry point and responsive SECURE/E-MB shell.
- The Python service validates arithmetic expressions, computes valuation,
  verifies every protected request through existing Google licensing, and has
  persistent owner-scoped job storage.
- Browser lifecycle code creates a separate sandboxed browser process/context,
  allows only the configured portal origin, and deletes session state on close.
- The public job creation API intentionally returns 503. No fake jobs, credit
  deductions, portal saves or PDF extraction are reported as successful.
- This API Docker image does NOT include a browser worker.

## Verify

Run `python -m unittest -v` from this folder. Run `node --check app/app.js`
from the website repository root. Unit tests cover calculations, ownership and
HTTP access boundaries with a mocked licensing service. Browser isolation,
portal sign-in and save selectors still require runtime verification.

## Deploy the API

Build this folder's Dockerfile on a separate HTTPS service. Configure:

* `LICENSE_URL`: existing HTTPS licensing service URL (no credentials).
* `WEB_ORIGIN`: exact website origin, default https://securemeasurementassistant.info
* `DATA_DIR`: private persistent disk directory, default `/data` in Docker.
* `PORT`: service port, default 8080.

Set `app/config.json` apiBase and googleClientId after verifying the service.
The Google web client must authorize the website origin and be accepted by the
licensing service. Do not replace the desktop client ID and break existing
desktop sign-in. Add explicit supported-audience validation on the licensing
server before sharing access between desktop and web clients.

## Remaining production work

1. Deploy HTTPS API and persistent storage; verify Google web login with real
   owner, trial, expired, and subscribed accounts.
2. Run browser workers as non-root users with Chromium sandbox, resource/time
   limits and restricted egress. Exact portal origins only; no public CDP.
3. Provide authenticated session-bound interactive browser access for login,
   OTP and CAPTCHA. Never store portal passwords or expose sessions by job ID.
4. Port desktop PDF extraction and measurement review. Preserve expressions,
   blank dimensions, repeated item codes, and separate deduction rows.
5. Port two-pass item/measurement automation and read-back verification.
   Resume must compare saved portal rows to avoid duplicates after failure.
6. Add worker leases, cancellation, retention/purge, upload limits, TLS,
   rate limiting, audit events, and per-user concurrency limits.
7. Consume task credit only after all requested saves are verified server-side.
8. Test complete live SECURE and E-MB journeys before enabling job submission.

No hosting was purchased or provisioned by adding this framework. Do not
advertise full web parity until the above live checks pass.
