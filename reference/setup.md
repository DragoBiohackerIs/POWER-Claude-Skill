# POWER: setup on a new machine

Run once per machine, and again after any change to `agents/` or `power_workflow.js`:

    python "<skill>/power_check.py" install           # copies the agent types and the workflow
    python "<skill>/power_check.py" install --check   # reports only; also lists missing settings

`install` copies `agents/*.md` to `~/.claude/agents/` and `power_workflow.js` to
`~/.claude/workflows/power-execute.js`. Run `/reload-skills` (or start a new session) so the session sees them.
`install` never edits settings: add these to `~/.claude/settings.json` by hand.

```json
{
  "subagentPromptCacheTtl": "1h",
  "autoContinueAtUsageLimit": true,
  "permissions": { "allow": ["Workflow(power-execute)"] }
}
```

| Setting | Why POWER needs it | Enforced by |
|--|--|--|
| `subagentPromptCacheTtl: "1h"` | Workflow agents keep their prompt cache 5 minutes by default. A task's verifier often starts later than that after the previous agent of its type, and then pays the prefix again. With `1h` same-type agents (same model, effort, type, tools, folder) keep sharing it. On the API, 1h cache writes cost more; on a subscription it is a plain saving. | `install --check` reports it (code); you add it (external) |
| `autoContinueAtUsageLimit: true` | Without it an agent that hits the usage limit fails and its task is BLOCKED as infrastructure. With it the run pauses and continues after the reset (interactive sessions only, up to two waits per run). | `install --check` reports it (code); you add it (external) |
| `Workflow(power-execute)` in `permissions.allow` | Skips the per-run approval prompt for the named workflow. Needed for `START DEPLOY` without stops and for any headless (`claude -p`) run, where the prompt is never shown. | `install --check` reports it (code); you add it (external) |

What else is checked by code on this machine, before any state, agent or edit (`power_run.py start` preflight,
`PREFLIGHT FAILED` names the fix): the agent types `power-reader`, `power-executor`, `power-verifier`; `claude`
(`POWER_CLAUDE` or PATH), `git`, Git Bash, the helper scripts; a valid `## POWER` json block (an invalid one is a stop,
never an empty config - V3-06); with a Codex model the codex executable (`CODEX_EXE`, the desktop app under
`LOCALAPPDATA/OpenAI/Codex/bin`, or PATH - a `CODEX_EXE` that points nowhere is an error) and the login (`auth.json`
under `CODEX_HOME`), without a paid probe, plus the Codex model cache (`codex_task.py ready` makes the same checks
for the Workflow path). What is not pre-checked by code: whether the project's `## POWER` block matches the project
(`power_check.py config` prints it; reading it is the session's job).

Requirements: Claude Code with custom agent types (`omitClaudeMd`, `maxTurns`) and `claude` on PATH (the harness
starts `claude -p` processes; `shutil.which("claude")`, or `POWER_CLAUDE`), Python 3, Git Bash on Windows; Workflows
only for the in-session fallback. Codex (Astra / Sol / Luna) is optional: without `codex` on PATH the menu offers only Claude models.

Project rules come from a `## POWER` section in the project's `CLAUDE.md` (`reference/codex-config.md`). Without it
POWER still runs, with the general rules and a manual deploy.

Why the workflow is a named copy and not `scriptPath`: Workflow starts only from a script file the session is
allowed to read, and the skill folder is usually outside the working directory.

Resume: relaunching a stopped run reruns every agent that started after the first failed one, even finished ones.
After a crash, run Workflow again only for the BLOCKED tasks (`workflow-args` with their ids, or `--verify` when
only the check was broken) instead of resuming the whole run.
