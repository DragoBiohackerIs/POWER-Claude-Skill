#!/usr/bin/env python3
"""POWER harness: phases 0-6 of a POWER run driven from code, outside the Claude session.

  power_run.py start    --run-dir R --project-dir P --request-file F --step1 opus --step2 sonnet --step3 opus
                        [--flags START,DEPLOY] [--recon name=focus ...] [--lens name=focus ...] [--title T]
  power_run.py continue --run-dir R --project-dir P [--replan] [--diagnose] [--refresh-quota]   # after Gate A ("da"), or after a crash
  power_run.py verify   --run-dir R --project-dir P --tasks id:author:round[,...]   # after a fixed check command
  power_run.py status   --run-dir R                              # phase, tasks, running agents, cost, next step
  power_run.py deploy   --run-dir R --project-dir P [--go] [--claimed-by T]   # phase 6: plan + fetch; --go: apply + watch
  power_run.py cost     --run-dir R                              # the measured cost of the harness agents

Why: measured over the last five runs, the orchestrating session was 32-52% of the token cost of a POWER run: it
waits, reads every result and re-sends its whole context on every call. Here the session only starts the harness
in the background, reads <run>/harness/gate_a.md or gate_b.md and relays it to #THE_OPERATOR#. Every agent is a headless
`claude -p --agent power-* --model ...` process (7-8k start context: no CLAUDE.md, no memory, no session), script
checks and Codex jobs are plain subprocesses (no runner agents at all), and the orchestrator's judgement at the
gates stays with the planner (the plan), the final reviewer (the review) and #THE_OPERATOR#.

Files in <run>/harness/: state.json (resumable per phase and per task), status.json ({done, total, last_item,
updated_at}: readable by a batch status reader), log.txt (one line per event), calls.jsonl (one line per agent
process: role, model, turns, tokens, cost), gate_a.md / gate_b.md (the messages for #THE_OPERATOR#, in Bulgarian).
Stops: Gate A (unless START; QUESTION risks, PRE-BROKEN or SUSPECT baseline stop even with START), Gate B (always:
the deploy is a separate command), infrastructure failures (the phase is left for `continue`).

One process per run: start/continue/verify/deploy hold <run>/harness/run.lock (pid, started_at, command); a second
process on the same run refuses, a lock whose pid is not alive is replaced (logged). POWER_MAX_AGENTS (0 = no cap)
counts every started agent in state.json (`agents_started`); at the cap the phase stops with INCOMPLETE tasks (or an
INCOMPLETE final review) and a gate text, a higher cap and `continue` resume. A failed Codex job's detached runner
is killed (codex_task.py kill) before the Claude fallback takes over.

Environment: POWER_CLAUDE (the claude command; default: `claude` on PATH), POWER_MAX_PARALLEL (6 agent
processes), POWER_MAX_AGENTS (0: no cap on started agents), POWER_PERMISSION_MODE (auto), POWER_AGENT_TIMEOUT
(3600 s per agent), POWER_BUDGET_SCALE (1.0).
"""
import argparse
import atexit
import concurrent.futures
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import power_check  # noqa: E402
from power_check import PlanError, agent_types, load_config, load_plan, read_json, write_json, write_text  # noqa: E402

PY = sys.executable
CHECK_HELPER = os.environ.get("POWER_CHECK_HELPER") or os.path.join(HERE, "power_check.py")
CODEX_HELPER = os.environ.get("POWER_CODEX_HELPER") or os.path.join(HERE, "codex_task.py")
PROMPTS_HELPER = os.path.join(HERE, "power_prompts.py")
DEPLOY_HELPER = os.environ.get("POWER_DEPLOY_HELPER") or os.path.join(HERE, "power_deploy.py")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

MAX_PARALLEL = int(os.environ.get("POWER_MAX_PARALLEL", "6"))
MAX_AGENTS = int(os.environ.get("POWER_MAX_AGENTS", "0") or 0)   # 0 = no cap on the agents a run may start
PERMISSION_MODE = os.environ.get("POWER_PERMISSION_MODE", "auto")
AGENT_TIMEOUT = int(os.environ.get("POWER_AGENT_TIMEOUT", "3600"))
BUDGET_SCALE = float(os.environ.get("POWER_BUDGET_SCALE", "1.0"))
CODEX = {"astra", "sol", "luna"}
FALLBACK = {"astra": "opus", "sol": "sonnet", "luna": "haiku"}   # same tier when a Codex job fails
OTHER = {"opus": "fable", "fable": "opus", "astra": "opus"}       # the other strong model (same-model trap)
NAMES = {"opus": "Opus", "fable": "Fable", "sonnet": "Sonnet", "haiku": "Haiku", "astra": "Astra", "sol": "Sol",
         "luna": "Luna"}
STEP1 = ("opus", "fable", "astra")
STEP2 = ("sonnet", "haiku", "opus", "fable", "sol", "luna", "astra")
STEP3 = ("opus", "fable", "astra")
# role -> (agent type role in agents/, max turns, effort, budget in USD before scaling)
ROLES = {
    "recon": ("reader", 32, "low", 2.0), "lens": ("reader", 32, "low", 2.0),
    "plan": ("executor", 55, None, 15.0), "area": ("executor", 55, None, 8.0),
    "work": ("executor", 55, None, 10.0), "fix": ("executor", 45, None, 8.0), "finish": ("executor", 55, None, 15.0),
    "verify": ("verifier", 25, None, 5.0), "review": ("executor", 55, None, 15.0),
    "finalfix": ("executor", 55, None, 10.0), "finalverify": ("verifier", 30, None, 5.0),
}
RESULT_KEYS = {"status": "DONE", "files_changed": [], "mismatch": "", "summary": "", "how_verified": "",
               "open_issues": [], "plan_change": ""}
VERDICT_KEYS = {"pass": False, "problems": [], "plan_problem": False, "notes": ""}
RESULT_FORMAT = ('FINAL ANSWER FORMAT: your last message must be exactly one JSON object and nothing else (no prose '
                 'before or after it, no code fence): {"status": "DONE" | "PLAN_MISMATCH", "files_changed": [...], '
                 '"mismatch": "", "summary": "", "how_verified": "", "open_issues": [...], "plan_change": ""}')
VERDICT_FORMAT = ('FINAL ANSWER FORMAT: your last message must be exactly one JSON object and nothing else (no prose '
                  'before or after it, no code fence): {"pass": true | false, "problems": ["file:line - what is wrong '
                  '- what is expected", ...], "plan_problem": true | false, "notes": ""}')
FILES_FORMAT = "When the files are written, answer with one line that says which files you wrote."


class AgentCap(Exception):
    """POWER_MAX_AGENTS reached: no agent may start; the phase stops with INCOMPLETE and `continue` resumes."""


LOCK_BYTE = 1 << 30   # the locked byte lies far past the record, so `status` can read the record while it is held


def os_lock_try(fd):
    """Take the OS lock of run.lock on this open handle without waiting: True = ours now, False = held elsewhere.
    Windows: msvcrt.locking on one byte at LOCK_BYTE (a region lock, released when the handle closes or the process
    dies); elsewhere fcntl.flock(LOCK_EX | LOCK_NB). The lock lives on the handle, not on the file's name or content."""
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, LOCK_BYTE, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def os_lock_release(fd):
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, LOCK_BYTE, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def pid_alive(pid):
    """Is a process with this pid running? (the lock holder check; a pid of another user counts as alive)"""
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
            return k32.GetLastError() == 5        # ERROR_ACCESS_DENIED: it exists, another user owns it
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
    return True


def now():
    return datetime.now().isoformat(timespec="seconds")


def fwd(p):
    return str(p).replace("\\", "/")


def k(n):
    return f"{n / 1000:.0f}k"


def norm(p):
    p = fwd(p or "").lower()
    return p[2:] if p.startswith("./") else p.lstrip("/")


def parse_json(text):
    """The JSON object in an agent's final message: fenced, or the outermost braces."""
    if not text:
        return None
    cands = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)]
    i, j = text.find("{"), text.rfind("}")
    if 0 <= i < j:
        cands.append(text[i:j + 1])
    for c in reversed(cands):
        try:
            d = json.loads(c)
        except ValueError:
            continue
        if isinstance(d, dict):
            return d
    return None


def shaped(d, keys):
    """Fill the missing keys of a result/verdict with their defaults; None when it is not that shape at all."""
    if not isinstance(d, dict):
        return None
    if "status" in keys and d.get("status") not in ("DONE", "PLAN_MISMATCH", "CODEX_FAILED"):
        return None
    if "pass" in keys and not isinstance(d.get("pass"), bool):
        return None
    out = {}
    for key, default in keys.items():
        v = d.get(key, default)
        if isinstance(default, list):
            v = [str(x) for x in v] if isinstance(v, list) else ([str(v)] if v else [])
        elif isinstance(default, bool):
            v = bool(v)
        elif isinstance(default, str):
            v = "" if v is None else str(v)
        out[key] = v
    return out


# ---------- state ----------

