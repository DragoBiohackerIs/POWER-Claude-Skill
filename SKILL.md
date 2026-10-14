---
name: power
description: POWER mode for a large task (plan with anchors and contracts, parallel executors, script checks, a verifier different from the author, gated deploy). Use when the user's message starts with POWER, POWER START, POWER DEPLOY or POWER START DEPLOY.
---

# POWER mode

Protocol for a large task with maximum parallel work and minimum intervention from #THE_OPERATOR#. One skill for all projects
under `d:\Claude\`; project specifics come from `## POWER` in the project's `CLAUDE.md`. This file is the core (read it
whole); the details are in `reference/` and are read only in the phase they belong to:

All messages to #THE_OPERATOR# are in Bulgarian; file contents, plans and reports follow the project rules.

| File | When to read it |
|--|--|
| `reference/harness.md` | every run: `power_run.py` commands, its files, resume after a crash, environment knobs; the table "What enforces what" (which rule is code, prompt or external) |
| `reference/plan.md` | Gate A: what an executable plan is, a large plan by areas, the agent budgets |
| `reference/workflow.md` | the loop of one task (statuses, BLOCKED cases, verify-only) when a result needs explaining |
| `reference/deploy.md` | phase 6: `power_deploy.py` step by step, rollback, hard stops |
| `reference/gates.md` | Gate A/B: what the messages hold and the `/compact` hint |
| `reference/codex-config.md` | on a Codex step; on a new project: the `## POWER` json block |
| `reference/setup.md` | new machine or `install --check` reports missing settings; sharing the skill |
| `reference/session-path.md` | the fallback: the whole protocol run from the session (Workflow), when the harness cannot run |

`<skill>` = the folder of this file. Python always with `PYTHONUTF8=1`, Bash with timeout 600000, everywhere
`--run-dir <run> --project-dir <project>`. Scripts: `power_run.py` (**the harness**: phases 0-6 from code, outside the
session), `power_check.py` (plan, snapshot, baseline, check, restore, results, config, install), `power_prompts.py`
(the single source of agent instructions), `codex_task.py` (Codex jobs; `models`, `usage`, `status`),
`power_workflow.js` (the in-session Workflow of the fallback), `power_deploy.py`, `power_cost.py`, `agents/*.md`.
First run on a machine: `python "<skill>/power_check.py" install`, then the settings in `reference/setup.md`.

Markers in this file: **[code]** = a script refuses, stops or records it (covered by `tests/`); **[prompt]** = the
agents are told so in their instructions, a model can ignore it, the harness checks the result where it can;
**[external]** = the session or #THE_OPERATOR# does it, the scripts only make it visible. The full table is in
`reference/harness.md` → "What enforces what".

## 1. Trigger, flags, session model

The standalone word `POWER` in capitals at the start of the message (not POWERCasino, NADPower). Four forms,
no model names in the command; `START` and `DEPLOY` only in capitals, so that the word "deploy" in the task text
does not trigger them:

| Message | Plan approval (Gate A) | Deploy confirmation (Gate B) |
|--|--|--|
| `POWER <task>` | yes | yes |
| `POWER START <task>` | no | yes |
| `POWER DEPLOY <task>` | yes | no |
| `POWER START DEPLOY <task>` | no | no |

**The session is thin.** Every agent of the run is a headless `claude -p --agent power-* --model …` process started by
`power_run.py` (7-8k start context, no CLAUDE.md, no memory); script checks and Codex jobs are plain subprocesses. The
session only takes the request, starts the harness in the background, relays `harness/gate_a.md` and `harness/gate_b.md`
to #THE_OPERATOR#, and runs the deploy command after their answer. Measured before the harness: the orchestrating session was
32-52% of a run's cost; now it is a few calls, so the session may stay on the model it is on (Opus is still the
recommendation for the judgement at the gates). Nothing is planned, reviewed or summarized in the session itself.
Only the fallback of `reference/session-path.md` (the Workflow run from the session) needs `/model opus` first.

## 2. Models: three steps

| Step | Role | Options, first is default |
|--|--|--|
| 1 | plan, verification of every task, finishing on escalation, the fixes from the final review | Opus · Fable · Astra |
| 2 | execution and one fix | Sonnet · Haiku · Opus · Sol · Luna · Astra |
| 3 | final review, DEPLOY.md + `deploy.json`, review of the merge with prod | Opus · Fable · Astra |

