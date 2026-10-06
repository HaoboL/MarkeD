# MarkeD

MarkeD is an early open-source Agent/Skill recommendation prototype. It keeps Agent instructions and Skills as separate versioned artifacts, recommends one item or a compatible pair for a task, and shows the source, license claim, version, and reason before loading contributed text. The host model performs the user's task.

The current router is a conservative **keyword baseline**. Its topical score is not a quality score or a calibrated probability. No user benefit, quality improvement, automatic ranking, marketplace economy, or sustainable revenue has been established.

## What the prototype does

- Imports a complete artifact package as pending; a named local reviewer must check the exact source, license, and content package before admission.
- Previews a recommendation from a `UserPromptSubmit` Hook without sending the Agent/Skill body. The user can accept, switch to a displayed alternative, or reject. With no suitable title or reviewed-tag match, the router abstains.
- Loads the selected package only after the choice. Supporting files are hash-verified and exported locally; MarkeD does not execute them. A paused version cannot be loaded through an old route.
- Supports per-task-type and per-Harness manual active/canary/paused rollouts with audit records. One real task receives one delivered version, with no hidden comparison run.
- Offers **default-off**, per-session local analysis of clear next-turn feedback after an actually loaded route. It stores a short matched excerpt, not the whole message. Users can inspect, correct, delete, or turn off this signal. `python3 -m marked feedback-combinations` shows exact Agent/Skill configurations by task type and host with separate clear, unjudged, missing and ambiguous counts; it is not a quality score. No extra feedback model call or automatic score change occurs.

The Codex Hook was exercised in isolated live host sessions using artificial tasks. The Claude Code adapter has local protocol checks but no completed live host validation. The examples are brief engineering demonstrations, not certified expert content.

## Requirements and local setup

Python 3.10 or newer is required; the runtime has no third-party Python dependencies. A fresh clone contains **no populated catalog**. The plugin package contains runtime code only; it does not include `examples/`, a database, private conversations, or downloaded third-party Skills.

From the source checkout, create a local catalog and import the two original MIT demonstration artifacts:

```bash
python3 -m marked init
python3 -m marked import examples/code-review-agent/artifact.json
python3 -m marked import examples/security-review-skill/artifact.json
python3 -m marked review code-review-agent 1.0.1 approve --reviewer local-operator --evidence-ref examples/REVIEW.md --source-checked --license-checked --content-checked
python3 -m marked review security-review-skill 1.0.1 approve --reviewer local-operator --evidence-ref examples/REVIEW.md --source-checked --license-checked --content-checked
python3 -m marked admit code-review-agent 1.0.1
python3 -m marked admit security-review-skill 1.0.1
python3 -m marked rollout code-review-agent 1.0.1 active --reason 'local example'
python3 -m marked rollout security-review-skill 1.0.1 active --reason 'local example'
python3 -m marked route 'code review this security change' --harness codex --task-type code-review
```

Read `examples/REVIEW.md` and inspect the exact files yourself before attesting to a review. These commands set local test rollouts; they do not publish or certify content. An academic editor and proofreading example are also available under `examples/`. Each original example package includes its MIT `LICENSE.txt`.

To install the Codex plugin from this checkout:

```bash
codex plugin marketplace add /path/to/MarkeD --json
codex plugin add marked@marked-local --json
```

Review and trust the Hook in the host. Set `MARKED_DB` to the populated catalog path in the environment that starts Codex. Without it, the installed Hook may use an empty plugin-data catalog and make no recommendation. `MARKED_TASK_TYPE` defaults to `general`; use an appropriate explicit task type when testing a task-specific rollout. No content is downloaded or activated automatically.

In a host session, use the exact command displayed in the preview, such as `marked use ROUTE_ID`, `marked reject ROUTE_ID`, or `marked switch ROUTE_ID ARTIFACT_ID VERSION`. Sending `marked feedback on` enables the local next-turn signal for that session; `marked feedback off` stops it. Normal context delivery can still use host tokens. `MARKED_AUTO_LOAD=1` is a separate operator opt-in for immediate loading of single-file selections and is outside the default preview-first path.

## Data, contribution, and validation boundaries

Routing uses the current task text in memory and stores a salted session digest, route IDs, selected versions, and choice/load events in local SQLite. It does not store the raw task or output by default. The optional feedback path stores only clear, short matched excerpts locally after per-session opt-in; it has no automatic expiry. `feedback-clear` removes inferred feedback and consent settings but retains separate route/choice/load history. Codex itself has its own data handling.

The importer and checks do not prove ownership, safety, or task quality. First-round third-party review is limited to MIT or Apache-2.0 complete packages with a bundled license file and named manual source/license/content review. Apache approvals also require a recorded upstream NOTICE check (`--notice-checked`); applicable NOTICE and attribution must be preserved. See `CONTRIBUTING.md` for the candidate format and PR, complaint, appeal and withdrawal paths. Third-party proposals are not admitted to MarkeD-distributed routing until a private sensitive-report channel and external handling process are operational. The optional sample download scripts keep fetched material in ignored local quarantine; no fetched third-party Skill is bundled here.

Run the local checks with:

```bash
python3 -B -m unittest discover -s tests -v
```

The MarkeD-owned code, documentation, and four original examples are MIT licensed. Third-party Agent/Skill artifacts retain their own licenses. This is an experimental open-source prototype; any commercial service is a separate decision.
