#!/usr/bin/env python3
"""POWER run checks: plan preparation, snapshot, baseline, mechanical checks, restore.

The plan lives in <run>/tasks.json:
  {"goal": "...", "contracts": [{"id", "text", "provided_by"}], "risks": [...], "tasks": [task, ...]}
(a bare list of tasks from older runs is still read). A task:
  {"id", "title", "spec",
   "files":   [{"path", "action": "edit|create|delete"}],     closed list, project-relative
   "anchors": [{"path", "text"}],                            literal snippets that must exist before the work
   "provides": [contract ids], "uses": [contract ids], "non_goals": [...], "acceptance": [...],
   "checks":  [{"name", "cmd", "expect_exit", "contains", "not_contains",
                "before": "fail|pass|any", "serial", "timeout", "retries", "known_broken"}],
   "review_focus", "rules", "size": {"files", "lines"}, "depends_on": [task or contract ids]}

Subcommands (a human summary, then one machine line that starts with a TAG):
  config   --project-dir P                        merged json block of every `## POWER` section above P
  merge    --run-dir R                            skeleton.json + plan-areas/*.json -> tasks.json + PLAN.md
  prepare  --run-dir R --project-dir P            normalize + validate tasks.json (written back when valid)
  snapshot --run-dir R --project-dir P [--force]  copy every owned file to <run>/before/ + scope baseline
  baseline --run-dir R --project-dir P            run every check once before the work
  check    --run-dir R --project-dir P (--task ID | --tasks A,B | --all) --round N
                                                  diff against the snapshot + mechanical checks -> CHECK_JSON
  restore  --run-dir R --project-dir P --task ID [--keep stash|blocked]
                                                  put the task's files back to the snapshot, keep a copy first
  status   --run-dir R                            last check of every task
  results  --run-dir R                            the Workflow result (results.json) as a short table
  install  [--check]                              agents/*.md -> ~/.claude/agents, power_workflow.js -> ~/.claude/workflows

Checks run in Git Bash from the project directory with MSYS_NO_PATHCONV=1 and PYTHONUTF8=1
(override the shell with GIT_BASH).
"""
import argparse
import concurrent.futures
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ACTIONS = ("edit", "create", "delete")
BEFORE_VALUES = ("fail", "pass", "any")
MAX_FILES = 6
MAX_LINES = 400
DEFAULT_TIMEOUT = 120   # per check, seconds
CHECK_BUDGET = 540      # all checks of one task must fit in the runner's 600 s Bash call
TAIL = 200          # of a failed check's output in CHECK_JSON; the full output is in the per-check log
CURL_TOOL_EXIT = {2, 3, 5, 6, 7, 23, 26, 27, 43, 45, 48}  # curl's own option/transport errors, never an HTTP verdict
MAX_LINES_PER_FILE = 25  # above this per file, more than MAX_FILES files looks like more than one PR
# The skill's own agent types (agents/*.md, copied to ~/.claude/agents by `install`): no CLAUDE.md or memory in
# the agent's context and a hard maxTurns. workflow-args passes the installed ones to power_workflow.js.
POWER_AGENTS = {"runner": "power-runner", "reader": "power-reader", "verifier": "power-verifier",
                "executor": "power-executor"}
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Never in a plan, whatever the project says. A project adds its own via `deny` in its `## POWER` block.
DEFAULT_DENY = [
    "**/.env", "**/.env.*", "**/_secrets/**", "**/wp-config.php", "**/storage/secrets/**",
    "**/*.pem", "**/*.key", "**/id_rsa*", "**/.git/**", "**/vendor/**", "**/node_modules/**",
    "**/composer.lock", "**/package-lock.json", "**/pnpm-lock.yaml", "**/yarn.lock",
    "**/docs/power/**",
]
DEFAULT_ALLOW = ["**/.env.example", "**/.env.dist", "**/.env.sample"]
WALK_SKIP = {".git", "node_modules", "vendor", "__pycache__", ".idea", ".vscode"}


class PlanError(Exception):
    pass


# ---------- small helpers ----------

def now():
    return datetime.now().isoformat(timespec="seconds")


def out(*lines):
    print("\n".join(str(x) for x in lines), flush=True)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_text(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def write_json(path, data):
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def sha(data):
    return hashlib.sha256(data).hexdigest()[:16] if data is not None else None


def file_sig(path):
    if not os.path.isfile(path):
        return None
    if os.path.getsize(path) > 20 * 1024 * 1024:
        st = os.stat(path)
        return f"size:{st.st_size}:{st.st_mtime_ns}"
    return sha(read_bytes(path))


def as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if str(x) != ""]
    return [str(v)] if str(v) != "" else []


def fwd(p):
    return p.replace("\\", "/")


def ws(s):
    return re.sub(r"\s+", " ", s or "").strip()


