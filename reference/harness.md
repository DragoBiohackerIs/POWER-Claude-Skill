# POWER: the harness (`power_run.py`)

Phases 0-6 driven from code, outside the session. Every agent is one headless process:

    claude -p --output-format json --no-session-persistence --model <m> --max-turns <n> --permission-mode auto
           --agent power-<type> [--effort low] [--max-budget-usd <x>]        (the prompt comes on stdin)

Start context 7-8k (the agent types have `omitClaudeMd`; measured 13.10.2026), no session file, no subagents.
Structured output (`--json-schema`) does not fire for agent types with a restricted `tools` list, so the harness asks
for one JSON object as the final message and parses it (fenced or bare); a result without a usable object counts as a
dead agent (one retry for verifiers, as in the Workflow). The run folder and the prompts are the same as in the
in-session path: `power_prompts.py write` renders them to `<run>/prompts/*.md`. The harness passes the rendered text in
the agent's message itself (PERF-04: one tool round and one re-sent context less per agent; the file stays as the
audit copy and the message names it); the in-session Workflow still says "Read <file>", as its script has no file
access.

## Commands

| Command | What it does | Stops at |
|--|--|--|
| `start --run-dir R --project-dir P --request-file F --step1 --step2 --step3 [--flags START,DEPLOY] [--recon n=focus …] [--lens n=focus …] [--title T] [--refresh-quota]` | preflight before any state, agent or edit (the agent types; `claude` = `POWER_CLAUDE` or PATH, `git`, Git Bash, the helper scripts, a valid `## POWER` json block (V3-06), and with a Codex model the codex executable + the login `auth.json` under `CODEX_HOME` without a paid probe (V3 readiness): `PREFLIGHT FAILED` names the fix, V2-16), writes `request.md` (if missing), recon agents in parallel, the planner, `prepare` (one planner retry with the errors), `RECON:` risks → one more explorer and a revised plan, `baseline`, `harness/gate_a.md`; two planners without a plan (dead, or no PLAN.md + tasks.json) = phase `plan_failed`, exit 2, a gate_a.md that names the cause and `continue --replan`, no traceback | Gate A; with `START` it continues, except on `QUESTION:` risks, PRE-BROKEN or SUSPECT checks |
| `continue [--replan] [--diagnose] [--refresh-quota]` | from `gate_a`: `ready` → the work loop → the final review → `REPORT.md` + `gate_b.md`; from any interrupted phase: resumes it; `--replan`: the planner again after answers in `request.md`; `--diagnose`: the lenses and the reviewer run even when `check --all final` fails (default: `FAIL` right away, PERF-02; from `gate_b` it is a new review generation); `--refresh-quota`: `codex_task.py usage` before the work and after the review (PERF-03) | Gate B |
| `verify --tasks id:author:round[,…]` | only `check` + verifier for the named tasks (after a fixed check command); merged into `results.json`; every `BLOCKED_DEP` task whose dependencies now PASS is released (the chain too) and the phase goes back to `work`, so `continue` runs them without touching the PASS tasks | (then `continue`) |
| `status [--run-dir R]` | phase, recon/prepare/baseline, per task status, running agents, cost, the next step | - |
| `cost` | the `HARNESS:` lines by role (also in REPORT.md) | - |
| `deploy [--go] [--force] [--yes N …] [--approved A:H] [--claimed-by T]` | `power_deploy.py plan` → `fetch` → `HARD STOP:` lines → the `deploy --go --approved <token>` command for this candidate; `--go`, `--force` and `--yes` need `--approved <attempt:hash>` = the `CANDIDATE` token of the fetch (a token of another candidate is refused before anything is recorded or uploaded); `--go`: `apply --go --approved` (+ `watch --go` when there are no DB steps); an apply that is not `DEPLOYED` (`AFTER_FAILED` = a required `after` hook failed and the files are on prod, `DEPLOYED_UNVERIFIED`, a stop before the upload) is exit 2, phase `deploy_failed`, `HARD STOP:` + `HARNESS DEPLOY: <status>` and no watch (V2-12); the run is `done` only when that watch exits 0 with `HEALTHY` - a rollback, an incomplete rollback, a timeout or unreadable watch output ends with exit 2, phase `deploy_failed`, a `HARD STOP:` line and `HARNESS DEPLOY: <outcome>` (V2-05); with DB steps the phase is `awaiting_watch` until the session's `watch --go --db-rollback` is HEALTHY; refuses on hard stops unless `--force` after #THE_OPERATOR#'s yes; a `needs_yes` item is answered only by `--yes N --approved` (`--force` does not cover it; the answer is bound to the candidate and is stale after a fetch that gives another one); after an apply that wrote to prod it prints `ALREADY_APPLIED` and never fetches again (the backup stays) | after fetch, or after apply |

`start` refuses while `harness/state.json` exists (`--force` wipes the state only), and refuses before any agent runs
when the agent types `power-reader`, `power-executor`, `power-verifier` are not installed (project `.claude/agents`
or `~/.claude/agents`; `power_check.py install`): without them every agent would run with the full CLAUDE.md context
and no turn cap. `continue` after a crash: finished tasks keep their outcome; a task with a `stage` but no `status` is
restored to the snapshot (the half edit goes to `<run>/stash/<id>.<n>/`) and redone; `ready` is not repeated once the
snapshot exists.

## One process per run, the agent cap, killed Codex runners (5.1-5.3)

