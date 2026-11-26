# CLAUDE.md

A process that takes one archive and one camera, and publishes each sensor's occupancy. The plan is rails49/.github#60.

The models sometimes report a car that is not there, and sometimes miss one. Present nothing here as a safety interlock.

gate: bin/test.sh

## Where things are

What each kind of doc holds: org [ADR-0025](https://github.com/rails49/.github/blob/main/docs/adr/0025-each-fact-has-one-owner-and-an-adr-is-written-after-its-decision-has-run.md).

- Terms: `GLOSSARY.md`. Decisions: `docs/adr/`.
- The HTTP contract for live data: `LIVE.md`.
- The `.r49` format is `rails49-r49`'s, pinned to one version (org [ADR-0022](https://github.com/rails49/.github/blob/main/docs/adr/0022-the-r49-format-is-r49s-and-each-consumer-pins-it.md)).

## Agent skills

The rules every rails49 repository shares are the organisation's
[`docs/agents/`](https://github.com/rails49/.github/tree/main/docs/agents):
`issue-tracker.md`, `triage-labels.md` and `domain.md`.

- Issue tracker: GitHub Issues on rails49/occupancy-service, via the `gh` CLI.
- Triage labels: `needs-triage`, `needs-info`, `ready-for-agent`,
  `ready-for-human`, `wontfix`.
- Domain docs: `GLOSSARY.md` and `docs/adr/` at the repo root.