def glob_re(pattern):
    p, i, parts = pattern.replace("\\", "/"), 0, []
    while i < len(p):
        if p.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif p.startswith("**", i):
            parts.append(".*")
            i += 2
        elif p[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif p[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(p[i]))
            i += 1
    return re.compile("^" + "".join(parts) + "$", re.IGNORECASE)


def matches(path, patterns):
    return any(glob_re(p).match(path) for p in patterns)


def to_rel(path, project_dir):
    p = str(path or "").strip().replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", p) or p.startswith("/"):
        try:
            p = os.path.relpath(os.path.abspath(p), project_dir).replace("\\", "/")
        except ValueError:
            raise PlanError(f"path outside the project: {path}")
    parts = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise PlanError(f"path leaves the project: {path}")
        parts.append(seg)
    if not parts:
        raise PlanError(f"empty path: {path!r}")
    return "/".join(parts)


def run_rel(run_dir, project_dir):
    try:
        rel = os.path.relpath(run_dir, project_dir).replace("\\", "/")
    except ValueError:  # another drive
        return None
    return None if rel.startswith("..") else rel


def in_dir(path, rel_dir):
    return bool(rel_dir) and (path == rel_dir or path.startswith(rel_dir + "/"))


# ---------- project config (`## POWER` json block in CLAUDE.md files) ----------

def power_block(md_path):
    try:
        with open(md_path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    m = re.search(r"^## POWER\b.*$", text, re.M)
    if not m:
        return None
    rest = text[m.end():]
    nxt = re.search(r"^## ", rest, re.M)
    section = rest[: nxt.start()] if nxt else rest
    j = re.search(r"```json[ \t]*\r?\n(.*?)\r?\n```", section, re.S)
    if not j:
        return None
    try:
        return json.loads(j.group(1))
    except ValueError as e:
        raise PlanError(f"invalid json in the ## POWER section of {md_path}: {e}")


def load_config(project_dir):
    """Lists add up from the project upwards; for other keys the nearest CLAUDE.md wins."""
    cfg = {"deny": [], "allow": [], "lenses": [], "smoke": [], "sources": [], "_dirs": {}}
    d = os.path.abspath(project_dir)
    while True:
        block = power_block(os.path.join(d, "CLAUDE.md"))
        if block:
            cfg["sources"].append(os.path.join(d, "CLAUDE.md"))
            for k, v in block.items():
                if isinstance(v, list) and isinstance(cfg.get(k), list):
                    cfg[k].extend(v)
                elif k not in cfg:
                    cfg[k] = v
                    cfg["_dirs"][k] = d  # relative paths and commands of this key resolve from here
        parent = os.path.dirname(d)
        if os.path.isdir(os.path.join(d, "_secrets")) or parent == d:
            break
        d = parent
    return cfg


# ---------- plan loading and normalization ----------

def load_plan(run_dir):
    path = os.path.join(run_dir, "tasks.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        raise PlanError(f"no tasks.json in {run_dir}")
    except ValueError as e:
        raise PlanError(f"tasks.json is not valid JSON: {e}")
    if isinstance(data, list):
        data = {"tasks": data}
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise PlanError(f"tasks.json missing or invalid in {run_dir}")
    data.setdefault("contracts", [])
    data["tasks"] = [task_view(t) for t in data["tasks"]]
    return data


def task_view(t):
    """Defaults for every field, without touching the disk (safe for rendering prompts)."""
    t = dict(t or {})
    t["id"] = str(t.get("id", "")).strip()
    files = []
    for f in t.get("files") or []:
        if isinstance(f, str):
            f = {"path": f, "action": ""}
        files.append({"path": str(f.get("path", "")).replace("\\", "/").strip(),
                      "action": str(f.get("action") or "").lower().strip()})
    t["files"] = files
    t["anchors"] = [{"path": str(a.get("path", "")).replace("\\", "/").strip(), "text": str(a.get("text", ""))}
                    for a in (t.get("anchors") or []) if isinstance(a, dict)]
    for key in ("provides", "uses", "non_goals", "acceptance", "depends_on"):
        t[key] = [x.strip() for x in as_list(t.get(key)) if x.strip()]
    checks = []
    for i, c in enumerate(t.get("checks") or []):
        if isinstance(c, str):
            c = {"cmd": c}
        exp = c.get("expect_exit", 0)
        checks.append({
            "name": str(c.get("name") or f"check {i + 1}"),
            "cmd": str(c.get("cmd") or "").strip(),
            "expect_exit": -1 if exp is None else int(exp),
            "contains": as_list(c.get("contains")),
            "not_contains": as_list(c.get("not_contains")),
            "before": str(c.get("before") or "any").lower(),
            "serial": bool(c.get("serial", False)),
            "timeout": int(c.get("timeout") or DEFAULT_TIMEOUT),
            "retries": int(c.get("retries") or 0),
            "known_broken": bool(c.get("known_broken", False)),
        })
    t["checks"] = checks
    for key in ("title", "spec", "review_focus", "rules"):
        t[key] = str(t.get(key) or "")
    if not t["review_focus"] and t.get("verify"):
        t["review_focus"] = str(t["verify"])
    size = t.get("size") if isinstance(t.get("size"), dict) else {}
    t["size"] = {"files": int(size.get("files") or len(files)), "lines": int(size.get("lines") or 0)}
    return t


def normalize_plan(plan, project_dir):
    plan = dict(plan)
    contracts = []
    for c in plan.get("contracts") or []:
        contracts.append({"id": str(c.get("id", "")).strip(), "text": str(c.get("text", "")),
                          "provided_by": str(c.get("provided_by", "")).strip()})
    plan["contracts"] = contracts
    tasks = []
    for t in plan["tasks"]:
        t = task_view(t)
        for f in t["files"]:
            f["path"] = to_rel(f["path"], project_dir)
            if not f["action"]:
                f["action"] = "edit" if os.path.exists(os.path.join(project_dir, f["path"])) else "create"
        for a in t["anchors"]:
            a["path"] = to_rel(a["path"], project_dir)
        tasks.append(t)
    providers = {c["id"]: c["provided_by"] for c in contracts}
    ids = {t["id"] for t in tasks}
    for t in tasks:
        deps = []
        for d in t["depends_on"]:
            d = providers.get(d, d) if d not in ids else d
            if d and d not in deps:
                deps.append(d)
        t["depends_on"] = deps
    plan["tasks"] = tasks
    return plan


def find_cycle(tasks):
    graph = {t["id"]: t["depends_on"] for t in tasks}
    state, stack = {}, []

    def visit(n):
        state[n] = 1
        stack.append(n)
        for m in graph.get(n, []):
            if m not in graph:
                continue
            if state.get(m) == 1:
                return stack[stack.index(m):] + [m]
            if not state.get(m):
                c = visit(m)
                if c:
                    return c
        stack.pop()
        state[n] = 2
        return None

    for n in graph:
        if not state.get(n):
            c = visit(n)
            if c:
                return c
    return None


def validate(plan, project_dir, run_dir, config):
    errors, warnings = [], []
    tasks, contracts = plan["tasks"], plan["contracts"]
    ids = [t["id"] for t in tasks]
    cids = [c["id"] for c in contracts]
    deny = DEFAULT_DENY + as_list(config.get("deny"))
    allow = DEFAULT_ALLOW + as_list(config.get("allow"))
    rdir = run_rel(run_dir, project_dir)

    if not tasks:
        errors.append("the plan has no tasks")
    for i in sorted({x for x in ids if ids.count(x) > 1}):
        errors.append(f"task id {i} is used twice")
    for i in sorted({x for x in cids if cids.count(x) > 1}):
        errors.append(f"contract id {i} is used twice")
    for c in contracts:
        if not c["id"]:
            errors.append("a contract has no id")
        if c["provided_by"] not in ids:
            errors.append(f"contract {c['id']}: provided_by {c['provided_by']!r} is not a task")
        if c["id"] in ids:
            errors.append(f"contract id {c['id']} is also a task id")

    owners = {}
    for t in tasks:
        for f in t["files"]:
            owners.setdefault(f["path"].lower(), []).append(t["id"])

    for t in tasks:
        tid = t["id"]
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", tid):
            errors.append(f"task id {tid!r} is not a plain id")
        for key in ("title", "spec"):
            if not t[key].strip():
                errors.append(f"{tid}: empty {key}")
        if not t["files"]:
            errors.append(f"{tid}: no files")
        if not t["acceptance"]:
            errors.append(f"{tid}: no acceptance criteria")
        if not t["checks"]:
            errors.append(f"{tid}: no mechanical checks")
        for f in t["files"]:
            p, act = f["path"], f["action"]
            full = os.path.join(project_dir, p)
            if act not in ACTIONS:
                errors.append(f"{tid}: {p}: action {act!r} is not edit/create/delete")
            if len(owners.get(p.lower(), [])) > 1 and owners[p.lower()][0] == tid:
                errors.append(f"{p} is owned by several tasks: {', '.join(owners[p.lower()])}")
            if in_dir(p, rdir):
                errors.append(f"{tid}: {p} is inside the run directory")
            elif matches(p, deny) and not matches(p, allow):
                errors.append(f"{tid}: {p} is on the never-in-a-plan list")
            if os.path.isdir(full):
                errors.append(f"{tid}: {p} is a directory, list files")
            elif act in ("edit", "delete") and not os.path.isfile(full):
                errors.append(f"{tid}: {p} is marked {act} but does not exist")
            elif act == "create" and os.path.exists(full):
                errors.append(f"{tid}: {p} is marked create but already exists")
        for a in t["anchors"]:
            full = os.path.join(project_dir, a["path"])
            if not os.path.isfile(full):
                errors.append(f"{tid}: anchor file {a['path']} does not exist")
                continue
            with open(full, encoding="utf-8", errors="replace") as fh:
                if ws(a["text"]) not in ws(fh.read()):
                    errors.append(f"{tid}: anchor not found in {a['path']}: {a['text'][:100]!r}")
            other = [o for o in owners.get(a["path"].lower(), []) if o != tid]
            if other:
                warnings.append(f"{tid}: anchor in {a['path']}, which {', '.join(other)} edits in parallel")
        if any(f["action"] in ("edit", "delete") for f in t["files"]) and not t["anchors"]:
            warnings.append(f"{tid}: edits files but has no anchors")
        for c in t["provides"]:
            prov = next((x for x in contracts if x["id"] == c), None)
            if not prov:
                errors.append(f"{tid}: provides unknown contract {c}")
            elif prov["provided_by"] != tid:
                errors.append(f"{tid}: provides {c}, but its provided_by is {prov['provided_by']}")
        for c in t["uses"]:
            if c not in cids:
                errors.append(f"{tid}: uses unknown contract {c}")
        for c in t["checks"]:
            if not c["cmd"]:
                errors.append(f"{tid}: check {c['name']!r} has no command")
            if c["before"] not in BEFORE_VALUES:
                errors.append(f"{tid}: check {c['name']!r}: before must be fail/pass/any")
            if c["timeout"] <= 0:
                errors.append(f"{tid}: check {c['name']!r}: timeout must be positive")
            if "/dev/null" in re.sub(r"[>&]\s*/dev/null", "", c["cmd"]):
                warnings.append(f"{tid}: check {c['name']!r} passes /dev/null as an argument: under MSYS_NO_PATHCONV=1 a "
                                f"native program gets the literal path (curl -o /dev/null exits 23); redirect in the "
                                f"shell (> /dev/null) instead")
        for d in t["depends_on"]:
            if d not in ids:
                errors.append(f"{tid}: depends on unknown {d}")
            elif d == tid:
                errors.append(f"{tid}: depends on itself")
        budget = sum(c["timeout"] for c in t["checks"])
        if budget > CHECK_BUDGET:
            warnings.append(f"{tid}: the checks may take up to {budget} s together; the check runner has {CHECK_BUDGET} s "
                            f"(lower the timeouts or split the task)")
        if len(t["files"]) > MAX_FILES and (not t["size"]["lines"] or t["size"]["lines"] / len(t["files"]) > MAX_LINES_PER_FILE):
            warnings.append(f"{tid}: {len(t['files'])} files - more than one PR? (limit ~{MAX_FILES} for files with real "
                            f"logic; the same small edit in many files is one task, say so in size.lines)")
        if t["size"]["lines"] > MAX_LINES:
            warnings.append(f"{tid}: ~{t['size']['lines']} changed lines - more than one PR? (limit ~{MAX_LINES})")
    for c in contracts:
        prov = next((t for t in tasks if t["id"] == c["provided_by"]), None)
        if prov and c["id"] not in prov["provides"]:
            warnings.append(f"contract {c['id']}: {prov['id']} does not list it in provides")
    cycle = find_cycle(tasks)
    if cycle:
        errors.append("dependency cycle: " + " -> ".join(cycle))
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))


# ---------- scope (changes outside the run's files) ----------

def git_dirty(project_dir):
    """Project-relative paths that git reports as changed or untracked; None without git."""
    try:
        pre = subprocess.run(["git", "-C", project_dir, "rev-parse", "--show-prefix"], capture_output=True,
                             timeout=30, creationflags=CREATE_NO_WINDOW)
        if pre.returncode != 0:
            return None
        prefix = pre.stdout.decode("utf-8", "replace").strip()
        st = subprocess.run(["git", "-C", project_dir, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                             "--", "."], capture_output=True, timeout=180, creationflags=CREATE_NO_WINDOW)
        if st.returncode != 0:
            return None
    except (OSError, subprocess.TimeoutExpired):
        return None
    entries = st.stdout.decode("utf-8", "replace").split("\0")
    paths, i = set(), 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if len(e) < 4:
            continue
        if e[0] in "RC":
            i += 1  # the original path of a rename/copy follows
        p = e[3:]
        if prefix and not p.startswith(prefix):
            continue
        paths.add(p[len(prefix):])
    return paths


def walk_sigs(project_dir, rdir):
    sigs, count = {}, 0
    for root, dirs, files in os.walk(project_dir):
        rel_root = os.path.relpath(root, project_dir).replace("\\", "/")
        rel_root = "" if rel_root == "." else rel_root
        dirs[:] = [d for d in dirs if d not in WALK_SKIP and not in_dir(f"{rel_root}/{d}".lstrip("/"), rdir)]
        for name in files:
            rel = f"{rel_root}/{name}".lstrip("/")
            st = os.stat(os.path.join(root, name))
            sigs[rel] = [st.st_size, st.st_mtime_ns]
            count += 1
            if count > 200000:
                return None
    return sigs


def scope_state(project_dir, run_dir):
    rdir = run_rel(run_dir, project_dir)
    dirty = git_dirty(project_dir)
    if dirty is not None:
        return {"mode": "git", "dirty": {p: file_sig(os.path.join(project_dir, p))
                                          for p in sorted(dirty) if not in_dir(p, rdir)}}
    sigs = walk_sigs(project_dir, rdir)
    return {"mode": "walk", "files": sigs} if sigs is not None else {"mode": "off"}


def scope_changes(project_dir, run_dir, base, owned):
    rdir = run_rel(run_dir, project_dir)
    skip = lambda p: p.lower() in owned or in_dir(p, rdir) or "/__pycache__/" in f"/{p}"
    changed = []
    if base.get("mode") == "git":
        now_dirty = git_dirty(project_dir) or set()
        was = base.get("dirty", {})
        for p in sorted(set(was) | now_dirty):
            if skip(p):
                continue
            if p not in was or file_sig(os.path.join(project_dir, p)) != was[p]:
                changed.append(p)
    elif base.get("mode") == "walk":
        now_sigs = walk_sigs(project_dir, rdir) or {}
        was = base.get("files", {})
        for p in sorted(set(was) | set(now_sigs)):
            if not skip(p) and was.get(p) != now_sigs.get(p):
                changed.append(p)
    return changed


# ---------- running checks ----------

def bash_exe():
    env = os.environ.get("GIT_BASH")
    if env and os.path.isfile(env):
        return env
    if os.name != "nt":
        return shutil.which("bash") or "/bin/bash"
    candidates = []
    git = shutil.which("git")
    if git:
        candidates.append(os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(git))), "bin", "bash.exe"))
    candidates += [r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"]
    for c in candidates:
        if os.path.isfile(c):
            return c
    b = shutil.which("bash")
    if b and "system32" not in b.lower():  # System32\bash.exe is WSL, not Git Bash
        return b
    raise RuntimeError("Git Bash not found; set GIT_BASH to its bash.exe")


class FileLock:
    def __init__(self, path, stale=1800, wait=3600):
        self.path, self.stale, self.wait = path, stale, wait

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        deadline = time.time() + self.wait
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {now()}".encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > self.stale:
                        os.remove(self.path)
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    raise TimeoutError(f"serial lock {self.path} held for too long")
                time.sleep(1)

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except OSError:
            pass


def broken_check(cmd, code, text):
    """A failure of the check command itself (never a verdict on the code): curl option/transport errors, a shell
    syntax error. Such a failure cannot be fixed by an executor; the workflow blocks the task for the orchestrator."""
    tail = text[-3000:]
    m = re.search(r"curl: \((\d+)\)", tail)
    if m and int(m.group(1)) in CURL_TOOL_EXIT:
        return f"curl error {m.group(0)}: an option or transport error of the check itself"
    if code in CURL_TOOL_EXIT and re.search(r"(^|[\s;&|(])curl\s", cmd) and "code=" in tail.lower():
        return f"curl exit {code} with an HTTP code printed: the check's own output or option failed (-o /dev/null?)"
    if re.search(r"syntax error near unexpected token|unexpected EOF while looking for matching", tail):
        return "shell syntax error in the check command"
    return ""


def run_once(c, project_dir):
    env = dict(os.environ, MSYS_NO_PATHCONV="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    t0 = time.time()
    try:
        p = subprocess.run([bash_exe(), "-c", c["cmd"]], cwd=project_dir, env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=c["timeout"],
                           creationflags=CREATE_NO_WINDOW)
        code, raw, timed_out = p.returncode, p.stdout or b"", False
    except subprocess.TimeoutExpired as e:
        code, raw, timed_out = None, e.stdout or b"", True
    except OSError as e:
        code, raw, timed_out = None, str(e).encode(), False
    text = raw.decode("utf-8", "replace")
    reasons = []
    if timed_out:
        reasons.append(f"timed out after {c['timeout']} s")
    elif code is None:
        reasons.append("could not start: " + text.strip()[:200])
    elif c["expect_exit"] != -1 and code != c["expect_exit"]:
        reasons.append(f"exit {code}, expected {c['expect_exit']}")
    for s in c["contains"]:
        if s not in text:
            reasons.append(f"output lacks {s!r}")
    for s in c["not_contains"]:
        if s in text:
            reasons.append(f"output contains {s!r}")
    return {"ok": not reasons, "exit": code, "reasons": reasons, "output": text,
            "broken": broken_check(c["cmd"], code, text) if reasons else "",
            "seconds": round(time.time() - t0, 1)}


def run_check(c, project_dir, lock_path):
    res = None
    for attempt in range(1 + max(0, c["retries"])):
        if attempt:
            time.sleep(2)
        if c["serial"]:
            with FileLock(lock_path):
                res = run_once(c, project_dir)
        else:
            res = run_once(c, project_dir)
        res["attempt"] = attempt + 1
        if res["ok"]:
            break
    return res


def run_checks(checks, project_dir, lock_path, log_dir, prefix):
    """Run a task's checks (parallel unless serial); write one log per check."""
    results = [None] * len(checks)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(run_check, c, project_dir, lock_path): i for i, c in enumerate(checks)}
        for fut in concurrent.futures.as_completed(futs):
            i = futs[fut]
            try:
                results[i] = fut.result()
            except Exception as e:  # a broken lock or shell must not kill the whole report
                results[i] = {"ok": False, "exit": None, "reasons": [f"runner error: {e}"], "output": "",
                              "seconds": 0, "attempt": 1}
    for i, (c, r) in enumerate(zip(checks, results)):
        log = fwd(os.path.join(log_dir, f"{prefix}check{i + 1}.log"))
        write_text(log, f"$ {c['cmd']}\n# exit {r['exit']} in {r['seconds']} s, attempt {r['attempt']}\n\n{r['output']}")
        r["log"] = log
    return results


# ---------- diffs ----------

def is_binary(data):
    return data is not None and b"\0" in data[:8192]


def file_diff(path, old, new):
    """Unified diff text and (added, removed); None when unchanged."""
    if old == new:
        return None
    if is_binary(old) or is_binary(new):
        return f"Binary file {path} changed ({sha(old)} -> {sha(new)})\n", (0, 0)
    a = (old or b"").decode("utf-8", "replace").splitlines(keepends=True)
    b = (new or b"").decode("utf-8", "replace").splitlines(keepends=True)
    lines = difflib.unified_diff(a, b, f"a/{path}" if old is not None else "/dev/null",
                                 f"b/{path}" if new is not None else "/dev/null")
    text, added, removed = [], 0, 0
    for ln in lines:
        if not ln.endswith("\n"):
            ln += "\n\\ No newline at end of file\n"
        if ln.startswith("+") and not ln.startswith("+++"):
            added += 1
        elif ln.startswith("-") and not ln.startswith("---"):
            removed += 1
        text.append(ln)
    return "".join(text), (added, removed)


# ---------- commands ----------

def paths(a):
    a.run_dir = fwd(os.path.abspath(a.run_dir))
    if getattr(a, "project_dir", None):
        a.project_dir = fwd(os.path.abspath(a.project_dir))


def manifest_of(run_dir):
    m = read_json(os.path.join(run_dir, "before", "manifest.json"))
    if not m:
        raise PlanError("no snapshot: run `snapshot` first")
    return m


def cmd_config(a):
    cfg = load_config(a.project_dir)
    if getattr(a, "full", False):
        out(json.dumps(cfg, ensure_ascii=False, indent=2))
        return
    # Short by default: the output lands in the orchestrator's context for the whole run (the json is ~4k tokens).
    out("sources: " + (", ".join(fwd(s) for s in cfg["sources"]) or "none (no ## POWER section)"))
    out(f"deny: {len(cfg['deny'])} patterns, allow: {len(cfg['allow'])}, smoke: {len(cfg['smoke'])} commands, "
        f"claim: {cfg.get('claim') or '-'}")
    out("lenses: " + (", ".join(str(l.get("name")) + (f" ({l.get('agent_type')})" if l.get("agent_type") else "")
                                for l in cfg["lenses"]) or "default (domain, smoke, i18n)"))
    profiles = (cfg.get("deploy") or {}).get("profiles") or {}
    for name, p in profiles.items():
        out(f"deploy profile {name}: {p.get('local_root')} -> {p.get('remote_root')}; canary {len(p.get('canary') or [])}, "
            f"after {len(p.get('after') or [])}, pace {p.get('pace', 3)}, settle {p.get('settle', 15)}")
    if not profiles:
        out("deploy profiles: none (manual deploy)")
    found = agent_types(a.project_dir)
    missing = [v for k, v in POWER_AGENTS.items() if k not in found]
    out("agent types: " + (", ".join(found.values()) or "none") + (f"; NOT installed: {', '.join(missing)} (run install)" if missing else ""))
    out("CONFIG: OK (--full prints the merged json)")


def agent_types(project_dir):
    """The POWER agent types that are installed (project .claude/agents or ~/.claude/agents): role -> name."""
    dirs = [os.path.join(project_dir, ".claude", "agents"), os.path.join(os.path.expanduser("~"), ".claude", "agents")]
    return {role: name for role, name in POWER_AGENTS.items()
            if any(os.path.isfile(os.path.join(d, name + ".md")) for d in dirs)}


def session_files(project_dir):
    """Project files the run itself writes outside tasks.json (the CLAIM file): never 'changed outside the run'."""
    try:
        cfg = load_config(project_dir)
        rel = cfg.get("claim")
        if not rel:
            return []
        return [to_rel(os.path.join(cfg["_dirs"].get("claim") or project_dir, rel), project_dir)]
    except PlanError:
        return []


def cmd_results(a):
    data = read_json(os.path.join(a.run_dir, "results.json"))
    if isinstance(data, dict) and isinstance(data.get("result"), list):
        data = data["result"]
    if not isinstance(data, list):
        raise PlanError("results.json missing or not a list (copy the Workflow output there first)")
    counts = {}
    for r in data:
        if not isinstance(r, dict):
            continue
        st = str(r.get("status"))
        counts[st] = counts.get(st, 0) + 1
        vb = ", ".join(str(x) for x in (r.get("verified_by") or [])) or "-"
        out(f"{r.get('task')}: {st}{' (escalated)' if r.get('escalated') else ''} - author {r.get('author') or '-'}, "
            f"verified by {vb}, rounds {r.get('rounds', 0)}" + (f" - {str(r.get('reason'))[:160]}" if r.get("reason") else ""))
        res = r.get("result") or {}
        for p in (r.get("last_problems") or [])[:2]:
            out(f"    problem: {str(p)[:160]}")
        for p in (res.get("open_issues") or [])[:2]:
            out(f"    open: {str(p)[:160]}")
        if res.get("plan_change"):
            out(f"    plan_change: {str(res['plan_change'])[:160]}")
        if r.get("stray_files"):
            out(f"    stray files: {', '.join(str(x) for x in r['stray_files'])}")
        chk = r.get("check") or {}
        if chk.get("warnings"):
            out(f"    warnings: {' | '.join(str(x) for x in chk['warnings'])[:200]}")
    out("RESULTS: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))


def cmd_install(a):
    here = os.path.dirname(os.path.abspath(__file__))
    home = os.path.join(os.path.expanduser("~"), ".claude")
    pairs = [(os.path.join(here, "agents", f), os.path.join(home, "agents", f))
             for f in sorted(os.listdir(os.path.join(here, "agents"))) if f.endswith(".md")]
    pairs.append((os.path.join(here, "power_workflow.js"), os.path.join(home, "workflows", "power-execute.js")))
    copied = 0
    for src, dst in pairs:
        if os.path.isfile(dst) and read_bytes(src) == read_bytes(dst):
            state = "unchanged"
        else:
            state = "update" if os.path.isfile(dst) else "new"
            if not a.check:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                copied += 1
        out(f"  {fwd(dst)}: {state}" + (" -> copied" if state != "unchanged" and not a.check else ""))
    missing = settings_missing(home)
    for m in missing:
        out(f"  SETTING missing: {m}")
    if missing:
        out("  Add them to ~/.claude/settings.json by hand (see reference/setup.md); install never edits settings.")
    out(f"INSTALL: {'CHECK' if a.check else 'OK'} ({copied} copied; agent types {', '.join(POWER_AGENTS.values())}; "
        f"Workflow name power-execute; {len(missing)} settings missing)")


RECOMMENDED_SETTINGS = {
    "subagentPromptCacheTtl": ("1h", "agent prompt cache survives the gaps between a task's agents"),
    "autoContinueAtUsageLimit": (True, "a run pauses at the usage limit instead of failing its agents"),
}


def settings_missing(home):
    s = read_json(os.path.join(home, "settings.json"))
    s = s if isinstance(s, dict) else {}
    missing = [f'"{k}": {json.dumps(v)}  ({why})' for k, (v, why) in RECOMMENDED_SETTINGS.items() if s.get(k) != v]
    allow = (s.get("permissions") or {}).get("allow") or []
    if not any(x in ("Workflow", "Workflow(power-execute)") for x in allow):
        missing.append('permissions.allow: "Workflow(power-execute)"  (no approval prompt; needed for START DEPLOY and headless)')
    return missing


def cmd_prepare(a):
    raw = read_json(os.path.join(a.run_dir, "tasks.json"))
    plan = normalize_plan(load_plan(a.run_dir), a.project_dir)
    errors, warnings = validate(plan, a.project_dir, a.run_dir, load_config(a.project_dir))
    for t in plan["tasks"]:
        deps = ", ".join(t["depends_on"]) or "-"
        out(f"{t['id']}: {len(t['files'])} files, ~{t['size']['lines']} lines, {len(t['checks'])} checks, depends on {deps} - {t['title'][:70]}")
    for w in warnings:
        out("WARN  " + w)
    for e in errors:
        out("ERROR " + e)
    if errors:
        out(f"PREPARE: FAIL ({len(errors)} errors, {len(warnings)} warnings)")
        sys.exit(1)
    orig = os.path.join(a.run_dir, "tasks.orig.json")
    if raw is not None and not os.path.exists(orig):
        write_json(orig, raw)
    data = {k: v for k, v in plan.items()}
    write_json(os.path.join(a.run_dir, "tasks.json"), data)
    out(f"PREPARE: OK ({len(plan['tasks'])} tasks, {len(plan['contracts'])} contracts, {len(warnings)} warnings)")


def cmd_merge(a):
    skel = read_json(os.path.join(a.run_dir, "skeleton.json"))
    if not isinstance(skel, dict) or not skel.get("areas"):
        raise PlanError("skeleton.json missing or without areas")
    tasks, sections, problems, n = [], [], [], 0
    for area in skel["areas"]:
        name = str(area.get("name", "")).strip()
        ap = read_json(os.path.join(a.run_dir, "plan-areas", f"{name}.json"))
        if not isinstance(ap, dict):
            problems.append(f"area {name}: plan-areas/{name}.json missing")
            continue
        if str(ap.get("mismatch", "")).strip():
            problems.append(f"area {name}: MISMATCH {ap['mismatch']}")
            continue
        local = {}
        area_tasks = list(ap.get("tasks") or [])
        owns = [str(x).replace("\\", "/").strip().rstrip("/") for x in as_list(area.get("owns"))]
        for t in area_tasks:
            for f in t.get("files") or []:
                p = str(f.get("path", "") if isinstance(f, dict) else f).replace("\\", "/").strip().lstrip("./")
                if owns and not any(p == o or p.startswith(o + "/") for o in owns):
                    problems.append(f"area {name}: {t.get('id')} uses {p}, which is outside the area ({', '.join(owns)})")
        for t in area_tasks:
            n += 1
            local[str(t.get("id"))] = f"t{n}"
        for t in area_tasks:
            t = dict(t)
            t["depends_on"] = [local.get(str(d), str(d)) for d in as_list(t.get("depends_on"))]
            t["id"], t["area"] = local[str(t.get("id"))], name
            tasks.append(t)
        mapping = ", ".join(f"{k} = {v}" for k, v in local.items())
        sections.append(f"<!-- area {name}: {mapping} -->\n\n{ap.get('section_md', '').strip()}")
    if problems:
        for p in problems:
            out("ERROR " + p)
        out("MERGE: FAIL")
        sys.exit(1)
    contracts = []
    for c in skel.get("contracts") or []:
        c = dict(c)
        prov = [t["id"] for t in tasks if c.get("id") in as_list(t.get("provides"))]
        if len(prov) == 1:
            c["provided_by"] = prov[0]
        contracts.append(c)
    data = {"goal": skel.get("goal", ""), "risks": skel.get("risks", []), "contracts": contracts, "tasks": tasks}
    write_json(os.path.join(a.run_dir, "tasks.json"), data)
    head = ""
    sk_md = os.path.join(a.run_dir, "skeleton.md")
    if os.path.isfile(sk_md):
        with open(sk_md, encoding="utf-8") as f:
            head = f.read().rstrip()
    write_text(os.path.join(a.run_dir, "PLAN.md"), head + "\n\n" + "\n\n".join(sections) + "\n")
    out(f"MERGE: OK ({len(tasks)} tasks from {len(skel['areas'])} areas; run prepare next)")


def cmd_snapshot(a):
    mpath = os.path.join(a.run_dir, "before", "manifest.json")
    if os.path.exists(mpath) and not a.force:
        out(f"SNAPSHOT: EXISTS {fwd(mpath)} (use --force only before any work started)")
        return
    plan = load_plan(a.run_dir)
    files_dir = os.path.join(a.run_dir, "before", "files")
    if a.force and os.path.isdir(files_dir):
        shutil.rmtree(files_dir)
    manifest = {"taken_at": now(), "files": {}}
    for t in plan["tasks"]:
        for f in t["files"]:
            src = os.path.join(a.project_dir, f["path"])
            entry = {"task": t["id"], "action": f["action"], "exists": os.path.isfile(src), "sha": None}
            if entry["exists"]:
                dst = os.path.join(files_dir, f["path"])
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                entry["sha"] = sha(read_bytes(src))
            manifest["files"][f["path"]] = entry
    manifest["scope"] = scope_state(a.project_dir, a.run_dir)
    write_json(mpath, manifest)
    out(f"SNAPSHOT: OK ({len(manifest['files'])} files, scope mode {manifest['scope']['mode']})")


def cmd_baseline(a):
    # Needs only tasks.json: it runs while the owner reads the plan, before the snapshot.
    plan = load_plan(a.run_dir)
    base_dir = os.path.join(a.run_dir, "checks", "baseline")
    lock = os.path.join(a.run_dir, "checks", ".serial.lock")
    broken, weak, suspect = [], [], []
    for t in plan["tasks"]:
        res = run_checks(t["checks"], a.project_dir, lock, base_dir, f"{t['id']}.")
        record = []
        for c, r in zip(t["checks"], res):
            kind = "ok"
            if r.get("broken"):
                kind = "SUSPECT"
                suspect.append(f"{t['id']} / {c['name']}: {r['broken']} ({'; '.join(r['reasons'])})")
            elif c["before"] == "pass" and not r["ok"]:
                kind = "PRE-BROKEN"
                broken.append(f"{t['id']} / {c['name']}: {'; '.join(r['reasons'])}")
            elif c["before"] == "fail" and r["ok"]:
                kind = "WEAK"
                weak.append(f"{t['id']} / {c['name']}: passes before the work, so it cannot prove the work")
            record.append({"name": c["name"], "ok": r["ok"], "exit": r["exit"], "reasons": r["reasons"],
                           "class": kind, "log": r["log"]})
        write_json(os.path.join(base_dir, f"{t['id']}.json"), record)
        out(f"{t['id']}: " + ", ".join(f"{x['name']}={'pass' if x['ok'] else 'fail'}" + ("" if x["class"] == "ok" else f" ({x['class']})") for x in record))
    for b in broken:
        out("PRE-BROKEN " + b)
    for w in weak:
        out("WEAK " + w)
    for s in suspect:
        out("SUSPECT " + s + " - the check command itself fails; fix it in tasks.json before the work")
    out("BASELINE: " + ("OK" if not (broken or weak or suspect)
                        else f"WARN ({len(broken)} pre-broken, {len(weak)} weak, {len(suspect)} suspect)"))


def check_task(a, t, manifest, out_dir, prefix, lock):
    failures, warnings, diffs = [], [], []
    added = removed = 0
    unchanged = []
    for f in t["files"]:
        p, act = f["path"], f["action"]
        entry = manifest["files"].get(p)
        if entry is None:
            failures.append(f"{p}: not in the snapshot (the plan changed after the snapshot; run snapshot --force before work)")
            continue
        cur = os.path.join(a.project_dir, p)
        old = read_bytes(os.path.join(a.run_dir, "before", "files", p)) if entry["exists"] else None
        new = read_bytes(cur) if os.path.isfile(cur) else None
        if act == "create" and new is None:
            failures.append(f"{p}: not created")
        elif act == "delete" and new is not None:
            failures.append(f"{p}: not deleted")
        elif act == "edit" and new is None:
            failures.append(f"{p}: missing (it should be edited, not deleted)")
        d = file_diff(p, old, new)
        if old and new and not is_binary(old) and (b"\r\n" in old) != (b"\r\n" in new):
            warnings.append(f"{p}: line endings changed (CRLF/LF), so the diff shows whole lines as changed")
        if d is None:
            unchanged.append(p)
        else:
            diffs.append(d[0])
            added += d[1][0]
            removed += d[1][1]
    if t["files"] and len(unchanged) == len(t["files"]):
        failures.append("no change in the task's files")
    elif unchanged:
        warnings.append("declared but unchanged: " + ", ".join(unchanged))
    base = {x["name"]: x for x in read_json(os.path.join(a.run_dir, "checks", "baseline", f"{t['id']}.json"), [])}
    results = run_checks(t["checks"], a.project_dir, lock, out_dir, prefix)
    rows = []
    for c, r in zip(t["checks"], results):
        b = base.get(c["name"])
        if not r["ok"]:
            tail = r["output"].strip()[-TAIL:].replace("\n", " | ")
            msg = f"check {c['name']!r}: {'; '.join(r['reasons'])}" + (f" - output: {tail}" if tail else "")
            if r.get("broken"):
                # The check command itself failed: the workflow blocks the task for the orchestrator to fix the
                # plan (then verify-only), instead of sending a model to "fix" code that may be right.
                msg = f"CHECK-BROKEN: {msg} ({r['broken']}; fix the check in tasks.json, then re-verify)"
            if c["known_broken"]:
                warnings.append("known broken, ignored: " + msg)
            else:
                failures.append(msg)
        if b and b.get("class") == "PRE-BROKEN":
            warnings.append(f"check {c['name']!r} was already failing before the work")
        rows.append((c, r))
    return {"failures": failures, "warnings": warnings, "diff": "".join(diffs),
            "stat": {"files": len(diffs), "added": added, "removed": removed}, "rows": rows}


def smoke_checks(project_dir):
    """The project's `smoke` commands (`## POWER` block, merged upwards) as check items. Only `check --all` runs them
    (the final and final2 rounds): they are the local smoke of SKILL section 8, not a per-task check. They run from
    the run's project_dir, serially. An invalid `## POWER` block raises PlanError (V3-06): the check ends INFRA
    instead of silently running without the smoke suite."""
    cmds = load_config(project_dir).get("smoke") or []
    return [{"name": f"smoke {i + 1}", "cmd": str(c).strip(), "expect_exit": 0, "contains": [], "not_contains": [],
             "before": "any", "serial": True, "timeout": DEFAULT_TIMEOUT, "retries": 0, "known_broken": False}
            for i, c in enumerate(cmds) if str(c).strip()]


def run_smoke(a, out_dir, lock):
    items = smoke_checks(a.project_dir)
    if not items:
        return None
    res = run_checks(items, a.project_dir, lock, out_dir, "smoke.")
    fails = []
    for c, r in zip(items, res):
        if not r["ok"]:
            tail = r["output"].strip()[-TAIL:].replace("\n", " | ")
            fails.append(f"smoke: check {c['name']!r} (`{c['cmd']}`): {'; '.join(r['reasons'])}" + (f" - output: {tail}" if tail else ""))
    return {"pass": not fails, "failures": fails,
            "rows": [{"name": c["name"], "cmd": c["cmd"], "ok": r["ok"], "exit": r["exit"], "seconds": r["seconds"], "log": r["log"]}
                     for c, r in zip(items, res)]}


def report_md(title, results, scope, smoke=None):
    lines = [f"# {title}", ""]
    if smoke is not None:
        lines += [f"## smoke: {'PASS' if smoke['pass'] else 'FAIL'} (the project's smoke commands, `check --all` only)", ""]
        for f in smoke["failures"]:
            lines.append(f"- FAIL {f}")
        lines += ["", "| check | command | result | exit | s | log |", "|--|--|--|--|--|--|"]
        for x in smoke["rows"]:
            lines.append(f"| {x['name']} | `{x['cmd'].replace('|', '/')}` | {'pass' if x['ok'] else 'FAIL'} | {x['exit']} | {x['seconds']} | {x['log']} |")
        lines.append("")
    for tid, r in results.items():
        lines.append(f"## {tid}: {'PASS' if not r['failures'] else 'FAIL'}")
        lines.append(f"Diff: {r['stat']['files']} files, +{r['stat']['added']} -{r['stat']['removed']}")
        for f in r["failures"]:
            lines.append(f"- FAIL {f}")
        for w in r["warnings"]:
            lines.append(f"- warn {w}")
        lines += ["", "| check | result | exit | s | attempt | log |", "|--|--|--|--|--|--|"]
        for c, x in r["rows"]:
            res = "pass" if x["ok"] else ("known broken" if c["known_broken"] else "FAIL")
            lines.append(f"| {c['name']} | {res} | {x['exit']} | {x['seconds']} | {x['attempt']} | {x['log']} |")
        lines.append("")
    if scope:
        lines += ["## Changed outside the run's files since the snapshot",
                  "Another session, or an agent outside its list - the reviewer decides.", ""]
        lines += [f"- {p}" for p in scope]
    return "\n".join(lines) + "\n"


def cmd_check(a):
    load_config(a.project_dir)   # V3-06: an invalid ## POWER block (deny / smoke / claim) is INFRA for every check, not an empty config
    plan = load_plan(a.run_dir)
    manifest = manifest_of(a.run_dir)
    by_id = {t["id"]: t for t in plan["tasks"]}
    if a.task:
        sel = [a.task]
    elif a.tasks:
        sel = [x.strip() for x in a.tasks.split(",") if x.strip()]
    else:
        sel = list(by_id)
    unknown = [x for x in sel if x not in by_id]
    if unknown:
        raise PlanError("unknown task: " + ", ".join(unknown))
    single = bool(a.task)
    out_dir = os.path.join(a.run_dir, "checks", f"{a.task}.r{a.round}" if single else str(a.round))
    lock = os.path.join(a.run_dir, "checks", ".serial.lock")
    owned = {f["path"].lower() for t in plan["tasks"] for f in t["files"]}
    owned |= {p.lower() for p in session_files(a.project_dir)}
    results = {tid: check_task(a, by_id[tid], manifest, out_dir, "" if single else f"{tid}.", lock) for tid in sel}
    smoke = run_smoke(a, out_dir, lock) if a.all else None   # 4.7: the project's smoke commands, in the full rounds only
    scope = scope_changes(a.project_dir, a.run_dir, manifest.get("scope", {}), owned)
    diff_path = fwd(os.path.join(out_dir, "diff.patch"))
    report_path = fwd(os.path.join(out_dir, "report.md"))
    write_text(diff_path, "".join(results[t]["diff"] for t in sel))
    title = f"Checks {a.task} round {a.round}" if single else f"Checks round {a.round}: {', '.join(sel)}"
    write_text(report_path, report_md(title, results, scope, smoke))
    scope_warn = [f"changed outside the run's files: {', '.join(scope[:20])}" + (" ..." if len(scope) > 20 else "")] if scope else []
    # `scope_changes` is the machine-readable list of the same (P1-04: the harness turns it into a SUSPECT stop)
    if single:
        r = results[a.task]
        data = {"task": a.task, "round": str(a.round), "pass": not r["failures"], "failures": r["failures"],
                "warnings": r["warnings"] + scope_warn, "scope_changes": scope, "diff_path": diff_path, "report_path": report_path}
    else:
        data = {"round": str(a.round), "pass": all(not r["failures"] for r in results.values()),
                "tasks": {t: {"pass": not r["failures"], "failures": r["failures"], "warnings": r["warnings"]}
                          for t, r in results.items()},
                "failures": [f"{t}: {f}" for t, r in results.items() for f in r["failures"]],
                "warnings": [f"{t}: {w}" for t, r in results.items() for w in r["warnings"]] + scope_warn,
                "scope_changes": scope, "diff_path": diff_path, "report_path": report_path}
        if smoke is not None:
            data["smoke"] = smoke
            data["failures"] += smoke["failures"]
            data["pass"] = data["pass"] and smoke["pass"]
    write_json(os.path.join(out_dir, "check.json"), data)
    for tid, r in results.items():
        out(f"{tid}: {'PASS' if not r['failures'] else 'FAIL'} (+{r['stat']['added']} -{r['stat']['removed']} in {r['stat']['files']} files)")
    if smoke is not None:
        out(f"smoke: {'PASS' if smoke['pass'] else 'FAIL'} ({len(smoke['rows'])} commands)")
    out("CHECK_JSON: " + json.dumps(data, ensure_ascii=False))


def cmd_restore(a):
    plan = load_plan(a.run_dir)
    manifest = manifest_of(a.run_dir)
    t = next((x for x in plan["tasks"] if x["id"] == a.task), None)
    if not t:
        raise PlanError(f"unknown task {a.task}")
    if a.keep == "blocked":
        keep_dir = os.path.join(a.run_dir, "blocked", a.task)
    else:
        n = 1
        while os.path.exists(os.path.join(a.run_dir, "stash", f"{a.task}.{n}")):
            n += 1
        keep_dir = os.path.join(a.run_dir, "stash", f"{a.task}.{n}")
    restored = []
    for f in t["files"]:
        p = f["path"]
        entry = manifest["files"].get(p)
        if entry is None:
            continue
        cur = os.path.join(a.project_dir, p)
        old_path = os.path.join(a.run_dir, "before", "files", p)
        new = read_bytes(cur) if os.path.isfile(cur) else None
        old = read_bytes(old_path) if entry["exists"] else None
        if new == old:
            continue
        if new is not None:
            dst = os.path.join(keep_dir, p)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(cur, dst)
        if old is None:
            os.remove(cur)
        else:
            os.makedirs(os.path.dirname(cur), exist_ok=True)
            shutil.copy2(old_path, cur)
        restored.append(p)
    if restored:
        write_json(os.path.join(keep_dir, "restore.json"), {"task": a.task, "restored": restored, "at": now()})
    out(f"RESTORE: OK ({len(restored)} files back to the snapshot" + (f"; the changed versions are in {fwd(keep_dir)})" if restored else ")"))


def cmd_status(a):
    plan = load_plan(a.run_dir)
    root = os.path.join(a.run_dir, "checks")
    for t in plan["tasks"]:
        rounds = []
        if os.path.isdir(root):
            for d in os.listdir(root):
                m = re.match(re.escape(t["id"]) + r"\.r(\d+)$", d)
                if m:
                    rounds.append(int(m.group(1)))
        if not rounds:
            out(f"{t['id']}: no checks yet")
            continue
        last = read_json(os.path.join(root, f"{t['id']}.r{max(rounds)}", "check.json"), {})
        out(f"{t['id']}: round {max(rounds)} {'PASS' if last.get('pass') else 'FAIL'}"
            + (f" - {last['failures'][0][:120]}" if last.get("failures") else ""))


def main():
    ap = argparse.ArgumentParser(description="POWER: plan preparation and mechanical checks")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp, project=True):
        sp.add_argument("--run-dir", required=True)
        if project:
            sp.add_argument("--project-dir", required=True)

    sp = sub.add_parser("config")
    sp.add_argument("--project-dir", required=True)
    sp.add_argument("--full", action="store_true")
    common(sub.add_parser("merge"), project=False)
    common(sub.add_parser("prepare"))
    sp = sub.add_parser("snapshot")
    common(sp)
    sp.add_argument("--force", action="store_true")
    common(sub.add_parser("baseline"))
    sp = sub.add_parser("check")
    common(sp)
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--task")
    g.add_argument("--tasks")
    g.add_argument("--all", action="store_true")
    sp.add_argument("--round", required=True)
    sp = sub.add_parser("restore")
    common(sp)
    sp.add_argument("--task", required=True)
    sp.add_argument("--keep", choices=["stash", "blocked"], default="stash")
    common(sub.add_parser("status"), project=False)
    common(sub.add_parser("results"), project=False)
    sub.add_parser("install").add_argument("--check", action="store_true")

    a = ap.parse_args()
    if a.cmd == "config":
        a.project_dir = os.path.abspath(a.project_dir)
    elif a.cmd != "install":
        paths(a)
    try:
        {"config": cmd_config, "merge": cmd_merge, "prepare": cmd_prepare, "snapshot": cmd_snapshot,
         "baseline": cmd_baseline, "check": cmd_check, "restore": cmd_restore, "status": cmd_status,
         "results": cmd_results, "install": cmd_install}[a.cmd](a)
    except PlanError as e:
        out(f"ERROR {e}", f"{a.cmd.upper()}: FAIL")
        if a.cmd == "check":
            # INFRA: the run itself is broken (no snapshot, bad plan), not the task - the workflow blocks
            # the task instead of sending the executor to "fix" it.
            fail = {"task": getattr(a, "task", None) or "", "round": str(a.round), "pass": False,
                    "failures": [f"INFRA: power_check: {e}"], "warnings": [], "diff_path": "", "report_path": ""}
            out("CHECK_JSON: " + json.dumps(fail, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