class Run:
    def __init__(self, run_dir, project_dir=None):
        self.run_dir = fwd(os.path.abspath(run_dir))
        self.project_dir = fwd(os.path.abspath(project_dir)) if project_dir else None
        self.hdir = os.path.join(self.run_dir, "harness")
        os.makedirs(self.hdir, exist_ok=True)
        self.lock = threading.RLock()
        self.sem = threading.Semaphore(MAX_PARALLEL)
        self.running = {}
        self._lock_fd = None
        # read without the run lock (status, cost); the commands that write call hold(), which reads it again
        self.state = read_json(os.path.join(self.hdir, "state.json"), None) or {}
        if self.project_dir is None:
            self.project_dir = self.state.get("project_dir")
        self.claude = shlex.split(os.environ["POWER_CLAUDE"]) if os.environ.get("POWER_CLAUDE") else \
            [shutil.which("claude") or "claude"]

    # -- one process per run (5.1; V2-07: ownership is an OS lock on an open handle, not the file's existence)
    @property
    def lock_path(self):
        return os.path.join(self.hdir, "run.lock")

    def lock_holder(self):
        """(record, held): the last {pid, started_at, command, host} written into run.lock, and whether some process
        holds the OS lock on it right now. The record is information for `status` and the log; `held` is the truth.
        (None, False) without a lock file. An unreadable record with a held lock is a holder mid-publication."""
        if not os.path.exists(self.lock_path):
            return None, False
        rec = read_json(self.lock_path, None)
        if not isinstance(rec, dict):
            rec = {"pid": "?", "command": "?", "started_at": "?"}
        if self._lock_fd is not None:
            return rec, True   # our own handle holds it
        try:
            fd = os.open(self.lock_path, os.O_RDWR)
        except OSError:
            return rec, True   # cannot even open it: treat as held
        try:
            if os_lock_try(fd):
                os_lock_release(fd)
                return rec, False
            return rec, True
        finally:
            os.close(fd)

    def hold(self, command):
        """Take run.lock for this process or refuse: two harnesses on one run would start the same agents twice and
        write the same state. The lock is the OS's (msvcrt.locking / fcntl.flock on an open handle): it is held for the
        life of this process and vanishes with it, so a crashed holder needs no cleanup and a lock file that is empty,
        unreadable or carries a dead pid is never "stale" by itself - only the OS lock decides. The record inside
        the file (pid, command, since) is written after the lock is taken and serves `status` and the refusal text.
        After the lock, state.json is read again: it is the state as of this holder, not of the process before."""
        if self._lock_fd is not None:
            return   # already held by this process
        host = os.environ.get("COMPUTERNAME") or (os.uname().nodename if hasattr(os, "uname") else "")
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR)
        if not os_lock_try(fd):
            os.close(fd)
            # the holder writes its record right after taking the lock: give it a moment, so the refusal names the
            # live holder and not the record of an earlier one
            rec = {}
            for _ in range(20):
                rec = read_json(self.lock_path, None)
                rec = rec if isinstance(rec, dict) else {}
                if pid_alive(rec.get("pid")) and int(rec.get("pid") or 0) != os.getpid():
                    break
                time.sleep(0.05)
            who = (f"pid {rec.get('pid')}, `{rec.get('command', '?')}` since {rec.get('started_at', '?')}" if pid_alive(rec.get("pid"))
                   else f"a process whose record is not published yet (last record: pid {rec.get('pid', '?')}, `{rec.get('command', '?')}`)")
            sys.exit(f"the run is locked by another harness process: {who} ({fwd(self.lock_path)}); wait for it to end "
                     "(the lock is released by the OS when that process ends, nothing to clean up)")
        self._lock_fd = fd
        atexit.register(self.release)
        prev = read_json(self.lock_path, None)
        if isinstance(prev, dict) and prev.get("pid") not in (None, os.getpid()):
            self.log(f"lock record from pid {prev.get('pid')} (`{prev.get('command')}` since {prev.get('started_at')}, "
                     f"{'still running' if pid_alive(prev.get('pid')) else 'not running'}, lock not held) replaced by pid {os.getpid()}")
        mine = {"pid": os.getpid(), "started_at": now(), "command": command, "host": host}
        data = json.dumps(mine).encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, data)
        try:
            os.fsync(fd)
        except OSError:
            pass
        # the state is read under the lock (V2-07): whatever the previous holder wrote last is what we continue from
        self.state = read_json(os.path.join(self.hdir, "state.json"), None) or {}
        if self.project_dir is None:
            self.project_dir = self.state.get("project_dir")

    def release(self):
        fd, self._lock_fd = self._lock_fd, None
        if fd is None:
            return
        try:
            os_lock_release(fd)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    # -- the cap on started agents (5.2)
    def agent_slot(self, label, model):
        """Count one more started agent (atomic in state.json); at POWER_MAX_AGENTS raise AgentCap instead of starting."""
        with self.lock:
            n = int(self.state.get("agents_started") or 0)
            if MAX_AGENTS and n >= MAX_AGENTS:
                self.log(f"{label}: not started, agent cap reached ({n} agents started, POWER_MAX_AGENTS={MAX_AGENTS})")
                raise AgentCap(f"{n} agents started, POWER_MAX_AGENTS={MAX_AGENTS}; raise the cap and `continue`")
            self.state["agents_started"] = n + 1
            self.running[label] = model
        self.save()
        self.status(f"{label} ({model})")

    # -- persistence
    def save(self):
        with self.lock:
            self.state["updated_at"] = now()
            write_json(os.path.join(self.hdir, "state.json"), self.state)

    def log(self, msg):
        line = f"{now()} {msg}"
        with self.lock:
            with open(os.path.join(self.hdir, "log.txt"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        print(line, flush=True)

    def status(self, last_item=None):
        """status.json: the batch convention {done, total, last_item, updated_at} plus the running agents."""
        with self.lock:
            tasks = self.state.get("tasks", {})
            total = len(self.state.get("plan_tasks", []) or tasks)
            done = sum(1 for t in tasks.values() if t.get("status"))
            data = {"phase": self.state.get("phase"), "done": done, "total": total,
                    "last_item": last_item or self.state.get("last_item", ""), "updated_at": now(),
                    "running": sorted(self.running), "cost_usd": round(self.cost_total(), 2),
                    "agents_started": int(self.state.get("agents_started") or 0), "max_agents": MAX_AGENTS}
            if last_item:
                self.state["last_item"] = last_item
            write_json(os.path.join(self.hdir, "status.json"), data)

    def phase(self, name, **extra):
        with self.lock:
            self.state["phase"] = name
            self.state.update(extra)
        self.save()
        self.status(f"phase {name}")
        self.log(f"PHASE {name}")

    # -- cost
    def record_call(self, rec):
        with self.lock:
            with open(os.path.join(self.hdir, "calls.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def calls(self):
        out = []
        try:
            with open(os.path.join(self.hdir, "calls.jsonl"), encoding="utf-8") as f:
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass
        return out

    def cost_total(self):
        return sum(float(c.get("cost_usd") or 0) for c in self.calls())

    def cost_lines(self):
        """The lines for REPORT.md: like power_cost.py, but from the harness's own records."""
        cs = self.calls()
        if not cs:
            return ["HARNESS: no agents yet"]
        by = {}
        for c in cs:
            b = by.setdefault(c["role"], {"n": 0, "turns": 0, "input": 0, "out": 0, "usd": 0.0, "dead": 0})
            b["n"] += 1
            b["turns"] += int(c.get("turns") or 0)
            b["input"] += int(c.get("input") or 0)
            b["out"] += int(c.get("output") or 0)
            b["usd"] += float(c.get("cost_usd") or 0)
            b["dead"] += 0 if c.get("ok") else 1
        lines = [f"HARNESS: {len(cs)} agents, {sum(b['turns'] for b in by.values())} turns, input "
                 f"{k(sum(b['input'] for b in by.values()))}, output {k(sum(b['out'] for b in by.values()))}, "
                 f"${sum(b['usd'] for b in by.values()):.2f} (API price proxy; 0 on a subscription for some models, "
                 f"the input tokens are the measure), {sum(b['dead'] for b in by.values())} failed"]
        for role, b in sorted(by.items(), key=lambda kv: -kv[1]["usd"]):
            lines.append(f"  {role:<11} {b['n']:>3} agents / {b['turns']:>4} turns / input {k(b['input']):>7} / ${b['usd']:.2f}")
        lines.append("MAIN LOOP: none (harness; the session only relayed the gates)")
        return lines

    # -- the models of the run
    @property
    def models(self):
        return self.state["models"]

    def escalate_model(self, author=None):
        """The planner finishes a rejected task, unless it is the model that really wrote the rejected code (`author`,
        after any Codex fallback; the configured step 2 before any work ran) - then the other strong Claude (V2-13)."""
        m = self.models
        author = author or m["step2"]
        return OTHER[m["step1"]] if m["step1"] == author else m["step1"]

    def authors(self):
        """The models that really wrote code kept in the tree (V2-13, V3-02): the accumulated `authors` of every task
        outcome (work, fix and finish after any Codex fallback - a restore to the snapshot drops the earlier ones;
        older outcomes have only `author`), plus every final fixer whose DONE fix is in the tree (this generation
        and the archived ones); the configured step 2 before any task finished."""
        out = set()
        for o in (self.state.get("tasks") or {}).values():
            out.update(o.get("authors") or ([o["author"]] if o.get("author") else []))
        for final in [self.state.get("final") or {}] + list(self.state.get("final_history") or []):
            ff = final.get("finalfix") or {}
            if ff.get("by") and (ff.get("result") or {}).get("status") == "DONE":
                out.add(ff["by"])
        return sorted(out) if out else [self.models["step2"]]

    def review_choice(self):
        """(reviewer, fallback, authors) for the final review: the configured step 3 when it wrote nothing; else the
        first of the other strong models (the other Claude, then Astra when the run has it) that wrote nothing. When
        every strong model wrote retained code there is no independent reviewer: step 3 reviews and the review is
        recorded as not independent (a hard stop; the orchestrator reviews by hand or changes step 3) (V2-13)."""
        m = self.models
        authors = self.authors()
        step3 = m["step3"]
        candidates = [step3, OTHER.get(step3, "opus")] + [x for x in ("opus", "fable") if x not in (step3, OTHER.get(step3))]
        if self.codex_slug("astra"):
            candidates.append("astra")
        model = next((x for x in candidates if x not in authors), step3)
        fallback = next((x for x in ("fable", "opus") if x not in authors and x != model), None) or other_claude(model if model in ("opus", "fable") else m["step2"])
        return model, fallback, authors

    def final_model(self):
        return self.review_choice()[0]

    def note_plan_change(self, task, kind, n, r, by=None):
        """V2-15: append-only history of every reported plan_change (work / fix / finish / finalfix, or the
        orchestrator's own resolution); a later empty field never erases it; the same report is kept once."""
        text = str((r or {}).get("plan_change") or "").strip() if isinstance(r, dict) else str(r or "").strip()
        if not text:
            return None
        by = by or (r.get("by") if isinstance(r, dict) else None) or "-"
        with self.lock:
            hist = self.state.setdefault("plan_changes", [])
            for h in hist:
                if h["task"] == task and h["kind"] == kind and h["text"] == text:
                    return h
            h = {"id": len(hist) + 1, "task": task, "kind": kind, "round": n, "by": by, "text": text, "at": now()}
            hist.append(h)
        self.log(f"plan change #{h['id']} reported by {by} ({kind} {task}): {text[:160]}")
        return h

    def plan_changes_for(self, task):
        return [h for h in self.state.get("plan_changes") or [] if h["task"] == task]

    def verifier_for(self, author):
        step1 = self.models["step1"]
        if author != step1:
            return step1
        return "fable" if author == "opus" else "opus"

    def codex_slug(self, fam):
        return (self.state.get("codex_models") or {}).get(fam)

    def agent_type(self, role):
        return (self.state.get("agent_types") or {}).get(role)

    # ---------- processes ----------
    def run_py(self, helper, sub, extra=(), timeout=1800, project=True):
        cmd = [PY, helper, sub, "--run-dir", self.run_dir]
        if project:
            cmd += ["--project-dir", self.project_dir]
        cmd += list(extra)
        env = dict(os.environ, PYTHONUTF8="1")
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                               cwd=self.project_dir, timeout=timeout, creationflags=CREATE_NO_WINDOW)
            return p.returncode, (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
        except subprocess.TimeoutExpired:
            return -1, f"TIMEOUT after {timeout} s: {' '.join(cmd[1:4])}"
        except OSError as e:
            return -1, f"cannot start {helper}: {e}"

    @staticmethod
    def tag(output, name):
        hits = [ln[len(name) + 1:].strip() for ln in output.splitlines() if ln.startswith(name + ":")]
        return hits[-1] if hits else None

    @staticmethod
    def tag_json(output, name):
        v = Run.tag(output, name)
        if v is None:
            return None
        try:
            return json.loads(v)
        except ValueError:
            return None

    @staticmethod
    def last_line(output):
        lines = [ln for ln in output.splitlines() if ln.strip()]
        return lines[-1][:200] if lines else "(no output)"

    def claude_call(self, role, model, prompt, label, want=None, agent_override=None):
        """One headless agent. want: 'result' | 'verdict' | None (a file-writing agent; the text is returned).
        Returns the shaped dict (or the text), None when the agent died or returned nothing usable."""
        agent_role, turns, effort, budget = ROLES[role]
        agent = agent_override or self.agent_type(agent_role)
        cmd = list(self.claude) + ["-p", "--output-format", "json", "--no-session-persistence", "--model", model,
                                   "--max-turns", str(turns), "--permission-mode", PERMISSION_MODE]
        if agent:
            cmd += ["--agent", agent]
        if effort:
            cmd += ["--effort", effort]
        if budget and BUDGET_SCALE > 0:
            cmd += ["--max-budget-usd", f"{budget * BUDGET_SCALE:.2f}"]
        fmt = {"result": RESULT_FORMAT, "verdict": VERDICT_FORMAT}.get(want, FILES_FORMAT)
        text = prompt.rstrip() + "\n\n" + fmt
        rec = {"label": label, "role": role, "model": model, "agent": agent or "", "started": now()}
        t0 = time.time()
        self.agent_slot(label, model)   # raises AgentCap at the cap, before anything starts
        env = dict(os.environ, PYTHONUTF8="1")
        env.pop("CLAUDECODE", None)  # a nested session must not think it runs inside another one
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        try:
            with self.sem:
                p = subprocess.run(cmd, input=text, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   env=env, cwd=self.project_dir, timeout=AGENT_TIMEOUT, creationflags=CREATE_NO_WINDOW)
            raw, err, code = p.stdout, p.stderr, p.returncode
        except subprocess.TimeoutExpired:
            raw, err, code = "", f"timeout after {AGENT_TIMEOUT} s", -1
        except OSError as e:
            raw, err, code = "", f"cannot start claude: {e}", -1
        finally:
            with self.lock:
                self.running.pop(label, None)
        data = None
        try:
            data = json.loads(raw) if raw.strip() else None
        except ValueError:
            data = None
        usage = (data or {}).get("usage") or {}
        rec.update({"seconds": round(time.time() - t0, 1), "exit": code, "turns": (data or {}).get("num_turns"),
                    "cost_usd": (data or {}).get("total_cost_usd"),
                    "input": usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0),
                    "cache_read": usage.get("cache_read_input_tokens", 0), "output": usage.get("output_tokens", 0)})
        result_text = (data or {}).get("result") if isinstance((data or {}).get("result"), str) else ""
        failed = data is None or data.get("is_error") or data.get("subtype") not in (None, "success")
        if failed:
            why = (data or {}).get("subtype") or (err or raw or "no output").strip()[-300:]
            rec.update({"ok": False, "error": str(why)[:300]})
            self.record_call(rec)
            self.log(f"{label}: agent {model} died ({str(why)[:160]})")
            self.status(f"{label} died")
            return None
        structured = data.get("structured_output")
        value = result_text
        if want:
            keys = RESULT_KEYS if want == "result" else VERDICT_KEYS
            value = shaped(structured if isinstance(structured, dict) else None, keys) or shaped(parse_json(result_text), keys)
            if value is None:
                rec.update({"ok": False, "error": "no usable JSON in the answer: " + result_text[-200:]})
                self.record_call(rec)
                self.log(f"{label}: {model} returned no usable JSON ({result_text[-120:]!r})")
                return None
        rec["ok"] = True
        self.record_call(rec)
        self.log(f"{label}: {model} done in {rec['seconds']} s, {rec['turns']} turns, ${rec['cost_usd'] or 0:.2f}")
        self.status(f"{label} done")
        return value

    def codex_job(self, kind, label, extra, want, model="codex"):
        """A Codex job through codex_task.py; the command re-attaches to a running job, so it is repeated.
        `model` is the family name (astra/sol/luna) for the call record; the slug travels in `extra`."""
        self.agent_slot(label, model)   # a Codex job counts as one started agent too
        t0 = time.time()
        try:
            while True:
                code, outp = self.run_py(CODEX_HELPER, kind, list(extra) + ["--max-wait", "300"], timeout=600)
                st = self.tag(outp, "STATUS") or "FAILED"
                if st in ("RUNNING", "QUEUED"):
                    if time.time() - t0 > AGENT_TIMEOUT:
                        self.log(f"{label}: Codex job still {st} after {AGENT_TIMEOUT} s, giving up")
                        return None
                    time.sleep(0.5)   # PERF-09: the helper already waited --max-wait; re-attach right away
                    continue
                break
        finally:
            with self.lock:
                self.running.pop(label, None)
        slug = extra[list(extra).index("--model") + 1] if "--model" in list(extra) else ""
        self.record_call({"label": label, "role": {"plan-area": "area"}.get(kind, kind), "model": model, "slug": slug, "agent": "",
                          "started": now(), "seconds": round(time.time() - t0, 1), "turns": None, "cost_usd": 0,
                          "input": 0, "output": 0, "ok": st == "DONE", "error": "" if st == "DONE" else (self.tag(outp, "REASON") or "")[:300]})
        if want is None:
            self.log(f"{label}: Codex {st}: {self.tag(outp, 'RESULT') or self.tag(outp, 'REASON') or ''}")
            return {"status": st, "summary": self.tag(outp, "RESULT") or self.tag(outp, "REASON") or ""}
        r = self.tag_json(outp, "RESULT_JSON")
        keys = RESULT_KEYS if want == "result" else VERDICT_KEYS
        v = shaped(r, keys)
        self.log(f"{label}: Codex {st}" + ("" if st == "DONE" else f" ({self.tag(outp, 'REASON') or ''})"))
        return v

    def call(self, role, model, label, msg, want=None, codex=None, fallback=None, agent_override=None):
        """The one door to a model, for every phase (P2-06). `model` is a family name: a Codex family (astra, sol,
        luna) runs as a Codex job (`codex` = (codex_task.py kind, extra args)), every other as a headless claude.
        Returns (value, by): the shaped result / verdict, the text of a file-writing agent, or None when the agent
        died; and the family that actually answered, which is what the results record as author / verified_by.
        A Codex job that fails, or a Codex family in a role without a Codex job, falls back once to `fallback`
        (default FALLBACK[model]) through claude; the caller picks a fallback that is not the author of the code."""
        if model in CODEX:
            slug = self.codex_slug(model)
            why = ""
            if codex and slug:
                kind, extra = codex
                v = self.codex_job(kind, label, list(extra) + ["--model", slug], want, model)
                ok = v is not None and (v.get("status") == "DONE" if want is None else not codex_failed(v))
                if ok:
                    return v, model
                why = f"Codex {model} failed ({(v or {}).get('summary') or (v or {}).get('notes') or 'job died'})"
                # 5.3: the detached runner must not keep editing while (or after) the fallback works, and must not
                # finish later and be picked up by a re-attach: kill it and record the kill in the job's done.json.
                # V2-06A: a kill that did not confirm the runner is gone (FAILED, denied, timed out, unreadable
                # answer) means no fallback at all - a Claude agent editing under a live runner is worse than a
                # BLOCKED role. The caller gets (None, None): nobody answered and nobody may take over.
                k = self.codex_kill(kind, list(extra) + ["--model", slug], label)
                if k["result"] not in ("KILLED", "NOT_RUNNING", "NONE"):
                    self.log(f"{label}: no fallback - {why[:120]}, and the Codex runner (pid {k['pid']}) could not be killed: "
                             f"{k['detail'][:160]}; end it by hand (`codex_task.py kill --kind {kind} ...`), then `continue`")
                    return None, None
            else:
                why = f"no Codex job for {role}" if slug else f"no Codex {model} model in the run"
            fb = fallback or FALLBACK[model]
            self.log(f"{label}: {why[:160]}, {fb} takes over")
            model = fb
        return self.claude_call(role, model, msg, label, want, agent_override), model

    def codex_kill(self, kind, extra, label):
        """`codex_task.py kill` for the job of (kind, args): ends its detached runner if it still runs and writes
        `killed` into the job's done.json. Returns {result, pid, detail}: result KILLED / NOT_RUNNING / NONE when the
        runner is confirmed gone (or never ran), FAILED otherwise (also for a timed-out or unreadable kill command)."""
        code, outp = self.run_py(CODEX_HELPER, "kill", ["--kind", kind] + list(extra), timeout=120)
        k = self.tag(outp, "KILL") or "FAILED"
        if code != 0 and k in ("KILLED", "NOT_RUNNING", "NONE"):
            k = "FAILED"   # the command itself failed after printing; do not trust the tag
        pid = self.tag(outp, "PID") or "?"
        detail = self.tag(outp, "REASON") or self.last_line(outp)
        if k == "KILLED":
            self.log(f"{label}: Codex runner killed (pid {pid})")
        elif k == "NOT_RUNNING":
            self.log(f"{label}: Codex runner already gone (pid {pid}), nothing to kill")
        elif k == "NONE":
            self.log(f"{label}: Codex job never started, nothing to kill")
        else:
            self.log(f"{label}: could not kill the Codex runner (pid {pid}): {detail[:200]}")
        return {"result": k, "pid": pid, "detail": detail, "exit": code}


# ---------- task helpers (the port of power_workflow.js) ----------

def codex_failed(r):
    return not r or r.get("status") == "CODEX_FAILED" or str(r.get("notes", "")).startswith("CODEX_FAILED")


def is_infra(c):
    return not c or any(str(f).startswith("INFRA:") for f in (c.get("failures") or []))


def is_broken(c):
    fails = (c or {}).get("failures") or []
    return bool(c) and not c.get("pass") and bool(fails) and all(str(f).startswith("CHECK-BROKEN:") for f in fails)


def check_mismatch(m):
    return bool(re.match(r"^\s*CHECK:", str(m or "")))


def tagged(r, by):
    return dict(r, by=by) if r else None


def other_claude(model):
    """The strong Claude model that is not `model`: the fallback of a verifier / fixer whose Codex job failed."""
    return "fable" if model == "opus" else "opus"


def strip(r):
    return {key: r.get(key) for key in RESULT_KEYS} if r else None


def stray_files(t, r, project_dir=""):
    """Files the agent reports outside its list; absolute paths inside the project count as relative."""
    owned = {norm(f) for f in t.get("files") or []}
    root = norm(project_dir).rstrip("/") + "/" if project_dir else ""

    def rel(f):
        f = norm(f)
        return f[len(root):] if root and f.startswith(root) else f
    return [f for f in (r or {}).get("files_changed") or [] if rel(f) not in owned]


def prompt_file(run, t, kind):
    return f"{run.run_dir}/prompts/{t['id']}.{kind}.md"


def inline_prompt(path, head, tail=""):
    """PERF-04: the agent's instructions travel in the message itself - one tool round (and one re-sent context)
    less per agent than "Read <file>". The prompt file stays on disk as the audit copy and is named in the message
    (tests and mock_claude read the run dir from it). A missing file falls back to the older `Read` form."""
    try:
        with open(path, encoding="utf-8") as f:
            body = f.read().strip()
    except OSError:
        body = ""
    if not body:
        return f"{head} Read {path} and follow it exactly{tail}."
    return f"{head} Your instructions follow; follow them exactly{tail}. (Audit copy: {path} - the same text, do not read it.)\n\n{body}"


def claude_msg(run, t, kind, extra=""):
    return inline_prompt(prompt_file(run, t, kind), f"POWER {kind} for task {t['id']}.") + (f"\n\n{extra}" if extra else "")


def named_msg(run, kind, name="", tail=""):
    """The message of a run-level agent (recon, plan, area, lens, review, finalfix, finalverify): PERF-04 inline."""
    path = f"{run.run_dir}/prompts/{kind}" + (f".{name}" if name else "") + ".md"
    return inline_prompt(path, f"POWER {kind}" + (f" {name}" if name else "") + ".", tail)


class TaskLoop:
    """work -> gate -> (fix -> gate) -> escalate -> gate -> BLOCKED, one task per thread."""

    def __init__(self, run):
        self.run = run
        self.m = run.models

    def work(self, t):
        run = self.run
        r, by = run.call("work", self.m["step2"], f"work:{t['id']}", claude_msg(run, t, "work"), "result",
                         codex=("work", ["--task", t["id"]]))
        run.note_plan_change(t["id"], "work", 1, tagged(r, by))
        return tagged(r, by)

    def fix(self, t, r, v, n):
        run = self.run
        problems = list(v["problems"]) or ["(the verifier gave no details)"]
        if v.get("check") and v["check"].get("report_path"):
            problems.append(f"Full check report: {v['check']['report_path']}; diff: {v['check']['diff_path']}")
        pf = os.path.join(run.run_dir, "codex", f"{t['id']}.fix{n}.problems.json")
        write_json(pf, problems)
        extra = f"Round {n}. Your previous result: {json.dumps(strip(r), ensure_ascii=False)}\nProblems to fix:\n" + \
            "\n".join("- " + p for p in problems)
        f, by = run.call("fix", r["by"], f"fix:{t['id']}#{n}", claude_msg(run, t, "fix", extra), "result",
                         codex=("fix", ["--task", t["id"], "--round", str(n), "--problems-file", pf]))
        run.note_plan_change(t["id"], "fix", n, tagged(f, by))
        return tagged(f, by)

    def checks(self, t, n):
        run = self.run
        c = None
        for attempt in (1, 2):
            code, outp = run.run_py(CHECK_HELPER, "check", ["--task", t["id"], "--round", str(n)], timeout=660)
            c = run.tag_json(outp, "CHECK_JSON")
            if c is None:
                c = {"pass": False, "failures": [f"INFRA: {run.last_line(outp)}"], "warnings": [], "diff_path": "", "report_path": ""}
            if not is_infra(c):
                break
            run.log(f"check:{t['id']}#{n}: INFRA ({c['failures'][0][:120]})" + (", retrying" if attempt == 1 else ""))
        run.log(f"check:{t['id']}#{n}: {'PASS' if c.get('pass') else 'FAIL'}" + ("" if c.get("pass") else f" ({(c.get('failures') or ['?'])[0][:120]})"))
        return c

    def verify(self, t, n, author, c):
        run = self.run
        who = run.verifier_for(author)   # `author` is the model that really wrote it (after any Codex fallback)
        evidence = (f"Round {n}. Evidence (the mechanical checks passed):\n- diff of the task's files against the snapshot: "
                    f"{c.get('diff_path')}\n- report of the mechanical checks: {c.get('report_path')}")
        v, who = run.call("verify", who, f"verify:{t['id']}#{n}", claude_msg(run, t, "verify", evidence), "verdict",
                          codex=("verify", ["--task", t["id"], "--round", str(n)]), fallback=other_claude(author))
        if not v and who is None:
            # V2-06A: the Codex verifier failed and its runner could not be killed - no Claude verifier may start
            return {"pass": False, "infra": True, "problems": ["no verifier: the Codex job failed and its runner could not be killed "
                                                               "(see log.txt; end it by hand, then `continue`)"], "plan_problem": False, "notes": "", "by": "-"}
        if not v:
            v = run.claude_call("verify", who, claude_msg(run, t, "verify", evidence + "\n(second attempt)"), f"verify:{t['id']}#{n}", "verdict")
        if not v:
            return {"pass": False, "infra": True, "problems": ["the verifier died twice"], "plan_problem": False, "notes": "", "by": who}
        run.log(f"verify:{t['id']}#{n}: {'PASS' if v['pass'] else 'FAIL'} by {who}" + ("" if v["pass"] else f" ({(v['problems'] or ['?'])[0][:120]})"))
        return tagged(v, who)

    def gate(self, t, n, author):
        c = self.checks(t, n)
        if is_infra(c):
            return {"pass": False, "infra": True, "problems": c["failures"], "plan_problem": False, "notes": "", "by": "checks", "check": c}
        if is_broken(c):
            return {"pass": False, "broken": True, "problems": c["failures"], "plan_problem": False, "notes": "; ".join(c.get("warnings") or []), "by": "checks", "check": c}
        if not c.get("pass"):
            return {"pass": False, "problems": c["failures"], "plan_problem": False, "notes": "; ".join(c.get("warnings") or []), "by": "checks", "check": c}
        return dict(self.verify(t, n, author, c), check=c)

    def restore(self, t, keep="stash"):
        code, outp = self.run.run_py(CHECK_HELPER, "restore", ["--task", t["id"], "--keep", keep], timeout=300)
        msg = self.run.tag(outp, "RESTORE")
        if msg is None:
            return {"ok": False, "message": self.run.last_line(outp)}
        return {"ok": True, "message": "RESTORE: " + msg}

    def escalate(self, t, r, verdicts, mismatch, restored):
        run = self.run
        author = (r or {}).get("by") or self.m["step2"]   # the model that really wrote the rejected code
        esc = run.escalate_model(author)
        history = [{"round": i + 1, "by": v.get("by"), "problems": v.get("problems", [])} for i, v in enumerate(verdicts)]
        reason = f"The executor stopped with PLAN_MISMATCH: {mismatch}" if mismatch else f"The task was rejected {len(verdicts)} time(s)."
        state = (f"The executor's partial edits were rolled back: the task's files are at the snapshot state ({restored})."
                 if mismatch else "The files hold the executor's last attempt; continue from it or rewrite it.")
        hf = os.path.join(run.run_dir, "codex", f"{t['id']}.finish.history.json")
        write_json(hf, {"reason": reason, "verdicts": history})
        lines = [reason] + [f"- round {h['round']} ({h['by']}): {' | '.join(h['problems'])}" for h in history] + [state]
        if r and not mismatch:
            lines.append(f"The executor's last result: {json.dumps(strip(r), ensure_ascii=False)}")
        lines.append(f"The check reports of the earlier rounds are in {run.run_dir}/checks/{t['id']}.r*/report.md.")
        # the fallback must not be the model that wrote the code (the same-model trap, by the REAL author after any
        # Codex fallback - V2-13), so it is the other strong Claude
        e, by = run.call("finish", esc, f"escalate:{t['id']}", claude_msg(run, t, "finish", "\n".join(lines)), "result",
                         codex=("finish", ["--task", t["id"], "--history-file", hf] + (["--restored"] if mismatch else [])),
                         fallback=other_claude(author))
        run.note_plan_change(t["id"], "finish", len(verdicts) + 1, tagged(e, by))
        return tagged(e, by)

    @staticmethod
    def outcome(t, status, r, verdicts, reason="", escalated=False, strays=None, authors=None):
        """One results.json row. `authors` (V3-02) = every model whose edits are in the task's files now (work, fix,
        finish; a restore drops the earlier ones); `author` stays the last one. Without the list: the author."""
        last = verdicts[-1] if verdicts else None
        strays = sorted(set(strays or []))
        authors = sorted(set(authors or []) | ({r["by"]} if r and r.get("by") else set()))
        if status == "PASS" and strays:
            # P1-04: a file outside the task's list that the executor itself reported is never a PASS, whatever the
            # verifier said about the diff of the listed files; the orchestrator decides (restore or a plan change)
            status, reason = "BLOCKED", "edited files outside the task's list (reported by the executor): " + ", ".join(strays)
        # files changed outside every task list, seen only in the diff of the checks (another session, or an agent
        # that did not report them): the task keeps its verdict, the run gets a SUSPECT hard stop, nothing is restored
        scope = sorted({p for v in verdicts for p in ((v.get("check") or {}).get("scope_changes") or [])})
        return {
            "task": t["id"], "status": status, "escalated": bool(escalated), "reason": reason or "",
            "author": r.get("by") if r else None, "authors": authors, "verified_by": [v.get("by") for v in verdicts], "rounds": len(verdicts),
            "result": strip(r), "last_problems": (last.get("problems") if last and not last.get("pass") else []) or [],
            "notes": (last or {}).get("notes", "") or "", "stray_files": strays, "scope_changes": scope,
            "check": ({"diff_path": last["check"].get("diff_path"), "report_path": last["check"].get("report_path"),
                       "warnings": last["check"].get("warnings")} if last and last.get("check") else None),
            "finished_at": now(),
        }

    def run_task(self, t):
        run = self.run
        verdicts, strays, authors = [], [], set()   # authors: V3-02, every model whose edits are in the files now

        def O(t, status, r, verdicts, reason="", escalated=False, strays=None):
            return self.outcome(t, status, r, verdicts, reason, escalated, strays, sorted(authors))

        def note_strays(r):
            s = stray_files(t, r, run.project_dir)
            if s:
                strays.extend(s)
                run.log(f"{t['id']}: {r['by']} reports files outside its list: {', '.join(s)}")

        r = self.work(t)
        if not r:
            return O(t, "BLOCKED", None, verdicts, "the executor died")
        authors.add(r["by"])
        note_strays(r)
        mismatch = (r.get("mismatch") or "no details") if r["status"] == "PLAN_MISMATCH" else ""
        if check_mismatch(mismatch):
            return O(t, "BLOCKED", r, verdicts, f"check broken (reported by {r['by']}): {mismatch}", False, strays)
        if not mismatch:
            v = self.gate(t, 1, r["by"])
            verdicts.append(v)
            if v.get("infra"):
                return O(t, "BLOCKED", r, verdicts, "infrastructure: " + v["problems"][0], False, strays)
            if v.get("broken"):
                return O(t, "BLOCKED", r, verdicts, "check broken: " + v["problems"][0], False, strays)
            if v["pass"]:
                return O(t, "PASS", r, verdicts, "", False, strays)
            if not v.get("plan_problem"):
                f = self.fix(t, r, v, 1)
                if f:
                    r = f
                    authors.add(r["by"])
                    note_strays(r)
                    if r["status"] == "PLAN_MISMATCH":
                        mismatch = r.get("mismatch") or "no details"
                    else:
                        v = self.gate(t, 2, r["by"])
                        verdicts.append(v)
                        if v.get("infra"):
                            return O(t, "BLOCKED", r, verdicts, "infrastructure: " + v["problems"][0], False, strays)
                        if v.get("broken"):
                            return O(t, "BLOCKED", r, verdicts, "check broken: " + v["problems"][0], False, strays)
                        if v["pass"]:
                            return O(t, "PASS", r, verdicts, "", False, strays)
        if check_mismatch(mismatch):
            return O(t, "BLOCKED", r, verdicts, f"check broken (reported by {r['by']}): {mismatch}", False, strays)
        esc = run.escalate_model(r["by"])
        run.log(f"{t['id']}: escalating to {esc}" + (f" (not {self.m['step1']}: it wrote the rejected code)" if esc != self.m["step1"] else "")
                + (f" (PLAN_MISMATCH: {mismatch[:120]})" if mismatch else ""))
        restored = ""
        if mismatch:
            rs = self.restore(t)
            if not rs["ok"]:
                return O(t, "BLOCKED", r, verdicts, "infrastructure: restore failed: " + rs["message"], True, strays)
            restored = rs["message"]
            authors.clear()   # the files are back at the snapshot: nothing of the earlier authors is in them
        e = self.escalate(t, r, verdicts, mismatch, restored)
        if not e:
            return O(t, "BLOCKED", r, verdicts, "the planner model died", True, strays)
        authors.add(e["by"])
        note_strays(e)
        if e["status"] == "PLAN_MISMATCH":
            pre = f"check broken (reported by {e['by']}): " if check_mismatch(e.get("mismatch")) else ""
            return O(t, "BLOCKED", e, verdicts, pre + (e.get("mismatch") or "QUESTION: no details"), True, strays)
        v = self.gate(t, len(verdicts) + 1, e["by"])
        verdicts.append(v)
        if v.get("infra"):
            return O(t, "BLOCKED", e, verdicts, "infrastructure: " + v["problems"][0], True, strays)
        if v.get("broken"):
            return O(t, "BLOCKED", e, verdicts, "check broken: " + v["problems"][0], True, strays)
        return O(t, "PASS" if v["pass"] else "BLOCKED", e, verdicts, "" if v["pass"] else "rejected after escalation", True, strays)

    def stray_resolution(self, t, strays, n):
        """V2-10: which reported stray files are really resolved now, and how. A stray is resolved only by an act the
        harness can see: the plan lists the file for the task (tasks.json edited by the orchestrator - recorded as a
        plan change), or the file is back at the snapshot (restored by hand) / gone when the snapshot had none."""
        run = self.run
        resolved = {}
        plan = read_json(os.path.join(run.run_dir, "tasks.json"), {}) or {}
        owned_now = {norm(f.get("path")) for pt in plan.get("tasks") or [] if pt.get("id") == t["id"] for f in pt.get("files") or []}
        manifest = (read_json(os.path.join(run.run_dir, "before", "manifest.json"), {}) or {}).get("files") or {}
        for s in strays:
            if norm(s) in owned_now:
                resolved[s] = f"listed for the task in tasks.json before verify-only round {n} (plan change)"
                run.note_plan_change(t["id"], "orchestrator", n, {"plan_change": f"{s} added to the task's files before verify-only round {n}"}, by="orchestrator")
                continue
            cur = os.path.join(run.project_dir, s)
            snap = os.path.join(run.run_dir, "before", "files", s)
            entry = manifest.get(s) or manifest.get(norm(s))
            if entry is not None and not entry.get("exists") and not os.path.exists(cur):
                resolved[s] = "removed again (the snapshot had no such file)"
            elif os.path.isfile(snap) and os.path.isfile(cur):
                with open(snap, "rb") as f1, open(cur, "rb") as f2:
                    if f1.read() == f2.read():
                        resolved[s] = f"back at the snapshot before verify-only round {n}"
        for s, how in resolved.items():
            run.log(f"{t['id']}: stray {s} resolved - {how}")
        return resolved

    def verify_only(self, t):
        """Checks + verifier only (after the orchestrator fixed a check command, restored a file, changed the plan).
        The verdict is MERGED into the task's earlier outcome (V2-10): the result, author, earlier verdicts, plan
        changes and the reported stray files survive; a clean verdict releases the task only when every stray file
        is really resolved (`stray_resolution`), never by itself."""
        run = self.run
        n = int(t.get("round") or 1)
        prev = (run.state.get("tasks") or {}).get(t["id"]) or {}
        prev = prev if prev.get("status") else {}
        author = t.get("author") or prev.get("author") or self.m["step2"]
        authors = prev.get("authors") or ([prev["author"]] if prev.get("author") else [])   # V3-02: the merge keeps them
        r = dict(prev["result"], by=author) if prev.get("result") else {"by": author}
        earlier = [{"by": b, "pass": True, "problems": [], "notes": "", "check": {"scope_changes": prev.get("scope_changes") or []} if i == 0 else None}
                   for i, b in enumerate(prev.get("verified_by") or [])]
        resolved = self.stray_resolution(t, prev.get("stray_files") or [], n)
        strays = [s for s in prev.get("stray_files") or [] if s not in resolved]
        v = self.gate(t, n, author)
        verdicts = earlier + [v]
        esc = bool(prev.get("escalated"))
        if v.get("infra"):
            o = self.outcome(t, "BLOCKED", r, verdicts, "infrastructure: " + v["problems"][0], esc, strays, authors)
        elif v.get("broken"):
            o = self.outcome(t, "BLOCKED", r, verdicts, "check broken: " + v["problems"][0], esc, strays, authors)
        else:
            o = self.outcome(t, "PASS" if v["pass"] else "BLOCKED", r, verdicts, "" if v["pass"] else "rejected in verify-only mode", esc, strays, authors)
        o["stray_resolved"] = dict(prev.get("stray_resolved") or {}, **resolved)
        o["plan_changes"] = run.plan_changes_for(t["id"])
        o["verify_only_round"] = n
        return o


# ---------- phases ----------

def write_prompts(run, kinds, items=()):
    extra = []
    for name, text in items:
        extra += ["--item", f"{name}={text}"]
    code, outp = run.run_py(PROMPTS_HELPER, "write", ["--kinds", kinds] + extra, timeout=120)
    if run.tag(outp, "PROMPTS") is None or code != 0:
        raise PlanError(f"power_prompts.py write {kinds}: {run.last_line(outp)}")


def parse_items(items, default=None):
    out = []
    for it in items or []:
        name, _, text = it.partition("=")
        if not name.strip() or not text.strip():
            raise PlanError(f"--recon/--lens wants name=focus, got {it!r}")
        out.append((re.sub(r"[^A-Za-z0-9_-]+", "_", name.strip()), text.strip()))
    return out or (default or [])


def echo_line(run):
    m, fl = run.models, run.state.get("flags", {})
    parts = ["POWER" + (" START" if fl.get("start") else "") + (" DEPLOY" if fl.get("deploy") else ""),
             f"план {NAMES[m['step1']]}", f"изпълнение {NAMES[m['step2']]}", f"финал {NAMES[m['step3']]}",
             f"одобрение на плана: {'не' if fl.get('start') else 'да'}",
             f"потвърждение на деплоя: {'не' if fl.get('deploy') else 'да'}"]
    esc = run.escalate_model()
    if esc != m["step1"]:
        parts.append(f"ескалация {NAMES[esc]} (автоматично: {NAMES[m['step1']]} е и изпълнител)")
    fin = run.final_model()
    if fin != m["step3"]:
        parts.append(f"финален преглед {NAMES[fin]} (автоматично: {NAMES[m['step3']]} е и изпълнител)")
    for fam in ("step1", "step2", "step3"):
        if m[fam] in CODEX and run.codex_slug(m[fam]):
            parts.append(f"{NAMES[m[fam]]} = {run.codex_slug(m[fam])}")
    return " · ".join(parts)


def phase_recon(run, items):
    run.phase("recon")
    done = run.state.setdefault("recon", {})
    todo = [(n, f) for n, f in items if not os.path.isfile(os.path.join(run.run_dir, "recon", f"{n}.md"))]
    if not todo:
        return
    write_prompts(run, "recon", todo)

    def one(name):
        out, _ = run.call("recon", "sonnet", f"recon:{name}", named_msg(run, "recon", name))
        path = os.path.join(run.run_dir, "recon", f"{name}.md")
        if out is not None and not os.path.isfile(path) and len(out) > 200:
            write_text(path, out)  # the explorer returned the map as its answer
        ok = os.path.isfile(path)
        with run.lock:
            done[name] = {"status": "ok" if ok else "failed", "file": fwd(path) if ok else ""}
        run.save()
        return ok

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(len(todo), MAX_PARALLEL))) as ex:
        list(ex.map(one, [n for n, _ in todo]))


def plan_files_ok(run):
    r = run.run_dir
    full = os.path.isfile(os.path.join(r, "tasks.json")) and os.path.isfile(os.path.join(r, "PLAN.md"))
    skel = os.path.isfile(os.path.join(r, "skeleton.json"))
    return full, skel


def phase_plan(run, retry_note=""):
    """The planner (step 1), the areas of a skeleton plan, merge, prepare (one retry with the errors), the RECON risks."""
    run.phase("plan")
    m = run.models
    step1 = m["step1"]
    for attempt in (1, 2):
        write_prompts(run, "plan")
        note = retry_note
        msg = named_msg(run, "plan", tail=" and write the files they name")
        if note:
            msg += f"\n\n{note}"
        # a Codex plan job is idempotent (it re-attaches to the finished job), so a retry with a note goes to Claude
        run.call("plan", step1, "plan" if attempt == 1 else "plan#2", msg, None,
                 codex=None if note else ("plan", []), fallback="opus")
        full, skel = plan_files_ok(run)
        if skel and not full:
            phase_areas(run)
            full, _ = plan_files_ok(run)
        if not full:
            retry_note = "The previous planner wrote no PLAN.md + tasks.json (or skeleton.json). Write them now."
            run.log("planner wrote no plan files" + (", retrying once" if attempt == 1 else ""))
            continue
        code, outp = run.run_py(CHECK_HELPER, "prepare", timeout=300)
        run.state["prepare"] = {"ok": code == 0, "output": outp[-4000:]}
        run.save()
        if code == 0:
            break
        errors = [ln for ln in outp.splitlines() if ln.startswith("ERROR")]
        run.log(f"prepare FAIL ({len(errors)} errors)" + (", the planner fixes tasks.json once" if attempt == 1 else ""))
        retry_note = ("Your plan failed `power_check.py prepare` with these errors; fix " + run.run_dir +
                      "/tasks.json (and PLAN.md where the same text appears) so that prepare passes, change nothing else:\n" +
                      "\n".join(errors[:40]))
    else:
        run.phase("plan_failed")
        prep = run.state.get("prepare")
        if prep:
            text = ("# Планът не минава `prepare`\n\n```\n" + str(prep.get("output") or "")[-3000:] + "\n```\n\n"
                    "#THE_OPERATOR#, трябва да решиш: поправка на tasks.json на ръка и `power_run.py continue`, или `continue --replan`.\n")
        else:
            # V2-16: prepare never ran - the planner died or wrote no PLAN.md + tasks.json twice; no traceback, the
            # cause from the call records and the way on
            fails = [c for c in run.calls() if c.get("role") == "plan" and not c.get("ok")]
            why = (fails[-1].get("error") or "no usable answer") if fails else "no PLAN.md + tasks.json after the planner's answer"
            text = (f"# Планът не беше написан\n\nПланиращият модел ({NAMES.get(step1, step1)}, стъпка 1) два пъти не остави "
                    f"`PLAN.md` + `tasks.json` в {fwd(run.run_dir)}.\n\nПоследна грешка: `{str(why)[:300]}`\n\n"
                    f"Записи: {fwd(os.path.join(run.hdir, 'log.txt'))}, {fwd(os.path.join(run.hdir, 'calls.jsonl'))}.\n\n"
                    "#THE_OPERATOR#, трябва да решиш: `power_run.py continue --replan` (същият модел, нов опит) или `start --force` с друг "
                    "модел за стъпка 1; нищо в проекта не е променено.\n")
        write_text(os.path.join(run.hdir, "gate_a.md"), text)
        print(text)
        print("PLAN: FAILED (phase plan_failed; see harness/gate_a.md)", flush=True)
        return False
    plan = read_json(os.path.join(run.run_dir, "tasks.json"), {})
    risks = [str(x) for x in plan.get("risks") or []]
    recon_more = [r for r in risks if r.startswith("RECON:")]
    if recon_more and not run.state.get("recon_extended"):
        run.state["recon_extended"] = True
        run.save()
        items = [(f"more{i + 1}", r[len("RECON:"):].strip()) for i, r in enumerate(recon_more[:3])]
        run.log(f"plan asks for more recon: {len(items)} explorer(s)")
        phase_recon(run, items)
        return phase_plan(run, "The recon notes were extended per your RECON: risks (see " + run.run_dir +
                          "/recon/more*.md). Revise PLAN.md and tasks.json with them; keep what still holds.")
    run.state["plan_tasks"] = [t["id"] for t in plan.get("tasks") or []]
    run.state["questions"] = [r for r in risks if r.startswith("QUESTION:")]
    run.save()
    return True


def phase_areas(run):
    """Skeleton mode: one area planner per area in parallel, then merge."""
    skel = read_json(os.path.join(run.run_dir, "skeleton.json"), {})
    areas = [a.get("name") for a in skel.get("areas") or [] if a.get("name")]
    if not areas:
        return
    write_prompts(run, "areas")
    step1 = run.models["step1"]

    def one(name):
        run.call("area", step1, f"area:{name}", named_msg(run, "area", name, " and write the file they name"),
                 None, codex=("plan-area", ["--area", name]), fallback="opus")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(len(areas), MAX_PARALLEL))) as ex:
        list(ex.map(one, areas))
    code, outp = run.run_py(CHECK_HELPER, "merge", timeout=120, project=False)
    run.log("merge: " + (run.tag(outp, "MERGE") or run.last_line(outp)))