`start`, `continue`, `verify` and `deploy` hold `<run>/harness/run.lock` for their whole life; `status` and `cost`
do not. Ownership is an **OS lock on an open handle** (`msvcrt.locking` of one byte far past the record on Windows,
`fcntl.flock` elsewhere; V2-07), not the file's existence or content: it is taken without waiting, lives exactly as
long as the process, and is released by the OS when the process ends, crashes or is killed - nothing to clean up, and
no manual removal is ever needed or useful. The record inside the file (`{pid, started_at, command, host}`) is
written *after* the lock is taken and serves `status` and the refusal text only; an empty or unreadable file with
the lock held is a holder mid-publication, never a "stale" lock. A second process on the same run exits at once
with the holder's pid and command (two harnesses on one run would start the same agents twice and overwrite each
other's `state.json`); a record of a dead holder is taken over with one line in `log.txt`. The state (`state.json`)
is read again under the lock, so the new holder continues from what the previous one wrote last. Test lockatomic:
six processes race for one run and exactly one holds it; the holder is killed and the next command takes the lock
with no cleanup; an empty but held lock file refuses.

`POWER_MAX_AGENTS` (default 0 = no cap) is a cap on the agents one run may start, counted in `state.json` →
`agents_started` (every headless `claude` and every Codex job, taken under the run's lock before the process starts;
`status.json` shows `agents_started` / `max_agents`). At the cap no agent starts: in the work phase the task ends as
`INCOMPLETE` (not PASS, not BLOCKED; its `stage` is kept, so `continue` restores the task's files to the snapshot and
redoes it), a task whose dependency is INCOMPLETE is INCOMPLETE too (not BLOCKED_DEP), the phase stays `work`, and
`harness/gate_b.md` gets the stop text (agents started, the cap, the INCOMPLETE tasks, how to go on). In the final
review the `final` block ends as `INCOMPLETE` and the phase stays `review`. Before the work phase (recon, plan) the
command exits 3 with `AGENT CAP:` and the phase is kept. INCOMPLETE tasks and an INCOMPLETE final review are hard
stops. The counter never resets: raise the cap (or set 0) and `continue`.

A Codex job that fails (or is abandoned after `POWER_AGENT_TIMEOUT`) is killed before the Claude fallback takes the
role over: `codex_task.py kill --kind K <job args>` reads the runner's pid from `<run>/codex/<job>.state.json`,
ends the process tree (`taskkill /T /F` on Windows, SIGTERM then SIGKILL elsewhere) and writes `killed`, `pid`,
`killed_at` and status `KILLED` into `<job>.done.json`, so the runner can neither keep editing the files under the
fallback nor finish later and be picked up by a re-attach. `log.txt` says `Codex runner killed (pid N)`, `already
gone` or `never started`. **A kill that does not confirm the runner is gone** (`KILL: FAILED`, a denied or timed-out
kill command, unreadable output) **means no fallback** (V2-06A): `Run.call` returns nobody, the role ends as died /
BLOCKED with the reason (`no verifier: the Codex job failed and its runner could not be killed`), `log.txt` says
`no fallback - ... could not be killed ... end it by hand, then continue`. A Claude agent editing under a runner
that may still write is worse than a blocked task. Platform: the process-tree kill is `taskkill /T /F` and is
verified on Windows only (test codexkill, a real sleeper); on POSIX `kill_tree` signals the pid alone, a child of
the runner can survive, and the runner's `creationflags` are Windows-only - the harness is **Windows-only** until
stage D ports it (declared limitation, not covered by a test).

Every entry into the final review is a **new review generation** (V2-04): the previous `final` block (its
`check_final` / `check_final2`, review verdict, fixer and verifier evidence) moves to `state.json` →
`final_history`, the previous `review.json`, `REVIEW.md`, `DEPLOY.md` and `review/` lens reports move to
`<run>/archive/final.g<N>/` (with a copy of `deploy.json`), and the Codex jobs `review`, `finalfix`, `finalverify`
are named with the generation (`review.g2`), so a job finished in an older generation is never re-attached to. The
verdict counts only when this generation's reviewer finished (`review.json` of a dead reviewer = `MISSING`), the
`CLEAN` status requires this generation's `check --all final` to pass (else `FAIL` with the first failure as the
reason), and `hard_stops` / `latest_check` see only the current generation. Test reviewgen: a passing final2 of
generation 1 does not cover a failing check of generation 2; a reviewer that dies in generation 2 leaves MISSING; a
Codex review job runs again in generation 2.

## One yes per candidate (P1-03)

A deploy candidate is what `fetch` produced: the `upload/` files (local, or prod + our hunks), the `prod-before/`
backups, `deploy.json` **and the effective configuration** (the resolved profile: transport command templates, remote
root, `after`, `canary`, `last`, `fatal`, `origin_ip`, plus `verify` / `db` / `allow_unrestorable` and the ordered
remote targets - stored only as a digest, `effective_sha`, never as the templates; V2-03). `fetch` names it with a
token `attempt:hash` (`CANDIDATE:` line; `deploy/state.json` → `candidate`; the same bytes and the same profile give
the same token, so repeated fetches of an unchanged prod do not invalidate a yes; a profile edited in `CLAUDE.md`
after the fetch is `CANDIDATE_CHANGED` on apply itself, no new fetch needed to notice). Every yes is for one token:
the harness refuses `--go`, `--force` and `--yes` without `--approved <token>`, and refuses a token of another
candidate or attempt before it records or uploads anything; `power_deploy.py apply --go` checks the same token itself
(`NOT_APPROVED`, `APPROVAL_MISMATCH`) and records the approval in the state. A `needs_yes` answer is stored with the
token and counts only while the candidate is the same. A hand-resolved `conflict` / `diverged-no-base` /
`merge-error` row becomes part of the candidate only through `power_deploy.py seal` (V2-11): it validates the
resolution, hashes it and issues a new token; the harness `deploy` keeps a sealed candidate instead of fetching it
away (`--refetch` drops it). The attempt number changes only with `fetch --new-attempt` after a prod write; the old
attempt (backup, candidate, state with its approval) is archived under `deploy/attempts/<n>/`. States written before
this version have no token: `status` says so and `apply --go` asks for a new fetch.