- `opus`, `sonnet`, `haiku`, `fable` point to the latest version. Astra (frontier), Sol (workhorse) and Luna (cheap)
  are Codex families (#THE_OPERATOR#'s ChatGPT plan, outside the Claude limit); `codex_task.py models` gives the slugs, never
  write them by hand. No choice: recon and lenses - Sonnet `effort low`; prod verification - Sonnet.
- **Agent types** (`agents/`, passed as `--agent` by the harness): `power-reader` (recon, lenses), `power-verifier`
  (verifier, final verification), `power-executor` (plan, execution, fixes, finishing, final review); `power-runner`
  only in the fallback. `omitClaudeMd` + a hard `maxTurns`; project rules reach the agents via `rules` in the plan. The
  harness adds `--max-budget-usd` per role (`ROLES` in `power_run.py`) and `--permission-mode auto`.
- **Who verifies whom.** Code is never verified by its author: step 1 verifies; if step 1 is the author -
  Opus; if Opus is also the author - Fable. If step 2 is the same model as step 1, escalation goes to the other strong
  model (Opus → Fable, Fable → Opus, Astra → Opus); if step 2 is the same as step 3, the final shifts by the same
  table. "Author" is always the model that REALLY wrote the code as `results.json` records it (after a Codex
  fallback, after an escalation), not the configured step 2, and a task has as many authors as models whose edits
  are in its files (`authors`: work, fix and finish accumulate; a restore to the snapshot drops the earlier ones; a
  final fixer counts too) - Opus writing the code and Fable finishing the task leaves Opus an author, never an
  "independent" reviewer of its own code (V3-02). The escalation model, its fallback and the final reviewer are
  chosen from that union; when both Opus and Fable wrote retained code and no Codex model is in the run, the final
  review is recorded as not independent and is a hard stop. Automatic, the echo line states it, #THE_OPERATOR# can override
  it with one word.

| 1 / 2 / 3 | Task verification | Escalation finishes | Verification after escalation | Final |
|--|--|--|--|--|
| Opus / Sonnet / Opus | Opus | Opus | Fable | Opus |
| Fable / Opus / Fable | Fable | Fable | Opus | Fable |
| Opus / Astra / Fable | Opus | Opus | Fable | Fable |
| Astra / Sol / Opus | Astra | Astra | Opus | Opus |
| Opus / Opus / Opus | Fable | Fable | Opus | Fable |

- **If Codex fails** (limit, crash, timeout): Sol → Sonnet, Luna → Haiku, Astra → Opus; verifier Astra → Opus (Fable if Opus
  is the author). The harness logs it, the result carries `author` and `verified_by`; many failures at once = limit used up.
- **Call cap** in every prompt (`BUDGET` in `power_prompts.py`): recon 25 hard, plan ~20, task ~25 with stop at 40
  (`PLAN_MISMATCH` „BUDGET: …"), fix 20/30, finishing 30/45, verification 15, lens 15, final review 30/40. Cost grows
  with the square of the agent's length; a task that hits the cap was sized wrong in the plan: escalation finishes it
  once, on a second hit it is BLOCKED and the plan splits it.

## 3. Phase 0: intake (the session)

1. **Clarity and size, before anything.** **Unclear request or more than one reasonable interpretation - stop and
   ask before planning**, also with `START`: ten agents on a misunderstanding are the most expensive mistake.
   If the request would be ~3 tasks or fewer in one area (a few files, one module, texts) - say so in one sentence:
   POWER costs 8-10 agents fixed (recon, plan, lenses, review) and for such a request a normal session +
   `php-reviewer` (or the project reviewer) before deploy is 5-7× cheaper. #THE_OPERATOR# decides; with `START`/`DEPLOY` it
   is also said, but does not stop.
2. **The menu.** `python "<skill>/codex_task.py" models`, then one AskUserQuestion with three questions (for every form):
   „Кой планира и проверява парчетата?" (header „План": Opus (Recommended) · Fable · Astra), „Кой изпълнява
   парчетата?" („Изпълнение": Sonnet (Recommended) · Haiku · Sol · Luna; Opus and Astra via „Other", the menu
   allows 4 options), „Кой прави финалния преглед и плана за
   деплой?" („Финал": Opus (Recommended) · Fable · Astra). Next to Astra/Sol - the exact slug and `description` from
   `models` + „ChatGPT плана, без Docker и мрежа"; Opus: най-доверен; Fable: най-силен, най-скъп; Sonnet: бърз и
   евтин, планът е изпълним, а проверката е от друг модел; Haiku и Luna: най-евтини, за механични парчета
   (преводи, текстове, еднакви промени в N файла), не за логика. Warning from `models` or a missing family - say so and
   remove the option.
