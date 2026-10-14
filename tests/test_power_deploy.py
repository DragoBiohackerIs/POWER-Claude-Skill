#!/usr/bin/env python3
"""Offline tests of power_deploy.py: a synthetic project, a local fake prod folder, a controllable transport
(fake_transport.py) and simulated HTTP probes (POWER_PROBE_MAP). No network, no credentials.
Run: PYTHONUTF8=1 python tests/test_power_deploy.py [--keep] [names...]

Scenarios: canary_before (503 / unreachable / fatal marker before the upload = no mutation; healthy deploys),
partial_write (a put that writes and then fails is rolled back), rollback_fail (a failing rollback is reported as
incomplete, never as rolled back), new_file_cleanup (a new file is removed on rollback), refetch (a second fetch
after apply keeps the original backup; rollback still works), remote_changed (prod changed between fetch and apply
= stop before any write), candidate_changed (the reviewed candidate was edited after fetch = stop),
binary_unknown (a binary whose prod state is unknown blocks the fetch), approval (P1-03 stage 2: fetch prints a
candidate token attempt:hash; apply --go needs --approved <token>; a token of another candidate or attempt never
passes; an edited deploy.json is a changed candidate), recovery_hooks (V3-04: the after hooks run after the
rollback of a failed put / byte verify too), watch_degraded (V3-03: a canary that is down now is never HEALTHY; a
bad baseline makes it DEGRADED without an automatic rollback).
"""
import json
import os
import re
import shutil
import subprocess
import sys


def q(s):
    return '"' + str(s).replace("\\", "/") + '"'

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from test_power_run import rm_rf, ROOT, PY, CREATE_NO_WINDOW  # noqa: E402

TRANSPORT = os.path.join(HERE, "fake_transport.py").replace("\\", "/")
CANARY = "https://canary.test/"
OLD = {"a.php": "<?php\nfunction a_init() {\n  return 'OLD_A';\n}\n", "b.php": "<?php\nfunction b_init() {\n  return 'OLD_B';\n}\n"}
NEW = {"a.php": OLD["a.php"].replace("OLD_A", "NEW_A"), "b.php": OLD["b.php"].replace("OLD_B", "NEW_B"),
       "c.php": "<?php\nfunction c_init() {\n  return 'NEW_C';\n}\n"}


def claude_md(prod_dir, canary=True, extra=None):
    t = f'"{PY}" "{TRANSPORT}"'.replace("\\", "/")
    profile = {"local_root": ".", "remote_root": prod_dir,
               "get": t + ' get "{remote}" "{local}"', "put": t + ' put "{local}" "{remote_dir}"',
               "rm": t + ' rm "{remote}"', "ls": t + ' ls "{remote_dir}"',
               "canary": [CANARY] if canary else [], "pace": 0, "settle": 0}
    profile.update(extra or {})
    block = json.dumps({"deploy": {"profiles": {"shop": profile}}})
    return f"# test project\n\n## POWER\n\n```json\n{block}\n```\n"


def sha(data):
    import hashlib
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def task(tid, path, action="edit"):
    return {"id": tid, "title": path, "spec": "x", "files": [{"path": path, "action": action}], "anchors": [], "provides": [],
            "uses": [], "non_goals": [], "acceptance": ["x"], "checks": [], "review_focus": "", "rules": "",
            "size": {"files": 1, "lines": 1}, "depends_on": []}


class Case:
    def __init__(self, name, files=("a.php", "b.php"), new_files=(), prod_files=None, canary=True):
        self.name = name
        self.dir = os.path.join(ROOT, "deploy_" + name)
        rm_rf(self.dir)
        self.proj = os.path.join(self.dir, "proj")
        self.prod = os.path.join(self.dir, "prod").replace("\\", "/")
        self.run = os.path.join(self.proj, "docs", "power", "2026-10-14-deploy")
        os.makedirs(self.proj)
        os.makedirs(self.prod)
        os.makedirs(self.run)
        self.behaviour_path = os.path.join(self.dir, "behaviour.json")
        self.log_path = os.path.join(self.dir, "transport.log")
        self.probe_path = os.path.join(self.dir, "probes.json")
        self.failed = []
        self.extra_env = {}
        self.canary = canary
        # local project at the OLD version, prod at OLD, snapshot, then the local edit to NEW
        all_files = list(files) + list(new_files)
        for fn in files:
            self.write(os.path.join(self.proj, fn), OLD[fn])
        for fn in (prod_files if prod_files is not None else files):
            self.write(os.path.join(self.prod, fn), OLD.get(fn, "binary"))
        self.write(os.path.join(self.proj, "CLAUDE.md"), claude_md(self.prod, canary))
        tasks = {"mode": "full", "goal": "deploy test", "rules": "", "risks": [], "contracts": [],
                 "tasks": [task(f"t{i + 1}", fn, "edit" if fn in files else "create") for i, fn in enumerate(all_files)]}
        json.dump(tasks, open(os.path.join(self.run, "tasks.json"), "w", encoding="utf-8"), indent=1)
        self.check_tool("snapshot")
        for fn in all_files:
            self.write(os.path.join(self.proj, fn), NEW.get(fn, b"\x89PNG\x00binary"))
        self.deploy_json({"profile": "shop", "files": [{"local": fn, "order": i + 1} for i, fn in enumerate(all_files)]})
        self.probes({CANARY: {"code": 200, "body": "<html>ok</html>"}})
        self.behaviour({})

    @staticmethod
    def write(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "wb" if isinstance(data, bytes) else "w"
        with open(path, mode, **({} if isinstance(data, bytes) else {"encoding": "utf-8", "newline": "\n"})) as f:
            f.write(data)

    def deploy_json(self, spec):
        json.dump(spec, open(os.path.join(self.run, "deploy.json"), "w", encoding="utf-8"), indent=1)

    def profile(self, **extra):
        """Rewrite the project's ## POWER deploy profile (CLAUDE.md) with these keys changed."""
        self.write(os.path.join(self.proj, "CLAUDE.md"), claude_md(self.prod, self.canary, extra or None))

    def row(self, local):
        return next(r for r in self.state()["files"] if r["local"] == local)

    def upload_path(self, local):
        return os.path.join(self.run, "deploy", "upload", local)

    def probes(self, m):
        json.dump(m, open(self.probe_path, "w", encoding="utf-8"))
        try:
            os.remove(self.probe_path + ".count.json")
        except OSError:
            pass

    def behaviour(self, b):
        json.dump(b, open(self.behaviour_path, "w", encoding="utf-8"))

    def env(self):
        return dict(os.environ, PYTHONUTF8="1", FAKE_TRANSPORT_BEHAVIOUR=self.behaviour_path, FAKE_TRANSPORT_LOG=self.log_path,
                    POWER_PROBE_MAP=self.probe_path, **self.extra_env)

    def check_tool(self, *argv):
        p = subprocess.run([PY, os.path.join(SKILL, "power_check.py"), *argv, "--run-dir", self.run, "--project-dir", self.proj],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=self.env(), creationflags=CREATE_NO_WINDOW)
        assert p.returncode == 0, p.stdout + p.stderr
        return p.stdout

    def deploy(self, *argv, timeout=300):
        p = subprocess.run([PY, os.path.join(SKILL, "power_deploy.py"), *argv, "--run-dir", self.run, "--project-dir", self.proj],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=self.env(), timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    def prod_text(self, fn):
        path = os.path.join(self.prod, fn)
        return open(path, encoding="utf-8").read() if os.path.isfile(path) else None

    def state(self):
        return json.load(open(os.path.join(self.run, "deploy", "state.json"), encoding="utf-8"))

    def token(self):
        """The candidate token (attempt:hash) fetch recorded, or None."""
        try:
            return self.state().get("candidate")
        except (OSError, ValueError):
            return None

    def go(self, *extra):
        """apply --go approved for the current candidate (the normal path of the orchestrator after #THE_OPERATOR#'s yes)."""
        tok = self.token()
        return self.deploy("apply", "--go", *(["--approved", tok] if tok else []), *extra)

    def transport_calls(self, *cmds):
        if not os.path.isfile(self.log_path):
            return []
        rows = [json.loads(l) for l in open(self.log_path, encoding="utf-8") if l.strip()]
        return [r for r in rows if not cmds or r["cmd"] in cmds]

    def clear_log(self):
        try:
            os.remove(self.log_path)
        except OSError:
            pass

    def check(self, label, cond, info=""):
        print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"\n       {info}"))
        if not cond:
            self.failed.append(label)


