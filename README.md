# POWER - multi-agent mode for large tasks in Claude Code

> **Before the first run:** replace `#THE_OPERATOR#` with your name in all files. See [Adapting to another user or workspace](#adapting-to-another-user-or-workspace).

POWER is a Claude Code skill that takes one large change (a feature across many files, a refactor, a batch of
translations, a multi-module fix) and runs it as a controlled pipeline of independent agents:

**recon → executable plan → approval → parallel executors → script checks → verifier with clean context →
final review → report → gated deploy.**

The session you type in stays thin. The work is done by headless `claude -p` processes driven by a Python harness
(`power_run.py`), each with a small start context, a hard call cap and a dollar budget. Code is never verified by
the model that wrote it.

Snapshot of the skill as of 2026-10-14, after three hardening reviews by Astra (harness v3, review v3 stages A+B): deploy lifecycle with
verified rollback and a sealed candidate per approval token (file bytes, backups, `deploy.json` AND the effective
profile), prod re-check of every row before the first write, backup digests, required/optional `after` hooks
(`AFTER_FAILED` is not a deploy), one dispatcher for Claude and Codex agents with real-author provenance (verifier,
escalation and final reviewer chosen by who really wrote the code), append-only plan-change history, verify-only
passes that merge instead of overwrite, review generations, an atomic run lock, agent cap, killed Codex runners before
any fallback, preflight of the executables; first live run 2026-10-13: 20/20 tasks PASS. The in-session Workflow
fallback carries the same outcome rules where a script can (`reference/workflow.md` says what it still lacks).

---

## Contents

| Path | What it is |
|--|--|
| `SKILL.md` | The core protocol the session reads when triggered (trigger forms, models, phases, hard stops) |
| `power_run.py` | **The harness**: runs phases 0-6 from code, outside the session; `start`, `continue`, `verify`, `status`, `cost`, `deploy` |
| `power_check.py` | Plan validation (`prepare`), snapshot, baseline, script checks, restore, results table, project config, `install` |
| `power_prompts.py` | Single source of every agent instruction (recon, plan, task, fix, verify, lens, review...) and the call caps |
| `power_deploy.py` | Deploy: `plan` → `fetch` (prod backup + 3-way merge, candidate token) → `apply --go --approved <token>` (canary, ordered upload, byte-verify, verified rollback) → `watch` → `rollback`, `status` |
| `codex_task.py` | Optional Codex (OpenAI) jobs: `models`, `ready`, work/fix/finish/verify/plan/review/finalfix/finalverify, `kill`, `usage`, `status` |
| `power_cost.py` | Cost breakdown by role from the harness call log |
| `power_workflow.js` | The in-session Workflow (fallback path when the harness cannot run) |
| `agents/*.md` | Four agent types: `power-reader`, `power-executor`, `power-verifier`, `power-runner` |
| `reference/*.md` | Phase-specific details, read only when the phase needs them (see the table in `SKILL.md`) |
| `tests/` | Offline test suite: a fake `claude` (`mock_claude.py`), a fake Codex helper (`mock_codex.py`), a controllable deploy transport (`fake_transport.py`); 41 harness scenarios + 18 deploy scenarios (all offline) |

---

## What it does

1. **Intake.** Checks that the request is clear (asks before planning if it is not) and big enough to justify
   POWER (it says so when a normal session would be 5-7× cheaper). Shows a 3-question model menu.
2. **Recon.** 1-4 read-only explorer agents map the code in parallel and write short reports.
3. **Plan.** The step-1 model writes an *executable* plan: a closed list of files per task, anchors (exact code the
   task expects to find), contracts between tasks, non-goals, risks, and script checks (`php -l`, grep asserts,
   smoke commands). `prepare` validates it mechanically; `baseline` runs every check *before* any edit so a check that
   is already red is flagged (PRE-BROKEN / SUSPECT) instead of blamed on the executor.
4. **Gate A.** A message with the task table, contracts, risks, open questions and expected agent count. You say
   "да" (yes), edit `tasks.json`, or add decisions to `request.md` and re-plan.
5. **Work.** Up to 6 tasks in parallel, no waves: a task starts when its dependencies PASS. Per task:
   executor → script checks → verifier (clean context: task, contracts, diff, check report - never the author's
   summary) → one fix → re-check → re-verify → on a second return the stronger model finishes → otherwise BLOCKED.
   If an anchor or contract does not match reality, the executor stops with `PLAN_MISMATCH`, the file is restored to
   the snapshot and the planner fixes the plan.
6. **Final review.** The whole diff is checked again, review lenses (domain, smoke, i18n, or your own) run in
   parallel, and the step-3 model confirms every finding against the code, then writes `REVIEW.md`, `DEPLOY.md` and a
   machine-readable `deploy.json`. Medium+ findings are fixed by step 1 and verified by a model that did not write them.
7. **Report + Gate B.** `REPORT.md`: per task status, author, verifier, rounds, escalations, plan changes, changed
   files, measured cost by role, the deploy plan and hard stops.
8. **Deploy.** `power_deploy.py` downloads the prod version of every file first (backup), classifies it
   (clean / same / new / merged / conflict), names the result with a candidate token (`attempt:hash`), and uploads only
   with `--approved <token>`: your yes is for exactly the candidate you saw, a later fetch of a changed prod is another
   one and asks again. Canary URLs before (a site already down stops the upload) and after, upload in order, byte
   verification, automatic rollback on a certain failure with every restored file verified. Only the orchestrator
   deploys, serially; agents never touch prod.
9. **Commit (default).** After Gate B (no deploy) or after `watch` ends `HEALTHY`, the session makes one git commit
   per touched repo with only the plan's closed file list plus `docs/power/<run>/`: `power(<slug>): ...`, never
   `push`, never `-A`, never with a BLOCKED task or a conflict. Off only with "без комит" in the task line
   (`SKILL.md` §9).

---

## Benefits

- **Quality by construction.** Every task is verified by a different model than its author, against the plan and a
  script check report, not against the author's own account of what it did.
- **No silent drift.** Anchors and closed file lists stop an agent the moment the code differs from the plan; every
  plan change is written to `PLAN.md` ("Plan changes") and to the report.
- **Cheap session, bounded agents.** The session only relays two gate messages; each agent starts with ~7-8k
  context (no `CLAUDE.md`, no memory), has a call cap and a `--max-budget-usd`. Measured: the orchestrating session
  went from 32-52% of a run's cost to a few calls.
- **Parallel by default.** Independent recon, tasks and lenses run at once; serial only where a shared resource
  requires it (prod upload, WAF-paced prod requests, checks marked `serial`).
- **Crash-safe.** `continue` resumes any interrupted phase; finished tasks keep their result, a half-done task is
  stashed and redone, nothing else repeats.
- **Safe deploy.** Prod backup before upload, merge detection against prod, canaries before and after, automatic and
  verified rollback, one yes per candidate (`--approved attempt:hash`), and hard stops (DB/options/secrets changes,
  conflicts, blocked tasks, failed smoke, prod or candidate changed since the fetch) that override even `DEPLOY`.
- **Bounded runs.** One harness process per run (`run.lock`), an optional cap on the agents a run may start
  (`POWER_MAX_AGENTS`: the run stops with INCOMPLETE tasks, never with a fake PASS), and a failed Codex runner is
  killed before a Claude model takes its role over.
- **Transparent cost.** Every agent call is logged (`harness/calls.jsonl`) with model, turns, tokens and cost;
  `power_run.py cost` and `REPORT.md` show the totals by role.
- **Mixed vendors (optional).** Codex models (Astra / Sol / Luna) can plan, execute or review, paid from a ChatGPT
  plan outside the Claude limit, with automatic fallback to Claude models when Codex fails.

When **not** to use it: ~3 tasks or fewer in one area. POWER has a fixed overhead of 8-10 agents (recon, plan,
lenses, review); a normal session plus one reviewer agent is cheaper there. The skill tells you this at intake.

---

## Installation

Requirements: Claude Code with custom agent types (`omitClaudeMd`, `maxTurns`), `claude` on PATH, Python 3,
Git Bash on Windows. Codex CLI is optional.

1. Copy this folder to a skills directory and name it `power`, e.g. `~/.claude/skills/power/` or
   `<workspace>/.claude/skills/power/`.
2. Install the agent types and the fallback workflow:

       python "<skill>/power_check.py" install
       python "<skill>/power_check.py" install --check

   This copies `agents/*.md` to `~/.claude/agents/` and `power_workflow.js` to
   `~/.claude/workflows/power-execute.js`. Re-run after any change to those files. Then `/reload-skills` or a new session.
3. Add to `~/.claude/settings.json` by hand (`install` never edits settings):

   ```json
   {
     "subagentPromptCacheTtl": "1h",
     "autoContinueAtUsageLimit": true,
     "permissions": { "allow": ["Workflow(power-execute)"] }
   }
   ```

4. Run the offline tests (no API calls, no network: a fake `claude`, a fake Codex helper, a fake transport):

       PYTHONUTF8=1 python "<skill>/tests/test_power_run.py"
       PYTHONUTF8=1 python "<skill>/tests/test_power_deploy.py"

   On non-Windows the test projects go to `/tmp/prun_test`; override with `POWER_TEST_ROOT`.

### Project configuration (optional)

Add a `## POWER` section with one `json` block to the project's `CLAUDE.md`. Without it POWER still runs with the
general rules and a manual deploy. Full key reference: `reference/codex-config.md`.

```json
{"deny": ["**/generated/**", "vendor/**"],
 "lenses": [
   {"name": "domain", "agent_type": "php-reviewer", "focus": "escaping, auth on every write route"},
   {"name": "smoke", "focus": "local stack http://localhost:8080: login and the touched routes, never a 500"}
 ],
 "smoke": ["docker exec app php -l /var/www/html/index.php"],
 "claim": ".project/SESSIONS.md",
 "deploy": {"profiles": {
   "app": {"local_root": "src",
           "remote_root": "/home/acct/app",
           "get": "python tools/remote.py get \"{remote}\" \"{local}\"",
           "put": "python tools/remote.py put \"{local}\" \"{remote_dir}\" --go",
           "rm":  "python tools/remote.py rm \"{remote}\" --go",
           "ls":  "python tools/remote.py ls \"{remote_dir}\"",
           "last": ["bootstrap.php"],
           "after": ["python tools/purge_cache.py --urls {urls}"],
           "canary": ["https://example.com/", "https://example.com/login"],
           "origin_ip": "1.2.3.4", "pace": 3, "settle": 20}}}}
```

- `deny` - paths no agent may change; `lenses` - final review angles (an `agent_type` runs the lens as that agent);
  `smoke` - commands for the local smoke check; `claim` - a file with CLAIM lines for parallel sessions.
- Deploy profile: `get/put/rm/ls` are your transport templates (FTP, SFTP, cPanel API, `cp`...), `last` = files
  uploaded last, `after` = cache invalidation, `canary` = URLs that must stay below HTTP 500.

---

## User guide

### The four trigger forms

Start the message with `POWER` in capitals. `START` and `DEPLOY` also only in capitals, so the word "deploy" inside
the task text does not trigger anything.

| Message | Plan approval (Gate A) | Deploy confirmation (Gate B) |
|--|--|--|
| `POWER <task>` | yes | yes |
| `POWER START <task>` | no | yes |
| `POWER DEPLOY <task>` | yes | no |
| `POWER START DEPLOY <task>` | no | no |

No model names in the command: a menu asks every time.

### The model menu

| Step | Role | Options (first = default) |
|--|--|--|
| 1 | plan, verification of every task, finishing on escalation, final fixes | Opus · Fable · Astra |
| 2 | execution and one fix | Sonnet · Haiku · Opus · Sol · Luna · Astra |
| 3 | final review, deploy plan, review of merges with prod | Opus · Fable · Astra |

Rule of thumb: Opus / Sonnet / Opus for normal code; Haiku or Luna for mechanical tasks (translations, identical
edits in N files); Fable when correctness matters more than cost. If you pick the same model for two roles, the
verifier and the escalation shift automatically to the other strong model, and the echo line says so.

### Example 1 - standard run with both gates

    POWER Add a "Return requested" order status to the admin: migration, model, list filter,
    detail page badge, and an ActivityLog entry on change. EN + BG labels.

What happens:

1. The menu appears; you pick Opus / Sonnet / Opus.
2. Echo line: `POWER · план Opus · изпълнение Sonnet · финал Opus · одобрение на плана: да · потвърждение на деплоя: да`.
3. The harness runs in the background. Ask for progress any time, or the session reports about every 15 minutes:

       PYTHONUTF8=1 python "<skill>/power_run.py" status --run-dir <run>

4. Gate A arrives: 6 tasks (migration, model, filter, badge, log, i18n), their contracts, risks, ~22 expected
   agents. You answer `да`.
5. Work runs; Gate B arrives with `REPORT.md` linked and the deploy plan. The session runs `power_run.py deploy`
   (plan + fetch: the backup, the merges with prod, the hard stops and a candidate token). You answer `да`, the
   session runs `power_run.py deploy --go --approved <token>`; if prod changed in between, the token no longer
   matches and the session shows the new candidate before asking again.

### Example 2 - mechanical batch, no plan approval

    POWER START Translate the 14 new FAQ entries in faq/en.json into de, fr, es, it, pl, cs, bg.
    Keep the HTML tags and the product names as they are.

Pick Opus / Haiku / Opus. `START` skips Gate A, but the run still stops on its own if the planner raises a
`QUESTION:` risk or a baseline check is already red. You only see Gate B.

### Example 3 - fully autonomous

    POWER START DEPLOY Fix the 5 PHP 8.3 deprecation warnings listed in storage/logs/php-8.3.txt,
    one task per file, no behaviour change.

No gates, but the hard stops still apply: a BLOCKED task, an unclosed review finding, a failed smoke, any DB / option
/ secrets change, a conflict with prod, or an automatic rollback stops the run and asks you.

### Example 4 - changing the plan at Gate A

At Gate A you can:

- answer `да` - continue;
- give decisions in words ("t3: keep the old column, do not drop it") - the session writes them to
  `request.md` → "Решения на #THE_OPERATOR#" and runs `power_run.py continue --replan`;
- edit `<run>/tasks.json` by hand and then answer `да`.

### Example 5 - partial deploy

If a task ends BLOCKED, Gate B asks explicitly, e.g. "Качвам t1, t2, t4-t6 без t3?". Only PASS tasks that do not
depend on the blocked one (by `depends_on` and contracts) are offered. The blocked task is restored, the reduced diff
is re-checked and reviewed before upload.

### Example 6 - after a crash or a closed terminal

    PYTHONUTF8=1 python "<skill>/power_run.py" continue --run-dir <run> --project-dir <project>

Never restart with `start` (it refuses while a state exists). `continue` resumes where it stopped.

### Example 7 - a broken check command, not broken code

A task ends `CHECK-BROKEN:` (e.g. a typo in a grep assert). Fix the command in `tasks.json`, then re-run only the
check and the verifier:

    PYTHONUTF8=1 python "<skill>/power_run.py" verify --tasks t4:sonnet:1 --run-dir <run> --project-dir <project>
    PYTHONUTF8=1 python "<skill>/power_run.py" continue --run-dir <run> --project-dir <project>

### Where everything lands

Run folder: `<project>/docs/power/<YYYY-MM-DD>-<slug>/`

| File | Content |
|--|--|
| `request.md` | The task verbatim + your decisions |
| `PLAN.md`, `tasks.json` | The plan (human / machine) and "Plan changes" |
| `recon/`, `review/` | Explorer and lens reports |
| `checks/<task>/`, `checks/final/diff.patch` | Script check reports and the full diff |
| `results.json` | Per task outcome (`power_check.py results` prints a table) |
| `REVIEW.md`, `DEPLOY.md`, `deploy.json` | Final review and deploy plan |
| `REPORT.md` | The summary for you |
| `harness/` | `state.json`, `status.json`, `log.txt`, `calls.jsonl` (cost), `gate_a.md`, `gate_b.md` |
| `deploy/` | Prod backups, merge diffs, deploy report |

### Useful environment knobs

| Variable | Default | Effect |
|--|--|--|
| `POWER_MAX_PARALLEL` | 6 | agent processes at once |
| `POWER_MAX_AGENTS` | 0 | cap on the agents one run may start (0 = none); at the cap the run stops with INCOMPLETE tasks, raise it and `continue` |
| `POWER_AGENT_TIMEOUT` | 3600 | seconds per agent before it counts as dead |
| `POWER_BUDGET_SCALE` | 1.0 | multiplies the per-role dollar caps; 0 removes them |
| `POWER_PERMISSION_MODE` | `auto` | `--permission-mode` of every agent |
| `POWER_CLAUDE` | `claude` | path to the CLI (the tests point it at the mock) |

---

## Rules POWER never breaks

Each rule says how it is enforced: **code** = a script refuses, stops or records it and a test covers it; **prompt** =
the agents are told so, a model can ignore it; **external** = the session or you do it. The full table is in
`reference/harness.md` → "What enforces what".

- An agent never touches prod (prompt + orchestration: prod is written only by `power_deploy.py` from the session; the
  approval token is an integrity guard, not actor authorization - see "Trust boundary" in `reference/harness.md`),
  `.env` or secrets (code at plan time and at deploy, prompt during the work), other people's files (code: BLOCKED
  when the agent reports it, SUSPECT hard stop when only the diff shows it; a verify-only pass never clears it) or git
  history (prompt only; the one end-of-run commit is the session's, `SKILL.md` §9, never an agent's).
