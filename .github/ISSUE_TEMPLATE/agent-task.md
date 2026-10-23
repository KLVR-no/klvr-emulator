---
name: Roadmap item
about: One change, sized for one PR (or one linked PR per repo), that an agent or a person can finish and prove
labels: []
---

<!-- Labels: one train-N, one or more surface:*, one theme:*, and a gate when it applies
     (agent-ready / needs-hardware / needs-decision). Canon: firmware repo docs/agents/README.md -->

## Why

<!-- Evidence: contract row (C-id), roadmap row (SR-id, theme), customer report, measurement. -->

## Done when

<!-- The test that fails before and passes after. Name the file and the case.
     Hardware-only checks: the bench-walk row(s). -->

## Scope

- Repo(s) and order: <!-- firmware → emulator → Control, for cross-repo items -->
- In:
- Out:

## Verification

- [ ] Test written first, fails on the base branch (when this change touches code; otherwise n/a: e.g. docs-only)
- [ ] PR CI green (when the repo has CI for this change; otherwise n/a: e.g. docs-only)
- [ ] Reviewer: simplicity standard and repo invariants, net lines reported
- [ ] Red team (code changes)
- [ ] Parity (cross-repo)
- [ ] Bench (needs-hardware)
