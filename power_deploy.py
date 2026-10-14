#!/usr/bin/env python3
"""POWER deploy: prod comparison, 3-way merge, ordered upload with byte verification, canary health check
with automatic rollback, marker verification. Dry-run by default; prod changes only with --go.

  plan     --run-dir R --project-dir P                  validate deploy.json against the profile; list the files
  fetch    --run-dir R --project-dir P [--new-attempt]  download the prod versions, classify, 3-way merge -> <run>/deploy/;
                                                        refuses after an apply --go (prod-before/ is the only backup);
                                                        --new-attempt archives the attempt to deploy/attempts/<n>/ first
                                                        and starts attempt n+1; prints the candidate token
                                                        `CANDIDATE: <attempt>:<hash>` (sha256 over every file's action,
                                                        candidate and backup hashes and deploy.json): the same bytes
                                                        give the same token, any change another one
  apply    --run-dir R --project-dir P [--go] --approved ATTEMPT:HASH [--claimed-by TOPIC] [--force-canary]
                                                        --go needs the token of the candidate that was reviewed and
                                                        got #THE_OPERATOR#'s yes: missing = NOT_APPROVED, another candidate or
                                                        attempt = APPROVAL_MISMATCH, both before any write; then stops
                                                        before any write on: an interrupted earlier apply, a
                                                        candidate or deploy.json edited since fetch
                                                        (CANDIDATE_CHANGED), prod changed since fetch
                                                        (REMOTE_CHANGED), a canary already down (PRECHECK_FAILED;
                                                        --force-canary = #THE_OPERATOR#'s explicit yes); then upload in order
                                                        (attempted flag before each put, byte verify after), after
                                                        hooks (a failed REQUIRED hook = AFTER_FAILED, exit 2, the
                                                        files stay, the report names the hook for manual recovery),
                                                        canary after (verified rollback on 5xx / fatal), marker
                                                        verification; the approval is recorded in the state
  watch    --run-dir R --project-dir P [--go] [--db-rollback]
                                                        canary check now (after a DB flag was switched by the
                                                        orchestrator); on 5xx / fatal: roll back the attempted files
                                                        (+ the db rollback commands with --db-rollback)
  rollback --run-dir R --project-dir P [--go]           put every attempted file back from <run>/deploy/prod-before/,
                                                        only a backup whose digest is the one recorded at fetch, the
                                                        restore verified against that digest:
                                                        ROLLED_BACK / ROLLBACK_INCOMPLETE / ROLLBACK_UNVERIFIED
  seal     --run-dir R --project-dir P                  after a hand resolution of a conflict / diverged-no-base /
                                                        merge-error row in <run>/deploy/upload/<file>: checks (no
                                                        markers, a file for every stop row), regenerates the diff,
                                                        hashes the resolution and prints a NEW candidate token (the
                                                        pre-resolution token is void); the harness `deploy` keeps a
                                                        sealed candidate instead of fetching again
  status   --run-dir R                                  what fetch / apply did (attempt, interrupted files, archives)

Exit 0 only for DEPLOYED, DRY_OK, HEALTHY and a verified manual rollback; every other status exits 2 and names the
problem in the DEPLOY_JSON line. `watch` is HEALTHY only when every canary answers now: a canary that is down now
and was fine before the upload rolls back; one that was already down before (--force-canary) is DEGRADED, exit 2,
no automatic rollback (V3-03). The rollback after a failed put / byte verify runs the after hooks too (V3-04). A rollback whose required recovery hook failed is ROLLED_BACK_AFTER_FAILED (the bytes
are back, the service recovery is not confirmed), never a clean ROLLED_BACK. The candidate token covers the file bytes, deploy.json AND the effective
configuration (profile: transport commands, remote root, after, canary, last; the upload order): a changed profile is
CANDIDATE_CHANGED on apply. Before the first write apply also stops on BACKUP_INVALID (a prod-before/ file no longer
has the fetched digest) and REMOTE_CHANGED for EVERY row (skip rows too; a planned-new file must be confirmed absent,
an unavailable `ls` is unknown, never absence). POWER_PROBE_MAP=<json> (tests) replaces the HTTP probes with canned
answers; POWER_GIT_MERGE (tests) replaces `git merge-file`.

deploy.json in the run directory (written by the final reviewer, step 3):
  {"profile": "shop",
   "files": [{"local": "includes/x.php", "order": 1, "note": "..."}],   local = project-relative, as in tasks.json
   "after": ["<command>" | {"cmd": "<command>", "optional": true}],
                                      cache etc. (default: the profile's); {urls} = the edge verify urls. A hook is
                                      REQUIRED unless it says optional: a failed required hook is AFTER_FAILED (apply)
                                      or ROLLED_BACK_AFTER_FAILED (recovery); a failed optional hook is a warning
   "canary": ["https://..."],         optional (default: the profile's); must not become 5xx / unreachable / fatal
                                      after the upload (a 3xx/4xx that was there before is not a regression)
   "verify": [{"url": "https://...", "contains": "marker", "via": "edge" | "origin"}],
   "db": [{"description": "...", "apply": "<command>", "rollback": "<command>"}],  never run by this script;
                                      apply = the orchestrator after an explicit yes; rollback = `watch --db-rollback`
   "needs_yes": ["..."], "risks": ["..."]}

The transport is a profile under "deploy" -> "profiles" in a `## POWER` json block (power_check.py config):
  {"local_root": "my-plugin", "remote_root": "/home/u/site/wp-content/plugins/my-plugin",
   "get": "... \"{remote}\" \"{local}\"", "put": "... \"{local}\" \"{remote_dir}\"", "rm": "... \"{remote}\"",
   "ls": "... \"{remote_dir}\"", "last": ["my-plugin.php"], "after": ["..."], "canary": ["https://..."],
   "origin_ip": "1.2.3.4", "pace": 3, "settle": 15, "fatal": ["Fatal error"]}
Commands run in Git Bash from the directory of the CLAUDE.md that defines the profile.
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from power_check import (PlanError, bash_exe, fwd, load_config, load_plan, now, out, read_bytes,  # noqa: E402
                         read_json, to_rel, write_json, write_text)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".pdf", ".zip",
              ".gz", ".mp4", ".webm", ".psd", ".psb"}
FATAL_DEFAULT = ["Fatal error", "Parse error", "There has been a critical error on this website",
                 "Uncaught Error", "Uncaught Exception"]
CONFLICT = re.compile(r"^(<{7}|={7}|>{7})", re.M)


# ---------- helpers ----------

def lf(data):
    return data.replace(b"\r\n", b"\n") if data is not None else None


def is_binary_file(path):
    if os.path.splitext(path)[1].lower() in BINARY_EXT:
        return True
    data = read_bytes(path)
    return data is not None and b"\0" in data[:8192]


def sh3(cmd, cwd, timeout=600):
    """(exit, stdout, stderr) of a Git Bash command; exit None on timeout / failure to start (the message in stderr).
    stdout is kept apart from the diagnostics so that a command's product (a merge) never mixes with its errors."""
    env = dict(os.environ, MSYS_NO_PATHCONV="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.run([bash_exe(), "-c", cmd], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        return p.returncode, (p.stdout or b"").decode("utf-8", "replace"), (p.stderr or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return None, "", f"timed out after {timeout} s"
    except OSError as e:
        return None, "", str(e)


def sh(cmd, cwd, timeout=600):
    """(exit, combined output) for commands whose output is only read by people (transport, after commands)."""
    code, so, se = sh3(cmd, cwd, timeout)
    return code, so + (("\n" if so and se and not so.endswith("\n") else "") + se if se else "")


# tests only: a stand-in for `git merge-file` (tests/mock_merge.py injects process failures)
MERGE_CMD = os.environ.get("POWER_GIT_MERGE") or "git merge-file"


def q(s):
    return '"' + str(s).replace("\\", "/").replace('"', '\\"') + '"'


def sha256_of(path):
    data = read_bytes(path)
    return hashlib.sha256(data).hexdigest() if data is not None else None


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def candidate_token(state):
    """`<attempt>:<12 hex>` of the fetched candidate: sha256 over every row's local path, remote target, order, prod
    state, action, candidate hash (upload/) and backup hash (prod-before/), plus the hash of deploy.json at fetch and
    the digest of the effective configuration (`effective_sha`: the resolved profile - transport commands, remote
    root, after, canary, verify, db, last, fatal, origin - V2-03). The same prod, local files, deploy.json and profile
    give the same token on every fetch; a changed prod (other merge), an edited candidate, an edited deploy.json, a
    changed profile, another order or a new attempt give another one. #THE_OPERATOR#'s yes is for exactly one token.
    None before a fetch (or for a state written by an older version)."""
    rows = state.get("files")
    if not rows or not state.get("fetched_at") or not state.get("spec_sha"):
        return None
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda x: str(x.get("local", "")).lower()):
        h.update("|".join(str(r.get(k) or "") for k in ("local", "remote", "order", "prod", "action", "candidate_sha", "prod_before_sha")).encode("utf-8") + b"\n")
    h.update(("spec:" + str(state["spec_sha"])).encode("utf-8"))
    h.update(("config:" + str(state.get("effective_sha") or "")).encode("utf-8"))
    return f"{int(state.get('attempt') or 1)}:{h.hexdigest()[:12]}"


