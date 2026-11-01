# The closed-PR coverage audit

`coverage/closed-pr-audit.jsonl` records what every closed pull request in the
Ubertooth fork and the libbtbb fork means for this harness: whether a test here
would notice the behavior it fixed breaking again. It exists so the question is
asked once per PR and then stays answered.

## What a row says

One JSON object per line, sorted by repository then PR number:

| Field | Meaning |
|---|---|
| `repository` | `ubertooth` or `libbtbb` |
| `pr` | the pull request number in that repository |
| `title` | its title, as a handle for a reader |
| `state` | `merged`, or `closed` for one closed without merging |
| `merge_commit` | the exact commit the assessment was read from; empty when the PR was not merged |
| `closed_at` | the date it closed, `YYYY-MM-DD` |
| `classification` | see below |
| `candidate_key` | the behavior mechanism this row was grouped under, blank where the row needs no test |
| `evidence` | why that classification, naming the harness code or document that does or does not assert it |
| `audited_at` | the UTC date this row was written |
| `audited_against_harness_commit` | the harness `main` commit the row was judged against |

Classifications:

- **`covered`** — a test runs the behavior and checks its effect. The evidence
  names the test and the assertion.
- **`partial`** — some of it is checked and the evidence says what is not.
- **`gap`** — nothing here would notice a regression.
- **`not-needed`** — nothing a user meets changed: documentation, comments,
  repository process, bench-only firmware, or a refactor the PR itself
  establishes as behavior-preserving.
- **`closed-unmerged`** — closed without merging. The evidence names where the
  work landed instead, so it is accounted for without being judged twice.

`candidate_key` groups rows by the behavior a prospective test would exercise
rather than by title, so several PRs one test would cover share a key. It is a
grouping handle, not a promise that a test exists or is planned.

## What counts as coverage

Only an assertion. A PR mentioned in an issue, named in a changelog, or
exercised by a `-h` smoke invocation is not covered. Neither is a behavior
covered by a fork's own unit tests: those run in the fork's CI, not here, and
this file measures this harness. Where the evidence does not settle a row, it
says so and the row is not claimed as covered.

## Running the audit again

The audit is **incremental**. A later pass does two things and nothing else:

1. **Assess only PRs absent from the ledger.** Take the closed-PR list for both
   forks, subtract the `(repository, pr)` pairs already in the file, and read
   each remaining PR at its merge commit against the harness as it stands.
   Append the new rows and re-sort the file by repository then PR number.
2. **Re-evaluate every row classified `gap` or `partial`.** Those are the rows
   the harness can have caught up with. A test written since the row was
   recorded may now cover the behavior; if it does, change the classification,
   rewrite the evidence to name the test, and update `audited_at` and
   `audited_against_harness_commit` on that row.

Rows classified `covered`, `not-needed`, or `closed-unmerged` are left alone.
A `covered` row can only become wrong if a test is deleted, which the test-doc
and coverage rules already make visible; a `not-needed` row's subject did not
change behavior and will not start.

Keep the two audited fields per row rather than globally: after an incremental
pass the file holds rows judged against different harness commits, and the row
is what says which.

A row is corrected rather than rewritten from scratch: keep what still holds
and replace what changed, so the evidence does not lose the detail that made it
checkable. Read the merge commit, not the PR title or body — in the first audit
a row was nearly reclassified on its own summary and the commit showed the
opposite.

## What it is for

The gap and partial rows are the input to choosing the next integration test.
They are grouped by mechanism so a candidate test can be scored against the
whole group it would cover rather than one PR at a time. Picking which
candidates to implement is a maintainer decision; this file supplies the
evidence for it.
