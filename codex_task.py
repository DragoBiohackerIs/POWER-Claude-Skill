#!/usr/bin/env python3
"""Run one Codex (OpenAI, ChatGPT plan) job for a POWER run.

Every job subcommand is idempotent: the first call starts the job detached, later calls only wait. Each
call waits at most --max-wait seconds (default 540, under the 600 s Bash limit) and prints one STATUS line:

  STATUS: QUEUED   - parallel limit reached, the job has not started yet; call again
  STATUS: RUNNING  - the job is still running; call again
  STATUS: DONE     - work/fix/finish/verify: followed by RESULT_JSON: {...}
                     plan/plan-area/review: followed by RESULT: <which files were written>
  STATUS: FAILED   - followed by REASON: ..., RESULT_JSON with a schema-valid failure object, and the log tail

Jobs (the instructions come from power_prompts.py, the same text Claude agents get):
  work      --task T                                executor                       workspace-write
  fix       --task T --round N --problems-file F    executor fixes once            workspace-write
  finish    --task T --history-file F [--restored]  planner model, escalation      workspace-write
  verify    --task T --round N                      verifier                       read-only
  plan                                              planner (reads request.md)     read-only
  plan-area --area NAME                             area planner                   read-only
  review                                            final reviewer                 read-only
  finalfix                                          step 1 fixes the review findings   workspace-write
  finalverify                                       verifies the final fixes       read-only
Every job takes --run-dir R --project-dir P [--model SLUG] [--max-wait S]; without --model Codex uses
~/.codex/config.toml. plan writes PLAN.md + tasks.json (or skeleton.md + skeleton.json), plan-area writes
plan-areas/NAME.json, review writes REVIEW.md, DEPLOY.md, deploy.json and review.json.

Other subcommands:
  models                 newest model of each Codex family (astra = frontier, sol = workhorse, luna = cheap), as JSON
  workflow-args --run-dir R --project-dir P --step1 opus|fable|astra --step2 sonnet|haiku|opus|sol|luna|astra
                [--verify t9:opus:4,...]
                         the `args` JSON for the Workflow call (power_workflow.js); refuses without snapshot/prompts;
                         --verify = mode verify (checks + verifier only, for tasks whose check the orchestrator fixed)
  ready         --run-dir R --project-dir P --step1 .. --step2 .. [--baseline]
                         prepare -> (baseline) -> snapshot -> prompts for the tasks -> workflow-args, in one call
  usage [--log FILE]     ChatGPT plan usage snapshot (one tiny Codex call); appends to FILE
  status --run-dir R     tokens per job
  kill --run-dir R --project-dir P --kind K [job args]   end the detached runner of that job (taskkill /T /F on
                         Windows, SIGTERM/SIGKILL otherwise) and record `killed` in its done.json; prints
                         KILL: KILLED | NOT_RUNNING | NONE (never started) | FAILED. The harness calls it before a
                         Claude fallback takes a failed Codex job over, so the runner cannot keep editing.
  prompt --run-dir R --project-dir P --kind K [--task T] [--round N] [--area A]   print a job prompt

Codex runs in the unelevated Windows sandbox: no network and no Docker. Env overrides: CODEX_EXE,
POWER_CODEX_MAX_PARALLEL (6), POWER_CODEX_TIMEOUT (3600 s).
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import power_check  # noqa: E402
import power_prompts  # noqa: E402
from power_check import PlanError, agent_types, load_plan, normalize_plan, read_json, write_json, write_text  # noqa: E402
from power_prompts import area_prefix, render_area, render_final, render_plan, render_review, render_task  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

MAX_PARALLEL = int(os.environ.get("POWER_CODEX_MAX_PARALLEL", "6"))
JOB_TIMEOUT = int(os.environ.get("POWER_CODEX_TIMEOUT", "3600"))

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # creationflags are Windows-only (V2 2.4)

STR = {"type": "string"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}
STR_LIST = {"type": "array", "items": STR}


def obj(props):
    return {"type": "object", "additionalProperties": False, "properties": props, "required": list(props)}


def enum(*values):
    return {"type": "string", "enum": list(values)}


RESULT_SCHEMA = obj({"status": enum("DONE", "PLAN_MISMATCH"), "files_changed": STR_LIST, "mismatch": STR,
                     "summary": STR, "how_verified": STR, "open_issues": STR_LIST, "plan_change": STR})
VERDICT_SCHEMA = obj({"pass": BOOL, "problems": STR_LIST, "plan_problem": BOOL, "notes": STR,
                      "unchecked": STR_LIST})   # what the verifier did not check (Gate B "НЕПРОВЕРЕНО")
CHECK_ITEM = obj({"name": STR, "cmd": STR, "expect_exit": INT, "contains": STR_LIST, "not_contains": STR_LIST,
                  "before": enum("fail", "pass", "any"), "serial": BOOL, "timeout": INT, "retries": INT})
TASK_SCHEMA = obj({
    "id": STR, "title": STR, "spec": STR,
    "files": {"type": "array", "items": obj({"path": STR, "action": enum("edit", "create", "delete")})},
    "anchors": {"type": "array", "items": obj({"path": STR, "text": STR})},
    "provides": STR_LIST, "uses": STR_LIST, "non_goals": STR_LIST, "acceptance": STR_LIST,
    "checks": {"type": "array", "items": CHECK_ITEM}, "review_focus": STR, "rules": STR,
    "size": obj({"files": INT, "lines": INT}), "depends_on": STR_LIST})
PLAN_SCHEMA = obj({
    "mode": enum("full", "skeleton"), "goal": STR, "scope": STR, "out_of_scope": STR, "deploy_plan": STR,
    "rules": STR, "risks": STR_LIST,
    "contracts": {"type": "array", "items": obj({"id": STR, "text": STR, "provided_by": STR})},
    "areas": {"type": "array", "items": obj({"name": STR, "owns": STR_LIST, "covers": STR, "est_tasks": INT})},
    "tasks": {"type": "array", "items": TASK_SCHEMA}, "plan_md": STR})
AREA_SCHEMA = obj({"mismatch": STR, "tasks": {"type": "array", "items": TASK_SCHEMA}, "section_md": STR})
DEPLOY_SCHEMA = obj({
    "profile": STR,
    "files": {"type": "array", "items": obj({"local": STR, "order": INT, "note": STR})},
    "after": {"type": "array", "items": {"anyOf": [STR, obj({"cmd": STR, "optional": {"type": "boolean"}})]}}, "canary": STR_LIST,
    "verify": {"type": "array", "items": obj({"url": STR, "contains": STR, "via": enum("edge", "origin")})},
    "db": {"type": "array", "items": obj({"description": STR, "apply": STR, "rollback": STR})},
    "needs_yes": STR_LIST, "risks": STR_LIST})
REVIEW_SCHEMA = obj({
    "verdict": enum("PASS", "PASS_WITH_FIXES", "FAIL"),
    "findings": {"type": "array", "items": obj({"severity": enum("critical", "high", "medium", "low"),
                                                "file": STR, "line": INT, "problem": STR, "fix": STR})},
    # the request items against the change, and the lens findings the reviewer threw out (Gate B / REPORT.md)
    "coverage": {"type": "array", "items": obj({"request_item": STR, "where": STR,
                                                "status": enum("done", "partial", "missing", "out_of_scope_by_plan")})},
    "dismissed": {"type": "array", "items": obj({"lens": STR, "file": STR, "line": INT, "reason": STR, "evidence": STR,
                                                 "severity": enum("critical", "high", "medium", "low")})},
    "review_md": STR, "deploy_md": STR, "deploy": DEPLOY_SCHEMA})

# kind -> (sandbox, schema)
KINDS = {
    "work": ("workspace-write", RESULT_SCHEMA),
    "fix": ("workspace-write", RESULT_SCHEMA),
    "finish": ("workspace-write", RESULT_SCHEMA),
    "verify": ("read-only", VERDICT_SCHEMA),
    "plan": ("read-only", PLAN_SCHEMA),
    "area": ("read-only", AREA_SCHEMA),
    "review": ("read-only", REVIEW_SCHEMA),
    "finalfix": ("workspace-write", RESULT_SCHEMA),
    "finalverify": ("read-only", VERDICT_SCHEMA),
}
FILE_KINDS = ("plan", "area", "review")  # the script writes their files; the output stays short
NO_TASK_KINDS = ("plan", "area", "review", "finalfix", "finalverify")  # run-level jobs, no --task


def now():
    return datetime.now().isoformat(timespec="seconds")


def out(*lines):
    print("\n".join(lines), flush=True)


def failure_object(kind, reason):
    if kind in ("work", "fix", "finish", "finalfix"):
        return {"status": "CODEX_FAILED", "files_changed": [], "mismatch": "", "summary": "CODEX_FAILED: " + reason,
                "how_verified": "", "open_issues": [reason], "plan_change": ""}
    if kind in ("verify", "finalverify"):
        return {"pass": False, "problems": [], "plan_problem": False, "notes": "CODEX_FAILED: " + reason, "unchecked": []}
    return {"failed": reason}


def resolve_codex():
    exe = os.environ.get("CODEX_EXE")
    if exe:
        # an explicit override that points nowhere is a configuration error, not a reason to pick another install
        return exe if os.path.isfile(exe) else None
    # The desktop app keeps the CLI in a hashed folder that changes on update.
    found = glob.glob(os.path.join(os.environ.get("LOCALAPPDATA", ""), "OpenAI", "Codex", "bin", "*", "codex.exe"))
    if found:
        return max(found, key=os.path.getmtime)
    return shutil.which("codex")


def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


CODEX_FAMILIES = ("astra", "sol", "luna")


def readiness():
    """V3 readiness, no paid probe: the problems that would make every Codex job fail - no codex executable
    (CODEX_EXE, the desktop app, PATH) or no login (auth.json under CODEX_HOME). [] when both are there."""
    problems = []
    exe = resolve_codex()
    if not exe:
        problems.append("codex executable not found (CODEX_EXE=" + repr(os.environ.get("CODEX_EXE") or "unset")
                        + "; the desktop app under LOCALAPPDATA/OpenAI/Codex/bin, or `codex` on PATH) - no Codex job can run; "
                          "install Codex or choose Claude models")
    auth = os.path.join(codex_home(), "auth.json")
    if not os.path.isfile(auth):
        problems.append(f"Codex is not logged in: {auth} missing (CODEX_HOME={os.environ.get('CODEX_HOME') or 'unset'}); "
                        "run `codex login` first, or choose Claude models")
    return problems


def job_paths(run_dir, job):
    base = os.path.join(run_dir, "codex", job)
    return {k: f"{base}.{k}" for k in ("prompt.md", "schema.json", "state.json", "out.json", "log", "done.json",
                                       "post.json")}


def active_jobs(run_dir):
    n = 0
    for state in glob.glob(os.path.join(run_dir, "codex", "*.state.json")):
        if os.path.exists(state[: -len("state.json")] + "done.json"):
            continue
        # A runner that outlived its own timeout is dead; do not let it hold a slot.
        if time.time() - os.path.getmtime(state) > JOB_TIMEOUT + 300:
            continue
        n += 1
    return n


def spawn_runner(a, job, sandbox):
    cmd = [sys.executable, os.path.abspath(__file__), "_runner", "--run-dir", a.run_dir,
           "--project-dir", a.project_dir, "--job", job, "--sandbox", sandbox]
    if a.model:
        cmd += ["--model", a.model]
    quiet = {"close_fds": True, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        base = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        for flags in (base | CREATE_BREAKAWAY_FROM_JOB, base):
            try:
                return subprocess.Popen(cmd, creationflags=flags, **quiet).pid
            except OSError:
                continue
        raise RuntimeError("could not start the runner process")
    # POSIX: the runner leads its own session / process group, so kill_tree can end it with its codex child
    try:
        return subprocess.Popen(cmd, start_new_session=True, **quiet).pid
    except OSError as e:
        raise RuntimeError(f"could not start the runner process: {e}")


def try_start(a, job, kind, prompt):
    """Start the job unless it already started. Returns False when queued."""
    p = job_paths(a.run_dir, job)
    if os.path.exists(p["state.json"]):
        return True
    if active_jobs(a.run_dir) >= MAX_PARALLEL:
        return False
    sandbox, schema = KINDS[kind]
    write_text(p["prompt.md"], prompt)
    write_json(p["schema.json"], schema)
    try:
        fd = os.open(p["state.json"], os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return True
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"job": job, "started_at": now()}, f)
    pid = spawn_runner(a, job, sandbox)
    write_json(p["state.json"], {"job": job, "kind": kind, "sandbox": sandbox, "started_at": now(), "pid": pid,
                                 "model": a.model or "config default"})
    return True


def post_process(a, kind, job, result):
    """Write the files of plan/area/review jobs once; return a one-line summary."""
    p = job_paths(a.run_dir, job)
    done = read_json(p["post.json"])
    if done:
        return done.get("summary", "")
    r = a.run_dir
    if kind == "plan":
        meta = {k: result.get(k) for k in ("mode", "goal", "scope", "out_of_scope", "deploy_plan", "rules", "risks",
                                           "contracts")}
        if result.get("mode") == "skeleton":
            write_text(os.path.join(r, "skeleton.md"), result.get("plan_md", ""))
            write_json(os.path.join(r, "skeleton.json"), dict(meta, areas=result.get("areas", [])))
            summary = f"wrote skeleton.md + skeleton.json ({len(result.get('areas', []))} areas)"
        else:
            write_text(os.path.join(r, "PLAN.md"), result.get("plan_md", ""))
            write_json(os.path.join(r, "tasks.json"), dict(meta, tasks=result.get("tasks", [])))
            summary = f"wrote PLAN.md + tasks.json ({len(result.get('tasks', []))} tasks)"
        q = [x for x in result.get("risks", []) if str(x).startswith("QUESTION:")]
        if q:
            summary += f"; {len(q)} QUESTION risks"
    elif kind == "area":
        write_json(os.path.join(r, "plan-areas", f"{a.area}.json"),
                   {k: result.get(k) for k in ("mismatch", "tasks", "section_md")})
        summary = (f"MISMATCH: {result['mismatch']}" if str(result.get("mismatch", "")).strip()
                   else f"wrote plan-areas/{a.area}.json ({len(result.get('tasks', []))} tasks)")
    else:
        write_text(os.path.join(r, "REVIEW.md"), result.get("review_md", ""))
        write_text(os.path.join(r, "DEPLOY.md"), result.get("deploy_md", ""))
        write_json(os.path.join(r, "review.json"), {"verdict": result.get("verdict"),
                                                     "findings": result.get("findings", []),
                                                     "coverage": result.get("coverage", []),
                                                     "dismissed": result.get("dismissed", [])})
        if isinstance(result.get("deploy"), dict):
            write_json(os.path.join(r, "deploy.json"), result["deploy"])
        summary = f"wrote REVIEW.md, DEPLOY.md, deploy.json, review.json (verdict {result.get('verdict')}, {len(result.get('findings', []))} findings)"
    write_json(p["post.json"], {"summary": summary, "at": now()})
    return summary


def report(a, kind, job):
    p = job_paths(a.run_dir, job)
    done = read_json(p["done.json"], {})
    result = read_json(p["out.json"])
    if done.get("status") == "OK" and isinstance(result, dict):
        if kind in FILE_KINDS:
            out("STATUS: DONE", "RESULT: " + post_process(a, kind, job, result))
        else:
            out("STATUS: DONE", "RESULT_JSON: " + json.dumps(result, ensure_ascii=False))
        return
    tail = ""
    try:
        with open(p["log"], encoding="utf-8", errors="replace") as f:
            tail = "".join(f.readlines()[-15:])
    except OSError:
        pass
    reason = done.get("error") or f"codex status {done.get('status')}, exit code {done.get('exit_code')}"
    if done.get("status") == "OK":
        reason = "codex finished but wrote no valid result JSON"
    out("STATUS: FAILED", "REASON: " + reason,
        "RESULT_JSON: " + json.dumps(failure_object(kind, reason), ensure_ascii=False), "LOG_TAIL:", tail)


def run_job(a, kind, job, prompt):
    """Start (or re-attach to) the job and wait for done.json up to --max-wait. PERF-09: the poll starts at 0.25 s
    and backs off to 2 s, and done.json is checked right after the start, so a finished job is reported within a
    quarter of a second instead of after a fixed 5 s sleep (with 10-20 jobs per run that was minutes of idle time)."""
    p = job_paths(a.run_dir, job)
    os.makedirs(os.path.dirname(p["log"]), exist_ok=True)
    deadline = time.time() + a.max_wait
    started = os.path.exists(p["state.json"])
    wait = 0.25
    while True:
        if os.path.exists(p["done.json"]):
            report(a, kind, job)
            return
        if not started:
            started = try_start(a, job, kind, prompt())
            if started and os.path.exists(p["done.json"]):
                continue
        if time.time() >= deadline:
            out("STATUS: " + ("RUNNING" if started else "QUEUED"), f"JOB: {job}")
            return
        time.sleep(min(wait, max(0.0, deadline - time.time())) or 0.05)
        wait = min(wait * 2, 2.0)


def fail_now(kind, reason):
    out("STATUS: FAILED", "REASON: " + reason,
        "RESULT_JSON: " + json.dumps(failure_object(kind, reason), ensure_ascii=False))
    sys.exit(0)


def load_task(a, kind):
    try:
        plan = load_plan(a.run_dir)
    except PlanError as e:
        fail_now(kind, str(e))
    t = next((x for x in plan["tasks"] if x["id"] == a.task), None)
    if not t:
        fail_now(kind, f"task {a.task} not in tasks.json")
    return plan, t


# ---------- prompts ----------

def prompt_for(a, kind):
    """A callable that renders the job prompt (called once, when the job starts)."""
    if kind == "plan":
        return lambda: render_plan(a.run_dir, a.project_dir, "codex")
    if kind == "review":
        return lambda: render_review(a.run_dir, a.project_dir, "codex")
    if kind in ("finalfix", "finalverify"):
        if not os.path.isfile(os.path.join(a.run_dir, "review.json")):
            fail_now(kind, "review.json missing in the run directory (the final review comes first)")
        return lambda: final_prompt(a, kind)
    if kind == "area":
        skel = read_json(os.path.join(a.run_dir, "skeleton.json"), {})
        areas = skel.get("areas") or []
        i = next((n for n, x in enumerate(areas) if x.get("name") == a.area), None)
        if i is None:
            fail_now(kind, f"area {a.area} not in skeleton.json")
        return lambda: render_area(a.run_dir, a.project_dir, areas[i], area_prefix(i), "codex")
    plan, t = load_task(a, kind)
    rd = a.run_dir.replace("\\", "/")
    if kind == "work":
        return lambda: render_task("work", plan, t, a.run_dir, a.project_dir, "codex")
    if kind == "fix":
        def fix_prompt():
            prev_job = f"{t['id']}.fix{a.round - 1}" if a.round > 1 else f"{t['id']}.work"
            previous = read_json(job_paths(a.run_dir, prev_job)["out.json"], {})
            problems = read_json(a.problems_file)
            if isinstance(problems, list):
                text = "\n".join(f"- {x}" for x in problems)
            else:
                with open(a.problems_file, encoding="utf-8") as f:
                    text = f.read()
            dynamic = (f"Your previous result: {json.dumps(previous, ensure_ascii=False)}\n"
                       f"It was rejected with these problems:\n{text}")
            return render_task("fix", plan, t, a.run_dir, a.project_dir, "codex", dynamic)
        return fix_prompt
    if kind == "finish":
        def finish_prompt():
            h = read_json(a.history_file, {}) or {}
            lines = [f"Why you were called: {h.get('reason', 'the executor did not finish')}"]
            for v in h.get("verdicts", []):
                lines.append(f"- round {v.get('round')} ({v.get('by')}): " + " | ".join(v.get("problems", [])))
            lines.append("State of the files: " + (
                "the executor's partial edits were rolled back, the files are at the snapshot state."
                if a.restored else "the files hold the executor's last attempt; continue from it or rewrite it."))
            lines.append(f"The check reports of the earlier rounds are in {rd}/checks/{t['id']}.r*/report.md.")
            return render_task("finish", plan, t, a.run_dir, a.project_dir, "codex", "\n".join(lines))
        return finish_prompt
    if kind == "verify":
        ev = (f"Evidence for round {a.round} (the mechanical checks passed):\n"
              f"- diff of the task's files against the snapshot: {rd}/checks/{t['id']}.r{a.round}/diff.patch\n"
              f"- report of the mechanical checks: {rd}/checks/{t['id']}.r{a.round}/report.md")
        return lambda: render_task("verify", plan, t, a.run_dir, a.project_dir, "codex", ev)
    raise ValueError(kind)


def final_prompt(a, kind):
    """finalfix / finalverify after the final review: the same text the Claude agents get (power_prompts.render_final),
    the evidence paths of the final2 round appended for the verifier."""
    rd = a.run_dir.replace("\\", "/")
    text = render_final(kind, a.run_dir, a.project_dir, "codex")
    if kind == "finalverify":
        text += (f"\n\nEvidence: the diff after the fixes {rd}/checks/final2/diff.patch, the check report "
                 f"{rd}/checks/final2/report.md, the findings in {rd}/review.json.")
    return text


def job_name(a, kind):
    # review / finalfix / finalverify carry the final-review generation (`--gen N`, V2-04): a job finished in an
    # earlier generation is never re-attached to by a later one. Without --gen the old names stay (older runs).
    g = f".g{a.gen}" if getattr(a, "gen", None) else ""
    return {"work": lambda: f"{a.task}.work", "fix": lambda: f"{a.task}.fix{a.round}",
            "finish": lambda: f"{a.task}.finish", "verify": lambda: f"{a.task}.verify{a.round}",
            "plan": lambda: "plan", "area": lambda: f"area.{a.area}", "review": lambda: "review" + g,
            "finalfix": lambda: "finalfix" + g, "finalverify": lambda: "finalverify" + g}[kind]()


def pid_alive(pid):
    """Is the process running? (another user's process counts as alive)"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return k32.GetLastError() == 5        # ERROR_ACCESS_DENIED
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == 259          # STILL_ACTIVE
            return True
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not _zombie(pid)


def _zombie(pid):
    """POSIX: a process that exited but was not reaped yet (state Z) still answers os.kill(pid, 0); it runs nothing
    and never will (V3-01). /proc on Linux, `ps -o stat=` elsewhere; unknown = not a zombie."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
            fields = f.read().rsplit(")", 1)
        return len(fields) == 2 and fields[1].split()[:1] == ["Z"]
    except OSError:
        pass
    try:
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False
    return st[:1] == "Z"


def _children(pid):
    """POSIX: the pids below `pid` (pgrep -P, recursively); [] when pgrep is missing."""
    try:
        outp = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    kids = [int(x) for x in outp.split() if x.strip().isdigit()]
    return kids + [g for k in kids for g in _children(k)]


def kill_tree(pid):
    """End the runner AND its codex child. Returns True when the runner process is gone afterwards.
    Windows: taskkill /T /F (the whole tree). POSIX: the runner's process group (spawn_runner starts it with its
    own session) plus every descendant found with pgrep, children first; SIGTERM, then SIGKILL."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW)
    else:
        import signal
        # the descendants are listed BEFORE the first signal: once the runner is dead its children are re-parented and
        # pgrep -P no longer finds them (V3-01B); the group id too, while the leader still exists
        tree = list(reversed(_children(pid))) + [pid]
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError, OSError):
            pgid = None
        for sig, grace in ((signal.SIGTERM, 0.5), (signal.SIGKILL, 2.0)):
            targets = [t for t in tree if pid_alive(t)] + [t for t in _children(pid) if t not in tree and pid_alive(t)]
            if not targets:
                break
            if pgid == pid:          # the runner leads its own group: one signal reaches it and the codex child
                try:
                    os.killpg(pgid, sig)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            for t in targets:
                try:
                    os.kill(t, sig)
                except ProcessLookupError:
                    continue
                except PermissionError:
                    if t == pid:
                        return False
            # SIGTERM gets a short grace for the WHOLE tree (not only the runner): a child that ignores it, or that
            # outlives its parent, is SIGKILLed in the next round instead of surviving a "gone" verdict
            end = time.monotonic() + grace
            while time.monotonic() < end and any(pid_alive(t) for t in targets):
                time.sleep(0.05)
            if not any(pid_alive(t) for t in targets):
                return True
    for _ in range(50):
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


