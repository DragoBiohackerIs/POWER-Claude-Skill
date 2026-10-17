#!/usr/bin/env python3
"""Instructions for the agents of a POWER run: one source for Claude agents and Codex jobs.

  python power_prompts.py write --run-dir R --project-dir P --kinds plan,tasks,areas,review
  python power_prompts.py write --run-dir R --project-dir P --kinds finalfix,finalverify   (after review.json)
  python power_prompts.py write --run-dir R --project-dir P --kinds recon --item "name=what to map"
  python power_prompts.py write --run-dir R --project-dir P --kinds lens --item "name=what to look for"
  python power_prompts.py show  --run-dir R --project-dir P --kind work --task t1 [--engine codex]

`write` renders files under <run>/prompts/ for Claude agents, who get a one-line message
"read <file> and follow it". Dynamic parts (round, problems, history, evidence paths) are not in
the files: the message that starts the agent carries them. codex_task.py renders the same
templates with engine=codex and fills the dynamic parts itself.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from power_check import DEFAULT_DENY, PlanError, load_config, load_plan, out, read_json, write_text  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Claude agents run as the skill's own agent types (agents/*.md) without CLAUDE.md or memory in their context:
# the project rules reach them through the task's "rules". A read of PLAN.md or CLAUDE.md is paid again on every
# later call of the agent, so it is the exception, not the default. Codex reads AGENTS.md itself.
HEADER = {
    "claude": """POWER run - project {project_dir}, run directory {run_dir}.
Everything you need is below. Do not read PLAN.md, tasks.json or CLAUDE.md unless the spec points you to a
section of them; then read that section as a range, not the file.""",
    "codex": """POWER run - project {project_dir}, run directory {run_dir}.
