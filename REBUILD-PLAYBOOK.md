# Tool Rebuild Playbook — Assess, Gap, Rebuild

**The Inquirer · IT / Systems · v1.0**

A procedure for Claude Code: take an externally-built tool, evaluate it against the platform's build patterns, and reconstruct it into a conforming **fixit repo** — without ever modifying the source.

---

## What this is for

Tools arrive built outside the platform: a reporter's prototype, a personal-account
side project, an old internal app on someone's keys. Before such a tool can be hosted,
it has to be evaluated against the pattern framework and rebuilt to fit. This playbook is
the recipe for that evaluation and rebuild.

**The canonical case:** Stephen builds the Weather Machine in his own account. We pull
the source down, evaluate it, and rebuild it into a conforming tool we can host and demo —
without touching his repo or opening a PR against it.

---

## Operating rules (read before doing anything)

1. **The source repo is READ-ONLY. Never write to it, never open a PR against it, never
   push to it.** Clone it, read it, leave it untouched. Every change happens in the fixit
   repo, which is a separate repository.

2. **No secrets travel from source to fixit.** If the source contains an API key, token,
   `.env`, or credential of any kind, it is a *finding*, not an asset. Do not copy it
   forward. Record that it exists, note where, and design it out (Bedrock role, Secrets
   Manager, or instance role). The fixit repo must be safe to push to the org GitHub.

3. **Classify before you cut.** Don't refactor until the target pattern is named. The
   pattern dictates the fixes; fixing before classifying produces churn.

4. **Rebuild to the request-path boundary, not just the code.** A conforming tool is
   defined as much by its network/identity boundary (SG-to-SG lockdown, Okta gate, scoped
   role) as by its source. The rebuild plan must reach those, even though Claude Code
   can't provision them — it produces the spec the builder executes.

5. **Findings first, code on confirmation.** Produce the assessment report and the rebuild
   plan, then stop. Generate fixit code only after the operator confirms the plan. Do not
   silently absorb divergences — surface them.

---

## The five phases

```
1. INTAKE    clone source read-only, set up the fixit repo
2. INVENTORY what does this tool actually do?
3. CLASSIFY  which pattern(s) is it, per the framework?
4. GAP       as-built vs. pattern standard → findings table
5. REBUILD   ordered plan → (on confirmation) fixit code
```

---

## Phase 1 — Intake

**Goal:** source code on disk, read-only; an empty fixit repo ready to receive the rebuild.

```bash
# Clone the source to a read-only working location. Never push here.
git clone --depth 1 <SOURCE_REPO_URL> ./_source
chmod -R a-w ./_source        # belt-and-suspenders: make it hard to write by accident

# The fixit repo is a SEPARATE repository under the org.
# Create/clone it alongside — this is where all rebuilt code lands.
git clone <FIXIT_REPO_URL> ./fixit   # or: mkdir fixit && git init
```

Record in the report: source repo URL, commit SHA assessed, date, who built it.

> **Why a separate repo, not a fork:** a fork keeps a live link to the source and tempts a
> PR back. The fixit repo is a clean reconstruction the platform owns outright — org
> credentials, org billing, org access control. That ownership transfer is the entire
> point of bringing a tool onto the platform.

---

## Phase 2 — Inventory

**Goal:** an honest description of what the tool does, derived from the code — not from
the README's claims. Answer each, citing the file/line evidence.

| # | Question | Where to look |
|---|----------|---------------|
| 1 | **Entry points** — what starts it? | `main`, `app.py`, `index.js`, `handler`, a `Dockerfile` CMD, a cron line, a `serverless.yml` |
| 2 | **Runtime shape** — always-on server, or fires-and-stops? | A bound port + serve loop = always-on. A `handler(event)` signature = event-driven. |
| 3 | **Triggers** — what invokes it? | HTTP routes, schedule/cron, queue/topic subscription, file-drop watcher, webhook |
| 4 | **External calls** — what does it talk to? | HTTP clients, SDK calls, model APIs (`anthropic`, `openai`, `bedrock`), DB drivers |
| 5 | **AI inference** — does a model read/summarize/classify? | Model-client imports and call sites |
| 6 | **Secrets** — what credentials does it hold, and where? | `.env`, `os.environ`, hardcoded strings, config files, `*_KEY`, `*_TOKEN`, `*_SECRET` |
| 7 | **Persistence/output** — where does work land? | File writes, S3 puts, DB inserts, Slack/webhook posts, a served page |
| 8 | **Audience** — who is the consumer? | A logged-in human (UI), the public (open page/feed), or a machine (data/notifications) |