def fake_probe(map_path, url):
    """Tests only: the answer for `url` from a JSON map {url: {"code", "body"} | [answers in order]}."""
    try:
        with open(map_path, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return 0, "probe map unreadable"
    ans = m.get(url, {"code": 0, "body": "not in the probe map"})
    if isinstance(ans, list):
        cpath = map_path + ".count.json"
        counts = read_json(cpath, {}) or {}
        i = int(counts.get(url, 0))
        counts[url] = i + 1
        write_json(cpath, counts)
        ans = ans[min(i, len(ans) - 1)] if ans else {"code": 0, "body": ""}
    return int(ans.get("code", 0)), str(ans.get("body", ""))


def is_bad(v):
    """A canary answer that means the site is down: 5xx, unreachable, or a fatal marker in the body."""
    return int(v["code"]) >= 500 or int(v["code"]) == 0 or bool(v["fatal"])


def rollback_status(results):
    """ROLLED_BACK (every file verified back), ROLLBACK_INCOMPLETE (a put/rm failed or a file has no backup),
    ROLLBACK_UNVERIFIED (everything was put back, but the transport could not confirm the bytes)."""
    if any(x["ok"] is False for x in results):
        return "ROLLBACK_INCOMPLETE"
    if any(x["ok"] is None for x in results):
        return "ROLLBACK_UNVERIFIED"
    return "ROLLED_BACK"


class Deploy:
    def __init__(self, run_dir, project_dir):
        self.run_dir = fwd(os.path.abspath(run_dir))
        self.project_dir = fwd(os.path.abspath(project_dir))
        self.dir = os.path.join(self.run_dir, "deploy")
        self.cfg = load_config(self.project_dir)
        self.spec = read_json(os.path.join(self.run_dir, "deploy.json"))
        if not isinstance(self.spec, dict):
            raise PlanError("deploy.json missing or invalid in the run directory (the final reviewer writes it)")
        self.profile_name = str(self.spec.get("profile", "")).strip()
        profiles = (self.cfg.get("deploy") or {}).get("profiles") or {}
        if self.profile_name not in profiles:
            raise PlanError(f"profile {self.profile_name!r} is not in the ## POWER deploy profiles "
                            f"(known: {', '.join(profiles) or 'none'})")
        self.p = profiles[self.profile_name]
        self.base_dir = self.cfg["_dirs"].get("deploy") or self.project_dir
        for key in ("local_root", "remote_root", "get", "put"):
            if not self.p.get(key):
                raise PlanError(f"profile {self.profile_name}: missing {key!r}")
        self.root_abs = fwd(os.path.abspath(os.path.join(self.base_dir, self.p["local_root"])))
        self.remote_root = str(self.p["remote_root"]).rstrip("/")
        self.pace = float(self.p.get("pace", 3))
        self.settle = float(self.p.get("settle", 15))
        self.fatal = list(self.p.get("fatal") or FATAL_DEFAULT)
        self.state_path = os.path.join(self.dir, "state.json")
        self.state = read_json(self.state_path, {}) or {}

    # ---- the file list ----
    def files(self):
        plan = load_plan(self.run_dir)
        run_files = {f["path"].lower() for t in plan["tasks"] for f in t["files"]}
        rows, seen, problems = [], set(), []
        last = {str(x).lower() for x in (self.p.get("last") or [])}
        for i, f in enumerate(self.spec.get("files") or []):
            if isinstance(f, str):
                f = {"local": f}
            local = to_rel(f.get("local", ""), self.project_dir)
            if local.lower() in seen:
                problems.append(f"{local}: listed twice")
                continue
            seen.add(local.lower())
            abs_local = os.path.join(self.project_dir, local)
            if not os.path.isfile(abs_local):
                problems.append(f"{local}: not a file in the project")
                continue
            try:
                rel = os.path.relpath(abs_local, self.root_abs).replace("\\", "/")
            except ValueError:
                rel = ".."
            if rel.startswith(".."):
                problems.append(f"{local}: outside the profile root {self.p['local_root']}")
                continue
            if local.lower() not in run_files:
                problems.append(f"{local}: not a file of this run (tasks.json)")
            basename = os.path.basename(rel)
            rows.append({"local": local, "abs_local": fwd(abs_local), "rel": rel, "basename": basename,
                         "remote": f"{self.remote_root}/{rel}", "remote_dir": f"{self.remote_root}/{os.path.dirname(rel)}".rstrip("/"),
                         "order": int(f.get("order") or (i + 1)), "last": basename.lower() in last,
                         "note": str(f.get("note") or ""), "binary": is_binary_file(abs_local)})
        rows.sort(key=lambda r: (r["last"], r["order"], r["rel"]))
        not_deployed = sorted(p for p in run_files if p not in seen)
        return rows, problems, not_deployed

    # ---- transport ----
    def cmd(self, key, **kw):
        tpl = self.p.get(key)
        if not tpl:
            raise PlanError(f"profile {self.profile_name}: no {key!r} command")
        return tpl.format(**{k: str(v).replace("\\", "/") for k, v in kw.items()})

    def remote_exists(self, row):
        if not self.p.get("ls"):
            return None  # unknown
        code, output = sh(self.cmd("ls", remote_dir=row["remote_dir"]), self.base_dir, 300)
        if code != 0:
            # A missing parent directory means the file is new (cPanel: "... does not exist for the account").
            if re.search(r"does not exist|no such file or directory", output, re.I):
                return False
            return None
        return re.search(r"(^|\s)" + re.escape(row["basename"]) + r"(\s|$)", output) is not None

    def get(self, row, dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(dest):
            os.remove(dest)
        code, output = sh(self.cmd("get", remote=row["remote"], local=dest), self.base_dir, 300)
        if code == 0 and os.path.isfile(dest):
            return True, output
        return False, output

    def put(self, row, src):
        # The transport uploads under the basename of `src`, so `src` must carry the right name.
        assert os.path.basename(src) == row["basename"], (src, row["basename"])
        code, output = sh(self.cmd("put", local=src, remote_dir=row["remote_dir"], remote=row["remote"]), self.base_dir, 600)
        return code == 0, output

    def rm(self, row):
        code, output = sh(self.cmd("rm", remote=row["remote"]), self.base_dir, 300)
        return code == 0, output

    # ---- http probes ----
    def probe(self, url, via="origin"):
        """(code, fatal_marker or '', note). Origin-direct when the profile has origin_ip and via='origin'.
        POWER_PROBE_MAP (tests only) = a JSON file {url: {"code", "body"} | [...]}: the answers come from it."""
        if os.environ.get("POWER_PROBE_MAP"):
            code, body = fake_probe(os.environ["POWER_PROBE_MAP"], url)
            return code, next((m for m in self.fatal if m in body), ""), body
        host = urllib.parse.urlparse(url).hostname or ""
        fd, tmp = tempfile.mkstemp(prefix="power-probe-")
        os.close(fd)
        cmd = ["curl", "-sk", "--max-time", "45", "-A", "pp-power-deploy/1", "-o", tmp, "-w", "%{http_code}"]
        if via == "origin" and self.p.get("origin_ip") and url.lower().startswith("https://"):
            cmd += ["--resolve", f"{host}:443:{self.p['origin_ip']}"]
        cmd.append(url)
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)
            code = int((r.stdout or b"0").decode("ascii", "replace").strip() or 0)
            body = (read_bytes(tmp) or b"").decode("utf-8", "replace")
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            code, body = 0, str(e)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        marker = next((m for m in self.fatal if m in body), "")
        return code, marker, body

    def canaries(self):
        return list(self.spec.get("canary") or self.p.get("canary") or [])

    def check_canaries(self, label):
        res = {}
        for i, url in enumerate(self.canaries()):
            if i:
                time.sleep(1)
            code, marker, _ = self.probe(url, "origin")
            res[url] = {"code": code, "fatal": marker, "at": now()}
            out(f"  canary {url}: {code}" + (f" FATAL ({marker})" if marker else ""))
        self.state.setdefault("canary", {})[label] = res
        return res

    @staticmethod
    def broken(before, after):
        """URLs that were fine before and are 5xx / unreachable / fatal now."""
        bad = []
        for url, a in after.items():
            b = before.get(url, {})
            was_ok = 200 <= int(b.get("code", 0)) < 500 and not b.get("fatal")
            is_bad = int(a["code"]) >= 500 or int(a["code"]) == 0 or bool(a["fatal"])
            if was_ok and is_bad:
                bad.append(url)
        return bad

    def verify_markers(self):
        rows = []
        for i, v in enumerate(self.spec.get("verify") or []):
            via = str(v.get("via") or "edge")
            if i and via == "edge":
                time.sleep(self.pace)
            code, _, body = self.probe(v["url"], via)
            ok = code == int(v.get("expect", 200)) and (not v.get("contains") or str(v["contains"]) in body)
            rows.append({"url": v["url"], "via": via, "code": code, "contains": v.get("contains", ""), "ok": ok})
            out(f"  verify {v['url']} ({via}): {code} " + ("OK" if ok else "FAIL" + (" - marker missing" if v.get("contains") and str(v["contains"]) not in body else "")))
        self.state["verify"] = rows
        return rows

    def hooks(self):
        """The effective `after` hooks as (command template, optional): deploy.json's, else the profile's. A hook is a
        string (required) or {"cmd": ..., "optional": true} (V2-12: required unless it says so)."""
        rows = []
        for h in list(self.spec.get("after") or self.p.get("after") or []):
            if isinstance(h, dict):
                cmd = str(h.get("cmd") or "").strip()
                if cmd:
                    rows.append((cmd, bool(h.get("optional"))))
            elif str(h).strip():
                rows.append((str(h), False))
        return rows

    def after_commands(self, label):
        """Run the hooks; every result records cmd, exit, tail and whether it was optional. A failed REQUIRED hook
        is in `failed_hooks()` of the result: the caller turns it into AFTER_FAILED / *_AFTER_FAILED (nothing is
        undone automatically - a hook's compensation is not known - the report names it for manual recovery)."""
        urls = " ".join(q(v["url"]) for v in (self.spec.get("verify") or []) if str(v.get("via") or "edge") == "edge")
        results = []
        for tpl, optional in self.hooks():
            # {urls} may be empty: the command still runs (the opcache reset lives in the same command).
            cmd = tpl.replace("{urls}", urls).rstrip()
            code, output = sh(cmd, self.base_dir, 600)
            results.append({"cmd": cmd, "code": code, "optional": optional, "tail": output.strip()[-400:]})
            out(f"  after{' (optional)' if optional else ''}: {cmd}\n    -> exit {code}"
                + ("" if code == 0 else (": " + ("WARNING" if optional else "REQUIRED HOOK FAILED") + f" {output.strip()[-300:]}")))
        self.state.setdefault("after", {})[label] = results
        return results

    @staticmethod
    def failed_hooks(results):
        """The commands of the required hooks that failed (exit != 0)."""
        return [x["cmd"] for x in results or [] if x.get("code") != 0 and not x.get("optional")]

    @staticmethod
    def with_hooks(status, results):
        """A clean rollback status plus a failed required recovery hook is not a clean rollback."""
        return status + "_AFTER_FAILED" if status == "ROLLED_BACK" and Deploy.failed_hooks(results) else status

    def save(self):
        os.makedirs(self.dir, exist_ok=True)
        self.state["saved_at"] = now()
        write_json(self.state_path, self.state)

    # ---- attempts ----
    def mutated(self):
        """True once an apply --go has started writing (an applied record, or a file marked attempted)."""
        if (self.state.get("applied") or {}).get("go"):
            return True
        return any(r.get("attempted") for r in self.state.get("files") or [])

    def archive_attempt(self):
        n = int(self.state.get("attempt") or 1)
        dest = os.path.join(self.dir, "attempts", f"{n:02d}")
        os.makedirs(dest, exist_ok=True)
        # the archive index is read BEFORE state.json moves away (V2 2.4: earlier entries were lost otherwise)
        archived = list((read_json(self.state_path, {}) or {}).get("archived") or [])
        for name in ("prod-before", "upload", "merged", "state.json", "REPORT.md"):
            src = os.path.join(self.dir, name)
            if os.path.exists(src):
                shutil.move(src, os.path.join(dest, name))
        out(f"  attempt {n} archived in {fwd(dest)}; this fetch starts attempt {n + 1}")
        self.state = {"attempt": n + 1, "archived": archived + [fwd(dest)]}

    def interrupted_rows(self):
        """Files an earlier apply started writing without finishing (attempted, not uploaded/verified)."""
        return [r for r in self.state.get("files") or [] if r.get("attempted") and not r.get("verified") and not r.get("rolled_back")]

    def remote_changed(self, rows):
        """Every row the candidate relies on (upload, skip, resolved - not only the ones to upload, V2-02), against
        what the fetch saw: a fetched file must still have the recorded digest (a re-download that fails = unknown =
        stop); a file planned as new (text or binary) must be positively confirmed absent (`ls` unavailable = unknown
        = stop, never absence); a binary that existed must still exist. Unknown is never treated as fine."""
        changed = []
        for r in rows:
            if r.get("prod_before"):
                fd, tmp = tempfile.mkstemp(prefix="power-recheck-", suffix="-" + r["basename"])
                os.close(fd)
                ok, output = self.get(r, tmp)
                cur = read_bytes(tmp) if ok else None
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                if not ok or cur is None:
                    changed.append(f"{r['local']}: prod could not be re-read, so its state is unknown ({output.strip()[-120:]})")
                elif (sha256_bytes(cur) != r["prod_before_sha"]) if r.get("prod_before_sha") else (lf(cur) != lf(read_bytes(r["prod_before"]))):
                    changed.append(f"{r['local']}: prod differs from the fetched version" + (" (a skip row: prod no longer has the version the candidate counted on)" if r.get("action") == "skip" else ""))
            elif r.get("prod") in ("new", "binary-new"):
                exists = self.remote_exists(r)
                if exists is None:
                    changed.append(f"{r['local']}: planned as new, but whether it exists on prod now cannot be confirmed (ls unavailable) - unknown is not absence")
                elif exists:
                    changed.append(f"{r['local']}: planned as new, but a file is on prod now (another session's? there is no backup of it here)")
            elif r.get("prod") == "binary-existing":
                exists = self.remote_exists(r)
                if exists is None:
                    changed.append(f"{r['local']}: existed on prod at fetch, cannot confirm it now (ls unavailable)")
                elif not exists:
                    changed.append(f"{r['local']}: existed on prod at fetch, gone now")
            # binary-unknown: accepted explicitly with allow_unrestorable; nothing to compare
        return changed

    def verify_remote(self, row, expected_path, expected_sha=None):
        """True when prod has exactly the expected bytes (the digest `expected_sha` when given - a restore is checked
        against the digest recorded at fetch, not against the local backup file - else the bytes of expected_path),
        False when it differs, None when the re-download failed."""
        if row["binary"]:
            return None
        fd, tmp = tempfile.mkstemp(prefix="power-verify-", suffix="-" + row["basename"])
        os.close(fd)
        ok, _ = self.get(row, tmp)
        got = read_bytes(tmp) if ok else None
        try:
            os.remove(tmp)
        except OSError:
            pass
        if not ok or got is None:
            return None
        return sha256_bytes(got) == expected_sha if expected_sha else got == read_bytes(expected_path)

    def spec_sha(self):
        return sha256_of(os.path.join(self.run_dir, "deploy.json"))

    def effective_sha(self, rows):
        """Digest of the configuration apply would act on (V2-03): the resolved profile (transport command templates,
        remote root, base directory, last / fatal / origin rules), the effective after / canary / verify / db
        settings (deploy.json or the profile's defaults), allow_unrestorable, and the ordered targets of the rows.
        Only the digest is stored or printed, never the templates (they may carry credentials)."""
        p = self.p
        cfg = {"profile": self.profile_name, "base_dir": self.base_dir, "local_root": self.root_abs, "remote_root": self.remote_root,
               "transport": {k: p.get(k) for k in ("get", "put", "rm", "ls")},
               "last": sorted(str(x).lower() for x in (p.get("last") or [])),
               "after": list(self.spec.get("after") or p.get("after") or []), "canary": self.canaries(),
               "verify": self.spec.get("verify") or [], "db": self.spec.get("db") or [],
               "origin_ip": p.get("origin_ip"), "fatal": self.fatal, "allow_unrestorable": bool(self.spec.get("allow_unrestorable")),
               "rows": [[r.get("local"), r.get("rel"), r.get("remote"), r.get("order"), bool(r.get("last"))] for r in rows]}
        return hashlib.sha256(json.dumps(cfg, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()

    def candidate_changed(self, rows):
        """What makes the reviewed candidate another one: an upload/ file that no longer matches the hash recorded at
        fetch or seal (every row, a hand-resolved conflict included - the supported path is `seal`, V2-11), a
        deploy.json edited since the fetch (its after / canary / verify / db steps are part of the candidate), or an
        effective configuration (profile in CLAUDE.md: transport, remote root, after, canary, last, order) that
        differs from the one the fetch sealed (V2-03) - checked here, on every apply, not only by a new fetch."""
        changed = []
        if self.state.get("spec_sha") and self.spec_sha() != self.state["spec_sha"]:
            changed.append("deploy.json was edited after the fetch (its commands and checks are part of the reviewed candidate): run fetch again")
        if self.state.get("effective_sha") and self.effective_sha(self.state.get("files") or rows) != self.state["effective_sha"]:
            changed.append("the effective deploy configuration changed since the fetch (the profile in CLAUDE.md: after / canary / transport "
                           "commands / remote root / last rules, or the upload order): the yes was for the configuration that was reviewed - run fetch again")
        for r in rows:
            if r.get("upload_from") and sha256_of(r["upload_from"]) != r.get("candidate_sha"):
                changed.append(f"{r['local']}: {r['upload_from']} was edited after the fetch" + (" / seal" if r.get("resolved") else "") +
                               " (the reviewed candidate is gone" + ("" if r.get("resolved") else "; a hand resolution goes through `seal`") + ")")
        return changed

    def backup_invalid(self, rows):
        """Backups (prod-before/) whose bytes no longer match the digest recorded at fetch, or that are missing
        (V2-09): without them there is no rollback, so apply stops before the first write."""
        problems = []
        for r in rows:
            if not r.get("prod_before"):
                continue
            data = read_bytes(r["prod_before"])
            if data is None:
                problems.append(f"{r['local']}: the backup {r['prod_before']} is missing")
            elif r.get("prod_before_sha") and sha256_bytes(data) != r["prod_before_sha"]:
                problems.append(f"{r['local']}: the backup digest differs from the one recorded at fetch (sha256 {sha256_bytes(data)[:12]}, "
                                f"recorded {r['prod_before_sha'][:12]}) - prod-before/ was altered")
        return problems

    # ---- CLAIM ----
    def check_claims(self, rows, claimed_by):
        rel_path = self.cfg.get("claim")
        if not rel_path:
            return []
        path = os.path.join(self.cfg["_dirs"].get("claim") or self.base_dir, rel_path)
        if not os.path.isfile(path):
            return []
        hits = []
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip().startswith("CLAIM "):
                    continue
                parts = [x.strip() for x in line.split("|")]
                topic = parts[1] if len(parts) > 1 else "?"
                claimed = parts[2] if len(parts) > 2 else ""
                if claimed_by and claimed_by == topic:
                    continue
                for r in rows:
                    if r["basename"] in claimed or r["rel"] in claimed or r["local"] in claimed:
                        hits.append(f"{r['local']} is in another window's CLAIM: {topic}")
        return hits


# ---------- commands ----------

def cmd_plan(a):
    d = Deploy(a.run_dir, a.project_dir)
    rows, problems, not_deployed = d.files()
    out(f"profile {d.profile_name}: {d.root_abs} -> {d.remote_root} (commands run from {d.base_dir})")
    for r in rows:
        out(f"  {r['order']:>2}. {r['local']} -> {r['remote']}" + (" [binary]" if r["binary"] else "") + (" [last]" if r["last"] else "") + (f"  {r['note']}" if r["note"] else ""))
    for u in d.canaries():
        out(f"  canary: {u}")
    for v in d.spec.get("verify") or []:
        out(f"  verify: {v.get('url')} contains {v.get('contains', '')!r} via {v.get('via', 'edge')}")
    for c, optional in d.hooks():
        out(f"  after{' (optional: a failure is a warning)' if optional else ' (required: a failure is AFTER_FAILED)'}: {c}")
    for s in d.spec.get("db") or []:
        out(f"  DB (explicit yes, run by the orchestrator): {s.get('description')}  apply: {s.get('apply')}  rollback: {s.get('rollback')}")
    for y in d.spec.get("needs_yes") or []:
        out(f"  needs yes: {y}")
    for p in not_deployed:
        out(f"  not deployed (run file left out of deploy.json): {p}")
    for p in problems:
        out("ERROR " + p)
    if not rows and not problems:
        out("ERROR deploy.json lists no files")
        problems.append("no files")
    out("PLAN: " + ("FAIL" if problems else f"OK ({len(rows)} files)"))
    if problems:
        sys.exit(1)


def cmd_fetch(a):
    d = Deploy(a.run_dir, a.project_dir)
    rows, problems, _ = d.files()
    if problems:
        raise PlanError("fix deploy.json first (run plan)")
    manifest = read_json(os.path.join(d.run_dir, "before", "manifest.json"), {}) or {}
    # Once an apply has touched prod, prod-before/ is the only backup: a new fetch never overwrites it. The
    # finished (or interrupted) attempt is archived under attempts/<n>/ only on an explicit --new-attempt.
    if d.mutated():
        if not getattr(a, "new_attempt", False):
            raise PlanError(f"apply already ran with --go (status {d.state.get('status')}); the backup in {d.dir}/prod-before "
                            f"is kept. Use `rollback`, `watch` or `status`; `fetch --new-attempt` archives this attempt first")
        d.archive_attempt()
    before_dir = os.path.join(d.dir, "prod-before")
    upload_dir = os.path.join(d.dir, "upload")
    merged_dir = os.path.join(d.dir, "merged")
    for p in (before_dir, upload_dir, merged_dir):
        if os.path.isdir(p):
            shutil.rmtree(p)
    conflicts, merged, blocked = [], [], []
    for r in rows:
        dest_before = os.path.join(before_dir, r["rel"])
        dest_up = os.path.join(upload_dir, r["rel"])
        os.makedirs(os.path.dirname(dest_up), exist_ok=True)
        r["prod_before"] = None
        if r["binary"]:
            exists = d.remote_exists(r)
            r["prod"] = "binary-existing" if exists else ("binary-new" if exists is False else "binary-unknown")
            if exists:
                blocked.append(f"{r['local']}: binary file exists on prod and this transport cannot back it up (no rollback); "
                               f"set \"allow_unrestorable\": true in deploy.json to accept")
            elif exists is None:
                blocked.append(f"{r['local']}: binary-unknown - the transport cannot tell whether it exists on prod, so an "
                               f"upload might overwrite a file with no backup; set \"allow_unrestorable\": true to accept")
            shutil.copy2(r["abs_local"], dest_up)
            r["upload_from"] = fwd(dest_up)
            r["action"] = "upload"
            out(f"  {r['local']}: {r['prod']}")
            continue
        ok, output = d.get(r, dest_before)
        if not ok:
            exists = d.remote_exists(r)
            if exists is None or exists:
                raise PlanError(f"{r['local']}: could not download the prod version and cannot tell whether it exists: {output.strip()[-300:]}")
            r["prod"] = "new"
            shutil.copy2(r["abs_local"], dest_up)
            r["upload_from"], r["action"] = fwd(dest_up), "upload"
            out(f"  {r['local']}: new on prod")
            continue
        r["prod_before"] = fwd(dest_before)
        prod = read_bytes(dest_before)
        local = read_bytes(r["abs_local"])
        entry = (manifest.get("files") or {}).get(r["local"])
        snap_path = os.path.join(d.run_dir, "before", "files", r["local"])
        snap = read_bytes(snap_path) if entry and entry.get("exists") else None
        if lf(prod) == lf(local):
            r["prod"], r["action"] = "same", "skip"
            out(f"  {r['local']}: prod already has this version - skip")
            continue
        if snap is not None and lf(prod) == lf(snap):
            r["prod"], r["action"] = "clean", "upload"
            shutil.copy2(r["abs_local"], dest_up)
            r["upload_from"] = fwd(dest_up)
            out(f"  {r['local']}: clean (prod == snapshot) - upload the local version")
            continue
        if snap is None:
            r["prod"], r["action"] = "diverged-no-base", "stop"
            conflicts.append(f"{r['local']}: prod has a version, the snapshot has none (planned as create) - merge by hand")
            out(f"  {r['local']}: DIVERGED without a base")
            continue
        # 3-way merge: base = snapshot, ours = local, theirs = prod (all LF; git merge-file is conservative:
        # adjacent changes are a conflict). stdout is the product, stderr the diagnostics: they never mix (V2-08).
        tmp = tempfile.mkdtemp(prefix="power-merge-")
        paths = {}
        for name, data in (("base", snap), ("ours", local), ("theirs", prod)):
            paths[name] = os.path.join(tmp, name)
            with open(paths[name], "wb") as f:
                f.write(lf(data))
        code, merged_text, err = sh3(f"{MERGE_CMD} -p -L local -L snapshot -L prod {q(paths['ours'])} {q(paths['base'])} {q(paths['theirs'])}", d.base_dir, 120)
        shutil.rmtree(tmp, ignore_errors=True)
        markers = CONFLICT.search(merged_text) is not None
        # git merge-file: 0 = clean, 1..127 = that many conflicts (markers in the output), anything else = the
        # process failed (128+, negative, 127 "command not found", a timeout). Only "0 and no markers" is a merge;
        # conflicts need exit 1..127 AND markers; everything else is an infrastructure failure whose output is a
        # diagnostic, never a candidate file.
        if code == 0 and not markers:
            with open(dest_up, "w", encoding="utf-8", newline="\n") as f:
                f.write(merged_text)
            r["upload_from"] = fwd(dest_up)
            r["merged_diff"] = write_merged_diff(merged_dir, r, prod, merged_text)
            r["prod"], r["action"] = "merged", "upload"
            merged.append(r["local"])
            out(f"  {r['local']}: diverged - merged (prod + our hunks), review {r['merged_diff']}")
        elif code is not None and 0 < code < 128 and markers:
            with open(dest_up, "w", encoding="utf-8", newline="\n") as f:
                f.write(merged_text)
            r["upload_from"] = fwd(dest_up)
            r["merged_diff"] = write_merged_diff(merged_dir, r, prod, merged_text)
            r["prod"], r["action"] = "conflict", "stop"
            conflicts.append(f"{r['local']}: merge conflict - resolve in {r['upload_from']} (markers <<<<<<<), then `seal` (a new candidate for review and yes)")
            out(f"  {r['local']}: CONFLICT ({code})")
        else:
            detail = (err.strip() or merged_text.strip() or "no output")[-300:]
            r["prod"], r["action"] = "merge-error", "stop"
            r["merge_error"] = {"exit": code, "detail": detail, "markers_with_exit_0": bool(markers and code == 0)}
            conflicts.append(f"{r['local']}: the merge process failed (exit {code if code is not None else 'none'}: {detail}) - nothing was written to "
                             f"upload/; fix the cause (git on PATH?) and fetch again, or write the resolution to {fwd(dest_up)} and `seal`")
            out(f"  {r['local']}: MERGE FAILED (exit {code if code is not None else 'none'}): {detail}")
    for r in rows:
        # the reviewed candidate: apply uploads exactly these bytes. Every row with a file is hashed, a conflict row
        # too (its hand resolution goes through `seal`, which re-hashes it and issues a new token - V2-11);
        # prod_before_sha is the digest every later re-check and rollback is verified against (V2-09)
        r["candidate_sha"] = sha256_of(r["upload_from"]) if r.get("upload_from") else None
        r["prod_before_sha"] = sha256_of(r["prod_before"]) if r.get("prod_before") else None
    # The attempt number changes only with `fetch --new-attempt` (after a prod write); a repeated fetch of the same
    # candidate keeps it, so the token below is stable while nothing changed.
    status = "FAIL" if conflicts or (blocked and not d.spec.get("allow_unrestorable")) else "OK"
    if d.state.get("status") == "SEALED":
        out("  note: the sealed hand resolution of the previous candidate was dropped by this fetch (upload/ rebuilt)")
    # V2-01: the outcome of the fetch is part of the state apply reads - a FAIL leaves no applicable candidate
    d.state.update({"profile": d.profile_name, "fetched_at": now(), "files": rows, "applied": None, "status": "FETCHED",
                    "attempt": int(d.state.get("attempt") or 1), "spec_sha": d.spec_sha(), "approval": None,
                    "effective_sha": d.effective_sha(rows), "fetch_status": status, "blocked": blocked, "conflicts": conflicts})
    d.state.pop("sealed", None)
    d.state["candidate"] = candidate_token(d.state)
    d.save()
    for b in blocked:
        out("BLOCKED " + b)
    for c in conflicts:
        out("CONFLICT " + c)
    if merged:
        out("MERGED (step 3 reviews these diffs before apply): " + ", ".join(merged))
    n_up = sum(1 for r in rows if r["action"] == "upload")
    out(f"CANDIDATE: {d.state['candidate']}",
        f"  (attempt {d.state['attempt']}; `apply --go --approved {d.state['candidate']}` uploads exactly this candidate after the review and #THE_OPERATOR#'s yes)")
    out(f"FETCH: {status} ({n_up} to upload, {len(merged)} merged, {len(conflicts)} conflicts, "
        f"{sum(1 for r in rows if r['action'] == 'skip')} already on prod)")
    if status == "FAIL":
        sys.exit(1)


def write_merged_diff(merged_dir, r, prod, candidate_text):
    """The diff prod -> candidate that step 3 (or #THE_OPERATOR#, after a hand resolution) reviews."""
    diff = "".join(difflib.unified_diff(lf(prod).decode("utf-8", "replace").splitlines(True),
                                        candidate_text.splitlines(True), "prod/" + r["rel"], "merged/" + r["rel"]))
    path = os.path.join(merged_dir, r["rel"] + ".diff")
    write_text(path, diff)
    return fwd(path)


def cmd_seal(a):
    """V2-11: the supported path for a hand resolution. For every row that fetch stopped (conflict, diverged-no-base,
    merge-error) the resolved file is deploy/upload/<rel>: seal checks it exists and has no markers, regenerates the
    prod -> candidate diff in deploy/merged/, hashes the resolution, turns the row into an upload and issues a NEW
    candidate token (the pre-resolution token is void; a later edit is CANDIDATE_CHANGED like any other row)."""
    d = Deploy(a.run_dir, a.project_dir)
    rows = d.state.get("files")
    if not rows or not d.state.get("fetched_at"):
        raise PlanError("run fetch first")
    if d.mutated():
        raise PlanError(f"apply already ran with --go (status {d.state.get('status')}); the backup in {d.dir}/prod-before is kept - "
                        "a new round is `fetch --new-attempt`, then resolve and seal again")
    if d.state.get("spec_sha") and d.spec_sha() != d.state["spec_sha"]:
        raise PlanError("deploy.json was edited after the fetch: run fetch again, then resolve and seal")
    if d.state.get("effective_sha") and d.effective_sha(rows) != d.state["effective_sha"]:
        raise PlanError("the deploy profile (CLAUDE.md) changed since the fetch: run fetch again, then resolve and seal")
    # rows fetch stopped, and rows sealed before (an edit after the seal is CANDIDATE_CHANGED; sealing again is the way on)
    pending = [r for r in rows if r.get("action") == "stop" or r.get("resolved")]
    if not pending:
        raise PlanError("nothing to seal: no conflict / diverged-no-base / merge-error row in this candidate")
    problems = []
    for r in pending:
        up = os.path.join(d.dir, "upload", r["rel"])
        data = read_bytes(up)
        if data is None:
            problems.append(f"{r['local']}: no resolved file at {fwd(up)} - write the resolution there (the prod version is in "
                            f"{r.get('prod_before') or 'prod (binary, no backup)'}, the local one in {r['abs_local']})")
        elif not r["binary"] and CONFLICT.search(data.decode("utf-8", "replace")):
            problems.append(f"{r['local']}: conflict markers remain in {fwd(up)}")
    if problems:
        for p in problems:
            out("STOP " + p)
        raise PlanError("seal refused: resolve the files above first")
    merged_dir = os.path.join(d.dir, "merged")
    for r in pending:
        up = os.path.join(d.dir, "upload", r["rel"])
        data = read_bytes(up)
        r["upload_from"], r["action"] = fwd(up), "upload"
        r["candidate_sha"] = sha256_bytes(data)
        r["resolved"] = {"at": now(), "from": r["prod"], "sha256": r["candidate_sha"], "times": int((r.get("resolved") or {}).get("times") or 0) + 1}
        if r.get("prod_before") and not r["binary"]:
            r["merged_diff"] = write_merged_diff(merged_dir, r, read_bytes(r["prod_before"]) or b"", data.decode("utf-8", "replace"))
        out(f"  {r['local']}: {r['prod']} resolved by hand -> upload ({r['candidate_sha'][:12]})" + (f", review {r['merged_diff']}" if r.get("merged_diff") else ""))
    blocked = d.state.get("blocked") or []
    status = "FAIL" if blocked and not d.spec.get("allow_unrestorable") else "OK"
    d.state.update({"status": "SEALED", "fetch_status": status, "conflicts": [], "applied": None, "approval": None,
                    "sealed": {"at": now(), "files": [r["local"] for r in pending]}})
    d.state["candidate"] = candidate_token(d.state)
    d.save()
    for b in blocked:
        out("BLOCKED " + b)
    out(f"CANDIDATE: {d.state['candidate']}",
        f"  (sealed: the hand resolution is a new candidate - review the diffs in {fwd(merged_dir)}, then #THE_OPERATOR#'s yes for this token; "
        f"`apply --go --approved {d.state['candidate']}`)")
    out(f"SEAL: {status} ({len(pending)} resolved by hand, {sum(1 for r in rows if r['action'] == 'upload')} to upload)")
    if status == "FAIL":
        sys.exit(1)


def rollback_files(d, rows, go):
    """Reverse order; existing files back from prod-before, new files removed. Every file an apply *attempted*
    counts (a put may write and then fail), and each restore is verified: ok True / False / None (unverified)."""
    results = []
    for r in reversed([x for x in rows if x.get("attempted") or x.get("uploaded")]):
        if r.get("prod_before"):
            desc = f"put back {r['remote']} from prod-before"
            backup = read_bytes(r["prod_before"])
            expect = r.get("prod_before_sha")
            # V2-09: the backup is restored only when its bytes still have the digest recorded at fetch, and the
            # restore is verified against that digest - a corrupted, truncated, replaced or missing backup is never
            # uploaded and the rollback is INCOMPLETE
            if backup is None:
                ok, output = False, f"backup missing ({r['prod_before']}): not restored"
            elif expect and sha256_bytes(backup) != expect:
                ok, output = False, (f"backup digest mismatch (sha256 {sha256_bytes(backup)[:12]}, recorded at fetch {expect[:12]}): "
                                     "the altered backup was not uploaded")
            elif not go:
                ok, output = True, "(dry)"
            else:
                ok, output = d.put(r, r["prod_before"])
                if ok:
                    ok = d.verify_remote(r, r["prod_before"], expected_sha=expect)
                    if ok is False:
                        output += " (put back, but the bytes on prod differ from the digest recorded at fetch)"
        elif r.get("prod") in ("new", "binary-new") and d.p.get("rm"):
            desc = f"remove {r['remote']} (was new)"
            if not go:
                ok, output = True, "(dry)"
            else:
                ok, output = d.rm(r)
                if ok:
                    exists = d.remote_exists(r)
                    ok = None if exists is None else (not exists)
        else:
            desc = f"{r['remote']}: no backup, cannot roll back"
            ok, output = False, ""
        results.append({"file": r["local"], "ok": ok, "what": desc, "tail": output.strip()[-200:]})
        out(f"  rollback: {desc} -> " + {True: "OK", False: "FAILED", None: "UNVERIFIED"}[ok] + ("" if ok else f": {output.strip()[-200:]}"))
        if go and ok:
            r["rolled_back"] = True
    d.state.setdefault("rollbacks", []).append({"at": now(), "go": go, "results": results, "status": rollback_status(results)})
    return rollback_status(results)


OK_STATUSES = ("DEPLOYED", "DRY_OK", "HEALTHY", "WOULD_ROLL_BACK")


def failed_upload(d, rows, r):
    """A put that failed or a byte verify that failed: roll back every attempted file, then run the `after` hooks
    (V3-04: the bytes are back, but the cache / opcache may still hold the half-deployed code - the same recovery as
    after a canary rollback), FAILED_<rollback status>, with _AFTER_FAILED when a required hook failed."""
    rb = rollback_files(d, rows, True)
    out("== after (recovery)")
    rb_hooks = d.after_commands("rollback")
    finish(d, "FAILED_" + Deploy.with_hooks(rb, rb_hooks), True,
           {"failed": r["local"], "rollback": rb, "after_failed": Deploy.failed_hooks(rb_hooks)})


def finish(d, status, go, extra=None, phase="applied", exit_ok=None):
    # Only apply writes `applied` (the "already applied with --go" guard); watch/rollback keep their own record.
    # Exit 0 only for the statuses that mean "prod is as intended" (or a manual rollback that verified, exit_ok=True).
    d.state["status"] = status
    d.state[phase] = {"at": now(), "go": go, "status": status}
    d.save()
    report(d)
    data = {"status": status, "go": go, "report": fwd(os.path.join(d.dir, "REPORT.md"))}
    data.update(extra or {})
    out(f"DEPLOY: {status}", "DEPLOY_JSON: " + json.dumps(data, ensure_ascii=False))
    if not (status in OK_STATUSES if exit_ok is None else exit_ok):
        sys.exit(2)


def stop_before_upload(d, status, problems, go):
    """A stop before any mutation: no `applied` record, so fetch/apply can be repeated after the cause is fixed."""
    for p in problems:
        out(f"STOP {p}")
    finish(d, status, go, {"problems": problems}, phase="precheck")


def cmd_apply(a):
    d = Deploy(a.run_dir, a.project_dir)
    rows = d.state.get("files")
    if not rows or not d.state.get("fetched_at"):
        raise PlanError("run fetch first")
    if d.state.get("applied") and d.state["applied"].get("go"):
        raise PlanError("already applied with --go; use watch, rollback or a new run directory")
    hits = d.check_claims(rows, a.claimed_by)
    if hits:
        for h in hits:
            out("CLAIM " + h)
        raise PlanError("files in another window's active CLAIM (override with --claimed-by <topic> only if it is yours)")
    todo = [r for r in rows if r.get("action") == "upload"]
    stop = [r for r in rows if r.get("action") == "stop"]
    for r in todo:
        data = read_bytes(r["upload_from"]) or b""
        if r.get("prod") in ("merged", "conflict") and CONFLICT.search(data.decode("utf-8", "replace")):
            stop.append(r)
    if stop:
        for r in stop:
            out(f"STOP {r['local']}: {r.get('prod')} - resolve in {fwd(os.path.join(d.dir, 'upload', r['rel']))}, then `seal` "
                "(the resolution is a new candidate: review, then #THE_OPERATOR#'s yes for its token)")
        raise PlanError("unresolved merges: a hand resolution is applied only after `power_deploy.py seal`")
    if not todo:
        raise PlanError("nothing to upload (everything is already on prod)")
    if d.interrupted_rows():
        raise PlanError("an earlier apply --go was interrupted while writing " +
                        ", ".join(r["local"] for r in d.interrupted_rows()) + ": run `rollback --go` (or `status`) first")
    # V2-01: the unrestorable-binary policy is enforced here, right before the first write, whatever fetch printed
    # and whoever calls apply: a binary that exists on prod (or whose existence is unknown) has no backup and no
    # rollback, so it is uploaded only when deploy.json says allow_unrestorable (and that spec is the reviewed
    # candidate: a later edit is CANDIDATE_CHANGED). A fetch that ended FAIL left no applicable candidate at all.
    allow = bool(d.spec.get("allow_unrestorable"))
    unrestorable = [r for r in todo if r.get("prod") in ("binary-existing", "binary-unknown")] if not allow else []
    if unrestorable or d.state.get("fetch_status") == "FAIL":
        problems = [f"{r['local']}: {r['prod']} - the transport cannot back it up, so there is no rollback; "
                    "accepting that is \"allow_unrestorable\": true in deploy.json, which is a new candidate (fetch again, review, yes)"
                    for r in unrestorable]
        problems += [b for b in (d.state.get("blocked") or []) if not any(b.startswith(r["local"] + ":") for r in unrestorable)]
        problems += [c for c in (d.state.get("conflicts") or [])]
        if d.state.get("fetch_status") == "FAIL":
            problems.append("the last fetch ended FAIL: its candidate token is not an applicable candidate - fix the cause and run fetch again")
        stop_before_upload(d, "UNRESTORABLE_BINARY" if unrestorable else "FETCH_FAILED", problems, a.go)
        return
    tok = d.state.get("candidate")
    out(f"== {'APPLY' if a.go else 'DRY-RUN'} {d.profile_name}: {len(todo)} files (attempt {d.state.get('attempt', 1)}, candidate {tok})")
    # 0. the yes is for one candidate: --go needs the token of this fetch (P1-03 stage 2). A dry run checks a
    # given token too, so a stale yes shows up before anyone types --go.
    if a.go or a.approved:
        if not tok:
            stop_before_upload(d, "NOT_APPROVED", ["this deploy state has no candidate token (fetched by an older version): run fetch again"], a.go)
            return
        if not a.approved:
            stop_before_upload(d, "NOT_APPROVED", [f"apply --go needs --approved {tok}: the token of the candidate that was reviewed and got #THE_OPERATOR#'s yes (fetch printed it as CANDIDATE)"], a.go)
            return
        if a.approved.strip() != tok:
            stop_before_upload(d, "APPROVAL_MISMATCH", [f"--approved {a.approved} is for another candidate or attempt; this fetch is {tok} (attempt {d.state.get('attempt', 1)}): "
                                                        "review the current candidate and get the yes for it, then apply --approved with this token"], a.go)
            return
    # 1. the candidate (files, deploy.json, effective configuration) is what was reviewed, 2. the backups still have
    # the fetched digests, 3. prod is what was fetched - for every row, not only the ones to upload: all before the
    # first write
    problems = d.candidate_changed(todo)
    if problems:
        stop_before_upload(d, "CANDIDATE_CHANGED", problems, a.go)
        return
    problems = d.backup_invalid(rows)
    if problems:
        stop_before_upload(d, "BACKUP_INVALID", problems + ["prod-before/ is the only rollback: run fetch again to take a fresh backup"], a.go)
        return
    out("== prod re-check (every row of the candidate)")
    problems = d.remote_changed(rows)
    if problems:
        stop_before_upload(d, "REMOTE_CHANGED", problems + ["run fetch again (or `fetch --new-attempt`) to merge against the current prod"], a.go)
        return
    out("  prod still matches the fetched state")
    # 3. canaries: a site that is already down is a stop (SKILL section 8), not a warning - its failures could not
    # be told from ours. --force-canary (#THE_OPERATOR#'s explicit yes) records the override and goes on; those URLs then
    # cannot trigger a rollback, because `broken` compares with the state before.
    out("== canary before")
    before = d.check_canaries("before")
    already_bad = [u for u, v in before.items() if is_bad(v)]
    if already_bad and not a.force_canary:
        stop_before_upload(d, "PRECHECK_FAILED", [f"canary down before the upload: {u} ({before[u]['code']}{' FATAL' if before[u]['fatal'] else ''})"
                                                   for u in already_bad] + ["fix prod first, or `apply --go --force-canary` after #THE_OPERATOR#'s explicit yes"], a.go)
        return
    if already_bad:
        out("  WARN canaries failing BEFORE the upload, override by --force-canary (they cannot trigger a rollback): " + ", ".join(already_bad))
        d.state["canary_override"] = {"at": now(), "urls": already_bad}
    for r in todo:
        out(f"  {r['order']:>2}. {r['local']} ({r['prod']}) -> {r['remote']}")
    if not a.go:
        finish(d, "DRY_OK", False, phase="precheck")
        return
    d.state["approval"] = {"token": tok, "at": now(), "force_canary": bool(a.force_canary), "claimed_by": a.claimed_by or None}
    d.state["started_at"] = now()
    d.state["status"] = "APPLYING"
    d.save()
    out("== upload")
    for r in todo:
        # the attempt is on disk before the put: a put that writes and then fails (or a crash) is rolled back too
        r["attempted"] = True
        r["uploaded_sha"] = sha256_of(r["upload_from"])
        d.save()
        ok, output = d.put(r, r["upload_from"])
        if not ok:
            out(f"  {r['local']}: UPLOAD FAILED: {output.strip()[-300:]}")
            failed_upload(d, rows, r)
            return
        r["uploaded"] = True
        d.save()
        if r["binary"]:
            out(f"  {r['local']}: uploaded (binary, not byte-verified)")
            continue
        if d.verify_remote(r, r["upload_from"]) is not True:
            out(f"  {r['local']}: BYTE VERIFY FAILED (prod differs from what was sent, or the re-download failed)")
            failed_upload(d, rows, r)
            return
        r["verified"] = True
        out(f"  {r['local']}: uploaded, byte-verified")
    out("== after")
    hook_fail = Deploy.failed_hooks(d.after_commands("apply"))
    if hook_fail:
        out("!! required after hook(s) failed: " + " | ".join(hook_fail) + " - the files are on prod, the hooks need manual recovery (or a rollback)")
    out(f"== canary after ({d.settle:g} s settle)")
    time.sleep(d.settle)
    after = d.check_canaries("after")
    bad = Deploy.broken(before, after)
    if bad:
        out("  re-check in 10 s")
        time.sleep(10)
        after = d.check_canaries("after2")
        bad = Deploy.broken(before, after)
    if bad:
        out("!! canaries broken by the upload: " + ", ".join(bad) + " - rolling back")
        rb = rollback_files(d, rows, True)
        rb_hooks = d.after_commands("rollback")
        time.sleep(min(d.settle, 10))
        recovered = not Deploy.broken(before, d.check_canaries("after-rollback"))
        finish(d, Deploy.with_hooks(rb, rb_hooks), True, {"broken": bad, "rollback": rb, "recovered": recovered,
                                                          "after_failed": hook_fail + Deploy.failed_hooks(rb_hooks)})
        return
    out("== verify markers")
    rows_v = d.verify_markers()
    failed = [v["url"] for v in rows_v if not v["ok"]]
    if hook_fail:
        # V2-12: a required hook that failed is not a deploy, whatever the canaries say (the cache may serve the old
        # code); nothing is undone automatically - the hook's effect is unknown - the report names it
        finish(d, "AFTER_FAILED", True, {"after_failed": hook_fail, "unverified": failed})
        return
    finish(d, "DEPLOYED" if not failed else "DEPLOYED_UNVERIFIED", True, {"unverified": failed})


def cmd_watch(a):
    """HEALTHY only when every canary answers NOW (V3-03). A canary that is 5xx / unreachable / fatal now and was fine
    in the `before` snapshot is a regression: automatic rollback (+ recovery hooks). One that was already bad before
    the upload (an `apply --force-canary`) is DEGRADED, exit 2, without a rollback: nothing proves the upload caused
    it, the orchestrator decides (`rollback --go`, or fix prod and `watch --go` again)."""
    d = Deploy(a.run_dir, a.project_dir)
    rows = d.state.get("files") or []
    before = (d.state.get("canary") or {}).get("before") or {}
    out("== canary now")
    cur = d.check_canaries("watch")
    down = [u for u, v in cur.items() if is_bad(v)]
    if down:
        out("  re-check in 10 s")
        time.sleep(10)
        cur = d.check_canaries("watch2")
        down = [u for u, v in cur.items() if is_bad(v)]
    if not down:
        finish(d, "HEALTHY", a.go, phase="watched")
        return
    bad = Deploy.broken(before, cur) if before else down
    if not bad:
        baseline_bad = [u for u in down if is_bad(before.get(u) or {"code": 0, "fatal": ""})]
        out("!! canaries down now AND already down before the upload (--force-canary): " + ", ".join(down)
            + " - no automatic rollback (the upload is not shown to be the cause); `rollback --go` is the orchestrator's call")
        finish(d, "DEGRADED", a.go, {"broken": down, "baseline_bad": baseline_bad, "rollback": None}, phase="watched")
        return
    out("!! canaries broken: " + ", ".join(bad))
    db_results = []
    if a.db_rollback:
        for s in d.spec.get("db") or []:
            cmd = s.get("rollback")
            if not cmd:
                continue
            code, output = (0, "(dry)") if not a.go else sh(cmd, d.base_dir, 600)
            db_results.append({"description": s.get("description"), "cmd": cmd, "code": code, "tail": output.strip()[-300:]})
            out(f"  db rollback: {s.get('description')} -> exit {code}" + (f": {output.strip()[-300:]}" if output.strip() else ""))
    touched = any(r.get("attempted") or r.get("uploaded") for r in rows)
    rb = rollback_files(d, rows, a.go) if touched else "ROLLED_BACK"
    rb_hooks = []
    if a.go:
        rb_hooks = d.after_commands("watch-rollback")
        time.sleep(min(d.settle, 10))
        after_rb = d.check_canaries("after-watch-rollback")
        recovered = not Deploy.broken(before, after_rb) if before else not any(is_bad(v) for v in after_rb.values())
    else:
        recovered = None
    d.state.setdefault("db_rollbacks", []).append({"at": now(), "go": a.go, "results": db_results})
    finish(d, Deploy.with_hooks(rb, rb_hooks) if a.go else "WOULD_ROLL_BACK", a.go,
           {"broken": bad, "rollback": rb, "recovered": recovered, "after_failed": Deploy.failed_hooks(rb_hooks)}, phase="watched")


def cmd_rollback(a):
    d = Deploy(a.run_dir, a.project_dir)
    rows = d.state.get("files") or []
    if not any(r.get("attempted") or r.get("uploaded") for r in rows):
        raise PlanError("nothing was uploaded by apply")
    rb = rollback_files(d, rows, a.go)
    rb_hooks = d.after_commands("manual-rollback") if a.go else []
    status = Deploy.with_hooks(rb, rb_hooks) if a.go else "WOULD_ROLL_BACK"
    finish(d, status, a.go, {"rollback": rb, "after_failed": Deploy.failed_hooks(rb_hooks)}, phase="rolled_back",
           exit_ok=(status == "ROLLED_BACK" or not a.go))


def report(d):
    s = d.state
    lines = [f"# Deploy report: {s.get('profile')} - {s.get('status')}", "",
             f"Run: {d.run_dir}", f"Attempt: {s.get('attempt', 1)}; fetched: {s.get('fetched_at')}; applied: {s.get('applied')}",
             f"Candidate: {s.get('candidate')}; approved: " + (f"{s['approval']['token']} at {s['approval']['at']}" +
                                                                (" (--force-canary)" if s["approval"].get("force_canary") else "")
                                                                if s.get("approval") else "no"), "",
             "| file | prod state | action | attempted | uploaded | byte-verified | rolled back |", "|--|--|--|--|--|--|--|"]
    if s.get("sealed"):
        lines += [f"Sealed hand resolution at {s['sealed']['at']}: " + ", ".join(s["sealed"]["files"]), ""]
    for r in s.get("files") or []:
        lines.append(f"| {r['local']} | {r.get('prod')}{' (resolved by hand)' if r.get('resolved') else ''} | {r.get('action')} | {'yes' if r.get('attempted') else ''} | "
                     f"{'yes' if r.get('uploaded') else ''} | {'yes' if r.get('verified') else ('n/a' if r.get('binary') else '')} | "
                     f"{'yes' if r.get('rolled_back') else ('NOT RESTORED' if (r.get('attempted') or r.get('uploaded')) and s.get('rollbacks') else '')} |")
    if s.get("canary_override"):
        lines += ["", f"Canary override (--force-canary) at {s['canary_override']['at']}: " + ", ".join(s["canary_override"]["urls"])]
    if (s.get("precheck") or {}).get("status") not in (None, "DRY_OK"):
        lines += ["", f"Stopped before the upload: {s['precheck']['status']} at {s['precheck']['at']}"]
    for label, res in (s.get("canary") or {}).items():
        lines += ["", f"## Canary {label}", "", "| url | code | fatal |", "|--|--|--|"]
        lines += [f"| {u} | {v['code']} | {v['fatal']} |" for u, v in res.items()]
    if s.get("verify"):
        lines += ["", "## Markers", "", "| url | via | code | contains | ok |", "|--|--|--|--|--|"]
        lines += [f"| {v['url']} | {v['via']} | {v['code']} | {v['contains']} | {'OK' if v['ok'] else 'FAIL'} |" for v in s["verify"]]
    need = [(label, x) for label, res in (s.get("after") or {}).items() for x in res if x.get("code") != 0 and not x.get("optional")]
    if need:
        lines += ["", "## Hooks that need manual recovery", "", "Required `after` hooks that failed; nothing was undone automatically "
                  "(a hook's compensation is not known). Run them by hand, or roll back:"]
        lines += [f"- {label}: exit {x['code']}: `{x['cmd']}`" + (f" - {x['tail']}" if x.get("tail") else "") for label, x in need]
    for label, res in (s.get("after") or {}).items():
        lines += ["", f"## After commands ({label})", ""] + [
            f"- exit {x['code']}" + (" (optional" + (", warning" if x["code"] else "") + ")" if x.get("optional") else (" REQUIRED FAILED" if x["code"] else ""))
            + f": `{x['cmd']}`" + (f" - {x['tail']}" if x["code"] else "") for x in res]
    for rb in s.get("rollbacks") or []:
        lines += ["", f"## Rollback {rb['at']} (go={rb['go']}): {rb.get('status', '')}", ""] + \
                 [f"- {dict([(True, 'OK'), (False, 'FAILED'), (None, 'UNVERIFIED')])[x['ok']]}: {x['what']}" + (f" - {x['tail']}" if x["ok"] is not True and x.get("tail") else "") for x in rb["results"]]
    for rb in s.get("db_rollbacks") or []:
        lines += ["", f"## DB rollback {rb['at']} (go={rb['go']})", ""] + [f"- exit {x['code']}: {x['description']}" for x in rb["results"]]
    write_text(os.path.join(d.dir, "REPORT.md"), "\n".join(lines) + "\n")


def cmd_status(a):
    run_dir = fwd(os.path.abspath(a.run_dir))
    s = read_json(os.path.join(run_dir, "deploy", "state.json"))
    if not s:
        out("no deploy state (run fetch)")
        return
    out(f"profile {s.get('profile')}: status {s.get('status')}, attempt {s.get('attempt', 1)}, fetched {s.get('fetched_at')}, applied {s.get('applied')}")
    if s.get("candidate"):
        out(f"candidate {s['candidate']}; approved: " + (f"{s['approval']['token']} at {s['approval']['at']}" if s.get("approval") else
                                                        f"no (apply --go needs --approved {s['candidate']})"))
    else:
        out("candidate: none (state written before the candidate tokens; fetch again before an apply)")
    if s.get("sealed"):
        out(f"sealed hand resolution at {s['sealed']['at']}: " + ", ".join(s["sealed"]["files"]) + " (fetch again drops it)")
    for r in s.get("files") or []:
        out(f"  {r['local']}: {r.get('prod')}{' (resolved by hand)' if r.get('resolved') else ''} / {r.get('action')}" + (" attempted" if r.get("attempted") and not r.get("uploaded") else "") +
            (" uploaded" if r.get("uploaded") else "") + (" verified" if r.get("verified") else "") + (" ROLLED BACK" if r.get("rolled_back") else ""))
    if s.get("archived"):
        out("  archived attempts: " + ", ".join(s["archived"]))


def main():
    ap = argparse.ArgumentParser(description="POWER: deploy with prod comparison, merge, canaries and rollback")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(name, go=False):
        sp = sub.add_parser(name)
        sp.add_argument("--run-dir", required=True)
        sp.add_argument("--project-dir", required=True)
        if go:
            sp.add_argument("--go", action="store_true")
        return sp

    common("plan")
    common("fetch").add_argument("--new-attempt", action="store_true",
                                 help="after an apply --go: archive this attempt (backup, candidate, state) and fetch again")
    sp = common("apply", go=True)
    sp.add_argument("--approved", metavar="ATTEMPT:HASH",
                    help="the CANDIDATE token fetch printed; required with --go: the yes was given for exactly this candidate")
    sp.add_argument("--claimed-by")
    sp.add_argument("--force-canary", action="store_true",
                    help="upload although a canary is already down (#THE_OPERATOR#'s explicit yes; recorded in the state)")
    common("watch", go=True).add_argument("--db-rollback", action="store_true")
    common("rollback", go=True)
    common("seal")
    sp = sub.add_parser("status")
    sp.add_argument("--run-dir", required=True)
    sp.add_argument("--project-dir", help="accepted for a uniform command line; not needed")
    a = ap.parse_args()
    try:
        {"plan": cmd_plan, "fetch": cmd_fetch, "apply": cmd_apply, "watch": cmd_watch, "rollback": cmd_rollback,
         "seal": cmd_seal, "status": cmd_status}[a.cmd](a)
    except PlanError as e:
        out(f"ERROR {e}", f"{a.cmd.upper()}: FAIL")
        sys.exit(1)


if __name__ == "__main__":
    main()
