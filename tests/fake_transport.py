#!/usr/bin/env python3
"""Fake deploy transport for the tests: a local "prod" folder plus controllable failures.

  fake_transport.py put <src> <remote_dir>
  fake_transport.py get <remote> <dest>
  fake_transport.py rm <remote>
  fake_transport.py ls <remote_dir>

Behaviour comes from the JSON file in FAKE_TRANSPORT_BEHAVIOUR (optional):
  {"put": [{"src_contains": "upload/b.php", "mode": "write_then_fail"}],   # modes: fail | write_then_fail | truncate_then_fail | corrupt_write
   "rm":  [{"remote_contains": "c.php", "mode": "fail"}],
   "get": [{"remote_contains": "a.php", "mode": "fail"}],
   "ls":  {"mode": "fail"}}
Every call is appended to FAKE_TRANSPORT_LOG (one JSON line) so a test can assert what was attempted.
"""
import json
import os
import shutil
import sys


def log(entry):
    path = os.environ.get("FAKE_TRANSPORT_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def behaviour():
    path = os.environ.get("FAKE_TRANSPORT_BEHAVIOUR")
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def rule_for(rules, key, value):
    for r in rules or []:
        if r.get(key) and r[key].replace("\\", "/") in value.replace("\\", "/"):
            return r.get("mode")
    return None


def main():
    cmd, args = sys.argv[1], sys.argv[2:]
    b = behaviour()
    log({"cmd": cmd, "args": args})
    if cmd == "put":
        src, remote_dir = args
        dest = os.path.join(remote_dir, os.path.basename(src))
        mode = rule_for(b.get("put"), "src_contains", src)
        if mode == "fail":
            print("fake put: transport error before writing")
            sys.exit(1)
        os.makedirs(remote_dir, exist_ok=True)
        if mode == "truncate_then_fail":
            with open(src, "rb") as f:
                data = f.read()
            with open(dest, "wb") as f:
                f.write(data[: max(1, len(data) // 2)])
            print("fake put: connection lost mid-transfer")
            sys.exit(1)
        if mode == "corrupt_write":
            # the transport reports success but prod ends up with other bytes (a rewriting proxy, a CRLF filter)
            with open(src, "rb") as f:
                data = f.read()
            with open(dest, "wb") as f:
                f.write(data + b"\n// corrupted by the transport\n")
            return
        shutil.copyfile(src, dest)
        if mode == "write_then_fail":
            print("fake put: timeout after the write")
            sys.exit(1)
        return
    if cmd == "get":
        remote, dest = args
        if rule_for(b.get("get"), "remote_contains", remote) == "fail":
            print("fake get: connection reset")
            sys.exit(1)
        if not os.path.isfile(remote):
            print(f"fake get: {remote} does not exist")
            sys.exit(1)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        shutil.copyfile(remote, dest)
        return
    if cmd == "rm":
        (remote,) = args
        if rule_for(b.get("rm"), "remote_contains", remote) == "fail":
            print("fake rm: permission denied")
            sys.exit(1)
        if os.path.isfile(remote):
            os.remove(remote)
        return
    if cmd == "ls":
        (remote_dir,) = args
        if (b.get("ls") or {}).get("mode") == "fail":
            print("fake ls: service unavailable")
            sys.exit(3)
        if not os.path.isdir(remote_dir):
            print(f"fake ls: {remote_dir} does not exist")
            sys.exit(1)
        print("\n".join(sorted(os.listdir(remote_dir))))
        return
    sys.exit(f"unknown command {cmd}")


if __name__ == "__main__":
    main()
