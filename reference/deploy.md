# POWER: deploy with `power_deploy.py` (phase 6)

Dry-run without `--go`; `--go` only from the orchestrator, after Gate B or with `DEPLOY` and no hard stop. Never does
an agent from a Workflow or a subagent mutate prod. The profile comes from `## POWER` -> `deploy.profiles`
(`reference/codex-config.md`).

1. **`plan`**: checks `deploy.json` (written by step 3) against the profile - the files exist, are in the run, are
   under `local_root`; shows the order, canary and verify URLs, the DB steps and which run files do NOT go to prod
   (so it is not silent). Fix `deploy.json` until it passes.
2. **`fetch`** (may run already during the final review): downloads the prod versions into `<run>/deploy/prod-before/`
   (this is the backup) and classifies each file: `clean` (prod == snapshot: the local one is uploaded), `same` (prod
   already has it), `new`, `merged` (prod differs: `git merge-file` merges prod + only our hunks; the result in
   `deploy/upload/`, the diff against prod in `deploy/merged/`), `conflict`, `diverged-no-base` (prod has a version,
   the plan says create), `merge-error` (the `git merge-file` process failed: exit 128+, git missing, a timeout, or
   markers with exit 0 - its output is a diagnostic, nothing is written to `upload/`, V2-08; only exit 0 without
   markers is a merge, exit 1-127 with markers a conflict; stdout and stderr are kept apart, so a diagnostic never
   lands in the source). **Step 3 reviews every merged diff** before apply. A `conflict`, `diverged-no-base` or
   `merge-error` row is resolved by hand in `deploy/upload/<file>` (or ask), then **`seal`** (V2-11): it checks that
   every stopped row has a file without markers, regenerates the prod → candidate diff in `deploy/merged/`, hashes the
   resolution, turns the row into an upload and prints a **new** candidate token - the resolution is a new candidate,
   reviewed and approved like any other; the pre-resolution token is void, an edit after the seal is
   `CANDIDATE_CHANGED` (sealing again is the way on), and `apply` refuses an unsealed resolution. The harness
   `power_run.py deploy` keeps a sealed candidate instead of fetching it away (`--refetch` drops it explicitly). A
   binary that already exists on prod, or whose existence the transport cannot tell (`binary-unknown`, no `ls` or
   `ls` failed), cannot be restored: fetch stops, unless `"allow_unrestorable": true` after an explicit "yes". Fetch
   records the sha256 of every candidate (`upload/`, conflict rows too), of every backup (`prod_before_sha`: the digest
   every re-check and rollback is verified against), of `deploy.json` and of the **effective configuration**
   (`effective_sha`: the resolved profile - transport command templates, remote root, base directory, `last`, `fatal`,
   `origin_ip` - and the effective `after` / `canary` / `verify` / `db`, `allow_unrestorable`, the ordered remote
   targets; only the digest is stored, never the templates, V2-03), and prints the **candidate token** `CANDIDATE:
   <attempt>:<hash>` (12 hex of a sha256 over every file's remote target, order, prod state, action, candidate and
   backup hash, plus `deploy.json` and the effective configuration): the same prod, local files, `deploy.json` and
   profile give the same token on every fetch; a changed prod (another merge), an edited candidate, an edited
   `deploy.json`, a changed profile (a new `after` command, another canary, another transport or remote root),
   another upload order or a new attempt give another one. **Once `apply --go` has written anything, fetch
   refuses**: `prod-before/` is the only backup and is never overwritten. A new round is `fetch --new-attempt`: the
   whole attempt (backup, candidate, state with its approval, report) moves to `deploy/attempts/<n>/` first, the
   attempt number becomes n+1, and the new candidate needs a new review and a new yes.
