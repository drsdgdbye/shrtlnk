# PIPELINE: the beach-team protocol

Applies in the product repository where the harness is deployed (`scripts/render-team.py`).
Configuration — `beach-team.json`; resolution for dispatch — `.pipeline/team.json`.

## 0. Team configuration

- Stack: `go`.
- Core (always): lead, developer, reviewer. Modules: architect.
- Default routes: feat → full, fix → standard, chore → standard, hotfix → standard, release → release.
- Git model: `simple-gitflow` — main: `main`, dev: `dev`; task id format: `DRS-YYMMDD-NN` (pattern `DRS-\d{6}-\d{2}`).
- Required project checks:
- `gofmt -l .`
- `go vet ./...`
- `go build ./...`
- `go test ./...`
- `go test -race ./...`

## 1. Route

```
GOAL → DESIGN → APPROVAL → BRIEF → DISPATCH → VERIFY → ACCEPT → COMMIT
                                     ↑            │
                                     └── REWORK ←─┘   (≤ 3 attempts)
GAP / budget / timeout → ESCALATE
```

| State | Event | Condition | New state |
|---|---|---|---|
| GOAL | task accepted, `task_id` assigned | task type determined | DESIGN (feat) or BRIEF |
| DESIGN | design ready | `.pipeline/designs/<task_id>.md` written | APPROVAL |
| APPROVAL | human decision | "I approve" received | BRIEF |
| BRIEF | brief written | mandatory fields filled | DISPATCH |
| DISPATCH | developer launched | brief + route + (for `full`) approved design + budget | RUNNING |
| RUNNING | developer returned a result | changes made, checks run | VERIFYING |
| VERIFYING | reviewer verdict | PASS on the current hash | ACCEPTED |
| VERIFYING | reviewer verdict | FAIL, attempt < 3 | REWORK |
| VERIFYING | reviewer verdict | FAIL, attempt = 3 | ESCALATED |
| VERIFYING | reviewer verdict | INCONCLUSIVE | re-verification or ESCALATED |
| ACCEPTED | lead commit | verdict hash = index hash | COMMITTED |
| any active | timeout 60 min | — | TIMED_OUT (infrastructure failure) → retry up to 4 |

The lead maintains the status in `.pipeline/state/<task_id>.json`. Absence of status is not "almost
ready" but absence of state.

### 1.1. Task types and routes

Task types: `feat`, `fix`, `chore`, `hotfix`, `release`; task ids follow `DRS-YYMMDD-NN`
(strictly the configured format). The route is determined by the type: `routes.<type>` from
`.pipeline/team.json`; the human's words or the `/assisted` command can override it.

- `feat` — new functionality; by default the `full` route (architect → developer → reviewer); with the
  architect module disabled or by the human's word ("skip the architect") — `standard`.
- `fix` — a point fix: the `standard` route (developer → reviewer), the brief is minimal but
  mandatory; the reviewer checks the narrow criterion and the absence of side changes.
- `chore` — configuration, documentation, repository infrastructure; requirements as for `fix`.
- `hotfix` — an urgent fix of the stable branch: the `standard` route; the branch is forked from main.
- `release` — a release task: the `release` route, no developer or reviewer dispatch; the lead drives
  the release PR, the tag and the back-merge (§14).
- `assisted` — a route, not a type: the human makes the edit, the reviewer verifies, the lead commits.
  Enabled by the words "I'll fix it myself", "without the developer", `/assisted`; developer dispatch on
  this route is forbidden by the plugin, and a snapshot fixed by the human counts as an attempt.

Branch naming follows the type (`feat/<task_id>`, `fix/<task_id>`, `chore/<task_id>`,
`hotfix/<task_id>`, `release/<tag>`); bases, PR targets and merge rules — §14.

The light track does not cancel the gates: `task_id`, brief, independent verification and commit by the current PASS
apply. Required checks — from `checks` in `beach-team.json`.

## 2. Directories and identifiers