def phase_baseline(run):
    run.phase("baseline")
    code, outp = run.run_py(CHECK_HELPER, "baseline", timeout=3600)
    rows = {"PRE-BROKEN": [], "WEAK": [], "SUSPECT": []}
    for ln in outp.splitlines():
        for key in rows:
            if ln.startswith(key + " "):
                rows[key].append(ln[len(key) + 1:])
    run.state["baseline"] = {"summary": run.tag(outp, "BASELINE") or run.last_line(outp), "rows": rows, "output": outp[-6000:]}
    run.save()
    run.log("baseline: " + run.state["baseline"]["summary"])


def gate_a_text(run):
    plan = read_json(os.path.join(run.run_dir, "tasks.json"), {})
    tasks = plan.get("tasks") or []
    b = run.state.get("baseline", {}).get("rows", {})
    qs = run.state.get("questions", [])
    stops = []
    if qs:
        stops.append("въпроси в плана (QUESTION:)")
    if b.get("PRE-BROKEN"):
        stops.append("PRE-BROKEN проверки")
    if b.get("SUSPECT"):
        stops.append("SUSPECT проверки")
    lines = [f"# Врата A - {run.state.get('title') or os.path.basename(run.run_dir)}", "", echo_line(run), "",
             f"План: [PLAN.md]({run.run_dir}/PLAN.md) · задачи: `{run.run_dir}/tasks.json` · проучване: `{run.run_dir}/recon/`", "",
             "| id | какво | файлове | ~редове | зависи от |", "|--|--|--|--|--|"]
    for t in tasks:
        files = t.get("files") or []
        n = len(files)
        lines.append(f"| {t.get('id')} | {str(t.get('title', ''))[:70]} | {n} | {(t.get('size') or {}).get('lines', '?')} | "
                     f"{', '.join(t.get('depends_on') or []) or '-'} |")
    if plan.get("contracts"):
        lines += ["", "Контракти:"] + [f"- {c.get('id')}: {str(c.get('text', ''))[:160]} (дава {c.get('provided_by')})" for c in plan["contracts"]]
    ng = [f"- {t.get('id')}: {x}" for t in tasks for x in (t.get("non_goals") or [])[:2]]
    if ng:
        lines += ["", "Не се променя:"] + ng[:12]
    risks = [r for r in (plan.get("risks") or []) if not str(r).startswith("QUESTION:")]
    if risks:
        lines += ["", "Рискове:"] + [f"- {r}" for r in risks]
    if qs:
        lines += ["", "ВЪПРОСИ (планът чака отговор; отговорите отиват в request.md, раздел „Решения на #THE_OPERATOR#“, после `continue --replan`):"] + [f"- {q}" for q in qs]
    lines += ["", f"Baseline: {run.state.get('baseline', {}).get('summary', '-')}"]
    for key in ("PRE-BROKEN", "WEAK", "SUSPECT"):
        for row in b.get(key) or []:
            lines.append(f"- {key}: {row}")
    agents = 3 * len(tasks) + 8
    lines += ["", f"Очаквани агенти: ~{agents} (3 на задача + ~8 фиксирани)" + (" - над 50, планът е голям" if agents > 50 else ""),
              f"Файлове за CLAIM: {', '.join(sorted({f.get('path') for t in tasks for f in (t.get('files') or [])}))}"]
    if stops:
        lines += ["", "Спира дори със START: " + "; ".join(stops) + "."]
    lines += ["", "#THE_OPERATOR#, трябва да решиш дали да пусна изпълнението по този план: „да“ (после `power_run.py continue`), "
              "или промени (tasks.json / request.md „Решения на #THE_OPERATOR#“, после `continue` или `continue --replan`)."]
    return "\n".join(lines) + "\n"