def cmd_kill(a):
    """End a job's detached runner (if it still runs) and record the kill in its done.json, so a later re-attach
    reports the job as failed instead of waiting for, or picking up, a result that nobody wants any more."""
    kind = "area" if a.kind == "plan-area" else a.kind
    try:
        job = job_name(a, kind)
    except (KeyError, AttributeError, TypeError) as e:
        out("KILL: FAILED", f"REASON: cannot name the job of kind {a.kind}: {e}")
        return
    p = job_paths(a.run_dir, job)
    st = read_json(p["state.json"], None)
    if not isinstance(st, dict) or not st.get("pid"):
        out("KILL: NONE", f"JOB: {job}", "REASON: the job never started (no runner state)")
        return
    pid = st["pid"]
    done = read_json(p["done.json"], None)
    done = done if isinstance(done, dict) else {}
    alive = pid_alive(pid)
    gone = kill_tree(pid) if alive else True
    done.update({"pid": pid, "killed": bool(alive and gone), "killed_at": now(),
                 "status": done.get("status") or ("KILLED" if alive and gone else "FAILED")})
    if alive and gone and not done.get("error"):
        done["error"] = "runner killed by the harness (the Claude fallback took the job over)"
    write_json(p["done.json"], done)
    out("KILL: " + ("KILLED" if alive and gone else ("NOT_RUNNING" if not alive else "FAILED")), f"JOB: {job}", f"PID: {pid}")


