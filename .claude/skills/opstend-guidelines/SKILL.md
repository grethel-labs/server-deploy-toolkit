---
name: opstend-guidelines
description: OpsTend's rules for security, user experience, automation and code. Read before changing anything in this repository – code, UI text, workflows or docs – and when reviewing a change.
---

# OpsTend guidelines

OpsTend runs infrastructure for people who are not full-time administrators. Every
change has to keep three promises: **safe**, **understandable**, **self-managing**.

## Security invariants (never relax these)

1. **Every change is a job.** Nothing outside `core/jobs.py` changes a system. A job
   has a preview, approval (by a person or by policy), execution, readback and
   rollback. Do not add API routes or loops that call adapters directly.
2. **Stale previews are refused.** Actions declare the facts their preview depends on
   (`FACTS` in `actions.py`). If those facts change, approval fails.
3. **Only the leader executes, fenced by term.** Do not run jobs on followers.
4. **Least privilege.** The web app/API runs as the `opstend` user. Root work goes
   through the agent (`agent.py`) with its allowlist and deny list. Never add `sh`,
   `bash`, `env`, interpreters or editors to `ALLOWED`. New privileged commands need a
   test in `tests/test_agent.py` that proves dangerous variants are refused.
5. **Argument lists, never shell strings.** Use `Runner.run([...])`. Over SSH the
   runner quotes each argument; keep it that way.
6. **Secrets by reference.** Config points to files (mode 0600) or systemd
   credentials. Secrets never go into the replicated store, jobs, logs, Zorec context
   or the browser. Workload secrets are entered once and stored encrypted.
7. **Pinned trust.** Proxmox and UniFi certificates are pinned at adoption; cluster
   traffic is mutual TLS with the cluster CA; releases are verified with Sigstore and
   fail closed.
8. **Zorec suggests, people decide.** Model output is data. Suggested actions are
   validated against the registry and become job previews. Fleet data goes to the
   model inside `<fleet_data>` and is never treated as instructions.
9. **Fail closed, report gaps.** A scan that could not run is a finding ("scan
   incomplete"), never "no vulnerabilities". Unknown probe results are not healthy.
10. **Private by default.** Discovery scans private ranges only. The web app binds to
    the configured address; public exposure of services needs explicit approval.

## User experience

- **Lead with what needs attention:** incidents and decisions first, numbers second.
- **One word per concept.** Use: *Auftrag/job*, *Entscheidung/decision*,
  *Störung/incident*, *Hinweis/insight*, *Befund/finding*, *Wartungsfenster/maintenance
  window*. Do not introduce synonyms (no "Vorgang", "Beleg", "Readback" in the UI).
- **One language system.** English is the source language and the default for the API
  and everything internal. Write every human-facing text once, in English, with named
  placeholders: server `tr("Restart {service}", service=name)` (`opstend.i18n`), web
  `t("Restart {service}", { service })` (`useT()`). Translations live only in
  `src/opstend/locales/<lang>.json` (keyed by the English text); the web app loads the
  same file from `/api/i18n/<lang>`. Plurals use separate keys chosen by count.
  `tests/test_i18n.py` fails if a text is missing from a catalog or placeholders differ.
  Stored texts (jobs, steps, findings) keep their template and are rendered in each
  reader's language by the API (`i18n.localize`). Never build sentences by concatenating
  translated fragments; translate the whole sentence.
- **Say what happens.** Buttons name the action ("Freigeben und ausführen"), previews
  list each step in plain words, errors say what went wrong and what to do.
- **Same actions everywhere.** A guest offers the same buttons on Proxmox, libvirt and
  Podman; unsupported actions are hidden, not disabled without reason.
- **Phones count.** Every page works at 390 px without horizontal scrolling
  (`tests` include a phone project). Keep tables in `.table-wrap`.
- **Both themes.** Use the CSS tokens in `web/src/styles.css`; no literal colors in
  components. The topology uses excaliplant's `grethel-labs-light/dark` themes.
- **Diagrams with excaliplant.** Network views are PlantUML rendered by
  excaliplant with `aspectRatio` and `toSvg({ interactive: true })`; elements carry
  `@hook` ids from `network/plantuml.hook_id`.

## Automation

- New self-management behaviour goes into `selfmanage/reconciler.py` as a
  `Proposal` with a `Category`. The `Governor` decides (observe, propose, auto,
  budgets, cooldowns, windows, kill switch). Never bypass it.
- Disruptive categories (patch, upgrade, power, network) wait for the maintenance
  window unless urgent (known exploited and exposed).
- Fleet-wide changes go through `cluster/rollout.py`: canary first, leaves before
  core devices, never more voters offline than quorum allows, health gate per wave.

## Code

- Python 3.12+, typed (mypy clean), `ruff check` and `ruff format` clean. Pure logic
  gets unit tests with fakes; adapters get tests against recorded API shapes
  (`httpx.MockTransport`). Never claim real-system behaviour from fakes.
- Web: React + TypeScript, no UI framework, no CSS framework. Keep dependencies few.
- Commits and PR titles follow Conventional Commits (`feat:`, `fix:`, `security:`,
  `perf:`, `refactor:`, `docs:`, `test:`, `build:`, `ci:`, `chore:`). Releases are
  computed from them.
- Docs describe the current state. Plans live in `ROADMAP.md`, not in status logs.
