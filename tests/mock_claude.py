#!/usr/bin/env python3
"""Fake `claude` for the power_run.py tests (POWER_CLAUDE="python <this file>").

Reads the prompt from stdin like `claude -p`, works out the kind (work / fix / finish / verify for a task; recon,
plan, area, lens, review, finalfix, finalverify by the prompt file named), takes the answer from the scenario
(MOCK_SCENARIO json), applies the scenario's file edits in the project (cwd), writes the run files a real agent
would write, prints the JSON envelope of `claude -p --output-format json` and records the call in MOCK_STATE.

Scenario shape: {"<kind>": <answer> | {"<task or name>": <answer> | [<answer>, ...]}} where an answer is
{"result": {...}} | {"verdict": {...}} | {"text": "..."} | {"dead": true} | {"error": "..."} plus optional
"edits": [{"file": "a.php", "replace": ["OLD", "NEW"]} | {"file": "c.php", "write": "..."} | {"file": "x", "delete": true}]
and "files": {"<run-relative path>": "<text>" | <json>}. A list is consumed one answer per call (the last repeats).
"""
import json
import os
import re
import sys
import time

args = sys.argv[1:]


def opt(name):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else ""


prompt = sys.stdin.read()
scen = json.load(open(os.environ["MOCK_SCENARIO"], encoding="utf-8"))
state_path = os.environ["MOCK_STATE"]



def run_dir_of(text):
    """The run dir from the audit-copy path (PERF-04 inline message) or from the older `Read <file>` message."""
    mm = re.search(r"(?:Audit copy: |Read )(.+?)/prompts/", text)
    return mm.group(1) if mm else None


inline = bool(re.search(r"\(Audit copy: .+?/prompts/", prompt[:800]))   # the instructions travel in the message
m = re.match(r"POWER (work|fix|finish|verify) for task (\S+)\.", prompt)
m2 = None if m else re.match(r"POWER (recon|plan|area|lens|review|finalfix|finalverify)(?: (\S+))?\.", prompt)
if m:
    kind, name, run_dir = m.group(1), m.group(2), run_dir_of(prompt)
elif m2:
    kind, name, run_dir = m2.group(1), m2.group(2) or "", run_dir_of(prompt)
else:
    kind = name = run_dir = None
    for kd, pat in (("recon", r"Read (.+?)/prompts/recon\.([^.]+)\.md"), ("plan", r"Read (.+?)/prompts/plan\.md"),
                    ("area", r"Read (.+?)/prompts/area\.([^.]+)\.md"), ("lens", r"Read (.+?)/prompts/lens\.([^.]+)\.md"),
                    ("review", r"Read (.+?)/prompts/review\.md"), ("finalfix", r"Read (.+?)/prompts/finalfix\.md"),
                    ("finalverify", r"Read (.+?)/prompts/finalverify\.md")):
        mm = re.search(pat, prompt)
        if mm:
            kind, run_dir = kd, mm.group(1)
            name = mm.group(2) if mm.lastindex and mm.lastindex >= 2 else ""
            break
if not kind:
    print(json.dumps({"type": "result", "subtype": "error_unknown_prompt", "is_error": True, "result": prompt[:200]}))
    sys.exit(1)

# ---- the call counter and log (several mocks run at once: a lock file)
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
        state = {"counts": {}, "calls": []}
    key = f"{kind}:{name}"
    idx = state["counts"].get(key, 0)
    state["counts"][key] = idx + 1
    state["calls"].append({"kind": kind, "name": name, "agent": opt("--agent"), "model": opt("--model"),
                           "turns": opt("--max-turns"), "effort": opt("--effort"), "budget": opt("--max-budget-usd"),
                           "perm": opt("--permission-mode"), "cwd": os.getcwd().replace("\\", "/"), "index": idx,
                           "inline": inline, "prompt_chars": len(prompt)})
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
finally:
    try:
        os.remove(lock)
    except OSError:
        pass

# ---- the answer
ans = scen.get(kind)
if isinstance(ans, dict) and ("result" not in ans and "verdict" not in ans and "text" not in ans and "dead" not in ans
                              and "error" not in ans and "files" not in ans and "edits" not in ans):
    ans = ans.get(name) if name in ans else ans.get("*")
if isinstance(ans, list):
    ans = ans[min(idx, len(ans) - 1)] if ans else None
if ans is None:
    ans = {"recon": {"text": "# map\n\n- a.php:2 function a_init() - returns the marker\n- b.php:2 function b_init()\n\nUnmapped: nothing"},
           "lens": {"text": "No findings."}, "verify": {"verdict": {"pass": True, "problems": [], "plan_problem": False, "notes": "mock"}},
           "finalverify": {"verdict": {"pass": True, "problems": [], "plan_problem": False, "notes": "mock"}}}.get(kind, {"text": "done"})

if ans.get("dead"):
    sys.exit(1)
if ans.get("error"):
    print(json.dumps({"type": "result", "subtype": ans["error"], "is_error": True, "result": "", "num_turns": 1, "total_cost_usd": 0.01, "usage": {}}))
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
        if old not in s:
            print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "total_cost_usd": 0.02, "usage": {},
                              "result": json.dumps({"status": "PLAN_MISMATCH", "files_changed": [], "mismatch": f"anchor missing: {p}: {old!r}",
                                                    "summary": "", "how_verified": "", "open_issues": [], "plan_change": ""})}))
            sys.exit(0)
        open(p, "w", encoding="utf-8", newline="\n").write(s.replace(old, new))

for rel, content in (ans.get("files") or {}).items():
    path = os.path.join(run_dir, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=1))

# the file-writing kinds write their default file when the scenario gives none
if kind == "recon" and "files" not in ans:
    os.makedirs(os.path.join(run_dir, "recon"), exist_ok=True)
    open(os.path.join(run_dir, "recon", f"{name}.md"), "w", encoding="utf-8").write(ans.get("text", "map"))
if kind == "lens" and "files" not in ans:
    os.makedirs(os.path.join(run_dir, "review"), exist_ok=True)
    open(os.path.join(run_dir, "review", f"{name}.md"), "w", encoding="utf-8").write(ans.get("text", "No findings."))

if "result" in ans:
    text = "```json\n" + json.dumps(ans["result"], ensure_ascii=False) + "\n```"
elif "verdict" in ans:
    text = "Verdict follows.\n\n```json\n" + json.dumps(ans["verdict"], ensure_ascii=False) + "\n```"
else:
    text = ans.get("text", "done")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 3 + idx, "total_cost_usd": 0.05,
                  "session_id": "mock", "result": text,
                  "usage": {"input_tokens": 10, "cache_creation_input_tokens": 7000, "cache_read_input_tokens": 1000 * (3 + idx),
                            "output_tokens": 300}}, ensure_ascii=False))