def phase_ready(run):
    """prepare -> snapshot -> prompts -> args, one call (codex_task.py ready)."""
    run.phase("ready")
    m = run.models
    code, outp = run.run_py(CODEX_HELPER, "ready", ["--step1", m["step1"], "--step2", m["step2"]], timeout=600)
    args = None
    for ln in reversed(outp.splitlines()):
        if ln.startswith("{"):
            try:
                args = json.loads(ln)
            except ValueError:
                pass
            break
    if code != 0 or not args:
        raise PlanError("ready failed: " + run.last_line(outp))
    run.state["args"] = args
    run.state["codex_models"] = args.get("codex_models") or {}
    run.state["agent_types"] = args.get("agent_types") or run.state.get("agent_types") or {}
    run.save()
    run.log(f"ready: {len(args['tasks'])} tasks, snapshot and prompts in place")


def phase_work(run):
    """Every task starts as soon as its dependencies PASS (no waves); one thread per task, MAX_PARALLEL agents."""
    run.phase("work")
    args = run.state["args"]
    tasks = args["tasks"]
    by_id = {t["id"]: t for t in tasks}
    for t in tasks:
        for d in t.get("depends_on") or []:
            if d not in by_id:
                raise PlanError(f"{t['id']} depends on unknown {d}")
    outcomes = run.state.setdefault("tasks", {})
    loop = TaskLoop(run)
    futures = {}
    flock = threading.Lock()

    def finish(t, o):
        o["plan_changes"] = run.plan_changes_for(t["id"])   # V2-15: every reported change of this task, not the last result's field
        with run.lock:
            outcomes[t["id"]] = o
        run.save()
        run.status(f"{t['id']} {o['status']}")
        run.log(f"{t['id']}: {o['status']}" + (f" - {o['reason'][:160]}" if o.get("reason") else ""))
        return o

    def start(tid):
        with flock:
            if tid in futures:
                return futures[tid]
            futures[tid] = pool.submit(run_one, by_id[tid])
            return futures[tid]

    def run_one(t):
        prev = outcomes.get(t["id"])
        if prev and prev.get("status") and prev["status"] != "INCOMPLETE":
            return prev   # INCOMPLETE (stopped by the agent cap) is not final: it is redone on `continue`
        deps = [start(d).result() for d in t.get("depends_on") or []]
        waiting = [d["task"] for d in deps if d["status"] == "INCOMPLETE"]
        if waiting:
            # the dependency stopped at the agent cap, not failed: this task waits for the next `continue` too
            run.log(f"{t['id']}: not started, waits for the INCOMPLETE {', '.join(waiting)}")
            return finish(t, loop.outcome(t, "INCOMPLETE", None, [], f"waits for {', '.join(waiting)} (stopped by POWER_MAX_AGENTS)"))
        bad = [d["task"] for d in deps if d["status"] != "PASS"]
        if bad:
            run.log(f"{t['id']}: not started, depends on {', '.join(bad)}")
            return finish(t, loop.outcome(t, "BLOCKED_DEP", None, [], f"depends on {', '.join(bad)}"))
        if prev and prev.get("stage"):
            # a crashed or capped earlier attempt: its edits are in the files; back to the snapshot (kept in stash/), then again
            rs = loop.restore(t)
            run.log(f"{t['id']}: earlier attempt interrupted at {prev['stage']}; {rs['message'][:120]}")
        with run.lock:
            outcomes[t["id"]] = {"stage": "work", "started_at": now()}
        run.save()
        try:
            return finish(t, loop.run_task(t))
        except AgentCap as e:
            # 5.2: not a PASS and not a failure of the task: the files may hold a half edit (stage kept for the
            # restore on `continue`), the run stops after the other threads reach the cap too
            o = loop.outcome(t, "INCOMPLETE", None, [], f"stopped by the agent cap: {e}")
            o["stage"] = "work"
            return finish(t, o)
        except Exception as e:  # noqa: BLE001 - a bug in the loop must not take the other tasks down
            return finish(t, loop.outcome(t, "BLOCKED", None, [], f"harness error: {e}"))

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(tasks))) as pool:
        # submit every task first, then wait: `start(...).result()` inside one comprehension would run them serially
        futs = [start(t["id"]) for t in tasks]
        results = [f.result() for f in futs]
    write_results(run, results)
    count = lambda s: sum(1 for r in results if r["status"] == s)  # noqa: E731
    strays = sum(len(r.get("stray_files") or []) for r in results)
    run.log(f"PASS {count('PASS')}, BLOCKED {count('BLOCKED')}, BLOCKED_DEP {count('BLOCKED_DEP')}, escalated "
            f"{sum(1 for r in results if r.get('escalated'))}" + (f", files reported outside task lists: {strays}" if strays else "")
            + (f", INCOMPLETE {count('INCOMPLETE')} (agent cap)" if count("INCOMPLETE") else ""))
    return results


