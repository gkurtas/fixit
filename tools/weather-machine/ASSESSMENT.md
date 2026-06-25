# Rebuild Assessment — Philadelphia Weather Machine

## Source
- **Repo:** https://github.com/sstirling/storm-notify
- **Commit:** `d25b0214bde251ffe40435cdb71c21dd5afab929` (`d25b021`)
- **Built by:** Steve Stirling
- **Assessed:** 2026-06-23
- **Local copy:** `./_source` — git link stripped, read-only

## Inventory
A severe-weather situational-awareness tool for the Philadelphia / NWS Mount Holly (PHI)
region. Two halves:

1. **Entry points** — (a) AWS Lambda `handler(event, context)` (`pipeline/lambda_function.py:525`);
   (b) static web page `index.html` + IIFE (`app.js:10`); plus a dev-only CLI runner
   `pipeline/run_local.py:48`.
2. **Runtime shape** — both fires-and-stops: backend is event-driven (no serve loop),
   front-end is a static page polling on browser timers.
3. **Triggers** — EventBridge schedule `rate(3 minutes)` invokes the Lambda
   (`infra/template.yaml:120`); the page self-triggers on timers.
4. **External calls** — NWS api.weather.gov, IEM mesonet, RainViewer radar (all keyless
   public), Slack webhook, and AWS SDK to Bedrock/DynamoDB/S3/SSM (`lambda_function.py:61`).
5. **AI inference** — Claude Sonnet 4.6 via **Bedrock Converse** with forced-tool structured
   output + numeric fidelity guardrail (`lambda_function.py:147`). Local runner uses the
   first-party Anthropic SDK + API key (`core.py:100`, `run_local.py:59`).
6. **Secrets** — no committed secret. Runtime: Slack webhook in SSM SecureString
   `/pwm/slack-webhook` (`lambda_function.py:248`, `template.yaml:84`). Bedrock needs no key
   (role is the credential). Anthropic API key exists only in the local dev runner.
7. **Persistence/output** — Lambda writes `feed.json` to a private S3 bucket
   (`lambda_function.py:567`); DynamoDB holds rewrite cache + Slack/digest dedup; Slack posts.
8. **Audience** — **internal newsroom users, gated behind Okta** (operator-clarified). The
   current public CloudFront URL is misleading; the intended audience is internal.

## Classification
**Target: Pattern C** — a **Pattern A** Okta-gated EC2 dashboard reading `feed.json` from S3,
fed by a **Pattern B** EventBridge → Lambda(+Bedrock) backend.

Rationale: audience internal (not D) → has a visited UI (A) → also a triggered AI backend
(B) → both = C. The public CloudFront/S3 delivery is a Pattern D *mechanism* applied to what
should be a gated Pattern A surface — the central gap, not an asset.

## Findings
| ID | Finding (as-built) | Violates | Severity | Pattern target |
|----|--------------------|----------|----------|----------------|
| F1 | Front-end served Route 53 → CloudFront → private S3; open internet, no Okta gate (`template.yaml:147`) | Okta gate; internal-only audience | **High** | Re-platform to Pattern A (ALB+Okta+EC2) |
| F2 | No ALB SG / host SG / SG-to-SG lockdown — public CloudFront/OAC fronts the bucket | SG-to-SG lockdown (Okta-bypass keystone) | **High** | Host SG opens app port only from ALB SG by ID |
| F3 | No UI compute host — static files only; no EC2 service, systemd unit, dedicated port, or `/` health check | Pattern A service shape; reboot-survival | Medium | systemd service on dedicated port; health check on `/` |
| F4 | DNS alias → CloudFront distribution (`template.yaml:184`) | Pattern A delivery | Medium | DNS alias → ALB |
| F5 | Personal-account fingerprints: profile `pwm` (`deploy.sh:7`), `stephenstirling@gmail.com` (`deploy.sh:10`, `.env.example:9`), `pwm-*` naming / stack `pwm-weather` | Org ownership (creds, billing, access) | Medium | Org account/profile, contact, naming |
| F6 | Local dev runner uses personal `ANTHROPIC_API_KEY` (`run_local.py:59`, `core.py:100`) | Roles over keys | Low | Dev-only; prod already Bedrock. No key travels to fixit |
| F7 | Slack webhook in SSM SecureString, not Secrets Manager (`lambda_function.py:248`, `template.yaml:84`) | Secrets Manager (SYS-1623) named standard | Low | Confirm SSM accepted or migrate; already encrypted/runtime-read |
| F8 | `kms:Decrypt` on `Resource:"*"` (condition-scoped to ssm ViaService) (`template.yaml:88`) | Least-privilege tightness | Low | Acceptable; tighten to key ARN if org requires |