3. **`apply --go --approved <attempt:hash> [--claimed-by <topic>] [--force-canary]`** (the topic = the exact text of
   the CLAIM line). Before the first write, in this order, each a stop with no `applied` record so it can be repeated
   after the cause is fixed: refuses files in another session's active CLAIM; refuses while an earlier apply is
   interrupted; `UNRESTORABLE_BINARY` / `FETCH_FAILED` - a binary that exists on prod or whose existence is unknown
   has no backup and no rollback, so it is uploaded only when the reviewed `deploy.json` says `allow_unrestorable`
   (adding it later is another candidate: fetch, review, yes again), and a fetch that ended `FAIL` (such a binary, a
   conflict) left no applicable candidate at all, whatever token it printed - the policy is enforced here, right
   before the write, not only in fetch (V2-01); `NOT_APPROVED` - `--go` without `--approved`; `APPROVAL_MISMATCH` - the token is for
   another candidate or attempt (#THE_OPERATOR#'s yes was given for the candidate they saw; a later fetch that merged a changed
   prod, an edited `deploy.json` or a new attempt is another candidate and needs its own yes); the token that passed
   is recorded as `approval` in `deploy/state.json`, in `REPORT.md` and in `status`; then `CANDIDATE_CHANGED` - an
   `upload/` file differs from the hash fetch or seal recorded (someone edited the reviewed candidate; no row is
   exempt, a hand resolution goes through `seal`), `deploy.json` was edited after the fetch (its `after` / `canary` /
   `verify` / `db` steps are part of the reviewed candidate: fetch again), or the effective configuration differs from
   the fetched digest (the profile in `CLAUDE.md` changed: `after`, `canary`, transport commands, remote root, `last`;
   checked on every apply, a new fetch is not needed to notice - V2-03); `BACKUP_INVALID` - a `prod-before/` file no
   longer has the digest recorded at fetch, or is missing: there is no rollback, so nothing is written (fetch again
   for a fresh backup, V2-09); `REMOTE_CHANGED` - for **every** row the candidate relies on, not only the ones to
   upload (V2-02): a fetched file (upload, skip, resolved) no longer has the fetched digest or cannot be re-read; a
   file planned as new (text or binary) is not **positively confirmed absent** - it exists now, or `ls` is unavailable
   (unknown is never absence, so an unavailable `ls` never authorises a creation); a binary that existed is gone or
   unknown (fetch again, or `fetch --new-attempt`, to merge against the current prod);
   `PRECHECK_FAILED` - a canary (origin-direct via `origin_ip`, no WAF and no CF Access) is 5xx, unreachable or
   shows a fatal marker already before the upload: a site that is down cannot tell our breakage from its own, so
   this is the hard stop of SKILL §8, not a warning. `--force-canary` is #THE_OPERATOR#'s explicit yes: the override is
   recorded in the state and those URLs cannot trigger a rollback; while they stay down, `watch` reports `DEGRADED`
   (exit 2, no automatic rollback - V3-03), never `HEALTHY`. Then the upload in order (`order`; `last` from the
   profile always last, e.g. bootstrap): each file is marked `attempted` on disk **before** its put (a put that
   writes and then fails, or a crash, is rolled back too), byte-verified after it (downloaded back and compared);
   the `after` hooks (opcache, CF purge; `{urls}` = the edge verify URLs) - a hook is **required** unless written as
   `{"cmd": "…", "optional": true}`: a failed required hook ends the apply with `AFTER_FAILED` (exit 2; the files are on
   prod, nothing is undone automatically because a hook's compensation is not known; `REPORT.md` → "Hooks that need
   manual recovery"; the way on is the hook by hand or `rollback --go`), a failed optional hook is a warning (V2-12);
   `settle` seconds; canary after.
   **Automatic rollback** on a certain crash: a canary that was below 500 before is now 5xx, unreachable or shows a
   PHP fatal / "critical error", confirmed a second time after 10 s: every attempted file back in reverse order (new
   ones are deleted), each restore verified by downloading it again, the `after` hooks again (a failed required
   recovery hook makes the status `ROLLED_BACK_AFTER_FAILED`: the bytes are back, the service recovery is not
   confirmed - the same in `watch` and `rollback`), canary again, stop, notify #THE_OPERATOR#.
   A failed upload or byte-verify = the same rollback of everything attempted so far, followed by the `after` hooks as
   recovery (V3-04: the bytes are back, the opcache / CDN may still hold the half-deployed code) - `FAILED_ROLLED_BACK`,
   or `FAILED_ROLLED_BACK_AFTER_FAILED` when a required hook failed. Every restore (automatic,
   `watch`, manual `rollback --go`) uploads a backup only when its bytes still have the digest recorded at fetch
   (`prod_before_sha`) and verifies the restored prod bytes against that digest, not against the local file: a
   corrupted, truncated, replaced or missing backup is never uploaded and that row is FAILED (V2-09). The rollback
   result is never assumed: `ROLLED_BACK` (every file verified back), `ROLLBACK_INCOMPLETE` (a put/rm failed, a file
   has no backup, or a backup failed the digest check: the report names the files still wrong on prod),
   `ROLLBACK_UNVERIFIED` (put back, bytes not confirmed).
   Then the markers (`verify` in `deploy.json`, `via: edge|origin`): missing marker = `DEPLOYED_UNVERIFIED`, no
   rollback, stop and ask. Statuses: `DRY_OK`, `DEPLOYED`, `DEPLOYED_UNVERIFIED`, `NOT_APPROVED`, `APPROVAL_MISMATCH`,
   `UNRESTORABLE_BINARY`, `FETCH_FAILED`, `PRECHECK_FAILED`, `CANDIDATE_CHANGED`, `BACKUP_INVALID`, `REMOTE_CHANGED`,
   `AFTER_FAILED`, `ROLLED_BACK`, `ROLLED_BACK_AFTER_FAILED`, `ROLLBACK_INCOMPLETE`, `ROLLBACK_UNVERIFIED`, and after a
   failed upload `FAILED_ROLLED_BACK` / `FAILED_ROLLED_BACK_AFTER_FAILED` / `FAILED_ROLLBACK_INCOMPLETE` /
   `FAILED_ROLLBACK_UNVERIFIED`; `watch` adds `HEALTHY`, `DEGRADED` and `WOULD_ROLL_BACK` (dry); exit 0 only for
   `DEPLOYED`, `DRY_OK`, `HEALTHY`, `WOULD_ROLL_BACK` (and a manual `rollback --go` that verified). The `DEPLOY_JSON:`
   line (`status`, `go`, `report`, and per status `problems`, `failed`, `rollback`, `broken`, `baseline_bad`,
   `recovered`, `after_failed` = the failed required hooks, `unverified`) and `deploy/REPORT.md` (attempted /
   uploaded / verified / rolled back per file, every rollback with its result). `deploy/state.json` keys: `files`
   (the rows), `candidate`, `approval`, `canary_override`, `sealed` (the hand resolution the `seal` command recorded -
   the key is `sealed`, the command is `seal`), `canary.<label>` (before / after / after2 / watch / watch2 /
   after-rollback), `after.<label>` (every hook with cmd, exit, optional, tail; labels apply / rollback /
   watch-rollback / manual-rollback), `rollbacks`, `db_rollbacks`, and one record per phase: `precheck`, `applied`,
   `watched`, `rolled_back`.
4. **DB / options / flags** (`db` in `deploy.json`, each with an `apply` and a `rollback` command) are never executed
   by the script. Hard stop: the orchestrator runs them after an explicit "yes" (token wrapper via
   the profile's `put` + `curl -k --resolve <domain>:443:<origin_ip>`, then the profile's `rm` + 404; from Bash
   `MSYS_NO_PATHCONV=1` before the `/home/...` paths, otherwise the file lands in
   `/home/<acct>/C:/Program Files/Git/home/...`), **then** `power_deploy.py watch --go --db-rollback`: canary now -
   `HEALTHY` only when every canary answers (V3-03); a canary that is 5xx / unreachable / fatal now and was fine in
   the `before` snapshot is a crash; one that was already down before the upload (`--force-canary`) is `DEGRADED`,
   exit 2, without an automatic rollback (the upload is not shown to be the cause; `rollback --go` is the
   orchestrator's call, or fix prod and `watch --go` again). On
   a crash it restores the files and runs the `rollback` command of every DB step - the one place where a script runs
   a DB command; their exit codes land in `db_rollbacks`, separate from the file rollback status, because a verified
   file restore says nothing about the DB (the snippet that turns it off is
   ready before the flag is turned on). Other database changes are not rolled back automatically.
5. **`rollback --go`** is the manual rollback at any time (every attempted file, verified); `status` shows the state,
   including an interrupted apply and the archived attempts.
   Through the harness: `power_run.py deploy` runs plan → fetch → the `HARD STOP:` lines and ends with the exact
   `deploy --go --approved <token>` command for the candidate it just fetched. When the deploy state is a **sealed**
   hand resolution (`power_deploy.py seal` after a conflict), `deploy` does not fetch (that would rebuild `upload/`
   and lose the resolution): it shows `status`, a `DEPLOY: SEALED candidate <token>` line and the hard stops, and
   `deploy --go --approved <that token>` applies it (apply re-checks prod against the fetched digests anyway);
   `deploy --refetch` is the explicit way to drop the seal and fetch again. After `apply`, `deploy --go` runs
   `watch --go` (no DB steps) and the run is `done` **only** when the watch exits 0 with `HEALTHY`: `ROLLED_BACK`,
   `ROLLBACK_INCOMPLETE`, `ROLLBACK_UNVERIFIED`, `DEGRADED`, a timeout or unreadable output end with exit 2, phase
   `deploy_failed`, a `HARD STOP:` line and `HARNESS DEPLOY: <outcome>`; `status` then says what follows (`rollback
   --go` unless already rolled back, or `fetch --new-attempt` after the fix). With DB steps the harness stops at
   phase `awaiting_watch` (`HARNESS DEPLOY: APPLIED_AWAITING_WATCH`): the session runs them and `watch --go
   --db-rollback`, and closes the run only on `HEALTHY` (V2-05). **Every yes names the candidate**:
   `--go` and `--yes` require `--approved <attempt:hash>` (`--force` without `--go` is a dry request and needs none;
   with `--go` it needs the token like everything else), and a token of another candidate is refused
   before anything is recorded or uploaded (the orchestrator shows the new candidate and asks again). `deploy --go`
   refuses while a stop is open (`--force` after #THE_OPERATOR#'s yes to each). A `needs_yes` item of `deploy.json` is its own
   stop and `--force` does **not** cover it: each is answered with `deploy --yes <n> --approved <token>`, recorded in
   `harness/state.json` → `deploy_answers` together with the token; after a fetch that produced another candidate
   the old yes is stale and the stop is back (the line names both candidates). After an apply that wrote to prod,
   `deploy` prints `ALREADY_APPLIED` and the deploy status instead of fetching again (the backup stays), and
   `deploy --go` refuses: the follow-up is `watch`, `rollback`, or `power_deploy.py fetch --new-attempt` for a new,
   newly reviewed round with its own token.
6. **After:** prod check by a read-only Sonnet agent (`prod-verifier`, if the project has it), against the local
   screenshots from the final review, at the project's pace (WAF: ~1 request/3 s to the edge). Show the screenshots
   to #THE_OPERATOR#. Ends with „#THE_OPERATOR#, всичко е изпълнено и проверено - …", what was uploaded and a link to
   `deploy/REPORT.md`.

**Hard stops that beat `DEPLOY`** (summary + question): BLOCKED task (only the question about a partial deploy);
an unclosed issue from the final review; a failed local smoke; any DB/option/flag step, change to orders, customers,
`.env`, `wp-config.php` or secrets; an unanswered `needs_yes`; `conflict`, `diverged-no-base` or `merge-error` at
fetch (until the hand resolution is sealed and approved under its own token); binary without backup or with unknown
prod state; a canary that fails even before the upload (`PRECHECK_FAILED`: apply stops before any write; only
`--force-canary` after #THE_OPERATOR#'s yes goes on); the candidate, `deploy.json`, the deploy profile or prod changed since
the fetch, a backup that lost its digest (`BACKUP_INVALID`), an unconfirmed absence of a planned-new file; a yes for
another candidate (`APPROVAL_MISMATCH`, a stale `needs_yes` answer); automatic
rollback, and any rollback that is not `ROLLED_BACK`. All of these are enforced by the scripts (`power_deploy.py`,
`power_run.py deploy`); what stays with the orchestrator: judging a merged diff, the DB steps themselves, the
decision on a partial deploy, and the prod verification after it.
Without a deploy profile: `DEPLOY` is ignored, the deploy is manual per the project's documentation (for example project skills
such as `/deploy-wp` or `/verify-shop`), still only from the orchestrator.