```
.pipeline/
├── designs/<task_id>.md
├── briefs/<task_id>.md
├── verdicts/<task_id>-a<N>.md      human-readable verdict of the attempt
├── verdicts/<task_id>-a<N>.json    machine verdict for the commit gate
├── verdicts/<task_id>-a<N>.seal    seal of the attempt's file hashes
├── rework/<task_id>-a<N>.md        rework packet
├── rework/<task_id>-a<N>.seal      seal of the packet
├── state/<task_id>.json
├── tools/candidate-hash.sh
├── templates/
├── matrix.md
└── pipeline-log.md
```

- `task_id` — `DRS-YYMMDD-NN` (for example, `DRS-260922-01`), assigned by the lead; the
  format comes from `tasks.id_format` in `beach-team.json`.
- `attempt_id` — `<task_id>-a<N>`, N = 1..3.
- `finding_id` — `F-<number>` within the verdict.
- The brief is edited only by the lead and only before dispatch. Fixing a brief error after FAIL is formalized
  as `brief v2` with a note in state; the old verdict stays sealed and is superseded by the new
  brief, the attempt is spent (§5).

## 3. Candidate and hash

- The candidate is the working tree. The lead fixes it: `git add -A -- . ':(exclude).pipeline'`.
- Candidate hash: `.pipeline/tools/candidate-hash.sh` → `sha256:<hex>` of
  `git diff --cached --binary -- . ':(exclude).pipeline'`.
- The reviewer verifies exactly the fixed candidate and specifies the hash in the verdict.
- Any edit after fixation changes the index or the tree → the old hash stops matching, PASS
  loses force.
- Only the lead performs the commit: `git commit -m "<task_id>: <summary>"`. The plugin gate takes the **latest
  attempt**, requires `PASS`, a hash match with the index and a valid seal of the verdict files; on
  mismatch commit is forbidden. `--amend`, `-a`, `--patch` are forbidden.
- Index is a single delivery slot: one task per repository is in active work at a time.
  Parallel tasks will appear together with worktrees (stage 2); until then, do not start a second one.

## 4. Budgets and infrastructure failures

- An attempt = a fixed candidate. The `attempts` counter increases when the lead fixes a
  snapshot (`git add` with a new hash); the limit is 3 attempts per task. Changing the agent does not reset the counter.
- An infrastructure failure is a provider failure, an interrupted run or a timeout without a candidate. It does not
  consume an attempt: the plugin counts such cases separately (`infra_failures`) and after 4 failures
  forbids developer dispatch.
- After 4 infrastructure failures the human makes the decision: **continue**, **replace the executor**
  or **stop**. The decision is recorded in state (`infra_decision`); on `replace`
  the executor changes (`executor`), on `stop` the task is escalated. After the first allowed
  dispatch the failure counter is reset, the decision goes to `infra_decision_history`.
- Dispatch timeout: 60 minutes. The lead finds out the agent's state and, if it hangs, cancels it
  (`paseo_cancel_agent`); a timeout without a candidate is the same infrastructure failure.
- A timeout and INCONCLUSIVE are not FAIL: they do not prove a defect, but they do not permit release either.

## 5. Finding routing

- A finding in the current diff → an immediate fix via the rework packet.
- A finding in someone else's/legacy code → to a batch escalation to the human: the file
  `.pipeline/findings/<task_id>-F<N>-<slug>.md` according to the template `.pipeline/templates/finding.md`
  (severity, evidence, reproduction, recommendation). Such a finding is not included in the task diff,
  rework is not assembled for it; the priority decision is up to the human.
- Mandatory finding field "Evidence source": `reproduced` (the probe was actually run,
  in a scratch directory outside the repository) or `by code reading`; claiming "reproduced" without a run
  is forbidden.
- A legacy-code finding does not change the candidate verdict: the verdict is according to the brief's criteria.
- **Brief defect** (a finding with owner "lead": contradiction of criteria, a criterion unattainable in the
  scope): the verdict is not annulled and not edited — it remains evidence of its attempt,
  the attempt is consumed. The lead issues brief v(N+1) with the fix, records in state `brief_version`
  and `superseded_verdicts` (with the reason), enters the criteria diff into the log and reports to the human;
  the candidate is not rolled back, the next attempt is verified according to the new brief.
