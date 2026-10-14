#!/usr/bin/env python3
"""Fake `power_deploy.py` for one harness test (POWER_DEPLOY_HELPER=<this file>): every command is delegated to the
real script, except `watch`, which prints unusable output and exits 1 (a transport crash, a traceback) - the harness
must not call that a completed deploy."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REAL = os.path.join(os.path.dirname(HERE), "power_deploy.py")
args = sys.argv[1:]
if args and args[0] == "watch":
    print("Traceback (most recent call last):\n  File \"power_deploy.py\", line 1, in <module>\nRuntimeError: fake transport crashed during the canary")
    sys.exit(1)
sys.exit(subprocess.run([sys.executable, REAL] + args, env=dict(os.environ, PYTHONUTF8="1")).returncode)