3. **Run folder and request.** `<run>` = `<project>/docs/power/<YYYY-MM-DD>-<slug>/`, `<run>/request.md` (the task
   verbatim; the answers from item 1 and every later correction from #THE_OPERATOR# go into its section „Решения на #THE_OPERATOR#":
   the planner reads the file, and after `/compact` the session reads it instead of remembering). Read the project's
   `CLAUDE.md`, `power_check.py config --project-dir <project>` (short; `--full` for the json) and the deploy docs only
   as far as the recon foci and the hooks need: the agents read the rest.
4. **Start the harness** in the background (Bash `run_in_background`, timeout 600000; it may run for an hour):

       PYTHONUTF8=1 python "<skill>/power_run.py" start --run-dir <run> --project-dir <project> --step1 <m> --step2 <m> --step3 <m> [--flags START,DEPLOY] --recon "<name>=<what to map>" [--recon …] [--lens "<name>=<focus>"] --title "<slug>"

   One `--recon` per focus (1-4; one for a small task); without `--lens` the lenses come from `## POWER`, else
   `domain` + `smoke`. The harness runs recon → plan (step 1) → `prepare` (one planner retry on errors) → `baseline` →
   writes `<run>/harness/gate_a.md` and stops; with `START` it goes on by itself, except on `QUESTION:` risks,
   PRE-BROKEN or SUSPECT baseline checks (they stop even with `START`, the file says why).
