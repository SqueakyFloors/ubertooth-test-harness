# Agent Rules

- Keep the working tree clean at every handoff. Commit intended source changes;
  remove or ignore generated artifacts.
- Harness changes may merge to `main` without maintainer review unless
  the maintainer explicitly requests one, but all repository review, test,
  history, and clean-tree gates still apply.
- Before merge, determine the authoritative main remote from
  `branch.main.remote` or repository documentation/configuration; if missing or
  ambiguous, stop and ask rather than guessing. Fetch it, make sure the PR
  branch contains its latest `main`, rerun relevant tests, verify
  `git status --porcelain` is empty, and land the change as one squashed commit
  by squash-merging the PR. The PR branch needs no squashing beforehand; never
  open a new branch or PR only to squash.
- Do not rewrite a published branch without explicit user direction.
- Keep hardware operations sequential by serial unless a test explicitly
  targets concurrency. Apply hard command and whole-run deadlines.
- Boards are shared between agents through leases: hardware runs go through
  `run-suite` (it leases its boards), and every manual Ubertooth command goes
  through `ubertooth-harness lease ... -- <command>`. Check
  `ubertooth-harness boards` first; never use or reflash a board someone else
  holds. Anything that switches USB power or writes firmware also holds the
  host lock (`run-suite` takes it itself; use `lease --host-lock` for a manual
  power change; `ubertooth-dfu` under `lease` takes it automatically). Where
  the host's own scripts lock boards with per-board files, leases take those
  too (`--external-board-lock` / `UBERTOOTH_HARNESS_EXTERNAL_BOARD_LOCK`;
  README "External board locks").
- A power cycle or manual recovery may gather diagnostics but never converts a
  failed reliability run into a pass.
- Local development servers must bind to `127.0.0.1`. Do not add a remotely
  reachable listener until the networking/authentication design is approved
  and implemented fail-closed.
- Never commit credentials, authentication tokens, employee data, or captured
  device traffic.
- Bluetooth piconet roles are **central** and **peripheral**, for BR/EDR and
  LE alike, in code, messages, report and GUI text, registry roles, docs, and
  tests. Never master/slave. Two things stay as they are: names we do not own
  (another repository's `master` branch, quoted upstream output, spec titles,
  and third-party API identifiers such as ESP-IDF's `ESP_SPP_ROLE_MASTER`),
  and a pattern that has to match the Ubertooth fork's own output, which keeps
  matching what the fork prints today and accepts both wordings while the fork
  changes over. `scripts/ci.sh test` checks this against an allowlist of those
  external names.
- This repository is public: never name lab machines (host names, SSH aliases,
  IP addresses, `user@host` strings, lab home paths) or other systems running
  in the lab. See "Public text" in `.kiro/steering/branch-and-review.md`.
- Keep `COVERAGE.md` current: a PR that adds or changes test coverage of a
  CLI option updates its rows in the same PR.
- Keep `docs/hardware.md` current: a PR that changes the bench hardware, any
  device's firmware, or a helper script under `scripts/lab/` updates it in the
  same PR. That covers a new or retired device type, a firmware version or
  toolchain bump, a change to a protocol or API the harness depends on, a host
  setup step, a newly found limit, and a prebuilt image (record its source
  commit and SHA-256). Reviewers check for it.
- Keep `docs/tests.md` current: a PR that adds a catalog test, removes one, or
  changes what a test checks updates that test's section in the same PR.
  Section headings are the catalog names exactly, so a missing section is easy
  to spot. Reviewers check for it.
- Run the unit suite with `python scripts/test.py` (one process per module,
  one per core; `--durations 10` for the slowest tests). It is what
  `scripts/ci.sh test` runs. `python -m unittest discover -s tests` runs the
  same tests serially, and `python -m unittest tests.test_gui -v` one module.
  A test must pass both ways: nothing may depend on another module's state.
