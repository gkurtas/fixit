# The Philadelphia Weather Machine — fixit rebuild

A real-time severe-weather page for the Philadelphia region (NWS **Mount Holly /
PHI** office), rebuilt onto the platform's patterns as an **internal newsroom
tool behind the Okta gate**. It shows NWS watches/warnings/advisories plus
ground-truth local storm reports, rewrites the jargon into plain language with
Claude (via Amazon Bedrock), plots everything on an interactive map with radar,
and posts Slack alerts for matching warnings.

> This is the **fixit** reconstruction produced from the Rebuild Playbook, not
> the original. The source tool was assessed read-only; see
> [ASSESSMENT.md](ASSESSMENT.md) and the [assessment deck](assessment-deck.pptx)
> for the inventory, classification, findings, and plan this build implements.
> No secrets were carried forward from the source.

## Architecture — Pattern C (gated hybrid)

```
            ┌─────────────── Pattern A (front-end) ───────────────┐
Route 53 → ALB (HTTPS + Okta) → target group → EC2 service (:8080)
            │  authenticate-oidc → forward        systemd: serve.py │
            │  host SG accepts :8080 only from the ALB SG by id     │
            └───────────────────────────┬─────────────────────────┘
                                         │ reads feed.json (synced from S3, 1-min timer)
                                         ▼
            ┌─────────────── Pattern B (backend) ─────────────────┐
EventBridge rate(3 min) → Lambda (boto3 only)                     │
   fetch NWS/IEM → rewrite via Bedrock (Claude Sonnet 4.6)        │
   + numeric guardrail → DynamoDB cache/dedup → Slack             │
   → write feed.json to PRIVATE S3 ─────────────────────────────►┘
```

- **No public delivery.** The old CloudFront/public-S3 path is gone. The page is
  served by an EC2 service that is reachable **only through the ALB**, which
  enforces Okta. The host security group accepts the app port **only from the
  ALB security group by id** — the network keystone that makes the gate
  un-bypassable.
- **No key for inference.** Production authenticates to Claude through **Bedrock**
  via the Lambda's IAM role — nothing to store or rotate. The Anthropic API key
  exists only in the optional local dev runner.
- **One genuine secret**, the Slack webhook, lives in **Secrets Manager** and is
  read at runtime. It is never in source.
- **Clean A↔B contract:** the backend writes `feed.json` to a private S3 bucket;
  the front-end host syncs it locally and serves it same-origin. The page keeps
  its live-API fallback if the feed is missing or stale.

## Repository layout

```
site/                     front-end (served from the EC2 docroot)
  index.html, app.js, config.js, style.css, methodology.html, *.png, samples/
  serve.py                zero-dependency stdlib static server (systemd service)
pipeline/                 backend + local runner (shared, dependency-free logic)
  rewrite_common.py       prompt, guardrail, scope, Slack rules, feed assembly
  prompt.py               structured output shape for the local runner
  core.py, run_local.py   local runner (Anthropic SDK) → feed.json
  lambda_function.py      AWS Lambda: Bedrock rewrites, Slack (Secrets Manager), S3
  requirements.txt        local-only deps (anthropic, requests)
infra/
  template.yaml           CloudFormation: S3, DynamoDB, Lambda, schedule,
                          Secrets Manager, EC2 front-end host + scoped role + SG
  deploy.sh               one-command deploy (AWS CLI only)
  feed-sync.sh            S3 → docroot feed sync (run by the timer)
  systemd/                weather-machine-web.service, feed-sync.service/.timer
DEPLOYMENT.md             step-by-step deploy, including the builder-provisioned gate
```

## Local development

### Just the page

```bash
cd site
python3 -m http.server 8000          # http://localhost:8000 (live data via fallback)
# or:  python3 serve.py --port 8000  # the production server, run locally
# http://localhost:8000/?demo=1      # bundled sample fixtures
```

### Run the rewrite pipeline locally (optional, needs an Anthropic key)

Locally the runner uses the first-party Anthropic SDK; in AWS the same logic
runs through Bedrock with no key.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -U -r pipeline/requirements.txt
cp .env.example .env                 # paste your key into .env (gitignored)
python pipeline/run_local.py --source demo --limit 5   # cheap test
python pipeline/run_local.py                            # real PHI-region data
```

This writes `feed.json` into the repo root; serve `site/` and the page reads it.

## Deployment

See **[DEPLOYMENT.md](DEPLOYMENT.md)** — it covers the builder-provisioned gate
(Okta, ALB, target group, listener rule, SG-to-SG, DNS) and the one-command
stack deploy, in request-path order.

---

*The Inquirer · IT / Systems · Confidential — internal*
