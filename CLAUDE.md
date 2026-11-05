# CLAUDE.md

A process that takes one archive and one camera, and publishes each sensor's occupancy. The plan is rails49/.github#60.

The models sometimes report a car that is not there, and sometimes miss one. Present nothing here as a safety interlock.

gate: bin/test.sh

## Where things are

What each kind of doc holds: org [ADR-0021](https://github.com/rails49/.github/blob/main/docs/adr/0021-each-fact-has-one-owner-and-the-glossary-is-glossary-md.md). See `docs/agents/domain.md`.

- Terms: `GLOSSARY.md`. Decisions: `docs/adr/`.
- The HTTP contract for live data: `LIVE.md`.
- The `.r49` format is `rails49-r49`'s, pinned to one version (org [ADR-0022](https://github.com/rails49/.github/blob/main/docs/adr/0022-the-r49-format-is-r49s-and-each-consumer-pins-it.md)).

## Agent skills

### Issue tracker

GitHub Issues on `rails49/occupancy-service`, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five default labels, each string equal to its role name: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
