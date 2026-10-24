---
inclusion: always
---
# Branch Naming and Review Rules

This file is intentional version-controlled workspace policy and MUST NOT be
excluded by `.gitignore`. It supplements other repository safety/merge rules;
when rules differ, follow the stricter requirement.

## Issue-first branches

- For every new branch that will modify repository content, MUST identify or
  create the tracking issue before creating the branch whenever the issue
  tracker is available and issue creation is permitted.
- The new branch MUST start with `<issue-number>_`, followed by a short
  descriptive slug, for example `9_agent-rules`.
- For work spanning several issues, use the primary issue number and reference
  the others in commits and review notes.
- Existing published branches, externally named review refs, read-only work,
  unavailable issue tracking, and emergency recovery are exceptions; record
  why the issue-first prefix was impractical.

## Public text

This repository is public. Everything published from it — code, tests,
fixtures, plan files, file names, docs, comments, commit messages, branch
names, issues, pull requests and their comments, and CI output — is covered.
So is anything a text search cannot read: committed screenshots and other
images, and committed run output (`run.jsonl`, `report.md`, captures). Those
must be synthetic or scrubbed, because a real one leaks past every scan.

- MUST NOT name lab machines: no host names, SSH aliases, IP addresses,
  `user@host` strings, or lab home-directory paths. Write "the lab", "the lab
  host", "the Linux lab host", or "the jump host" instead. Scripts take the
  host from configuration, an environment variable, or an argument, never a
  built-in host name, and fail with a clear message when none is set.
- MUST NOT name other systems or services that run in the lab, or the
  maintainer's employer's internal tools, repositories, or services. Describe
  such workloads generically, for example "a production-style MAC polling
  workload" or "other software on the lab host".
- Board serial numbers of lab Ubertooths are fine.
- Keep the real host names, aliases, and credentials in private, uncommitted
  configuration outside the repository.
- Check what you are about to publish against these rules before committing,
  pushing, or posting. If unsure whether something is internal, ask.

## Independent agent review

Every merge candidate containing code, configuration, workflow, or policy
changes:

- Docs-only changes (`*.md` outside `.kiro/steering/`) get no agent review and
  no CI wait; merge once the PR is otherwise ready.
- MUST receive at least two independent read-only agent reviews.
- MUST use different recorded model identifiers or model families; two prompts
  or roles on the same model do not satisfy this requirement.
- MUST give each reviewer the same final merge-base diff and acceptance criteria
  without sharing the other reviewer's conclusions.
- SHOULD run reviewers in parallel.
- MUST resolve every blocker and rerun affected validation.
- If any candidate content changes after a blocker is reported, both reviewers
  MUST review the resulting final diff again.
- If two different review models are unavailable, MUST disclose that the gate
  is unsatisfied and MUST NOT describe the change as review-complete.

## Private merge gate

After the two-model review and relevant tests pass, private harness changes MAY
merge without a separate maintainer review unless the maintainer explicitly
requests one. Determine the authoritative main remote from `branch.main.remote`
or repository documentation/configuration; if missing or ambiguous, stop and
ask rather than guessing. The candidate MUST still satisfy existing clean-tree,
latest-main, one-logical-commit, testing, and published-history requirements.

## Time budget and the good-enough call

GitHub Actions CI is the merge gate. It is deliberately minimal: the `test` and
`build` jobs on Ubuntu 24.04 and the current macOS (`macos-latest`) only, run
once per PR head (branch pushes do not start a second run). Do not add other
OS versions or runners without the maintainer asking.

- **No CI wait** for changes limited to documentation or agent rules
  (`*.md`, `.kiro/steering/`): merge once the PR is otherwise ready.
- **Wait in the background** and keep working meanwhile; check status every
  30 seconds. Never block in the foreground on a sleep loop or a long
  `--watch`.
- **Start deadline, 5 minutes**: if no job has started 5 minutes after the
  push, stop waiting, say so, and decide with the maintainer.
- **Run deadline, 10 minutes after jobs start**: then use a bounded watch,
  `perl -e 'alarm shift; exec @ARGV' 600 gh pr checks <PR> --watch --fail-fast`
  (macOS has no `timeout`). A job with no progress for 10 minutes while the
  others are done is runner noise: cancel it, rerun it once with
  `gh run rerun <run-id> --failed`, and note it.
- **Good enough**: merge when every job has passed on the PR head. A job that
  fails without running any step (runner, billing, or capacity error) is not a
  code failure; report it to the maintainer, then fall back to local CI below.
- Any other failed job blocks the merge until it is explained or fixed.

### Local CI when GitHub Actions cannot run

Only when Actions jobs cannot run at all (runner, billing, or capacity errors
with no step executed), use local CI instead, after telling the maintainer:

- Run `scripts/local-ci.sh` on the exact PR head on a macOS host and on a
  Linux (Ubuntu 24.04) host. For the Linux lab host, through the jump host
  (substitute the real names, which are not recorded here):
  `scripts/local-ci.sh -s "ssh -o BatchMode=yes -o RequestTTY=no -o RemoteCommand=none jump-host.example ssh -o BatchMode=yes lab-host.example"`.
  Run both in the background, in parallel.
- Both must print `LOCAL CI PASSED <sha>` for the same SHA that will be
  squashed. Post both lines in a PR comment before merging.
- Any new commit on the branch needs a new pair of passes.
- The script tests a commit exported with `git archive`, never the working
  tree, so uncommitted changes cannot leak into a pass.