def status_of(out):
    for line in out.splitlines():
        if line.startswith("DEPLOY: "):
            return line[len("DEPLOY: "):].strip()
    return ""


# ---------- scenarios ----------

def t_canary_before():
    c = Case("canary")
    for label, probe in (("503", {"code": 503, "body": ""}), ("unreachable", {"code": 0, "body": ""}),
                         ("fatal marker", {"code": 200, "body": "<b>Fatal error</b>: boom"})):
        c.probes({CANARY: probe})
        c.clear_log()
        code, out = c.deploy("fetch")
        c.check(f"[{label}] fetch OK", code == 0 and "FETCH: OK" in out, out[-400:])
        code, out = c.go()
        c.check(f"[{label}] apply --go stops before the upload", code != 0 and status_of(out) == "PRECHECK_FAILED", (code, out[-500:]))
        c.check(f"[{label}] no put/rm happened", c.transport_calls("put", "rm") == [], c.transport_calls("put", "rm"))
        c.check(f"[{label}] prod unchanged", c.prod_text("a.php") == OLD["a.php"] and c.prod_text("b.php") == OLD["b.php"])
        c.check(f"[{label}] state has no applied record", not c.state().get("applied"), c.state().get("applied"))
    c.probes({CANARY: {"code": 200, "body": "ok"}})
    c.clear_log()
    code, out = c.go()
    c.check("[healthy] apply --go deploys", code == 0 and status_of(out) == "DEPLOYED", (code, out[-500:]))
    c.check("[healthy] prod has the new bytes", c.prod_text("a.php") == NEW["a.php"] and c.prod_text("b.php") == NEW["b.php"])
    return c


def t_partial_write():
    c = Case("partial")
    for mode in ("write_then_fail", "truncate_then_fail"):
        c.behaviour({"put": [{"src_contains": "upload/b.php", "mode": mode}]})
        c.prod_text("a.php")
        Case.write(os.path.join(c.prod, "a.php"), OLD["a.php"])
        Case.write(os.path.join(c.prod, "b.php"), OLD["b.php"])
        shutil.rmtree(os.path.join(c.run, "deploy"), ignore_errors=True)
        code, out = c.deploy("fetch")
        code, out = c.go()
        st = c.state()
        c.check(f"[{mode}] apply fails and rolls back", code != 0 and status_of(out) == "FAILED_ROLLED_BACK", (code, out[-600:]))
        c.check(f"[{mode}] b.php (the failed put) is back to OLD", c.prod_text("b.php") == OLD["b.php"], repr(c.prod_text("b.php")))
        c.check(f"[{mode}] a.php (uploaded before it) is back to OLD", c.prod_text("a.php") == OLD["a.php"], repr(c.prod_text("a.php")))
        rb = (st.get("rollbacks") or [{}])[-1].get("results") or []
        c.check(f"[{mode}] the rollback rows name both files, b first", [r["file"] for r in rb] == ["b.php", "a.php"], rb)
    return c


def t_rollback_fail():
    c = Case("rbfail")
    c.behaviour({"put": [{"src_contains": "upload/b.php", "mode": "write_then_fail"},
                         {"src_contains": "prod-before/a.php", "mode": "fail"}]})
    c.deploy("fetch")
    code, out = c.go()
    st = c.state()
    c.check("status is incomplete, not rolled back", code != 0 and status_of(out) == "FAILED_ROLLBACK_INCOMPLETE", (code, out[-600:]))
    c.check("b.php restored", c.prod_text("b.php") == OLD["b.php"])
    c.check("a.php still NEW on prod (the rollback of it failed)", c.prod_text("a.php") == NEW["a.php"])
    rb = (st.get("rollbacks") or [{}])[-1].get("results") or []
    c.check("the failed file is marked in the rollback rows", any(r["file"] == "a.php" and r["ok"] is False for r in rb), rb)
    rep = open(os.path.join(c.run, "deploy", "REPORT.md"), encoding="utf-8").read()
    c.check("REPORT names the unrestored file", "a.php" in rep and "FAILED" in rep, rep[-800:])
    return c


def t_new_file_cleanup():
    c = Case("newfile", files=("a.php",), new_files=("c.php",))
    # c.php (new) uploads first, then a.php fails before writing: c.php must be removed again
    c.deploy_json({"profile": "shop", "files": [{"local": "c.php", "order": 1}, {"local": "a.php", "order": 2}]})
    c.behaviour({"put": [{"src_contains": "upload/a.php", "mode": "fail"}]})
    code, out = c.deploy("fetch")
    c.check("fetch sees c.php as new", "c.php: new on prod" in out, out[-500:])
    code, out = c.go()
    c.check("rolled back", code != 0 and status_of(out) == "FAILED_ROLLED_BACK", (code, out[-500:]))
    c.check("c.php removed from prod", c.prod_text("c.php") is None)
    c.check("a.php untouched", c.prod_text("a.php") == OLD["a.php"])
    return c


def t_refetch():
    c = Case("refetch")
    c.deploy("fetch")
    code, out = c.go()
    c.check("first apply deployed", code == 0 and status_of(out) == "DEPLOYED", out[-300:])
    before = open(os.path.join(c.run, "deploy", "prod-before", "a.php"), encoding="utf-8").read()
    c.check("backup is OLD", before == OLD["a.php"])
    code, out = c.deploy("fetch")
    c.check("a second fetch after apply is refused", code != 0, out[-400:])
    c.check("backup still OLD", open(os.path.join(c.run, "deploy", "prod-before", "a.php"), encoding="utf-8").read() == OLD["a.php"])
    code, out = c.deploy("apply")
    c.check("dry-run after apply does not destroy state", code != 0 and os.path.isfile(os.path.join(c.run, "deploy", "prod-before", "a.php")), out[-300:])
    code, out = c.deploy("rollback", "--go")
    c.check("rollback still works", code == 0 and status_of(out) == "ROLLED_BACK", (code, out[-400:]))
    c.check("prod back to OLD", c.prod_text("a.php") == OLD["a.php"] and c.prod_text("b.php") == OLD["b.php"])
    return c


def t_remote_changed():
    c = Case("remote")
    c.deploy("fetch")
    Case.write(os.path.join(c.prod, "a.php"), OLD["a.php"].replace("OLD_A", "SOMEONE_ELSE"))
    c.clear_log()
    code, out = c.go()
    c.check("apply stops: prod changed since fetch", code != 0 and status_of(out) == "REMOTE_CHANGED", (code, out[-500:]))
    c.check("no put happened", c.transport_calls("put") == [], c.transport_calls("put"))
    c.check("the other change is still on prod", "SOMEONE_ELSE" in (c.prod_text("a.php") or ""))
    return c


def t_candidate_changed():
    c = Case("candidate")
    c.deploy("fetch")
    up = os.path.join(c.run, "deploy", "upload", "a.php")
    Case.write(up, open(up, encoding="utf-8").read().replace("NEW_A", "EDITED_AFTER_REVIEW"))
    c.clear_log()
    code, out = c.go()
    c.check("apply stops: the reviewed candidate changed", code != 0 and status_of(out) == "CANDIDATE_CHANGED", (code, out[-500:]))
    c.check("no put happened", c.transport_calls("put") == [])
    c.check("prod untouched", c.prod_text("a.php") == OLD["a.php"])
    return c


