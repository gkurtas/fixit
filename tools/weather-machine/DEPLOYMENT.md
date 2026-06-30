# Deployment — Philadelphia Weather Machine (A2-static)

This is the deploy runbook for the path the Weather Machine actually uses: **A2-static**.

- **Front-end** serves as **built static files** dropped into the shared landing host's
  serving directory under a path prefix — `tools.inquirer.com/weather/` — reached through the
  host's **existing** Okta gate. **No ALB / Okta / DNS / target-group / port changes.**
- **Backend** is a **backend-only** Lambda stack (Pattern B) that writes `feed.json` to a
  private S3 bucket; the host syncs that file beside `index.html` on a timer.

See the classification in [ASSESSMENT.md](ASSESSMENT.md) and the variant definitions in
[REBUILD-PLAYBOOK.md](../../REBUILD-PLAYBOOK.md) (Pattern A → **A2-static**).

Steps are tagged **[CODE]** (run from this repo) or **[BUILDER]** (operator action on AWS or
on the shared host, via SSM).

---

## Host-specific values — from the host profile (do not guess)

The shared host's execution facts are **owned by the host profile**, not by this runbook. Read
`INQ-System/inquirer-it-tools/hostprofiles/host-profile-tools-inquirer-com.md`
(https://github.com/INQ-System/inquirer-it-tools, read-only) and confirm it
before deploying. If the profile is **missing or its `verified_on` is stale, STOP** and raise
it with the operator (per the playbook). Pull these values from it:

| Placeholder | Meaning (from the host profile) |
|-------------|--------------------------------|
| `<AWS_BIN>` | The `aws` binary path **runnable as the landing host's service account** (a service account whose home is outside `/home` can hit a broken snap CLI — the profile records the runnable path). |
| `<SVC_USER>` | The service account that owns the docroot and runs `tools-landing.service`. |
| `<DOCROOT_BASE>` | The landing host's served directory (the tool lands at `<DOCROOT_BASE>/weather`). Expected: `/opt/tools/landing`. |
| `<SSM_WRITE>` | The profile's documented pattern for writing files on the host via SSM (heredocs mangle through `AWS-RunShellScript`; **base64-decode on the host** is the reliable pattern). |

Confirm from the profile that the host **Supports variant: A2-static**, its **listener is
catch-all** (so `/weather/` resolves with no new rule), and that a service-account-runnable
`aws` path is recorded. The third is the **assessment-time gate** — without it, do not run the
front-end delivery steps below.

---

## Prerequisites

- AWS access to the org account — CloudShell (ambient credentials) or `AWS_PROFILE=<org>`.
  `deploy.sh` passes `--profile` only when `AWS_PROFILE` is set, so CloudShell needs nothing.
- Region **us-east-1** (Bedrock model + stack default).
- One-time: **Bedrock model access** enabled for Claude Sonnet 4.6 in us-east-1 (console →
  Model access → submit the Anthropic use-case form). Until then, rewrites fall back to
  official text and the page still works.
- SSM Session Manager / Run Command access to the shared landing host.

---

## Part 1 — Backend (Pattern B), backend-only  [CODE]

Deploys only the backend: `SiteBucket`, `CacheTable`, `RewriteFunction` (+ role/permission),
the 3-minute schedule, and the `SlackSecret`. **No EC2 front-end host** — that's the default
(`DeployWebHost=false`).

```bash
cd tools/weather-machine
# CloudShell: ambient creds. Otherwise: export AWS_PROFILE=<org-profile>
bash infra/deploy.sh
```

Note the outputs — you need **`SiteBucket`** and **`SlackSecretArn`** below:

```bash
aws cloudformation describe-stacks --stack-name weather-machine \
  --query "Stacks[0].Outputs" --output table
```

Seed `feed.json` immediately (or wait for the schedule):

```bash
aws lambda invoke --function-name <FunctionName> /tmp/out.json && cat /tmp/out.json
aws s3 ls "s3://<SiteBucket>/feed.json"        # confirm it landed
```

Store the real Slack webhook (optional; until set, Slack is simply skipped):  **[BUILDER]**

```bash
aws secretsmanager put-secret-value --secret-id <SlackSecretArn> \
  --secret-string "https://hooks.slack.com/services/XXX/YYY/ZZZ"
```

---

## Part 2 — Front-end (A2-static) onto the shared landing host

The served set is the **static bundle only**. `site/serve.py` is the dev / A2-port server and
**must not** be published into the docroot (playbook: no server-side files in the served set).
`feed-sync.sh` and the systemd units are **control files** and live **outside** the docroot.

### 2a. Stage the static bundle to S3  [CODE]

Reuse the private `SiteBucket` (the host already reads `feed.json` from it) under a `site/`
prefix, excluding the dev server:

```bash
cd tools/weather-machine
aws s3 sync site/ "s3://<SiteBucket>/site/" --exclude "serve.py" --delete
```

(`feed.json` lives at the bucket **root**, not under `site/`, so this never touches it.)

### 2b. Create and own the per-tool docroot  [BUILDER, on host via SSM]

```bash
sudo install -d -o <SVC_USER> -g <SVC_USER> <DOCROOT_BASE>/weather
```

### 2c. Pull the bundle into the docroot  [BUILDER, on host via SSM]

```bash
<AWS_BIN> s3 sync "s3://<SiteBucket>/site/" <DOCROOT_BASE>/weather/ --delete --exclude "feed.json"
```

> **`--delete` gotcha (this bit us before):** a `--delete` sync wipes any file in the target
> not present in the source. `feed.json` is written into the docroot by the timer (2d), **not**
> by this bundle — so it **must** be `--exclude`d here, or each sync would delete the feed.
> This is also why the control scripts live outside the docroot: they'd be wiped too.

### 2d. Install the feed-sync control files OUTSIDE the docroot  [BUILDER, on host via SSM]

Place the script and runtime env where the web server never serves them and the bundle sync
never deletes them — e.g. `<DOCROOT_BASE>/weather-control/`. Use the profile's `<SSM_WRITE>`
base64 pattern to write files (heredocs mangle through `AWS-RunShellScript`):

```bash
sudo install -d <DOCROOT_BASE>/weather-control
# feed-sync.sh: base64-encode locally, decode on the host (per <SSM_WRITE>), e.g.
#   base64 -d <<<'<b64 of infra/feed-sync.sh>' | sudo tee <DOCROOT_BASE>/weather-control/feed-sync.sh
sudo chmod +x <DOCROOT_BASE>/weather-control/feed-sync.sh

# Runtime env read by the units:
sudo install -d /etc/tools-landing
printf 'SITE_BUCKET=%s\nAWS_REGION=us-east-1\nWM_DOCROOT=%s/weather\nFEED_KEY=feed.json\n' \
  "<SiteBucket>" "<DOCROOT_BASE>" | sudo tee /etc/tools-landing/weather.env
```

Install the units and enable the timer (decode them from `infra/systemd/` the same way):

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now weather-feed-sync.timer
sudo systemctl start weather-feed-sync.service     # first sync now
```

> **Divergence to fix in the repo (surfaced, not absorbed):**
> `infra/systemd/weather-feed-sync.service` currently sets
> `ExecStart=/opt/tools/landing/weather/feed-sync.sh` — **inside** the docroot, which
> contradicts the "control files outside the docroot" rule (it would be web-served and
> `--delete`-wiped). Before installing, point `ExecStart` at the control path, e.g.
> `<DOCROOT_BASE>/weather-control/feed-sync.sh`. It's a one-line unit fix, flagged here, not
> in this runbook's scope.

### 2e. Grant the host read access to the feed bucket  [BUILDER]

The shared host's instance role (owned with the host in `inquirer-it-tools`) needs
`s3:GetObject` on the bundle prefix and the feed:

```
s3:GetObject on  arn:aws:s3:::<SiteBucket>/site/*
s3:GetObject on  arn:aws:s3:::<SiteBucket>/feed.json
```

---

## Part 3 — Verify

```bash
# Through the gate, in a browser (Okta login expected):
#   https://tools.inquirer.com/weather/           -> the page renders
#   https://tools.inquirer.com/weather/healthz     -> "ok"
#   https://tools.inquirer.com/weather/feed.json   -> present and fresh

# On the host (via SSM):
systemctl status weather-feed-sync.timer --no-pager
journalctl -u weather-feed-sync.service -n 20 --no-pager
ls -l <DOCROOT_BASE>/weather/feed.json
```

If `feed.json` is missing the page still works via its live-API fallback (no AI summaries) —
that points at 2c/2d/2e (bundle sync, the timer, or the IAM grant), not the page itself.

---

## Updating later

- **Front-end change:** re-run **2a** (stage) then **2c** (pull). The `--exclude feed.json`
  rule still applies.
- **Backend / Lambda change:** re-run `bash infra/deploy.sh` (Part 1).
- **`feed.json`:** nothing to do — the Lambda writes it and the timer syncs it.

## Teardown

```bash
# Backend: empty the bucket, then delete the stack.
aws cloudformation delete-stack --stack-name weather-machine
# Front-end: remove <DOCROOT_BASE>/weather and the weather-control files + units on the host.
```

---

## A1 alternative (dedicated host) — not used here

If a future tool needs isolation (its own hostname, Okta app, target group, IAM role), the
repo still supports the **A1** path: `DEPLOY_WEB_HOST=true VPC_ID=… SUBNET_ID=… ALB_SG_ID=…
bash infra/deploy.sh` provisions the dedicated EC2 front-end host, and the stack emits the full
builder spec (target group, listener rule, host-SG ingress, DNS). See the **A1** target in
[REBUILD-PLAYBOOK.md](../../REBUILD-PLAYBOOK.md). The Weather Machine does not use it.

*The Inquirer · IT / Systems · Confidential — internal*