- **Executor objection**: "not confirmed" in the rework report is admissible only with evidence
  (reproduction, references to code/tests). The lead does not resolve a technical dispute on the merits, does not demand
  edits outside the brief or the permitted scope and does not accept work without a fresh PASS. If the objection
  and the verdict are incompatible — escalation to the human: both positions, evidence, options (stop,
  replace the executor, reissue the brief, re-verify with a fresh reviewer).
- It is **forbidden** to declare a verdict "annulled", delete or edit verdict files: the history of
  verdicts is preserved in full.
- Exception: a defect makes the current delivery unsafe or blocks verification — work
  stops in that part and is decided by the human.

## 6. GAP

An incomplete spec, a contradiction of criteria, monetary and irreversible rules → the lead asks the
human before dispatch. A silent stub or a guess is forbidden. An open question is recorded in
state (`open_questions`).

## 7. Escalation

Format: problem → impact → collected evidence (artifact paths, hash, verdict) →
continuation options. Channel — a Paseo notification to the human. The diff and artifacts are preserved.

## 8. Manual intervention

The human can approve or reject the design, answer a GAP, stop the task, and also make an
edit themselves — but then it passes reviewer verification and lead commit, like any change.

The reviewer may be invoked directly for an audit or a code question: it is read-only, this
does not affect the gates. The developer cannot be invoked directly, without a `task_id` and a brief — this is forbidden by the plugin.
Bypassing gates by hand is not allowed: editing code after PASS annuls the verdict, and a commit without PASS
is rejected by the commit gate.

## 9. What is mechanized and what is not

The full list is `.pipeline/matrix.md`. In short: mechanized are role write rights, "no
brief — no dispatch", "no approved design (`full` route) — no brief", "the `assisted` route
forbids developer dispatch", "architect — only when the module is enabled",
"reviewer only by a fixed candidate", the attempt counter by candidate, a separate
infrastructure-failure counter with threshold 4, commit only by the latest sealed PASS,
immutability of verdicts and rework packets, roadmap dependencies, approvals for delivery.
Environment permissions not covered by role rights are handled by the human (Paseo) — this is an external harness:
the `permission.ask` hook is not called in the current opencode build, do not rely on it. On role
discipline: the GAP gate, the watchdog, finding routing, brief and verdict quality. These shortfalls are named, not silent.

## 10. Report mode

Lead reports can be brief (by default) or detailed — for a new or complex project, for
a new user. Detailed mode is enabled by keywords in a message to the lead ("verbose",
"in detail", "simply", "for a beginner") or by the `/verbose` command;
it is disabled by "brief", "shorter", "no details", "normal mode" or `/brief`.

The mode is sticky: the plugin writes it to `.pipeline/report-mode` and duplicates it in `report_mode`
of the active task, the lead reads it at startup. A detailed report describes: what is happening now,
what has already been done and what it means, unfamiliar terms, what is required from the human, what comes
next — in simple language, without jargon.

## 11. Evidence and seals

- Verdicts and rework packets are immutable evidence: only creating a new file, without
  overwriting. The seal (`<task>-a<N>.seal`) records the hashes of the attempt's files; an edit after the seal
  breaks the commit gate.
- Only the reviewer writes the verdict; the lead does not write in `.pipeline/verdicts/**` at all. An error in a verdict
  is corrected by a new attempt or by a message, not by editing the file.
- Integrity check: `scripts/verify-artifacts.py --target <repository>` (part of
  `check-gates.sh`).

## 12. Plan and roadmap

- `.pipeline/roadmap.json` — the product plan: nodes `{id, name, status, completion, depends_on,
  evidence, next_action, blocking_reason}`; statuses `pending | active | blocked | done | cancelled`;
  `done` only with evidence.
- A task is linked to a node via `roadmap_id` in state. The plugin gate forbids dispatch while
  the node's dependencies are incomplete.