def t_binary_unknown():
    c = Case("binary", files=("a.php",), new_files=("logo.png",), prod_files=("a.php",))
    c.behaviour({"ls": {"mode": "fail"}})
    code, out = c.deploy("fetch")
    c.check("fetch blocks on binary-unknown", code != 0 and "binary-unknown" in out and "FETCH: FAIL" in out, out[-500:])
    c.deploy_json({"profile": "shop", "files": [{"local": "a.php", "order": 1}, {"local": "logo.png", "order": 2}], "allow_unrestorable": True})
    code, out = c.deploy("fetch")
    c.check("allow_unrestorable lets it through", code == 0 and "FETCH: OK" in out, out[-400:])
    return c


def t_binary_guard():
    """V2-01: the unrestorable-binary policy holds at apply too, not only at fetch. A fetch that FAILED leaves no
    applicable candidate: apply --go --approved <its token> stops before any write (binary-existing, binary-unknown);
    accepting the risk is a new candidate (`allow_unrestorable` in deploy.json changes the token)."""
    c = Case("binaryguard", files=(), new_files=("asset.png",), prod_files=("asset.png",))
    code, out = c.deploy("fetch")
    tok = c.token()
    c.check("fetch refuses binary-existing (FAIL) but records a token", code != 0 and "binary-existing" in out and "FETCH: FAIL" in out, out[-400:])
    before = open(os.path.join(c.prod, "asset.png"), "rb").read()
    c.clear_log()
    code, out = c.deploy("apply", "--go", "--approved", tok) if tok else (1, "no token")
    c.check("apply --go with the failed fetch's token stops before the upload", code != 0 and status_of(out) in ("FETCH_FAILED", "UNRESTORABLE_BINARY"), (code, out[-400:]))
    c.check("  no put, prod binary untouched, no applied record", c.transport_calls("put") == [] and open(os.path.join(c.prod, "asset.png"), "rb").read() == before and not c.state().get("applied"), c.transport_calls("put"))
    c.check("  the stop names the binary and allow_unrestorable", "asset.png" in out and "allow_unrestorable" in out, out[-400:])
    code, out = c.deploy("apply", "--approved", tok) if tok else (1, "")
    c.check("a dry apply stops the same way", code != 0 and status_of(out) in ("FETCH_FAILED", "UNRESTORABLE_BINARY"), (code, out[-300:]))
    # binary-unknown (ls fails) with allow_unrestorable absent: same stop at apply
    c2 = Case("binaryguard_unknown", files=("a.php",), new_files=("logo.png",), prod_files=("a.php",))
    c2.behaviour({"ls": {"mode": "fail"}})
    code, out = c2.deploy("fetch")
    tok2 = c2.token()
    c2.check("fetch FAIL on binary-unknown", code != 0 and "binary-unknown" in out, out[-300:])
    c2.clear_log()
    code, out = c2.deploy("apply", "--go", "--approved", tok2) if tok2 else (1, "no token")
    c2.check("apply stops before the upload for binary-unknown too; a.php not uploaded either", code != 0 and status_of(out) in ("FETCH_FAILED", "UNRESTORABLE_BINARY") and c2.transport_calls("put") == [] and c2.prod_text("a.php") == OLD["a.php"], (code, out[-400:], c2.transport_calls("put")))
    # the explicit acceptance: a new candidate (other token), then apply goes through
    c2.deploy_json({"profile": "shop", "files": [{"local": "a.php", "order": 1}, {"local": "logo.png", "order": 2}], "allow_unrestorable": True})
    code, out = c2.deploy("apply", "--go", "--approved", tok2) if tok2 else (1, "")
    c2.check("the old token does not pass for the spec with allow_unrestorable (CANDIDATE_CHANGED or FETCH_FAILED), no put", code != 0 and c2.transport_calls("put") == [], (code, out[-300:]))
    code, out = c2.deploy("fetch")
    tok3 = c2.token()
    c2.check("fetch OK with allow_unrestorable, another token", code == 0 and "FETCH: OK" in out and tok3 != tok2, (tok2, tok3, out[-300:]))
    code, out = c2.deploy("apply", "--go", "--approved", tok3)
    c2.check("apply with the accepted candidate deploys", code == 0 and status_of(out) == "DEPLOYED" and c2.prod_text("a.php") == NEW["a.php"] and os.path.isfile(os.path.join(c2.prod, "logo.png")), (code, out[-300:]))
    c.failed += [f"unknown: {x}" for x in c2.failed]
    if not c2.failed:
        rm_rf(c2.dir)
    return c


def t_approval():
    """P1-03 stage 2: the yes is for one candidate. fetch records attempt + content hash as a token; apply --go needs
    --approved <token>; another candidate (new attempt, edited deploy.json, other prod) never passes with the old one."""
    c = Case("approval")
    code, out = c.deploy("fetch")
    tok = c.token()
    c.check("fetch records and prints the candidate token attempt:hash", code == 0 and tok is not None and re.fullmatch(r"\d+:[0-9a-f]{12}", tok or "") is not None and f"CANDIDATE: {tok}" in out, (tok, out[-400:]))
    c.check("the first candidate is attempt 1", (tok or "").startswith("1:"), tok)
    code, out = c.deploy("fetch")
    c.check("a second fetch of the same prod and candidate gives the same token (same attempt)", code == 0 and c.token() == tok, (tok, c.token()))
    c.clear_log()
    code, out = c.deploy("apply", "--go")
    c.check("apply --go without --approved stops before any write: NOT_APPROVED", code != 0 and status_of(out) == "NOT_APPROVED", (code, out[-400:]))
    c.check("  the stop names the token to approve", tok in out, out[-400:])
    code, out = c.deploy("apply", "--go", "--approved", "1:000000000000")
    c.check("a token of another candidate stops: APPROVAL_MISMATCH", code != 0 and status_of(out) == "APPROVAL_MISMATCH", (code, out[-400:]))
    code, out = c.deploy("apply", "--go", "--approved", "2:" + tok.split(":")[1])
    c.check("the same hash with another attempt number stops too", code != 0 and status_of(out) == "APPROVAL_MISMATCH", (code, out[-400:]))
    c.check("no put happened, prod untouched, no applied record", c.transport_calls("put") == [] and c.prod_text("a.php") == OLD["a.php"] and not c.state().get("applied"), c.transport_calls("put"))
    # deploy.json edited after the fetch (an after command appears): the reviewed candidate is gone
    spec = json.load(open(os.path.join(c.run, "deploy.json"), encoding="utf-8"))
    spec["after"] = ["echo purge"]
    c.deploy_json(spec)
    code, out = c.deploy("apply", "--go", "--approved", tok)
    c.check("deploy.json edited after fetch = CANDIDATE_CHANGED, no put", code != 0 and status_of(out) == "CANDIDATE_CHANGED" and "deploy.json" in out and c.transport_calls("put") == [], (code, out[-400:]))
    code, out = c.deploy("fetch")
    tok_b = c.token()
    c.check("fetch with the edited deploy.json gives another token, same attempt", code == 0 and tok_b != tok and tok_b.startswith("1:"), (tok, tok_b))
    spec.pop("after")
    c.deploy_json(spec)
    c.deploy("fetch")
    c.check("the original deploy.json gives the original token again", c.token() == tok, (tok, c.token()))
    code, out = c.deploy("apply", "--go", "--approved", tok_b)
    c.check("the token of the edited spec does not pass for the restored one", code != 0 and status_of(out) == "APPROVAL_MISMATCH" and c.transport_calls("put") == [], (code, out[-400:]))
    code, out = c.deploy("apply", "--go", "--approved", tok)
    c.check("the matching token deploys", code == 0 and status_of(out) == "DEPLOYED" and c.prod_text("a.php") == NEW["a.php"], (code, out[-400:]))
    st = c.state()
    c.check("the approval is recorded in the state with the token", (st.get("approval") or {}).get("token") == tok, st.get("approval"))
    rep = open(os.path.join(c.run, "deploy", "REPORT.md"), encoding="utf-8").read()
    c.check("REPORT names the candidate and the approval", tok in rep and "pproved" in rep, rep[:600])
    # round 2: manual rollback (prod back to OLD = the snapshot), a newer local a.php -> fetch --new-attempt ->
    # attempt 2 with another hash; the attempt-1 yes is void
    code, out = c.deploy("rollback", "--go")
    c.check("manual rollback puts attempt 1 back", code == 0 and status_of(out) == "ROLLED_BACK" and c.prod_text("a.php") == OLD["a.php"], (code, out[-300:]))
    Case.write(os.path.join(c.proj, "a.php"), NEW["a.php"].replace("NEW_A", "NEWER_A"))
    code, out = c.deploy("fetch")
    c.check("a plain fetch after the apply is still refused (the backup stays)", code != 0 and "new-attempt" in out, out[-300:])
    code, out = c.deploy("fetch", "--new-attempt")
    tok2 = c.token()
    c.check("new attempt: attempt 2 and another hash", code == 0 and "FETCH: OK" in out and (tok2 or "").startswith("2:") and tok2.split(":")[1] != tok.split(":")[1], (out[-400:], tok2))
    c.check("attempt 1 is archived with its approval", os.path.isfile(os.path.join(c.run, "deploy", "attempts", "01", "state.json")) and
            json.load(open(os.path.join(c.run, "deploy", "attempts", "01", "state.json"), encoding="utf-8")).get("approval", {}).get("token") == tok)
    c.clear_log()
    code, out = c.deploy("apply", "--go", "--approved", tok)
    c.check("the attempt-1 approval does not pass for attempt 2", code != 0 and status_of(out) == "APPROVAL_MISMATCH" and c.transport_calls("put") == [], (code, out[-400:]))
    c.check("prod still OLD", c.prod_text("a.php") == OLD["a.php"], repr(c.prod_text("a.php")))
    code, out = c.deploy("apply", "--go", "--approved", tok2)
    c.check("the attempt-2 token deploys the newer file", code == 0 and status_of(out) == "DEPLOYED" and "NEWER_A" in (c.prod_text("a.php") or ""), (code, out[-400:]))
    code, out = c.deploy("status")
    c.check("status shows the candidate token and the approval", tok2 in out and "approved" in out, out[-400:])
    return c