5. **Echo line** after the menu (the harness prints the same one at the top of every file): `POWER START · план Opus ·
   изпълнение Sonnet · финал Opus · одобрение на плана: не · потвърждение на деплоя: да`, with the shifts per §2
   („ескалация Fable (автоматично: Opus е и изпълнител)").
6. CLAIM, if the project has one (`claim` in `## POWER`; for example `.project/SESSIONS.md`); the files are added
   from „Файлове за CLAIM" in `gate_a.md`. The CLAIM line is the only project file the session writes.
7. While the harness runs: `power_run.py status --run-dir <run>` (phase, tasks, running agents, cost, next step; also
   `<run>/harness/status.json` and `log.txt`); say where it is about every 15 minutes, never ask „до къде си".

## 4. Phase 1: plan and Gate A

1. When the harness stops at `gate_a` (the background task ends, or `status` says so): read `<run>/harness/gate_a.md`
   and relay it to #THE_OPERATOR# as it is (table of tasks, contracts, non-goals, risks, QUESTION risks, baseline decisions,
   expected agents, the CLAIM files, the closing „#THE_OPERATOR#, трябва да решиш …"). What an executable plan is and why,
   and what `prepare` and `baseline` complain about: `reference/plan.md`. A plan above 50 agents: say so.
2. **Gate A** (only without `START`): wait for „да". Changes by #THE_OPERATOR#: edit `tasks.json` by hand, or put the
   answers in `request.md` („Решения на #THE_OPERATOR#") and run `continue --replan` (the planner again, then
   `prepare` → `baseline` → a new `gate_a.md`). Above 150k context: the `/compact` hint from `reference/gates.md`.
3. On „да": `PYTHONUTF8=1 python "<skill>/power_run.py" continue --run-dir <run> --project-dir <project>` in the
   background. It runs `ready` (prepare → snapshot → prompts → args, the snapshot as fresh as possible), the work loop,
   the final review and the report, and stops at `gate_b`.

## 5. Phases 2-3: work and verification (inside `continue`)

The port of `power_workflow.js`, one thread per task, `POWER_MAX_PARALLEL` (6) agent processes at once. No waves:
a task starts as soon as its dependencies are PASS; per task: executor → `power_check.py check` (a subprocess, no
runner agent) → verifier with clean context (the task, the contracts, the diff and the report, not the author's
summary) → one fix → checks → verifier → on the second return step 1 finishes → checks → verifier → otherwise
BLOCKED. Stop rule: anchors/files/contracts are checked before the first edit, mismatch = `PLAN_MISMATCH` (restore
to the snapshot, step 1 fixes the plan and finishes). A red script check goes to a fix without the expensive model.
`INFRA:` and `CHECK-BROKEN:` (broken check command, not code) = BLOCKED without a fix; the orchestrator fixes the
command in `tasks.json` (a line in „Plan changes" of PLAN.md) and runs only check + verifier:
`power_run.py verify --tasks <id>:<author>:<round>`, then `continue`. The verdict is merged into the task's earlier
outcome: a reported file outside the list keeps the task BLOCKED until it is back at the snapshot or `tasks.json`
lists it for the task (both recorded). Details and statuses: `reference/workflow.md`.

Results go to `<run>/results.json` as each task finishes (`power_check.py results` prints the table); every
`plan_change` any agent reports (work, fix, finish, finalfix) is kept in an append-only history → section „Plan
changes" of PLAN.md and the report, with who reported it; reported is not approved. Budget: 3 agents per task, up to 10 on escalation, guideline up to
50 per run. **After a crash** (`continue` again): finished tasks keep their result, an interrupted task is restored to
the snapshot (its half edit kept in `stash/`) and redone, nothing else is repeated.

## 6. Phase 4: final review (inside `continue`)

1. `check --all --round final` first (the whole diff in `<run>/checks/final/`). **A failed final check ends the review
   right there** (PERF-02): status `FAIL` with the failure as the reason, no lens and no reviewer runs (a red check, a
   failed smoke or INFRA cannot become CLEAN whatever they say); fix the check or the code and `continue`;
   `continue --diagnose` runs the full review on the failing tree when the diagnosis itself is wanted. When it passes,
   the lenses run in parallel - they read its diff and report (`power-reader`,
   Sonnet, `effort low`; a lens with `agent_type` in `## POWER`, e.g. `php-reviewer`, runs as that type). They write
   `<run>/review/<name>.md`.
2. The final reviewer (step 3, shifted per §2 when it is also the executor; Astra via `codex_task.py review`)
   confirms every finding against the code and writes `REVIEW.md`, `DEPLOY.md`, `deploy.json`, `review.json`;
   `power_deploy.py plan` validates `deploy.json` right away.
3. Critical/high/medium findings: step 1 fixes (`finalfix`, all files of the run, nothing else) → `check --all --round
   final2` → the „not the author" verifier (`finalverify`). Still failing - BLOCKED in the report (`final.status`).
   Low - in the report.

## 7. Phase 5: summary and Gate B

The harness writes `<run>/REPORT.md` (the echo line and models; per task status, author, verifier, rounds, escalation,
`plan_change`, open issues, stray files; the changed files with absolute links and `checks/final/diff.patch`; the
baseline decisions and „changed outside the run's files"; the final review, fixes and verification; BLOCKED and the
questions; Codex `status`, and the `usage` snapshots before/after only with `--refresh-quota` on `start` / `continue`
(two real `codex exec`, PERF-03; otherwise the report says they were not fetched); the measured cost: the `HARNESS:`
lines by role, from `harness/calls.jsonl`; the deploy plan, DB steps, `needs_yes`, risks; the hard stops) and
`<run>/harness/gate_b.md`.
Relay `gate_b.md` to #THE_OPERATOR#; REPORT.md is linked, not pasted.

**Partial deploy** (there is a BLOCKED): `gate_b.md` already asks it explicitly („Качвам t1, t2, t4-t6 без t3?"); the
PASS tasks with no dependency on BLOCKED (`depends_on` and contracts). Before it: `restore --task <id> --keep blocked`
for BLOCKED, `check --tasks <PASS> --round partial` + local smoke, step 3 reviews the reduced diff (an Agent
`power-executor` with the review prompt, or the harness again on a reduced `tasks.json` in a new run).

**Gate B** (only without `DEPLOY`): wait for #THE_OPERATOR#'s answer; before it the `/compact` hint per `reference/gates.md`
if the context is above 150k.

## 8. Phase 6: deploy

Step 3 planned it (`DEPLOY.md`, `deploy.json`), `power_deploy.py` uploads, the orchestrator decides. Without a deploy
profile in `## POWER` - `DEPLOY` is ignored, ask, deploy is manual per the project docs, still only by the
orchestrator. **Only the orchestrator mutates prod, serially**, never an agent.

1. `power_run.py deploy --run-dir <run> --project-dir <project>` = `power_deploy.py plan` → `fetch` (prod-before = backup;
   clean/same/new/merged/conflict/merge-error; step 3 reviews every merged diff) → the hard stops it can see, printed
   as `HARD STOP:` lines → the exact `deploy --go --approved <attempt:hash>` command for **this candidate** (the
   `CANDIDATE` token of the fetch: file bytes, backups, `deploy.json` and the effective profile). Show them; a merged
   diff goes to step 3 (Agent `power-executor`, step 3 model) before `--go` [external]. A conflict is resolved by hand
   in `deploy/upload/<file>`, then `power_deploy.py seal` makes the resolution a new candidate with its own token
   (review, yes); `deploy` keeps a sealed candidate instead of fetching it away [code].
2. `deploy --go --approved <token> [--claimed-by "<the exact CLAIM topic>"]` only after Gate B or with `DEPLOY` and no
   hard stop. The token is #THE_OPERATOR#'s yes made concrete: `--go` and `--yes` are refused without it (`--force` has an effect
   only with `--go`, and then needs it too), and a token
   of another candidate (prod changed since they saw it, `deploy.json` edited, a new attempt) is refused before anything
   is recorded or uploaded - show the new candidate and ask again [code]. With a hard stop it refuses until #THE_OPERATOR# says
   yes to each (then `--go --force --approved <token>`) [code]. It runs `apply --go --approved` (canary before, upload
   in order, byte-verify, the `after` hooks - required unless `{"cmd", "optional": true}`: a failed required hook is
   `AFTER_FAILED`, the files stay on prod and the report names it for manual recovery, an optional one is a warning;
   settle, canary after; automatic rollback only on a certain failure, and a failed required recovery hook makes it
   `ROLLED_BACK_AFTER_FAILED`; the rollback after a failed put / byte verify runs the hooks as recovery too -
   `FAILED_ROLLED_BACK`, or `FAILED_ROLLED_BACK_AFTER_FAILED`, V3-04) and, when
   `deploy.json` has no DB steps, `watch --go` [code]. `watch` is HEALTHY only when every canary answers now: a
   canary down now that was fine before the upload rolls back; one that was already down before (`--force-canary`)
   is `DEGRADED`, exit 2, no automatic rollback - the orchestrator decides (V3-03). The run is done only when that
   watch exits 0 with `HEALTHY`; a rollback, `DEGRADED` or an unreadable watch is exit 2, phase `deploy_failed`,
   `HARD STOP:` + `HARNESS DEPLOY: <outcome>` [code]; with DB steps the phase is `awaiting_watch` until step 3 below
   ends HEALTHY [code].
3. The `apply` of a DB/option/flag step is never run by a script [code: hard stop]: after an explicit „да" the
   orchestrator runs it (token wrapper per the project docs) [external], then `power_deploy.py watch --go --db-rollback`
   [code] - the one script that runs DB commands: on a crash it runs the `rollback` of every DB step and reports their
   exit codes in `db_rollbacks`, separately from the file rollback status (a verified file restore does not prove the
   DB restore).
4. `prod-verifier` (Sonnet, read-only, the project's pace) against the local screenshots → „#THE_OPERATOR#, всичко е изпълнено и
   проверено - …" with a link to `deploy/REPORT.md` [external]. Details, statuses and rollback: `reference/deploy.md`.

**Hard stops that beat `DEPLOY`** (summary + question): BLOCKED or INCOMPLETE task (only the partial deploy
question) [code]; an unclosed issue from the final review, a failed or missing final check, SUSPECT files [code];
failed local smoke [code, via `check --all`]; any DB/option/flag step [code: stop; external: running it]; change to
orders or customers [external: the orchestrator's judgement]; `.env`, `wp-config.php`, secrets in `deploy.json`
[code]; an unanswered `needs_yes` or a yes for another candidate [code]; `conflict`, `diverged-no-base` or
`merge-error` at fetch, until sealed and approved under its own token [code]; binary without backup [code]; canary
failed already before the upload [code]; the candidate, `deploy.json`, the deploy profile or prod (any row, unknown
state included) changed since the fetch, a backup that lost its digest [code]; automatic rollback and any rollback
that is not `ROLLED_BACK` [code]; a failed required `after` hook (`AFTER_FAILED`: the files are on prod, the hook needs
manual recovery or a rollback) [code].

## 9. Phase 7: commit (default, no flag)

Every run ends with **one git commit by the session**, never by an agent, after the run is done: after Gate B when
there is no deploy, after `watch` ends `HEALTHY` when there is one. No flag turns it on; it is skipped only when
#THE_OPERATOR# says „без комит" in the task line or the project has no git repo at the run's `project_dir`.

1. `git -C <repo> add` **only the files of the plan's closed list** (plus `docs/power/<run>/`); never `-A`, never a
   file outside the list, never a file another CLAIM holds (the deploy's `--claimed-by` rule applies here too).
2. One commit per repo touched; message: first line `power(<slug>): <one line from REPORT.md>`, body = the task
   statuses and the deploy outcome, last line the attribution from the session's reminder.
3. Never `push`, never `stash`/`checkout`/`reset`/`rebase`, never with a BLOCKED or INCOMPLETE task (a partial
   deploy commits only the deployed PASS tasks and says so in the message), never with a `conflict` left in the
   working tree. A refused or failed commit is reported as `HARNESS COMMIT: <reason>`, not retried silently.
4. The commit hash goes into `REPORT.md` and the SESSIONS.md hand-off.

## 10. Never in POWER

- An agent does not touch prod [prompt + orchestration: prod is written only by `power_deploy.py` from the session; the
  candidate token is an integrity guard, not actor authorization - an agent with Bash on this workstation could read
  it, so the rule is policy, see `reference/harness.md` → Trust boundary], `.env` and
  secrets [code at plan time and at deploy, prompt during the work], other people's files [code: a file outside the
  task's list is BLOCKED when reported, SUSPECT when only the diff shows it], git history (commit, stash, checkout,
  reset) [prompt only] - the one commit at the end is the session's, Phase 7, never an agent's.
- The plan is not changed silently: every reported change of every call (work, fix, finish, finalfix) is in the
  append-only history, in "Plan changes" and in the report; a later empty field erases nothing [code].
- A verify-only pass never clears a reported file outside the list: the verdict is merged into the earlier outcome,
  the task stays BLOCKED until the file is back at the snapshot or the plan lists it (recorded) [code].
- The verifier does not get the executor's summary [code: the prompt is built from the task, contracts, diff and
  check report]; a check has not "passed" without a report from `power_check.py` [code].
- Code is not verified or finally reviewed by the model that wrote it, by the REAL authors in `results.json` [code:
  `verifier_for`, `escalate_model(author)`, `review_choice`; no independent strong model left = hard stop].
- No deploy with an unanswered question, even with `START DEPLOY` [code: `needs_yes` → `--yes N --approved`], and no
  deploy of a candidate #THE_OPERATOR# did not see [code: `--approved <attempt:hash>`].
- No agent without a call cap or without a type from `agents/` [code for Claude agents: preflight, `--max-turns`,
  budget; Codex jobs: sandbox, schema, `JOB_TIMEOUT` - their tool-call budget is prompt only; the cap inside the
  prompt is prompt]; no agent at all without the executables (`start` preflights `claude`, `git`, Git Bash and the
  helpers before any state) [code]; no planning, review or report writing in the session itself; no reading of whole modules;
  no Agent tool while the harness is running (it would duplicate the work) [external].
- Everything independent runs in parallel (recon agents, the tasks, the lenses after `check --all`); sequential only
  for a shared resource: the prod upload (order + clean rollback), requests to prod (WAF pace), checks with
  `serial: true` (lock), Codex up to 6 processes [code].
- The harness is not resumed by restarting `start` (it refuses while a state exists; `start --force` wipes the state,
  not the files): `continue` picks up where it stopped [code]. One harness process per run (`run.lock`), and
  `POWER_MAX_AGENTS` stops a run that grows past the cap with INCOMPLETE, never with a fake PASS [code].