def incomplete_tasks(run):
    return sorted(tid for tid, o in (run.state.get("tasks") or {}).items() if o.get("status") == "INCOMPLETE")


def gate_cap(run, where):
    """The stop at POWER_MAX_AGENTS: the phase is left where it is (`continue` resumes it), the text goes to
    harness/gate_b.md for the session to relay, and is printed."""
    n = int(run.state.get("agents_started") or 0)
    inc = incomplete_tasks(run)
    L = [f"# Спряно: таван на агентите - {run.state.get('title') or os.path.basename(run.run_dir)}", "", echo_line(run), "",
         f"Стартирани агенти: {n}, таван POWER_MAX_AGENTS={MAX_AGENTS}. Фаза: {where}.",
         f"INCOMPLETE задачи (не са PASS, при `continue` се връщат на snapshot-а и се повтарят): {', '.join(inc) or 'няма'}."]
    if (run.state.get("final") or {}).get("status") == "INCOMPLETE":
        L.append(f"Финалният преглед е INCOMPLETE: {run.state['final'].get('reason', '')}")
    L += ["", "#THE_OPERATOR#, трябва да решиш: вдигни тавана (`POWER_MAX_AGENTS=<n>`, 0 = без таван) и пусни `power_run.py continue`, "
          "или спри run-а тук (нищо не е деплойнато)."]
    text = "\n".join(L) + "\n"
    write_text(os.path.join(run.hdir, "gate_b.md"), text)
    run.state["capped"] = {"at": now(), "agents_started": n, "max_agents": MAX_AGENTS, "phase": where}
    run.save()
    run.status("agent cap reached")
    run.log(f"agent cap: {n} agents started (POWER_MAX_AGENTS={MAX_AGENTS}); INCOMPLETE: {', '.join(inc) or '-'}; harness/gate_b.md written")
    print(text)


def write_results(run, results):
    path = os.path.join(run.run_dir, "results.json")
    old = read_json(path, [])
    old = old.get("result") if isinstance(old, dict) else old
    merged = {r["task"]: r for r in (old or []) if isinstance(r, dict) and r.get("task")}
    for r in results:
        merged[r["task"]] = r
    write_json(path, list(merged.values()))
    append_plan_changes(run)


def plan_change_line(h):
    return f"- #{h['id']} {h['task']} ({h['by']}, {h['kind']} round {h['round']}): {h['text']}"


def append_plan_changes(run):
    """4.6 / V2-15: every reported plan change (the append-only history in the state: work, fix, finish, finalfix,
    the orchestrator's stray resolutions) lands in PLAN.md "Plan changes" once, with its provenance; the report
    alone was easy to miss, and the last result's field alone lost the earlier rounds."""
    plan_md = os.path.join(run.run_dir, "PLAN.md")
    hist = run.state.get("plan_changes") or []
    if not hist or not os.path.isfile(plan_md):
        return
    with open(plan_md, encoding="utf-8") as f:
        text = f.read()
    lines = [plan_change_line(h) for h in hist if plan_change_line(h) not in text]
    if lines:
        if "## Plan changes" not in text:
            text = text.rstrip() + ("\n\n## Plan changes\n\nReported during the run (the plan said otherwise); every report is kept, "
                                    "with who reported it and in which call - reported is not approved:\n")
        text = text.rstrip() + "\n" + "\n".join(lines) + "\n"
        write_text(plan_md, text)


def phase_verify_only(run, spec):
    """After the orchestrator fixed a broken check command: only checks + verifier for the named tasks."""
    run.phase("verify")
    args = run.state.get("args") or {}
    by_id = {t["id"]: t for t in args.get("tasks") or []}
    sel = []
    for item in spec.split(","):
        parts = item.strip().split(":")
        if len(parts) != 3 or parts[0] not in by_id:
            raise PlanError(f"--tasks wants id:author:round for a known task, got {item!r}")
        sel.append(dict(by_id[parts[0]], depends_on=[], author=parts[1], round=int(parts[2])))
    loop = TaskLoop(run)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(sel))) as pool:
        results = list(pool.map(loop.verify_only, sel))
    outcomes = run.state.setdefault("tasks", {})
    for r in results:
        outcomes[r["task"]] = r
        run.log(f"{r['task']}: {r['status']} (verify-only)" + (f" - {r['reason'][:120]}" if r.get("reason") else ""))
    # P2-05: a task that was BLOCKED_DEP only because of tasks that now PASS (or are released here) is released too,
    # so `continue` runs it; the PASS tasks keep their outcome and are never executed again
    released = []
    while True:
        more = [tid for tid, o in outcomes.items() if o.get("status") == "BLOCKED_DEP" and tid in by_id
                and all((outcomes.get(d) or {}).get("status") == "PASS" or d in released for d in by_id[tid].get("depends_on") or [])]
        if not more:
            break
        for tid in more:
            outcomes.pop(tid)
            released.append(tid)
    run.save()
    write_results(run, results)
    run.log(f"verify-only: PASS {sum(1 for r in results if r['status'] == 'PASS')} of {len(results)}"
            + (f"; released for the work loop: {', '.join(released)}" if released else ""))
    run.phase("work" if released else "work_done")
    return results


def lens_items(run):
    items = run.state.get("lenses")
    if items:
        return [(x["name"], x["focus"], x.get("agent_type")) for x in items]
    cfg = load_config(run.project_dir)
    out = [(x.get("name"), x.get("focus", ""), x.get("agent_type")) for x in cfg.get("lenses") or [] if x.get("name")]
    if not out:
        out = [("domain", "the project's traps and rules from its CLAUDE.md, applied to this diff", None),
               ("smoke", "local smoke of the affected pages and routes; on a UI change desktop and mobile screenshots", None)]
    return out


def installed_agent(run, name):
    for d in (os.path.join(run.project_dir, ".claude", "agents"), os.path.join(os.path.expanduser("~"), ".claude", "agents")):
        if name and os.path.isfile(os.path.join(d, name + ".md")):
            return name
    return None


REVIEW_FILES = ("review.json", "REVIEW.md", "DEPLOY.md")


def new_review_generation(run):
    """V2-04: every entry into the final review is a new generation. The previous `final` block (its checks, review,
    fixer and verifier evidence) moves to `final_history`; the previous generation's review files and lens reports
    are moved to <run>/archive/final.g<N>/ (deploy.json is copied, it stays the plan's input until the new reviewer
    rewrites it), so nothing a gate reads can come from an older working tree or an older reviewer."""
    prev = run.state.get("final") or {}
    gen = int(prev.get("generation") or 0) + 1
    if prev:
        prev = dict(prev, archived_at=now())
        prev.setdefault("generation", gen - 1)
        run.state.setdefault("final_history", []).append(prev)
        run.state["final_history"] = run.state["final_history"][-10:]
    dest = os.path.join(run.run_dir, "archive", f"final.g{gen - 1}")
    moved = []
    for name in REVIEW_FILES + ("review",):
        src = os.path.join(run.run_dir, name)
        if os.path.exists(src):
            os.makedirs(dest, exist_ok=True)
            target = os.path.join(dest, name)
            if os.path.exists(target):
                shutil.rmtree(target) if os.path.isdir(target) else os.remove(target)
            shutil.move(src, target)
            moved.append(name)
    dj = os.path.join(run.run_dir, "deploy.json")
    if moved and os.path.isfile(dj):
        shutil.copy2(dj, os.path.join(dest, "deploy.json"))
    run.state["final"] = {"generation": gen, "started_at": now()}
    run.save()
    if moved:
        run.log(f"final review generation {gen}: generation {gen - 1} evidence archived in {fwd(dest)} ({', '.join(moved)})")
    return run.state["final"]


def phase_final(run):
    """check --all + the lenses in parallel, the final review (step 3), deploy plan, finalfix / finalverify.
    At the agent cap the block ends as INCOMPLETE (not a verdict) and `continue` runs the review again, as a new
    generation: no check, verdict or fix of an earlier generation counts for this one (V2-04)."""
    run.phase("review")
    final = new_review_generation(run)
    try:
        _phase_final(run, final)
    except AgentCap as e:
        final["status"] = "INCOMPLETE"
        final["reason"] = f"stopped by the agent cap: {e}"
        run.save()
        run.log(f"final: INCOMPLETE - {final['reason']}")


