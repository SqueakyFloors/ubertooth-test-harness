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
  and, should one ever be needed, a pattern that has to match the Ubertooth
  fork's own output: it keeps matching what the fork prints and accepts both
  wordings while the fork changes over. The harness has no such pattern today.
  `scripts/ci.sh test` checks the rule; an exception of either kind goes in its
  `ROLE_TERMS_ALLOW` allowlist, which it names in its failure message.
- This repository is public: never name lab machines (host names, SSH aliases,
  IP addresses, `user@host` strings, lab home paths) or other systems running
  in the lab. See "Public text" in `.kiro/steering/branch-and-review.md`.
- Keep `COVERAGE.md` current: a PR that adds or changes test coverage of a
  CLI option updates its rows in the same PR.
- Coverage audits update `coverage/closed-pr-audit.jsonl` incrementally; never
  re-audit from scratch. A pass assesses only the closed fork PRs absent from
  the ledger, then re-evaluates the rows already classified `gap` or `partial`
  against the harness as it now stands, leaving `covered`, `not-needed`, and
  `closed-unmerged` rows alone. Read each PR at its merge commit, keep the file
  sorted by repository then PR number, and set `audited_at` and
  `audited_against_harness_commit` on every row you write. `docs/coverage-audit.md`
  has the field meanings and the procedure.
- Keep `docs/hardware.md` current: a PR that changes the bench hardware, any
  device's firmware, or a helper script under `scripts/lab/` updates it in the
  same PR. That covers a new or retired device type, a firmware version or
  toolchain bump, a change to a protocol or API the harness depends on, a host
  setup step, a newly found limit, and a prebuilt image (record its source
  commit and SHA-256). Reviewers check for it.
- Keep `docs/tests.md` current: a PR that adds a catalog test, removes one, or
  changes what a test checks updates that test's section in the same PR.
  Section headings are the catalog names exactly, so a missing section is easy
  to spot. The one exception is the trailing `Out of scope: ...` section, which
  records what the harness has decided not to test so that a missing catalog
  entry is not mistaken for an oversight; the file's own intro says so.
  Reviewers check for it.
- Keep `ubertooth_harness/test_info.yaml` current: it holds one entry per
  catalog test, keyed by the exact catalog name, and it is what the GUI shows
  beside a test name. A PR that adds a catalog test adds its entry, one that
  removes a test removes its entry, and one that changes what a test checks
  updates the fields that are no longer true, in the same PR and beside the
  `docs/tests.md` section it summarizes. Say "not established" rather than
  inventing a theory or a result. Do not restate what the catalog already
  says: the hardware roles, the accepted arguments and which are required,
  the coverage profiles, and the estimates come from `catalog.py`, and
  `/api/plans` reports them from there beside the prose, so an entry carries
  no argument default, no required flag, no role list, and no number of
  seconds. What each profile decides, how many cases and which, is this
  file's to say. The unit tests require an entry for every catalog test and
  refuse an entry that restates a catalog estimate, so drift fails rather
  than quietly misleading a reader. Reviewers check for it.
- Run the unit suite with `python scripts/test.py` (one process per module,
  one per core; `--durations 10` for the slowest tests). It is what
  `scripts/ci.sh test` runs. `python -m unittest discover -s tests` runs the
  same tests serially, and `python -m unittest tests.test_gui -v` one module.
  A test must pass both ways: nothing may depend on another module's state.