The full plan is {run_dir}/PLAN.md; read the parts you need. Read the project's AGENTS.md or CLAUDE.md (in the
project folder or a parent folder) first.""",
}

CHECK_RULE = """A mechanical check can be broken itself: when its failure is a tool error (curl exit 23/6/7 or "curl: (N)",
"command not found", a shell syntax error, a path this shell cannot write) it is not a verdict on the code, and
the check script marks such failures "CHECK-BROKEN:". Never change code to satisfy a broken check. If every
failure left is CHECK-BROKEN, stop and return status PLAN_MISMATCH with mismatch starting with "CHECK:" (the
check's name and why it is broken): your edits stay and the orchestrator fixes the plan. Otherwise fix the real
failures and list the broken checks in open_issues."""

HARD_LIMITS = """Hard limits: no production access of any kind (no FTP/SSH/cPanel, no prod SQL, no deploy, no requests to
live sites); never touch .env files or secrets; never run git commit, push, stash, checkout, reset or restore;
never write into the run directory {run_dir}. Other agents work on other files at the same time: never fix,
revert or reformat a file outside your list."""

# Call budgets. Cost grows with the square of an agent's length: every call re-sends everything read so far,
# so an agent with 120 calls and a 500k context re-reads 40M tokens (measured 2026-10-12: $11-17 each, 58% of
# all agent spend came from agents over 50 calls). Short agents with ranges instead of whole files are the lever.
READ_RULE = ("Read ranges, not files: grep for the symbol, then read the lines around the hit; never read a whole "
             "directory, vendor code, or a file over ~300 lines end to end.")
BUDGET = {
    "work": "Budget: this task is sized for about 25 tool calls. " + READ_RULE + " If you pass 40 tool calls, stop: "
            "return status PLAN_MISMATCH with mismatch starting with \"BUDGET:\" - what is done, what is not, and why "
            "the task is bigger than planned. Do not keep exploring.",
    "fix": "Budget: about 20 tool calls; the problems are named with file:line, go straight to them. " + READ_RULE +
           " If you pass 30, stop and return PLAN_MISMATCH with mismatch starting with \"BUDGET:\" and what remains.",
    "finish": "Budget: about 30 tool calls. " + READ_RULE + " If you pass 45, stop and return PLAN_MISMATCH with "
              "mismatch starting with \"BUDGET:\": the task must be split in the plan, not finished by exploring.",
    "verify": "Budget: at most 15 tool calls. The evidence (the diff and the check report) is already in your "
              "message; open the code only around the changed lines. " + READ_RULE + " Past 15, stop and give the "
              "verdict on what you have seen, naming in unchecked what you did not check.",
    "finalfix": "Budget: about 30 tool calls; the findings are named with file:line. " + READ_RULE + " Past 40, "
                "stop and return PLAN_MISMATCH with mismatch starting with \"BUDGET:\" and what remains.",
    "finalverify": "Budget: at most 20 tool calls; the diff and the reports are your evidence, open the code only "
                   "around the fixed lines. " + READ_RULE + " Past 20, give the verdict on what you have seen and "
                   "name in unchecked what you did not check.",
}

ENGINE = {
    "claude": "You may use the local Docker containers and the local sites (127.0.0.1) to test.",
    "codex": ("Your sandbox has no network and no Docker. Verify what you can locally (re-read your diff, syntax "
              "checks with local tools); a script with Docker access runs the mechanical checks after you."),
}

STOP_RULE = """STOP RULE - before your first edit, check the plan against the code:
- every anchor above exists;
- every file marked edit or delete exists, and every file marked create does not exist yet;
- the contracts you use exist, or another task provides them as written;
- the task can be done inside your file list.
If anything does not match, do NOT improvise and do NOT edit: return status PLAN_MISMATCH and write in
mismatch what the plan says and what the code has, with file:line. Shifted line numbers and cosmetic
differences are not a mismatch. If you hit a mismatch midway, stop at once and return PLAN_MISMATCH; your
partial edits are rolled back from the snapshot."""

RESULT = """Return: status (DONE or PLAN_MISMATCH), files_changed (project-relative paths), mismatch ("" when DONE),
summary, how_verified (what you ran and what you saw; say plainly what you could not run), open_issues,
plan_change ({plan_change})."""

WORK = """You are the EXECUTOR of one task in a POWER run.
{header}

{task}

{stop_rule}

Then implement the task fully, inside your files only. Before you return, run the mechanical checks you can
and fix what fails. {engine}

{check_rule}

{limits}

{result}"""

FIX = """You are the EXECUTOR of this task and your previous attempt was rejected.
{header}

{task}

{problems}

Fix every problem without breaking the other acceptance criteria, then run the mechanical checks you can. The
STOP RULE still applies: if a problem cannot be fixed because the plan itself is wrong, return PLAN_MISMATCH
and explain it in mismatch. {engine}

{check_rule}

{limits}

{result}"""

FINISH = """You are the PLANNER model of this POWER run, and you finish this task yourself because the executor did not.
{header}

{task}

{history}

Make every acceptance criterion hold, then run the mechanical checks you can. If the plan was wrong for this
task, you may correct it (spec, acceptance, a contract detail) and implement the corrected version; describe
the correction in plan_change ("" if none). You still own only this task's files. If finishing needs other
files or a decision of the owner, do not improvise: return PLAN_MISMATCH with mismatch starting with
"QUESTION:". {engine}

{check_rule}

{limits}

{result}"""

VERIFY = """You are an independent VERIFIER in a POWER run. You did not write this change, and on purpose you get no
summary from its author: judge only the change itself and the evidence.
{header}

{task}

{evidence}

Read the diff and the code around it. {verify_engine}
Check every acceptance criterion, every non-goal (nothing listed there may change), the exact names and
signatures of the contracts, the project rules and the review focus. The mechanical checks passed; look for
what they cannot see. If the diff touches anything the task does not own, or the check report lists changes
outside the run's files that look like this task's doing, that is a problem. A hunk in the task's own files
that no line of the spec or acceptance explains is a problem too (another agent may have strayed into this
file): report it, never ignore it.
pass = true only if every criterion is demonstrably met by the change. problems: one per item,
"file:line - what is wrong - what is expected". plan_problem = true only if the task cannot meet its
acceptance as planned (the plan is wrong, not the code). notes: anything the owner should know ("" if none).
{unchecked_rule}

{limits}"""

UNCHECKED_RULE = """unchecked: what you did not check - a criterion, a file, a case, a page you could not run - at most 5
items, each naming the file it is about; [] only when you checked everything. The owner sees this list before the
deploy, so an honest gap is worth more than a silent one."""

VERIFY_ENGINE = {
    "claude": ("You may run read-only commands (the checks, tests, php -l, curl or screenshots of the LOCAL "
               "site), but never edit, create or delete a file, and do not re-run checks marked [serial] "
               "(they write to shared local state)."),
    "codex": "You are in a read-only sandbox without network or Docker.",
}

FINAL_FIX = """You are the PLANNER model of this POWER run, fixing the findings of the final review.
{header}

The final review ({run_dir}/REVIEW.md, {run_dir}/review.json) found these problems in the combined change
({run_dir}/checks/final/diff.patch):
{findings}

Fix every finding of severity critical, high and medium. Unlike the executors, you may edit any file of the
run (the files listed in {run_dir}/tasks.json) - but no other file, and nothing on the never-in-a-plan list.
Keep the acceptance criteria and non-goals of every task. Run the mechanical checks of the tasks you touched.
If a finding needs a file outside the run or a decision of the owner, do not improvise: leave it and say so.
{engine}

{check_rule}

{limits}

Return: status (DONE, or PLAN_MISMATCH if a finding cannot be fixed inside the run's files), files_changed,
mismatch (what could not be fixed and why, "" when DONE), summary (what you changed per finding), how_verified,
open_issues, plan_change ("" or what you changed in the plan's intent)."""

FINAL_VERIFY = """You are an independent VERIFIER of the fixes after the final review of a POWER run. You did not write them.
{header}

The findings that had to be fixed:
{findings}

Evidence: the diff of all the run's files against the snapshot {run_dir}/checks/final2/diff.patch and the
mechanical re-check {run_dir}/checks/final2/report.md (compare with {run_dir}/checks/final/ before the fixes).
Read the diff and the code around it. {verify_engine}
pass = true only if every listed finding is demonstrably fixed and nothing else broke (acceptance criteria and
non-goals in {run_dir}/tasks.json still hold). problems: one per item, "file:line - what is wrong - what is
expected". plan_problem: false unless a finding cannot be fixed as planned. notes: for the owner ("" if none).
{unchecked_rule}

{limits}"""

PLAN_RULES = """- Small tasks: one concern each, at most about 6 files and 300-400 changed lines (translations and generated
  data do not count), each verifiable on its own. Merge trivial tasks: every task costs 2-3 agents.
- The same small edit in many files (translations, per-language catalogs, one line in N modules) is ONE task
  that owns all those files, never one task per file: one executor does eight 3-line edits in one go and one
  verifier reads them at once. Split by file only when the files carry real, different logic.
- files: the closed list of files the task may change, each with action edit | create | delete. A file belongs
  to exactly one task. Files that many tasks want (registries, manifests, shared CSS, a bootstrap file,
  translation catalogs) form their own task or come last via depends_on.
- Never in a plan: {deny}.
- anchors: for every file marked edit or delete, 1-3 literal snippets that must exist before the work (a
  function signature, a hook name, a unique line). The executor stops if one is missing.
- contracts: every interface between tasks (function and method signatures, hooks and filters, option keys,
  CSS classes, DB columns, i18n keys, routes), written exactly; each one is provided by one task (provided_by)
  and listed in provides / uses of the tasks.
- depends_on: task ids, or contract ids (a contract id means "after the task that provides it"). Use it only
  when this task's checks need the other task's working code; otherwise code against the contract in parallel.
- non_goals: behaviour, markup, APIs or data that must not change.
- acceptance: criteria that can be checked without the owner.
- checks: commands a script runs after the task, from the project directory, in Git Bash with
  MSYS_NO_PATHCONV=1: name, cmd, expect_exit (-1 = any), contains, not_contains, before (fail | pass | any -
  what the check does BEFORE the work; "fail" for a check that proves the new behaviour), serial (true if it
  writes to a shared local DB or state), timeout (seconds, default 120; all checks of one task must fit in
  540 s together), retries (only for known-flaky checks). Prefer checks that fail before the work and pass
  after it: the script runs every check once before the work and flags the ones that cannot prove anything.
  Never pass /dev/null as an argument to a program (under MSYS_NO_PATHCONV=1 curl -o /dev/null exits 23):
  redirect in the shell (> /dev/null) or print only what you need (curl -s -w '%{{http_code}}').
- review_focus: what the verifier must look at beyond the checks (edge cases, security, visuals).
- rules: the project pitfalls this task must know, written out (executors do not see CLAUDE.md), including the
  environment traps it will meet (shell, encoding, container paths, local URLs, rate limits). Rules that every
  task shares go once in the top-level "rules" of the plan; a task's own rules hold only what is specific to it.
- size: estimated files and changed lines.
- If something is ambiguous, still plan, and add every open question to risks starting with "QUESTION:"."""

TASK_SHAPE = """{"id": "t1", "title": "...", "spec": "...",
 "files": [{"path": "includes/x.php", "action": "edit"}],
 "anchors": [{"path": "includes/x.php", "text": "function x_init("}],
 "provides": ["c1"], "uses": [], "non_goals": ["..."], "acceptance": ["..."],
 "checks": [{"name": "lint", "cmd": "...", "expect_exit": 0, "contains": [], "not_contains": [],
             "before": "any", "serial": false, "timeout": 120, "retries": 0}],
 "review_focus": "...", "rules": "...", "size": {"files": 1, "lines": 80}, "depends_on": []}"""

PLAN = """You are the PLANNER of a POWER run: a big task split into small tasks that parallel executors implement; each
task is then checked by a script and by an independent verifier.
Project: {project_dir}. Run directory: {run_dir}.
First read the project rules (CLAUDE.md / AGENTS.md in the project and its parent folders; they are not in
your context), the TODO and deploy docs they point to, and the recon notes in {run_dir}/recon/ (written by
explorers). Plan FROM the notes:
open the code only to confirm an anchor, a signature or a fact the notes do not give, as ranges around grep
hits, within about 20 tool calls. If the notes do not cover an area, do not map it yourself: put it in risks as
"RECON: <what is missing>" so the orchestrator runs another explorer. Do not change any project file.

The task from the owner (with the answers to the clarifying questions, if any):
<<<
{request}
>>>

Write an EXECUTABLE plan. Rules:
{rules}

Mode: if the work needs 7 or more tasks in 2 or more independent areas, use mode "skeleton": goal, scope,
non-goals, contracts (provided_by = the name of the area), areas (name, owns = the files or folders the area
owns exclusively, covers = what it does, est_tasks) and NO tasks; the areas are then detailed in parallel.
Otherwise use mode "full" with all tasks.
plan_md: the plan for people, in the language of the request: goal, scope, what is NOT touched, contracts,
deploy plan (files, order, commands per the project deploy docs), risks, then one section per task (per area in
skeleton mode). Ids, paths and commands stay as they are.

{output}"""

PLAN_OUT = {
    "claude": """Write {run_dir}/PLAN.md (plan_md) and {run_dir}/tasks.json:
{{"mode": "full", "goal": "...", "rules": "<the rules every task shares>",
 "contracts": [{{"id": "c1", "text": "...", "provided_by": "t1"}}], "risks": ["..."], "tasks": [<task>, ...]}}
where a task looks like
{task_shape}
In skeleton mode write {run_dir}/skeleton.md and {run_dir}/skeleton.json instead:
{{"mode": "skeleton", "goal": "...", "scope": "...", "out_of_scope": "...", "deploy_plan": "...", "risks": [],
 "contracts": [...], "areas": [{{"name": "admin", "owns": ["src/Affiliate/"], "covers": "...", "est_tasks": 4}}]}}
Write only these files.""",
    "codex": "Return the plan as the structured output; the script writes the files.",
}

AREA = """You are an AREA PLANNER of a POWER run. The skeleton of the plan (goal, contracts, areas) is in
{run_dir}/skeleton.json and {run_dir}/skeleton.md. Detail ONLY area "{name}": {covers}
It owns exclusively: {owns}. Use only files inside it.
Project: {project_dir}. Plan from the recon notes in {run_dir}/recon/; open the code only to confirm anchors
and signatures, as ranges, within about 20 tool calls ("RECON: ..." in risks for what the notes lack). Do not change any
project file.

Rules for the tasks:
{rules}

Task ids: {prefix}1, {prefix}2, ... (they are renumbered when the areas are merged). A dependency on another
area is written as the id of the contract that area provides.
If the skeleton does not fit the code of your area (a contract cannot be implemented as written, a needed file
is outside your area), do not improvise: return mismatch describing it, with file:line, and no tasks.
section_md: the part of the plan for your area, in the language of the plan.

{output}"""

AREA_OUT = {
    "claude": """Write {run_dir}/plan-areas/{name}.json:
{{"mismatch": "", "tasks": [<task>, ...], "section_md": "..."}}
where a task looks like
{task_shape}
Write only this file.""",
    "codex": "Return the result as the structured output; the script writes the file.",
}

SEVERITY = """Severity rubric (by the effect, not by the size of the fix): critical or high = wrong money, stock or
order status, an auth or role leak, injection, data loss, or a regression on every page or for a whole role;
medium = a wrong result in a realistic path; low = cosmetic or hypothetical."""

REVIEW = """You are the FINAL REVIEWER of a POWER run. Read the owner's request {run_dir}/request.md (with its section
"Решения на #THE_OPERATOR#" when it has one), {run_dir}/PLAN.md (its non-goals too), the full diff
{run_dir}/checks/final/diff.patch, the mechanical re-check {run_dir}/checks/final/report.md, the parallel review
reports in {run_dir}/review/, the task results in {run_dir}/results.json, and the code around the changes.
Look at the change as a whole: interactions between tasks, the names and signatures of the contracts,
duplicated logic, a forgotten registry, manifest or translation, anything outside the plan, the non-goals. The
review reports can contain false positives: confirm each one against the code before you keep it. A lens finding
you keep goes to findings with the severity of the rubric below (it may differ from the lens's); one you throw
out goes to dismissed with the reason and the evidence (file:line, or the code that disproves it) - never drop
one silently.
{severity}
Request coverage: split the request and the owner's decisions into at most 10 items and set each against the
change: done, partial, missing, or out_of_scope_by_plan (a non-goal or a decision in PLAN.md leaves it out on
purpose); where = the file:line or the task that covers it, or what is absent. A missing item goes to the planner
as a medium finding by itself: do not repeat it in findings.
Budget: about 30 tool calls - the diff, the reports and the code around the changed lines as ranges, not the
modules in full. Past 40, stop and write the review on what you have seen, listing what you did not check.
Then write the deploy plan from the project deploy docs and the `## POWER` deploy profiles of the project
(`PYTHONUTF8=1 python "{skill_dir}/power_check.py" config --project-dir "{project_dir}" --full` prints the
merged block; the deploy docs are the ones CLAUDE.md names, read as ranges): which run files go to prod (dev-only files, tests and docs stay), in upload order, the
`after` hooks (cache / opcache commands; REQUIRED unless marked optional - a failed required hook is AFTER_FAILED,
the deploy is not done; they run after a rollback too, as recovery), the canary URLs (the affected pages, not only
the home page: a canary that is 5xx / unreachable / fatal after the upload and was fine before rolls the deploy
back; one that is down before the upload stops it), the verification URLs with a marker each, every DB / option /
flag step with its apply AND rollback command (these are never run by the script: the owner says yes first), risks,
and what needs the owner's explicit yes.
Do not change any project file.

{output}"""

DEPLOY_SHAPE = """{"profile": "<a profile name from the ## POWER deploy profiles>",
 "files": [{"local": "<project-relative path, as in tasks.json>", "order": 1, "note": "..."}],
 "after": ["<required cache command; {urls} = the edge verify urls>", {"cmd": "<warning-only command>", "optional": true}],
 "canary": ["https://.../"], "verify": [{"url": "https://...", "contains": "<marker>", "via": "edge"}],
 "db": [{"description": "...", "apply": "<command>", "rollback": "<command>"}],
 "needs_yes": ["..."], "risks": ["..."]}"""

REVIEW_OUT = {
    "claude": """Write {run_dir}/REVIEW.md (the review, in the language of the plan), {run_dir}/DEPLOY.md (the deploy plan for
people), {run_dir}/deploy.json (the same plan for power_deploy.py):
{deploy_shape}
and {run_dir}/review.json: {{"verdict": "PASS | PASS_WITH_FIXES | FAIL", "findings": [{{"severity":
"critical|high|medium|low", "file": "...", "line": 0, "problem": "...", "fix": "..."}}], "coverage": [{{"request_item":
"...", "status": "done|partial|missing|out_of_scope_by_plan", "where": "..."}}], "dismissed": [{{"lens": "...",
"file": "...", "line": 0, "severity": "the lens's severity", "reason": "...", "evidence": "..."}}]}} (an empty
list when there is nothing to put in it, never a missing key). Write only these files.""",
    "codex": ("Return verdict, findings, coverage, dismissed, review_md, deploy_md and deploy (the machine-readable deploy "
              "plan) as the structured output; the script writes the files."),
}

RECON = """You are an EXPLORER for a POWER run. Map this part of the code for the planner: {focus}
Project: {project_dir}. The task from the owner is in {run_dir}/request.md. The project's CLAUDE.md is not in
your context: if a command needs an environment rule (shell, encoding, container paths), read that one section
of {project_dir}/CLAUDE.md as a range.
Budget: the smallest map that answers the question, within a HARD limit of 25 tool calls. Search first
(grep/glob), then read only the ranges around the hits; never whole directories, vendor code or a file over ~300
lines end to end; no requests to live sites unless the question cannot be answered from the code, and then at
most one anonymous GET. At 25 calls stop and write what you have, ending with a line "Unmapped: ..." for what
you did not reach - the planner will ask for a second map if it needs one.
Write {run_dir}/recon/{name}.md: the relevant files with one line each, the key functions, hooks and classes
with file:line and their exact signatures, how the pieces connect, the existing tests and checks, and the traps
you see. Facts only, with file:line; no proposals. Do not change any other file. If you cannot write files,
return the whole map as your answer instead.
Otherwise return five lines: what you mapped and the most important facts."""

LENS = """You are a REVIEW LENS "{name}" in the final review of a POWER run: {focus}
Read {run_dir}/PLAN.md and the full diff {run_dir}/checks/final/diff.patch, then the code around the changed
lines as ranges, within 15 tool calls; past 15, write what you have and name what you skipped. The project's
CLAUDE.md is not in your context: for an environment rule read that one section of it as a range.
Write your findings to {run_dir}/review/{name}.md, one per item: "severity (critical|high|medium|low) - file:line
- problem - suggested fix", or "No findings.".
{severity}
End the file with one line "Skipped: <what you did not check - files, hunks, cases>" or "Skipped: none". Do not
change any other file. If you cannot write files, return the findings and the Skipped line as your answer instead.
Otherwise return one line: how many findings, by severity."""


def expect_text(c):
    parts = ["any exit" if c["expect_exit"] == -1 else f"exit {c['expect_exit']}"]
    parts += [f"output contains {s!r}" for s in c["contains"]]
    parts += [f"output lacks {s!r}" for s in c["not_contains"]]
    return ", ".join(parts)


def task_block(plan, t):
    contracts = {c.get("id"): c for c in plan.get("contracts", [])}
    lines = [f"Task {t['id']}: {t['title']}", "", "Spec:", t["spec"].strip(), "",
             "Files you own (the only files you may change):"]
    lines += [f"- {f['path']} ({f['action'] or 'edit'})" for f in t["files"]]
    if t["anchors"]:
        lines += ["", "Anchors (must exist in the code before you start):"]
        lines += [f"- {a['path']}: {a['text']}" for a in t["anchors"]]
    if t["provides"]:
        lines += ["", "Contracts this task provides (implement them exactly as written):"]
        lines += [f"- {c}: {contracts.get(c, {}).get('text', '?')}" for c in t["provides"]]
    if t["uses"]:
        lines += ["", "Contracts this task uses (another task provides them; code against them as written):"]
        lines += [f"- {c}: {contracts.get(c, {}).get('text', '?')} (provided by {contracts.get(c, {}).get('provided_by', '?')})"
                  for c in t["uses"]]
    if t["non_goals"]:
        lines += ["", "Non-goals (must NOT change):"] + [f"- {x}" for x in t["non_goals"]]
    lines += ["", "Acceptance criteria:"] + [f"- {x}" for x in t["acceptance"]]
    lines += ["", "Mechanical checks (a script runs them from the project directory after you finish):"]
    lines += [f"- {c['name']}{' [serial]' if c['serial'] else ''}: `{c['cmd']}` -> {expect_text(c)}" for c in t["checks"]]
    if t["review_focus"]:
        lines += ["", "Review focus: " + t["review_focus"]]
    shared = plan.get("rules") or ""
    if isinstance(shared, list):
        shared = "\n".join(str(x) for x in shared)
    rules = "\n".join(x.strip() for x in (str(shared), t["rules"]) if str(x).strip())
    if rules:
        lines += ["", "Project rules for this task:", rules]
    return "\n".join(lines)


def ctx(run_dir, project_dir):
    return {"run_dir": run_dir.replace("\\", "/"), "project_dir": project_dir.replace("\\", "/"),
            "skill_dir": os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")}


def render_task(kind, plan, t, run_dir, project_dir, engine="claude", dynamic=None):
    """kind: work | fix | finish | verify. dynamic: the text that the starting message would carry."""
    c = ctx(run_dir, project_dir)
    base = dict(header=HEADER[engine].format(**c), task=task_block(plan, t), engine=ENGINE[engine],
                limits=HARD_LIMITS.format(**c) + "\n" + BUDGET[kind], stop_rule=STOP_RULE, check_rule=CHECK_RULE)
    if kind == "work":
        return WORK.format(result=RESULT.format(plan_change='"" - executors never change the plan'), **base)
    if kind == "fix":
        problems = dynamic or "The problems to fix and your previous result are in the message that sent you here."
        return FIX.format(problems=problems,
                          result=RESULT.format(plan_change='"" - executors never change the plan'), **base)
    if kind == "finish":
        history = dynamic or ("Why you were called (the verdicts or the executor's mismatch) and the state of the "
                              "files are in the message that sent you here.")
        return FINISH.format(history=history, result=RESULT.format(plan_change="your correction of the plan, or \"\""),
                             **base)
    if kind == "verify":
        evidence = dynamic or ("The evidence for this round is in the message that sent you here: the diff of the "
                               "task's files against the snapshot and the report of the mechanical checks (all passed).")
        return VERIFY.format(header=base["header"], task=base["task"], evidence=evidence,
                             verify_engine=VERIFY_ENGINE[engine], limits=base["limits"], unchecked_rule=UNCHECKED_RULE)
    raise ValueError(kind)


def findings_text(run_dir):
    rv = read_json(os.path.join(run_dir, "review.json"), {}) or {}
    rows = [f for f in rv.get("findings", []) if str(f.get("severity", "")).lower() in ("critical", "high", "medium")]
    # a request item the reviewer found missing is a medium finding (power_run.py sends it down the same fix path)
    cov = [x for x in (rv.get("coverage") if isinstance(rv.get("coverage"), list) else []) if isinstance(x, dict)][:20]
    missing = [x for x in cov if str(x.get("status", "")).lower() == "missing"]
    if not rows and not missing:
        return "(no critical/high/medium findings in review.json)"
    lines = [f"- [{f.get('severity')}] {f.get('file')}:{f.get('line')} - {f.get('problem')} - fix: {f.get('fix')}" for f in rows]
    lines += [f"- [medium] request item missing: {x.get('request_item')} (where: {x.get('where') or '-'}) - fix: implement "
              "it inside the run's files, or leave it and say why it cannot be done there" for x in missing]
    return "\n".join(lines)


def render_final(kind, run_dir, project_dir, engine="claude"):
    """kind: finalfix | finalverify, after the final review (reads review.json)."""
    c = ctx(run_dir, project_dir)
    base = dict(header=HEADER[engine].format(**c), limits=HARD_LIMITS.format(**c) + "\n" + BUDGET[kind],
                findings=findings_text(run_dir), check_rule=CHECK_RULE, unchecked_rule=UNCHECKED_RULE, **c)
    if kind == "finalfix":
        return FINAL_FIX.format(engine=ENGINE[engine], **base)
    if kind == "finalverify":
        return FINAL_VERIFY.format(verify_engine=VERIFY_ENGINE[engine], **base)
    raise ValueError(kind)


def deny_text(project_dir):
    try:
        extra = load_config(project_dir).get("deny", [])
    except PlanError:
        extra = []
    return ", ".join(DEFAULT_DENY + list(extra))


def render_plan(run_dir, project_dir, engine="claude"):
    c = ctx(run_dir, project_dir)
    try:
        with open(os.path.join(run_dir, "request.md"), encoding="utf-8") as f:
            request = f.read().strip()
    except OSError:
        request = "(request.md is missing - ask the orchestrator)"
    output = PLAN_OUT[engine].format(task_shape=TASK_SHAPE, **c)
    return PLAN.format(request=request, rules=PLAN_RULES.format(deny=deny_text(project_dir)), output=output, **c)


def render_area(run_dir, project_dir, area, prefix, engine="claude"):
    c = ctx(run_dir, project_dir)
    owns = area.get("owns") or []
    owns = ", ".join(owns) if isinstance(owns, list) else str(owns)
    output = AREA_OUT[engine].format(task_shape=TASK_SHAPE, name=area["name"], **c)
    return AREA.format(name=area["name"], covers=area.get("covers", ""), owns=owns, prefix=prefix,
                       rules=PLAN_RULES.format(deny=deny_text(project_dir)), output=output, **c)


def render_review(run_dir, project_dir, engine="claude"):
    c = ctx(run_dir, project_dir)
    return REVIEW.format(output=REVIEW_OUT[engine].format(deploy_shape=DEPLOY_SHAPE, **c), severity=SEVERITY, **c)


def area_prefix(i):
    return "abcdefghijklmnopqrstuvwxyz"[i % 26] + ("" if i < 26 else str(i // 26))


def parse_items(items):
    pairs = []
    for it in items or []:
        name, _, text = it.partition("=")
        if not name.strip() or not text.strip():
            raise PlanError(f"--item must be name=text: {it!r}")
        pairs.append((name.strip(), text.strip()))
    return pairs


def cmd_write(a):
    kinds = [k.strip() for k in a.kinds.split(",") if k.strip()]
    pdir = os.path.join(a.run_dir, "prompts")
    written = []
    c = ctx(a.run_dir, a.project_dir)
    for kind in kinds:
        if kind == "plan":
            write_text(os.path.join(pdir, "plan.md"), render_plan(a.run_dir, a.project_dir))
            written.append("plan.md")
        elif kind == "tasks":
            plan = load_plan(a.run_dir)
            for t in plan["tasks"]:
                for k in ("work", "fix", "finish", "verify"):
                    write_text(os.path.join(pdir, f"{t['id']}.{k}.md"),
                               render_task(k, plan, t, a.run_dir, a.project_dir))
            written.append(f"{len(plan['tasks'])} tasks x work/fix/finish/verify")
        elif kind == "areas":
            skel = read_json(os.path.join(a.run_dir, "skeleton.json"))
            if not isinstance(skel, dict) or not skel.get("areas"):
                raise PlanError("skeleton.json missing or without areas")
            for i, area in enumerate(skel["areas"]):
                write_text(os.path.join(pdir, f"area.{area['name']}.md"),
                           render_area(a.run_dir, a.project_dir, area, area_prefix(i)))
            written.append(f"{len(skel['areas'])} areas")
        elif kind == "review":
            write_text(os.path.join(pdir, "review.md"), render_review(a.run_dir, a.project_dir))
            written.append("review.md")
        elif kind in ("finalfix", "finalverify"):
            if not os.path.isfile(os.path.join(a.run_dir, "review.json")):
                raise PlanError(f"{kind} needs review.json (the final review has not run)")
            write_text(os.path.join(pdir, f"{kind}.md"), render_final(kind, a.run_dir, a.project_dir))
            written.append(f"{kind}.md")
        elif kind in ("recon", "lens"):
            tpl = RECON if kind == "recon" else LENS
            for name, text in parse_items(a.item):
                write_text(os.path.join(pdir, f"{kind}.{name}.md"), tpl.format(name=name, focus=text, severity=SEVERITY, **c))
                written.append(f"{kind}.{name}.md")
        else:
            raise PlanError(f"unknown kind {kind}")
    out(f"PROMPTS: OK ({'; '.join(written)}) in {pdir.replace(chr(92), '/')}")


def cmd_show(a):
    if a.kind == "plan":
        out(render_plan(a.run_dir, a.project_dir, a.engine))
    elif a.kind == "review":
        out(render_review(a.run_dir, a.project_dir, a.engine))
    elif a.kind in ("finalfix", "finalverify"):
        out(render_final(a.kind, a.run_dir, a.project_dir, a.engine))
    elif a.kind == "area":
        skel = read_json(os.path.join(a.run_dir, "skeleton.json"), {})
        areas = skel.get("areas", [])
        i = next((n for n, x in enumerate(areas) if x.get("name") == a.area), None)
        if i is None:
            raise PlanError(f"no area {a.area} in skeleton.json")
        out(render_area(a.run_dir, a.project_dir, areas[i], area_prefix(i), a.engine))
    else:
        plan = load_plan(a.run_dir)
        t = next((x for x in plan["tasks"] if x["id"] == a.task), None)
        if not t:
            raise PlanError(f"unknown task {a.task}")
        out(render_task(a.kind, plan, t, a.run_dir, a.project_dir, a.engine))


def main():
    ap = argparse.ArgumentParser(description="POWER: agent instructions")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("write")
    sp.add_argument("--run-dir", required=True)
    sp.add_argument("--project-dir", required=True)
    sp.add_argument("--kinds", required=True, help="plan,tasks,areas,review,finalfix,finalverify,recon,lens")
    sp.add_argument("--item", action="append", help="name=text, for recon and lens")
    sp = sub.add_parser("show")
    sp.add_argument("--run-dir", required=True)
    sp.add_argument("--project-dir", required=True)
    sp.add_argument("--kind", required=True,
                    choices=["work", "fix", "finish", "verify", "plan", "area", "review", "finalfix", "finalverify"])
    sp.add_argument("--task")
    sp.add_argument("--area")
    sp.add_argument("--engine", choices=["claude", "codex"], default="claude")
    a = ap.parse_args()
    a.run_dir, a.project_dir = os.path.abspath(a.run_dir), os.path.abspath(a.project_dir)
    try:
        {"write": cmd_write, "show": cmd_show}[a.cmd](a)
    except PlanError as e:
        out(f"ERROR {e}", "PROMPTS: FAIL")
        sys.exit(1)


if __name__ == "__main__":
    main()