def cmd_job(kind):
    def run(a):
        if kind in NO_TASK_KINDS:
            p = job_paths(a.run_dir, job_name(a, kind))
            if not os.path.exists(p["state.json"]) and kind == "plan" and not os.path.isfile(
                    os.path.join(a.run_dir, "request.md")):
                fail_now(kind, "request.md missing in the run directory")
        run_job(a, kind, job_name(a, kind), prompt_for(a, kind))
    return run


def cmd_prompt(a):
    kind = a.kind
    if kind == "fix" and not a.problems_file:
        a.problems_file = os.devnull
    out(prompt_for(a, kind)())


# ---------- models, usage, status ----------

FAMILY = re.compile(r"^gpt-(\d+(?:\.\d+)*)-([a-z]+)$")
FAMILY_HINT = {"astra": "frontier", "sol": "workhorse", "luna": "affordable"}


def resolve_models(cache):
    listed = [m for m in cache.get("models", []) if m.get("visibility") == "list"]
    best, warnings = {}, []
    for m in listed:
        mm = FAMILY.match(m.get("slug", ""))
        if not mm:
            continue
        key = (tuple(int(x) for x in mm.group(1).split(".")), -m.get("priority", 999))
        fam = mm.group(2)
        if fam not in best or key > best[fam][0]:
            best[fam] = (key, m)
    pick = {f: v[1] for f, v in best.items()}
    for fam, word in FAMILY_HINT.items():
        if fam in pick:
            continue
        alt = sorted((m for m in listed if word in m.get("description", "").lower()),
                     key=lambda m: m.get("priority", 999))
        if alt:
            pick[fam] = alt[0]
            warnings.append(f"no {fam} family in the Codex cache; using {alt[0]['slug']} ({word})")
        else:
            warnings.append(f"no {fam} model in the Codex cache")
    fetched = cache.get("fetched_at")
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(str(fetched).replace("Z", "+00:00")[:32])
        if age.days >= 7:
            warnings.append(f"the Codex model cache is {age.days} days old; run Codex once to refresh it")
    except ValueError:
        pass

    def view(m):
        return {"slug": m["slug"], "display_name": m.get("display_name", m["slug"]),
                "description": m.get("description", "")}
    return {"fetched_at": fetched, "astra": view(pick["astra"]) if "astra" in pick else None,
            "sol": view(pick["sol"]) if "sol" in pick else None,
            "luna": view(pick["luna"]) if "luna" in pick else None, "warnings": warnings,
            "listed": [view(m) for m in sorted(listed, key=lambda m: m.get("priority", 999))]}


