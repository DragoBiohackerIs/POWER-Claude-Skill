# POWER: Codex and the project block `## POWER`

## Codex (Astra / Sol / Luna)

- Codex is not a Workflow model: every Codex task is a `power-runner` (haiku) that runs `codex_task.py` and returns
  `RESULT_JSON`. The work is paid from #THE_OPERATOR#'s ChatGPT plan, outside the Claude limit: Sol for step 2 is the way
  to take the executors' input off Claude; Luna is the cheapest (mechanical tasks only), falls back to Haiku.
- `codex exec` runs in an **unelevated** Windows sandbox (elevated does not start on this machine: `codex doctor`
  "provisioning failed"). `work`, `fix`, `finish` are `workspace-write`; `verify`, `plan`, `plan-area`, `review` are
  `read-only`, their files are written by the script from the JSON.
- No network and no Docker: checks are run by `power_check.py` through a Claude runner, Codex judges by the diff and
  the report.
- Each call waits at most 540 s and returns `STATUS: RUNNING`; the runner repeats the same command, Codex continues
  detached. Up to 6 in parallel (`POWER_CODEX_MAX_PARALLEL`), timeout 60 min (`POWER_CODEX_TIMEOUT`). Prompt, log
  and result of each task are in `<run>/codex/`.
- Codex reads `AGENTS.md`, not `CLAUDE.md`, and does not see Claude's memory: the task's traps are in `rules`, and
  its instructions tell it to read `CLAUDE.md`. Reasoning effort comes from `~/.codex/config.toml`.
- `codex_task.py prompt --kind <k> …` shows the exact prompt without running Codex; `usage [--log F]` - snapshot of
  the plan (two real `codex exec`; the harness takes it before and after the run only with `--refresh-quota` on
  `start` / `continue`, PERF-03, and the % difference goes into REPORT.md); `status --run-dir R` - tokens per task.
- A job's `run_job` polls for `done.json` from 0.25 s with a backoff to 2 s (PERF-09), so a finished job is reported
  at once; `--max-wait` (default 540 s) still bounds one call, after which `STATUS: RUNNING` / `QUEUED` means call again.
- If Codex fails (limit exhausted, crash, timeout): fallback per SKILL §2, the result carries `author`/`verified_by`;
  many failures at once = limit exhausted, goes into the summary.

## `## POWER` in the project's `CLAUDE.md`

The project `CLAUDE.md` (or a higher one in the same workspace) may have a `## POWER` section with text for humans
and one `json` block for the scripts. `power_check.py config --project-dir <project>` shows the merged result (short;
`--full` the whole json). Without such a section the general rules apply, anything missing is asked about, without a
deploy profile the deploy is manual.

```json
{"deny": ["**/generated/**"],
 "allow": [],
 "lenses": [{"name": "domain", "agent_type": "php-reviewer", "focus": "escaping, audit log, business rules"}],
 "smoke": ["MSYS_NO_PATHCONV=1 docker exec <container> php -l /var/www/html/index.php"],
 "claim": ".project/CLAIMS.md",
 "deploy": {"profiles": {
   "shop": {"local_root": "my-plugin",
            "remote_root": "/home/<acct>/<docroot>/wp-content/plugins/my-plugin",
            "get": "python <tool> get \"{remote}\" \"{local}\"",
            "put": "python <tool> put \"{local}\" \"{remote_dir}\" --go",
            "rm":  "python <tool> rm \"{remote}\" --go",
            "ls":  "python <tool> ls \"{remote_dir}\"",
            "last": ["my-plugin.php"],
            "after": ["python <invalidate> --change code --urls {urls}"],
            "canary": ["https://<shop>/", "https://<shop>/shop/"],
            "origin_ip": "1.2.3.4", "pace": 3, "settle": 20}}}}
```

- The lists (`deny`, `allow`, `lenses`, `smoke`) are merged from the project upward; for the other keys the nearest
  `CLAUDE.md` wins, and the relative paths and commands in them are run from its folder.
- Deploy profile: `local_root` (relative to this `CLAUDE.md`) ↔ `remote_root`; `get`/`put`/`rm`/`ls` are templates
  with `{local}`, `{remote}`, `{remote_dir}` (put uploads under the local file's name); `last` = names that are always
  uploaded last; `after` = commands after the upload (`{urls}` = the edge verify URLs from `deploy.json`, may be
  empty); `canary` = URLs that must stay below 500 (origin-direct via `origin_ip`: no WAF and no CF Access);
  `pace` = seconds between edge requests; `settle` = seconds before canary after the upload; `fatal` = crash markers
  in the body (default PHP fatal / WP critical error).
- A run that touches several projects (admin + CRO) has `project_dir` = the workspace folder with the shared
  `CLAUDE.md`; then the `local` paths in `tasks.json` and `deploy.json` are relative to it.
- `claim`: the file with the CLAIM lines (`power_deploy.py apply` refuses files in someone else's active CLAIM; `check`
  does not count this file as "changed outside the run").
