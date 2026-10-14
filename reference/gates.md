# POWER: Gate A, Gate B and the `/compact` hint

## Gate A (only without `START`)

Message with: table of tasks (id, what, files, size, dependencies), the contracts, non-goals,
the risks (with `QUESTION:` separately), the baseline decisions (PRE-BROKEN / WEAK / SUSPECT), the expected agents
(3 × tasks + ~8 fixed; above 50 - say so), link to PLAN.md; ends with „#THE_OPERATOR#, трябва да решиш …" and waits for
„да". Changes → `tasks.json` → `prepare` → `baseline`; #THE_OPERATOR#'s corrections → `request.md`, section „Решения на
#THE_OPERATOR#" (the planner reads the file; after `/compact` the session reads it instead of remembering).

## Gate B (only without `DEPLOY`)

Summary per SKILL §7, ends with „#THE_OPERATOR#, трябва да решиш дали да деплойна: …" and waits.

## `/compact` hint (only at the gates: the only pauses; with `START DEPLOY` there are none)

Before the gate message, run `turn_stats.py`. If `Ctx` (the third line, the absolute value) is above 150k,
put in the message body, before the „#THE_OPERATOR#, …" line, a ready command that #THE_OPERATOR# runs **before** he replies
(the model cannot run `/compact` itself). Conditions: nothing is running in the background (at Gate A: `baseline` has
finished and the decisions are in PLAN.md), all of #THE_OPERATOR#'s corrections are in „Решения на #THE_OPERATOR#" of `request.md`. Once per
gate; if #THE_OPERATOR# does not run it, it is not repeated. Below 150k there is no hint: compact costs one call over the whole context.
Fill the brackets with the real values, paths absolute.

What `/compact` does: it only rewrites the context into a summary and then waits. The text after it is a focus hint
for the summary, not a prompt: nothing runs after it, and #THE_OPERATOR# must still send their decision as a separate message.
The „След отговора: прочети …" part is addressed to the summarized session (so it knows where to pick up), not to
#THE_OPERATOR#. So the hint always comes as two numbered steps, and the second one is their answer:

    Контекстът е над 150k. Две стъпки:
    1. Пусни `/compact …` (свива паметта, не изпълнява нищо; след него сесията чака).
    2. После напиши решението си като ново съобщение: „да" или промените.

Gate A:

    /compact POWER run <run> (project <project>). Стоим на Врата A и чакаме отговор на #THE_OPERATOR# (да или промени по плана). Флагове: <POWER/START/DEPLOY>. Модели: стъпка 1 <…>, стъпка 2 <…>, стъпка 3 <…>. Решенията на Драго са в <run>/request.md, планът в <run>/PLAN.md и tasks.json, проучването в <run>/recon/. След отговора: прочети <абсолютен път>/SKILL.md от §4 т.6 („Right before the Workflow") нататък.

Gate B: the same with „Стоим на Врата B и чакаме решение за деплой", „REPORT.md, DEPLOY.md, deploy.json,
review.json и deploy/ в <run>" and „прочети SKILL.md §8 и reference/deploy.md".