def cmd_models(a):
    cache = read_json(os.path.join(codex_home(), "models_cache.json"), {})
    print(json.dumps(resolve_models(cache), ensure_ascii=False, indent=2))


STEP1 = ("opus", "fable", "astra")
STEP2 = ("sonnet", "haiku", "opus", "fable", "sol", "luna", "astra")   # the same menu as power_run.py STEP2


def cmd_workflow_args(a):
    """The `args` value for the Workflow call (power_workflow.js), from tasks.json and the menu choice.
    Refuses when the run is not ready: the Workflow itself cannot tell and would burn agents on INFRA failures."""
    if a.step1 not in STEP1 or a.step2 not in STEP2:
        sys.exit(f"step1 must be one of {STEP1}, step2 one of {STEP2}")
    try:
        plan = normalize_plan(load_plan(a.run_dir), a.project_dir)
    except PlanError as e:
        sys.exit(str(e))
    missing = []
    if not os.path.isfile(os.path.join(a.run_dir, "before", "manifest.json")):
        missing.append("snapshot (power_check.py snapshot)")
    for t in plan["tasks"]:
        for k in ("work", "fix", "finish", "verify"):
            if not os.path.isfile(os.path.join(a.run_dir, "prompts", f"{t['id']}.{k}.md")):
                missing.append("prompts (power_prompts.py write --kinds tasks)")
                break
        if missing and missing[-1].startswith("prompts"):
            break
    if not os.path.isdir(os.path.join(a.run_dir, "checks", "baseline")):
        print("WARN: no baseline (power_check.py baseline) - PRE-BROKEN/WEAK checks will not be flagged", file=sys.stderr)
    if missing:
        sys.exit("not ready: " + "; ".join(dict.fromkeys(missing)))
    models = resolve_models(read_json(os.path.join(codex_home(), "models_cache.json"), {}))
    codex = {f: (models.get(f) or {}).get("slug") for f in ("astra", "sol", "luna")}
    for step in (a.step1, a.step2):
        if step in codex and not codex[step]:
            sys.exit(f"no Codex {step} model in the cache; choose another model")
    here = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
    task_args = [{"id": t["id"], "depends_on": t["depends_on"], "files": [f["path"] for f in t["files"]]}
                 for t in plan["tasks"]]
    mode = "work"
    if getattr(a, "verify", None):
        # verify-only: the orchestrator fixed a broken check; only checks + verifier run, the edits stay
        by_id = {t["id"]: t for t in task_args}
        # V2-14: the task's earlier outcome (results.json) goes along, so the Workflow merges the new verdict into it
        # instead of building a fresh one (author, verdicts, plan changes and reported stray files survive)
        prev_rows = read_json(os.path.join(a.run_dir, "results.json"), None)
        if isinstance(prev_rows, dict) and isinstance(prev_rows.get("result"), list):
            prev_rows = prev_rows["result"]
        previous = {r.get("task"): r for r in (prev_rows if isinstance(prev_rows, list) else []) if isinstance(r, dict)}
        sel = []
        for item in a.verify.split(","):
            parts = item.strip().split(":")
            if len(parts) != 3 or parts[0] not in by_id:
                sys.exit(f"--verify wants id:author:round for a known task, got {item!r}")
            sel.append(dict(by_id[parts[0]], depends_on=[], author=parts[1], round=int(parts[2]), previous=previous.get(parts[0])))
        task_args, mode = sel, "verify"
    args = {"tasks": task_args,
            "run_dir": a.run_dir.replace("\\", "/"), "project_dir": a.project_dir.replace("\\", "/"),
            "step1": a.step1, "step2": a.step2, "codex_models": codex,
            "helpers": {"codex": f"{here}/codex_task.py", "check": f"{here}/power_check.py"},
            "agent_types": agent_types(a.project_dir), "mode": mode}
    print(json.dumps(args, ensure_ascii=False))


