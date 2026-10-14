#!/usr/bin/env python3
"""Fake `codex_task.py` for the power_run.py tests (POWER_CODEX_HELPER=<this file>).

Job subcommands (work, fix, finish, verify, plan, plan-area, review, finalfix, finalverify) answer from the scenario
in MOCK_CODEX_SCENARIO and record every call in MOCK_CODEX_STATE (kind, task/area, model slug, round). `ready` and
`status` are delegated to the real codex_task.py (they touch no Codex binary; `ready` needs CODEX_HOME with a
models_cache.json). `usage` prints a canned line.

Scenario shape: {"<kind>": <answer> | {"<task or area>": <answer>, "*": <answer>}} where an answer is
{"result": {...}} | {"verdict": {...}} | {"files": {"<run-relative path>": <text or json>}} | {"failed": "reason"}
plus optional "edits" (as in mock_claude.py). Without an answer: verify/finalverify pass, the rest fail.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REAL = os.path.join(os.path.dirname(HERE), "codex_task.py")
args = sys.argv[1:]
kind = args[0] if args else ""


def opt(name, default=""):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default


run_dir = opt("--run-dir")
name = opt("--task") or opt("--area")
state_path = os.environ.get("MOCK_CODEX_STATE") or os.path.join(run_dir, "mock_codex_state.json")


def record():
    lock = state_path + ".lock"
    for _ in range(600):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            time.sleep(0.05)
    try:
        try:
            state = json.load(open(state_path, encoding="utf-8"))
        except (OSError, ValueError):
            state = {"calls": []}
        state["calls"].append({"kind": kind, "name": name, "model": opt("--model"), "round": opt("--round"),
                               "cwd": os.getcwd().replace("\\", "/")})
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


if kind == "kill":
    record()   # the harness's kill request is part of the spy record; the real script does the killing
    if os.environ.get("MOCK_CODEX_KILL") == "FAILED":
        # V2-06A fixture: the runner cannot be ended (permission denied, taskkill failed); the harness must not fall back
        print(f"KILL: FAILED\nJOB: {name}\nPID: 123\nREASON: simulated permission denied")
        sys.exit(2)
if kind in ("ready", "status", "workflow-args", "models", "kill"):
    p = subprocess.run([sys.executable, REAL] + args, env=dict(os.environ, PYTHONUTF8="1"))
    sys.exit(p.returncode)
if kind == "usage":
    record()   # PERF-03: the harness must ask for it (--refresh-quota); the spy record proves when it did
    line = json.dumps({"taken_at": "mock", "used_percent": 1, "plan": "mock"})
    print(line)
    if opt("--log"):
        with open(opt("--log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    sys.exit(0)

JOBS = ("work", "fix", "finish", "verify", "plan", "plan-area", "review", "finalfix", "finalverify")
if kind not in JOBS:
    print(f"STATUS: FAILED\nREASON: mock_codex: unknown subcommand {kind}")
    sys.exit(2)

scen = json.load(open(os.environ["MOCK_CODEX_SCENARIO"], encoding="utf-8")) if os.environ.get("MOCK_CODEX_SCENARIO") else {}
record()

ans = scen.get(kind)
if isinstance(ans, dict) and not ({"result", "verdict", "files", "failed", "edits"} & set(ans)):
    ans = ans.get(name) if name in ans else ans.get("*")
if ans is None:
    ans = {"verdict": {"pass": True, "problems": [], "plan_problem": False, "notes": "mock codex"}} \
        if kind in ("verify", "finalverify") else {"failed": "no scenario answer for " + kind}


def failure_object(reason):
    if kind in ("verify", "finalverify"):
        return {"pass": False, "problems": [], "plan_problem": False, "notes": "CODEX_FAILED: " + reason}
    if kind in ("work", "fix", "finish", "finalfix"):
        return {"status": "CODEX_FAILED", "files_changed": [], "mismatch": "", "summary": "CODEX_FAILED: " + reason,
                "how_verified": "", "open_issues": [reason], "plan_change": ""}
    return {"failed": reason}


if ans.get("failed"):
    print(f"STATUS: FAILED\nREASON: {ans['failed']}\nRESULT_JSON: {json.dumps(failure_object(ans['failed']))}\nLOG_TAIL:\n(mock)")
    sys.exit(0)

for e in ans.get("edits") or []:
    p = e["file"]
    if e.get("delete"):
        if os.path.exists(p):
            os.remove(p)
    elif "write" in e:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        open(p, "w", encoding="utf-8", newline="\n").write(e["write"])
    elif "replace" in e:
        s = open(p, encoding="utf-8").read()
        old, new = e["replace"]
        open(p, "w", encoding="utf-8", newline="\n").write(s.replace(old, new))

for rel, content in (ans.get("files") or {}).items():
    path = os.path.join(run_dir, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=1))

if "result" in ans:
    print("STATUS: DONE\nRESULT_JSON: " + json.dumps(ans["result"], ensure_ascii=False))
elif "verdict" in ans:
    print("STATUS: DONE\nRESULT_JSON: " + json.dumps(ans["verdict"], ensure_ascii=False))
else:
    print("STATUS: DONE\nRESULT: wrote " + ", ".join(ans.get("files") or {}))