def _phase_final(run, final):
    gen = ["--gen", str(final.get("generation") or 1)]   # Codex job names carry the generation: no finished job of an older one is reused
    lenses = lens_items(run)
    write_prompts(run, "lens", [(n, f) for n, f, _ in lenses])

    def check_all():
        code, outp = run.run_py(CHECK_HELPER, "check", ["--all", "--round", "final"], timeout=3600)
        c = run.tag_json(outp, "CHECK_JSON") or {"pass": False, "failures": [f"INFRA: {run.last_line(outp)}"], "warnings": []}
        final["check_final"] = c
        run.log("check --all final: " + ("PASS" if c.get("pass") else f"FAIL ({(c.get('failures') or ['?'])[0][:120]})"))

    def lens(item):
        name, focus, atype = item
        agent = installed_agent(run, atype)
        msg = named_msg(run, "lens", name)
        # a project agent type (e.g. php-reviewer) gets the same message with its own prompt and tools
        out, _ = run.call("lens", "sonnet", f"lens:{name}", msg, agent_override=agent)
        path = os.path.join(run.run_dir, "review", f"{name}.md")
        if out is not None and not os.path.isfile(path):
            write_text(path, out)
        final.setdefault("lenses", {})[name] = fwd(path) if os.path.isfile(path) else ""

    # check --all first: it writes checks/final/diff.patch, which every lens reads (13.10: lenses that started in
    # parallel with it found no diff and reviewed the working tree blind)
    check_all()
    cf = final.get("check_final") or {}
    if not cf.get("pass") and not run.state.get("diagnose"):
        # PERF-02: a tree whose full check fails (a red check, a failed smoke, INFRA) cannot be CLEAN whatever the
        # lenses and the reviewer say (V2-04), so none of them runs: the status is FAIL with the failure as the
        # reason, nothing is uploaded, and the orchestrator fixes the check / the code and runs `continue`.
        # `continue --diagnose` runs the full review on the failing tree when the diagnosis itself is wanted.
        why = str((cf.get("failures") or ["the check did not run"])[0])[:200]
        _, _, authors = run.review_choice()
        final["lenses"] = {}
        final["review"] = {"by": "-", "verdict": "SKIPPED", "findings": [], "authors": authors, "independent": True}
        final["deploy_plan"] = {"ok": False, "line": "not evaluated (the final check failed; lenses and review skipped)", "output": ""}
        final["short_circuit"] = True
        final["status"] = "FAIL"
        final["reason"] = "check --all final: " + why
        run.save()
        run.log(f"final: FAIL - {final['reason']} (lenses and the final review skipped, PERF-02; `continue --diagnose` runs them)")
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(lenses))) as ex:
        fs = [ex.submit(lens, it) for it in lenses]
        for f in fs:
            f.result()
    run.save()
    # the final review (step 3, shifted when it wrote retained code - by the REAL authors in the results, V2-13)
    write_prompts(run, "review")
    step2 = run.models["step2"]
    reviewer, review_fb, authors = run.review_choice()
    if reviewer != run.models["step3"]:
        run.log(f"review: {reviewer} instead of {run.models['step3']} (it wrote code: authors {', '.join(authors)})")
    answer, who = run.call("review", reviewer, "review", named_msg(run, "review", tail=" and write the files they name"),
                           None, codex=("review", gen), fallback=review_fb)
    independent = who is not None and who not in authors
    # the verdict counts only when this generation's reviewer finished and wrote review.json now (the previous
    # generation's file was archived before the call; a dead reviewer leaves none)
    rv = read_json(os.path.join(run.run_dir, "review.json"), None) if answer is not None else None
    if not isinstance(rv, dict):
        final["review"] = {"by": who or "-", "verdict": "MISSING", "findings": [], "authors": authors, "independent": independent}
        run.log("the final reviewer " + ("died" if answer is None else "wrote no review.json"))
    else:
        final["review"] = {"by": who, "verdict": rv.get("verdict"), "findings": rv.get("findings") or [], "authors": authors, "independent": independent}
        run.log(f"review by {who}: {rv.get('verdict')}, {len(rv.get('findings') or [])} findings"
                + ("" if independent else f" - NOT independent: {who} also wrote code (authors {', '.join(authors)})"))
    run.save()
    # the deploy plan is validated right away (fetch is the deploy command's job)
    if os.path.isfile(os.path.join(run.run_dir, "deploy.json")):
        code, outp = run.run_py(DEPLOY_HELPER, "plan", timeout=120)
        final["deploy_plan"] = {"ok": code == 0, "line": run.tag(outp, "PLAN") or run.last_line(outp), "output": outp[-3000:]}
        run.log("deploy plan: " + final["deploy_plan"]["line"])
    else:
        final["deploy_plan"] = {"ok": False, "line": "no deploy.json (manual deploy per the project docs)", "output": ""}
    # critical / high / medium findings: step 1 fixes, re-check, the other model verifies
    serious = [f for f in final["review"]["findings"] if str(f.get("severity", "")).lower() in ("critical", "high", "medium")]
    if serious:
        write_prompts(run, "finalfix,finalverify")
        # step 1 fixes (as a Codex job when it is a Codex family); the fallback is a strong Claude that wrote none of
        # the code when one exists (the real authors, not the configured step 2), else the other Claude
        fix_fb = next((x for x in ("fable", "opus") if x not in authors), None) or other_claude(step2)
        r, fixer = run.call("finalfix", run.models["step1"], "finalfix", named_msg(run, "finalfix"),
                            "result", codex=("finalfix", gen), fallback=fix_fb)
        final["finalfix"] = {"by": fixer, "result": strip(r) if r else None}
        run.note_plan_change("final", "finalfix", int(final.get("generation") or 1), tagged(r, fixer))
        append_plan_changes(run)
        if not r:
            final["status"] = "BLOCKED"
            final["reason"] = "the final fixer died"
        elif r.get("status") != "DONE":
            # 4.3: a fixer that could not apply the findings as written is a stop, not a pass
            final["status"] = "BLOCKED"
            final["reason"] = f"the final fixer reported {r.get('status')}: {(r.get('mismatch') or 'no details')[:200]}"
        else:
            code, outp = run.run_py(CHECK_HELPER, "check", ["--all", "--round", "final2"], timeout=3600)
            c = run.tag_json(outp, "CHECK_JSON") or {"pass": False, "failures": [f"INFRA: {run.last_line(outp)}"], "warnings": []}
            final["check_final2"] = c
            if not c.get("pass"):
                final["status"] = "BLOCKED"
                final["reason"] = "checks after the final fix: " + (c.get("failures") or ["?"])[0][:200]
            else:
                evidence = (f"Evidence: the diff after the fixes {c.get('diff_path')}, the check report {c.get('report_path')}, "
                            f"the findings in {run.run_dir}/review.json.")
                v, ver = run.call("finalverify", run.verifier_for(fixer), "finalverify",
                                  named_msg(run, "finalverify") + f"\n\n{evidence}", "verdict",
                                  codex=("finalverify", gen), fallback=other_claude(fixer))
                final["finalverify"] = {"by": ver, "verdict": v}
                if not v or not v["pass"]:
                    final["status"] = "BLOCKED"
                    final["reason"] = "final verification: " + ((v or {}).get("problems") or ["the verifier died"])[0][:200]
                else:
                    final["status"] = "FIXED"
    else:
        verdict = final["review"]["verdict"]
        cf = final.get("check_final") or {}
        if verdict == "MISSING":
            final["status"] = "MISSING"
        elif verdict not in ("PASS", "PASS_WITH_FIXES"):
            final["status"] = "FAIL"
        elif not cf.get("pass"):
            # V2-04: a clean verdict on a tree whose full check fails is not CLEAN (this generation's check counts)
            final["status"] = "FAIL"
            final["reason"] = "check --all final: " + ((cf.get("failures") or ["not run"])[0])[:200]
        else:
            final["status"] = "CLEAN"
    run.save()
    run.log(f"final: {final['status']}" + (f" - {final.get('reason', '')}" if final.get("reason") else ""))


def hard_stops(run, fetch_line=None):
    """The hard stops of SKILL section 8 that the harness can see (the rest is the orchestrator's judgement)."""
    stops = []
    tasks = run.state.get("tasks", {})
    blocked = [tid for tid, o in tasks.items() if o.get("status") in ("BLOCKED", "BLOCKED_DEP")]
    if blocked:
        stops.append("BLOCKED задачи: " + ", ".join(blocked) + " (само частичен деплой, с изричен въпрос)")
    inc = incomplete_tasks(run)
    if inc:
        stops.append("INCOMPLETE задачи (спрени от тавана POWER_MAX_AGENTS, не са проверени): " + ", ".join(inc) + " (вдигни тавана и `continue`)")
    final = run.state.get("final", {})
    if final.get("status") not in ("CLEAN", "FIXED"):
        # V3-05: fail-closed - only the two statuses that mean "closed" pass; BLOCKED / FAIL / MISSING / INCOMPLETE,
        # a status the harness never writes, an empty or missing one (a review that is still running or was never
        # run) are all a stop, whatever the older fields of the block say
        stops.append(f"финалният преглед не е затворен (поколение {final.get('generation') or '-'}: статус {final.get('status') or 'липсва'}"
                     + (f": {str(final.get('reason', ''))[:120]}" if final.get("reason") else "") + ")")
    rv = final.get("review") or {}
    if rv and rv.get("independent") is False:
        # V2-13: every strong model wrote retained code; the reviewer is also an author (policy: never own code)
        stops.append(f"финалният преглед не е независим: {rv.get('by')} е и автор на код (автори: {', '.join(rv.get('authors') or [])}); "
                     "прегледай сам или пусни нов преглед с друг модел")
    # 4.2: the latest full check counts (final2 after a final fix); a check that never ran is not a pass
    rnd, cf = latest_check(final)
    if cf is None:
        stops.append("липсва финална проверка (check --all final не е пускана)")
    elif not cf.get("pass"):
        stops.append(f"паднала финална проверка (check --all {rnd})")
    suspect = suspect_files(run)
    if suspect:
        stops.append("SUSPECT: файлове, променени извън списъците на задачите (видени само в diff-а): " + ", ".join(suspect) +
                     " (друга сесия или агент извън списъка си; нищо не е възстановено автоматично, оркестраторът решава)")
    dep = read_json(os.path.join(run.run_dir, "deploy.json"), {}) or {}
    if dep.get("db"):
        stops.append(f"{len(dep['db'])} DB/опция/флаг стъпки (само след изрично „да“, оркестраторът ги пуска)")
    for p in (f.get("local", "") for f in dep.get("files") or []):
        if re.search(r"(^|/)(\.env|wp-config\.php)|_secrets/|/secrets/", str(p)):
            stops.append(f"деплой на чувствителен файл: {p}")
    tok = deploy_candidate(run)
    for n, text, stale in unanswered_questions(run, dep):
        line = f"неотговорен въпрос №{n} от deploy.json: {text} (отговор: `deploy --yes {n} --approved {tok or '<кандидат>'}`; --force не го покрива)"
        if stale:
            line += f" - има „да“ от {stale.get('at')}, но за друг кандидат ({stale.get('candidate') or 'без token'}); сегашният е {tok}"
        stops.append(line)
    if fetch_line and ("conflict" in fetch_line.lower() or "FAIL" in fetch_line):
        stops.append("fetch: конфликт или неподдържан файл")
    return stops


def latest_check(final):
    """(round, check) of the last `check --all` of the final review: final2 when a final fix ran, else final."""
    for rnd in ("final2", "final"):
        if final.get(f"check_{rnd}") is not None:
            return rnd, final[f"check_{rnd}"]
    return None, None


def suspect_files(run):
    """Files changed outside every task list, seen in the diff of any check (the tasks' rounds and the final ones)."""
    out = set()
    for o in (run.state.get("tasks") or {}).values():
        out.update(o.get("scope_changes") or [])
    final = run.state.get("final", {})
    for rnd in ("final", "final2"):
        out.update((final.get(f"check_{rnd}") or {}).get("scope_changes") or [])
    return sorted(out)


def deploy_candidate(run):
    """The candidate token (attempt:hash) of the last deploy fetch, or None (no fetch yet, or an older state)."""
    return deploy_state(run).get("candidate")


def unanswered_questions(run, dep=None):
    """deploy.json needs_yes items (1-based) without a yes for the CURRENT candidate: (n, text, stale answer or None).
    A yes is recorded with the candidate token it was given for (`deploy --yes N --approved T`); after a new fetch
    that produced another candidate (other merge, edited deploy.json, new attempt) it no longer counts."""
    dep = dep if dep is not None else (read_json(os.path.join(run.run_dir, "deploy.json"), {}) or {})
    answers = run.state.get("deploy_answers") or {}
    tok = deploy_candidate(run)
    open_items = []
    for i, y in enumerate(dep.get("needs_yes") or []):
        ans = answers.get(str(i + 1))
        if ans and tok and ans.get("candidate") == tok:
            continue
        open_items.append((i + 1, str(y), ans))
    return open_items


def deploy_state(run):
    return read_json(os.path.join(run.run_dir, "deploy", "state.json"), {}) or {}


def deploy_mutated(ds):
    return bool((ds.get("applied") or {}).get("go")) or any(r.get("attempted") for r in ds.get("files") or [])


def report_text(run):
    m = run.models
    results = read_json(os.path.join(run.run_dir, "results.json"), []) or []
    final = run.state.get("final", {})
    plan = read_json(os.path.join(run.run_dir, "tasks.json"), {})
    L = [f"# POWER доклад - {run.state.get('title') or os.path.basename(run.run_dir)}", "", echo_line(run), "",
         f"Заявка: [request.md]({run.run_dir}/request.md) · план: [PLAN.md]({run.run_dir}/PLAN.md) · "
         f"преглед: [REVIEW.md]({run.run_dir}/REVIEW.md) · деплой: [DEPLOY.md]({run.run_dir}/DEPLOY.md)", "",
         "## Задачи", "", "| id | статус | автори (всички с код в дървото) | проверил | рундове | ескалация | причина / бележки |", "|--|--|--|--|--|--|--|"]
    for r in results:
        notes = r.get("reason") or ""
        res = r.get("result") or {}
        if r.get("plan_changes"):
            notes += f" plan_change ×{len(r['plan_changes'])} (виж „Промени по плана“)"
        if res.get("open_issues"):
            notes += " open: " + "; ".join(res["open_issues"][:2])
        if r.get("stray_files"):
            notes += " извън списъка: " + ", ".join(r["stray_files"])
        if r.get("stray_resolved"):
            notes += " разрешени извън списъка: " + "; ".join(f"{k} ({v})" for k, v in r["stray_resolved"].items())
        if r.get("scope_changes"):
            notes += " SUSPECT, променени извън списъците (от diff): " + ", ".join(r["scope_changes"])
        who = ", ".join(str(x) for x in (r.get("authors") or ([r["author"]] if r.get("author") else []))) or "-"
        L.append(f"| {r.get('task')} | {r.get('status')} | {who} | {', '.join(str(x) for x in r.get('verified_by') or []) or '-'} | "
                 f"{r.get('rounds', 0)} | {'да' if r.get('escalated') else '-'} | {notes[:300].replace('|', '/')} |")
    # V2-15: every reported plan change, from the append-only history (work, fix, finish, finalfix, orchestrator)
    changes = [plan_change_line(h) for h in run.state.get("plan_changes") or []]
    if changes:
        L += ["", "Промени по плана (докладвани, не одобрени; същите в PLAN.md, раздел „Plan changes“):"] + changes
    L += ["", "## Файлове", ""]
    for t in plan.get("tasks") or []:
        for f in t.get("files") or []:
            p = f.get("path")
            L.append(f"- {t['id']} {f.get('action')}: [{p}]({run.project_dir}/{p})")
    rnd, cf = latest_check(final)
    cf = cf or {}
    rnd = rnd or "final"
    L += ["", f"Пълен diff: [diff.patch]({run.run_dir}/checks/{rnd}/diff.patch) · отчет: [report.md]({run.run_dir}/checks/{rnd}/report.md) · "
          f"финална проверка ({rnd}): {'PASS' if cf.get('pass') else ('FAIL' if cf else 'ЛИПСВА')}"]
    for w in cf.get("warnings") or []:
        L.append(f"- {w}")
    if suspect_files(run):
        L.append("- SUSPECT: променени извън списъците на задачите (от diff, без restore): " + ", ".join(suspect_files(run)))
    b = run.state.get("baseline", {})
    L += ["", f"Baseline: {b.get('summary', '-')}"] + [f"- {key}: {row}" for key in ("PRE-BROKEN", "WEAK", "SUSPECT") for row in (b.get("rows") or {}).get(key) or []]
    rv = final.get("review") or {}
    L += ["", "## Финален преглед", "", f"Преглед от {rv.get('by', '-')}: {rv.get('verdict', '-')}, {len(rv.get('findings') or [])} находки; "
          f"лещи: {', '.join(final.get('lenses') or {}) or '-'}; резултат: {final.get('status', '-')}" + (f" - {final.get('reason')}" if final.get("reason") else "")
          + (f"; автори на кода: {', '.join(rv.get('authors') or [])}" + ("" if rv.get("independent", True) else " - прегледът НЕ е независим") if rv.get("authors") else "")]
    for f in rv.get("findings") or []:
        L.append(f"- [{f.get('severity')}] {f.get('file')}:{f.get('line')} - {str(f.get('problem', ''))[:200]}")
    if final.get("finalfix"):
        L.append(f"- поправки от {final['finalfix'].get('by')}: {((final['finalfix'].get('result') or {}).get('summary') or '-')[:200]}; "
                 f"проверка от {(final.get('finalverify') or {}).get('by', '-')}: "
                 f"{'PASS' if ((final.get('finalverify') or {}).get('verdict') or {}).get('pass') else 'FAIL'}")
    blocked = [r for r in results if r.get("status") in ("BLOCKED", "BLOCKED_DEP", "INCOMPLETE")]
    if blocked:
        L += ["", "## BLOCKED и въпроси", ""] + [f"- {r['task']}: {r.get('reason', '')}" + (f" - {' | '.join(r.get('last_problems') or [])[:300]}" if r.get("last_problems") else "") for r in blocked]
    if any(m[s] in CODEX for s in ("step1", "step2", "step3")):
        L += ["", "## Codex", ""]
        if not any(run.state.get(k) for k in ("codex_usage_before", "codex_usage_after")):
            L.append("- usage: не е теглен (две реални `codex exec` на run; `start`/`continue --refresh-quota` ги пуска преди работата и след прегледа)")
        for key in ("codex_usage_before", "codex_usage_after"):
            if run.state.get(key):
                L.append(f"- {key.replace('codex_usage_', 'usage ')}: {run.state[key][:300]}")
        code, outp = run.run_py(CODEX_HELPER, "status", timeout=60, project=False)
        L += ["```", outp.strip()[-1500:], "```"]
    L += ["", "## Разход (измерен)", "", "```"] + run.cost_lines() + ["```"]
    dp = final.get("deploy_plan") or {}
    L += ["", "## Деплой", "", f"План: {dp.get('line', '-')}"]
    dep = read_json(os.path.join(run.run_dir, "deploy.json"), {}) or {}
    for s in dep.get("db") or []:
        L.append(f"- DB стъпка (само след „да“): {s.get('description')} - apply `{s.get('apply')}` - rollback `{s.get('rollback')}`")
    for y in dep.get("needs_yes") or []:
        L.append(f"- иска изрично „да“: {y}")
    for r in dep.get("risks") or []:
        L.append(f"- риск: {r}")
    stops = hard_stops(run)
    L += ["", "Твърди стопове: " + ("; ".join(stops) if stops else "няма")]
    return "\n".join(L) + "\n"