def cmd_ready(a):
    """One call instead of four: prepare -> (baseline) -> snapshot -> task prompts -> workflow-args.
    Each step prints its own TAG line; prepare stops the chain on errors (exit 1). With a Codex step the Codex
    install is checked first (executable + login, `readiness()`): READY: FAIL and exit 1 before anything is prepared."""
    if a.step1 in CODEX_FAMILIES or a.step2 in CODEX_FAMILIES:
        problems = readiness()
        if problems:
            for x in problems:
                out("ERROR " + x)
            out("READY: FAIL (Codex is not ready; nothing prepared)")
            sys.exit(1)
    ns = argparse.Namespace(run_dir=a.run_dir, project_dir=a.project_dir, force=False, kinds="tasks", item=None)
    power_check.paths(ns)
    power_check.cmd_prepare(ns)
    if a.baseline:
        power_check.cmd_baseline(ns)
    power_check.cmd_snapshot(ns)
    power_prompts.cmd_write(ns)
    cmd_workflow_args(a)


def job_tokens(log_path):
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            m = re.findall(r"tokens used\s*\n\s*([\d,]+)", f.read())
    except OSError:
        return 0
    return int(m[-1].replace(",", "")) if m else 0


def cmd_status(a):
    total = 0
    for state in sorted(glob.glob(os.path.join(a.run_dir, "codex", "*.state.json"))):
        job = os.path.basename(state)[: -len(".state.json")]
        base = state[: -len("state.json")]
        done = read_json(base + "done.json")
        s = read_json(state, {})
        tokens = job_tokens(base + "log")
        total += tokens
        out(f"{job}: {done['status'] if done else 'RUNNING'} (model {s.get('model')}, started {s.get('started_at')}, tokens {tokens:,})")
    out(f"TOTAL tokens (finished jobs): {total:,}")


