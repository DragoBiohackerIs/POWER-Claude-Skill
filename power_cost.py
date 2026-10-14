#!/usr/bin/env python3
"""Measured token cost of a POWER run: every agent of the session (Agent tool and Workflow), by role.

  python power_cost.py [--session ID] [--since HH:MM] [--top N]

Reads the session transcript (CLAUDE_CODE_SESSION_ID or --session) and its subagent transcripts under
<session>/subagents/**. For each agent: role (from the label), model, API calls, first-call input (the start
weight: system prompt + CLAUDE.md + memory + tools), the largest context and the input re-sent over all calls.
Output tokens of agents are not in the transcripts reliably (streaming chunks), so they are not shown.

Why it matters (measured 2026-10-12, FAQ run, 53 agents): half of all agent input was the ~62k start prefix,
re-sent on every call; the other half grew with the square of the agent's length. The REPORT of a run quotes
the summary lines of this script.
"""
import argparse
import glob
import json
import os
import re
from datetime import datetime
from pathlib import Path

HOME = Path.home() / ".claude" / "projects"
ROLES = ("recon", "lens", "plan", "work", "check", "verify", "fix", "escalate", "restore", "review", "finalfix", "finalverify")


def rows_of(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def calls_of(rows):
    """One record per API call; usage = max over the streamed chunks of a requestId."""
    acc, order = {}, []
    for r in rows:
        if r.get("type") != "assistant":
            continue
        rid = r.get("requestId") or r.get("uuid")
        m = r.get("message") or {}
        u = m.get("usage") or {}
        if rid not in acc:
            acc[rid] = {"model": m.get("model"), "in": 0, "cc": 0, "cr": 0, "t": r.get("timestamp")}
            order.append(rid)
        a = acc[rid]
        a["in"] = max(a["in"], u.get("input_tokens", 0))
        a["cc"] = max(a["cc"], u.get("cache_creation_input_tokens", 0))
        a["cr"] = max(a["cr"], u.get("cache_read_input_tokens", 0))
    return [acc[r] for r in order]


def first_text(rows):
    for r in rows:
        if r.get("type") == "user":
            c = (r.get("message") or {}).get("content")
            if isinstance(c, str):
                return c
            for p in c or []:
                if isinstance(p, dict) and p.get("type") == "text":
                    return p.get("text", "")
    return ""


def role_of(label, text):
    s = (label or "") + " " + text[:300]
    s = s.lower()
    for role in ROLES:
        if re.search(r"\b" + role + r"[:\s.#]", s) or f"prompts/{role}." in s or f".{role}.md" in s:
            return role
    if "thin runner" in s:
        return "check"
    return "other"


def k(n):
    return f"{n/1000:.0f}k"


def main():
    ap = argparse.ArgumentParser(description="POWER: measured token cost of the session's agents")
    ap.add_argument("--session", default=os.environ.get("CLAUDE_CODE_SESSION_ID"))
    ap.add_argument("--since", help="HH:MM (today), only agents whose transcript changed after it")
    ap.add_argument("--top", type=int, default=12)
    a = ap.parse_args()
    if not a.session:
        print("[power_cost: no session id; pass --session]")
        return
    hits = glob.glob(str(HOME / "*" / f"{a.session}.jsonl"))
    if not hits:
        print("[power_cost: transcript not found]")
        return
    main_path = Path(hits[0])
    since = 0.0
    if a.since:
        h, m = a.since.split(":")
        since = datetime.now().replace(hour=int(h), minute=int(m), second=0, microsecond=0).timestamp()
    agents = []
    for ap_ in glob.glob(str(main_path.with_suffix("")) + "/subagents/**/agent-*.jsonl", recursive=True):
        if os.path.getmtime(ap_) < since:
            continue
        rows = rows_of(ap_)
        cs = calls_of(rows)
        if not cs:
            continue
        meta = {}
        try:
            meta = json.load(open(ap_[: -len(".jsonl")] + ".meta.json", encoding="utf-8"))
        except (OSError, ValueError):
            pass
        label = meta.get("description") or ""
        text = first_text(rows)
        role = role_of(label, text)
        first = cs[0]["in"] + cs[0]["cc"] + cs[0]["cr"]
        sent = sum(c["in"] + c["cc"] + c["cr"] for c in cs)
        maxctx = max(c["in"] + c["cc"] + c["cr"] for c in cs)
        model = (cs[0]["model"] or "?").replace("claude-", "").split("-5-")[0]
        agents.append({"role": role, "label": label or text[:60].replace("\n", " "), "model": model,
                       "type": meta.get("agentType") or "", "calls": len(cs), "first": first, "max": maxctx, "sent": sent})
    if not agents:
        print("no agents in this session" + (f" since {a.since}" if a.since else ""))
        return
    agents.sort(key=lambda x: -x["sent"])
    print(f"{'label':<28} {'role':<11} {'type':<18} {'model':<7} {'calls':>5} {'start':>6} {'maxctx':>7} {'input':>8}")
    for x in agents[: a.top]:
        print(f"{x['label'][:28]:<28} {x['role']:<11} {x['type'][:18]:<18} {x['model']:<7} {x['calls']:>5} {k(x['first']):>6} {k(x['max']):>7} {k(x['sent']):>8}")
    by = {}
    for x in agents:
        b = by.setdefault(x["role"], {"n": 0, "calls": 0, "first": 0, "sent": 0})
        b["n"] += 1; b["calls"] += x["calls"]; b["first"] += x["first"]; b["sent"] += x["sent"]
    print("\nby role: agents / calls / avg start weight / input re-sent")
    for role, b in sorted(by.items(), key=lambda kv: -kv[1]["sent"]):
        print(f"  {role:<11} {b['n']:>3} / {b['calls']:>4} / {k(b['first'] // b['n']):>6} / {k(b['sent']):>8}")
    total_sent = sum(x["sent"] for x in agents)
    start_share = sum(x["first"] * x["calls"] for x in agents)
    main_calls = calls_of(rows_of(main_path))
    main_sent = sum(c["in"] + c["cc"] + c["cr"] for c in main_calls)
    main_max = max((c["in"] + c["cc"] + c["cr"] for c in main_calls), default=0)
    models = sorted({(c["model"] or "?").replace("claude-", "") for c in main_calls})
    print(f"\nAGENTS: {len(agents)} agents, {sum(x['calls'] for x in agents)} calls, input {k(total_sent)} "
          f"(start prefix re-sent: ~{k(start_share)}, {100 * start_share // max(total_sent, 1)}%)")
    print(f"MAIN LOOP: {len(main_calls)} calls on {', '.join(models)}, max context {k(main_max)}, input {k(main_sent)}")


if __name__ == "__main__":
    main()
