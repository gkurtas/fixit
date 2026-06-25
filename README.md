# fixit

The platform's home for **externally-built tools, reconstructed to conform**.

Tools arrive built outside the platform — a reporter's prototype, a personal-account
side project, an old internal app on someone's keys. Before such a tool can be
hosted, it is evaluated against the platform's build patterns and rebuilt to fit.
This repository is where every such rebuild lives: each tool is reconstructed
here as a clean, **org-owned** project — never a fork, never a link back to the
source.

## How it works

The procedure is the **[Rebuild Playbook](REBUILD-PLAYBOOK.md)** — five phases:
Intake → Inventory → Classify → Gap → Rebuild. The source repo is always cloned
**read-only** and never modified; no secrets are carried forward; the rebuild
targets the pattern's full request-path boundary, not just the code.

## Layout

```
REBUILD-PLAYBOOK.md     the shared procedure (applies to every tool)
tools/
  <tool-name>/          one self-contained rebuild per tool
    README.md           the rebuilt tool: architecture + local dev
    ASSESSMENT.md       the rebuild assessment (inventory, findings, plan)
    assessment-deck.pptx  the team-facing presentation
    DEPLOYMENT.md       step-by-step deploy, incl. the builder-provisioned gate
    site/ pipeline/ infra/   the rebuilt code + infrastructure
```

Each tool directory is independent. A new rebuild adds a `tools/<name>/` folder
on its own `rebuild/<name>` branch and merges via PR — `main` grows one vetted
tool at a time. The read-only source clone for an assessment lives in a
`_source/` directory (gitignored, never committed).

## Rebuilt tools

| Tool | Pattern | Status | Links |
|------|---------|--------|-------|
| [Weather Machine](tools/weather-machine/) | C (gated hybrid) | Rebuilt — pending gate provisioning | [README](tools/weather-machine/README.md) · [Assessment](tools/weather-machine/ASSESSMENT.md) · [Deploy](tools/weather-machine/DEPLOYMENT.md) |

---

*The Inquirer · IT / Systems · Confidential — internal*