## What enforces what (5.4)

Three kinds of rules, so that nobody mistakes a prompt line for a guarantee:

- **code**: a script refuses, stops or records it; a test in `tests/` covers it.
- **prompt**: the agent is told so in its instructions (`power_prompts.py`); the harness checks the result where it
  can, but a model can ignore a prompt.
- **external**: the orchestrating session or #THE_OPERATOR# does it; the scripts only make it visible.

The **Path** column says where the rule is actually executed (V2-14: the two loops are not identical): `harness` =
`power_run.py`; `Workflow` = `power_workflow.js`, the in-session fallback (the weaker path, see
`reference/workflow.md`); `deploy` = `power_deploy.py` (from the harness `deploy` or by hand); `external` = the session.

| Rule | How | Where | Path |
|--|--|--|--|
| An agent does not edit outside its file list | code (`check`: reported stray → BLOCKED; diff-only stray → SUSPECT hard stop) + prompt | `power_check.py check`, `power_run.py`, tests strayreported / straydiff | harness + Workflow (JS: reported stray → BLOCKED, `scope_changes` in the result; the SUSPECT stop is the harness's) |
| Never-in-a-plan paths (`.env`, secrets, `deny`) | code at plan time (`prepare` rejects them) + prompt + code at deploy (`hard_stops`: sensitive file in `deploy.json`) | `power_check.py prepare`, `power_run.py hard_stops` | harness (`prepare` also runs for the Workflow via `ready`) + deploy |
| No git commit / stash / checkout / reset by an agent | prompt only | `power_prompts.py` task rules | prompt, both loops |
| Anchors, files, contracts checked before the first edit; `PLAN_MISMATCH` → restore | code (`restore` to the snapshot, step 1 finishes) + prompt (the agent reports the mismatch) | `power_run.py`, tests mismatch / finalfixmismatch | harness + Workflow |
| The plan is not changed silently | code (every reported `plan_change` of every call - work, fix, finish, finalfix, the orchestrator's stray resolution - in the append-only history `plan_changes` of the state, each once → PLAN.md "Plan changes" with provenance, REPORT, `results.json` `plan_changes`; a later empty field erases nothing) | `power_run.py note_plan_change / append_plan_changes`, tests escalate / planchanges | harness (Workflow: `plan_changes` in the result; PLAN.md / REPORT are the session's) |
| A verify-only pass never clears a reported scope violation | code (`verify_only` merges the verdict into the earlier outcome: result, author, `authors`, verdicts, plan changes and `stray_files` survive; the task is released only when the stray is resolved by an act the harness can see - back at the snapshot, or listed for the task in `tasks.json`, recorded in `stray_resolved` and as a plan change; BLOCKED_DEP tasks are released only by a real PASS) | `power_run.py TaskLoop.verify_only / stray_resolution`, test verifystray | harness (Workflow: merge into `previous`, release only by a `tasks.json` listing, `stray_resolved` accumulates over rounds and a crashed verify keeps the evidence - `verifyFailed`, V3-07; no snapshot-bytes check) |
| The verifier never sees the author's summary | code (the verify prompt is built from the task, contracts, diff and check report) | `power_prompts.py`, `power_run.py` | harness + Workflow |
| A check has not passed without a `power_check.py` report | code (checks are subprocesses of the harness; a missing final check is a hard stop; only `CLEAN` / `FIXED` close the final review - any other status, unknown, `RUNNING`, empty or missing, is a stop, V3-05; an invalid `## POWER` block is INFRA for every check instead of an empty smoke suite, V3-06) | `power_run.py`, `power_check.py cmd_check`, tests hardstops / finalstatus / configbroken | harness + Workflow (the Workflow copies CHECK_JSON through a runner agent) |
| Code is never verified by its author | code (`verifier_for`, `other_claude()` after a Codex failure; `results.json` carries the real `author` / `verified_by` and the accumulated `authors` of the task - work, fix, finish; a restore to the snapshot clears them; `Run.authors()` adds every final fixer, V3-02; the escalation model and its fallback are chosen by the real author of the rejected code; the final reviewer by the real authors of all retained code - `review_choice`: step 3 when it wrote nothing, else the other strong Claude, else Astra when the run has it; when every strong model wrote code the review is recorded `independent: false` and is a hard stop) | `power_run.py`, tests codex / codexfallback / realauthor / authors | harness (Workflow: `verifierFor` per task, the escalation shift and `authors` in the result; no real-author final review - the session's) |
| Every agent has a type and a hard call cap | code for Claude agents (`start` preflight of the agent types; `--max-turns`, `--max-budget-usd` per role); Codex jobs have a sandbox, a schema and `JOB_TIMEOUT` instead, their tool-call budget is prompt only; + prompt (`BUDGET` in the prompts) | `power_run.py ROLES`, `codex_task.py`, test noagents | harness (Workflow: `agentType` only, no budget flag) |
| One harness process per run; the agent cap | code (`run.lock` = OS lock on an open handle, `POWER_MAX_AGENTS` → INCOMPLETE) | `power_run.py`, tests lock / lockatomic / cap | harness only |
| A failed Codex runner cannot keep editing | code (`codex_task.py kill` before the fallback; a kill that is not confirmed = no fallback) | tests codexkill / killfailed / killtree (Windows and WSL Ubuntu), Astra's kill_tree probes in WSL | harness + Workflow (`codexKill` before every fallback); Windows `taskkill /T`; POSIX: the process group plus every descendant listed before the first signal, a SIGTERM grace for the whole tree, SIGKILL for what survives, a zombie counts as gone (V3-01) |
| Old review evidence never covers a new working tree or a dead reviewer | code (review generations: `final_history`, `archive/final.g<N>/`, `--gen` Codex job names, CLEAN needs this generation's check) | test reviewgen | harness |
| A watch that is not HEALTHY is not a completed deploy | code (`deploy --go`: exit 2, phase `deploy_failed`, `HARD STOP:` + `HARNESS DEPLOY:` lines; DB steps → phase `awaiting_watch`; `watch` itself is HEALTHY only when every canary answers now - down now and fine before = rollback, down now and already down before (`--force-canary`) = `DEGRADED`, exit 2, no automatic rollback, V3-03) | harness test watchrollback, deploy test watch_degraded | harness `deploy` + deploy |
| A failed required `after` hook is not a deploy | code (`after` hooks are required unless `{"cmd", "optional": true}`: apply ends `AFTER_FAILED` (exit 2, the files stay on prod, nothing is undone automatically, the report names the hook for manual recovery); a failed required recovery hook turns a clean rollback into `ROLLED_BACK_AFTER_FAILED` in apply, watch and rollback; the rollback after a failed put / byte verify runs the hooks too - `FAILED_ROLLED_BACK` / `FAILED_ROLLED_BACK_AFTER_FAILED`, V3-04; an optional failure is a warning; the harness `deploy --go` treats any non-DEPLOYED apply as `deploy_failed`) | deploy tests after_hooks / recovery_hooks, harness test afterhook | deploy + harness `deploy` |
| No agent starts without the executables; dead planners stop cleanly | code (`start` preflight: `claude` (`POWER_CLAUDE` / PATH), `git`, Git Bash, the helper scripts, a valid `## POWER` block (V3-06), and with a Codex model the codex executable (`CODEX_EXE` - one that points nowhere is an error, not a fallback - the desktop app, PATH) and the login `auth.json` under `CODEX_HOME`, no paid probe (`codex_task.readiness`; `codex_task.py ready` makes the same check for the Workflow) - `PREFLIGHT FAILED`, no state, no agent, no edit; two planners without a plan = `plan_failed`, exit 2, gate_a.md with the cause and `continue --replan`, no traceback) | `power_run.py preflight / phase_plan`, tests preflight / configbroken / readiness | harness (+ `codex_task.py ready`) |
| A binary without backup is never uploaded without `allow_unrestorable` in the reviewed candidate | code (`apply`: `UNRESTORABLE_BINARY` / `FETCH_FAILED` before the first write, whatever fetch printed) | deploy test binary_guard | deploy |
| The prod re-check covers every row; unknown is never fine | code (`remote_changed` on upload, skip and resolved rows against the fetched digests; a planned-new file must be confirmed absent, `ls` unavailable = stop) | deploy test precheck_all | deploy |
| The yes covers the effective configuration | code (`effective_sha` in the token; a changed profile = `CANDIDATE_CHANGED` on apply) | deploy test effective_config | deploy |
| A merge process failure is never a candidate | code (`sh3` keeps stdout and stderr apart; only exit 0 without markers is `merged`, 1-127 with markers `conflict`, else `merge-error` with nothing in `upload/`) | deploy test merge_error (`tests/mock_merge.py`, `POWER_GIT_MERGE`) | deploy |
| A rollback restores only the fetched bytes | code (`prod_before_sha`: digest check before every restore and `BACKUP_INVALID` before the first write; the restored prod bytes are verified against the digest) | deploy test backup_integrity | deploy |
| A hand resolution is a new, sealed candidate | code (`seal`: no markers, a file per stop row, new token; no conflict exemption in `candidate_changed`; harness `deploy` keeps the seal, `--refetch` drops it) | deploy test seal, harness test deploysealed | deploy + harness `deploy` |
| Unclear request → ask before planning | external (the session, §3 of SKILL) + code for what the planner flags (`QUESTION:` risks stop at Gate A even with START) | `power_run.py`, test questions | external + harness |
| Baseline: a red check before any edit is not blamed on the executor | code (PRE-BROKEN / SUSPECT stop START) | `power_check.py baseline`, test prebroken | harness (+ `codex_task.py ready` for the Workflow) |
| Project smoke runs before the deploy | code (`smoke` commands in `check --all`, a failure fails the final check) | test smoke | harness |
| A failed final check runs no lens and no reviewer | code (PERF-02: `_phase_final` ends with `FAIL`, `short_circuit: true`, `review.verdict: SKIPPED`, the deploy plan not evaluated; `continue --diagnose` runs the full review as a new generation) | `power_run.py _phase_final / cmd_continue`, test perfshort | harness |
| No paid Codex probe without a request | code (PERF-03: `codex_task.py usage` only with `--refresh-quota`; the report says when it was not fetched) | `power_run.py codex_usage`, tests quota / codex | harness |
| An agent spends no tool round on its own instructions | code (PERF-04: `inline_prompt` puts the rendered prompt in the message, the file stays as the audit copy and is named in the message; a missing file falls back to `Read <file>`) | `power_run.py claude_msg / named_msg`, test perfshort | harness (the Workflow still says `Read`) |
| Hard stops beat `DEPLOY` | code for the ones the scripts can see (BLOCKED / INCOMPLETE, unclosed final, failed or missing final check, SUSPECT, DB steps, sensitive files, `needs_yes`, conflicts, binary without backup, canary down, candidate / prod / `deploy.json` changed, approval for another candidate, rollback) | `power_run.py hard_stops`, `power_deploy.py`, both test suites | harness + deploy |
| Changes to orders / customers / data; judging a merged diff; partial deploy | external (orchestrator's judgement; the scripts show the diffs and ask) | SKILL §7-8 | external |
| No deploy with an unanswered question, even with `START DEPLOY` | code (`needs_yes` → `--yes N --approved`, `--force` does not cover it) | `power_run.py deploy`, test start | harness `deploy` + deploy |
| A yes is for one candidate | code (`--approved <attempt:hash>`, `NOT_APPROVED` / `APPROVAL_MISMATCH`, stale answers) | tests approval / start / deploytwice | harness `deploy` + deploy |
| Prod is written only by the orchestrator, serially, with backup and verified rollback | code (`power_deploy.py`: `attempted` before put, byte verify, canary, rollback statuses) + prompt (agents are told never to touch prod) | `tests/test_power_deploy.py` | deploy (policy, not actor authorization - see Trust boundary) |
| DB / option / flag steps | external for the `apply` commands (the orchestrator runs them after a yes); code makes them a hard stop and never runs the `apply`; the ONE script that runs DB commands is `watch --go --db-rollback`: the `rollback` of every DB step after a crash, recorded in `db_rollbacks` with their exit codes, separate from the file rollback status (a verified file restore does not prove the DB restore - the orchestrator reads both) | `power_deploy.py cmd_watch`, `power_run.py hard_stops` | deploy (`watch --db-rollback`) + external |
| Another session's CLAIM | code at deploy (`check_claims` refuses files in a foreign active CLAIM) + external (the session writes its own CLAIM line) | `power_deploy.py` | deploy + external |
| Messages to #THE_OPERATOR# in Bulgarian | code for the gate files and reports; external for the session's relay | `power_run.py gate_a / gate_b / report_text` | harness + external |
| Thin session: no planning, review or Agent tool while the harness runs | external | SKILL §1, §10 | external |
| Prod verification after the deploy (`prod-verifier`) | external | SKILL §8 | external |
| Settings on a new machine | code reports (`install --check`), external applies (settings are never edited by `install`) | `reference/setup.md` | `power_check.py install` + external |

## Trust boundary (V2 2.2)

The candidate token (`--approved <attempt:hash>`) is an **integrity and version guard**: it proves that what is
uploaded is the candidate #THE_OPERATOR# saw, with its backups, `deploy.json` and the effective profile, and that nothing
changed since. It is **not actor authorization**: the token is printed and stored in `deploy/state.json`, readable by
the same local account, and an agent with Bash on this workstation could read it, call the transport commands
directly or edit the state. "Agents never touch prod or git" is therefore prompt + orchestration policy on a
trusted personal workstation (the agents are given no such instructions and the harness is the only caller of
`power_deploy.py`), not an OS-level boundary; a read-only agent type with Bash is not a read-only boundary either.
Real enforcement would need separate credentials, a deployment broker the agent processes cannot reach, or an
account without the transport credentials. The same holds for the approval / state digests: they catch
accidental change, not a process that rewrites manifest, hashes and payload together.

## Model dispatch

Every agent of every phase goes through one door, `Run.call(role, model, …)`: a Codex family (`astra`, `sol`,
`luna`) runs as a `codex_task.py` job of the matching kind (`work`, `fix`, `finish`, `verify`, `plan`, `plan-area`,
`review`, `finalfix`, `finalverify`), every other family as a headless `claude`. The call returns the value and the
family that really answered, and that is what `results.json` records as `author` / `verified_by` and what
`harness/state.json` → `final.finalfix.by` / `finalverify.by` hold. A Codex job that fails falls back once to a Claude
model chosen by the caller so that it is never the model that wrote the code (verifier of an Opus author → Fable;
finish / finalfix → a strong Claude that wrote none of the retained code). Every such choice is made from the REAL
authors (V2-13), never from the configured step 2: the verifier from the author of the task's code after any fallback;
the escalation model and its fallback from the author of the rejected code (`escalate_model(author)`); the final
reviewer from the `author` of every task outcome (`Run.authors()`, `review_choice()`): step 3 when it wrote nothing,
else the other strong Claude, else Astra when the run has a Codex model, and when every strong model wrote code the
configured step 3 reviews and `final.review.independent` is false - a hard stop ("финалният преглед не е независим"),
the orchestrator reviews by hand or reruns the review with another model. `final.review.authors` lists them. The log
says who took over; `calls.jsonl` keeps the failed Codex job (model = the family, `slug` = the Codex model) and the
fallback agent as two records.

## Files outside the task lists (P1-04)

Two different things, both visible in `results.json`, REPORT.md and the hard stops: a file the executor itself
reports in `files_changed` outside its list makes the task `BLOCKED` whatever the verifier said (`stray_files`,
reason "edited files outside the task's list"); a file changed outside every task list that only the diff shows
(`scope_changes` of `check`, from the snapshot's git / walk state: another session, or an agent that did not report
it) keeps the task's verdict but is `SUSPECT`: a hard stop that names the files, nothing is restored automatically,
the orchestrator decides. The hard stops also read the latest full check (`check --all --round final2` when a final
fix ran, else `final`); a check that never ran is a stop, not a pass. The project's `smoke` commands (`## POWER`)
run as part of `check --all` (final and final2; `smoke` block in `check.json`, `smoke.checkN.log`), never per task.
A final fixer that answers `PLAN_MISMATCH` blocks the final review. Every `plan_change` any agent reports (work, fix,
finish, finalfix) goes into the append-only history `plan_changes` of the state the moment it arrives (`Run.note_plan_change`,
one entry per task + call kind + text, with who reported it and in which round), and from there once into `PLAN.md`
under "Plan changes" (`#id task (by, kind round n): text`), into REPORT ("Промени по плана") and into the task's
`plan_changes` in `results.json`; a fixer that reports an empty field erases nothing, a resumed run or a second review
generation adds nothing twice. Reported is not approved: the orchestrator judges the list.

`power_run.py verify --tasks id:author:round` (verify-only, after the orchestrator fixed a check command, restored a
file or changed the plan) merges the new verdict into the task's earlier outcome (V2-10): result, author, earlier
verdicts, plan changes and `stray_files` survive, `rounds` grows. A reported stray file is resolved only by an act the
harness can see, recorded in `stray_resolved`: the file is back at the snapshot (`before/files/`), it is gone again
when the snapshot had none, or `tasks.json` now lists it for the task (a plan change by the orchestrator, recorded in
the history as kind `orchestrator`). Otherwise a clean verdict leaves the task BLOCKED, its dependants BLOCKED_DEP,
and the hard stop in place.

## Files in `<run>/harness/`

- `state.json`: flags, models, Codex slugs, agent types, phase, recon/prepare/baseline results, the Workflow-style
  `args`, per task outcome (`tasks`), the final review block (`final`), deploy status.
- `status.json`: `{phase, done, total, last_item, updated_at, running, cost_usd}` (the batch convention; a status
  reader prints it).
- `log.txt`: one line per event (agent done, check PASS/FAIL, verdict, escalation, phase).
- `calls.jsonl`: one line per agent process: label, role, model, agent type, turns, input/cache/output tokens,
  `total_cost_usd` (0 on the subscription for some models; the input tokens are the measure), ok/error.
- `gate_a.md`, `gate_b.md`: the messages for #THE_OPERATOR#, in Bulgarian, relayed by the session as they are.

The rest of the run (`PLAN.md`, `tasks.json`, `recon/`, `prompts/`, `before/`, `checks/`, `results.json`, `review/`,
`REVIEW.md`, `DEPLOY.md`, `deploy.json`, `review.json`, `REPORT.md`, `deploy/`) is the same as in the in-session path.

## Phases inside the harness

| Phase | Agents | Script steps |
|--|--|--|
| recon | `power-reader` Sonnet low, one per `--recon` (default one for the scope) | `power_prompts.py write --kinds recon` |
| plan | `power-executor` step 1 (Astra: `codex_task.py plan`); skeleton → one area planner per area | `write --kinds plan` / `areas`, `power_check.py merge`, `prepare` (one retry), `baseline` |
| work | per task: `power-executor` step 2 → `power-verifier` (per §2 of SKILL) → fix → finish by the escalation model | `codex_task.py ready`, `power_check.py check --task`, `restore`, Codex jobs as subprocesses |
| review | `check --all --round final` (+ the project's `smoke` commands); a failure ends the review as `FAIL` (PERF-02, unless `continue --diagnose`); else the lenses (`power-reader` Sonnet low, or the lens's `agent_type`) in parallel; reviewer = step 3 shifted; `finalfix` step 1 (a Codex job for Astra) → `check --all --round final2` → `finalverify` by the verifier of the real fixer | `write --kinds lens / review / finalfix,finalverify`, `power_deploy.py plan` |
| report | - | `REPORT.md`, `gate_b.md`; `codex_task.py status` on a Codex step, `usage` before/after only with `--refresh-quota` (PERF-03) |
| deploy | - | `power_deploy.py plan`, `fetch` (candidate token; skipped for a sealed hand resolution unless `--refetch`), `apply --go --approved <token>`, `watch --go` |

Roles, turns, budgets: `ROLES` in `power_run.py` (reader 32 turns, verifier 25/30, executor 45-55; budgets
2-15 USD per agent, scaled by `POWER_BUDGET_SCALE`). The permission mode is `auto` (measured: the default headless
mode denies Bash; `acceptEdits` and `auto` allow the agent's edits and commands).

## Environment

| Variable | Default | Why |
|--|--|--|
| `POWER_CLAUDE` | `claude` on PATH | the command (forward slashes on Windows); `start` preflights it (a missing command = `PREFLIGHT FAILED`, nothing started); the tests point it at `tests/mock_claude.py` |
| `POWER_MAX_PARALLEL` | 6 | agent processes at once (each is a node process; the Codex limit is separate) |
| `POWER_MAX_AGENTS` | 0 | cap on the agents one run may start (0 = none); at the cap the phase stops with INCOMPLETE, `continue` resumes |
| `POWER_PERMISSION_MODE` | `auto` | `--permission-mode` of every agent |
| `POWER_AGENT_TIMEOUT` | 3600 | seconds per agent process, then it counts as dead |
| `POWER_BUDGET_SCALE` | 1.0 | multiplies the per-role `--max-budget-usd`; 0 removes the caps |
| `POWER_CHECK_HELPER`, `POWER_CODEX_HELPER`, `POWER_DEPLOY_HELPER` | the skill's scripts | overrides for tests |

## Tests

    PYTHONUTF8=1 python "<skill>/tests/test_power_run.py" [--keep] [happy fix escalate mismatch checkbroken dead resume start prebroken
                                                                 questions deploytwice strayreported straydiff noagents hardstops
                                                                 finalfixmismatch smoke codex codexfallback lock cap codexkill
                                                                 watchrollback reviewgen killfailed lockatomic deploysealed
                                                                 verifystray realauthor planchanges afterhook preflight workflow killtree
                                                                 authors finalstatus configbroken readiness]
    PYTHONUTF8=1 python "<skill>/tests/test_power_deploy.py" [--keep] [canary_before partial_write rollback_fail new_file_cleanup refetch
                                                                    remote_changed candidate_changed binary_unknown approval binary_guard
                                                                    precheck_all effective_config merge_error backup_integrity seal
                                                                    after_hooks recovery_hooks watch_degraded]

A synthetic git project under `D:\tmp\prun_test\<case>` (two PHP files, the skill's agent types in `.claude/agents`,
a `## POWER` block with lenses and a `cp` deploy profile against a fake prod folder), a scenario json per case, the
fake `claude` (`tests/mock_claude.py`: applies the scenario's edits, writes the run files, prints the JSON envelope,
records every call), the fake `codex_task.py` (`tests/mock_codex.py` via `POWER_CODEX_HELPER`, with a `CODEX_HOME`
model cache fixture; `ready` / `status` / `kill` delegate to the real script), real `power_check.py` checks.
Twenty-two scenarios cover Gate A/B, fix, escalation, PLAN_MISMATCH + restore, CHECK-BROKEN + verify-only + the
release of the dependent tasks, a dead executor, resume after a crash, START with finalfix/finalverify, baseline and
QUESTION stops, `deploy --go` refused by a hard stop and without `--approved`, `--yes` bound to the candidate (stale
after an edited `deploy.json`), deploy twice with the candidate token, a reported and a diff-only stray file, missing
agent types, the latest-check hard stop, a PLAN_MISMATCH final fix, smoke commands, Astra as step 1 through Codex
jobs, the fallback when every Codex job fails, the run lock (a live holder refuses every command, a stale one is
replaced), `POWER_MAX_AGENTS=3` (INCOMPLETE, then `continue` with no cap) and the kill of a failed Codex job's
runner (a real sleeper process, killed through the real `kill`). Stage A of the v2 review (14.10) adds four: a watch
that rolls back / cannot be read / is healthy (`watchrollback`, with `tests/mock_deploy_watch_broken.py`), review
generations (`reviewgen`: stale final2, dead reviewer, versioned Codex review jobs), a failed kill without fallback
(`killfailed`, `MOCK_CODEX_KILL=FAILED`) and the atomic run lock (`lockatomic`: six racing processes, a killed
holder, an empty held lock); stage B adds `deploysealed` (a conflict resolved by hand and sealed: `deploy` keeps the
sealed candidate, the old token is refused, `--refetch` drops the seal); stage C adds `verifystray` (verify-only keeps
a reported stray file and its dependants blocked; released only by a restore to the snapshot or a `tasks.json` plan
change, each recorded), `realauthor` (the final reviewer and the escalation fallback by the real authors after a Codex
fallback and after escalation; mixed strong authors → `independent: false` hard stop, `deploy --go` refused) and
`planchanges` (the first executor's `plan_change` survives a fixer's empty field, finalfix changes reach PLAN.md and
REPORT, nothing is duplicated by verify-only or a second review generation); stage D adds `afterhook` (an apply that ends
`AFTER_FAILED` is `deploy_failed` for the harness, no watch), `preflight` (a missing `claude` stops before any state,
agent or edit; two dead planners end in `plan_failed` with a gate_a.md and `continue --replan` recovers), `workflow`
(the real `outcome` / `verifyOutcome` of `power_workflow.js` run in node: reported stray → BLOCKED, `scope_changes`,
`plan_changes`, verify-only merge with a stray kept or released by `tasks.json`, `codexKill` before every fallback) and
`killtree` (`codex_task.kill_tree` ends the runner and its child; the creation flags are Windows-only); stage A of the
v3 review (14.10) adds `authors` (V3-02: Opus writes, Fable finishes → both are authors, the review is not independent
and a hard stop; a restore drops the earlier author; a final fixer counts; verify-only keeps the list), `finalstatus`
(V3-05: `RUNNING`, an unknown, an empty or a missing final status is a stop and `deploy --go` refuses), `configbroken`
(V3-06: an invalid `## POWER` block = `PREFLIGHT FAILED` at start, INFRA in `check --task` and `check --all`, the task
BLOCKED as infrastructure without a verifier) and `readiness` (a missing codex executable or `auth.json` with a Codex
model = `PREFLIGHT FAILED` and `READY: FAIL`, Claude-only runs need neither), and extends `workflow` (V3-07:
`stray_resolved` history, `verifyFailed` keeps the evidence, `authors` parity); stage B of v3 adds `perfshort` (PERF-02:
a failed final check = `FAIL`, no lens, no reviewer, `deploy --go` refused, `continue --diagnose` runs both as generation
2 and a plain `continue` from `gate_b` runs nothing; PERF-04: every Claude agent got its instructions inline and the
prompt files stay), `quota` (PERF-03: `--refresh-quota` fetches the Codex usage twice and the report shows both; `codex`
proves none without it) and `polling` (PERF-09: `codex_task.run_job` reports a finished job within 0.25 s and a later
one within one backoff step, against the real module) - forty-one in all; `killtree`, `codexkill` and `polling` also
run in WSL Ubuntu, where Astra's `kill_tree_zombie` / `kill_tree_survivor` probes pass (V3-01).
`tests/test_power_deploy.py` (eighteen
scenarios, a controllable transport `tests/fake_transport.py` and canned probes via `POWER_PROBE_MAP`) covers the
deploy script alone: canary down before the upload, a put that writes and then fails, a failing rollback, new-file
cleanup, the backup kept across a refetch, prod or the candidate changed since the fetch, binary-unknown, the
approval token (no yes, another candidate, an edited `deploy.json`, a new attempt), the binary guard at apply
(`binary_guard`: a failed fetch's token never uploads; `allow_unrestorable` is a new candidate), and stage B of the
v2 review: the prod re-check of every row (`precheck_all`: a binary or text file that appeared where a new one was
planned, `ls` down = unknown = stop, a changed skip row, a re-download that fails), the effective configuration in
the token (`effective_config`: `after`, `canary`, transport command, remote root, `last`, order), merge process
failures (`merge_error`: exit 128 / 127 / 255 and markers with exit 0 are never a candidate, stderr noise never lands
in the source, a real conflict stays a conflict), backup integrity (`backup_integrity`: a corrupted, truncated,
replaced or missing backup is `ROLLBACK_INCOMPLETE` and never uploaded, `BACKUP_INVALID` before the first write, a
transport that rewrites the restore) and the sealed hand resolution (`seal`: unsealed and hand-edited state refused,
markers refused, new token, old token void, edit after seal = `CANDIDATE_CHANGED`, re-seal idempotent,
diverged-no-base), and stage D's `after_hooks` (a failed required hook = `AFTER_FAILED` with the files on prod and the
hook named for manual recovery; optional = warning, `DEPLOYED`; a failed required recovery hook =
`ROLLED_BACK_AFTER_FAILED` in apply, watch and manual rollback); stage A of the v3 review adds `recovery_hooks` (V3-04: the
hooks run after the rollback of a failed put and of a failed byte verify - `FAILED_ROLLED_BACK`, `_AFTER_FAILED` when a
required one fails, optional = warning) and `watch_degraded` (V3-03: a 503 baseline under `--force-canary` and a 503
now = `DEGRADED`, exit 2, no rollback; the same baseline with prod answering = `HEALTHY`; fine before and down now =
`ROLLED_BACK`). Nothing in them touches a network, a credential or prod. A live probe
(13.10.2026): a haiku executor edited a file and a haiku verifier judged it through `Run.claude_call`, 10 s each.

## Known follow-ups (source-level observations)

Still open, documented so that nobody reads them as covered:

- Approval and state digests detect accidental change only (see Trust boundary).
- POSIX is tested in WSL Ubuntu (`killtree`, `codexkill`, Astra's kill probes), not by the full suite; Windows is the
  supported platform. `_children` needs `pgrep`; without it only the process group is signalled.
- `apply --force-canary` still ends `DEPLOYED` when the overridden canary stays down (that is #THE_OPERATOR#'s explicit yes);
  the following `watch` reports it as `DEGRADED`.

Closed in stage B of the v3 review (14.10, the quick performance wins): a failed `check --all final` runs no lens and
no reviewer (PERF-02, `continue --diagnose` for the full review); the Codex `usage` probe runs only with
`--refresh-quota` (PERF-03); the agent instructions travel in the message, the prompt file is the audit copy (PERF-04);
`codex_task.run_job` polls from 0.25 s with a backoff to 2 s instead of a fixed 5 s sleep, and the harness re-attaches
to a still-running job after 0.5 s (PERF-09). Astra's PERF-01/05/06/07/08 (fast / standard / thorough mode, a Markdown
renderer, lenses by path, a usage ledger, a run budget) and the DeepSeek pilot are design changes, decided after the
first live run with measurements. Closed in stage A of the v3 review (14.10): `watch` separates current health from the baseline (`DEGRADED`, V3-03);
the rollback after a failed put / byte verify runs the after hooks (V3-04); `hard_stops` requires `CLEAN` / `FIXED`
(V3-05); an invalid `## POWER` block is INFRA (V3-06); the POSIX `kill_tree` waits for the whole tree and treats a
zombie as gone (V3-01); `authors` accumulate per task (V3-02); the Workflow keeps `stray_resolved` history and the
evidence of a crashed verify (V3-07). Closed in stage D of v2: the archive index of `fetch --new-attempt` is read
before `state.json` moves (earlier entries were lost), the Windows creation flags are guarded by `os.name`.

## When the harness cannot run

No `claude` on PATH, a machine without the agent types (`install`), or a failure before Gate A that `continue`
cannot pass: the protocol runs from the session with the Workflow (`reference/session-path.md`, the former core).
It costs the session's context on every call (measured 32-52% of a run), so it is the fallback, not the default.
