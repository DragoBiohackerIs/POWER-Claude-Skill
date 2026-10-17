# POWER: Gate A, Gate B and the `/clear` hint

## Gate A (only without `START`)

Message with: table of tasks (id, what, files, size, dependencies), the contracts, non-goals,
the risks (with `QUESTION:` separately), the baseline decisions (PRE-BROKEN / WEAK / SUSPECT), the expected agents
(3 × tasks + ~8 fixed; above 50 - say so), the cost line (below), link to PLAN.md; ends with „#THE_OPERATOR#, трябва да
решиш …" and waits for „да". Changes → `tasks.json` → `prepare` → `baseline`; #THE_OPERATOR#'s corrections → `request.md`,
section „Решения на #THE_OPERATOR#" (the planner reads the file; after `/clear` the fresh session reads it instead of
remembering).

**Cost line** (17.10, `cost_forecast`): „Очаквана цена: ~$X (диапазон $A-$B; медиана на роля и модел от N предишни
run-а; ориентировъчно; вече похарчени $Y)". The history is the `harness/calls.jsonl` of the sibling runs in the same
`docs/power/` folder (this run left out) that have at least one priced call; with fewer than 3 such runs the line is
not there. Expected calls: work = the tasks; fix / verify / finish = the tasks × the history's count of the role per
work call; lenses = the lens list; review / finalfix / finalverify = the history's average per run. A call costs the
median (p25-p75 for the range) of its role and model, else the role's median over all models (named at the end of
the line; a role with no data at all is left out, named too); a Codex model adds „Codex: квота, не в $". Indicative,
not a cap.

## Gate B (only without `DEPLOY`)

Summary per SKILL §7, ends with „#THE_OPERATOR#, трябва да решиш дали да деплойна: …" and waits. Besides the counts, the
final review and the hard stops, `gate_b.md` has (17.10):

- **„НЕПРОВЕРЕНО / ПРОПУСНАТО:"**: what the run did not check, the deploy-relevant lines first - lenses with no
  report after the retry, run files `deploy.json` leaves out, each PASS task's `unchecked` items, the request items
  the reviewer found missing (with „подадена на финалната поправка" and whether finalverify confirmed it) or partial,
  agents a turn or budget cap stopped, lenses FAST skipped, the lenses' `Skipped:` and recon's `Unmapped:` lines. At
  most 10 lines, then „(+N още в REPORT.md)"; REPORT.md has every line in „## Непроверено / пропуснато", every
  request row in „## Покритие на заявката" and every dismissed finding in „## Отхвърлени находки на лещите". A run
  whose state predates 17.10 has no section.
- **„Отхвърлени находки на лещите: N (REPORT.md)"**, or the warning „ВНИМАНИЕ: review.json няма списък `dismissed`
  …" when the reviewer wrote no `dismissed` list.
- **The coverage questions c1-c3**, among the hard stops as „неотговорен въпрос cN (покритие, harness): …": c1 lenses
  with no report, c2 run files not deployed, c3 a PASS task whose verifier did not check something about a file
  `deploy.json` uploads; asked only when `deploy.json` uploads files. Relay each one; #THE_OPERATOR#'s yes →
  `deploy --yes cN --approved <token>` (bound to the candidate and to the exact text: a changed gap is a new
  question; `--force` does not cover them).

## `/clear` hint (only at the gates: the only pauses; with `START DEPLOY` there are none)

Before the gate message, check the session's context size (`/context` or a status hook). If it is above 150k, the
session is not continued after the gate: #THE_OPERATOR# runs `/clear` and a fresh session picks the run up from its files.
`/clear` replaces `/compact` (decided 2026-10-15): a fresh session costs only the base context plus the hand-off,
compact runs one summarization call over the whole history and leaves a lossy summary that duplicates the run files.
Conditions: nothing is running in the background (at Gate A: `baseline` has finished and the decisions are in
PLAN.md), all of #THE_OPERATOR#'s corrections are in „Решения на #THE_OPERATOR#" of `request.md`. Once per gate. Below 150k there is no hint.

1. Write the hand-off block in the project's hand-off file (for example
   `<project>/.project/HANDOFF.md`, newest block on top). Inside: the run
   folder (absolute), the gate, flags and models, the candidate token at Gate B, and an ordered „СЛЕДВА" list whose
   first item is the exact command for „да" with absolute paths (Gate A: `power_run.py continue …`; Gate B:
   `power_run.py deploy … --go --approved <token> --yes N --yes cN … --claimed-by "<the WHOLE CLAIM topic, verbatim>"` -
   `--claimed-by` is compared to the topic exactly, a shortened topic stops APPLY at the CLAIM check), the second
   item for changes (Gate A: answers into `request.md` „Решения на #THE_OPERATOR#" + `continue --replan`), then which
   SKILL.md section to read (Gate A: §4 т.6 „Right before the Workflow"; Gate B: §8 and `reference/deploy.md`).
2. The gate message ends with the clear block instead of the „#THE_OPERATOR#, …" decision line:

       Контекстът е над 150k. Хендофът е в <файл>.
       #THE_OPERATOR#, пусни /clear и после напиши: да (или „следва" + промените)

After `/clear`, a bare „да" (or „следва") as the first message = read the newest hand-off and execute item 1 of its
„СЛЕДВА" list (a rule in the global `CLAUDE.md`); „следва" + text = #THE_OPERATOR#'s changes: item 2.