**High: 2 · Medium: 3 · Low: 3**

Already conforming (no finding): EventBridge→Lambda for bursty work; Bedrock role auth (no
stored model key); least-privilege Lambda IAM; idempotent DynamoDB dedup; no login code in
app; clean explicit S3 `feed.json` contract; no committed secret.

## Rebuild plan (request-path order)
1. **Boundary [BUILDER]** — Okta OIDC app + newsroom access group; ALB HTTPS:443 with
   authenticate-oidc → forward listener rule (host-header condition); host SG accepts app port
   only from ALB SG by ID; target group (instance:port) health check on `/`; DNS alias → ALB;
   retire the public CloudFront delivery.
2. **App → Pattern C [CODE]** — front-end static files served by a small static-file service
   under systemd on a dedicated port (e.g. 8080), health on `/`, no login code. EC2 pulls
   `feed.json` from S3 on a systemd timer and serves it same-origin (page's `fetch('feed.json')`
   + live-API fallback unchanged). Backend kept as idempotent `handler(event)`, re-pointed at
   org resources.
3. **Secrets [CODE]** — Bedrock role stays prod credential; Slack webhook runtime-read from
   SSM/Secrets Manager; front-end EC2 role scoped to `s3:GetObject` on `feed.json` only; local
   runner key stays dev-only `.env` (gitignored), never in fixit.
4. **Platform expectations [CODE]** — systemd unit + feed sync timer (reboot-survival); health
   endpoint; backend idempotency already present; C contract already explicit.

## Builder spec (items Claude Code can't provision)
- **Okta:** app + access group `<newsroom-weather-machine>`; redirect URI `https://<host>/oauth2/idpresponse`
- **Host SG:** open port `8080` from ALB SG `<sg-id>` only
- **Target group:** `instance:8080`, health check `/`
- **Listener rule (HTTPS:443):** host-header `<host>` → authenticate (OIDC) → forward
- **DNS:** alias `<host>` → ALB
- **Org cleanup:** replace `pwm` profile/account, `stephenstirling@gmail.com`, `pwm-*` naming

## Decisions (operator-confirmed 2026-06-23)
1. **F7 — Secrets Manager.** Migrate the Slack webhook from SSM SecureString to Secrets
   Manager (SYS-1623); runtime-read, never in source. Lambda IAM updated to
   `secretsmanager:GetValue` on that secret ARN.
2. **Static server — Python stdlib `ThreadingHTTPServer`.** A small (~30-line) zero-dependency
   service: serves the static dir read-only, `/` returns `index.html` (also the health-check
   target), directory listing off, bound to the dedicated port, privileges dropped via systemd.
   Production caveats of `http.server` are neutralized by the ALB+Okta+SG boundary (not
   internet-exposed) and trivial internal traffic. Upgrade path to Caddy/nginx noted in code.
3. **F6 — keep the Anthropic-SDK local dev path.** Two inference paths retained (local = SDK,
   prod = Bedrock). Key stays in gitignored `.env`; only `.env.example` placeholder in fixit.

## Open questions / divergences (remaining)
- Backend `feed.json` S3 bucket stays private, now read by EC2 instead of CloudFront; OAC/CloudFront removed.