- The plan is never changed silently (code: every reported `plan_change` of every call is kept in an append-only
  history, each once in `PLAN.md`, the report and `results.json`; reported is not approved).
- The verifier never sees the executor's summary (code: its prompt is built from the task, the diff and the check
  report); a check has not passed without a `power_check.py` report (code).
- Code is never verified or finally reviewed by the model that really wrote it (code, after Codex fallbacks and
  escalation too; no independent strong model left = hard stop).
- No deploy with an unanswered question, even with `START DEPLOY` (code: `--yes N --approved`), and no deploy of a
  candidate you did not see (code: `--approved attempt:hash` over the bytes, backups, `deploy.json` and the effective
  profile; every prod row re-checked before the first write; a hand resolution is sealed into a new candidate).
- A failed required `after` hook, a rollback or an unhealthy watch is never a completed deploy (code: `AFTER_FAILED`,
  `ROLLED_BACK*`, exit 2, phase `deploy_failed`).
- One harness process per run, every Claude agent with a type and a hard call cap (Codex jobs: sandbox + timeout), an
  optional cap on agents per run, no agent without the executables (code).
- Judging a merged diff, running DB `apply` steps after a yes (`watch --db-rollback` runs only their rollback),
  deciding a partial deploy, verifying prod afterwards (external).

---

## Adapting to another user or workspace

This skill was extracted from a working setup. Before the first run:

- **Replace `#THE_OPERATOR#` with your own name.** It marks the human who approves the plan and the deploy. It
  appears in the scripts, the gate messages, `SKILL.md` and `reference/`. Find every place with:

      grep -rn "#THE_OPERATOR#" .

- The gate messages, the menu and the closing lines are in **Bulgarian** (`power_run.py`, `SKILL.md`,
  `reference/gates.md`, `reference/session-path.md`). Translate them if needed. `tests/test_power_run.py` checks one
  of these strings, so update it together with `power_run.py`.
- `SKILL.md` mentions `d:\Claude\` as the workspace root; it only describes where project `CLAUDE.md` files live.
- The tests write to `<system temp>/prun_test` (`POWER_TEST_ROOT` overrides it).
- No secrets, tokens or project data are included.