# ---------- Astra review v2 (2026-10-14), stage B: "complete evidence" ----------

MERGE_MOCK = os.path.join(HERE, "mock_merge.py").replace("\\", "/")


def merge_subcases(cases):
    """Fold the sub-cases' failures into the first one; remove the clean sub-case folders."""
    first = cases[0]
    for c in cases[1:]:
        first.failed += [f"{c.name}: {x}" for x in c.failed]
        if not c.failed:
            rm_rf(c.dir)
    return first


def t_precheck_all_rows():
    """V2-02: the prod re-check before the first write covers every row the candidate relies on, not only the ones
    to upload: a binary planned as new that appeared, a new text file whose existence cannot be confirmed (ls down =
    unknown, never absence), a skip row that changed. Each is REMOTE_CHANGED before any put."""
    # A. binary planned as new; another session puts one there before apply
    c = Case("precheck_binary", files=(), new_files=("asset.png",))
    code, out = c.deploy("fetch")
    c.check("[binary-new] fetch OK", code == 0 and "FETCH: OK" in out, out[-300:])
    other = b"OTHER SESSION BINARY"
    Case.write(os.path.join(c.prod, "asset.png"), other)
    c.clear_log()
    code, out = c.go()
    c.check("[binary-new] apply stops: a binary appeared on prod since the fetch", code != 0 and status_of(out) == "REMOTE_CHANGED", (code, out[-400:]))
    c.check("[binary-new] no put; the other session's file is intact", c.transport_calls("put") == [] and open(os.path.join(c.prod, "asset.png"), "rb").read() == other, c.transport_calls("put"))
    c.check("[binary-new] no applied record (repeatable after a new fetch)", not c.state().get("applied"))
    # B. text file planned as new; another session creates it and ls is unavailable at apply
    c2 = Case("precheck_unknown", files=(), new_files=("c.php",))
    c2.deploy("fetch")
    Case.write(os.path.join(c2.prod, "c.php"), "ANOTHER SESSION ADDED THIS")
    c2.behaviour({"ls": {"mode": "fail"}})
    c2.clear_log()
    code, out = c2.go()
    c2.check("[new+unknown] apply stops: unknown existence is not absence", code != 0 and status_of(out) == "REMOTE_CHANGED", (code, out[-400:]))
    c2.check("[new+unknown] no put; the other session's file is intact", c2.transport_calls("put") == [] and c2.prod_text("c.php") == "ANOTHER SESSION ADDED THIS", c2.transport_calls("put"))
    c2.check("[new+unknown] the stop says the existence could not be confirmed", "cannot" in out or "unknown" in out, out[-400:])
    os.remove(os.path.join(c2.prod, "c.php"))
    code, out = c2.go()
    c2.check("[new+unknown] even a truly absent file is not confirmed absent while ls is down: stop", code != 0 and status_of(out) == "REMOTE_CHANGED" and c2.transport_calls("put") == [], (code, out[-300:]))
    c2.behaviour({})
    code, out = c2.go()
    c2.check("[new] with ls back and the file absent, apply deploys", code == 0 and status_of(out) == "DEPLOYED" and c2.prod_text("c.php") == NEW["c.php"], (code, out[-300:]))
    # C. a skip row (prod already at the requested version) changes on prod before apply
    c3 = Case("precheck_skip")
    Case.write(os.path.join(c3.prod, "b.php"), NEW["b.php"])
    code, out = c3.deploy("fetch")
    c3.check("[skip] fetch: a.php upload, b.php skip", code == 0 and [r["action"] for r in c3.state()["files"]] == ["upload", "skip"], out[-300:])
    Case.write(os.path.join(c3.prod, "b.php"), "CHANGED AFTER FETCH")
    c3.clear_log()
    code, out = c3.go()
    c3.check("[skip] apply stops: the skip row changed on prod (the approved set no longer exists)", code != 0 and status_of(out) == "REMOTE_CHANGED" and "b.php" in out, (code, out[-400:]))
    c3.check("[skip] no put, a.php untouched", c3.transport_calls("put") == [] and c3.prod_text("a.php") == OLD["a.php"], c3.transport_calls("put"))
    Case.write(os.path.join(c3.prod, "b.php"), NEW["b.php"])
    code, out = c3.go()
    c3.check("[skip] with prod back as fetched, apply deploys", code == 0 and status_of(out) == "DEPLOYED" and c3.prod_text("a.php") == NEW["a.php"], (code, out[-300:]))
    # D. a fetched text row whose re-download fails at apply is a stop, not "unchanged"
    c4 = Case("precheck_getfail", files=("a.php",))
    c4.deploy("fetch")
    c4.behaviour({"get": [{"remote_contains": "a.php", "mode": "fail"}]})
    c4.clear_log()
    code, out = c4.go()
    c4.check("[get fails] apply stops when prod cannot be re-read", code != 0 and status_of(out) == "REMOTE_CHANGED" and c4.transport_calls("put") == [], (code, out[-300:]))
    return merge_subcases([c, c2, c3, c4])


