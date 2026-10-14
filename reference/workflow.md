# POWER: the loop of one task (phases 2-3)

The same loop runs in two places: `power_run.py continue` (the harness, default: a Python port with one thread per
task, `power_check.py check`/`restore` and Codex jobs as subprocesses, no runner agents) and `power_workflow.js` (the
Workflow of the in-session fallback, `reference/session-path.md`). Statuses, BLOCKED cases and the result shape are
the same in both, and since stage D of the v2 review (V2-14) the Workflow applies the harness's outcome rules where a
script can: a file the executor reports outside its list makes the task BLOCKED whatever the verifier said; the result
carries `scope_changes` (from the check) and `plan_changes` (every `plan_change` of work / fix / finish, append-only);
`mode: "verify"` MERGES the verdict into the task's earlier row of `results.json` (`previous`, attached by
`codex_task.py workflow-args --verify`): result, author, earlier verdicts, plan changes and reported stray files
survive, and a stray is released only when `tasks.json` now lists the file for the task (recorded as an orchestrator
plan change); a failed Codex job's runner is killed (`codex_task.py kill`) before any Claude fallback, and an
unconfirmed kill is a BLOCKED task, not a second writer. **The Workflow is still the weaker path**, declared, not
hidden: it has no run lock, no agent counter / `INCOMPLETE`, no snapshot-bytes check for a restored stray file (the
harness's `stray_resolution`), no real-author choice of the final reviewer, and nothing of it writes PLAN.md or
REPORT.md - the session does that from the result. The harness stays the default; the enforcement table in
`reference/harness.md` names the path of every rule. The text below describes the Workflow; read "runner" as
"subprocess" for the harness.

## The Workflow (`power_workflow.js`)

Called with `name: "power-execute"` (the copy in `~/.claude/workflows/` from `install`; the tool refuses `scriptPath`
to the skill folder) and `args` from `codex_task.py ready` or `workflow-args`:
`tasks` (id, depends_on, files), `run_dir`, `project_dir`, `step1`, `step2`, `codex_models`, `helpers`,
`agent_types` (the installed types), `mode` (`work` | `verify`). Claude agents receive only "Read
<run>/prompts/<id>.<kind>.md and follow it exactly" plus the dynamic part (round, problems, history, paths to the
diff and the report) - the script has no file access, so unlike the harness (PERF-04: the prompt text inline, the file
as the audit copy) it costs the agent one Read round; Codex tasks go through a runner that runs `codex_task.py`.

## The loop of one task

executor → `power_check.py check` (runner) → clean-context verifier → one fix by the executor →
checks → verifier → on the second return step 1 finishes (or the shifted model per SKILL §2) → checks →
verifier → otherwise BLOCKED.

- **No waves:** a task starts as soon as its dependencies are PASS; a BLOCKED dependency = `BLOCKED_DEP`, not worked.
- **Stop rule:** the executor checks the anchors, files and contracts before the first edit; on a
  mismatch it returns `PLAN_MISMATCH` (what the plan says, what the code has, file:line) and does not improvise. The
  task goes to step 1: the runner first restores the files to the snapshot (`restore`, copy in `<run>/stash/`), then
  step 1 fixes the plan for the task (`plan_change`) and finishes it. If the verifier says the plan is wrong
  (`plan_problem`), also to step 1, but without restore: the code stays as the base. If step 1 also cannot proceed
  without a foreign file or a decision by #THE_OPERATOR# - `PLAN_MISMATCH` with `QUESTION:` and the task is BLOCKED with the question.
- **Cheap check first:** the runner runs `check`: diff against the snapshot, the expected create/delete,
  the plan's checks against the baseline. Red goes straight to a fix with the output and the path to the full report,
  without the expensive model, and counts as a return. `CHECK_JSON` is short on purpose (reasons + 200 chars of output;
  the full output is in the check log). If a live run shows that haiku distorts it, `RUNNER` at the top of the script
  becomes `'sonnet'`.
- **The verifier** sees the task, the criteria, the contracts, non-goals, the diff and the check report, never
  the executor's summary. It may run checks (without `[serial]`) and read code; it does not touch files.
- **Failed infrastructure is not a failed task:** `INFRA:` from the script (no snapshot, broken plan), a dead
  runner or verifier (one retry each), a failed restore - BLOCKED with `reason: infrastructure: …`, nobody is sent
  to "fix" it. The orchestrator resolves it (the cause, then Workflow again only for those tasks).
- **A broken check is not broken code:** a failure that is an error of the command itself (curl exit 23/6/7,
  "curl: (N)", shell syntax) is marked by the script as `CHECK-BROKEN:`; if all failures of the round are such -
  BLOCKED with `reason: check broken: …` without a fix, without restore, without escalation; the code stays. Same for
  `PLAN_MISMATCH` with mismatch `CHECK: …` from executor/fix/finish (the rule is in their prompt). The orchestrator
  fixes the command in `tasks.json` (a line in "Plan changes"), then
  `codex_task.py workflow-args … --verify <id>:<author>:<round>` → Workflow with `mode: "verify"`: only `check` + verifier
  for those tasks; the verdict is merged into the task's earlier row (`previous`), and the session copies the merged
  row back into `results.json`.
- **Foreign files:** a file outside the list that the agent itself reports (`files_changed`) goes into `stray_files`
  and the log and makes the task BLOCKED whatever the verifier said (P1-04, as in the harness); it is not fixed
  automatically (the file may belong to another task and have been touched legitimately since), it goes into the
  final review and the report. An unacknowledged change is visible only in the owner's diff or
  in "changed outside the run's files" of the report (the project's claim file is excluded from there).
- **After a crash, do not resume the whole run:** a relaunch reruns every agent that started after the first
  failed one, even finished ones. Run Workflow again only for the BLOCKED tasks.
- **Escalation with the same model:** if step 2 is also step 1, the other strong model finishes (Opus ↔ Fable,
  Astra → Opus); it is verified by "not the author". Codex failure: the job's runner is killed first (`codexKill`, a
  runner agent with `codex_task.py kill`; KILLED / NOT_RUNNING / NONE), then the fallback per SKILL §2, `log()` says
  so; a kill that is not confirmed = BLOCKED `infrastructure: …`, no fallback.

## Result

For each task: `status` (PASS | BLOCKED | BLOCKED_DEP), `author`, `verified_by` (list per round; `checks` =
stopped by the script check), `rounds`, `escalated`, `reason`, `result` (status, files_changed, summary,
how_verified, open_issues, plan_change), `last_problems`, `notes`, `stray_files`, `scope_changes` (files changed
outside every list that only the diff shows - SUSPECT for the orchestrator), `plan_changes` ([{kind, by, text}],
reported is not approved), `check` (diff_path, report_path, warnings); in verify mode also `stray_resolved` and
`verify_only_round`. The tool returns a file with key `result`; it is copied to `<run>/results.json` and read with
`power_check.py results --run-dir <run>`. With `mode: "verify"` the result has the same shape, only for the passed tasks.

## Budget

3 agents per task (work, checks, verifier), up to 10 on escalation (with restore). Guideline up to 50 agents per
Workflow; if the plan exceeds it - say so at Gate A and in the summary, do not cut silently. Measurement after the run:
`power_cost.py` (startup weight, calls, input by role).