Output of this phase is an **Inventory block** in the report — the factual basis everything
downstream rests on.

---

## Phase 3 — Classify

**Goal:** name the target pattern(s) by running the inventory through the platform
classifier. Answer in order; the first strong signal usually decides it.

| # | Question | Result |
|---|----------|--------|
| 1 | Audience public / unauthenticated? | **Yes → Pattern D**, and stop — it leaves the internal platform. **No** → continue. |
| 2 | Is there a UI someone visits? | **Yes →** at least **Pattern A**. **No, pure backend →** lean **Pattern B**. |
| 3 | Always-on or bursty? | **Always-listening → A.** **Fires on event/schedule → B.** |
| 4 | Both a visited UI *and* triggered/AI backend? | **Yes → Pattern C** (hybrid). |
| 5 | Processes data with a model? | **Yes →** backend is **B** (Lambda + Bedrock); prefer Bedrock role auth over a key. |
| 6 | Holds a genuine secret that a role can't replace? | **Yes →** Secrets Manager (SYS-1623) is in scope. |

A tool can resolve to **more than one pattern** — that's expected, not a failure. A gated
dashboard fed by an AI listener is **B + C**. A tool with a public feed *and* an internal
console spans **D + C** — the signal to treat them as **two products**, rebuilt separately.

State the verdict explicitly: *"Target: Pattern C — Pattern A dashboard reading from a
Pattern B Lambda backend."*

---

## Phase 4 — Gap analysis

**Goal:** compare as-built against the standard for the target pattern. Produce a findings
table. One row per gap.

### Findings table format

| ID | Finding (as-built) | Violates | Severity | Pattern target |
|----|--------------------|----------|----------|----------------|
| F1 | Anthropic API key in `.env`, read at startup | Roles over keys | **High** | Bedrock role auth |
| F2 | Flask binds `0.0.0.0:5000`, no auth | Okta gate + SG lockdown | **High** | A: ALB-Okta, SG-to-SG |
| F3 | Always-on poller for an hourly feed | Bursty work → Lambda | Medium | B: EventBridge + Lambda |
| … | … | … | … | … |

### Severity rubric

- **High** — breaks a security invariant. Anything exposing the app outside the Okta gate,
  any credential in source, any host reachable from the open internet on the app port.
  These are non-negotiable; the rebuild does not ship with any High open.
- **Medium** — wrong pattern shape (works, but mis-architected): always-on compute for
  bursty work, a stored key where a role would serve, a public surface bolted to the
  internal stack.
- **Low** — hygiene: missing health check, no idempotency, config in code, no `systemd`
  unit, reboot-fragile.

### The signature failure modes — grep for these first

These recur in nearly every externally-built tool. Check them explicitly:

- **Personal API key as the credential.** `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` in `.env`
  or environment. → Replace with **Bedrock** (the IAM role becomes the credential; nothing
  to store or rotate). Reach for a stored key only if a capability genuinely isn't in-account.
- **App open to the world.** Binds `0.0.0.0` with no auth, or an SG with `0.0.0.0/0` on the
  app port. → The host must accept the app port **only from the ALB SG by ID**. This is the
  keystone — it's what makes Okta impossible to bypass at the network level.
- **Auth in the app.** Login code, session handling, a password check inside the tool. →
  Delete it. Authentication happens at the ALB via Okta; the app carries no login code.
- **Always-on compute for bursty work.** A 24/7 poller for an hourly feed. → EventBridge
  schedule or event trigger → Lambda; ~free when idle.
- **No SSM role / SSH assumptions.** Key-pair login, `ssh` in docs, no instance role. →
  SSM-only; attach the SSM instance role **at launch** (most-forgotten step; without it the
  host runs but is unreachable).
- **Genuine secret with nowhere to live.** A Slack webhook URL, a third-party token a role
  can't replace. → Secrets Manager, read at runtime, never in source.
- **Public + internal tangled together.** One codebase serving both a public feed and an
  internal console. → Split into two products: public on Pattern D's separate track,
  internal on A/C behind Okta.

---

## Phase 5 — Rebuild

**Goal:** an ordered remediation plan, then — on confirmation — the fixit code.

### Ordering principle

