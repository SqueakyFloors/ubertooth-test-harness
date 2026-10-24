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
  holds.
- A power cycle or manual recovery may gather diagnostics but never converts a
  failed reliability run into a pass.
- Local development servers must bind to `127.0.0.1`. Do not add a remotely
  reachable listener until the networking/authentication design is approved
  and implemented fail-closed.
- Never commit credentials, authentication tokens, employee data, or captured
  device traffic.
