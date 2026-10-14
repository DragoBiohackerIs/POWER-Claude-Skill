# POWER: executable plan (phase 1)

The full planner rules are in `power_prompts.py` (`PLAN_RULES`, `TASK_SHAPE`); here is what the
orchestrator checks when it reads PLAN.md/tasks.json and when `prepare` complains.

## Task fields

- `files`: closed list with `edit | create | delete`. Everything outside it is forbidden for the task by definition:
  read anywhere, write only to its own files. One file - one task; files that many tasks want
  (registries, manifests, shared CSS, bootstrap, translation catalogs) are their own task or come last via `depends_on`.
- Never in a plan: `.env*`, `_secrets/`, `wp-config.php`, keys, `vendor/`, `node_modules/`, lock files,
  `docs/power/`, plus `deny` from `## POWER` (`allow` opens exceptions such as `.env.example`).
- `anchors`: 1-3 literal excerpts for every file with edit or delete; `prepare` checks that they exist; the executor
  stops with `PLAN_MISMATCH` if one is missing.
- `contracts`: every interface between tasks (signatures, hooks, option keys, CSS classes, DB columns, i18n
  keys, routes), written exactly, with `provided_by`; tasks list them in `provides` and `uses`.
- `non_goals`: what does not change in meaning (behavior, markup, API, data).
- `acceptance`: verifiable without #THE_OPERATOR#. `checks`: the commands the script runs after the task (Git Bash from the
  project folder, `MSYS_NO_PATHCONV=1`): `name`, `cmd`, `expect_exit` (-1 = any), `contains`, `not_contains`,
  `before` (fail | pass | any - what it does BEFORE the work; "fail" for a check that proves the new behavior),
  `serial` (writes to a shared local DB/state), `timeout` (120 s by default; all checks of the task combined
  up to 540 s), `retries` (only for known flaky ones), `known_broken`. Never `/dev/null` as a program argument
  (under `MSYS_NO_PATHCONV=1` `curl -o /dev/null` gives exit 23): redirect in the shell or `-w '%{http_code}'`.
- `review_focus`: what the verifier looks at beyond the checks. `rules`: the project traps for this task, spelled out
  (executors do not see CLAUDE.md), including the environment (shell, encoding, container paths, local URLs,
  rate limit); those common to all tasks - once in the plan's `rules`.
- `size`: files and lines. `depends_on`: only when the task's checks need the other task's working code; otherwise
  parallel against the contract. A contract id in `depends_on` = "after the task that provides it".

## Size

One goal per task, up to ~6 files with real logic and ~300-400 changed lines (translations and generated data do not
count), each verifiable on its own; trivial ones are merged; up to ~10-12 tasks per run. **The same small change in N
files (translations, per-language catalogs, one line in N modules) is ONE task with all the files**, not a task per file:
each task costs 3 agents (7 translations as 7 tasks = 21 agents for 7 × 3 lines), while one verifier reads
eight 3-line diffs at once. `prepare` warns above 6 files only if the lines per file are many.

## Large plan (7+ tasks in 2+ independent areas)

The planner returns a skeleton (`skeleton.md`, `skeleton.json`: goal, scope, contracts with `provided_by` = the area
name, areas with exclusive files `owns`, `covers`, `est_tasks`) without tasks. The details in parallel:
`power_prompts.py write … --kinds areas` and one Agent `power-executor` of step 1 per area ("Read
<run>/prompts/area.<name>.md and follow it"), or `codex_task.py plan-area --area <name> --max-wait 5` for all,
then wait for each. Finally `power_check.py merge --run-dir <run>` (renumbers the ids, merges PLAN.md) and
`prepare`. An area with `mismatch` or a file outside the area returns the skeleton to step 1.

## What `prepare` checks

Fields, one owner per file, paths inside the project, forbidden files, edit/delete exist, create do not
exist, anchors, contracts (`provided_by` is a task, `provides`/`uses` are known), dependencies without cycles,
size, total check time, `/dev/null` as an argument. Writes the normalized `tasks.json` (the original in
`tasks.orig.json`). After the work `prepare` is expected to fail (anchors are consumed, create files exist).

## Baseline

`baseline` runs every check once before the work (needs only `tasks.json`, starts in parallel with Gate A):
PRE-BROKEN (`before: pass`, fails), WEAK (`before: fail`, passes - proves nothing), SUSPECT (the command itself
fails: curl tool error, shell syntax - fixed in the plan before the work). For each: a plan fix,
`"known_broken": true` with a note in PLAN.md, or a question to #THE_OPERATOR#.

## Agent budgets (`BUDGET` in `power_prompts.py`)

Recon 25 hard (ends with "Unmapped:"), plan ~20 (plans from the recon notes; missing = risk "RECON: …", not
independent mapping: a planner with 400k context costs $11-15), task ~25 with a stop at 40
(`PLAN_MISMATCH` "BUDGET: …"), fix 20/30, finish 30/45, verification 15, lens 15, final review 30/40,
final fix 30/40, final verification 20; read ranges around grep hits, never whole directories
or files over ~300 lines. Why: every call resends everything read so far - an agent with 120 calls and
500k context costs $11-17, as much as a whole run; an agent with 10 calls under $1. The types in `agents/` add a hard
`maxTurns` above the soft stop in the prompt.
