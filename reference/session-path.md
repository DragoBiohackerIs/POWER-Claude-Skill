# POWER: the in-session path (fallback)

The protocol as it ran before the harness (13.10.2026): the session itself orchestrates phases 0-6 with Agent
calls and the Workflow `power-execute`. Use it only when `power_run.py` cannot run (`reference/harness.md`, last
section). It costs the session's whole context on every call (measured 32-52% of a run), and the session must be on
Opus (`/model opus`) before anything starts. Everything below is the former SKILL.md core, unchanged.

# POWER mode (session path)

Protocol for a large task with maximum parallel work and minimum intervention from #THE_OPERATOR#. One skill for all projects
under `d:\Claude\`; project specifics come from `## POWER` in the project's `CLAUDE.md`. This skill is the explicit
opt-in for the **Workflow** tool. This file is the core (read it whole); the details are in `reference/` and are
read only in the phase they belong to:

All messages to #THE_OPERATOR# are in Bulgarian; file contents, plans and reports follow the project rules.

| File | When to read it |
|--|--|
| `reference/plan.md` | phase 1: what an executable plan is, a large plan by areas, the agent budgets |
| `reference/workflow.md` | phase 2-3: what `power_workflow.js` does, statuses, BLOCKED cases |
| `reference/deploy.md` | phase 6: `power_deploy.py` step by step, rollback, hard stops |
| `reference/gates.md` | Gate A/B: the table for #THE_OPERATOR# and the `/compact` hint |
| `reference/codex-config.md` | on a Codex step; on a new project: the `## POWER` json block |
| `reference/setup.md` | new machine or `install --check` reports missing settings; sharing the skill |

`<skill>` = the folder of this file. Python always with `PYTHONUTF8=1`, Bash with timeout 600000, everywhere
`--run-dir <run> --project-dir <project>`. Scripts: `power_check.py` (plan, snapshot, baseline, check, restore,
results, config, install), `power_prompts.py` (the single source of agent instructions; `write`, `show`),
`codex_task.py` (Codex jobs; `models`, `ready`, `workflow-args`, `usage`, `status`), `power_workflow.js`
(Workflow `name: "power-execute"`), `power_deploy.py`, `power_cost.py` (measured cost), `agents/*.md`.
First run on a machine: `python "<skill>/power_check.py" install`, then the settings in `reference/setup.md`
(`install --check` lists what is missing; say so in the echo line if anything is).

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

