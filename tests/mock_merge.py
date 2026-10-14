#!/usr/bin/env python3
"""Tests only: stands in for `git merge-file` (POWER_GIT_MERGE) to inject process failures into the 3-way merge.

MOCK_MERGE modes:
  exit128       stderr "fatal: simulated merge subprocess failure", exit 128 (git internal error)
  exit127       stderr "bash: git: command not found", exit 127 (git missing)
  exit255       stderr "fatal: negative exit from git", exit 255
  markers_exit0 stdout with conflict markers, exit 0 (an impossible git answer; still never uploadable)
  stderr_noise  the real `git merge-file` with the same arguments, plus a diagnostic line on stderr (must not
                end up in the candidate)
"""
import os
import subprocess
import sys

mode = os.environ.get("MOCK_MERGE", "exit128")
if mode == "exit128":
    sys.stderr.write("fatal: simulated merge subprocess failure\n")
    sys.exit(128)
if mode == "exit127":
    sys.stderr.write("bash: git: command not found\n")
    sys.exit(127)
if mode == "exit255":
    sys.stderr.write("fatal: negative exit from git\n")
    sys.exit(255)
if mode == "markers_exit0":
    sys.stdout.write("<<<<<<< local\nours\n=======\ntheirs\n>>>>>>> prod\n")
    sys.exit(0)
if mode == "stderr_noise":
    p = subprocess.run(["git", "merge-file", *sys.argv[1:]], capture_output=True)
    sys.stdout.buffer.write(p.stdout)
    sys.stderr.write("warning: simulated diagnostic on stderr\n")
    sys.exit(p.returncode)
sys.exit(f"unknown MOCK_MERGE mode {mode}")