- On a "status" request the lead shows the tree (✅ / 🟡 / ⬜) and the paragraph "Live state".

## 13. Context and lifecycles

- Developer and reviewer — one attempt per session; a new attempt — a fresh agent. After the task is closed
  the executors are archived.
- Agents created outside the roles (drill leads, probes, observer agents) are archived manually when the
  drill or investigation is closed.
- The architect is invoked only at a genuine fork.
- The lead monitors context usage: >70% — finish the step and transfer the state to files; on
  auto-compaction or degradation — recreation by the resume procedure (state, roadmap, log).
- State transfer — `.pipeline/handoff/<task_id>-<timestamp>.md` according to the template
  `.pipeline/templates/handoff.md`; the path is recorded in state (`handoff_file`). The new lead
  resumes by the Resume section, not by a retelling in chat.

## 14. Git model and delivery

The model is set by `git.model` in `beach-team.json`; long-lived branches are `main` (stable,
releases) and `dev` (integration).

**simple-gitflow** — typed branches:

| Type | Branch | Forked from | PR base | Merge |
|---|---|---|---|---|
| feat | `feat/<task_id>` | dev | dev | squash |
| fix | `fix/<task_id>` | dev | dev | squash |
| chore | `chore/<task_id>` | dev | dev | squash |
| hotfix | `hotfix/<task_id>` | main | main | merge commit |
| release | `release/<tag>` | dev | main | merge commit |

- Task ids follow `DRS-YYMMDD-NN`; only the configured format is accepted for new tasks (old
  ids stay in history and are not rewritten).
- Release task: the human gives the version ("release v0.1.0"); the lead creates `release/v0.1.0`
  from dev, opens a PR to main (merge commit, the branch is kept), merges it, runs
  `gh release create v0.1.0 --target main --generate-notes`, and performs a back-merge: a PR
  main → dev merged with a merge commit so that dev does not lag behind.
- Hotfix: a normal task forked from main with the PR into main; after the merge — release and
  back-merge as for a release.
- Direct pushes to main/dev are forbidden.

**trunk** — task branches are forked from the default branch, PRs target it, merges are squashes,
releases go through `gh release create` after the merge. The PR base in the table above becomes the
default branch.

**Delivery chain**: `commit → push → PR → merge → release`. Every mutating step simultaneously
requires a sealed reviewer PASS tied to the delivered commit and the human's explicit approval
(`.pipeline/approvals/<task_id>.json`; the file is written only by the plugin).

- The lead creates the typed branch before development and pushes only it and only the verified
  commit (`delivered_commit`).
- The PR base is not invented: in simple-gitflow it is `dev` (feat/fix/chore) or `main`
  (hotfix/release); a wrong or missing `--base` is rejected by the plugin.
- Task PRs: `gh pr merge <n> --squash --delete-branch`; release and back-merge PRs:
  `gh pr merge <n> --merge`.
- `gh release create <tag>` — only after the release merge into main; the tag must match the task's
  `version`.
- Repository bootstrap (first setup): with the `bootstrap` approval — `git push -u origin main` and
  `git push -u origin dev` (creating branches only, no active task required). `gh repo create` keeps
  its separate `repo` approval.
- CI monitoring is read-only (`gh pr checks`, `gh run view/list`) without approval; `gh workflow run`
  and a mutating `gh api` are forbidden.
- Approvals: "I approve the commit / push / pr / merge / release / repo", "I approve the delivery"
  (= commit + push + PR), "I approve the repository setup" (bootstrap); the commands
  `/approve commit|push|pr|merge|release|repo|bootstrap|delivery`; revocation — "I revoke the commit",
  `/revoke <op>`. Negations ("don't commit") are not recognized: use the revocation.

## 15. Changing the rules

A rule is considered changed only after it is written to a file: protocol and matrix — in beach-team,
task settings — in state. A promise in chat does not count. After a change to the team configuration —
deployment and `check-gates`; process problems are recorded in `team/issues.md` (analysis — on the human's
command).