**The session runs on Opus.** The orchestrator waits, reads and summarizes, and on Fable the same costs 2.5× against
the limit. If the session is on Fable, the first message is only (in
Bulgarian): „Сесията е на Fable, POWER върви на Opus: пусни `/model opus` и напиши „давай"." and nothing starts until
the switch (#THE_OPERATOR#'s decision); it continues on Fable only if #THE_OPERATOR# explicitly says so, and the echo line notes it.

## 2. Models: three steps

| Step | Role | Options, first is default |
|--|--|--|
| 1 | plan, verification of every task, finishing on escalation, the fixes from the final review | Opus · Fable · Astra |
| 2 | execution and one fix | Sonnet · Haiku · Opus · Sol · Luna · Astra |
| 3 | final review, DEPLOY.md + `deploy.json`, review of the merge with prod | Opus · Fable · Astra |

- `opus`, `sonnet`, `haiku`, `fable` point to the latest version. Astra (frontier), Sol (workhorse) and Luna (cheap)
  are Codex families
  (#THE_OPERATOR#'s ChatGPT plan, outside the Claude limit); `codex_task.py models` gives the slugs, never write them by hand.
- No choice: recon and lenses - Sonnet `effort: "low"`; script checks - haiku runner; prod verification - Sonnet.
- **Agent types** (`agents/`, `subagent_type` in Agent / `agentType` in Workflow): `power-runner` (Bash, haiku),
  `power-reader` (recon, lenses), `power-verifier` (verifier, final verification), `power-executor` (plan,
  execution, fixes, finishing, final review). No CLAUDE.md and memory in context (`omitClaudeMd`), with a hard
  `maxTurns`: start 5k tokens instead of 62k, and the prefix is resent on every call (it was 71% of the agent
  input before). Project rules reach the agents via `rules` in the plan. The generic type
  `general-purpose` only if `install` has not run (`config` says).
- **Who verifies whom.** Code is never verified by its author: step 1 verifies; if step 1 is the author -
  Opus; if Opus is also the author - Fable. If step 2 is the same model as step 1, escalation goes to the other strong
  model (Opus → Fable, Fable → Opus, Astra → Opus); if step 2 is the same as step 3, the final shifts by the same
  table. Automatic, the echo line states it, #THE_OPERATOR# can override it with one word.

| 1 / 2 / 3 | Task verification | Escalation finishes | Verification after escalation | Final |
|--|--|--|--|--|
| Opus / Sonnet / Opus | Opus | Opus | Fable | Opus |
| Fable / Opus / Fable | Fable | Fable | Opus | Fable |
| Opus / Astra / Fable | Opus | Opus | Fable | Fable |
| Astra / Sol / Opus | Astra | Astra | Opus | Opus |
| Opus / Opus / Opus | Fable | Fable | Opus | Fable |

- **If Codex fails** (limit, crash, timeout): Sol → Sonnet, Luna → Haiku, Astra → Opus; verifier Astra → Opus (Fable if Opus
  is the author). The script logs it, the result carries `author` and `verified_by`; many failures at once = limit used up.
- **Call cap** in every prompt (`BUDGET` in `power_prompts.py`): recon 25 hard, plan ~20, task
  ~25 with stop at 40 (`PLAN_MISMATCH` „BUDGET: …"), fix 20/30, finishing 30/45, verification 15, lens 15, final
  review 30/40. Cost grows with the square of the agent's length; a task that hits the cap was sized wrong in the plan:
  escalation finishes it once, on a second hit it is BLOCKED and the plan splits it.

## 3. Phase 0: intake

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
3. **In the same message:** `<run>` = `<project>/docs/power/<YYYY-MM-DD>-<slug>/`, `<run>/request.md` (the task
   verbatim), and in background 1-4 recon agents (`power-reader`, `model: "sonnet"`, `effort: "low"`; each ~1M
   tokens, one for a small task), one focus each: `python "<skill>/power_prompts.py" write … --kinds recon --item
   "<name>=<what to map>"`, the agent gets "Read <run>/prompts/recon.<name>.md and follow it" and writes
   `<run>/recon/<name>.md` (if it returns the map as its answer, write it yourself).
4. **Echo line** after the menu: `POWER START · план Opus · изпълнение Sonnet · финал Opus · одобрение на плана: не ·
   потвърждение на деплоя: да`, with the shifts per §2 („ескалация Fable (автоматично: Opus е и изпълнител)"). It goes
   at the top of PLAN.md and REPORT.md.
5. Read the project's `CLAUDE.md`, `python "<skill>/power_check.py" config --project-dir <project>` (short output;
   `--full` for the whole json), TODO and the deploy docs.
   The answers from item 1 and every later correction from #THE_OPERATOR# („не пипай X") go into `request.md`, section „Решения на #THE_OPERATOR#":
   the planner reads the file, and after `/compact` the session reads it instead of remembering.
6. CLAIM, if the project has one (`claim` in `## POWER`; for example `.project/SESSIONS.md`); files are added after the plan.

## 4. Phase 1: plan

1. Wait for recon, `power_prompts.py write … --kinds plan`, then the planner: Opus/Fable - Agent
   `subagent_type: "power-executor"`, `model` of step 1, "Read <run>/prompts/plan.md and follow it"; Astra -
   `codex_task.py plan … --model <slug>`, repeat on `RUNNING`/`QUEUED`, on `FAILED` Opus plans (say so).
   Never the session itself: everything read stays in its context until the end. Writes `PLAN.md` + `tasks.json`.
   A risk "RECON: …" in the plan = one more recon agent with that focus and the plan is extended; the planner does not map by itself.
   Large plan (7+ tasks in 2+ areas): skeleton + areas in parallel, `reference/plan.md`.
2. `power_check.py prepare` until it passes (normalizes, validates: file owner, forbidden files, anchors,
   contracts, cycles, size, check time up to 540 s, `/dev/null` as an argument). `QUESTION:` risks are asked
   as in §3 item 5. What an executable plan is and why: `reference/plan.md`.
3. `power_check.py baseline` in background (only `tasks.json`): PRE-BROKEN (must pass, fails before the work), WEAK
   (passes before the work, proves nothing), SUSPECT (the command itself crashes). For each: fix the plan,
   `"known_broken": true` + a line in PLAN.md, or a question.
4. CLAIM: add the files.
5. **Gate A** (only without `START`): the table from `reference/gates.md`, wait for „да". Changes → `tasks.json` →
   `prepare` → `baseline`. Above 150k context: the `/compact` hint from there.
6. **Right before the Workflow** (snapshot as fresh as possible): `codex_task.py ready --step1 <…> --step2 <…>` =
   prepare → snapshot → prompts → `args` (with `START` add `--baseline`: item 3 is then skipped and `ready` is the whole
   preparation). `snapshot --force` only if the plan changed after snapshot and work has not started.

## 5. Phases 2-3: work and verification in one Workflow

Workflow: `name: "power-execute"` and `args` from `ready`/`workflow-args` (refuses "not ready" without snapshot/prompts).
Only if the name is unknown - read `power_workflow.js` and pass it as `script` (~4.5k output tokens). On a
Codex step: `codex_task.py usage --log "<run>/codex-usage.jsonl"` before and after only when #THE_OPERATOR# wants the quota
snapshot (two real `codex exec`, PERF-03); otherwise say in the summary that it was not fetched. The "Read
<run>/prompts/…" messages below are the in-session form; the harness passes the rendered prompt text in the message
itself (PERF-04), which an Agent call may do too when the context allows.

In short (details in `reference/workflow.md`): no waves - a task starts as soon as its dependencies are PASS; per task:
executor → `power_check.py check` via runner → verifier with clean context (sees the task, the contracts,
the diff and the report, not the author's summary) → one fix → checks → verifier → on the second return (round) step 1
finishes → checks → verifier → otherwise BLOCKED. Stop rule: anchors/files/contracts are checked
before the first edit, mismatch = `PLAN_MISMATCH` (restore to the snapshot, step 1 fixes the plan and
finishes). A red script check goes to a fix without the expensive model. `INFRA:` and `CHECK-BROKEN:` (broken
check command, not code) = BLOCKED without a fix, the orchestrator fixes the cause and runs only check +
verifier: `workflow-args … --verify <id>:<author>:<round>` → `mode: "verify"` (the task's earlier row of `results.json`
travels as `previous` and the verdict is merged into it; a reported stray file stays BLOCKED unless `tasks.json` now
lists it).

After the Workflow: the result file → `<run>/results.json`; read it with `power_check.py results` (table, not JSON);
every `plan_change` → section "Plan changes" of PLAN.md. Budget: 3 agents per task, up to 10 on escalation,
guideline up to 50 per Workflow - if the plan exceeds it, say so at Gate A and in the summary.

## 6. Phase 4: final review (step 3)

1. `power_check.py check --all --round final` (all checks on the final state, the whole diff in
   `<run>/checks/final/`), and **at the same time** the lenses: `power_prompts.py write … --kinds lens --item "<name>=<focus>"`,
   one Agent `power-reader` (Sonnet, `effort: "low"`) per lens: "Read <run>/prompts/lens.<name>.md and follow it".
   The lenses are `lenses` from `## POWER` (may have `agent_type`, e.g. `php-reviewer`); without them: `domain` (the traps from
   CLAUDE.md), `smoke` (local smoke + desktop and mobile screenshots on UI change), `i18n` (only if the run touches
   text in several languages). They write `<run>/review/<name>.md`.
2. `power_prompts.py write … --kinds review`, then Agent `power-executor` with `model` of step 3 ("Read
   <run>/prompts/review.md and follow it") or `codex_task.py review --model <slug>`; if it is the same model as the
   executor - the shifted one per §2. Confirms every finding against the code (lenses make mistakes). Writes `REVIEW.md`,
   `DEPLOY.md`, `deploy.json`, `review.json`. Immediately `power_deploy.py plan` (validates); `fetch` may start here.
3. Critical/high/medium findings are fixed by step 1: `write --kinds finalfix,finalverify` → Agent `power-executor`
   per `finalfix.md` (all files of the run, nothing else) → `check --all --round final2` → Agent `power-verifier`
   per `finalverify.md` by the "not the author" rule. Still fails - BLOCKED in the report. Low - in the report.

## 7. Phase 5: summary and Gate B

`REPORT.md` in the run and briefly in chat: the echo line and the models (with slugs); per task status, author,
verifier, rounds, escalation, `plan_change`, open_issues (`power_check.py results`); the changed files
(absolute links) and `checks/final/diff.patch`; the evidence (`checks/`, baseline decisions); changes outside
the run's files; BLOCKED and the questions; Codex `usage` before/after (when fetched) and `codex_task.py status`; the measured cost -
the `AGENTS:` and `MAIN LOOP:` lines from `python "<skill>/power_cost.py"` (`--since HH:MM` for a run that started later in
the session); the deploy plan, the risks and what is checked manually.

**Partial deploy** (there is a BLOCKED): the PASS tasks with no dependency on BLOCKED (`depends_on` and contracts);
`restore --task <id> --keep blocked` for BLOCKED, `check --tasks <PASS> --round partial` + local smoke, step 3
reviews the reduced diff. The question is always explicit, also with `DEPLOY` (in Bulgarian): „Качвам t1, t2, t4-t6 без t3?"

**Gate B** (only without `DEPLOY`): „#THE_OPERATOR#, трябва да решиш дали да деплойна: …" and wait; before it the
`/compact` hint per `reference/gates.md`, if context is above 150k.

## 8. Phase 6: deploy

Step 3 plans it (`DEPLOY.md`, `deploy.json`), `power_deploy.py` uploads, the orchestrator decides. Without a deploy profile in
`## POWER` - `DEPLOY` is ignored, ask, deploy is manual per the project docs, still only by the
orchestrator. **Only the orchestrator mutates prod, serially**, never an agent; `--go` only after Gate B or with `DEPLOY`
and no hard stop. Order: `plan` → `fetch` (prod-before = backup; clean/same/new/merged/conflict; step 3 reviews
every merged diff; the fetch prints the candidate token) → `apply --go --approved <attempt:hash> [--claimed-by <topic>]`
(canary before, upload in order, byte-verify, the `after` hooks - a failed required one is `AFTER_FAILED`, settle,
canary after; automatic rollback only on a certain failure) → DB/options/flags by hand after an explicit „да" → `watch --go
--db-rollback` → `prod-verifier` (Sonnet, read-only, the project's pace) against the local screenshots → „#THE_OPERATOR#, всичко е
изпълнено и проверено - …" with a link to `deploy/REPORT.md`. Details, statuses and rollback: `reference/deploy.md`.

**Hard stops that beat `DEPLOY`** (summary + question): BLOCKED task (only the partial deploy question);
an unclosed issue from the final review; failed local smoke; any DB/option/flag step, change to orders,
customers, `.env`, `wp-config.php`, secrets; `conflict` or `diverged-no-base` at fetch; binary without backup; canary
failed already before the upload; automatic rollback.

## 8a. Phase 7: commit (default)

After the run is done (Gate B without deploy, `watch` HEALTHY with one) the **session** makes one git commit per
touched repo: only the plan's files plus `docs/power/<run>/`, message `power(<slug>): …` from REPORT.md, never
`push`/`-A`/other CLAIMs' files, never with BLOCKED or a conflict. Off only with „без комит" in the task line.
SKILL §9.

## 9. Never in POWER

- An agent does not touch prod, `.env`, secrets, other people's files, git history (commit, stash, checkout, reset); the one end-of-run commit is the session's (§8a).
- The plan is not changed silently: every change is in the file, in "Plan changes" and in the report.
- The verifier does not get the executor's summary; a check has not "passed" without a report from `power_check.py`.
- Code is not verified by the model that wrote it.
- No deploy with an unanswered question, even with `START DEPLOY`.
- No agent without a call cap or without a type from `agents/`; no planning or review in the session itself; no reading of whole modules.
- Everything independent runs in parallel (recon with the menu, baseline while #THE_OPERATOR# reads the plan, the tasks, the lenses,
  fetch during the review). Sequential only for a shared resource: the prod upload (order + clean rollback),
  requests to prod (WAF pace), checks with `serial: true` (lock), Codex up to 6 processes.