def t_effective_config():
    """V2-03: the candidate token covers the effective configuration, not only deploy.json and the file bytes: the
    profile's after / canary / transport commands / remote root and the upload order. A profile changed after the
    fetch is CANDIDATE_CHANGED on a direct apply (no re-fetch needed to notice), and a fetch with it gives another
    token. The transport command templates are never written to the state or the report."""
    c = Case("effective", files=("a.php", "b.php"))
    code, out = c.deploy("fetch")
    tok = c.token()
    c.check("fetch OK with a token", code == 0 and tok, out[-300:])
    marker = os.path.join(c.proj, "profile_changed_marker.txt")
    variants = (("after", {"after": [f"echo PROFILE_CHANGED > {q(marker)}"]}),
                ("canary", {"canary": ["https://other-canary.test/"]}),
                ("put command", {"put": f'"{PY}" "{TRANSPORT}" put "{{local}}" "{{remote_dir}}" --other'.replace("\\", "/")}),
                ("remote root", {"remote_root": c.prod + "/sub"}),
                ("last rule", {"last": ["a.php"]}))
    for label, extra in variants:
        c.profile(**extra)
        c.clear_log()
        code, out = c.deploy("apply", "--go", "--approved", tok)
        c.check(f"[{label}] a profile changed after the fetch stops the direct apply: CANDIDATE_CHANGED", code != 0 and status_of(out) == "CANDIDATE_CHANGED", (code, out[-400:]))
        c.check(f"[{label}] no put, no after command ran, prod untouched", c.transport_calls("put") == [] and not os.path.exists(marker) and c.prod_text("a.php") == OLD["a.php"], c.transport_calls("put"))
        if label in ("after", "canary", "remote root", "last rule"):
            code, out = c.deploy("fetch")
            c.check(f"[{label}] a fetch with the changed profile gives another token, same attempt", code == 0 and c.token() != tok and c.token().startswith("1:"), (tok, c.token(), out[-300:]))
            c.profile()
            c.deploy("fetch")
            c.check(f"[{label}] the original profile gives the original token again", c.token() == tok, (tok, c.token()))
        else:
            c.profile()
    # the upload order is part of the candidate (same files, same bytes, same deploy.json keys)
    c.deploy_json({"profile": "shop", "files": [{"local": "b.php", "order": 1}, {"local": "a.php", "order": 2}]})
    code, out = c.deploy("fetch")
    c.check("[order] swapped order = another token", code == 0 and c.token() != tok, (tok, c.token()))
    c.deploy_json({"profile": "shop", "files": [{"local": "a.php", "order": 1}, {"local": "b.php", "order": 2}]})
    c.deploy("fetch")
    c.check("[order] the original order gives the original token", c.token() == tok, (tok, c.token()))
    st_text = open(os.path.join(c.run, "deploy", "state.json"), encoding="utf-8").read()
    c.check("the transport command templates are not stored in the state", "fake_transport.py" not in st_text and TRANSPORT not in st_text, st_text[:300])
    c.clear_log()
    code, out = c.deploy("apply", "--go", "--approved", tok)
    c.check("the unchanged configuration deploys with the original token", code == 0 and status_of(out) == "DEPLOYED" and c.prod_text("a.php") == NEW["a.php"], (code, out[-300:]))
    rep = open(os.path.join(c.run, "deploy", "REPORT.md"), encoding="utf-8").read()
    c.check("the report does not expose the transport commands", "fake_transport.py" not in rep, rep[:400])
    return c


def t_merge_error():
    """V2-08: a `git merge-file` process failure (exit 128, git missing 127, 255, a timeout) is never a merged
    candidate: the row is `merge-error` / stop, nothing is written to upload/, fetch is FAIL and apply refuses.
    Diagnostics on stderr never end up in the source; markers with exit 0 are still a conflict."""
    prod_tail = OLD["a.php"] + "// prod tail\n"      # a divergence that merges cleanly (other hunk)
    cases = []
    for mode, label in (("exit128", "exit 128"), ("exit127", "git missing, exit 127"), ("exit255", "exit 255")):
        c = Case("merge_" + mode, files=("a.php",))
        Case.write(os.path.join(c.prod, "a.php"), prod_tail)
        c.extra_env = {"POWER_GIT_MERGE": f'"{PY}" "{MERGE_MOCK}"'.replace("\\", "/"), "MOCK_MERGE": mode}
        code, out = c.deploy("fetch")
        row = c.row("a.php")
        c.check(f"[{label}] fetch FAIL, the row is merge-error / stop, not merged", code != 0 and "FETCH: FAIL" in out and row["prod"] == "merge-error" and row["action"] == "stop", (code, row.get("prod"), row.get("action"), out[-400:]))
        c.check(f"[{label}] no candidate file was published from the diagnostic", not os.path.isfile(c.upload_path("a.php")), c.upload_path("a.php"))
        c.check(f"[{label}] the output names the merge failure", "merge" in out.lower() and ("fatal" in out or "not found" in out or str(mode[-3:]) in out), out[-400:])
        c.clear_log()
        code, out = c.go()
        c.check(f"[{label}] apply refuses, no put, prod intact", code != 0 and c.transport_calls("put") == [] and c.prod_text("a.php") == prod_tail, (code, out[-300:]))
        cases.append(c)
    c = Case("merge_markers0", files=("a.php",))
    Case.write(os.path.join(c.prod, "a.php"), prod_tail)
    c.extra_env = {"POWER_GIT_MERGE": f'"{PY}" "{MERGE_MOCK}"'.replace("\\", "/"), "MOCK_MERGE": "markers_exit0"}
    code, out = c.deploy("fetch")
    row = c.row("a.php")
    c.check("[markers, exit 0] still a conflict / stop, never upload", code != 0 and row["action"] == "stop" and row["prod"] in ("conflict", "merge-error"), (row.get("prod"), row.get("action")))
    code, out = c.go()
    c.check("[markers, exit 0] apply refuses", code != 0 and c.transport_calls("put") == [], out[-300:])
    cases.append(c)
    c = Case("merge_noise", files=("a.php",))
    Case.write(os.path.join(c.prod, "a.php"), prod_tail)
    c.extra_env = {"POWER_GIT_MERGE": f'"{PY}" "{MERGE_MOCK}"'.replace("\\", "/"), "MOCK_MERGE": "stderr_noise"}
    code, out = c.deploy("fetch")
    row = c.row("a.php")
    merged = open(c.upload_path("a.php"), encoding="utf-8").read() if os.path.isfile(c.upload_path("a.php")) else ""
    c.check("[stderr noise] a clean merge with diagnostics on stderr is merged", code == 0 and row["prod"] == "merged" and row["action"] == "upload", (code, row.get("prod"), out[-300:]))
    c.check("[stderr noise] the candidate holds our hunk and prod's, not the diagnostic", "NEW_A" in merged and "// prod tail" in merged and "warning" not in merged, merged)
    code, out = c.go()
    c.check("[stderr noise] apply deploys the merged file", code == 0 and status_of(out) == "DEPLOYED" and c.prod_text("a.php") == merged, (code, out[-300:]))
    cases.append(c)
    # a real conflict without the mock stays a conflict (regression guard)
    c = Case("merge_realconflict", files=("a.php",))
    Case.write(os.path.join(c.prod, "a.php"), OLD["a.php"].replace("OLD_A", "PROD_A"))
    code, out = c.deploy("fetch")
    c.check("[real conflict] conflict / stop with the markers in upload/", code != 0 and c.row("a.php")["prod"] == "conflict" and "<<<<<<<" in open(c.upload_path("a.php"), encoding="utf-8").read(), out[-300:])
    cases.append(c)
    return merge_subcases(cases)