def cmd_usage(a):
    # `codex exec --json` carries no rate limits; the session rollout file does, so this one
    # tiny call is deliberately not --ephemeral.
    exe = resolve_codex()
    if not exe:
        sys.exit("codex executable not found")
    started = time.time()
    scratch = os.path.join(os.environ.get("TEMP", "."), "power-codex-usage")
    os.makedirs(scratch, exist_ok=True)
    subprocess.run([exe, "exec", "--skip-git-repo-check", "-s", "read-only",
                    "-c", 'windows.sandbox="unelevated"', "-c", "notify=[]",
                    "-c", 'model_reasoning_effort="low"', "-C", scratch, "Reply with exactly: OK"],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=300, creationflags=CREATE_NO_WINDOW)
    files = [f for f in glob.glob(os.path.join(codex_home(), "sessions", "**", "*.jsonl"), recursive=True)
             if os.path.getmtime(f) >= started - 5]
    limits = None
    for path in sorted(files, key=os.path.getmtime, reverse=True):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"rate_limits"' in line:
                    try:
                        limits = json.loads(line).get("payload", {}).get("rate_limits") or limits
                    except ValueError:
                        pass
        if limits:
            break
    if not limits or not limits.get("primary"):
        sys.exit("no rate_limits found in the new Codex session")
    p = limits["primary"]
    snap = {"taken_at": now(), "used_percent": p.get("used_percent"),
            "window_days": round(p.get("window_minutes", 0) / 1440, 1),
            "resets_at": datetime.fromtimestamp(p["resets_at"]).isoformat(timespec="minutes"),
            "plan": limits.get("plan_type"), "secondary": limits.get("secondary")}
    line = json.dumps(snap, ensure_ascii=False)
    print(line)
    if a.log:
        with open(a.log, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def cmd_runner(a):
    p = job_paths(a.run_dir, a.job)
    exe = resolve_codex()
    result = {"status": "FAILED", "exit_code": None}
    if not exe:
        result["error"] = "codex executable not found (install @openai/codex or set CODEX_EXE)"
    else:
        cmd = [exe, "exec",
               "-c", 'windows.sandbox="unelevated"',  # the elevated sandbox fails to provision here
               "-c", "notify=[]",
               "-s", a.sandbox, "-C", a.project_dir,
               "--skip-git-repo-check", "--ephemeral",
               "--output-schema", p["schema.json"], "-o", p["out.json"]]
        if a.model:
            cmd += ["-m", a.model]
        cmd.append("-")
        try:
            with open(p["prompt.md"], "rb") as fin, open(p["log"], "wb") as flog:
                proc = subprocess.run(cmd, stdin=fin, stdout=flog, stderr=subprocess.STDOUT,
                                      timeout=JOB_TIMEOUT, creationflags=CREATE_NO_WINDOW)
            result = {"status": "OK" if proc.returncode == 0 else "FAILED", "exit_code": proc.returncode}
        except subprocess.TimeoutExpired:
            result = {"status": "TIMEOUT", "exit_code": None, "error": f"codex ran over {JOB_TIMEOUT} s"}
        except OSError as e:
            result["error"] = str(e)
    result["finished_at"] = now()
    write_json(p["done.json"], result)


def main():
    ap = argparse.ArgumentParser(description="POWER: run a Codex job")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def job(name):
        sp = sub.add_parser(name)
        sp.add_argument("--run-dir", required=True)
        sp.add_argument("--project-dir", required=True)
        sp.add_argument("--max-wait", type=int, default=540)
        sp.add_argument("--model", help="Codex model slug; default = ~/.codex/config.toml")
        return sp

    job("work").add_argument("--task", required=True)
    sp = job("fix")
    sp.add_argument("--task", required=True)
    sp.add_argument("--round", type=int, required=True)
    sp.add_argument("--problems-file", required=True)
    sp = job("finish")
    sp.add_argument("--task", required=True)
    sp.add_argument("--history-file", required=True)
    sp.add_argument("--restored", action="store_true")
    sp = job("verify")
    sp.add_argument("--task", required=True)
    sp.add_argument("--round", type=int, required=True)
    job("plan")
    job("plan-area").add_argument("--area", required=True)
    for name in ("review", "finalfix", "finalverify"):
        job(name).add_argument("--gen", type=int, help="final-review generation (the harness's; versions the job name)")
    sp = sub.add_parser("prompt")
    sp.add_argument("--run-dir", required=True)
    sp.add_argument("--project-dir", required=True)
    sp.add_argument("--kind", required=True, choices=list(KINDS))
    sp.add_argument("--task")
    sp.add_argument("--round", type=int, default=1)
    sp.add_argument("--area")
    sp.add_argument("--problems-file")
    sp.add_argument("--history-file", default=os.devnull)
    sp.add_argument("--restored", action="store_true")
    sp = sub.add_parser("kill")
    sp.add_argument("--run-dir", required=True)
    sp.add_argument("--project-dir", required=True)
    sp.add_argument("--kind", required=True, choices=list(KINDS) + ["plan-area"])
    sp.add_argument("--task")
    sp.add_argument("--round", type=int, default=1)
    sp.add_argument("--area")
    sp.add_argument("--gen", type=int)
    sp.add_argument("--problems-file")
    sp.add_argument("--history-file")
    sp.add_argument("--restored", action="store_true")
    sp.add_argument("--model")
    sp.add_argument("--max-wait", type=int)
    sp = sub.add_parser("status")
    sp.add_argument("--run-dir", required=True)
    for name in ("workflow-args", "ready"):
        sp = sub.add_parser(name)
        sp.add_argument("--run-dir", required=True)
        sp.add_argument("--project-dir", required=True)
        sp.add_argument("--step1", required=True)
        sp.add_argument("--step2", required=True)
        if name == "ready":
            sp.add_argument("--baseline", action="store_true", help="also run the baseline (START: no gate A before)")
        else:
            sp.add_argument("--verify", help="id:author:round,... -> mode verify (checks + verifier only)")
    sub.add_parser("models")
    sub.add_parser("usage").add_argument("--log")
    sp = sub.add_parser("_runner")
    sp.add_argument("--run-dir", required=True)
    sp.add_argument("--project-dir", required=True)
    sp.add_argument("--job", required=True)
    sp.add_argument("--sandbox", required=True, choices=["read-only", "workspace-write"])
    sp.add_argument("--model")

    a = ap.parse_args()
    if hasattr(a, "run_dir"):
        a.run_dir = os.path.abspath(a.run_dir)
    if hasattr(a, "project_dir"):
        a.project_dir = os.path.abspath(a.project_dir)
    if a.cmd == "plan-area":
        a.cmd = "area"
    handlers = {"status": cmd_status, "models": cmd_models, "usage": cmd_usage, "_runner": cmd_runner,
                "prompt": cmd_prompt, "workflow-args": cmd_workflow_args, "ready": cmd_ready, "kill": cmd_kill}
    handlers.update({k: cmd_job(k) for k in KINDS})
    handlers[a.cmd](a)


if __name__ == "__main__":
    main()