Fix in **request-path order**, outermost boundary inward — the same order the platform
teaches a request: **identity/network boundary → app → secrets/data**. Rationale: the
boundary is what makes everything else safe to run, so it's specified first even though
the builder provisions it last. Concretely:

1. **Name the boundary.** Okta app + access group for this door; the host-SG-from-ALB-SG
   rule; the target group + listener rule (authenticate → forward). Claude Code can't
   provision these — it writes the **spec** (ports, health-check path, group name, host-header)
   the builder runs.
2. **Reshape the app to the pattern.** A → `systemd` service on a dedicated port, health
   check on `/`, no login code. B → `handler(event)` with an idempotent body, no serve loop.
   C → both halves meeting at a defined data shape in a defined location (typically an S3 prefix).
3. **Design out every secret.** Key → Bedrock role. Genuine secret → Secrets Manager read
   at runtime. Nothing sensitive in the fixit repo.
4. **Add what the platform expects.** `systemd` unit (survives reboot), health endpoint,
   idempotency/dedupe for B, a small explicit read/write contract for C.

### Per-pattern rebuild targets (what "conforming" means)

**Pattern A — always-on internal web tool**
`Route 53 → ALB (HTTPS + Okta) → target group → EC2 service on its own port`
- App runs as a `systemd` service on a dedicated port (survives reboot).
- Host SG opens that port **only from the ALB SG by ID** — never an IP range.
- Target group (instance:port), health check on `/`.
- Okta OIDC app + access group for this door.
- HTTPS:443 listener rule — host-header condition → **authenticate (OIDC)** → **forward**.
- DNS alias → ALB. No login code in the app; no nginx required.

**Pattern B — event-driven / AI backend**
`Event/schedule → Lambda → (Bedrock for inference) → output (S3 / Slack / data store)`
- Trigger defined (EventBridge schedule, S3/SNS event, or webhook).
- Lambda IAM role scoped tight: `bedrock:InvokeModel` on the chosen model + its output target.
- Bedrock model access enabled for the model/region (confirm `us-east-1`).
- Outputs to a durable target; function **idempotent** (dedupe events).
- Any genuine secret left → Secrets Manager, read at runtime.

**Pattern C — hybrid**
`[A] ALB+Okta → EC2 dashboard  ←reads—  [B] Lambda(+Bedrock) → S3 / data store`
- Backend stands up as Pattern B, writing to a known S3 prefix / data store.
- Front-end stands up as Pattern A, reading from that store.
- Contract between them small and explicit — a defined shape in a defined location.
- **Single-host caveat:** services sharing one EC2 host run under **one IAM role** — no
  separate AWS permissions at the OS level. A Lambda backend sidesteps this (own role).
  When two tenants need genuinely different scoped perms on one box, one earns its own
  instance/container.

**Pattern D — public / unauthenticated**
`Route 53 → CloudFront + S3, or public ALB path — separate from the Okta stack`
- Treated as a distinct product with its own review — not bolted onto the internal ALB.
- Any internal console for the same tool stays on A/C, gated as normal.
- For life-safety content, latency and availability needs stated explicitly and reviewed
  before build.

### Stop point

After producing the plan, **stop and present**. Generate fixit code only after the operator
confirms. When confirmed, build into the fixit repo following the per-pattern targets,
carrying no secrets forward.

---

## The report Claude Code produces

A single markdown report, structured exactly so:

```
# Rebuild Assessment — <tool name>

## Source
- Repo: <url>   Commit: <sha>   Built by: <name>   Assessed: <date>

## Inventory
<the 8-question factual description, with file/line evidence>

## Classification
Target pattern(s): <verdict + one-line rationale>

## Findings
<the findings table: ID | finding | violates | severity | pattern target>
High: <n>   Medium: <n>   Low: <n>

## Rebuild plan
<ordered steps, request-path order; mark which are builder-provisioned vs. code>

## Builder spec (for the items Claude Code can't provision)
- Okta: app + group <name>, redirect URI <…>
- Host SG: open port <p> from ALB SG <id>
- Target group: instance:<p>, health check <path>
- Listener rule: host-header <host> → authenticate → forward

## Open questions / divergences
<anything that doesn't fit cleanly — surfaced, not absorbed>
```

Then, on confirmation, the fixit repo is populated and the tool is ready to host and demo.

---

*The Inquirer · IT / Systems · Confidential — internal*