def t_backup_integrity():
    """V2-09: a rollback restores only a backup whose digest is the one recorded at fetch (`prod_before_sha`), and
    verifies the restored prod bytes against that digest: a corrupted, truncated, replaced or missing backup is
    ROLLBACK_INCOMPLETE and is never uploaded; the same check runs before the first write of apply."""
    c = Case("backup", files=("a.php",))
    c.deploy("fetch")
    code, out = c.go()
    c.check("deployed", code == 0 and status_of(out) == "DEPLOYED", out[-300:])
    backup = c.row("a.php")["prod_before"]
    good = open(backup, "rb").read()
    c.check("the state records the backup digest", c.row("a.php").get("prod_before_sha") == sha(good), c.row("a.php").get("prod_before_sha"))
    for label, data in (("corrupted", b"CORRUPTED BACKUP"), ("truncated", good[: len(good) // 2]), ("replaced, same length", good[:-1] + b"X")):
        Case.write(backup, data)
        c.clear_log()
        code, out = c.deploy("rollback", "--go")
        c.check(f"[{label}] rollback is INCOMPLETE, exit 2", code != 0 and status_of(out) == "ROLLBACK_INCOMPLETE", (code, out[-400:]))
        c.check(f"[{label}] the bad backup was not uploaded; prod keeps the deployed version", c.transport_calls("put") == [] and c.prod_text("a.php") == NEW["a.php"], (c.transport_calls("put"), c.prod_text("a.php")))
        c.check(f"[{label}] the output names the digest mismatch", "digest" in out.lower() or "sha" in out.lower(), out[-400:])
    os.remove(backup)
    c.clear_log()
    code, out = c.deploy("rollback", "--go")
    c.check("[missing] rollback is INCOMPLETE, nothing uploaded", code != 0 and status_of(out) == "ROLLBACK_INCOMPLETE" and c.transport_calls("put") == [], (code, out[-300:]))
    Case.write(backup, good)
    code, out = c.deploy("rollback", "--go")
    c.check("[intact] the original backup rolls back and verifies", code == 0 and status_of(out) == "ROLLED_BACK" and c.prod_text("a.php") == OLD["a.php"], (code, out[-300:]))
    rep = open(os.path.join(c.run, "deploy", "REPORT.md"), encoding="utf-8").read()
    c.check("REPORT lists the failed restores", "ROLLBACK_INCOMPLETE" in rep and "FAILED" in rep, rep[-600:])
    # before the first write: a backup that does not match the fetched digest stops apply
    c2 = Case("backup_pre", files=("a.php",))
    c2.deploy("fetch")
    Case.write(c2.row("a.php")["prod_before"], "CORRUPTED BEFORE APPLY")
    c2.clear_log()
    code, out = c2.go()
    c2.check("[pre-apply] apply stops before any write when the backup digest differs", code != 0 and status_of(out) == "BACKUP_INVALID" and c2.transport_calls("put") == [] and c2.prod_text("a.php") == OLD["a.php"], (code, out[-400:]))
    # the restored bytes are checked against the digest, not against whatever the transport reports
    c3 = Case("backup_wrongwrite", files=("a.php",))
    c3.deploy("fetch")
    c3.go()
    c3.behaviour({"put": [{"src_contains": "prod-before/a.php", "mode": "corrupt_write"}]})
    code, out = c3.deploy("rollback", "--go")
    c3.check("[transport rewrites] a restore whose prod bytes differ from the digest is INCOMPLETE", code != 0 and status_of(out) == "ROLLBACK_INCOMPLETE" and "corrupted by the transport" in (c3.prod_text("a.php") or ""), (code, out[-300:]))
    return merge_subcases([c, c2, c3])


def t_seal():
    """V2-11: the supported path for a hand-resolved conflict: resolve in upload/<file>, then `seal` validates (no
    markers, a file for every stop row), regenerates the diff, hashes the resolution and issues a NEW candidate token
    for review and yes. The pre-resolution token cannot approve it; an edit after the seal is CANDIDATE_CHANGED (no
    conflict exemption); editing the state by hand does not bypass the hash; diverged-no-base has the same path."""
    c = Case("seal", files=("a.php",))
    Case.write(os.path.join(c.prod, "a.php"), OLD["a.php"].replace("OLD_A", "PROD_A"))
    code, out = c.deploy("fetch")
    tok0 = c.token()
    row = c.row("a.php")
    c.check("fetch: a real conflict, FAIL, with a token", code != 0 and row["prod"] == "conflict" and tok0, (row.get("prod"), tok0))
    c.check("the conflict row is hashed too (no exemption)", row.get("candidate_sha") == sha(open(row["upload_from"], "rb").read()), row.get("candidate_sha"))
    c.clear_log()
    code, out = c.deploy("apply", "--go", "--approved", tok0)
    c.check("apply refuses the unresolved conflict, no put", code != 0 and c.transport_calls("put") == [], out[-300:])
    resolved = NEW["a.php"].replace("NEW_A", "RESOLVED_A")
    Case.write(row["upload_from"], resolved)
    code, out = c.deploy("apply", "--go", "--approved", tok0)
    c.check("a resolved file that is not sealed is still refused, and the refusal names `seal`", code != 0 and c.transport_calls("put") == [] and "seal" in out, out[-400:])
    # the tempting workaround: flip the action in the state by hand with other bytes -> the hash check catches it
    st = c.state()
    st["files"][0]["action"] = "upload"
    json.dump(st, open(os.path.join(c.run, "deploy", "state.json"), "w", encoding="utf-8"))
    Case.write(row["upload_from"], resolved.replace("RESOLVED_A", "UNREVIEWED_RESOLUTION"))
    code, out = c.deploy("apply", "--go", "--approved", tok0)
    c.check("a hand-edited state does not deploy unreviewed bytes with the old token", code != 0 and status_of(out) in ("CANDIDATE_CHANGED", "FETCH_FAILED") and c.transport_calls("put") == [] and c.prod_text("a.php") != resolved.replace("RESOLVED_A", "UNREVIEWED_RESOLUTION"), (code, out[-400:]))
    st["files"][0]["action"] = "stop"
    json.dump(st, open(os.path.join(c.run, "deploy", "state.json"), "w", encoding="utf-8"))
    # seal: markers left -> refused; clean resolution -> new token
    Case.write(row["upload_from"], "<<<<<<< local\nx\n=======\ny\n>>>>>>> prod\n")
    code, out = c.deploy("seal")
    c.check("seal refuses while markers remain", code != 0 and "marker" in out.lower() and c.token() == tok0, (code, out[-300:]))
    Case.write(row["upload_from"], resolved)
    code, out = c.deploy("seal")
    tok1 = c.token()
    row = c.row("a.php")
    c.check("seal OK: a new token, the row is upload with the resolved hash", code == 0 and "SEAL: OK" in out and f"CANDIDATE: {tok1}" in out and tok1 != tok0 and tok1.startswith("1:") and row["action"] == "upload" and row.get("candidate_sha") == sha(resolved) and row.get("resolved"), (code, tok0, tok1, row.get("action"), out[-400:]))
    diff_path = row.get("merged_diff") or ""
    c.check("the diff against prod was regenerated for the resolution", os.path.isfile(diff_path) and "RESOLVED_A" in open(diff_path, encoding="utf-8").read(), diff_path)
    c.check("the state no longer counts the conflict as open", c.state().get("fetch_status") == "OK" and not c.state().get("conflicts"), (c.state().get("fetch_status"), c.state().get("conflicts")))
    c.clear_log()
    code, out = c.deploy("apply", "--go", "--approved", tok0)
    c.check("the pre-resolution token cannot approve the sealed candidate", code != 0 and status_of(out) == "APPROVAL_MISMATCH" and c.transport_calls("put") == [], (code, out[-300:]))
    Case.write(row["upload_from"], resolved.replace("RESOLVED_A", "EDITED_AFTER_SEAL"))
    code, out = c.deploy("apply", "--go", "--approved", tok1)
    c.check("an edit after the seal is CANDIDATE_CHANGED", code != 0 and status_of(out) == "CANDIDATE_CHANGED" and c.transport_calls("put") == [], (code, out[-300:]))
    Case.write(row["upload_from"], resolved)
    code, out = c.deploy("seal")
    c.check("sealing the same bytes again gives the same token", code == 0 and c.token() == tok1, (tok1, c.token(), out[-200:]))
    code, out = c.deploy("apply", "--go", "--approved", tok1)
    c.check("the sealed candidate deploys the resolution", code == 0 and status_of(out) == "DEPLOYED" and c.prod_text("a.php") == resolved, (code, out[-300:]))
    code, out = c.deploy("seal")
    c.check("seal after an apply --go is refused (the backup stays)", code != 0 and c.prod_text("a.php") == resolved, out[-300:])
    code, out = c.deploy("rollback", "--go")
    c.check("rollback restores prod's version", code == 0 and status_of(out) == "ROLLED_BACK" and "PROD_A" in (c.prod_text("a.php") or ""), (code, out[-300:]))
    # diverged-no-base: prod has a file the plan creates
    c2 = Case("seal_nobase", files=(), new_files=("c.php",), prod_files=("c.php",))
    code, out = c2.deploy("fetch")
    c2.check("[no base] fetch: diverged-no-base, FAIL", code != 0 and c2.row("c.php")["prod"] == "diverged-no-base", out[-300:])
    code, out = c2.deploy("seal")
    c2.check("[no base] seal without a resolved file refuses and names where it goes", code != 0 and "c.php" in out and "upload" in out, out[-300:])
    Case.write(c2.upload_path("c.php"), NEW["c.php"])
    code, out = c2.deploy("seal")
    tok = c2.token()
    c2.check("[no base] seal OK with the resolution in upload/", code == 0 and "SEAL: OK" in out and c2.row("c.php")["action"] == "upload", (code, out[-300:]))
    code, out = c2.deploy("apply", "--go", "--approved", tok)
    c2.check("[no base] the sealed candidate deploys", code == 0 and status_of(out) == "DEPLOYED" and c2.prod_text("c.php") == NEW["c.php"], (code, out[-300:]))
    code, out = c2.deploy("rollback", "--go")
    c2.check("[no base] rollback puts prod's own version back", code == 0 and status_of(out) == "ROLLED_BACK" and c2.prod_text("c.php") == "binary", (code, c2.prod_text("c.php")))
    return merge_subcases([c, c2])


# ---------- Astra review v2 (2026-10-14), stage D ----------

def t_after_hooks():
    """V2-12: an `after` hook is required unless it says `optional`. A failed required hook is AFTER_FAILED (exit 2,
    the files stay on prod - nothing is undone automatically - and the report names the hook for manual recovery),
    never DEPLOYED; a failed optional hook is a warning. The same policy for the recovery hooks of a rollback:
    ROLLED_BACK_AFTER_FAILED is not a clean rollback (exit 2), in apply, watch and rollback alike."""
    ok, down = {"code": 200, "body": "ok"}, {"code": 503, "body": ""}
    c = Case("after_hooks", files=("a.php",))
    c.deploy_json({"profile": "shop", "files": [{"local": "a.php"}], "after": ["exit 7"]})
    code, out = c.deploy("fetch")
    c.check("fetch OK", code == 0 and "FETCH: OK" in out, out[-300:])
    code, out = c.go()
    st = c.state()
    c.check("required hook failed: AFTER_FAILED, exit 2, never DEPLOYED", code == 2 and status_of(out) == "AFTER_FAILED", (code, status_of(out), out[-400:]))
    c.check("the file stays on prod (hook effects are not undone automatically)", c.prod_text("a.php") == NEW["a.php"])
    hooks = (st.get("after") or {}).get("apply") or [{}]
    c.check("state records the hook, its exit and that it is required", hooks[0].get("code") == 7 and hooks[0].get("optional") is False, hooks)
    c.check("DEPLOY_JSON names the failed required hooks", '"after_failed"' in out and "exit 7" in out, out[-500:])
    rep = open(os.path.join(c.run, "deploy", "REPORT.md"), encoding="utf-8").read()
    c.check("REPORT names the hook for manual recovery", "manual recovery" in rep and "exit 7" in rep, rep[-900:])
    c.check("applied record exists (prod was written)", (st.get("applied") or {}).get("go") is True, st.get("applied"))
    # a manual rollback whose required recovery hook fails is not a clean rollback
    code, out = c.deploy("rollback", "--go")
    c.check("rollback --go with a failing required hook: ROLLED_BACK_AFTER_FAILED, exit 2", code == 2 and status_of(out) == "ROLLED_BACK_AFTER_FAILED", (code, status_of(out), out[-400:]))
    c.check("prod bytes are back at OLD all the same", c.prod_text("a.php") == OLD["a.php"])
    rb = (c.state().get("rollbacks") or [{}])[-1]
    c.check("the file rollback itself is recorded as ROLLED_BACK (bytes) - the hook is the open part", rb.get("status") == "ROLLED_BACK", rb)
    # the orchestrator marks the hook optional by hand -> the rollback is clean
    c.deploy_json({"profile": "shop", "files": [{"local": "a.php"}], "after": [{"cmd": "exit 7", "optional": True}]})
    code, out = c.deploy("rollback", "--go")
    c.check("optional hook failing on rollback: ROLLED_BACK, exit 0", code == 0 and status_of(out) == "ROLLED_BACK", (code, status_of(out), out[-300:]))
    # optional on apply = a warning, DEPLOYED; both hooks and their flags are recorded
    c2 = Case("after_hooks_opt", files=("a.php",))
    c2.deploy_json({"profile": "shop", "files": [{"local": "a.php"}], "after": [{"cmd": "exit 7", "optional": True}, "true"]})
    c2.deploy("fetch")
    code, out = c2.go()
    c2.check("optional hook failing: DEPLOYED, exit 0", code == 0 and status_of(out) == "DEPLOYED", (code, status_of(out), out[-300:]))
    hooks2 = (c2.state().get("after") or {}).get("apply") or []
    c2.check("both hooks recorded with code and optional flag", [(h.get("code"), h.get("optional")) for h in hooks2] == [(7, True), (0, False)], hooks2)
    rep2 = open(os.path.join(c2.run, "deploy", "REPORT.md"), encoding="utf-8").read()
    c2.check("REPORT marks the optional failure as a warning, not as manual recovery", "optional" in rep2 and "manual recovery" not in rep2, rep2[-600:])
    c2.check("plan lists the hooks with their flags", "optional" in c2.deploy("plan")[1], c2.deploy("plan")[1][-400:])
    # the profile's hook (CLAUDE.md) is required too; canary down -> automatic rollback whose recovery hook fails
    c3 = Case("after_hooks_rb", files=("a.php",))
    c3.profile(after=["exit 3"])
    c3.probes({CANARY: [ok, down, down, ok]})   # before, after, after2, after-rollback
    c3.deploy_json({"profile": "shop", "files": [{"local": "a.php"}]})
    c3.deploy("fetch")
    code, out = c3.go()
    st3 = c3.state()
    c3.check("canary down + failing recovery hook: ROLLED_BACK_AFTER_FAILED, exit 2", code == 2 and status_of(out) == "ROLLED_BACK_AFTER_FAILED", (code, status_of(out), out[-400:]))
    c3.check("prod back at OLD", c3.prod_text("a.php") == OLD["a.php"])
    c3.check("state: apply and rollback hooks both recorded", "apply" in (st3.get("after") or {}) and "rollback" in (st3.get("after") or {}), list((st3.get("after") or {}).keys()))
    rep3 = open(os.path.join(c3.run, "deploy", "REPORT.md"), encoding="utf-8").read()
    c3.check("REPORT lists the recovery hook for manual recovery", "manual recovery" in rep3 and "exit 3" in rep3, rep3[-900:])
    # watch --go: canary down later, rollback, recovery hook fails
    c4 = Case("after_hooks_watch", files=("a.php",))
    c4.probes({CANARY: [ok, ok]})
    c4.deploy("fetch")
    code, out = c4.go()
    c4.check("healthy apply: DEPLOYED", code == 0 and status_of(out) == "DEPLOYED", (code, status_of(out)))
    c4.profile(after=["exit 3"])
    c4.probes({CANARY: [down, down, ok]})   # watch, watch2, after-watch-rollback
    code, out = c4.deploy("watch", "--go")
    c4.check("watch --go rollback with a failing recovery hook: ROLLED_BACK_AFTER_FAILED, exit 2", code == 2 and status_of(out) == "ROLLED_BACK_AFTER_FAILED", (code, status_of(out), out[-400:]))
    c4.check("prod back at OLD after the watch rollback", c4.prod_text("a.php") == OLD["a.php"])
    return merge_subcases([c, c2, c3, c4])



def t_recovery_hooks():
    """V3-04: the `after` hooks run after EVERY rollback, the one after a failed put or a failed byte verify included
    (the bytes are back, the cache may still serve the half-deployed code): the status stays FAILED_ROLLED_BACK with
    the hooks recorded under `rollback`; a failed required recovery hook makes it FAILED_ROLLED_BACK_AFTER_FAILED;
    exit 2 either way; an optional hook failing is a warning."""
    c = Case("recovery_hooks")
    c.profile(after=["echo recovered > recovery_marker.txt"])
    c.behaviour({"put": [{"src_contains": "upload/b.php", "mode": "write_then_fail"}]})
    c.check("fetch OK", c.deploy("fetch")[0] == 0)
    code, out = c.go()
    marker = os.path.join(c.proj, "recovery_marker.txt")
    c.check("failed put: FAILED_ROLLED_BACK, exit 2", code == 2 and status_of(out) == "FAILED_ROLLED_BACK", (code, status_of(out), out[-400:]))
    c.check("both files back at OLD", all(c.prod_text(fn) == OLD[fn] for fn in ("a.php", "b.php")), (c.prod_text("a.php"), c.prod_text("b.php")))
    c.check("the recovery hook ran after the rollback (marker written in the deploy dir)", os.path.isfile(marker))
    st = c.state()
    hooks = (st.get("after") or {}).get("rollback") or []
    c.check("state records the hook under `rollback`, exit 0, required", [(h.get("code"), h.get("optional")) for h in hooks] == [(0, False)], st.get("after"))
    c.check("DEPLOY_JSON carries rollback ROLLED_BACK and an empty after_failed", '"rollback": "ROLLED_BACK"' in out and '"after_failed": []' in out, out[-400:])
    # a failed byte verify (the transport corrupts the upload) + a failing required recovery hook
    c2 = Case("recovery_hooks_fail")
    c2.profile(after=["exit 3"])
    c2.behaviour({"put": [{"src_contains": "upload/a.php", "mode": "corrupt_write"}]})
    c2.deploy("fetch")
    code, out = c2.go()
    c2.check("byte verify failed + failing recovery hook: FAILED_ROLLED_BACK_AFTER_FAILED, exit 2", code == 2 and status_of(out) == "FAILED_ROLLED_BACK_AFTER_FAILED", (code, status_of(out), out[-500:]))
    c2.check("prod back at OLD", c2.prod_text("a.php") == OLD["a.php"], repr(c2.prod_text("a.php")))
    c2.check("DEPLOY_JSON names the failed hook", '"after_failed": ["exit 3"]' in out, out[-400:])
    rep = open(os.path.join(c2.run, "deploy", "REPORT.md"), encoding="utf-8").read()
    c2.check("REPORT names the hook for manual recovery", "manual recovery" in rep and "exit 3" in rep, rep[-900:])
    # an optional recovery hook failing is a warning: FAILED_ROLLED_BACK
    c3 = Case("recovery_hooks_opt")
    c3.deploy_json({"profile": "shop", "files": [{"local": "a.php", "order": 1}, {"local": "b.php", "order": 2}], "after": [{"cmd": "exit 3", "optional": True}]})
    c3.behaviour({"put": [{"src_contains": "upload/b.php", "mode": "write_then_fail"}]})
    c3.deploy("fetch")
    code, out = c3.go()
    hooks3 = (c3.state().get("after") or {}).get("rollback") or []
    c3.check("optional recovery hook failing: FAILED_ROLLED_BACK, exit 2, the hook recorded as optional",
             code == 2 and status_of(out) == "FAILED_ROLLED_BACK" and [(h.get("code"), h.get("optional")) for h in hooks3] == [(3, True)], (code, status_of(out), hooks3))
    return merge_subcases([c, c2, c3])


def t_watch_degraded():
    """V3-03: `watch` judges the CURRENT health first. HEALTHY only when every canary answers now; a canary that is
    5xx / unreachable now but was already bad in the `before` snapshot (an `apply --force-canary`) is DEGRADED, exit
    2, without an automatic rollback (nothing proves the upload caused it; the orchestrator decides `rollback --go`);
    a canary that was fine before and is bad now still rolls back."""
    ok, down = {"code": 200, "body": "ok"}, {"code": 503, "body": "down"}
    c = Case("watch_degraded", files=("a.php",))
    c.probes({CANARY: down})
    c.check("fetch OK", c.deploy("fetch")[0] == 0)
    code, out = c.go("--force-canary")
    c.check("apply --force-canary with a 503 baseline: DEPLOYED (#THE_OPERATOR#'s explicit override)", code == 0 and status_of(out) == "DEPLOYED", (code, status_of(out), out[-300:]))
    code, out = c.deploy("watch", "--go")
    st = c.state()
    c.check("watch --go: DEGRADED, exit 2, never HEALTHY", code == 2 and status_of(out) == "DEGRADED", (code, status_of(out), out[-400:]))
    c.check("no automatic rollback: prod keeps the new file, no rollback record", c.prod_text("a.php") == NEW["a.php"] and not st.get("rollbacks"), st.get("rollbacks"))
    c.check("DEPLOY_JSON names the bad canary and that the baseline was bad too", '"broken"' in out and '"baseline_bad"' in out and CANARY in out, out[-500:])
    c.check("state: the watch canary answer (503) is recorded", st["canary"]["watch"][CANARY]["code"] == 503, st.get("canary"))
    rep = open(os.path.join(c.run, "deploy", "REPORT.md"), encoding="utf-8").read()
    c.check("REPORT says DEGRADED", "DEGRADED" in rep, rep[:300])
    c.probes({CANARY: ok})
    code, out = c.deploy("watch", "--go")
    c.check("the same bad baseline, prod answers now: HEALTHY, exit 0", code == 0 and status_of(out) == "HEALTHY", (code, status_of(out)))
    c2 = Case("watch_regressed", files=("a.php",))
    c2.probes({CANARY: [ok, ok, down, down, ok]})   # before, after, watch, watch2, after-watch-rollback
    c2.deploy("fetch")
    c2.go()
    code, out = c2.deploy("watch", "--go")
    c2.check("before OK, now 503: ROLLED_BACK, exit 2, prod back at OLD", code == 2 and status_of(out) == "ROLLED_BACK" and c2.prod_text("a.php") == OLD["a.php"], (code, status_of(out), out[-300:]))
    return merge_subcases([c, c2])


def main():
    keep = "--keep" in sys.argv
    names = [a for a in sys.argv[1:] if not a.startswith("--")]
    tests = {"canary_before": t_canary_before, "partial_write": t_partial_write, "rollback_fail": t_rollback_fail,
             "new_file_cleanup": t_new_file_cleanup, "refetch": t_refetch, "remote_changed": t_remote_changed,
             "candidate_changed": t_candidate_changed, "binary_unknown": t_binary_unknown, "approval": t_approval,
             "binary_guard": t_binary_guard, "precheck_all": t_precheck_all_rows, "effective_config": t_effective_config,
             "merge_error": t_merge_error, "backup_integrity": t_backup_integrity, "seal": t_seal,
             "after_hooks": t_after_hooks, "recovery_hooks": t_recovery_hooks, "watch_degraded": t_watch_degraded}
    failed = []
    for name, fn in tests.items():
        if names and name not in names:
            continue
        print(f"== {name}")
        try:
            c = fn()
        except Exception as e:  # noqa: BLE001
            print(f"  CRASH {e!r}")
            failed.append(f"{name}: crash")
            continue
        failed += [f"{name}: {f}" for f in c.failed]
        if not keep and not c.failed:
            rm_rf(c.dir)
    print("\n" + ("ALL OK" if not failed else "FAILED:\n  " + "\n  ".join(failed)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