def gate_b_text(run):
    results = read_json(os.path.join(run.run_dir, "results.json"), []) or []
    passed = [r["task"] for r in results if r.get("status") == "PASS"]
    blocked = [r["task"] for r in results if r.get("status") in ("BLOCKED", "BLOCKED_DEP")]
    final = run.state.get("final", {})
    stops = hard_stops(run)
    L = [f"# Врата B - {run.state.get('title') or os.path.basename(run.run_dir)}", "", echo_line(run), "",
         f"Доклад: [REPORT.md]({run.run_dir}/REPORT.md) · PASS {len(passed)}, BLOCKED {len(blocked)} · финален преглед: "
         f"{(final.get('review') or {}).get('verdict', '-')} -> {final.get('status', '-')} · разход: ${run.cost_total():.2f}", ""]
    if stops:
        L += ["Твърди стопове (бият DEPLOY):"] + [f"- {s}" for s in stops] + [""]
    if final.get("short_circuit"):
        L += ["Лещите и финалният преглед са пропуснати (паднала финална проверка не може да стане CLEAN): поправи проверката / кода и "
              "`power_run.py continue`; ако искаш диагнозата върху падащото дърво: `power_run.py continue --diagnose`.", ""]
    if blocked:
        L.append(f"#THE_OPERATOR#, трябва да решиш дали да деплойна частично: {', '.join(passed) or 'нищо'} без {', '.join(blocked)}? "
                 f"(`power_run.py deploy` показва plan + fetch; `deploy --go` качва само след „да“)")
    elif stops:
        L.append("#THE_OPERATOR#, трябва да решиш дали да деплойна въпреки стоповете по-горе (DB стъпките ги пускаш ти след „да“; "
                 "`power_run.py deploy` показва plan + fetch, `deploy --go` качва).")
    else:
        L.append("#THE_OPERATOR#, трябва да решиш дали да деплойна: `power_run.py deploy` (plan + fetch), после `deploy --go` (apply + watch).")
    return "\n".join(L) + "\n"


def phase_report(run):
    run.phase("report")
    write_text(os.path.join(run.run_dir, "REPORT.md"), report_text(run))
    write_text(os.path.join(run.hdir, "gate_b.md"), gate_b_text(run))
    run.phase("gate_b")
    run.log(f"REPORT.md and harness/gate_b.md written; cost so far ${run.cost_total():.2f}")


def codex_usage(run, key):
    """PERF-03: `codex_task.py usage` is two real `codex exec` (a paid, slow probe) - only when the run asked for it
    (`start` / `continue --refresh-quota`); the report says when it was not fetched."""
    if run.state.get("refresh_quota") and any(run.models[s] in CODEX for s in ("step1", "step2", "step3")):
        code, outp = run.run_py(CODEX_HELPER, "usage", ["--log", os.path.join(run.run_dir, "codex-usage.jsonl")], timeout=60, project=False)
        run.state[key] = outp.strip()[-500:]
        run.save()


def run_to_gate_b(run):
    """Everything after Gate A: ready -> work -> final review -> report. Each phase resumes where it stopped."""
    ph = run.state.get("phase")
    if ph in ("gate_a", "plan", "baseline", "recon", "plan_failed", "ready"):
        phase_ready(run)
        codex_usage(run, "codex_usage_before")
        ph = "work"
    if ph in ("work",):
        phase_work(run)
        if incomplete_tasks(run):
            gate_cap(run, "work")   # the phase stays `work`: `continue` redoes the INCOMPLETE tasks
            return
        run.state.pop("capped", None)
        run.phase("work_done")
        ph = "work_done"
    if ph in ("work_done", "verify", "review"):
        phase_final(run)
        if run.state.get("final", {}).get("status") == "INCOMPLETE":
            gate_cap(run, "review")   # the phase stays `review`: `continue` runs the final review again
            return
        run.state.pop("capped", None)
        codex_usage(run, "codex_usage_after")
        ph = "report"
    if ph in ("report", "gate_b"):
        phase_report(run)


# ---------- commands ----------

def preflight(run, models):
    """V2-16: the executables every run needs, checked before any state, agent or edit. Returns the problems."""
    problems = []
    exe = (run.claude or ["claude"])[0]
    if not (shutil.which(exe) or os.path.isfile(exe)):
        problems.append(f"claude not found: {exe!r} (POWER_CLAUDE={os.environ.get('POWER_CLAUDE') or 'unset'}; set it to the claude "
                        "command, or put `claude` on PATH) - the harness starts no agent without it")
    if not shutil.which("git"):
        problems.append("git not on PATH (the snapshot, diffs and checks need it)")
    try:
        power_check.bash_exe()
    except Exception as e:  # noqa: BLE001
        problems.append(f"bash not found: {e} (the checks and deploy commands run in Git Bash)")
    helpers = [CHECK_HELPER, PROMPTS_HELPER, DEPLOY_HELPER] + ([CODEX_HELPER] if any(m in CODEX for m in models.values()) else [])
    for h in helpers:
        if not os.path.isfile(h):
            problems.append(f"helper missing: {fwd(h)}")
    try:
        load_config(run.project_dir)   # V3-06: an invalid ## POWER block is a stop here, never an empty config later
    except PlanError as e:
        problems.append(f"{e} (fix the json block, then start again)")
    if any(m in CODEX for m in models.values()):
        # V3 readiness: the Codex executable and the login, checked without a paid probe (codex_task.readiness)
        try:
            import codex_task  # noqa: E402
            problems += codex_task.readiness()
        except Exception as e:  # noqa: BLE001
            problems.append(f"cannot check the Codex install: {e}")
    return problems


def cmd_start(a):
    run = Run(a.run_dir, a.project_dir)
    run.hold("start")
    if run.state.get("phase") and not a.force:
        sys.exit(f"the run already has a state (phase {run.state['phase']}); use `continue`, or `start --force` to start over")
    for s, allowed in (("step1", STEP1), ("step2", STEP2), ("step3", STEP3)):
        if getattr(a, s) not in allowed:
            sys.exit(f"{s} must be one of {allowed}")
    flags = {f.strip().upper() for f in (a.flags or "").split(",") if f.strip()}
    bad = flags - {"START", "DEPLOY"}
    if bad:
        sys.exit(f"unknown flags {sorted(bad)}; allowed: START, DEPLOY")
    req_path = os.path.join(run.run_dir, "request.md")
    if a.request_file:
        with open(a.request_file, encoding="utf-8") as f:
            text = f.read()
        if not os.path.isfile(req_path):
            write_text(req_path, text if text.startswith("#") else f"# Заявка\n\n{text.strip()}\n")
    elif not os.path.isfile(req_path):
        sys.exit("no request: pass --request-file or write <run>/request.md first")
    models = {"step1": a.step1, "step2": a.step2, "step3": a.step3}
    codex_models = {}
    if any(m in CODEX for m in models.values()):
        try:
            import codex_task  # noqa: E402
            cache = read_json(os.path.join(codex_task.codex_home(), "models_cache.json"), {})
            res = codex_task.resolve_models(cache)
            codex_models = {f: (res.get(f) or {}).get("slug") for f in ("astra", "sol", "luna")}
        except Exception as e:  # noqa: BLE001
            sys.exit(f"cannot resolve the Codex models: {e}")
        for m in models.values():
            if m in CODEX and not codex_models.get(m):
                sys.exit(f"no Codex {m} model in the cache; choose another model")
    types = agent_types(run.project_dir)
    # 4.1: without the agent types every agent would run with the full CLAUDE.md + memory context and no turn cap
    # (the measured 63k start context); the runner type is only for the session path, so it is not required here
    missing = [power_check.POWER_AGENTS[r] for r in ("reader", "executor", "verifier") if r not in types]
    if missing:
        sys.exit(f"agent types not installed: {', '.join(missing)} (run `python {fwd(CHECK_HELPER)} install`, then start again; "
                 "the harness does not start agents without them)")
    problems = preflight(run, models)
    if problems:
        sys.exit("PREFLIGHT FAILED (no state written, no agent started, nothing edited):\n  - " + "\n  - ".join(problems))
    run.state = {"run_dir": run.run_dir, "project_dir": run.project_dir, "title": a.title or os.path.basename(run.run_dir),
                 "flags": {"start": "START" in flags, "deploy": "DEPLOY" in flags}, "models": models, "codex_models": codex_models,
                 "agent_types": types, "started_at": now(), "tasks": {},
                 "refresh_quota": bool(a.refresh_quota),   # PERF-03: codex usage snapshots only on request
                 "lenses": [{"name": n, "focus": f} for n, f in parse_items(a.lens)] or None}
    run.save()
    run.log("start: " + echo_line(run) + f" · agent types: {', '.join(types.values())}")
    first = open(req_path, encoding="utf-8").read().strip().splitlines()
    first = next((ln for ln in first if ln.strip() and not ln.startswith("#")), "the request")[:200]
    phase_recon(run, parse_items(a.recon, [("scope", f"everything the request touches: {first}")]))
    if not phase_plan(run):
        sys.exit(2)
    phase_baseline(run)
    gate_a(run)


def gate_a(run):
    text = gate_a_text(run)
    write_text(os.path.join(run.hdir, "gate_a.md"), text)
    b = run.state.get("baseline", {}).get("rows", {})
    must_stop = bool(run.state.get("questions") or b.get("PRE-BROKEN") or b.get("SUSPECT"))
    run.phase("gate_a")
    if run.state["flags"].get("start") and not must_stop:
        run.log("START: no Gate A, continuing")
        run_to_gate_b(run)
    else:
        run.log("Gate A: harness/gate_a.md written" + (" (stops even with START)" if must_stop and run.state["flags"].get("start") else ""))
        print(text)


def cmd_continue(a):
    run = Run(a.run_dir, a.project_dir)
    run.hold("continue")
    if not run.state.get("phase"):
        sys.exit("no state: use `start`")
    run.state["diagnose"] = bool(a.diagnose)   # PERF-02: one `continue` at a time - the full review on a failing tree
    if a.refresh_quota:
        run.state["refresh_quota"] = True      # PERF-03
    if a.diagnose and run.state["phase"] in ("report", "gate_b"):
        run.phase("review")   # the final review again, as a new generation, with the lenses and the reviewer this time
    if a.replan:
        for key in ("prepare", "baseline", "questions", "plan_tasks", "recon_extended"):
            run.state.pop(key, None)
        if not phase_plan(run):
            sys.exit(2)
        phase_baseline(run)
        gate_a(run)
        return
    ph = run.state["phase"]
    if ph in ("recon", "plan", "plan_failed"):
        if not phase_plan(run):
            sys.exit(2)
        phase_baseline(run)
        gate_a(run)
        return
    if ph == "baseline":
        phase_baseline(run)
        gate_a(run)
        return
    if ph == "gate_b":
        print(open(os.path.join(run.hdir, "gate_b.md"), encoding="utf-8").read())
        return
    run_to_gate_b(run)
    if run.state.get("phase") == "gate_b":
        print(open(os.path.join(run.hdir, "gate_b.md"), encoding="utf-8").read())


def cmd_verify(a):
    run = Run(a.run_dir, a.project_dir)
    run.hold("verify")
    if not run.state.get("args"):
        sys.exit("the run has no work state yet (no `ready`)")
    phase_verify_only(run, a.tasks)
    print("verify-only done; `power_run.py continue` " + ("runs the released tasks, then the final review"
                                                        if run.state.get("phase") == "work" else "runs the final review"))


