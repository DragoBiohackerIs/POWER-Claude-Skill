#!/usr/bin/env python3
"""Offline tests of power_run.py: a synthetic git project, a fake `claude` (mock_claude.py), real power_check.py
checks (grep in Git Bash). Run: PYTHONUTF8=1 python tests/test_power_run.py [--keep] [names...]

Scenarios: happy (Gate A, continue, Gate B), fix (reject -> fix -> pass), escalate (two rejects -> step 1
finishes; plan_change lands in PLAN.md), mismatch (PLAN_MISMATCH -> restore -> finish), checkbroken (CHECK: ->
BLOCKED, then verify-only releases the dependent tasks and `continue` runs them), dead (executor died -> BLOCKED,
dependent BLOCKED_DEP), resume (interrupted task -> restore -> again), start (START flag runs to Gate B; medium
finding -> finalfix -> finalverify; needs_yes stops), prebroken (baseline stops START), questions (QUESTION risk,
--replan), deploytwice (deploy --go against a fake prod, second deploy keeps the backup), strayreported (a reported
stray file -> BLOCKED), straydiff (a diff-only stray file -> SUSPECT hard stop), noagents (missing agent types stop
`start`), hardstops (the latest final check counts; missing = stop), finalfixmismatch (final fixer PLAN_MISMATCH ->
BLOCKED), smoke (project smoke commands in check --all), codex (Astra step 1 through Codex jobs, mock_codex.py),
codexfallback (every Codex job fails; the fallback never lands on the author), lock (a live run.lock refuses a
second process, a stale one is replaced), cap (POWER_MAX_AGENTS stops the work with INCOMPLETE, continue resumes),
codexkill (the detached runner of a failed Codex job is killed before the fallback), authors (V3-02: accumulated
authors per task choose the reviewer), finalstatus (V3-05: only CLEAN/FIXED closes the review), configbroken (V3-06:
invalid ## POWER json = INFRA stop), readiness (codex executable + login checked in preflight), perfshort (PERF-02: a
failed final check skips lenses and reviewer, `continue --diagnose` runs them; PERF-04: instructions inline in the
message), quota (PERF-03: `codex_task.py usage` only with --refresh-quota), polling (PERF-09: run_job backoff, no fixed
5 s sleep), fast (FAST profile: haiku recon/lenses, --effort medium for work/fix, `"fast": false` lens skipped; a run
without the flag is unchanged), ultraguard (17.10 A: `--disallowedTools Workflow,Agent` on every agent after a `--help`
probe; none on an old CLI), coverage (B: Gate B НЕПРОВЕРЕНО section, coverage questions c1-c3, `deploy --yes cN`),
requestcov (C/D: request coverage - a missing item runs the final fix; dismissed lens findings; the severity rubric),
forecast (E: Gate A cost forecast from 3+ earlier runs). All offline: no network, no credentials, no prod.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
ROOT = os.environ.get("POWER_TEST_ROOT", os.path.join(tempfile.gettempdir(), "prun_test"))
PY = sys.executable
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

def rm_rf(path):
    """rmtree that also removes git's read-only objects on Windows."""
    def on_err(fn, p, exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            fn(p)
        except OSError:
            pass
    if os.path.isdir(path):
        shutil.rmtree(path, onerror=on_err)


A_PHP = "<?php\nfunction a_init() {\n  return 'OLD_A';\n}\n"
B_PHP = "<?php\nfunction b_init() {\n  return 'OLD_B';\n}\n"
CLAUDE_MD = """# test project

## POWER

```json
{"lenses": [{"name": "domain", "focus": "the traps"}, {"name": "smoke", "focus": "the pages"}],
 "deploy": {"profiles": {"shop": {"local_root": ".", "remote_root": "PROD_DIR",
   "get": "cp \\"{remote}\\" \\"{local}\\"", "put": "cp \\"{local}\\" \\"{remote_dir}/\\"", "rm": "rm -f \\"{remote}\\"",
   "ls": "ls \\"{remote_dir}\\"", "canary": [], "pace": 0, "settle": 0}}}}
```
"""
TASKS = {
    "mode": "full", "goal": "flip the markers", "rules": "plain hyphens", "risks": [],
    "contracts": [{"id": "c1", "text": "a_init() returns NEW_A", "provided_by": "t1"}],
    "tasks": [
        {"id": "t1", "title": "a.php returns NEW_A", "spec": "change the return value",
         "files": [{"path": "a.php", "action": "edit"}], "anchors": [{"path": "a.php", "text": "return 'OLD_A'"}],
         "provides": ["c1"], "uses": [], "non_goals": ["no other function"], "acceptance": ["grep NEW_A"],
         "checks": [{"name": "marker", "cmd": "grep -q NEW_A a.php", "expect_exit": 0, "before": "fail"}],
         "review_focus": "the string", "rules": "", "size": {"files": 1, "lines": 1}, "depends_on": []},
        {"id": "t2", "title": "b.php returns NEW_B", "spec": "change the return value",
         "files": [{"path": "b.php", "action": "edit"}], "anchors": [{"path": "b.php", "text": "return 'OLD_B'"}],
         "provides": [], "uses": ["c1"], "non_goals": [], "acceptance": ["grep NEW_B"],
         "checks": [{"name": "marker", "cmd": "grep -q NEW_B b.php", "expect_exit": 0, "before": "fail"}],
         "review_focus": "the string", "rules": "", "size": {"files": 1, "lines": 1}, "depends_on": ["t1"]},
    ],
}
DONE = {"status": "DONE", "files_changed": ["a.php"], "mismatch": "", "summary": "flipped", "how_verified": "grep", "open_issues": [], "plan_change": ""}
PASS = {"pass": True, "problems": [], "plan_problem": False, "notes": "ok"}
FAIL = {"pass": False, "problems": ["a.php:3 - wrong - expected NEW_A"], "plan_problem": False, "notes": ""}
EDIT_A = [{"file": "a.php", "replace": ["OLD_A", "NEW_A"]}]
EDIT_B = [{"file": "b.php", "replace": ["OLD_B", "NEW_B"]}]
REVIEW_CLEAN = {"files": {"REVIEW.md": "clean", "DEPLOY.md": "manual", "review.json": {"verdict": "PASS", "findings": []}}}
PLAN = {"files": {"PLAN.md": "# plan\n\nt1, t2", "tasks.json": TASKS}}


def base_scenario():
    return {"plan": PLAN, "work": {"t1": {"result": DONE, "edits": EDIT_A}, "t2": {"result": dict(DONE, files_changed=["b.php"]), "edits": EDIT_B}},
            "review": REVIEW_CLEAN}


CODEX_CACHE = {"fetched_at": "2026-10-14T00:00:00Z",
               "models": [{"slug": "gpt-5.5-astra", "visibility": "list", "priority": 1, "display_name": "Astra", "description": "frontier"},
                          {"slug": "gpt-5.5-sol", "visibility": "list", "priority": 2, "display_name": "Sol", "description": "workhorse"},
                          {"slug": "gpt-5.5-luna", "visibility": "list", "priority": 3, "display_name": "Luna", "description": "affordable"}]}


class Case:
    def __init__(self, name, claude_md=CLAUDE_MD, agents=True):
        self.name = name
        self.dir = os.path.join(ROOT, name)
        rm_rf(self.dir)
        self.proj = os.path.join(self.dir, "proj")
        os.makedirs(self.proj)
        for fn, txt in (("a.php", A_PHP), ("b.php", B_PHP), ("CLAUDE.md", claude_md)):
            open(os.path.join(self.proj, fn), "w", encoding="utf-8", newline="\n").write(txt)
        if agents:
            # the skill's agent types, installed in the fixture project (the harness refuses to start without them)
            adir = os.path.join(self.proj, ".claude", "agents")
            os.makedirs(adir)
            for fn in os.listdir(os.path.join(SKILL, "agents")):
                shutil.copyfile(os.path.join(SKILL, "agents", fn), os.path.join(adir, fn))
        for cmd in (["git", "init", "-q"], ["git", "add", "."], ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
            subprocess.run(cmd, cwd=self.proj, capture_output=True, creationflags=CREATE_NO_WINDOW)
        self.run = os.path.join(self.proj, "docs", "power", "2026-10-13-test")
        os.makedirs(self.run)
        open(os.path.join(self.dir, "request.md"), "w", encoding="utf-8").write("# Заявка\n\nflip the markers in a.php and b.php\n")
        self.scen_path = os.path.join(self.dir, "scenario.json")
        self.state_path = os.path.join(self.dir, "mock_state.json")
        self.codex_scen_path = os.path.join(self.dir, "codex_scenario.json")
        self.codex_state_path = os.path.join(self.dir, "mock_codex_state.json")
        self.codex_home = os.path.join(self.dir, "codex_home")
        os.makedirs(self.codex_home)
        json.dump(CODEX_CACHE, open(os.path.join(self.codex_home, "models_cache.json"), "w", encoding="utf-8"))
        open(os.path.join(self.codex_home, "auth.json"), "w", encoding="utf-8").write("{}")   # "logged in" (readiness check)
        self.env = {}
        self.failed = []

    def scenario(self, s):
        json.dump(s, open(self.scen_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    def codex_scenario(self, s):
        json.dump(s, open(self.codex_scen_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    def power(self, *argv, timeout=600):
        env = dict(os.environ, PYTHONUTF8="1", POWER_CLAUDE=f'"{PY}" "{os.path.join(HERE, "mock_claude.py")}"',
                   MOCK_SCENARIO=self.scen_path, MOCK_STATE=self.state_path, POWER_MAX_PARALLEL="4", POWER_AGENT_TIMEOUT="120",
                   POWER_CODEX_HELPER=os.path.join(HERE, "mock_codex.py"), MOCK_CODEX_SCENARIO=self.codex_scen_path,
                   MOCK_CODEX_STATE=self.codex_state_path, CODEX_HOME=self.codex_home, CODEX_EXE=PY)
        env.update(self.env)
        p = subprocess.run([PY, os.path.join(SKILL, "power_run.py"), *argv, "--run-dir", self.run, "--project-dir", self.proj],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    def start(self, *extra, step1="opus", step2="sonnet", step3="opus"):
        return self.power("start", "--request-file", os.path.join(self.dir, "request.md"), "--step1", step1, "--step2", step2,
                          "--step3", step3, "--recon", "scope=the two files", *extra)

    def codex_calls(self):
        try:
            return json.load(open(self.codex_state_path, encoding="utf-8"))["calls"]
        except (OSError, ValueError):
            return []

    def harness_calls(self):
        out = []
        try:
            for line in open(os.path.join(self.run, "harness", "calls.jsonl"), encoding="utf-8"):
                out.append(json.loads(line))
        except OSError:
            pass
        return out

    def read(self, *rel):
        return open(os.path.join(self.run, *rel), encoding="utf-8").read()

    def state(self):
        return json.load(open(os.path.join(self.run, "harness", "state.json"), encoding="utf-8"))

    def deploy_token(self):
        """The candidate token (attempt:hash) the last deploy fetch recorded in deploy/state.json, or None."""
        try:
            return json.load(open(os.path.join(self.run, "deploy", "state.json"), encoding="utf-8")).get("candidate")
        except (OSError, ValueError):
            return None

    def results(self):
        return {r["task"]: r for r in json.load(open(os.path.join(self.run, "results.json"), encoding="utf-8"))}

    def calls(self):
        try:
            return json.load(open(self.state_path, encoding="utf-8"))["calls"]
        except (OSError, ValueError):
            return []

    def kinds(self):
        return [f"{c['kind']}:{c['name']}" if c["name"] else c["kind"] for c in self.calls()]

    def check(self, label, cond, info=""):
        print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f"  -> {str(info)[:400]}"))
        if not cond:
            self.failed.append(label)

    def file(self, rel):
        return open(os.path.join(self.proj, rel), encoding="utf-8").read()


def t_happy():
    c = Case("happy")
    c.scenario(base_scenario())
    code, out = c.start()
    st = c.state()
    c.check("start exits 0", code == 0, out[-600:])
    c.check("stops at gate_a", st.get("phase") == "gate_a", st.get("phase"))
    c.check("gate_a.md written", os.path.isfile(os.path.join(c.run, "harness", "gate_a.md")))
    c.check("recon + plan + prepare + baseline", st["recon"]["scope"]["status"] == "ok" and st["prepare"]["ok"] and "OK" in st["baseline"]["summary"], st.get("baseline"))
    c.check("no edits before gate A", "OLD_A" in c.file("a.php"))
    ks = c.kinds()
    c.check("agents so far: recon, plan", ks == ["recon:scope", "plan"], ks)
    code, out = c.power("continue")
    st, r = c.state(), c.results()
    c.check("continue exits 0", code == 0, out[-600:])
    c.check("t1 PASS by sonnet, verified by opus", r["t1"]["status"] == "PASS" and r["t1"]["author"] == "sonnet" and r["t1"]["verified_by"] == ["opus"] and r["t1"]["rounds"] == 1, r["t1"])
    c.check("t2 PASS after t1", r["t2"]["status"] == "PASS", r["t2"])
    c.check("files edited", "NEW_A" in c.file("a.php") and "NEW_B" in c.file("b.php"))
    c.check("check reports exist", os.path.isfile(os.path.join(c.run, "checks", "t1.r1", "report.md")) and os.path.isfile(os.path.join(c.run, "checks", "final", "diff.patch")))
    ks = c.kinds()[2:]
    c.check("work loop agents", sorted(k for k in ks if k.startswith(("work", "verify"))) == ["verify:t1", "verify:t2", "work:t1", "work:t2"], ks)
    c.check("lenses from the project config + review", "lens:domain" in ks and "lens:smoke" in ks and "review" in ks, ks)
    c.check("no runner agents", not any(k.startswith(("check", "restore")) for k in ks), ks)
    c.check("final CLEAN, phase gate_b", st["final"]["status"] == "CLEAN" and st["phase"] == "gate_b", (st["final"].get("status"), st["phase"]))
    rep = open(os.path.join(c.run, "REPORT.md"), encoding="utf-8").read()
    c.check("REPORT.md has the table, cost and no hard stops", "| t1 | PASS |" in rep and "HARNESS:" in rep and "Твърди стопове: няма" in rep, rep[-800:])
    gb = open(os.path.join(c.run, "harness", "gate_b.md"), encoding="utf-8").read()
    c.check("gate_b asks for deploy", "#THE_OPERATOR#, трябва да решиш дали да деплойна" in gb, gb)
    agents = {cc["agent"] for cc in c.calls()}
    c.check("agent types and permission mode passed", all(cc["perm"] == "auto" for cc in c.calls()) and {"power-executor", "power-reader", "power-verifier"} <= agents, (agents, c.calls()[0]))
    code, out = c.power("status")
    c.check("status prints next step", code == 0 and "next:" in out, out[-300:])
    code, out = c.power("cost")
    c.check("cost prints HARNESS line", "HARNESS:" in out, out)
    return c


def t_fix():
    c = Case("fix")
    s = base_scenario()
    s["work"]["t1"] = {"result": DONE, "edits": [{"file": "a.php", "replace": ["OLD_A", "NEW_A_TYPO"]}]}
    s["verify"] = {"t1": [{"verdict": FAIL}, {"verdict": PASS}], "*": {"verdict": PASS}}
    s["fix"] = {"t1": {"result": DONE, "edits": [{"file": "a.php", "replace": ["NEW_A_TYPO", "NEW_A"]}]}}
    c.scenario(s)
    c.start("--flags", "START")
    r = c.results()
    c.check("t1 PASS in round 2 after one fix", r["t1"]["status"] == "PASS" and r["t1"]["rounds"] == 2 and not r["t1"]["escalated"] and r["t1"]["verified_by"] == ["opus", "opus"], r["t1"])
    c.check("fix agent ran once", c.kinds().count("fix:t1") == 1, c.kinds())
    return c


def t_escalate():
    c = Case("escalate")
    s = base_scenario()
    s["verify"] = {"t1": [{"verdict": FAIL}, {"verdict": FAIL}, {"verdict": PASS}], "*": {"verdict": PASS}}
    s["fix"] = {"t1": {"result": DONE}}
    s["finish"] = {"t1": {"result": dict(DONE, plan_change="anchor was fine, executor misread")}}
    c.scenario(s)
    c.start("--flags", "START")
    r = c.results()
    c.check("escalated to opus, verified by fable", r["t1"]["status"] == "PASS" and r["t1"]["escalated"] and r["t1"]["author"] == "opus" and r["t1"]["verified_by"] == ["opus", "opus", "fable"], r["t1"])
    c.check("plan_change in the report", "anchor was fine" in open(os.path.join(c.run, "REPORT.md"), encoding="utf-8").read())
    plan_md = c.read("PLAN.md")
    c.check("4.6 plan_change appended to PLAN.md once", "## Plan changes" in plan_md and plan_md.count("anchor was fine") == 1 and "t1" in plan_md.split("## Plan changes")[-1], plan_md[-400:])
    c.check("no restore on rejections", not os.path.isdir(os.path.join(c.run, "stash")))
    return c


def t_mismatch():
    c = Case("mismatch")
    s = base_scenario()
    s["work"]["t1"] = {"result": {"status": "PLAN_MISMATCH", "files_changed": ["a.php"], "mismatch": "anchor missing: a.php:3", "summary": "", "how_verified": "", "open_issues": [], "plan_change": ""},
                       "edits": [{"file": "a.php", "replace": ["OLD_A", "GARBAGE"]}]}
    s["finish"] = {"t1": {"result": dict(DONE, plan_change="anchor fixed"), "edits": EDIT_A}}
    c.scenario(s)
    c.start("--flags", "START")
    r = c.results()
    c.check("mismatch -> restore -> finish PASS", r["t1"]["status"] == "PASS" and r["t1"]["escalated"] and r["t1"]["author"] == "opus", r["t1"])
    c.check("stash holds the partial edit", os.path.isfile(os.path.join(c.run, "stash", "t1.1", "a.php")) and "GARBAGE" in open(os.path.join(c.run, "stash", "t1.1", "a.php"), encoding="utf-8").read())
    c.check("final file is NEW_A", "NEW_A" in c.file("a.php") and "GARBAGE" not in c.file("a.php"))
    c.check("t2 ran after t1", r["t2"]["status"] == "PASS", r["t2"])
    return c


T3 = {"id": "t3", "title": "c.php created", "spec": "create it", "files": [{"path": "c.php", "action": "create"}], "anchors": [],
      "provides": [], "uses": [], "non_goals": [], "acceptance": ["grep NEW_C"],
      "checks": [{"name": "marker", "cmd": "grep -q NEW_C c.php", "expect_exit": 0, "before": "fail"}],
      "review_focus": "the file", "rules": "", "size": {"files": 1, "lines": 1}, "depends_on": ["t2"]}
WORK_T3 = {"result": dict(DONE, files_changed=["c.php"]), "edits": [{"file": "c.php", "write": "<?php\n// NEW_C\n"}]}


def t_checkbroken():
    c = Case("checkbroken")
    s = base_scenario()
    tasks = json.loads(json.dumps(TASKS))
    tasks["tasks"].append(T3)
    s["plan"] = {"files": {"PLAN.md": "# plan\n\nt1, t2, t3", "tasks.json": tasks}}
    s["work"]["t1"] = {"result": {"status": "PLAN_MISMATCH", "files_changed": ["a.php"], "mismatch": "CHECK: marker - the grep flag is wrong", "summary": "", "how_verified": "", "open_issues": [], "plan_change": ""},
                       "edits": EDIT_A}
    s["work"]["t3"] = WORK_T3
    c.scenario(s)
    c.start("--flags", "START")
    r = c.results()
    c.check("BLOCKED check broken, edits stay", r["t1"]["status"] == "BLOCKED" and r["t1"]["reason"].startswith("check broken (reported by sonnet): CHECK:") and "NEW_A" in c.file("a.php"), r["t1"])
    c.check("t2, t3 BLOCKED_DEP", r["t2"]["status"] == "BLOCKED_DEP" and r["t3"]["status"] == "BLOCKED_DEP", (r["t2"], r["t3"]))
    c.check("no fix, restore or finish", not any(k.startswith(("fix", "finish")) for k in c.kinds()) and not os.path.isdir(os.path.join(c.run, "stash")), c.kinds())
    gb = open(os.path.join(c.run, "harness", "gate_b.md"), encoding="utf-8").read()
    c.check("gate_b asks about a partial deploy", "частично" in gb and "t1" in gb, gb)
    # the orchestrator "fixes" the check command, then verify-only
    code, out = c.power("verify", "--tasks", "t1:sonnet:1")
    r = c.results()
    st = c.state()
    c.check("verify-only PASS for t1", code == 0 and r["t1"]["status"] == "PASS" and r["t1"]["verified_by"] == ["opus"], (out[-300:], r["t1"]))
    # P2-05: the tasks that waited only for t1 are released (the chain too), the phase goes back to work
    c.check("P2-05: BLOCKED_DEP t2 and t3 released from the state", not st["tasks"].get("t2", {}).get("status") and not st["tasks"].get("t3", {}).get("status") and st["phase"] == "work", (st["phase"], st["tasks"].get("t2"), st["tasks"].get("t3")))
    code, out = c.power("continue")
    r = c.results()
    c.check("P2-05: continue runs t2 then t3 to PASS, gate_b", code == 0 and r["t2"]["status"] == "PASS" and r["t3"]["status"] == "PASS" and c.state()["phase"] == "gate_b", (out[-400:], r.get("t2"), r.get("t3")))
    ks = c.kinds()
    c.check("P2-05: t1 not executed again, t2/t3 once", ks.count("work:t1") == 1 and ks.count("work:t2") == 1 and ks.count("work:t3") == 1 and ks.count("verify:t1") == 1, ks)
    c.check("files edited", "NEW_B" in c.file("b.php") and "NEW_C" in c.file("c.php"))
    rep = c.read("REPORT.md")
    c.check("report: no BLOCKED left", "| t2 | PASS |" in rep and "| t3 | PASS |" in rep and "BLOCKED_DEP" not in rep, rep[-600:])
    return c


def t_dead():
    c = Case("dead")
    s = base_scenario()
    s["work"]["t1"] = {"dead": True}
    c.scenario(s)
    c.start("--flags", "START")
    r = c.results()
    c.check("dead executor -> BLOCKED", r["t1"]["status"] == "BLOCKED" and r["t1"]["reason"] == "the executor died", r["t1"])
    c.check("dependent BLOCKED_DEP", r["t2"]["status"] == "BLOCKED_DEP")
    rep = open(os.path.join(c.run, "REPORT.md"), encoding="utf-8").read()
    c.check("report lists the hard stop", "BLOCKED задачи" in rep, rep[-500:])
    return c


def t_resume():
    c = Case("resume")
    c.scenario(base_scenario())
    c.start()
    code, out = c.power("continue")
    # simulate a crash in the middle of t1: an interrupted stage, a half edit, t2 untouched
    st = c.state()
    st["phase"] = "work"
    st["tasks"] = {"t1": {"stage": "work", "started_at": "x"}}
    st.pop("final", None)
    json.dump(st, open(os.path.join(c.run, "harness", "state.json"), "w", encoding="utf-8"))
    open(os.path.join(c.proj, "a.php"), "w", encoding="utf-8", newline="\n").write(A_PHP.replace("OLD_A", "HALF"))
    open(os.path.join(c.proj, "b.php"), "w", encoding="utf-8", newline="\n").write(B_PHP)
    os.remove(os.path.join(c.run, "results.json"))
    json.dump({"counts": {}, "calls": []}, open(c.state_path, "w"))
    code, out = c.power("continue")
    r = c.results()
    c.check("resume: t1 restored and redone, PASS", code == 0 and r["t1"]["status"] == "PASS" and "NEW_A" in c.file("a.php"), (out[-400:], r.get("t1")))
    c.check("resume: stash kept the half edit", any("HALF" in open(os.path.join(dp, f), encoding="utf-8").read()
                                                      for dp, _, fs in os.walk(os.path.join(c.run, "stash")) for f in fs if f == "a.php"))
    c.check("resume: no ready again (snapshot kept)", "t2" in r and r["t2"]["status"] == "PASS", r.get("t2"))
    c.check("resume: gate_b reached", c.state()["phase"] == "gate_b")
    return c


def t_start_finalfix():
    c = Case("start")
    s = base_scenario()
    s["review"] = {"files": {"REVIEW.md": "one medium", "DEPLOY.md": "manual", "deploy.json": {"profile": "shop", "files": [{"local": "a.php", "order": 1, "note": ""}], "db": [{"description": "flag", "apply": "x", "rollback": "y"}],
                                                                                                  "needs_yes": ["flip the flag on prod too?"]},
                             "review.json": {"verdict": "PASS_WITH_FIXES", "findings": [{"severity": "medium", "file": "a.php", "line": 3, "problem": "comment missing", "fix": "add it"}]}}}
    s["finalfix"] = {"result": dict(DONE, summary="comment added"), "edits": [{"file": "a.php", "replace": ["NEW_A", "NEW_A /* ok */"]}]}
    c.scenario(s)
    code, out = c.start("--flags", "START,DEPLOY", "--lens", "only=one lens")
    st = c.state()
    c.check("START runs to gate_b in one go", code == 0 and st["phase"] == "gate_b", (out[-400:], st["phase"]))
    c.check("no gate_a stop (file still written)", os.path.isfile(os.path.join(c.run, "harness", "gate_a.md")) and "START" in open(os.path.join(c.run, "harness", "gate_a.md"), encoding="utf-8").read())
    c.check("finalfix by opus, finalverify by fable, FIXED", st["final"]["status"] == "FIXED" and st["final"]["finalfix"]["by"] == "opus" and st["final"]["finalverify"]["by"] == "fable", st["final"])
    c.check("final2 checks ran", os.path.isdir(os.path.join(c.run, "checks", "final2")))
    c.check("explicit --lens replaces the config lenses", "lens:only" in c.kinds() and "lens:domain" not in c.kinds(), c.kinds())
    gb = open(os.path.join(c.run, "harness", "gate_b.md"), encoding="utf-8").read()
    c.check("DB step is a hard stop in gate_b", "DB/опция/флаг" in gb, gb)
    code, out = c.power("deploy")
    c.check("deploy dry: plan OK, fetch OK, DB hard stop", code == 0 and "PLAN: OK" in out and "FETCH: OK" in out and "HARD STOP: 1 DB" in out, out[-600:])
    c.check("deploy dry: the needs_yes item is a hard stop", "HARD STOP: неотговорен въпрос №1" in out, out[-600:])
    tok = c.deploy_token()
    c.check("deploy dry prints the candidate token and the approved --go command", tok is not None and f"CANDIDATE: {tok}" in out and f"--approved {tok}" in out, (tok, out[-500:]))
    code, out = c.power("deploy", "--go")
    c.check("deploy --go refused by the hard stop", code != 0 and "refused" in out, out[-300:])
    code, out = c.power("deploy", "--go", "--force")
    c.check("--force without --approved is refused", code != 0 and "--approved" in out, out[-300:])
    code, out = c.power("deploy", "--go", "--force", "--approved", tok)
    c.check("--force does not cover an unanswered needs_yes", code != 0 and "needs_yes" in out, out[-300:])
    code, out = c.power("deploy", "--yes", "1")
    c.check("--yes without --approved is refused, nothing recorded", code != 0 and "--approved" in out and not c.state().get("deploy_answers"), (out[-300:], c.state().get("deploy_answers")))
    code, out = c.power("deploy", "--yes", "1", "--approved", "1:000000000000")
    c.check("--yes with the token of another candidate is refused, nothing recorded", code != 0 and "another candidate" in out and not c.state().get("deploy_answers"), (out[-300:], c.state().get("deploy_answers")))
    code, out = c.power("deploy", "--yes", "1", "--approved", tok)
    ans = c.state().get("deploy_answers") or {}
    # (17.10: deploy.json leaves b.php out, so the harness's coverage question c2 stays open - only item 1 is cleared)
    c.check("--yes 1 --approved records the answer bound to the candidate and clears that stop", code == 0 and "неотговорен въпрос №1" not in out and ans.get("1", {}).get("text", "").startswith("flip") and ans.get("1", {}).get("candidate") == tok, (out[-400:], ans))
    code, out = c.power("deploy", "--yes", "7", "--approved", tok)
    c.check("--yes for an unknown item is refused", code != 0 and "no needs_yes item 7" in out, out[-200:])
    # the candidate changes (deploy.json edited): the recorded yes is for the old one and the stop is back
    dj = os.path.join(c.run, "deploy.json")
    spec = json.load(open(dj, encoding="utf-8"))
    spec["files"][0]["note"] = "edited after the yes"
    json.dump(spec, open(dj, "w", encoding="utf-8"))
    code, out = c.power("deploy")
    tok2 = c.deploy_token()
    c.check("an edited deploy.json is another candidate (other token, same attempt)", tok2 is not None and tok2 != tok and tok2.startswith("1:"), (tok, tok2))
    c.check("the old yes is stale: the needs_yes stop is back and names both candidates", "HARD STOP: неотговорен въпрос №1" in out and tok in out and tok2 in out, out[-700:])
    code, out = c.power("deploy", "--go", "--force", "--approved", tok2)
    c.check("--go --force with the new token still needs a fresh yes", code != 0 and "needs_yes" in out, out[-300:])
    return c


def t_deploy_twice():
    """START DEPLOY with a real fake prod: deploy --go uploads; a second deploy keeps the backup and refuses --go."""
    c = Case("deploytwice")
    prod = os.path.join(c.dir, "prod").replace("\\", "/")
    os.makedirs(prod)
    for fn in ("a.php", "b.php"):
        shutil.copyfile(os.path.join(c.proj, fn), os.path.join(prod, fn))
    md = os.path.join(c.proj, "CLAUDE.md")
    text = open(md, encoding="utf-8").read().replace("PROD_DIR", prod)
    open(md, "w", encoding="utf-8", newline="\n").write(text)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "prod dir"], cwd=c.proj, capture_output=True, creationflags=CREATE_NO_WINDOW)
    s = base_scenario()
    s["review"] = {"files": {"REVIEW.md": "clean", "DEPLOY.md": "two files",
                             "deploy.json": {"profile": "shop", "files": [{"local": "a.php", "order": 1}, {"local": "b.php", "order": 2}]},
                             "review.json": {"verdict": "PASS", "findings": []}}}
    c.scenario(s)
    code, out = c.start("--flags", "START,DEPLOY")
    c.check("run reaches gate_b", code == 0 and c.state()["phase"] == "gate_b", out[-300:])
    code, out = c.power("deploy", "--go")
    c.check("deploy --go without --approved is refused after plan + fetch, prod untouched", code != 0 and "--approved" in out and "FETCH: OK" in out and "OLD_A" in open(os.path.join(prod, "a.php")).read(), (code, out[-400:]))
    code, out = c.power("deploy")
    tok = c.deploy_token()
    c.check("deploy dry: candidate token printed with the --go command", code == 0 and tok is not None and f"CANDIDATE: {tok}" in out and f"deploy --go --approved {tok}" in out, (tok, out[-400:]))
    code, out = c.power("deploy", "--go", "--approved", "1:000000000000")
    c.check("deploy --go with another candidate's token is refused, prod untouched", code != 0 and "another candidate" in out and "OLD_A" in open(os.path.join(prod, "a.php")).read(), (code, out[-400:]))
    code, out = c.power("deploy", "--go", "--approved", tok)
    c.check("deploy --go --approved: DEPLOYED, phase done", code == 0 and "DEPLOY: DEPLOYED" in out and c.state()["phase"] == "done", (code, out[-500:]))
    c.check("the harness state keeps the approved token", c.state().get("deploy", {}).get("approved") == tok, c.state().get("deploy"))
    c.check("prod has NEW_A and NEW_B", "NEW_A" in open(os.path.join(prod, "a.php")).read() and "NEW_B" in open(os.path.join(prod, "b.php")).read())
    backup = os.path.join(c.run, "deploy", "prod-before", "a.php")
    c.check("backup holds OLD_A", "OLD_A" in open(backup).read())
    code, out = c.power("deploy")
    c.check("second deploy: no fetch, ALREADY_APPLIED", code == 0 and "ALREADY_APPLIED" in out and "FETCH:" not in out, out[-400:])
    c.check("backup still OLD_A", "OLD_A" in open(backup).read())
    code, out = c.power("deploy", "--go")
    c.check("second deploy --go refused", code != 0 and "ALREADY_APPLIED" in out, out[-300:])
    return c


def t_prebroken():
    c = Case("prebroken")
    s = base_scenario()
    tasks = json.loads(json.dumps(TASKS))
    tasks["tasks"][0]["checks"].append({"name": "lint", "cmd": "grep -q NEVER_THERE a.php", "expect_exit": 0, "before": "pass"})
    s["plan"] = {"files": {"PLAN.md": "# plan", "tasks.json": tasks}}
    c.scenario(s)
    code, out = c.start("--flags", "START")
    st = c.state()
    c.check("PRE-BROKEN baseline stops even with START", st["phase"] == "gate_a" and st["baseline"]["rows"]["PRE-BROKEN"], (st["phase"], st["baseline"]))
    ga = open(os.path.join(c.run, "harness", "gate_a.md"), encoding="utf-8").read()
    c.check("gate_a says why", "Спира дори със START" in ga and "PRE-BROKEN" in ga, ga[-400:])
    c.check("nothing executed", not any(k.startswith("work") for k in c.kinds()), c.kinds())
    return c


def t_questions():
    c = Case("questions")
    s = base_scenario()
    tasks = json.loads(json.dumps(TASKS))
    tasks["risks"] = ["QUESTION: keep the old marker in b.php?"]
    s["plan"] = [{"files": {"PLAN.md": "# plan", "tasks.json": tasks}}, {"files": {"PLAN.md": "# plan v2", "tasks.json": TASKS}}]
    c.scenario(s)
    code, out = c.start("--flags", "START")
    st = c.state()
    c.check("QUESTION risk stops at gate_a even with START", st["phase"] == "gate_a" and st["questions"], (st["phase"], st.get("questions")))
    code, out = c.power("continue", "--replan")
    st = c.state()
    c.check("replan: planner again, then START continues to gate_b", code == 0 and c.kinds().count("plan") == 2 and st["phase"] == "gate_b", (out[-300:], c.kinds(), st["phase"]))
    return c


DEPLOY_REVIEW = {"files": {"REVIEW.md": "clean", "DEPLOY.md": "two files",
                           "deploy.json": {"profile": "shop", "files": [{"local": "a.php", "order": 1}, {"local": "b.php", "order": 2}]},
                           "review.json": {"verdict": "PASS", "findings": []}}}
MEDIUM_REVIEW = {"files": {"REVIEW.md": "one medium", "DEPLOY.md": "manual", "deploy.json": {"profile": "shop", "files": [{"local": "a.php", "order": 1, "note": ""}]},
                           "review.json": {"verdict": "PASS_WITH_FIXES", "findings": [{"severity": "medium", "file": "a.php", "line": 3, "problem": "comment missing", "fix": "add it"}]}}}
FINALFIX_OK = {"result": dict(DONE, summary="comment added"), "edits": [{"file": "a.php", "replace": ["NEW_A", "NEW_A /* ok */"]}]}


def t_stray_reported():
    """P1-04: an executor that admits a file outside its list is BLOCKED, whatever the verifier says."""
    c = Case("strayreported")
    s = base_scenario()
    s["work"]["t1"] = {"result": dict(DONE, files_changed=["a.php", "b.php"]), "edits": EDIT_A}
    c.scenario(s)
    c.start("--flags", "START")
    r = c.results()
    c.check("t1 BLOCKED for the reported stray file, not PASS", r["t1"]["status"] == "BLOCKED" and "b.php" in r["t1"]["reason"] and r["t1"]["stray_files"] == ["b.php"], r["t1"])
    c.check("the verifier still passed it (the block is the harness's)", r["t1"]["verified_by"] == ["opus"] and r["t1"]["last_problems"] == [], r["t1"])
    c.check("t2 BLOCKED_DEP", r["t2"]["status"] == "BLOCKED_DEP", r["t2"])
    rep = c.read("REPORT.md")
    c.check("REPORT names the stray file and the hard stop", "| t1 | BLOCKED |" in rep and "b.php" in rep and "BLOCKED задачи" in rep, rep[-800:])
    return c


def t_stray_diff():
    """P1-04: a file changed outside every task list, seen only in the diff: SUSPECT in the results, a hard stop, no restore."""
    c = Case("straydiff")
    s = base_scenario()
    s["work"]["t1"] = {"result": DONE, "edits": EDIT_A + [{"file": "zzz.php", "write": "<?php // not in any list\n"}]}
    s["review"] = DEPLOY_REVIEW
    c.scenario(s)
    code, out = c.start("--flags", "START")
    r = c.results()
    c.check("t1 PASS but marked: scope_changes carries zzz.php", r["t1"]["status"] == "PASS" and r["t1"].get("scope_changes") == ["zzz.php"], r["t1"])
    c.check("no restore of the stray file", os.path.isfile(os.path.join(c.proj, "zzz.php")))
    rep = c.read("REPORT.md")
    gb = c.read("harness", "gate_b.md")
    c.check("REPORT + gate_b: SUSPECT hard stop with the file", "SUSPECT" in rep and "zzz.php" in rep and "SUSPECT" in gb and "zzz.php" in gb, gb)
    code, out = c.power("deploy", "--go")
    c.check("deploy --go refused by the SUSPECT stop", code != 0 and "HARD STOP: SUSPECT" in out and "refused" in out, out[-500:])
    return c


def t_noagents():
    """4.1: without the agent types the harness refuses to start (preflight), before any agent runs."""
    c = Case("noagents", agents=False)
    home = os.path.join(c.dir, "home")
    os.makedirs(home)
    c.env = {"HOME": home, "USERPROFILE": home}
    c.scenario(base_scenario())
    code, out = c.start("--flags", "START")
    c.check("start refuses: agent types missing", code != 0 and "power-reader" in out and "power-executor" in out and "install" in out, out[-400:])
    c.check("no agent ran, no state written", c.kinds() == [] and not os.path.isfile(os.path.join(c.run, "harness", "state.json")), c.kinds())
    return c


def t_hardstops():
    """4.2: the hard stops read the latest final check (final2 when present); a missing check is not a pass."""
    c = Case("hardstops")
    s = base_scenario()
    s["review"] = DEPLOY_REVIEW
    c.scenario(s)
    c.start("--flags", "START")
    code, out = c.power("deploy")
    c.check("clean run: no hard stop", code == 0 and "HARD STOP" not in out, out[-400:])
    sp = os.path.join(c.run, "harness", "state.json")
    st = c.state()
    st["final"]["check_final2"] = {"pass": False, "failures": ["t1: check 'marker': exit 1, expected 0"], "warnings": [], "round": "final2"}
    json.dump(st, open(sp, "w", encoding="utf-8"))
    code, out = c.power("deploy")
    c.check("final2 FAIL is a hard stop even when final passed", "HARD STOP:" in out and "финална проверка" in out and "final2" in out, out[-500:])
    st["final"].pop("check_final2")
    st["final"].pop("check_final")
    json.dump(st, open(sp, "w", encoding="utf-8"))
    code, out = c.power("deploy")
    c.check("missing final check is a hard stop", "HARD STOP:" in out and "финална проверка" in out, out[-500:])
    code, out = c.power("deploy", "--go")
    c.check("deploy --go refused", code != 0 and "refused" in out, out[-300:])
    return c


def t_finalfix_mismatch():
    """4.3: a final fixer that answers PLAN_MISMATCH blocks the final review instead of passing as FIXED."""
    c = Case("finalfixmismatch")
    s = base_scenario()
    s["review"] = MEDIUM_REVIEW
    s["finalfix"] = {"result": {"status": "PLAN_MISMATCH", "files_changed": [], "mismatch": "the finding points at a line that no longer exists", "summary": "", "how_verified": "", "open_issues": [], "plan_change": ""}}
    c.scenario(s)
    c.start("--flags", "START")
    st = c.state()
    c.check("final BLOCKED with the mismatch", st["final"]["status"] == "BLOCKED" and "PLAN_MISMATCH" in st["final"]["reason"] and "no longer exists" in st["final"]["reason"], st["final"])
    c.check("no final2 check, no finalverify", "finalverify" not in c.kinds() and not os.path.isdir(os.path.join(c.run, "checks", "final2")), c.kinds())
    gb = c.read("harness", "gate_b.md")
    c.check("gate_b: the final review is a hard stop", "финалният преглед не е затворен" in gb, gb)
    return c


def t_smoke():
    """4.7: the project's smoke commands run in `check --all` (final, final2) as checks; a failing one fails the final check."""
    md = CLAUDE_MD.replace('{"lenses":', '{"smoke": ["grep -q NEW_A a.php", "test -f no_such_file_here"], "lenses":')
    c = Case("smoke", claude_md=md)
    s = base_scenario()
    s["review"] = DEPLOY_REVIEW
    c.scenario(s)
    code, out = c.start("--flags", "START")
    st = c.state()
    cf = st["final"]["check_final"]
    c.check("final check FAIL because of the smoke command", not cf["pass"] and any("smoke" in f and "no_such_file_here" in f for f in cf["failures"]), cf)
    cj = json.load(open(os.path.join(c.run, "checks", "final", "check.json"), encoding="utf-8"))
    c.check("check.json carries the smoke block: 1 pass, 1 fail", cj.get("smoke") and cj["smoke"]["pass"] is False and len(cj["smoke"]["rows"]) == 2 and cj["smoke"]["rows"][0]["ok"] and not cj["smoke"]["rows"][1]["ok"], cj.get("smoke"))
    c.check("smoke logs written under checks/final", os.path.isfile(os.path.join(c.run, "checks", "final", "smoke.check1.log")) and os.path.isfile(os.path.join(c.run, "checks", "final", "smoke.check2.log")))
    rep = c.read("checks", "final", "report.md")
    c.check("report.md has a smoke section", "## smoke" in rep and "no_such_file_here" in rep, rep[-600:])
    tj = json.load(open(os.path.join(c.run, "checks", "t1.r1", "check.json"), encoding="utf-8"))
    c.check("per-task checks do not run the smoke", "smoke" not in tj and tj["pass"], tj)
    c.check("tasks PASS, final check is the hard stop", c.results()["t1"]["status"] == "PASS" and "паднала финална проверка" in c.read("harness", "gate_b.md"), c.read("harness", "gate_b.md"))
    return c


def t_codex():
    """P2-06: step 1 Astra - plan, verifiers and finalfix as Codex jobs, finalverify by Opus; results name the real authors."""
    c = Case("codex")
    s = base_scenario()
    s.pop("plan")
    s["review"] = MEDIUM_REVIEW
    c.scenario(s)
    c.codex_scenario({"plan": PLAN, "finalfix": FINALFIX_OK})
    code, out = c.start("--flags", "START", step1="astra", step2="sonnet", step3="opus")
    st, r = c.state(), c.results()
    c.check("run reaches gate_b", code == 0 and st["phase"] == "gate_b", (out[-500:], st.get("phase")))
    c.check("codex slug resolved", st["codex_models"]["astra"] == "gpt-5.5-astra", st.get("codex_models"))
    ck = [f"{x['kind']}:{x['name']}" if x["name"] else x["kind"] for x in c.codex_calls()]
    c.check("codex jobs: plan, verify t1/t2, finalfix", sorted(ck) == ["finalfix", "plan", "verify:t1", "verify:t2"], ck)
    c.check("codex jobs got the slug", all(x["model"] == "gpt-5.5-astra" for x in c.codex_calls()), c.codex_calls())
    c.check("t1 by sonnet, verified by astra", r["t1"]["author"] == "sonnet" and r["t1"]["verified_by"] == ["astra"] and r["t1"]["status"] == "PASS", r["t1"])
    c.check("finalfix by astra, finalverify by opus, FIXED", st["final"]["status"] == "FIXED" and st["final"]["finalfix"]["by"] == "astra" and st["final"]["finalverify"]["by"] == "opus", st["final"])
    c.check("no claude agent was started with a Codex model", not any(x["model"] in ("astra", "sol", "luna") for x in c.calls()), [x["model"] for x in c.calls()])
    hc = c.harness_calls()
    c.check("calls.jsonl records the Codex jobs as astra, ok", sum(1 for x in hc if x["model"] == "astra" and x["ok"]) == 4, [(x["label"], x["model"], x["ok"]) for x in hc])
    c.check("REPORT has the Codex section", "## Codex" in c.read("REPORT.md"))
    return c


def t_codex_fallback():
    """P2-06: every Codex job fails - the Claude fallback never lands on the model that wrote the code."""
    c = Case("codexfallback")
    s = base_scenario()
    s["work"]["t1"] = {"result": DONE, "edits": EDIT_A}
    s["review"] = MEDIUM_REVIEW
    s["finalfix"] = FINALFIX_OK
    c.scenario(s)
    c.codex_scenario({k: {"failed": "sandbox down"} for k in ("plan", "work", "fix", "finish", "verify", "review", "finalfix", "finalverify")})
    code, out = c.start("--flags", "START", step1="astra", step2="opus", step3="fable")
    st, r = c.state(), c.results()
    c.check("run reaches gate_b", code == 0 and st["phase"] == "gate_b", (out[-500:], st.get("phase")))
    models = {(x["kind"], x["name"]): x["model"] for x in c.calls()}
    c.check("plan: codex failed, opus planned", models.get(("plan", "")) == "opus", models)
    c.check("t1 by opus, verified by fable (astra failed; not opus, the author)", r["t1"]["author"] == "opus" and r["t1"]["verified_by"] == ["fable"] and r["t1"]["status"] == "PASS", r["t1"])
    c.check("finalfix fell back to fable (not opus, the executor), finalverify to opus", st["final"]["finalfix"]["by"] == "fable" and st["final"]["finalverify"]["by"] == "opus" and st["final"]["status"] == "FIXED", st["final"])
    c.check("no claude agent was started with a Codex model", not any(x["model"] in ("astra", "sol", "luna") for x in c.calls()), [x["model"] for x in c.calls()])
    hc = c.harness_calls()
    c.check("calls.jsonl: the failed Codex jobs are recorded as astra, not ok", sum(1 for x in hc if x["model"] == "astra" and not x["ok"]) >= 4, [(x["label"], x["model"], x["ok"]) for x in hc])
    return c


def write_lock(c, pid, command="continue"):
    path = os.path.join(c.run, "harness", "run.lock")
    json.dump({"pid": pid, "started_at": "2026-10-14T00:00:00", "command": command, "host": "test"}, open(path, "w", encoding="utf-8"))
    return path


def dead_pid():
    """The PID of a process that has already exited (a stale lock holder)."""
    p = subprocess.Popen([PY, "-c", "pass"], creationflags=CREATE_NO_WINDOW)
    p.wait()
    return p.pid


def lock_held(path):
    """Does some process hold the OS lock of run.lock? (the harness's own probe)"""
    sys.path.insert(0, SKILL)
    import power_run as h
    if not os.path.exists(path):
        return False
    fd = os.open(path, os.O_RDWR)
    try:
        if h.os_lock_try(fd):
            h.os_lock_release(fd)
            return False
        return True
    finally:
        os.close(fd)


def t_lock():
    """5.1 / V2-07: one process per run - while a process holds the OS lock on run.lock, start/continue/verify/deploy
    refuse; a record of a dead holder (no OS lock) is replaced; `status` reads the holder."""
    c = Case("lock")
    c.scenario(base_scenario())
    code, out = c.start()
    c.check("start reaches gate_a", code == 0 and c.state()["phase"] == "gate_a", out[-300:])
    lock = os.path.join(c.run, "harness", "run.lock")
    c.check("lock not held after start", not lock_held(lock))
    # a live holder: this test process takes the OS lock and writes its record
    write_lock(c, os.getpid())
    sys.path.insert(0, SKILL)
    import power_run as h
    fd = os.open(lock, os.O_RDWR)
    c.check("the test process holds the lock", h.os_lock_try(fd))
    try:
        code, out = c.power("continue")
        c.check("continue refused by a live lock, names pid and command", code != 0 and "run.lock" in out and str(os.getpid()) in out and "continue" in out, out[-400:])
        c.check("nothing ran", c.state()["phase"] == "gate_a" and c.kinds() == ["recon:scope", "plan"], c.kinds())
        code, out = c.start("--force")
        c.check("start --force refused by the live lock too", code != 0 and "run.lock" in out, out[-300:])
        code, out = c.power("verify", "--tasks", "t1:sonnet:1")
        c.check("verify refused by the live lock", code != 0 and "run.lock" in out, out[-300:])
        code, out = c.power("deploy")
        c.check("deploy refused by the live lock", code != 0 and "run.lock" in out, out[-300:])
        code, out = c.power("status")
        c.check("status still works and shows the holder", code == 0 and "lock: held" in out and str(os.getpid()) in out, out[-300:])
        c.check("the live lock's record is kept", json.load(open(lock, encoding="utf-8"))["pid"] == os.getpid())
    finally:
        h.os_lock_release(fd)
        os.close(fd)
    # a record of a dead holder without the OS lock: taken over, the log says so
    stale = dead_pid()
    write_lock(c, stale)
    code, out = c.power("status")
    c.check("status: a record without the OS lock is shown as not held", code == 0 and "not held" in out and str(stale) in out, out[-300:])
    code, out = c.power("continue")
    c.check("continue takes the lock and runs to gate_b", code == 0 and c.state()["phase"] == "gate_b", out[-300:])
    log = c.read("harness", "log.txt")
    c.check("log says the dead holder's record was replaced", "replaced" in log and str(stale) in log, [ln for ln in log.splitlines() if "lock" in ln])
    c.check("lock not held after continue", not lock_held(lock))
    return c


def t_cap():
    """5.2: POWER_MAX_AGENTS - the counter stops the work with INCOMPLETE (not PASS); a higher cap and `continue` resume."""
    c = Case("cap")
    c.scenario(base_scenario())
    c.env["POWER_MAX_AGENTS"] = "3"
    code, out = c.start("--flags", "START")
    st, r = c.state(), c.results()
    c.check("start exits 0 and stops in the work phase", code == 0 and st["phase"] == "work", (code, st.get("phase"), out[-300:]))
    c.check("exactly 3 agents started: recon, plan, work t1", c.kinds() == ["recon:scope", "plan", "work:t1"], c.kinds())
    c.check("counter in state.json", st.get("agents_started") == 3, st.get("agents_started"))
    sj = json.load(open(os.path.join(c.run, "harness", "status.json"), encoding="utf-8"))
    c.check("counter and cap in status.json", sj.get("agents_started") == 3 and sj.get("max_agents") == 3, sj)
    c.check("t1 INCOMPLETE (not PASS), reason names the cap", r["t1"]["status"] == "INCOMPLETE" and "POWER_MAX_AGENTS" in r["t1"]["reason"], r["t1"])
    c.check("t2 INCOMPLETE too (waits for t1), not BLOCKED_DEP", r["t2"]["status"] == "INCOMPLETE", r["t2"])
    c.check("gate text written and printed", "INCOMPLETE" in out and "POWER_MAX_AGENTS" in c.read("harness", "gate_b.md"), out[-400:])
    code, sout = c.power("status")
    c.check("status shows the cap and how to go on", code == 0 and "INCOMPLETE" in sout and "POWER_MAX_AGENTS" in sout, sout[-400:])
    c.env["POWER_MAX_AGENTS"] = "0"
    code, out = c.power("continue")
    st, r = c.state(), c.results()
    c.check("continue reaches gate_b", code == 0 and st["phase"] == "gate_b", (code, st.get("phase"), out[-300:]))
    c.check("t1 and t2 PASS now", r["t1"]["status"] == "PASS" and r["t2"]["status"] == "PASS", (r["t1"]["status"], r["t2"]["status"]))
    c.check("t1 restored to the snapshot and redone (work ran twice)", c.kinds().count("work:t1") == 2 and "interrupted" in c.read("harness", "log.txt"), c.kinds())
    c.check("files edited", "NEW_A" in c.file("a.php") and "NEW_B" in c.file("b.php"))
    c.check("counter kept counting", (st.get("agents_started") or 0) > 3, st.get("agents_started"))
    c.check("REPORT has no INCOMPLETE left", "INCOMPLETE" not in c.read("REPORT.md"))
    return c


def t_codex_kill():
    """5.3: a failed Codex job's detached runner is killed before the Claude fallback; done.json records `killed`."""
    c = Case("codexkill")
    s = base_scenario()
    s.pop("plan")
    c.scenario(s)
    c.codex_scenario({"plan": PLAN, "verify": {"failed": "sandbox down"}})
    # the runner of t1's first verify job is still alive when the job reports FAILED (a sleeper stands in for it)
    sleeper = subprocess.Popen([PY, "-c", "import time; time.sleep(300)"], creationflags=CREATE_NO_WINDOW)
    try:
        os.makedirs(os.path.join(c.run, "codex"))
        json.dump({"job": "t1.verify1", "kind": "verify", "sandbox": "read-only", "started_at": "2026-10-14T00:00:00",
                   "pid": sleeper.pid, "model": "gpt-5.5-astra"},
                  open(os.path.join(c.run, "codex", "t1.verify1.state.json"), "w", encoding="utf-8"))
        code, out = c.start("--flags", "START", step1="astra", step2="sonnet", step3="opus")
        st, r = c.state(), c.results()
        c.check("run reaches gate_b", code == 0 and st["phase"] == "gate_b", (out[-500:], st.get("phase")))
        try:
            sleeper.wait(timeout=10)
            dead = True
        except subprocess.TimeoutExpired:
            dead = False
        c.check("the runner process was killed", dead, sleeper.poll())
        done = json.load(open(os.path.join(c.run, "codex", "t1.verify1.done.json"), encoding="utf-8")) \
            if os.path.isfile(os.path.join(c.run, "codex", "t1.verify1.done.json")) else None
        c.check("done.json records the kill", done and done.get("killed") is True and done.get("status") == "KILLED" and done.get("pid") == sleeper.pid, done)
        c.check("no done.json for the job that never started", not os.path.isfile(os.path.join(c.run, "codex", "t2.verify1.done.json")))
        kills = [x for x in c.codex_calls() if x["kind"] == "kill"]
        c.check("kill asked for both failed verify jobs, not for the plan", sorted(x["name"] for x in kills) == ["t1", "t2"] and all(x["model"] == "gpt-5.5-astra" for x in kills), kills)
        log = c.read("harness", "log.txt")
        c.check("log: runner killed for t1, nothing to kill for t2", f"killed (pid {sleeper.pid})" in log and "nothing to kill" in log, [ln for ln in log.splitlines() if "Codex" in ln])
        c.check("t1 PASS, verified by the Claude fallback (opus)", r["t1"]["status"] == "PASS" and r["t1"]["verified_by"] == ["opus"], r["t1"])
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
    return c


# ---------- Astra review v2 (2026-10-14), stage A: "false success" ----------

def prod_case(name, scenario, canary_probes=None, after=None):
    """A Case with a fake prod folder (a.php, b.php at OLD), deploy.json for both files (and the `after` hooks given)
    and, with canary_probes, one canary URL whose answers are canned in order (POWER_PROBE_MAP)."""
    c = Case(name)
    prod = os.path.join(c.dir, "prod").replace("\\", "/")
    os.makedirs(prod)
    for fn in ("a.php", "b.php"):
        shutil.copyfile(os.path.join(c.proj, fn), os.path.join(prod, fn))
    md = os.path.join(c.proj, "CLAUDE.md")
    text = open(md, encoding="utf-8").read().replace("PROD_DIR", prod)
    open(md, "w", encoding="utf-8", newline="\n").write(text)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "prod dir"], cwd=c.proj, capture_output=True, creationflags=CREATE_NO_WINDOW)
    dj = {"profile": "shop", "files": [{"local": "a.php", "order": 1}, {"local": "b.php", "order": 2}]}
    if after is not None:
        dj["after"] = after
    if canary_probes is not None:
        dj["canary"] = [CANARY]
        probes = os.path.join(c.dir, "probes.json")
        json.dump({CANARY: canary_probes}, open(probes, "w", encoding="utf-8"))
        c.env["POWER_PROBE_MAP"] = probes
    scenario["review"] = {"files": {"REVIEW.md": "clean", "DEPLOY.md": "two files", "deploy.json": dj, "review.json": {"verdict": "PASS", "findings": []}}}
    c.scenario(scenario)
    c.prod = prod
    return c


CANARY = "https://canary.test/"


def t_watch_rollback():
    """V2-05: a watch that rolls back (or does not end HEALTHY) is a failed deploy for the harness too: exit 2, a
    HARD STOP line, no phase `done`; only HEALTHY completes the run."""
    ok, down = {"code": 200, "body": "ok"}, {"code": 503, "body": ""}
    c = prod_case("watchrollback", base_scenario(), canary_probes=[ok, ok, down, down, ok])   # before, after, watch, watch2, after-rollback
    code, out = c.start("--flags", "START")
    c.check("run reaches gate_b", code == 0 and c.state()["phase"] == "gate_b", out[-300:])
    code, out = c.power("deploy")
    tok = c.deploy_token()
    code, out = c.power("deploy", "--go", "--approved", tok)
    st = c.state()
    c.check("apply DEPLOYED, watch ROLLED_BACK", st["deploy"]["apply"] == "DEPLOYED" and st["deploy"]["watch"] == "ROLLED_BACK", st.get("deploy"))
    c.check("prod is back at OLD", "OLD_A" in open(os.path.join(c.prod, "a.php")).read())
    c.check("the harness exits 2, not 0", code == 2, (code, out[-400:]))
    c.check("phase is not done", st["phase"] != "done", st["phase"])
    c.check("a HARD STOP line names the rollback", "HARD STOP" in out and "ROLLED_BACK" in out, out[-600:])
    c.check("DEPLOY: line of the harness is the watch outcome, not DEPLOYED", "DEPLOY: ROLLED_BACK" in out.splitlines()[-1] or "HARNESS DEPLOY: ROLLED_BACK" in out, out[-300:])
    code, sout = c.power("status")
    c.check("status says the deploy failed and what follows", code == 0 and "ROLLED_BACK" in sout and ("rollback" in sout or "new-attempt" in sout), sout[-500:])
    code, out = c.power("deploy")
    c.check("a later deploy shows ALREADY_APPLIED (backup kept), exit 0 without --go", code == 0 and "ALREADY_APPLIED" in out, out[-300:])
    # a watch that cannot be judged (malformed output) is not success either
    c2 = prod_case("watchbad", base_scenario())
    c2.start("--flags", "START")
    c2.power("deploy")
    c2.env["POWER_DEPLOY_HELPER"] = os.path.join(HERE, "mock_deploy_watch_broken.py")
    code, out = c2.power("deploy", "--go", "--approved", c2.deploy_token())
    st2 = c2.state()
    c2.check("broken watch output: exit 2, phase not done, HARD STOP", code == 2 and st2["phase"] != "done" and "HARD STOP" in out, (code, st2.get("phase"), out[-400:]))
    c2.check("the watch record keeps the raw outcome", st2["deploy"].get("watch_exit") not in (None, 0), st2.get("deploy"))
    c.failed += c2.failed
    if not c2.failed:
        rm_rf(c2.dir)
    # the healthy case still completes
    c3 = prod_case("watchhealthy", base_scenario(), canary_probes=[ok, ok, ok, ok])
    c3.start("--flags", "START")
    c3.power("deploy")
    code, out = c3.power("deploy", "--go", "--approved", c3.deploy_token())
    c3.check("healthy watch: exit 0, phase done, watch HEALTHY", code == 0 and c3.state()["phase"] == "done" and c3.state()["deploy"]["watch"] == "HEALTHY", (code, c3.state().get("deploy")))
    c.failed += c3.failed
    if not c3.failed:
        rm_rf(c3.dir)
    return c


def t_review_generations():
    """V2-04: every final review is a new generation. A new generation never inherits the previous one's checks,
    review.json or fixer evidence: a failing new check is a hard stop even when the old final2 passed, and a reviewer
    that dies leaves MISSING, not the old verdict."""
    # A: the old final2 passed; the same smoke command fails in the new generation (an env flag, no file changes)
    md = CLAUDE_MD.replace('{"lenses":', '{"smoke": ["test \\"$POWER_T_SMOKE_FAIL\\" != 1"], "lenses":')
    c = Case("reviewgen", claude_md=md)
    s = base_scenario()
    s["review"] = MEDIUM_REVIEW
    s["finalfix"] = FINALFIX_OK
    c.scenario(s)
    c.env["POWER_T_SMOKE_FAIL"] = "0"
    code, out = c.start("--flags", "START")
    st = c.state()
    c.check("generation 1: FIXED with a passing final2", code == 0 and st["final"]["status"] == "FIXED" and st["final"]["check_final2"]["pass"], st.get("final"))
    c.check("generation 1 is numbered", st["final"].get("generation") == 1, st["final"].get("generation"))
    c.env["POWER_T_SMOKE_FAIL"] = "1"
    c.scenario(base_scenario())   # the new reviewer is clean; the smoke now fails
    code, out = c.power("verify", "--tasks", "t1:sonnet:2")
    code, out = c.power("continue")
    st = c.state()
    f = st["final"]
    c.check("generation 2: the new final check FAILS", f.get("check_final", {}).get("pass") is False, f.get("check_final"))
    c.check("no check_final2 inherited from generation 1", "check_final2" not in f and "finalfix" not in f and "finalverify" not in f, sorted(f))
    c.check("generation 2 numbered, generation 1 archived in final_history", f.get("generation") == 2 and len(st.get("final_history") or []) == 1 and st["final_history"][0].get("generation") == 1 and st["final_history"][0].get("status") == "FIXED", (f.get("generation"), st.get("final_history")))
    gb = c.read("harness", "gate_b.md")
    c.check("the failed final check is a hard stop in gate_b", "паднала финална проверка" in gb and "final)" in gb, gb)
    c.check("final status is not CLEAN/FIXED after a failed check", f.get("status") not in ("CLEAN", "FIXED"), f.get("status"))
    code, out = c.power("deploy")
    c.check("deploy dry shows the hard stop", "HARD STOP" in out and "финална проверка" in out, out[-500:])
    c.check("generation 1 evidence archived (review.json of gen 1 kept aside)", os.path.isfile(os.path.join(c.run, "archive", "final.g1", "review.json")), os.listdir(os.path.join(c.run, "archive")) if os.path.isdir(os.path.join(c.run, "archive")) else "no archive")
    # B: clean first review; the second generation's reviewer dies -> MISSING, hard stop, old review.json not consumed
    c2 = Case("reviewdead")
    c2.scenario(base_scenario())
    code, out = c2.start("--flags", "START")
    c2.check("B generation 1: CLEAN", code == 0 and c2.state()["final"]["status"] == "CLEAN", c2.state().get("final"))
    s = base_scenario()
    s["review"] = {"dead": True}
    c2.scenario(s)
    c2.power("verify", "--tasks", "t1:sonnet:2")
    code, out = c2.power("continue")
    st = c2.state()
    rec = [x for x in c2.harness_calls() if x["role"] == "review"][-1]
    c2.check("B: the new reviewer died", rec["ok"] is False, rec)
    c2.check("B: final is MISSING, not CLEAN", st["final"].get("status") == "MISSING" and st["final"]["review"]["verdict"] == "MISSING", st.get("final"))
    c2.check("B: no review.json of generation 2 in the run dir", not os.path.isfile(os.path.join(c2.run, "review.json")), os.listdir(c2.run))
    c2.check("B: hard stop in gate_b", "финалният преглед не е затворен" in c2.read("harness", "gate_b.md"), c2.read("harness", "gate_b.md"))
    code, out = c2.power("deploy", "--go", "--approved", "1:000000000000")
    c2.check("B: deploy --go refused", code != 0, out[-300:])
    c.failed += [f"B {x}" for x in c2.failed]
    if not c2.failed:
        rm_rf(c2.dir)
    # C: Codex review jobs are versioned by generation (the real helper reuses a finished job of the same name)
    c3 = Case("reviewgencodex")
    c3.scenario(base_scenario())
    c3.codex_scenario({"review": REVIEW_CLEAN})
    code, out = c3.start("--flags", "START", step1="opus", step2="sonnet", step3="astra")
    c3.check("C generation 1 via Codex review: CLEAN", code == 0 and c3.state()["final"]["status"] == "CLEAN" and c3.state()["final"]["review"]["by"] == "astra", (out[-400:], c3.state().get("final")))
    c3.codex_scenario({"review": {"failed": "sandbox down"}})
    s2 = base_scenario()
    s2["review"] = {"dead": True}   # the Claude fallback dies too
    c3.scenario(s2)
    c3.power("verify", "--tasks", "t1:sonnet:2")
    code, out = c3.power("continue")
    reviews = [x for x in c3.codex_calls() if x["kind"] == "review"]
    c3.check("C: the Codex review job ran again in generation 2 (two review jobs)", len(reviews) == 2, reviews)
    c3.check("C: generation 2 is MISSING, the generation-1 verdict is not reused", c3.state()["final"].get("status") == "MISSING", c3.state().get("final"))
    c.failed += [f"C {x}" for x in c3.failed]
    if not c3.failed:
        rm_rf(c3.dir)
    return c


def t_kill_failed_no_fallback():
    """V2-06A: when the failed Codex job's runner cannot be killed, no Claude fallback starts (it could edit under a
    still-running runner); the role ends as died/BLOCKED with the reason, and the log says how to go on."""
    c = Case("killfailed")
    s = base_scenario()
    s.pop("plan")
    c.scenario(s)
    c.codex_scenario({"plan": PLAN, "verify": {"failed": "sandbox down"}})
    c.env["MOCK_CODEX_KILL"] = "FAILED"
    code, out = c.start("--flags", "START", step1="astra", step2="sonnet", step3="opus")
    st, r = c.state(), c.results()
    c.check("run reaches gate_b (nothing hangs)", code == 0 and st["phase"] == "gate_b", (out[-500:], st.get("phase")))
    c.check("no Claude verifier started for t1 or t2", not any(k.startswith("verify:") for k in c.kinds()), c.kinds())
    c.check("t1 is BLOCKED, reason names the kill", r["t1"]["status"] == "BLOCKED" and "kill" in (r["t1"].get("reason") or "").lower(), r["t1"])
    log = c.read("harness", "log.txt")
    c.check("log: fallback refused because the runner could not be killed", "could not kill" in log and "no fallback" in log, [ln for ln in log.splitlines() if "kill" in ln.lower()])
    c.check("gate_b lists BLOCKED tasks as a hard stop", "BLOCKED задачи" in c.read("harness", "gate_b.md"))
    c.check("t2 (depends on t1) is BLOCKED_DEP, never verified", r["t2"]["status"] == "BLOCKED_DEP", r["t2"])
    kills = [x for x in c.codex_calls() if x["kind"] == "kill"]
    c.check("kill was asked once, for t1's failed verify", [x["name"] for x in kills] == ["t1"], kills)
    return c


LOCK_WORKER = """
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
import power_run as h
run_dir, folder, role = sys.argv[2], sys.argv[3], sys.argv[4]
while not os.path.exists(os.path.join(folder, "go")):
    time.sleep(0.005)
try:
    r = h.Run(run_dir, os.path.dirname(os.path.dirname(os.path.dirname(run_dir))))
    r.hold(role)
except SystemExit as e:
    open(os.path.join(folder, role + ".refused"), "w").write(str(e))
    sys.exit(0)
open(os.path.join(folder, role + ".acquired"), "w").write(str(os.getpid()))
while not os.path.exists(os.path.join(folder, "end")):
    time.sleep(0.01)
"""


def t_lock_atomic():
    """V2-07: ownership of run.lock is an OS lock on the open handle, not the lock file's existence or content: N
    processes that race for one run give exactly one holder; an empty or unreadable lock file is never 'stale'; a
    holder that dies releases it without manual cleanup; `status` reads it while held."""
    c = Case("lockatomic")
    c.scenario(base_scenario())
    c.start()
    folder = os.path.join(c.dir, "race")
    os.makedirs(folder)
    procs = [subprocess.Popen([PY, "-c", LOCK_WORKER, SKILL, c.run, folder, f"w{i}"], creationflags=CREATE_NO_WINDOW,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for i in range(6)]
    try:
        open(os.path.join(folder, "go"), "w").close()
        import time
        deadline = time.time() + 30
        while time.time() < deadline:
            done = [f for f in os.listdir(folder) if f.endswith((".acquired", ".refused"))]
            if len(done) >= 6:
                break
            time.sleep(0.05)
        acquired = [f for f in os.listdir(folder) if f.endswith(".acquired")]
        refused = [f for f in os.listdir(folder) if f.endswith(".refused")]
        c.check("six racing processes: exactly one holder, five refused", len(acquired) == 1 and len(refused) == 5, (acquired, refused))
        if refused:
            msg = open(os.path.join(folder, refused[0]), encoding="utf-8").read()
            c.check("the refusal names the holder's pid", acquired and open(os.path.join(folder, acquired[0])).read() in msg, msg[:300])
        code, out = c.power("status")
        c.check("status works while the lock is held and shows it", code == 0 and "lock" in out and "held" in out, out[-400:])
        code, out = c.power("continue")
        c.check("continue refused while a worker holds the lock", code != 0 and "run.lock" in out, out[-300:])
        # the holder dies without releasing: the next process takes the lock with no manual cleanup
        holder_pid = int(open(os.path.join(folder, acquired[0])).read()) if acquired else None
        for p in procs:
            if p.pid == holder_pid:
                p.kill()
                p.wait(timeout=10)
    finally:
        open(os.path.join(folder, "end"), "w").close()
        for p in procs:
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
    code, out = c.power("continue")
    c.check("after the holder's death: continue takes the lock and runs to gate_b", code == 0 and c.state()["phase"] == "gate_b", out[-400:])
    log = c.read("harness", "log.txt")
    c.check("log mentions the record of the dead holder being replaced", "replaced" in log and str(holder_pid) in log, [ln for ln in log.splitlines() if "lock" in ln])
    # an empty lock file (a holder mid-publication, or a crash of an older version) is not proof of a dead owner
    lock = os.path.join(c.run, "harness", "run.lock")
    open(lock, "w").close()
    fd = os.open(lock, os.O_RDWR)
    try:
        import power_run as h_mod  # noqa: F401  (imported for the OS lock helper)
    except ImportError:
        sys.path.insert(0, SKILL)
    import power_run as h
    held = h.os_lock_try(fd)
    c.check("test process can hold the OS lock on an empty file", held)
    code, out = c.power("deploy")
    c.check("an empty but held lock file refuses, is not treated as stale", code != 0 and "run.lock" in out and "stale" not in out.lower(), out[-300:])
    h.os_lock_release(fd)
    os.close(fd)
    code, out = c.power("status")
    c.check("status after release: not held", code == 0 and "held by" not in out, out[-300:])
    return c


# ---------- Astra review v2 (2026-10-14), stage B: "complete evidence" ----------

def t_deploy_sealed():
    """V2-11 in the harness: `deploy` must consume a sealed candidate (a hand-resolved conflict) instead of fetching
    the conflict back; `--refetch` is the explicit way to drop the seal. The old token never approves the resolution."""
    c = prod_case("sealed", base_scenario())
    with open(os.path.join(c.prod, "a.php"), encoding="utf-8") as f:
        prod_a = f.read()
    with open(os.path.join(c.prod, "a.php"), "w", encoding="utf-8", newline="\n") as f:
        f.write(prod_a.replace("OLD_A", "PROD_A"))     # the same line changed on prod: a real conflict
    code, out = c.start("--flags", "START")
    c.check("run reaches gate_b", code == 0 and c.state()["phase"] == "gate_b", out[-300:])
    code, out = c.power("deploy")
    tok0 = c.deploy_token()
    c.check("deploy dry: the conflict is a HARD STOP with a token", "HARD STOP" in out and "conflict" in out.lower() and tok0, (tok0, out[-500:]))
    ds = json.load(open(os.path.join(c.run, "deploy", "state.json"), encoding="utf-8"))
    row = next(r for r in ds["files"] if r["local"] == "a.php")
    resolved = A_PHP.replace("OLD_A", "RESOLVED_A")
    with open(row["upload_from"], "w", encoding="utf-8", newline="\n") as f:
        f.write(resolved)

    def deploy_tool(*argv):
        p = subprocess.run([PY, os.path.join(SKILL, "power_deploy.py"), *argv, "--run-dir", c.run, "--project-dir", c.proj],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=dict(os.environ, PYTHONUTF8="1"), creationflags=CREATE_NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    code, out = deploy_tool("seal")
    tok1 = c.deploy_token()
    c.check("seal: OK with a new token", code == 0 and "SEAL: OK" in out and tok1 and tok1 != tok0, (code, tok0, tok1, out[-300:]))
    code, out = c.power("deploy")
    c.check("deploy after the seal keeps the sealed candidate: no fetch, same token, no conflict stop", code == 0 and "FETCH:" not in out and c.deploy_token() == tok1 and f"--approved {tok1}" in out and "HARD STOP" not in out, (code, c.deploy_token(), out[-500:]))
    c.check("  the output says the candidate is sealed", "SEALED" in out or "sealed" in out, out[-400:])
    code, out = c.power("deploy", "--go", "--approved", tok0)
    c.check("the pre-resolution token is refused by the harness, prod untouched", code != 0 and "another candidate" in out and "PROD_A" in open(os.path.join(c.prod, "a.php"), encoding="utf-8").read(), (code, out[-300:]))
    code, out = c.power("deploy", "--refetch")
    c.check("deploy --refetch drops the seal: the fetch runs and the conflict is back", "FETCH:" in out and "HARD STOP" in out and c.deploy_token() == tok0, (code, c.deploy_token(), out[-400:]))
    with open(row["upload_from"], "w", encoding="utf-8", newline="\n") as f:
        f.write(resolved)
    code, out = deploy_tool("seal")
    c.check("sealed again: the same resolution gives the same token", code == 0 and c.deploy_token() == tok1, (code, c.deploy_token(), out[-200:]))
    code, out = c.power("deploy", "--go", "--approved", tok1)
    c.check("deploy --go with the sealed token: DEPLOYED, phase done, prod has the resolution", code == 0 and "DEPLOY: DEPLOYED" in out and c.state()["phase"] == "done" and "RESOLVED_A" in open(os.path.join(c.prod, "a.php"), encoding="utf-8").read(), (code, out[-400:]))
    c.check("the harness recorded the sealed token as approved", c.state().get("deploy", {}).get("approved") == tok1, c.state().get("deploy"))
    return c


# ---------- Astra review v2 (2026-10-14), stage C: "provenance and policy" ----------

STRAY_T1 = {"result": dict(DONE, files_changed=["a.php", "b.php"]),
            "edits": EDIT_A + [{"file": "b.php", "write": B_PHP + "// written outside t1\n"}]}


def t_verify_keeps_stray():
    """V2-10: verify-only must not erase a reported scope violation. The new verdict is merged into the old outcome
    (result, author, strays, rounds kept); the task stays BLOCKED and its dependants stay BLOCKED_DEP until the
    violation is really resolved: the stray file is back at the snapshot, or the plan now lists it for the task."""
    c = Case("verifystray")
    s = base_scenario()
    s["work"]["t1"] = STRAY_T1
    c.scenario(s)
    c.start("--flags", "START")
    r = c.results()
    c.check("t1 BLOCKED (stray b.php), t2 BLOCKED_DEP", r["t1"]["status"] == "BLOCKED" and r["t1"]["stray_files"] == ["b.php"] and r["t2"]["status"] == "BLOCKED_DEP", (r["t1"], r["t2"]))
    code, out = c.power("verify", "--tasks", "t1:sonnet:2")
    r, st = c.results(), c.state()
    t1 = r["t1"]
    c.check("verify-only with a clean verdict keeps t1 BLOCKED for the stray file", code == 0 and t1["status"] == "BLOCKED" and t1["stray_files"] == ["b.php"] and "b.php" in t1["reason"], (out[-300:], t1))
    c.check("  the old evidence survives: author, result, both verdicts", t1["author"] == "sonnet" and (t1.get("result") or {}).get("summary") == "flipped" and t1["verified_by"] == ["opus", "opus"] and t1["rounds"] == 2, t1)
    c.check("  t2 stays BLOCKED_DEP, nothing released, phase work_done", (st["tasks"].get("t2") or {}).get("status") == "BLOCKED_DEP" and st["phase"] == "work_done", (st["phase"], st["tasks"].get("t2")))
    c.check("  the out-of-scope edit is still in b.php (nothing restored by the harness)", "outside t1" in c.file("b.php"))
    code, out = c.power("continue")
    gb = c.read("harness", "gate_b.md")
    rep = c.read("REPORT.md")
    c.check("continue: the hard stop is still there (BLOCKED t1, t2), REPORT shows the stray", "BLOCKED задачи" in gb and "t1" in gb and "| t1 | BLOCKED |" in rep and "b.php" in rep and "BLOCKED_DEP" in rep, (gb[-500:], rep[-600:]))
    # explicit resolution 1: the orchestrator puts the stray file back to the snapshot, then verify-only again
    open(os.path.join(c.proj, "b.php"), "w", encoding="utf-8", newline="\n").write(B_PHP)
    code, out = c.power("verify", "--tasks", "t1:sonnet:3")
    r, st = c.results(), c.state()
    t1 = r["t1"]
    c.check("stray file restored to the snapshot: verify-only now PASSes t1 and records the resolution", code == 0 and t1["status"] == "PASS" and t1["stray_files"] == [] and "b.php" in json.dumps(t1.get("stray_resolved") or {}), (out[-300:], t1))
    c.check("  t2 released for the work loop, phase work", not st["tasks"].get("t2", {}).get("status") and st["phase"] == "work", (st["phase"], st["tasks"].get("t2")))
    code, out = c.power("continue")
    r = c.results()
    c.check("  continue: t2 PASS, gate_b, no BLOCKED left", code == 0 and r["t2"]["status"] == "PASS" and c.state()["phase"] == "gate_b" and "BLOCKED задачи" not in c.read("harness", "gate_b.md"), (out[-300:], r.get("t2")))
    c.check("  t1 was never executed again", c.kinds().count("work:t1") == 1 and c.kinds().count("verify:t1") == 3, c.kinds())
    # explicit resolution 2: the plan now lists the file for the task (tasks.json edited by the orchestrator)
    c2 = Case("verifystray2")
    c2.scenario(s)
    c2.start("--flags", "START")
    tj = os.path.join(c2.run, "tasks.json")
    plan = json.load(open(tj, encoding="utf-8"))
    plan["tasks"][0]["files"].append({"path": "b.php", "action": "edit"})
    json.dump(plan, open(tj, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    code, out = c2.power("verify", "--tasks", "t1:sonnet:2")
    t1 = c2.results()["t1"]
    c2.check("plan change (b.php added to t1 in tasks.json): verify-only PASSes, resolution recorded", code == 0 and t1["status"] == "PASS" and t1["stray_files"] == [] and "b.php" in json.dumps(t1.get("stray_resolved") or {}), (out[-300:], t1))
    pm = c2.read("PLAN.md")
    c2.check("  the resolution is a line in PLAN.md 'Plan changes'", "## Plan changes" in pm and "b.php" in pm.split("## Plan changes")[-1] and "t1" in pm.split("## Plan changes")[-1], pm[-500:])
    c2.check("  the edit outside stays (it is in scope now)", "outside t1" in c2.file("b.php"))
    # a verify-only spec that names a file the plan does not list is still a stray: no silent clearing
    c3 = Case("verifystray3")
    c3.scenario(s)
    c3.start("--flags", "START")
    code, out = c3.power("verify", "--tasks", "t1:sonnet:2")
    code, out = c3.power("verify", "--tasks", "t1:sonnet:3")
    t1 = c3.results()["t1"]
    c3.check("two verify-only passes in a row never clear the stray", t1["status"] == "BLOCKED" and t1["stray_files"] == ["b.php"] and t1["rounds"] == 3, t1)
    c.failed += c2.failed + c3.failed
    if not c2.failed:
        rm_rf(c2.dir)
    if not c3.failed:
        rm_rf(c3.dir)
    return c


def t_review_real_author():
    """V2-13: the final reviewer (and the escalation fallback) are chosen by the REAL authors of the retained code
    (results.json after any Codex fallback), never by the configured step 2. Mixed authors: an independent model if
    one exists (the other strong Claude, or Astra), else the review is marked not independent and is a hard stop."""
    # (a) Astra's probe: step2 Astra fails, Opus writes everything, step3 Opus must not review its own code
    c = Case("realauthor")
    c.scenario(base_scenario())
    c.codex_scenario({"work": {"failed": "simulated outage"}})
    code, out = c.start("--flags", "START", step1="fable", step2="astra", step3="opus")
    st, r = c.state(), c.results()
    authors = sorted({x["author"] for x in r.values()})
    rv = st["final"]["review"]
    c.check("(a) run reaches gate_b, opus is the real author of both tasks", code == 0 and st["phase"] == "gate_b" and authors == ["opus"], (out[-300:], authors))
    c.check("(a) per-task verifier is fable", all(x["verified_by"] == ["fable"] for x in r.values()), [x["verified_by"] for x in r.values()])
    c.check("(a) final review by fable (not opus, the real author), marked independent", rv["by"] == "fable" and rv.get("independent") is True and rv.get("authors") == ["opus"], rv)
    c.check("(a) no hard stop about the review", "не е независим" not in c.read("harness", "gate_b.md"), c.read("harness", "gate_b.md")[-400:])
    # (b) escalation: step3 Opus is also the escalation author of t1 -> fable reviews
    c2 = Case("realauthor2")
    s = base_scenario()
    s["verify"] = {"t1": [{"verdict": FAIL}, {"verdict": FAIL}, {"verdict": PASS}], "*": {"verdict": PASS}}
    s["fix"] = {"t1": {"result": DONE}}
    s["finish"] = {"t1": {"result": DONE}}
    c2.scenario(s)
    c2.start("--flags", "START", step1="opus", step2="sonnet", step3="opus")
    st2, r2 = c2.state(), c2.results()
    rv2 = st2["final"]["review"]
    c2.check("(b) t1 by opus after escalation, t2 by sonnet", r2["t1"]["author"] == "opus" and r2["t2"]["author"] == "sonnet", (r2["t1"]["author"], r2["t2"]["author"]))
    c2.check("(b) final review by fable, authors [opus, sonnet], independent", rv2["by"] == "fable" and rv2.get("independent") is True and rv2.get("authors") == ["opus", "sonnet"], rv2)
    # (c) both strong Claude models authored retained code and no Codex model is in the run: no independent reviewer
    c3 = Case("realauthor3")
    s3 = base_scenario()
    s3["verify"] = {"t1": [{"verdict": FAIL}, {"verdict": FAIL}, {"verdict": PASS}], "*": {"verdict": PASS}}
    s3["fix"] = {"t1": {"result": DONE}}
    s3["finish"] = {"t1": {"result": DONE}}
    s3["review"] = DEPLOY_REVIEW
    c3.scenario(s3)
    c3.start("--flags", "START", step1="opus", step2="fable", step3="opus")
    st3, r3 = c3.state(), c3.results()
    rv3 = st3["final"]["review"]
    c3.check("(c) t1 by opus (escalation), t2 by fable", r3["t1"]["author"] == "opus" and r3["t2"]["author"] == "fable", (r3["t1"]["author"], r3["t2"]["author"]))
    c3.check("(c) the review is explicitly NOT independent and names the overlap", rv3.get("independent") is False and rv3.get("authors") == ["fable", "opus"] and rv3["by"] in ("opus", "fable"), rv3)
    gb3 = c3.read("harness", "gate_b.md")
    c3.check("(c) hard stop: the final review is not independent", "не е независим" in gb3 and rv3["by"] in gb3, gb3[-600:])
    code, out = c3.power("deploy", "--go")
    c3.check("(c) deploy --go refused by that stop", code != 0 and "refused" in out and "не е независим" in out, out[-500:])
    # (d) the escalation fallback: step1 Astra's finish job fails after Astra's work job failed (Opus wrote the code);
    #     the fallback must be fable, not opus (the author), and the review is by a non-author (Astra job or fable)
    c4 = Case("realauthor4")
    s4 = base_scenario()
    s4["verify"] = {"t1": [{"verdict": FAIL}, {"verdict": FAIL}, {"verdict": PASS}], "*": {"verdict": PASS}}
    s4["fix"] = {"t1": {"result": DONE}}
    s4["finish"] = {"t1": {"result": DONE}}
    c4.scenario(s4)
    c4.codex_scenario({"work": {"failed": "outage"}, "verify": {"failed": "outage"}, "finish": {"failed": "outage"}, "review": {"failed": "outage"}})
    code, out = c4.start("--flags", "START", step1="astra", step2="astra", step3="opus")
    st4, r4 = c4.state(), c4.results()
    rv4 = st4["final"]["review"]
    c4.check("(d) run reaches gate_b", code == 0 and st4["phase"] == "gate_b", out[-400:])
    c4.check("(d) t1 escalation fell back to fable (not opus, the real author of the rejected code)", r4["t1"]["author"] == "fable" and r4["t1"]["escalated"] and r4["t2"]["author"] == "opus", (r4["t1"].get("author"), r4["t2"].get("author")))
    c4.check("(d) opus and fable both authored: the review is marked not independent (Astra failed)", rv4.get("authors") == ["fable", "opus"] and rv4.get("independent") is False, rv4)
    c4.check("(d) hard stop present", "не е независим" in c4.read("harness", "gate_b.md"), c4.read("harness", "gate_b.md")[-500:])
    for x in (c2, c3, c4):
        c.failed += x.failed
        if not x.failed:
            rm_rf(x.dir)
    return c


def t_plan_change_history():
    """V2-15: every plan_change an agent reports (work, fix, finish, finalfix) is kept in an append-only history and
    reaches PLAN.md "Plan changes" and REPORT; a later empty field never erases an earlier report; no duplicates on
    a second pass (verify-only, a new review generation)."""
    c = Case("planchanges")
    s = base_scenario()
    s["work"]["t1"] = {"result": dict(DONE, plan_change="A documented adjustment made by the first executor."), "edits": EDIT_A}
    s["verify"] = {"t1": [{"verdict": FAIL}, {"verdict": PASS}], "*": {"verdict": PASS}}
    s["fix"] = {"t1": {"result": dict(DONE, plan_change="")}}
    s["review"] = MEDIUM_REVIEW
    s["finalfix"] = {"result": dict(DONE, summary="comment added", plan_change="the final fix also renamed the comment marker"),
                     "edits": [{"file": "a.php", "replace": ["NEW_A", "NEW_A /* ok */"]}]}
    c.scenario(s)
    code, out = c.start("--flags", "START")
    st, r = c.state(), c.results()
    c.check("run reaches gate_b, t1 PASS after the fix, final FIXED", code == 0 and st["phase"] == "gate_b" and r["t1"]["status"] == "PASS" and st["final"]["status"] == "FIXED", (out[-300:], r["t1"]["status"], st["final"].get("status")))
    pm, rep = c.read("PLAN.md"), c.read("REPORT.md")
    c.check("the first executor's plan_change is in PLAN.md although the fixer reported none", "A documented adjustment" in pm.split("## Plan changes")[-1], pm[-500:])
    c.check("  and in REPORT", "A documented adjustment" in rep, rep[-800:])
    c.check("  and in results.json as the task's history", any("A documented adjustment" in x.get("text", "") for x in r["t1"].get("plan_changes") or []), r["t1"].get("plan_changes"))
    c.check("the finalfix plan_change is in PLAN.md and REPORT", "renamed the comment marker" in pm.split("## Plan changes")[-1] and "renamed the comment marker" in rep, (pm[-500:], rep[-800:]))
    hist = st.get("plan_changes") or []
    c.check("state keeps the history with provenance (task, kind, by)", [(h["task"], h["kind"], h["by"]) for h in hist] == [("t1", "work", "sonnet"), ("final", "finalfix", "opus")], hist)
    # a second pass: verify-only of t1 and a new review generation; nothing is lost, nothing is duplicated
    code, out = c.power("verify", "--tasks", "t1:sonnet:3")
    code, out = c.power("continue")
    pm, rep, st = c.read("PLAN.md"), c.read("REPORT.md"), c.state()
    c.check("after verify-only + continue: both changes once in PLAN.md", pm.count("A documented adjustment") == 1 and pm.count("renamed the comment marker") == 1, pm[-600:])
    c.check("  both once in REPORT", rep.count("A documented adjustment") == 1 and rep.count("renamed the comment marker") == 1, rep[-800:])
    c.check("  history not duplicated by the second review generation", len(st.get("plan_changes") or []) == 2, st.get("plan_changes"))
    c.check("  verify-only kept the task's history", any("A documented adjustment" in x.get("text", "") for x in c.results()["t1"].get("plan_changes") or []), c.results()["t1"].get("plan_changes"))
    return c


# ---------- Astra review v2 (2026-10-14), stage D: hooks, preflight, Workflow parity, process trees ----------

def t_after_hook_harness():
    """V2-12 in the harness: an apply that ends AFTER_FAILED (a required `after` hook failed) is a failed deploy for
    `deploy --go` too: exit 2, phase deploy_failed, HARD STOP + HARNESS DEPLOY: AFTER_FAILED, no watch; status says so."""
    c = prod_case("afterhook", base_scenario(), after=["exit 7"])
    code, out = c.start("--flags", "START")
    c.check("run reaches gate_b", code == 0 and c.state()["phase"] == "gate_b", out[-300:])
    c.power("deploy")
    code, out = c.power("deploy", "--go", "--approved", c.deploy_token())
    st = c.state()
    c.check("exit 2, phase deploy_failed", code == 2 and st["phase"] == "deploy_failed", (code, st.get("phase"), out[-400:]))
    c.check("apply status AFTER_FAILED recorded", (st.get("deploy") or {}).get("apply") == "AFTER_FAILED", st.get("deploy"))
    c.check("HARD STOP + HARNESS DEPLOY: AFTER_FAILED lines", "HARD STOP" in out and "HARNESS DEPLOY: AFTER_FAILED" in out, out[-500:])
    c.check("no watch ran after a failed apply", "watch" not in (st.get("deploy") or {}), st.get("deploy"))
    c.check("the files are on prod (hook effects are never undone automatically)", "NEW_A" in open(os.path.join(c.prod, "a.php")).read())
    code, sout = c.power("status")
    c.check("status names the failed deploy and the way on", code == 0 and "AFTER_FAILED" in sout and "deploy_failed" in sout, sout[-500:])
    return c


def t_preflight():
    """V2-16: no agent starts without the executables (POWER_CLAUDE / claude, bash, git): `start` stops before any
    state, agent or edit with a message that names the fix, no traceback. Two planners that leave no plan end in a
    controlled plan_failed (nonzero exit, gate_a.md with the way on, no traceback), and `continue --replan` recovers."""
    c = Case("preflight")
    c.scenario(base_scenario())
    missing = os.path.join(c.dir, "definitely_missing_claude").replace("\\", "/")
    c.env["POWER_CLAUDE"] = missing
    code, out = c.start("--flags", "START")
    c.check("missing claude: nonzero exit, no traceback", code != 0 and "Traceback" not in out, (code, out[-400:]))
    c.check("the message names the executable and POWER_CLAUDE", "definitely_missing_claude" in out and "POWER_CLAUDE" in out, out[-400:])
    c.check("no state, no agent, no edit", not os.path.isfile(os.path.join(c.run, "harness", "state.json")) and c.calls() == [] and "OLD_A" in c.file("a.php"),
            (os.path.isfile(os.path.join(c.run, "harness", "state.json")), c.kinds()))
    # planners that never write PLAN.md + tasks.json (dead agent twice): a fresh run
    c2 = Case("preflight_noplan")
    s = base_scenario()
    s["plan"] = {"dead": True}
    c2.scenario(s)
    code, out = c2.start("--flags", "START")
    st = c2.state() if os.path.isfile(os.path.join(c2.run, "harness", "state.json")) else {}
    c2.check("no plan twice: nonzero exit, no traceback, phase plan_failed", code != 0 and "Traceback" not in out and st.get("phase") == "plan_failed", (code, st.get("phase"), out[-400:]))
    gpath = os.path.join(c2.run, "harness", "gate_a.md")
    gate = open(gpath, encoding="utf-8").read() if os.path.isfile(gpath) else ""
    c2.check("gate_a.md says the planner wrote no plan and names the way on", "PLAN.md" in gate and "--replan" in gate, gate[:500] or "(no gate_a.md)")
    c2.check("the planner ran twice, nothing was edited", c2.kinds().count("plan") == 2 and "OLD_A" in c2.file("a.php"), c2.kinds())
    code, sout = c2.power("status")
    c2.check("status: plan_failed and the next step", code == 0 and "plan_failed" in sout and "replan" in sout, sout[-400:])
    c2.scenario(base_scenario())
    code, out = c2.power("continue", "--replan")
    c2.check("continue --replan with a working planner reaches gate_b", code == 0 and c2.state().get("phase") == "gate_b", (code, out[-300:]))
    c.failed += [f"noplan: {x}" for x in c2.failed]
    if not c2.failed:
        rm_rf(c2.dir)
    return c


def t_workflow_parity():
    """V2-14: the Workflow fallback (power_workflow.js) applies the harness's outcome rules where a script can: a
    reported stray file -> BLOCKED, `scope_changes` and `plan_changes` in the result, a verify-only pass merges into
    the previous outcome (the stray survives unless tasks.json now lists the file), and every Claude fallback of a
    failed Codex job is preceded by `codex_task.py kill`. Runs the real functions in node; no Workflow, no agent."""
    c = Case("workflow", agents=False)
    node = shutil.which("node")
    if not node:
        c.check("node on PATH for the JS parity test", False, "node not found")
        return c
    js = open(os.path.join(SKILL, "power_workflow.js"), encoding="utf-8").read()
    # the Workflow tool runs the script as the body of an async function (top-level await and return): check it so
    tmp = os.path.join(c.dir, "power_workflow.mjs")
    open(tmp, "w", encoding="utf-8", newline="\n").write("(async (args, agent, log) => {\n" + js.replace("export const meta", "const meta", 1) + "\n})")
    p = subprocess.run([node, "--check", tmp], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    c.check("power_workflow.js parses (as the Workflow tool wraps it)", p.returncode == 0, p.stderr[-300:])
    try:
        fn = js[js.index("function outcome("):js.index("// work -> gate ->")]
        if "function verifyFailed(" not in fn:
            c.check("verifyFailed (the exception path of verify-only) is a function next to verifyOutcome", False, "not found")
            return c
        norm = next(ln for ln in js.splitlines() if ln.startswith("const norm ="))
    except (ValueError, StopIteration) as e:
        c.check("outcome / verifyOutcome / norm found in the script", False, repr(e))
        return c
    prog = "const strip = r => r && ({status: r.status, plan_change: r.plan_change});\n" + norm + "\n" + fn + r"""
const t = {id: 't1', files: ['a.php']}
const pass = {by: 'opus', pass: true, problems: [], notes: '', check: {scope_changes: ['z.php'], diff_path: 'd', report_path: 'r'}}
const o1 = outcome(t, 'PASS', {by: 'sonnet', status: 'DONE', plan_change: 'first'}, [pass], '', false, ['rogue.php'], [{kind: 'work', by: 'sonnet', text: 'first'}])
const o2 = outcome(t, 'PASS', {by: 'sonnet', status: 'DONE', plan_change: ''}, [pass], '', false, [], [])
const m1 = verifyOutcome(t, pass, 'sonnet', 2, o1)
const m2 = verifyOutcome({id: 't1', files: ['a.php', 'rogue.php']}, pass, 'sonnet', 3, o1)
const m3 = verifyOutcome(t, pass, 'sonnet', 1, null)
const m4 = verifyOutcome({id: 't1', files: ['a.php', 'late.php']}, pass, 'sonnet', 4, {...m2, stray_files: ['late.php']})
const o3 = outcome(t, 'PASS', {by: 'fable', status: 'DONE', plan_change: ''}, [pass], '', true, [], [], ['opus', 'fable'])
const m5 = verifyOutcome(t, pass, 'sonnet', 2, o3)
const f1 = verifyFailed(t, 'sonnet', 3, new Error('boom'), o3)
const f2 = verifyFailed(t, 'sonnet', 1, 'plain text', null)
console.log(JSON.stringify({o1, o2, m1, m2, m3, m4, o3, m5, f1, f2}))
"""
    p = subprocess.run([node, "-e", prog], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    c.check("the outcome functions run in isolation", p.returncode == 0, p.stderr[-400:])
    r = json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else {}
    o1, o2, m1, m2, m3, m4, o3, m5, f1, f2 = (r.get(k) or {} for k in ("o1", "o2", "m1", "m2", "m3", "m4", "o3", "m5", "f1", "f2"))
    c.check("reported stray -> BLOCKED with the harness's reason, stray kept", o1.get("status") == "BLOCKED" and "outside" in o1.get("reason", "") and o1.get("stray_files") == ["rogue.php"], o1)
    c.check("scope_changes and plan_changes in the result", o1.get("scope_changes") == ["z.php"] and o1.get("plan_changes") == [{"kind": "work", "by": "sonnet", "text": "first"}], o1)
    c.check("a clean PASS stays PASS", o2.get("status") == "PASS" and o2.get("stray_files") == [], o2)
    c.check("verify-only merge keeps the stray and BLOCKED; rounds, verifiers and the result survive",
            m1.get("status") == "BLOCKED" and m1.get("stray_files") == ["rogue.php"] and m1.get("rounds") == 2 and m1.get("verified_by") == ["opus", "opus"]
            and (m1.get("result") or {}).get("plan_change") == "first" and m1.get("verify_only_round") == 2 and m1.get("author") == "sonnet", m1)
    c.check("tasks.json lists the file now -> PASS, stray_resolved, an orchestrator plan change recorded",
            m2.get("status") == "PASS" and m2.get("stray_resolved") == ["rogue.php"] and m2.get("stray_files") == []
            and any(x.get("kind") == "orchestrator" for x in m2.get("plan_changes") or []), m2)
    c.check("verify-only without a previous outcome: a fresh result by the author", m3.get("status") == "PASS" and m3.get("author") == "sonnet" and m3.get("rounds") == 1, m3)
    # V3-07: a later verify-only round keeps the earlier resolutions (history), and resolves the new stray too
    c.check("V3-07: stray_resolved accumulates over verify-only rounds (rogue.php from round 3 survives round 4)",
            m4.get("stray_resolved") == ["rogue.php", "late.php"] and m4.get("stray_files") == [] and m4.get("status") == "PASS" and m4.get("rounds") == 3, m4)
    # V3-02 parity: every model whose edits stay is an author
    c.check("V3-02: outcome carries the accumulated authors (sorted), author = the last one",
            o3.get("authors") == ["fable", "opus"] and o3.get("author") == "fable" and o1.get("authors") == ["sonnet"], (o3.get("authors"), o1.get("authors")))
    c.check("verify-only merge keeps the authors", m5.get("authors") == ["fable", "opus"] and m5.get("author") == "fable", m5)
    # V3-07: the exception path of verify-only keeps the earlier evidence instead of a bare BLOCKED row
    c.check("verifyFailed: BLOCKED with the error, result / author / authors / verified_by / strays / plan changes of the previous outcome kept",
            f1.get("status") == "BLOCKED" and "workflow error: boom" in f1.get("reason", "") and f1.get("author") == "fable" and f1.get("authors") == ["fable", "opus"]
            and f1.get("verified_by") == ["opus"] and (f1.get("result") or {}).get("status") == "DONE" and f1.get("verify_only_round") == 3 and f1.get("escalated") is True, f1)
    c.check("verifyFailed without a previous outcome: BLOCKED by the author, round kept", f2.get("status") == "BLOCKED" and f2.get("author") == "sonnet" and "plain text" in f2.get("reason", "") and f2.get("verify_only_round") == 1, f2)
    c.check("every Claude fallback of a failed Codex job is preceded by codexKill (work, fix, verify)",
            js.count("await codexKill(") >= 3 and "async function codexKill" in js and "KILLED" in js, js.count("await codexKill("))
    ct = open(os.path.join(SKILL, "codex_task.py"), encoding="utf-8").read()
    wa = ct.split("def cmd_workflow_args")[1].split("def cmd_ready")[0] if "def cmd_workflow_args" in ct else ""
    c.check("workflow-args --verify passes the previous outcome from results.json", "previous=" in wa and "results.json" in wa)
    return c


def t_kill_tree():
    """Probe kill_tree / 2.4: codex_task.kill_tree ends the runner AND its child (taskkill /T on Windows, the process
    group elsewhere): a child that would write after its parent's death never does. The Windows creation flags are
    guarded by os.name, the POSIX runner starts its own session so that the group kill reaches it."""
    c = Case("killtree", agents=False)
    sys.path.insert(0, SKILL)
    import codex_task  # noqa: E402
    marker, ready = os.path.join(c.dir, "child_wrote"), os.path.join(c.dir, "child_ready")
    child = "from pathlib import Path; import sys,time; Path(sys.argv[2]).touch(); time.sleep(1.5); Path(sys.argv[1]).write_text('still running')"
    parent_code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2],sys.argv[3]]); time.sleep(30)"
    kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    parent = subprocess.Popen([PY, "-c", parent_code, child, marker, ready], **kw)
    try:
        end = time.time() + 10
        while not os.path.exists(ready) and time.time() < end:
            time.sleep(0.05)
        c.check("the child started", os.path.exists(ready))
        gone = codex_task.kill_tree(parent.pid)
        time.sleep(2.5)
        c.check("kill_tree reports the parent gone", gone is True and parent.poll() is not None, (gone, parent.poll()))
        c.check("the child did not write after the kill (the tree died, not only the parent)", not os.path.exists(marker))
    finally:
        if parent.poll() is None:
            parent.kill()
    c.check("CREATE_NO_WINDOW is 0 off Windows (creationflags are Windows-only)", codex_task.CREATE_NO_WINDOW == (0x08000000 if os.name == "nt" else 0), codex_task.CREATE_NO_WINDOW)
    src = open(os.path.join(SKILL, "codex_task.py"), encoding="utf-8").read()
    spawn = src.split("def spawn_runner")[1].split("\ndef ")[0]
    c.check("spawn_runner: creationflags only on nt, start_new_session elsewhere", 'os.name == "nt"' in spawn and "start_new_session" in spawn, spawn[-400:])
    kt = src.split("def kill_tree")[1].split("\ndef ")[0]
    c.check("kill_tree: POSIX branch kills the process group / children, not the pid alone", "killpg" in kt or "children" in kt, kt[-400:])
    return c



def t_authors():
    """V3-02: every model whose edits stay in the tree is an author of the task (`authors`), not only the last one:
    work + fix + finish accumulate, a restore to the snapshot drops the earlier ones, a final fix adds its fixer.
    The final reviewer is chosen (and its independence judged) by that union - Opus writing the code and Fable
    finishing the task never leaves Opus as an "independent" reviewer of its own code."""
    # (a) Astra's probe: Opus writes, two rejections, Fable (step 1) finishes without a restore, Opus is step 3
    c = Case("authors")
    s = base_scenario()
    s["plan"] = {"files": {"PLAN.md": "# plan\n\nt1", "tasks.json": dict(TASKS, tasks=TASKS["tasks"][:1])}}
    s["work"].pop("t2")
    s["verify"] = {"t1": [{"verdict": FAIL}, {"verdict": FAIL}, {"verdict": PASS}, {"verdict": PASS}]}
    s["fix"] = {"t1": {"result": DONE}}
    s["finish"] = {"t1": {"result": DONE, "edits": [{"file": "a.php", "replace": ["NEW_A", "NEW_A"]}]}}
    c.scenario(s)
    code, out = c.start("--flags", "START", step1="fable", step2="opus", step3="opus")
    r, st = c.results(), c.state()
    rv = st["final"]["review"]
    c.check("(a) run reaches gate_b; t1 PASS, finished by fable, opus's edit retained",
            code == 0 and st["phase"] == "gate_b" and r["t1"]["status"] == "PASS" and r["t1"]["author"] == "fable" and "NEW_A" in c.file("a.php"), (out[-300:], r.get("t1")))
    c.check("(a) authors of t1 = fable AND opus", r["t1"].get("authors") == ["fable", "opus"], r["t1"].get("authors"))
    c.check("(a) the review names both authors and is NOT independent (no third strong model in the run)",
            rv.get("authors") == ["fable", "opus"] and rv.get("independent") is False and rv.get("by") in ("opus", "fable"), rv)
    c.check("(a) hard stop: the review is not independent", "не е независим" in c.read("harness", "gate_b.md"), c.read("harness", "gate_b.md")[-500:])
    code, out = c.power("verify", "--tasks", "t1:fable:4")
    r = c.results()
    c.check("(a) verify-only keeps the accumulated authors", code == 0 and r["t1"]["status"] == "PASS" and r["t1"].get("authors") == ["fable", "opus"] and r["t1"]["author"] == "fable", r["t1"])
    # (b) PLAN_MISMATCH -> restore to the snapshot -> finish: the executor's edit is gone, only the finisher is an author
    c2 = Case("authors2")
    s2 = base_scenario()
    s2["work"]["t1"] = {"result": {"status": "PLAN_MISMATCH", "files_changed": ["a.php"], "mismatch": "anchor missing: a.php:3", "summary": "", "how_verified": "", "open_issues": [], "plan_change": ""},
                        "edits": [{"file": "a.php", "replace": ["OLD_A", "GARBAGE"]}]}
    s2["finish"] = {"t1": {"result": DONE, "edits": EDIT_A}}
    c2.scenario(s2)
    c2.start("--flags", "START")
    r2, rv2 = c2.results(), c2.state()["final"]["review"]
    c2.check("(b) restore drops sonnet: authors of t1 = [opus], author opus", r2["t1"].get("authors") == ["opus"] and r2["t1"]["author"] == "opus" and r2["t1"]["status"] == "PASS", r2["t1"])
    c2.check("(b) t2 by sonnet alone; the review by fable is independent of [opus, sonnet]",
             r2["t2"].get("authors") == ["sonnet"] and rv2["by"] == "fable" and rv2.get("authors") == ["opus", "sonnet"] and rv2.get("independent") is True, rv2)
    # (c) a fix round by the same model adds nothing; the final fixer counts as an author for the next generation
    c3 = Case("authors3")
    s3 = base_scenario()
    s3["verify"] = {"t1": [{"verdict": FAIL}, {"verdict": PASS}], "*": {"verdict": PASS}}
    s3["fix"] = {"t1": {"result": DONE}}
    s3["review"] = MEDIUM_REVIEW
    s3["finalfix"] = FINALFIX_OK
    c3.scenario(s3)
    c3.start("--flags", "START")
    r3, st3 = c3.results(), c3.state()
    c3.check("(c) work + fix by sonnet: authors [sonnet]", r3["t1"].get("authors") == ["sonnet"] and r3["t1"]["rounds"] == 2, r3["t1"])
    sys.path.insert(0, SKILL)
    import power_run  # noqa: E402
    run = power_run.Run(c3.run, c3.proj)
    c3.check("(c) FIXED; the final fixer (opus) is an author of the tree now: Run.authors() = [opus, sonnet]",
             st3["final"]["status"] == "FIXED" and st3["final"]["finalfix"]["by"] == "opus" and run.authors() == ["opus", "sonnet"], (st3["final"].get("status"), run.authors()))
    rep = c3.read("REPORT.md")
    c3.check("(c) REPORT.md lists the authors of the code", "opus" in rep and "sonnet" in rep, rep[-800:])
    for x in (c2, c3):
        c.failed += [f"{x.name}: {f}" for f in x.failed]
        if not x.failed:
            rm_rf(x.dir)
    return c


def t_final_status():
    """V3-05: the hard stops accept only a CLOSED final review (CLEAN / FIXED); every other status - unknown, RUNNING,
    INCOMPLETE, missing - is a stop whatever the older fields of the block say, and `deploy --go` refuses."""
    c = Case("finalstatus")
    s = base_scenario()
    s["review"] = DEPLOY_REVIEW
    c.scenario(s)
    c.start("--flags", "START")
    code, out = c.power("deploy")
    c.check("CLEAN: no hard stop", code == 0 and "HARD STOP" not in out, out[-300:])
    sp = os.path.join(c.run, "harness", "state.json")
    for bad in ("RUNNING", "DONE", "", None):
        st = c.state()
        if bad is None:
            st["final"].pop("status", None)
        else:
            st["final"]["status"] = bad
        json.dump(st, open(sp, "w", encoding="utf-8"))
        code, out = c.power("deploy")
        c.check(f"final status {bad!r}: a hard stop names the unclosed review", "HARD STOP:" in out and "финалният преглед не е затворен" in out, out[-400:])
        code, out = c.power("deploy", "--go", "--approved", c.deploy_token() or "x")
        c.check(f"final status {bad!r}: deploy --go refused", code != 0 and "refused" in out, out[-300:])
    st = c.state()
    st["final"]["status"] = "FIXED"
    json.dump(st, open(sp, "w", encoding="utf-8"))
    code, out = c.power("deploy")
    c.check("FIXED: no stop about the review", "финалният преглед не е затворен" not in out, out[-300:])
    return c


def t_config_broken():
    """V3-06: an invalid `## POWER` json block is an INFRA stop, never an empty config: `start` refuses in preflight
    (no state, no agent), `check` ends FAIL with an INFRA failure (per task and --all alike, the smoke suite is not
    silently dropped), the harness BLOCKs the task as infrastructure without starting a verifier."""
    bad_md = CLAUDE_MD.replace("```json\n{", "```json\n{,", 1)
    c = Case("configbroken", claude_md=bad_md)
    c.scenario(base_scenario())
    code, out = c.start("--flags", "START")
    c.check("start: PREFLIGHT FAILED names the invalid ## POWER block; no state, no agent",
            code != 0 and "PREFLIGHT FAILED" in out and "invalid json" in out and not os.path.exists(os.path.join(c.run, "harness", "state.json")) and not c.calls(), out[-500:])
    # a run that reached gate_b, then CLAUDE.md breaks (an edit by another session)
    c2 = Case("configbroken2")
    c2.scenario(base_scenario())
    c2.start("--flags", "START")
    open(os.path.join(c2.proj, "CLAUDE.md"), "w", encoding="utf-8", newline="\n").write(bad_md)
    for argv in (["--all", "--round", "final9"], ["--task", "t1", "--round", "9"]):
        p = subprocess.run([PY, os.path.join(SKILL, "power_check.py"), "check", *argv, "--run-dir", c2.run, "--project-dir", c2.proj],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=dict(os.environ, PYTHONUTF8="1"), creationflags=CREATE_NO_WINDOW)
        line = next((x for x in p.stdout.splitlines() if x.startswith("CHECK_JSON: ")), "")
        obj = json.loads(line[len("CHECK_JSON: "):]) if line else {}
        c2.check(f"check {argv[0]}: exit 1, pass False, an INFRA failure naming the json",
                 p.returncode != 0 and obj.get("pass") is False and any("INFRA" in f and "invalid json" in f for f in obj.get("failures") or []), (p.returncode, obj, (p.stdout + p.stderr)[-300:]))
    n = len(c2.calls())
    code, out = c2.power("verify", "--tasks", "t1:sonnet:2")
    r = c2.results()["t1"]
    c2.check("harness verify-only: t1 BLOCKED as infrastructure, author kept, no verifier started",
             r["status"] == "BLOCKED" and "infrastructure" in r["reason"] and "invalid json" in r["reason"] and r["author"] == "sonnet" and len(c2.calls()) == n, (r, len(c2.calls()) - n))
    c.failed += [f"{c2.name}: {f}" for f in c2.failed]
    if not c2.failed:
        rm_rf(c2.dir)
    return c


def t_readiness():
    """V3 readiness: with a Codex model in the menu, `start` checks in preflight that the codex executable resolves
    (CODEX_EXE / the desktop app / PATH; a CODEX_EXE that points nowhere is an error, not a fallback) and that Codex is
    logged in (auth.json under CODEX_HOME) - no paid probe, no state, no agent. A Claude-only run needs no codex.
    `codex_task.py ready` (the Workflow path) makes the same check."""
    c = Case("readiness")
    c.scenario(base_scenario())
    missing = os.path.join(c.dir, "no_such_codex.exe")
    c.env["CODEX_EXE"] = missing
    code, out = c.start("--flags", "START", step1="astra")
    c.check("codex executable missing: PREFLIGHT FAILED, no state, no agent",
            code != 0 and "PREFLIGHT FAILED" in out and "codex" in out.lower() and not os.path.exists(os.path.join(c.run, "harness", "state.json")) and not c.calls(), out[-500:])
    code, out = c.start("--flags", "START")
    c.check("Claude-only models: codex is not needed, the run reaches gate_b", code == 0 and c.state()["phase"] == "gate_b", out[-300:])
    # the Workflow path: `codex_task.py ready` on a fresh run (tasks.json written, nothing run yet)
    c3 = Case("readiness3", agents=True)
    json.dump(TASKS, open(os.path.join(c3.run, "tasks.json"), "w", encoding="utf-8"), indent=1)

    def ready(step1, step2, **env):
        e = dict(os.environ, PYTHONUTF8="1", CODEX_HOME=c3.codex_home, CODEX_EXE=PY)
        e.update(env)
        p = subprocess.run([PY, os.path.join(SKILL, "codex_task.py"), "ready", "--run-dir", c3.run, "--project-dir", c3.proj, "--step1", step1, "--step2", step2],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=e, creationflags=CREATE_NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    code, out = ready("astra", "sonnet", CODEX_EXE=missing)
    c3.check("codex_task.py ready with a Codex step and no codex: exit 1, READY: FAIL naming codex, nothing prepared",
             code != 0 and "READY: FAIL" in out and "codex" in out.lower() and not os.path.isdir(os.path.join(c3.run, "before")), (code, out[-400:]))
    os.remove(os.path.join(c3.codex_home, "auth.json"))
    code, out = ready("opus", "sol")
    c3.check("codex_task.py ready: codex present, not logged in = READY: FAIL naming auth.json", code != 0 and "READY: FAIL" in out and "auth.json" in out, (code, out[-400:]))
    code, out = ready("opus", "sonnet", CODEX_EXE=missing)
    c3.check("codex_task.py ready with Claude-only steps needs no codex: exit 0, run prepared", code == 0 and os.path.isdir(os.path.join(c3.run, "before")) and '"mode": "work"' in out, (code, out[-400:]))
    c2 = Case("readiness2")
    c2.scenario(base_scenario())
    os.remove(os.path.join(c2.codex_home, "auth.json"))
    code, out = c2.start("--flags", "START", step2="sol")
    c2.check("codex present, not logged in (no auth.json): PREFLIGHT FAILED naming the login, no state",
             code != 0 and "PREFLIGHT FAILED" in out and "auth.json" in out and not os.path.exists(os.path.join(c2.run, "harness", "state.json")), out[-500:])
    for x in (c2, c3):
        c.failed += [f"{x.name}: {f}" for f in x.failed]
        if not x.failed:
            rm_rf(x.dir)
    return c


def t_perf_short():
    """PERF-02: a failed `check --all final` ends the review right there - no lenses, no reviewer; status FAIL with the
    failure as the reason and `short_circuit: true`; `continue --diagnose` runs the full review on the failing tree as
    a new generation. PERF-04: every Claude agent gets its instructions in the message itself (the prompt file stays
    as the audit copy), so no agent spends a tool round on `Read <prompt file>`."""
    md = CLAUDE_MD.replace('{"lenses":', '{"smoke": ["grep -q NEW_A a.php", "test -f no_such_file_here"], "lenses":')
    c = Case("perfshort", claude_md=md)
    s = base_scenario()
    s["review"] = DEPLOY_REVIEW
    c.scenario(s)
    code, out = c.start("--flags", "START")
    st = c.state()
    f = st["final"]
    c.check("final check FAIL (smoke)", f.get("check_final") and f["check_final"]["pass"] is False, f.get("check_final"))
    c.check("short circuit: FAIL, the failure is the reason, generation 1", f["status"] == "FAIL" and f.get("short_circuit") is True
            and str(f.get("reason", "")).startswith("check --all final:") and f.get("generation") == 1, f)
    kinds = c.kinds()
    c.check("no lens and no reviewer agent ran", not any(k.startswith("lens:") or k == "review" for k in kinds), kinds)
    hc = c.harness_calls()
    c.check("calls.jsonl has no lens / review rows", not any(x["role"] in ("lens", "review") for x in hc), [x["role"] for x in hc])
    c.check("review block SKIPPED, no lenses, deploy plan not evaluated", f.get("review", {}).get("verdict") == "SKIPPED" and f.get("lenses") == {}
            and f.get("deploy_plan", {}).get("ok") is False, (f.get("review"), f.get("lenses"), f.get("deploy_plan")))
    gb = c.read("harness", "gate_b.md")
    c.check("gate_b: failed final check + unclosed review are hard stops, `continue --diagnose` named",
            "паднала финална проверка" in gb and "не е затворен" in gb and "--diagnose" in gb, gb)
    code2, out2 = c.power("deploy", "--go")
    c.check("deploy --go refused", code2 != 0, out2[-400:])
    calls = c.calls()
    c.check("every agent got its instructions inline (no Read-the-file round)", calls and all(x.get("inline") for x in calls),
            [(x["kind"], x["name"], x.get("inline")) for x in calls])
    c.check("the inline message carries the prompt text (longer than a one-line pointer)", calls and min(x.get("prompt_chars", 0) for x in calls) > 600,
            [(x["kind"], x.get("prompt_chars")) for x in calls])
    c.check("prompt files kept as the audit copy", os.path.isfile(os.path.join(c.run, "prompts", "t1.work.md")) and os.path.isfile(os.path.join(c.run, "prompts", "plan.md")))
    # the full review on demand: lenses + reviewer on the failing tree, a new generation
    code3, out3 = c.power("continue", "--diagnose")
    st = c.state()
    f = st["final"]
    kinds = c.kinds()
    c.check("continue --diagnose: generation 2 ran the lenses and the reviewer", f.get("generation") == 2 and any(k.startswith("lens:") for k in kinds)
            and "review" in kinds and not f.get("short_circuit"), (f.get("generation"), f.get("short_circuit"), kinds))
    c.check("still FAIL (V2-04: the check fails) with the reviewer's verdict recorded", f["status"] == "FAIL" and f["review"]["verdict"] in ("PASS", "PASS_WITH_FIXES")
            and str(f.get("reason", "")).startswith("check --all final"), f)
    c.check("generation 1 archived with short_circuit", st.get("final_history") and st["final_history"][-1].get("short_circuit") is True, st.get("final_history"))
    c.check("phase gate_b again, exit 0", code3 == 0 and st["phase"] == "gate_b", (code3, out3[-300:]))
    code4, out4 = c.power("continue")
    c.check("a plain `continue` from gate_b only prints the gate (no third generation)", code4 == 0 and c.state()["final"]["generation"] == 2, (code4, out4[-200:]))
    return c


def t_quota():
    """PERF-03: `codex_task.py usage` is two real `codex exec` per run; the harness runs it only on request
    (`--refresh-quota` on start or continue). Without the flag (test codex) no usage call is made and the report says so."""
    c = Case("quota")
    s = base_scenario()
    s.pop("plan")
    c.scenario(s)
    c.codex_scenario({"plan": PLAN})
    code, out = c.start("--flags", "START", "--refresh-quota", step1="astra", step2="sonnet", step3="opus")
    st = c.state() if os.path.isfile(os.path.join(c.run, "harness", "state.json")) else {}
    ck = [x["kind"] for x in c.codex_calls()]
    c.check("run reaches gate_b", code == 0 and st["phase"] == "gate_b", (out[-400:], st.get("phase")))
    c.check("usage fetched twice (before the work, after the review)", ck.count("usage") == 2 and st.get("codex_usage_before") and st.get("codex_usage_after"),
            (ck, st.get("codex_usage_before")))
    rep = c.read("REPORT.md")
    c.check("REPORT shows both snapshots", "usage before" in rep and "usage after" in rep, rep[-1200:])
    return c


def t_polling():
    """PERF-09: codex_task.run_job delivers a finished job right away (a 0.25 s poll that backs off to 2 s), not after
    a fixed 5 s sleep; a job that finishes later is seen within one backoff step. Pure Python, no Codex binary."""
    import importlib.util
    import threading
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location("codex_task_polling", os.path.join(SKILL, "codex_task.py"))
    codex = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(codex)
    c = Case("polling")
    folder = os.path.join(c.dir, "jobs")

    def go(label, delay):
        delivered = []

        def fake_start(a, job, kind, prompt):
            p = codex.job_paths(a.run_dir, job)["done.json"]

            def finish():
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "w", encoding="utf-8").write("{}")
            if delay:
                threading.Timer(delay, finish).start()
            else:
                finish()
            return True
        codex.try_start, codex.report = fake_start, lambda *a: delivered.append(True)
        a = SimpleNamespace(run_dir=os.path.join(folder, label), max_wait=20)
        t0 = time.monotonic()
        codex.run_job(a, "work", "job", lambda: "prompt")
        return bool(delivered), round(time.monotonic() - t0, 2)
    ok, sec = go("now", 0)
    c.check("immediate completion delivered under 1.5 s", ok and sec < 1.5, sec)
    ok, sec = go("later", 1.2)
    c.check("completion after 1.2 s delivered under 3.5 s (backoff capped at 2 s)", ok and 1.1 < sec < 3.5, sec)
    return c


def t_fast():
    """FAST (15.10): `--flags START,FAST` or `--fast` = state["fast"]; recon and lenses run on haiku, work / fix / area
    agents get `--effort medium`, a config lens with `"fast": false` is skipped; plan, verifier and final review keep
    their model and no effort flag; the echo line says FAST. A run without the flag keeps the old knobs (recon on
    sonnet, no effort for work, `fast` false in the state) - the other scenarios never see FAST."""
    md = CLAUDE_MD.replace('{"name": "smoke", "focus": "the pages"}',
                           '{"name": "smoke", "focus": "the pages"}, {"name": "i18n", "focus": "the languages", "fast": false}')
    c = Case("fast", claude_md=md)
    s = base_scenario()
    s["work"]["t1"] = {"result": DONE, "edits": [{"file": "a.php", "replace": ["OLD_A", "NEW_A_TYPO"]}]}
    s["verify"] = {"t1": [{"verdict": FAIL}, {"verdict": PASS}], "*": {"verdict": PASS}}
    s["fix"] = {"t1": {"result": DONE, "edits": [{"file": "a.php", "replace": ["NEW_A_TYPO", "NEW_A"]}]}}
    c.scenario(s)
    code, out = c.start("--flags", "START,FAST")
    st, r = c.state(), c.results()
    c.check("start exits 0", code == 0, out[-600:])
    c.check("state: fast true, START, no DEPLOY", st.get("fast") is True and st["flags"] == {"start": True, "deploy": False}, (st.get("fast"), st.get("flags")))
    by = {f"{cc['kind']}:{cc['name']}" if cc["name"] else cc["kind"]: cc for cc in c.calls()}
    c.check("recon on haiku, effort low as before", by["recon:scope"]["model"] == "haiku" and by["recon:scope"]["effort"] == "low", by.get("recon:scope"))
    c.check("lenses on haiku; i18n (fast: false) skipped", by["lens:domain"]["model"] == "haiku" and by["lens:smoke"]["model"] == "haiku" and "lens:i18n" not in by, sorted(by))
    c.check("work and fix on sonnet with --effort medium", by["work:t1"]["model"] == "sonnet" and by["work:t1"]["effort"] == "medium" and by["fix:t1"]["model"] == "sonnet" and by["fix:t1"]["effort"] == "medium", (by.get("work:t1"), by.get("fix:t1")))
    c.check("plan, verify and review keep opus and no effort flag", by["plan"]["model"] == "opus" and by["plan"]["effort"] in ("", None) and by["verify:t1"]["model"] == "opus" and by["verify:t1"]["effort"] in ("", None) and by["review"]["model"] == "opus" and by["review"]["effort"] in ("", None), (by.get("plan"), by.get("verify:t1"), by.get("review")))
    c.check("t1 PASS after one fix, final CLEAN, phase gate_b", r["t1"]["status"] == "PASS" and r["t1"]["rounds"] == 2 and st["final"]["status"] == "CLEAN" and st["phase"] == "gate_b", (r.get("t1"), st.get("final", {}).get("status"), st.get("phase")))
    log = c.read("harness", "log.txt")
    c.check("echo line says FAST and the knobs", "POWER START FAST ·" in log and "FAST: изпълнение effort medium, recon/лещи Haiku, паралел 4" in log, log[:500])
    gb = c.read("harness", "gate_b.md")
    c.check("gate_b.md carries the FAST echo line", "POWER START FAST" in gb, gb[:300])
    # --fast alone (no START): the same profile, stops at Gate A; a plain start keeps the old knobs
    c2 = Case("fast_flag", claude_md=md)
    c2.scenario(base_scenario())
    code, out = c2.start("--fast")
    st2, by2 = c2.state(), {f"{cc['kind']}:{cc['name']}" if cc["name"] else cc["kind"]: cc for cc in c2.calls()}
    c.check("--fast: state fast, gate_a, recon on haiku, plan on opus", code == 0 and st2.get("fast") is True and st2["phase"] == "gate_a" and by2["recon:scope"]["model"] == "haiku" and by2["plan"]["model"] == "opus", (code, st2.get("fast"), st2.get("phase"), by2))
    c.check("--fast echo line: POWER FAST (no START)", "POWER FAST ·" in c2.read("harness", "log.txt"), c2.read("harness", "log.txt")[:300])
    c3 = Case("fast_off", claude_md=md)
    c3.scenario(base_scenario())
    code, out = c3.start("--flags", "START")
    st3, by3 = c3.state(), {f"{cc['kind']}:{cc['name']}" if cc["name"] else cc["kind"]: cc for cc in c3.calls()}
    c.check("without FAST: fast false, recon on sonnet, work without effort, i18n lens runs", st3.get("fast") is False and by3["recon:scope"]["model"] == "sonnet" and by3["work:t1"]["effort"] in ("", None) and "lens:i18n" in by3 and by3["lens:i18n"]["model"] == "sonnet", (st3.get("fast"), by3.get("recon:scope"), by3.get("work:t1"), sorted(by3)))
    c.check("without FAST: no FAST in the echo line", "FAST" not in c3.read("harness", "log.txt").split("\n")[0], c3.read("harness", "log.txt")[:300])
    code, out = c3.power("start", "--request-file", os.path.join(c3.dir, "request.md"), "--step1", "opus", "--step2", "sonnet", "--step3", "opus", "--flags", "START,TURBO", "--force")
    c.check("unknown flag TURBO is refused and names FAST", code != 0 and "TURBO" in out and "START, DEPLOY, FAST" in out, out[-300:])
    if not c.failed:
        rm_rf(c2.dir)
        rm_rf(c3.dir)
    return c


def t_power_json():
    """The POWER config may live in `.claude/power.json` instead of the `## POWER` block (15.10): the same keys, the
    same merge, `config` names the file as the source and a full START run uses its lenses; both in one directory is
    PREFLIGHT FAILED (two sources would drift), an invalid file is the same stop as an invalid block."""
    md = "# test project\n\nno POWER section here\n"
    block = json.loads(CLAUDE_MD.split("```json\n", 1)[1].split("\n```", 1)[0])
    block["lenses"] = [{"name": "domain", "focus": "the traps"}, {"name": "extra", "focus": "the json file"}]

    def write_json(case, text):
        jp = os.path.join(case.proj, ".claude", "power.json")
        os.makedirs(os.path.dirname(jp), exist_ok=True)
        open(jp, "w", encoding="utf-8", newline="\n").write(text)
        for cmd in (["git", "add", "."], ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "config"]):
            subprocess.run(cmd, cwd=case.proj, capture_output=True, creationflags=CREATE_NO_WINDOW)

    def config(case):
        p = subprocess.run([PY, os.path.join(SKILL, "power_check.py"), "config", "--project-dir", case.proj], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=dict(os.environ, PYTHONUTF8="1"), creationflags=CREATE_NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    c = Case("powerjson", claude_md=md)
    write_json(c, json.dumps(block, ensure_ascii=False, indent=1))
    code, out = config(c)
    c.check("config: power.json is the source and its lenses are listed", code == 0 and "power.json" in out and "extra" in out and "CLAUDE.md" not in out, out[-400:])
    c.scenario(base_scenario())
    code, out = c.start("--flags", "START")
    st = c.state()
    c.check("start with power.json only: gate_b, the extra lens ran, no smoke lens (not in the file)",
            code == 0 and st["phase"] == "gate_b" and "lens:extra" in c.kinds() and "lens:smoke" not in c.kinds(), (code, st.get("phase"), c.kinds(), out[-300:]))
    c2 = Case("powerjson_both")          # the CLAUDE.md block of the fixture + a power.json
    write_json(c2, json.dumps(block))
    code, out = config(c2)
    c.check("both sources in one directory: config refuses and says so", code != 0 and "two POWER configs" in out, out[-400:])
    c2.scenario(base_scenario())
    code, out = c2.start("--flags", "START")
    c.check("both sources: PREFLIGHT FAILED, no state, no agent",
            code != 0 and "PREFLIGHT FAILED" in out and "two POWER configs" in out and not os.path.exists(os.path.join(c2.run, "harness", "state.json")) and not c2.calls(), out[-500:])
    c3 = Case("powerjson_bad", claude_md=md)
    write_json(c3, "{,")
    c3.scenario(base_scenario())
    code, out = c3.start("--flags", "START")
    c.check("invalid power.json: PREFLIGHT FAILED names invalid json in the file, no agent",
            code != 0 and "PREFLIGHT FAILED" in out and "invalid json" in out and "power.json" in out and not c3.calls(), out[-500:])
    if not c.failed:
        rm_rf(c2.dir)
        rm_rf(c3.dir)
    return c


def t_ultraguard():
    """A (17.10): a POWER run owns the turn - `start` probes the CLI once (`claude --help`, kept in the state) and every
    agent gets `--disallowedTools Workflow,Agent`; a CLI without the flag (MOCK_OLD_CLI=1) gets no flag and the run
    works as before; a state from before the probe is probed once by `continue`."""
    c = Case("ultraguard")
    c.scenario(base_scenario())
    code, out = c.start("--flags", "START")
    st, calls = c.state(), c.calls()
    c.check("start: the probe result is in the state, gate_b", code == 0 and st.get("cli_disallowed_tools") is True and st["phase"] == "gate_b",
            (code, st.get("cli_disallowed_tools"), st.get("phase"), out[-300:]))
    c.check("every agent got --disallowedTools Workflow,Agent", calls and all(cc.get("disallowed") == "Workflow,Agent" for cc in calls),
            [(cc["kind"], cc.get("disallowed")) for cc in calls])
    c.check("the log names the guard", "agents run with --disallowedTools Workflow,Agent" in c.read("harness", "log.txt"))
    c2 = Case("ultraguard_old")
    c2.env["MOCK_OLD_CLI"] = "1"
    c2.scenario(base_scenario())
    code, out = c2.start("--flags", "START")
    st2 = c2.state()
    c.check("old CLI: probe false, no agent got the flag, the run reaches gate_b as before",
            code == 0 and st2.get("cli_disallowed_tools") is False and c2.calls() and all(not cc.get("disallowed") for cc in c2.calls())
            and st2["phase"] == "gate_b", (code, st2.get("cli_disallowed_tools"), st2.get("phase"), out[-300:]))
    c.check("old CLI: the log says the block is off", "the claude CLI has no --disallowedTools" in c2.read("harness", "log.txt"))
    c3 = Case("ultraguard_resume")
    c3.scenario(base_scenario())
    code, out = c3.start()
    p = os.path.join(c3.run, "harness", "state.json")
    st3 = json.load(open(p, encoding="utf-8"))
    st3.pop("cli_disallowed_tools", None)   # a state written before 17.10
    json.dump(st3, open(p, "w", encoding="utf-8"))
    code, out = c3.power("continue")
    st3 = c3.state()
    work = [cc for cc in c3.calls() if cc["kind"] in ("work", "verify", "lens", "review")]
    c.check("older state: continue probes once and saves it; the work and review agents get the flag",
            code == 0 and st3.get("cli_disallowed_tools") is True and work and all(cc.get("disallowed") == "Workflow,Agent" for cc in work),
            (code, st3.get("cli_disallowed_tools"), [(cc["kind"], cc.get("disallowed")) for cc in work][:4]))
    if not c.failed:
        rm_rf(c2.dir)
        rm_rf(c3.dir)
    return c


def t_coverage():
    """B (17.10): Gate B lists what the run did NOT check (final.coverage): the verifiers' `unchecked`, a lens dead
    twice (one retry, then c1), the agents a cap stopped, run files left out of deploy.json, a lens `Skipped:` line, a
    recon `Unmapped:` line - at most 10 lines, the rest in REPORT.md. The deploy-relevant gaps are harness-owned
    questions c1-c3 (deploy.json is never edited): a hard stop until `deploy --yes cN --approved <token>`; a changed
    gap is another question (the old yes is stale); --force does not cover them."""
    c = Case("coverage")
    s = base_scenario()
    s["recon"] = {"text": "# map\n\n- a.php:2 a_init\n\nUnmapped: the cron hooks in b.php"}
    s["verify"] = {"t1": {"verdict": dict(PASS, unchecked=["a.php: the mobile header was not rendered"])},
                   "t2": {"verdict": dict(PASS, unchecked=["case one", "case two", "case three", "case four"])}}
    s["lens"] = {"domain": {"error": "error_max_turns"}, "smoke": {"text": "No findings.\n\n**Skipped:** the mobile layout of b.php"}}
    s["review"] = {"files": {"REVIEW.md": "clean", "DEPLOY.md": "a.php only", "review.json": {"verdict": "PASS", "findings": [], "dismissed": []},
                             "deploy.json": {"profile": "shop", "files": [{"local": "a.php", "order": 1, "note": ""}]}}}
    c.scenario(s)
    code, out = c.start("--flags", "START")
    st = c.state()
    cov = (st.get("final") or {}).get("coverage") or {}
    c.check("START to gate_b, final CLEAN", code == 0 and st["phase"] == "gate_b" and st["final"]["status"] == "CLEAN", (code, st.get("phase"), out[-400:]))
    c.check("the dead lens got exactly one retry", c.kinds().count("lens:domain") == 2 and c.kinds().count("lens:smoke") == 1, c.kinds())
    c.check("coverage block: dead lens, not deployed, unchecked per PASS task, caps, skips, unmapped",
            cov.get("lenses_dead") == ["domain"] and cov.get("not_deployed") == ["b.php"]
            and cov.get("unchecked_by_task", {}).get("t1") == ["a.php: the mobile header was not rendered"] and len(cov.get("unchecked_by_task", {}).get("t2") or []) == 4
            and cov.get("cap_hits") == ["lens:domain (error_max_turns)", "lens:domain#2 (error_max_turns)"]
            and cov.get("lens_skips") == ["smoke: the mobile layout of b.php"] and cov.get("recon_unmapped") == ["scope: the cron hooks in b.php"], cov)
    gb = c.read("harness", "gate_b.md")
    c.check("gate_b: c1, c2, c3 are hard stops", all(f"неотговорен въпрос {q} (покритие, harness)" in gb for q in ("c1", "c2", "c3")), gb)
    c.check("gate_b: c3 names t1 and the a.php item, c2 names b.php", "t1: a.php: the mobile header" in gb and "deploy.json НЕ качва: b.php" in gb, gb)
    c.check("gate_b: НЕПРОВЕРЕНО section, 10 lines + (+1 още в REPORT.md)", "НЕПРОВЕРЕНО / ПРОПУСНАТО:" in gb and "(+1 още в REPORT.md)" in gb
            and "recon, неразучено" not in gb and "Отхвърлени находки на лещите: 0" in gb, gb)
    rep = c.read("REPORT.md")
    c.check("REPORT.md: every gap line, the recon one too", "## Непроверено / пропуснато" in rep and "recon, неразучено: scope: the cron hooks in b.php" in rep
            and "леща, пропуснала: smoke: the mobile layout of b.php" in rep and "агент, спрян от таван: lens:domain#2 (error_max_turns)" in rep, rep[-1500:])
    code, out = c.power("deploy")
    tok = c.deploy_token()
    c.check("deploy dry: the three coverage questions are hard stops", code == 0 and all(f"HARD STOP: неотговорен въпрос {q}" in out for q in ("c1", "c2", "c3")), out[-900:])
    code, out = c.power("deploy", "--go", "--force", "--approved", tok)
    c.check("--go --force does not cover them (refusal names c1, c2, c3)", code != 0 and "c1, c2, c3" in out and "--force does not cover" in out, out[-400:])
    code, out = c.power("deploy", "--yes", "c9", "--approved", tok)
    c.check("--yes c9 (no such question) is refused", code != 0 and "no open coverage question c9" in out, out[-300:])
    code, out = c.power("deploy", "--yes", "C2", "--approved", tok)
    ans = c.state().get("deploy_answers") or {}
    c.check("--yes C2 records c2 with the candidate and the text; c1, c3 stay",
            code == 0 and ans.get("c2", {}).get("candidate") == tok and "b.php" in ans.get("c2", {}).get("text", "")
            and "неотговорен въпрос c2" not in out and "неотговорен въпрос c1" in out and "неотговорен въпрос c3" in out, (out[-700:], ans))
    # the gap changes (another run file left out): the recorded yes is for another text
    p = os.path.join(c.run, "harness", "state.json")
    stj = json.load(open(p, encoding="utf-8"))
    stj["final"]["coverage"]["not_deployed"] = ["b.php", "c.php"]
    json.dump(stj, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    code, out = c.power("deploy")
    c.check("a changed gap: c2 is open again and says the yes was for another text", "HARD STOP: неотговорен въпрос c2" in out and "за друг текст" in out, out[-900:])
    return c


def t_request_coverage():
    """C/D (17.10): the final reviewer reads request.md and the plan's non-goals and writes coverage[] and dismissed[]
    into review.json; a `missing` request item is a medium finding - the final fix path runs even without findings
    and the fixer's prompt lists it; Gate B prints the missing and partial items and the dismissed count, REPORT.md
    every coverage row and every dismissed finding; a review.json without `dismissed` is a warning, not silence. The
    lens and review prompts carry the severity rubric, the lens prompt the `Skipped:` line."""
    c = Case("requestcov")
    s = base_scenario()
    cov = [{"request_item": "flip a.php", "status": "done", "where": "a.php:3"},
           {"request_item": "flip b.php", "status": "partial", "where": "b.php:3"},
           {"request_item": "log the change", "status": "missing", "where": "no log call"},
           {"request_item": "touch c.php", "status": "out_of_scope_by_plan", "where": "t1 non-goal"}]
    s["review"] = {"files": {"REVIEW.md": "ok", "DEPLOY.md": "manual", "review.json": {
        "verdict": "PASS_WITH_FIXES", "findings": [], "coverage": cov,
        "dismissed": [{"lens": "domain", "file": "a.php", "line": 3, "severity": "high", "reason": "false positive", "evidence": "a.php:3 returns NEW_A"}]}}}
    s["finalfix"] = {"result": dict(DONE, summary="log added"), "edits": [{"file": "a.php", "replace": ["NEW_A", "NEW_A /* logged */"]}]}
    c.scenario(s)
    code, out = c.start("--flags", "START")
    st = c.state()
    c.check("a missing request item takes the final fix path: finalfix + finalverify, FIXED",
            code == 0 and "finalfix" in c.kinds() and "finalverify" in c.kinds() and st["final"]["status"] == "FIXED", (code, c.kinds(), st["final"].get("status"), out[-400:]))
    c.check("the fixer's prompt lists the missing item as medium", "[medium] request item missing: log the change" in c.read("prompts", "finalfix.md"), c.read("prompts", "finalfix.md")[-600:])
    c.check("review prompt: request.md, non-goals, rubric, coverage and dismissed in the output shape",
            all(x in c.read("prompts", "review.md") for x in ("request.md", "Решения на #THE_OPERATOR#", "non-goals", "Severity rubric", '"coverage"', '"dismissed"')))
    c.check("lens prompt: rubric and the Skipped line", "Severity rubric" in c.read("prompts", "lens.domain.md") and "Skipped: none" in c.read("prompts", "lens.domain.md"))
    rv = st["final"]["review"]
    c.check("final.review keeps coverage and dismissed", len(rv.get("coverage") or []) == 4 and len(rv.get("dismissed") or []) == 1 and rv.get("dismissed_missing") is False, rv)
    gb = c.read("harness", "gate_b.md")
    c.check("gate_b: missing (sent to the fix, verified) and partial items, no done/out_of_scope rows",
            "заявката, ЛИПСВА: log the change (no log call) - подадена на финалната поправка (проверена: PASS)" in gb and "заявката, частично: flip b.php" in gb
            and "flip a.php" not in gb and "touch c.php" not in gb, gb)
    c.check("gate_b: the dismissed count", "Отхвърлени находки на лещите: 1 (REPORT.md)" in gb, gb)
    rep = c.read("REPORT.md")
    c.check("REPORT.md: every coverage row and the dismissed finding with its evidence",
            "## Покритие на заявката" in rep and "- [out_of_scope_by_plan] touch c.php - t1 non-goal" in rep and "- [done] flip a.php - a.php:3" in rep
            and "## Отхвърлени находки на лещите" in rep and "- [high] domain a.php:3 - false positive (доказателство: a.php:3 returns NEW_A)" in rep, rep[-1500:])
    c2 = Case("requestcov_nodismissed")
    s2 = base_scenario()
    s2["review"] = {"files": {"REVIEW.md": "ok", "DEPLOY.md": "manual", "review.json": {"verdict": "PASS", "findings": [], "coverage": cov[:2]}}}
    c2.scenario(s2)
    code, out = c2.start("--flags", "START")
    st2 = c2.state()
    gb2 = c2.read("harness", "gate_b.md")
    c.check("no `dismissed` key: CLEAN (partial is not a fix), the warning in gate_b, REPORT.md and the log",
            code == 0 and st2["final"]["status"] == "CLEAN" and "finalfix" not in c2.kinds() and "ВНИМАНИЕ: review.json няма списък `dismissed`" in gb2
            and "ВНИМАНИЕ: review.json няма списък `dismissed`" in c2.read("REPORT.md") and "no `dismissed` list" in c2.read("harness", "log.txt"),
            (code, st2["final"].get("status"), c2.kinds(), gb2))
    if not c.failed:
        rm_rf(c2.dir)
    return c


def t_forecast():
    """E (17.10): Gate A shows an indicative cost from the earlier runs' calls.jsonl (sibling run folders, this run left
    out): median (range p25-p75) per role and model times the expected calls, plus what recon and plan already spent; a
    role with no data for the model uses the role's median over all models (noted). Fewer than 3 earlier runs with
    priced calls: no line."""
    def history(case, names, priced=True):
        for i, name in enumerate(names):
            h = os.path.join(case.proj, "docs", "power", name, "harness")
            os.makedirs(h, exist_ok=True)
            rows = [("work", "sonnet", i + 1.0), ("work", "sonnet", i + 1.0), ("verify", "opus", 2.0), ("verify", "opus", 2.0),
                    ("fix", "sonnet", 0.5), ("lens", "sonnet", 0.25), ("lens", "sonnet", 0.25), ("review", "opus", 3.0),
                    ("finalfix", "opus", 1.0), ("finalverify", "opus", 1.0)]
            with open(os.path.join(h, "calls.jsonl"), "w", encoding="utf-8") as f:
                for role, model, usd in rows:
                    f.write(json.dumps({"label": role, "role": role, "model": model, "cost_usd": usd if priced else 0, "ok": True}) + "\n")

    c = Case("forecast")
    history(c, ["2026-10-01-a", "2026-10-02-b", "2026-10-03-c"])
    c.scenario(base_scenario())
    code, out = c.start()
    ga = c.read("harness", "gate_a.md")
    # work 2 x median 2 (p25 1.25, p75 2.75), fix 1 x 0.5, verify 2 x 2, lens 2 x 0.25, review 3, finalfix 1,
    # finalverify (fable: no data) 1 x the role median 1; + 0.10 spent on recon and plan
    c.check("gate A: the forecast line with the median, the range, 3 runs and the spent part",
            code == 0 and "Очаквана цена: ~$14.10 (диапазон $12.60-$15.60; медиана на роля и модел от 3 предишни run-а; ориентировъчно; вече похарчени $0.10)" in ga, ga[-900:])
    c.check("gate A: the role fallback is named", "finalverify fable: медиана на ролята за всички модели" in ga, ga[-600:])
    c2 = Case("forecast_two")
    history(c2, ["2026-10-01-a", "2026-10-02-b"])
    history(c2, ["2026-10-03-codex"], priced=False)   # only cost-0 calls (Codex): not a run for the forecast
    c2.scenario(base_scenario())
    code, out = c2.start()
    c.check("two priced earlier runs (this run and a cost-0 run do not count): no forecast line",
            code == 0 and "Очаквана цена" not in c2.read("harness", "gate_a.md"), c2.read("harness", "gate_a.md")[-600:])
    if not c.failed:
        rm_rf(c2.dir)
    return c


TESTS = {"happy": t_happy, "fix": t_fix, "escalate": t_escalate, "mismatch": t_mismatch, "checkbroken": t_checkbroken,
         "dead": t_dead, "resume": t_resume, "start": t_start_finalfix, "prebroken": t_prebroken, "questions": t_questions,
         "deploytwice": t_deploy_twice, "strayreported": t_stray_reported, "straydiff": t_stray_diff, "noagents": t_noagents,
         "hardstops": t_hardstops, "finalfixmismatch": t_finalfix_mismatch, "smoke": t_smoke, "codex": t_codex,
         "codexfallback": t_codex_fallback, "lock": t_lock, "cap": t_cap, "codexkill": t_codex_kill,
         "watchrollback": t_watch_rollback, "reviewgen": t_review_generations, "killfailed": t_kill_failed_no_fallback,
         "lockatomic": t_lock_atomic, "deploysealed": t_deploy_sealed,
         "verifystray": t_verify_keeps_stray, "realauthor": t_review_real_author, "planchanges": t_plan_change_history,
         "afterhook": t_after_hook_harness, "preflight": t_preflight, "workflow": t_workflow_parity, "killtree": t_kill_tree,
         "authors": t_authors, "finalstatus": t_final_status, "configbroken": t_config_broken, "readiness": t_readiness,
         "perfshort": t_perf_short, "quota": t_quota, "polling": t_polling, "fast": t_fast, "powerjson": t_power_json,
         "ultraguard": t_ultraguard, "coverage": t_coverage, "requestcov": t_request_coverage, "forecast": t_forecast}


def main():
    names = [a for a in sys.argv[1:] if not a.startswith("--")] or list(TESTS)
    keep = "--keep" in sys.argv
    failed = []
    for n in names:
        print(f"== {n}")
        c = TESTS[n]()
        failed += [f"{n}: {x}" for x in c.failed]
        if not keep and not c.failed:
            rm_rf(c.dir)
    print("\n" + ("ALL PASS" if not failed else f"{len(failed)} FAILED:\n  " + "\n  ".join(failed)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