def cmd_status(a):
    run = Run(a.run_dir)
    st = run.state
    if not st:
        print("no harness state in " + run.hdir)
        return
    s = read_json(os.path.join(run.hdir, "status.json"), {}) or {}
    print(f"phase: {st.get('phase')}  (updated {st.get('updated_at')})")
    print(echo_line(run))
    rec, held = run.lock_holder()
    if rec:
        print(f"lock: {'held' if held else 'not held'}; last record pid {rec.get('pid')} (`{rec.get('command')}` since {rec.get('started_at')}"
              + (")" if held else f", {'still running' if pid_alive(rec.get('pid')) else 'not running'}) - the next start/continue/verify/deploy takes it"))
    if st.get("agents_started") is not None:
        print(f"agents started: {st.get('agents_started')}" + (f" of POWER_MAX_AGENTS={MAX_AGENTS}" if MAX_AGENTS else " (no cap)"))
    ds = deploy_state(run)
    if ds.get("candidate"):
        appr = ds.get("approval") or {}
        print(f"deploy candidate: {ds['candidate']} (attempt {ds.get('attempt', 1)}, status {ds.get('status')}; "
              + (f"approved {appr.get('token')} at {appr.get('at')})" if appr else f"not approved: `deploy --go --approved {ds['candidate']}` after #THE_OPERATOR#'s yes)"))
    rec = st.get("recon") or {}
    if rec:
        print("recon: " + ", ".join(f"{n} {v.get('status')}" for n, v in rec.items()))
    if st.get("prepare"):
        print("prepare: " + ("OK" if st["prepare"].get("ok") else "FAIL"))
    if st.get("baseline"):
        print("baseline: " + st["baseline"].get("summary", ""))
    tasks = st.get("tasks") or {}
    for tid in st.get("plan_tasks") or sorted(tasks):
        o = tasks.get(tid) or {}
        line = o.get("status") or (f"running since {o.get('started_at')}" if o.get("stage") else "waiting")
        print(f"  {tid}: {line}" + (f" - {o['reason'][:100]}" if o.get("reason") else ""))
    if s.get("running"):
        print("running agents: " + ", ".join(s["running"]))
    fin = st.get("final") or {}
    if fin:
        print(f"final: {fin.get('status', '?')} (review {(fin.get('review') or {}).get('verdict', '-')}, deploy plan {(fin.get('deploy_plan') or {}).get('line', '-')})")
    print(f"cost so far: ${run.cost_total():.2f} over {len(run.calls())} agents; log: {fwd(os.path.join(run.hdir, 'log.txt'))}")
    dpl = st.get("deploy") or {}
    nxt = {"gate_a": "#THE_OPERATOR# answers harness/gate_a.md, then `continue`", "gate_b": "#THE_OPERATOR# answers harness/gate_b.md, then `deploy`",
           "plan_failed": "fix tasks.json and `continue`, or `continue --replan`", "deploy": "apply ran; see power_deploy.py status and deploy/REPORT.md",
           "deploy_failed": f"DEPLOY FAILED: apply {dpl.get('apply')}, watch {dpl.get('watch')} (exit {dpl.get('watch_exit')}) - prod is not confirmed; "
                            "`power_deploy.py status`, then `rollback --go` unless already ROLLED_BACK, or `fetch --new-attempt` for a new candidate",
           "awaiting_watch": "apply done; the DB steps and `power_deploy.py watch --go --db-rollback` are the session's; the run is done only when that watch is HEALTHY",
           "done": "deployed and watched (HEALTHY); prod-verifier + the closing message are the session's"}.get(st.get("phase"), "running or interrupted: `continue` resumes")
    if incomplete_tasks(run) or fin.get("status") == "INCOMPLETE":
        cap = st.get("capped") or {}
        nxt = (f"stopped by the agent cap ({cap.get('agents_started', st.get('agents_started'))} agents, POWER_MAX_AGENTS={cap.get('max_agents', '?')}): "
               f"INCOMPLETE {', '.join(incomplete_tasks(run)) or 'final review'}; raise POWER_MAX_AGENTS (0 = no cap) and `continue`")
    print("next: " + nxt)


def cmd_cost(a):
    run = Run(a.run_dir)
    print("\n".join(run.cost_lines()))


def cmd_deploy(a):
    run = Run(a.run_dir, a.project_dir)
    run.hold("deploy")
    if run.state.get("phase") not in ("gate_b", "deploy", "done", "deploy_failed", "awaiting_watch"):
        sys.exit(f"the run is at phase {run.state.get('phase')}; the deploy comes after Gate B")
    qs = {str(i): str(t) for i, t in enumerate((read_json(os.path.join(run.run_dir, "deploy.json"), {}) or {}).get("needs_yes") or [], 1)}
    for n in a.yes or []:
        if str(n) not in qs:
            sys.exit(f"--yes {n}: deploy.json has no needs_yes item {n} (known: {', '.join(qs) or 'none'})")
    ds = deploy_state(run)
    if deploy_mutated(ds):
        # An apply already wrote to prod: the backup in deploy/prod-before is kept, no fetch here (it would need
        # --new-attempt and a fresh review); the follow-up is watch / rollback / status, by the orchestrator.
        code, outp = run.run_py(DEPLOY_HELPER, "status", timeout=60, project=False)
        print(outp.strip())
        print(f"DEPLOY: ALREADY_APPLIED (status {ds.get('status')}, attempt {ds.get('attempt', 1)}; backup kept in deploy/prod-before; "
              f"use power_deploy.py watch / rollback, or `fetch --new-attempt` for a new round)")
        if a.go:
            sys.exit(2)
        return
    code, outp = run.run_py(DEPLOY_HELPER, "plan", timeout=120)
    print(outp.strip())
    if code != 0:
        run.log("deploy plan FAIL")
        sys.exit(1)
    if ds.get("status") == "SEALED" and not a.refetch:
        # V2-11: a hand-resolved conflict sealed by `power_deploy.py seal` is the candidate; a fetch would rebuild
        # upload/ and throw the resolution away, so the harness keeps it (apply still re-checks prod against the
        # fetched digests and stops on any change). `deploy --refetch` is the explicit way to drop the seal.
        code, outp = run.run_py(DEPLOY_HELPER, "status", timeout=60, project=False)
        print(outp.strip())
        print(f"DEPLOY: SEALED candidate {ds.get('candidate')} (hand resolution of " + ", ".join((ds.get("sealed") or {}).get("files") or []) +
              f" sealed at {(ds.get('sealed') or {}).get('at')}; kept instead of a new fetch - review deploy/merged/, `deploy --refetch` drops it)")
        code = 0 if ds.get("fetch_status") == "OK" else 1
        fetch_line = f"SEALED: {ds.get('fetch_status')} ({', '.join(ds.get('blocked') or []) or 'no open conflicts'})"
    else:
        code, outp = run.run_py(DEPLOY_HELPER, "fetch", timeout=1800)
        print(outp.strip())
        fetch_line = run.tag(outp, "FETCH") or run.last_line(outp)
    # P1-03 stage 2: every yes (--go, --force, --yes) is for the candidate this fetch produced, named by its token.
    # A token of another candidate (prod changed since #THE_OPERATOR# saw it, deploy.json edited, a new attempt) is refused
    # before anything is recorded or uploaded; the orchestrator shows the new candidate and asks again.
    tok = deploy_candidate(run)
    if a.approved and a.approved.strip() != (tok or ""):
        run.log(f"deploy refused: --approved {a.approved} is for another candidate (this fetch: {tok})")
        sys.exit(f"deploy refused: --approved {a.approved} is for another candidate or attempt; this fetch is {tok} - read the plan/fetch output above, "
                 f"get #THE_OPERATOR#'s yes for it and repeat with --approved {tok}")
    if a.yes:
        if not a.approved:
            sys.exit(f"deploy --yes refused: a yes is for one candidate - repeat with --approved {tok} (the CANDIDATE token of this fetch)")
        for n in a.yes:
            run.state.setdefault("deploy_answers", {})[str(n)] = {"text": qs[str(n)], "at": now(), "candidate": tok}
            run.log(f"deploy: #THE_OPERATOR# answered yes to needs_yes {n} for candidate {tok}: {qs[str(n)][:100]}")
        run.save()
    stops = hard_stops(run, fetch_line if code != 0 else None)
    if code != 0:
        stops.append("fetch: " + fetch_line)
    for s in stops:
        print("HARD STOP: " + s)
    if not a.go:
        print(f"DEPLOY: DRY (plan + fetch done; `deploy --go --approved {tok}` uploads exactly this candidate"
              + (" - blocked by the hard stops above until #THE_OPERATOR# says yes to each (then also --force)" if stops else "") + ")")
        return
    if not a.approved:
        run.log("deploy --go refused: no --approved")
        sys.exit(f"deploy --go refused: it needs --approved {tok}, the CANDIDATE token of this fetch - #THE_OPERATOR#'s yes is for exactly that candidate "
                 "(a later fetch that merges a changed prod is another one)")
    if unanswered_questions(run):
        run.log("deploy --go refused: unanswered needs_yes")
        sys.exit(f"deploy --go refused: deploy.json has unanswered needs_yes items for candidate {tok}; answer each with "
                 f"`deploy --yes <n> --approved {tok}` (--force does not cover them)")
    if stops and not a.force:
        run.log("deploy --go refused: hard stops")
        sys.exit(f"deploy --go refused: hard stops above need #THE_OPERATOR#'s explicit yes (then `deploy --go --force --approved {tok}`)")
    run.phase("deploy")
    extra = ["--go", "--approved", tok] + (["--claimed-by", a.claimed_by] if a.claimed_by else [])
    code, outp = run.run_py(DEPLOY_HELPER, "apply", extra, timeout=3600)
    print(outp.strip())
    status = run.tag(outp, "DEPLOY") or run.last_line(outp)
    run.state["deploy"] = {"apply": status, "at": now(), "approved": tok, "forced": bool(a.force)}
    run.save()
    run.log("apply: " + status)
    if status != "DEPLOYED":
        # V2-12: AFTER_FAILED (a required after hook failed: the files are on prod, the cache may serve the old code),
        # DEPLOYED_UNVERIFIED, a rollback or a stop before the upload - none of them is a deploy; no watch runs
        run.phase("deploy_failed", deploy_outcome=status)
        print(f"HARD STOP: apply не завърши с DEPLOYED ({status}) - виж power_deploy.py status и deploy/REPORT.md"
              + (" (паднал задължителен after hook: файловете са на prod, hook-ът се пуска на ръка или rollback)" if status == "AFTER_FAILED" else ""))
        print(f"HARNESS DEPLOY: {status}")
        sys.exit(2)
    dep = read_json(os.path.join(run.run_dir, "deploy.json"), {}) or {}
    if dep.get("db"):
        # V2-05: the run is not done until the watch after the DB steps says HEALTHY; the harness never runs them
        run.phase("awaiting_watch")
        print("DB steps are never run by the harness: run them after an explicit yes, then `power_deploy.py watch --go --db-rollback`; "
              "the run stays at phase awaiting_watch until that watch reports HEALTHY (then the session closes it)")
        print("HARNESS DEPLOY: APPLIED_AWAITING_WATCH")
        return
    code, outp = run.run_py(DEPLOY_HELPER, "watch", ["--go"], timeout=1800)
    print(outp.strip())
    wst = run.tag(outp, "DEPLOY")
    run.state["deploy"]["watch"] = wst or run.last_line(outp)
    run.state["deploy"]["watch_exit"] = code
    run.save()
    run.log(f"watch: {wst or run.last_line(outp)} (exit {code})")
    # V2-05: only a watch that ended with exit 0 AND HEALTHY completes the run; a rollback (prod is back at the old
    # version), an incomplete rollback, a timeout or unreadable output is a failed deploy for the harness too
    if code != 0 or wst != "HEALTHY":
        outcome = wst or "WATCH_UNREADABLE"
        run.phase("deploy_failed", deploy_outcome=outcome)
        print(f"HARD STOP: watch след деплоя завърши с {outcome} (exit {code}), не HEALTHY - prod {'е върнат на старата версия' if outcome.startswith('ROLLED_BACK') else 'НЕ е потвърден'}; "
              f"виж deploy/REPORT.md, `power_deploy.py status`; следва rollback (ако не е ROLLED_BACK) или нов кандидат с `fetch --new-attempt` след поправка")
        print(f"HARNESS DEPLOY: {outcome}")
        sys.exit(2)
    run.phase("done")
    print("HARNESS DEPLOY: HEALTHY")


def main():
    ap = argparse.ArgumentParser(description="POWER harness: phases 0-6 outside the session")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp, project=True):
        sp.add_argument("--run-dir", required=True)
        if project:
            sp.add_argument("--project-dir", required=True)

    sp = sub.add_parser("start")
    common(sp)
    sp.add_argument("--request-file")
    sp.add_argument("--step1", required=True, help="opus | fable | astra")
    sp.add_argument("--step2", required=True, help="sonnet | haiku | opus | fable | sol | luna | astra")
    sp.add_argument("--step3", required=True, help="opus | fable | astra")
    sp.add_argument("--flags", default="", help="START,DEPLOY")
    sp.add_argument("--recon", action="append", help="name=focus (one explorer each; default: one for the whole scope)")
    sp.add_argument("--lens", action="append", help="name=focus (default: the project's lenses, else domain + smoke)")
    sp.add_argument("--title")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--refresh-quota", action="store_true", help="run `codex_task.py usage` before the work and after the review (two real codex exec)")
    sp = sub.add_parser("continue")
    common(sp)
    sp.add_argument("--replan", action="store_true", help="plan again (after answers in request.md)")
    sp.add_argument("--diagnose", action="store_true",
                    help="run the lenses and the final reviewer even when `check --all final` fails (default: FAIL right away, nothing else runs)")
    sp.add_argument("--refresh-quota", action="store_true", help="as for start")
    sp = sub.add_parser("verify")
    common(sp)
    sp.add_argument("--tasks", required=True, help="id:author:round,...")
    for name in ("status", "cost"):
        sp = sub.add_parser(name)
        common(sp, project=False)
        sp.add_argument("--project-dir", help="accepted for a uniform command line; not needed")
    sp = sub.add_parser("deploy")
    common(sp)
    sp.add_argument("--go", action="store_true")
    sp.add_argument("--force", action="store_true", help="with --go: past the hard stops, after #THE_OPERATOR#'s explicit yes")
    sp.add_argument("--yes", action="append", type=int, metavar="N",
                    help="#THE_OPERATOR#'s yes to needs_yes item N of deploy.json (repeatable; recorded in the run state with the candidate token)")
    sp.add_argument("--approved", metavar="ATTEMPT:HASH",
                    help="the CANDIDATE token of the last `deploy` output; required with --go, --force and --yes: the yes is for exactly that candidate")
    sp.add_argument("--claimed-by")
    sp.add_argument("--refetch", action="store_true",
                    help="fetch again although the candidate is a sealed hand resolution (power_deploy.py seal); the resolution is dropped")
    a = ap.parse_args()
    try:
        {"start": cmd_start, "continue": cmd_continue, "verify": cmd_verify, "status": cmd_status, "cost": cmd_cost,
         "deploy": cmd_deploy}[a.cmd](a)
    except PlanError as e:
        print(f"ERROR {e}\n{a.cmd.upper()}: FAIL", flush=True)
        sys.exit(1)
    except AgentCap as e:
        # before the work phase (recon, plan, areas): nothing to mark INCOMPLETE, the phase stays for `continue`
        print(f"AGENT CAP: {e}\n{a.cmd.upper()}: INCOMPLETE (the phase is kept; `power_run.py continue` resumes it)", flush=True)
        sys.exit(3)
    except KeyboardInterrupt:
        print("interrupted; `power_run.py continue` resumes", flush=True)
        sys.exit(130)


if __name__ == "__main__":
    main()
