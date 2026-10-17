export const meta = {
  name: 'power-execute',
  description: 'POWER: executor per task, script checks, clean-context verification by a model other than the author, escalation to the planner model',
  phases: [
    { title: 'Work', detail: 'step 2 model implements each task' },
    { title: 'Check', detail: 'power_check.py: diff against the snapshot + mechanical checks' },
    { title: 'Verify', detail: 'clean-context verifier, never the model that wrote the code' },
    { title: 'Fix', detail: 'the executor fixes once' },
    { title: 'Escalate', detail: 'step 1 model finishes the task' },
  ],
}

// args (from `codex_task.py workflow-args`): {
//   tasks: [{ id, depends_on: [...], files: [...] }],   // ids, dependencies, owned files; instructions are files
//   run_dir, project_dir,                                // absolute, forward slashes
//   step1: 'opus' | 'fable' | 'astra',                   // plans, verifies, finishes after escalation
//   step2: 'sonnet' | 'haiku' | 'opus' | 'sol' | 'luna' | 'astra', // executes and fixes once
//   codex_models: { astra: '<slug>', sol: '<slug>', luna: '<slug>' },
//   helpers: { codex: '<abs>/codex_task.py', check: '<abs>/power_check.py' },
//   agent_types: { runner, executor, verifier },         // the skill's agent types that are installed (optional)
//   mode: 'work' | 'verify',                              // verify: only checks + verifier for tasks [{id, author, round,
//                                                        //   previous}]; previous = the task's earlier outcome from
//                                                        //   results.json, merged like the harness does (V2-14)
// }
// Claude agents read <run_dir>/prompts/<task>.<kind>.md (power_prompts.py write --kinds tasks).
const { tasks, run_dir, project_dir, step1, step2, codex_models, helpers, mode = 'work', agent_types = {} } = args

const CODEX = new Set(['astra', 'sol', 'luna'])
const FALLBACK = { astra: 'opus', sol: 'sonnet', luna: 'haiku' } // same tier when a Codex job fails (ChatGPT limit, crash)
// Same-model trap: if the executor is the step 1 model, "step 1 finishes the task" would be a third attempt by
// the model that already failed twice - the other strong model finishes instead (Opus <-> Fable, Astra -> Opus).
const OTHER = { opus: 'fable', fable: 'opus', astra: 'opus' }
const ESCALATE = step1 === step2 ? OTHER[step1] : step1
// Thin runners (script checks, restore, Codex jobs) copy a command's output; switch to 'sonnet' if a live run
// shows haiku mangling CHECK_JSON.
const RUNNER = 'haiku'
// The skill's own agent types (agents/*.md, installed with `power_check.py install`): no CLAUDE.md or memory in the
// agent's context and a hard maxTurns - the start weight of an agent drops from ~62k to ~10k tokens, and that
// prefix is re-sent on every call. Not installed = the default workflow subagent.
const TYPE = role => (agent_types && agent_types[role] ? { agentType: agent_types[role] } : {})

const STR = { type: 'string' }
const STR_LIST = { type: 'array', items: STR }
const RESULT = {
  type: 'object',
  properties: {
    status: { type: 'string', enum: ['DONE', 'PLAN_MISMATCH', 'CODEX_FAILED'] },
    files_changed: STR_LIST, mismatch: STR, summary: STR, how_verified: STR, open_issues: STR_LIST, plan_change: STR,
  },
  required: ['status', 'files_changed', 'mismatch', 'summary', 'how_verified', 'open_issues', 'plan_change'],
}
// `unchecked` (what the verifier did not check, at most 5) is optional: a verifier that omits it still passes the schema
const VERDICT = {
  type: 'object',
  properties: { pass: { type: 'boolean' }, problems: STR_LIST, plan_problem: { type: 'boolean' }, notes: STR, unchecked: STR_LIST },
  required: ['pass', 'problems', 'plan_problem', 'notes'],
}
const CHECK = {
  type: 'object',
  properties: { pass: { type: 'boolean' }, failures: STR_LIST, warnings: STR_LIST, scope_changes: STR_LIST, diff_path: STR, report_path: STR },
  required: ['pass', 'failures', 'warnings', 'scope_changes', 'diff_path', 'report_path'],
}
const SHELL = { type: 'object', properties: { ok: { type: 'boolean' }, message: STR }, required: ['ok', 'message'] }

// ---------- who checks whom ----------
// Code is never verified by the model that wrote it: step 1 verifies; if step 1 wrote it, Opus; if Opus wrote it, Fable.
function verifierFor(author) {
  if (author !== step1) return step1
  return author === 'opus' ? 'fable' : 'opus'
}
const claudeBackup = author => (author === 'opus' ? 'fable' : 'opus')

// ---------- commands and prompts ----------
const py = (helper, sub, rest) =>
  `PYTHONUTF8=1 python "${helper}" ${sub} --run-dir "${run_dir}" --project-dir "${project_dir}" ${rest}`
const promptFile = (t, kind) => `${run_dir}/prompts/${t.id}.${kind}.md`
const claudeMsg = (t, kind, extra) =>
  `POWER ${kind} for task ${t.id}. Read ${promptFile(t, kind)} and follow it exactly.` + (extra ? `\n\n${extra}` : '')

const codexRunner = (command, prep) => `You are a thin runner for a Codex job. Do not do the task yourself and do not read or edit project files.${prep}
Run with the Bash tool (timeout 600000): ${command}
If the output says "STATUS: RUNNING" or "STATUS: QUEUED", run exactly the same command again, until it says "STATUS: DONE" or "STATUS: FAILED".
Then return the object after "RESULT_JSON:" exactly as it is.`

const checkRunner = (t, n, retry) => `You are a thin runner. Do not read or edit project files and do not judge the result.${retry ? ' (second attempt)' : ''}
Run with the Bash tool (timeout 600000): ${py(helpers.check, 'check', `--task ${t.id} --round ${n}`)}
Return the object after "CHECK_JSON:" exactly as it is, every string verbatim. If the command prints no CHECK_JSON, return pass false, failures ["INFRA: <the last error line>"], warnings [], scope_changes [], diff_path "", report_path "".`

const restoreRunner = t => `You are a thin runner. Do not read or edit project files yourself.
Run with the Bash tool (timeout 600000): ${py(helpers.check, 'restore', `--task ${t.id} --keep stash`)}
Return ok true and message = the line that starts with "RESTORE:" verbatim; if there is none, ok false and message = the last error line.`

const codexFailed = r => !r || r.status === 'CODEX_FAILED' || String(r.notes || '').startsWith('CODEX_FAILED')
// A failed Codex job's detached runner may still be editing: it is killed (codex_task.py kill) before any Claude
// fallback starts, as in the harness (V2-06A / V2-14). A kill that is not confirmed (KILLED / NOT_RUNNING / NONE) means
// no fallback: the task is BLOCKED as infrastructure instead of two writers in the same files.
const killRunner = (sub, t, extra, model) => `You are a thin runner. Do not read or edit project files and do not judge anything.
Run with the Bash tool (timeout 300000): ${py(helpers.codex, 'kill', `--kind ${sub} --task ${t.id} --model ${codex_models[model]}${extra}`)}
Return ok true when the output has a line "KILL: KILLED", "KILL: NOT_RUNNING" or "KILL: NONE", else ok false; message = that KILL: line verbatim (or the last line of the output).`
async function codexKill(phase, sub, t, extra, model) {
  const k = await agent(killRunner(sub, t, extra, model), { label: `kill:${t.id}:${sub}`, phase, model: RUNNER, effort: 'low', schema: SHELL, ...TYPE('runner') })
  const ok = !!(k && k.ok && /KILL: (KILLED|NOT_RUNNING|NONE)/.test(String(k.message || '')))
  log(`${t.id}: Codex ${sub} runner ${ok ? String(k.message || '').trim() : 'NOT killed (' + (k ? k.message : 'kill runner died') + ') - no Claude fallback'}`)
  return ok
}
const noFallback = (by, sub) => ({ by, status: 'CODEX_FAILED', infra: `the failed Codex ${sub} runner could not be killed (codex_task.py kill): no Claude fallback, the files may still be edited` })
const isInfra = c => !c || (c.failures || []).some(f => String(f).startsWith('INFRA:'))
const tagged = (r, by) => (r ? { ...r, by } : null)
const strip = r => r && {
  status: r.status, files_changed: r.files_changed, mismatch: r.mismatch, summary: r.summary,
  how_verified: r.how_verified, open_issues: r.open_issues, plan_change: r.plan_change,
}
const norm = p => String(p || '').replace(/\\/g, '/').replace(/^\.\//, '').replace(/^\//, '').toLowerCase()
// Files the executor itself reports outside its list: cannot be auto-fixed, but must not pass unnoticed.
const strayFiles = (t, r) => {
  const owned = new Set((t.files || []).map(norm))
  return (r && r.files_changed || []).filter(f => !owned.has(norm(f)))
}

function codexJob(label, phase, sub, t, extra, prep, model, schema) {
  const cmd = py(helpers.codex, sub, `--task ${t.id} --model ${codex_models[model]}${extra}`)
  return agent(codexRunner(cmd, prep), { label, phase, model: RUNNER, effort: 'low', schema, ...TYPE('runner') })
}

// ---------- the steps ----------
async function work(t) {
  if (CODEX.has(step2)) {
    const r = await codexJob(`work:${t.id}`, 'Work', 'work', t, '', '', step2, RESULT)
    if (!codexFailed(r)) return tagged(r, step2)
    if (!(await codexKill('Work', 'work', t, '', step2))) return noFallback(step2, 'work')
    log(`${t.id}: Codex ${step2} failed (${r ? r.summary : 'runner died'}), ${FALLBACK[step2]} takes over`)
    return tagged(await agent(claudeMsg(t, 'work'), { label: `work:${t.id}`, phase: 'Work', model: FALLBACK[step2], schema: RESULT, ...TYPE('executor') }), FALLBACK[step2])
  }
  return tagged(await agent(claudeMsg(t, 'work'), { label: `work:${t.id}`, phase: 'Work', model: step2, schema: RESULT, ...TYPE('executor') }), step2)
}

async function fix(t, r, v, n) {
  const problems = v.problems.length ? [...v.problems] : ['(the verifier gave no details)']
  if (v.check && v.check.report_path) problems.push(`Full check report: ${v.check.report_path}; diff: ${v.check.diff_path}`)
  const by = r.by
  if (CODEX.has(by)) {
    const pf = `${run_dir}/codex/${t.id}.fix${n}.problems.json`
    const f = await codexJob(`fix:${t.id}#${n}`, 'Fix', 'fix', t, ` --round ${n} --problems-file "${pf}"`,
      `\nFirst, with the Write tool, create ${pf} containing exactly this JSON: ${JSON.stringify(problems)}`, by, RESULT)
    if (!codexFailed(f)) return tagged(f, by)
    if (!(await codexKill('Fix', 'fix', t, ` --round ${n}`, by))) return noFallback(by, 'fix')
    log(`${t.id}: Codex fix #${n} failed, ${FALLBACK[by]} takes over`)
  }
  const model = CODEX.has(by) ? FALLBACK[by] : by
  const extra = `Round ${n}. Your previous result: ${JSON.stringify(strip(r))}\nProblems to fix:\n${problems.map(p => '- ' + p).join('\n')}`
  return tagged(await agent(claudeMsg(t, 'fix', extra), { label: `fix:${t.id}#${n}`, phase: 'Fix', model, schema: RESULT, ...TYPE('executor') }), model)
}

// Script checks via a haiku runner; one retry when the runner dies or reports INFRA (git lock, missing snapshot).
async function checks(t, n) {
  let c = await agent(checkRunner(t, n, false), { label: `check:${t.id}#${n}`, phase: 'Check', model: RUNNER, effort: 'low', schema: CHECK, ...TYPE('runner') })
  if (isInfra(c)) c = await agent(checkRunner(t, n, true), { label: `check:${t.id}#${n}`, phase: 'Check', model: RUNNER, effort: 'low', schema: CHECK, ...TYPE('runner') })
  return c
}

async function verify(t, n, author, c) {
  let who = verifierFor(author)
  const evidence = `Round ${n}. Evidence (the mechanical checks passed):\n- diff of the task's files against the snapshot: ${c.diff_path}\n- report of the mechanical checks: ${c.report_path}`
  if (CODEX.has(who)) {
    const v = await codexJob(`verify:${t.id}#${n}`, 'Verify', 'verify', t, ` --round ${n}`, '', who, VERDICT)
    if (!codexFailed(v)) return tagged(v, who)
    if (!(await codexKill('Verify', 'verify', t, ` --round ${n}`, who))) return { pass: false, infra: true, problems: [noFallback(who, 'verify').infra], plan_problem: false, notes: '', by: who }
    who = claudeBackup(author)
    log(`${t.id}: Codex verify #${n} failed, ${who} verifies`)
  }
  let v = await agent(claudeMsg(t, 'verify', evidence), { label: `verify:${t.id}#${n}`, phase: 'Verify', model: who, schema: VERDICT, ...TYPE('verifier') })
  if (!v) v = await agent(claudeMsg(t, 'verify', evidence + '\n(second attempt)'), { label: `verify:${t.id}#${n}`, phase: 'Verify', model: who, schema: VERDICT, ...TYPE('verifier') })
  return v ? tagged(v, who) : { pass: false, infra: true, problems: ['the verifier died twice'], plan_problem: false, notes: '', by: who }
}

// Script checks first (cheap); the verifier model only sees what the script let through.
// infra: the run is broken (not the code) - the task is BLOCKED, nobody is sent to "fix" it.
// broken: every failure is the check command's own error (CHECK-BROKEN: from the script) - same treatment: the
// orchestrator fixes the check in tasks.json and re-runs this workflow with mode 'verify'; the edits stay.
const isBroken = c => !!c && !c.pass && (c.failures || []).length > 0 && c.failures.every(f => String(f).startsWith('CHECK-BROKEN:'))
const checkMismatch = m => /^\s*CHECK:/.test(String(m || ''))
async function gate(t, n, author) {
  const c = await checks(t, n)
  if (isInfra(c)) return { pass: false, infra: true, problems: c ? c.failures : ['the check runner died twice'], plan_problem: false, notes: '', by: 'checks', check: c }
  if (isBroken(c)) return { pass: false, broken: true, problems: c.failures, plan_problem: false, notes: c.warnings.join('; '), by: 'checks', check: c }
  if (!c.pass) return { pass: false, problems: c.failures, plan_problem: false, notes: c.warnings.join('; '), by: 'checks', check: c }
  return { ...(await verify(t, n, author, c)), check: c }
}

async function restore(t) {
  const r = await agent(restoreRunner(t), { label: `restore:${t.id}`, phase: 'Escalate', model: RUNNER, effort: 'low', schema: SHELL, ...TYPE('runner') })
  if (!r || !r.ok) return { ok: false, message: r ? r.message : 'restore runner died' }
  return r
}

async function escalate(t, r, verdicts, mismatch, restored) {
  const history = verdicts.map((v, i) => ({ round: i + 1, by: v.by, problems: v.problems }))
  const reason = mismatch
    ? `The executor stopped with PLAN_MISMATCH: ${mismatch}`
    : `The task was rejected ${verdicts.length} time(s).`
  const state = mismatch
    ? `The executor's partial edits were rolled back: the task's files are at the snapshot state (${restored}).`
    : 'The files hold the executor\'s last attempt; continue from it or rewrite it.'
  if (CODEX.has(ESCALATE)) {
    const hf = `${run_dir}/codex/${t.id}.finish.history.json`
    const prep = `\nFirst, with the Write tool, create ${hf} containing exactly this JSON: ${JSON.stringify({ reason, verdicts: history })}`
    const e = await codexJob(`finish:${t.id}`, 'Escalate', 'finish', t, ` --history-file "${hf}"${mismatch ? ' --restored' : ''}`, prep, ESCALATE, RESULT)
    if (!codexFailed(e)) return tagged(e, ESCALATE)
    if (!(await codexKill('Escalate', 'finish', t, '', ESCALATE))) return noFallback(ESCALATE, 'finish')
    log(`${t.id}: Codex finish failed, opus finishes`)
  }
  const model = CODEX.has(ESCALATE) ? 'opus' : ESCALATE
  const lines = [reason]
  for (const h of history) lines.push(`- round ${h.round} (${h.by}): ${h.problems.join(' | ')}`)
  lines.push(state)
  if (r && !mismatch) lines.push(`The executor's last result: ${JSON.stringify(strip(r))}`)
  lines.push(`The check reports of the earlier rounds are in ${run_dir}/checks/${t.id}.r*/report.md.`)
  return tagged(await agent(claudeMsg(t, 'finish', lines.join('\n')), { label: `escalate:${t.id}`, phase: 'Escalate', model, schema: RESULT, ...TYPE('executor') }), model)
}

function outcome(t, status, r, verdicts, reason, escalated, strays, planChanges, authors) {
  const last = verdicts[verdicts.length - 1]
  const stray = [...new Set(strays || [])]
  // V3-02 (as in the harness): every model whose edits are in the task's files now; `author` stays the last one
  const who = [...new Set([...(authors || []), ...(r && r.by ? [r.by] : [])])].sort()
  // P1-04 (as in the harness): an executor that itself reports files outside its list is BLOCKED whatever the
  // verifier said; the verifier judged the task's files, not the foreign edit.
  if (stray.length && status === 'PASS') {
    status = 'BLOCKED'
    reason = `edited files outside the task's list: ${stray.join(', ')}`
  }
  return {
    task: t.id, status, escalated: !!escalated, reason: reason || '',
    author: r ? r.by : null,
    authors: who,
    verified_by: verdicts.map(v => v.by),
    rounds: verdicts.length,
    result: strip(r),
    last_problems: last && !last.pass ? last.problems : [],
    notes: last ? last.notes || '' : '',
    // what the verifier that passed the task did not check (the session's Gate B "НЕПРОВЕРЕНО" section)
    unchecked: last && last.pass ? (last.unchecked || []).slice(0, 5) : [],
    stray_files: stray,
    // files changed outside every task list that only the diff shows (check's scope_changes): SUSPECT for the
    // orchestrator, the verdict stands (the harness turns it into a hard stop)
    scope_changes: [...new Set(verdicts.flatMap(v => (v.check && v.check.scope_changes) || []))],
    // every plan_change any call reported (work, fix, finish, orchestrator), append-only; reported is not approved
    plan_changes: planChanges || [],
    check: last && last.check ? { diff_path: last.check.diff_path, report_path: last.check.report_path, warnings: last.check.warnings } : null,
  }
}

// verify-only (mode 'verify'): the new verdict is MERGED into the task's earlier outcome (prev = results.json row,
// passed as t.previous by workflow-args), as the harness does (V2-10 / V2-14): result, author, earlier verdicts,
// plan changes and reported stray files survive. A stray is resolved here only when tasks.json now lists the file
// for the task (t.files is the current list) - recorded as an orchestrator plan change; the snapshot-bytes check
// of the harness has no equivalent in the Workflow (the orchestrator runs `power_run.py verify` for that).
function verifyOutcome(t, v, author, n, prev) {
  const reason = v.infra ? 'infrastructure: ' + v.problems[0] : v.broken ? 'check broken: ' + v.problems[0] : v.pass ? '' : 'rejected in verify-only mode'
  const status = reason ? 'BLOCKED' : 'PASS'
  if (!prev) return { ...outcome(t, status, { by: author }, [v], reason, false, [], []), verify_only_round: n }
  const owned = new Set((t.files || []).map(norm))
  const strays = (prev.stray_files || []).filter(f => !owned.has(norm(f)))
  const resolved = (prev.stray_files || []).filter(f => owned.has(norm(f)))
  const history = [...(prev.plan_changes || [])]
  if (resolved.length) history.push({ kind: 'orchestrator', by: 'orchestrator', text: `tasks.json now lists ${resolved.join(', ')} for ${t.id}` })
  const earlier = (prev.verified_by || []).map(b => ({ by: b, pass: true, problems: [], notes: '', check: null }))
  const o = outcome(t, status, { ...(prev.result || {}), by: prev.author || author }, [...earlier, v], reason, prev.escalated, strays, history, prev.authors)
  o.scope_changes = [...new Set([...(prev.scope_changes || []), ...o.scope_changes])]
  // V3-07: the resolutions of the earlier verify-only rounds are history too - merged, never replaced
  return { ...o, stray_resolved: [...new Set([...(prev.stray_resolved || []), ...resolved])], verify_only_round: n }
}

// verify-only that crashed (a helper threw): BLOCKED with the error, but the earlier evidence of the task (result,
// author(s), verifiers, strays, plan changes, resolutions) survives as in a merge - a bare row would read as a task
// that never ran (V3-07).
function verifyFailed(t, author, n, err, prev) {
  const msg = 'workflow error: ' + (err && err.message ? err.message : err)
  const p = prev || {}
  const earlier = (p.verified_by || []).map(b => ({ by: b, pass: true, problems: [], notes: '', check: null }))
  const r = p.result ? { ...p.result, by: p.author || author } : { by: p.author || author }
  const o = outcome(t, 'BLOCKED', r, earlier, msg, !!p.escalated, p.stray_files, p.plan_changes, p.authors)
  o.scope_changes = [...new Set([...(p.scope_changes || []), ...o.scope_changes])]
  return { ...o, stray_resolved: p.stray_resolved || [], verify_only_round: n }
}

// work -> gate -> (fix -> gate) -> escalate -> gate -> BLOCKED.
// Two rejections, a plan problem seen by the verifier, or PLAN_MISMATCH send the task to step 1.
async function runTask(t) {
  const verdicts = []
  const strays = []
  const history = []
  const authors = new Set()   // V3-02: every model whose edits are in the files now (a restore clears it)
  const noteStrays = r => {
    const s = strayFiles(t, r)
    if (s.length) { strays.push(...s); log(`${t.id}: ${r.by} reports files outside its list: ${s.join(', ')}`) }
  }
  const notePlan = (kind, x) => {
    if (x && x.plan_change && !history.some(h => h.kind === kind && h.text === x.plan_change)) history.push({ kind, by: x.by, text: x.plan_change })
  }
  const done = (status, r, reason, escalated) => outcome(t, status, r, verdicts, reason, escalated, strays, history, [...authors])
  let r = await work(t)
  if (!r) return done('BLOCKED', null, 'the executor died')
  if (r.infra) return done('BLOCKED', r, 'infrastructure: ' + r.infra)
  authors.add(r.by)
  noteStrays(r)
  notePlan('work', r)
  let mismatch = r.status === 'PLAN_MISMATCH' ? r.mismatch || 'no details' : ''
  if (checkMismatch(mismatch)) return done('BLOCKED', r, `check broken (reported by ${r.by}): ${mismatch}`)
  if (!mismatch) {
    let v = await gate(t, 1, r.by)
    verdicts.push(v)
    if (v.infra) return done('BLOCKED', r, 'infrastructure: ' + v.problems[0])
    if (v.broken) return done('BLOCKED', r, 'check broken: ' + v.problems[0])
    if (v.pass) return done('PASS', r, '')
    if (!v.plan_problem) {
      const f = await fix(t, r, v, 1)
      if (f && f.infra) return done('BLOCKED', r, 'infrastructure: ' + f.infra)
      if (f) {
        r = f
        authors.add(r.by)
        noteStrays(r)
        notePlan('fix', r)
        if (r.status === 'PLAN_MISMATCH') mismatch = r.mismatch || 'no details'
        else {
          v = await gate(t, 2, r.by)
          verdicts.push(v)
          if (v.infra) return done('BLOCKED', r, 'infrastructure: ' + v.problems[0])
          if (v.broken) return done('BLOCKED', r, 'check broken: ' + v.problems[0])
          if (v.pass) return done('PASS', r, '')
        }
      }
    }
  }
  if (checkMismatch(mismatch)) return done('BLOCKED', r, `check broken (reported by ${r.by}): ${mismatch}`)
  log(`${t.id}: escalating to ${ESCALATE}` + (ESCALATE !== step1 ? ` (not ${step1}: it is also the executor)` : '') + (mismatch ? ` (PLAN_MISMATCH: ${mismatch.slice(0, 120)})` : ''))
  let restored = ''
  if (mismatch) {
    const rs = await restore(t)
    if (!rs.ok) return done('BLOCKED', r, 'infrastructure: restore failed: ' + rs.message, true)
    restored = rs.message
    authors.clear()   // the files are back at the snapshot: nothing of the earlier authors is in them
  }
  const e = await escalate(t, r, verdicts, mismatch, restored)
  if (!e) return done('BLOCKED', r, 'the planner model died', true)
  if (e.infra) return done('BLOCKED', r, 'infrastructure: ' + e.infra, true)
  authors.add(e.by)
  noteStrays(e)
  notePlan('finish', e)
  if (e.status === 'PLAN_MISMATCH') return done('BLOCKED', e, (checkMismatch(e.mismatch) ? `check broken (reported by ${e.by}): ` : '') + (e.mismatch || 'QUESTION: no details'), true)
  const v = await gate(t, verdicts.length + 1, e.by)
  verdicts.push(v)
  if (v.infra) return done('BLOCKED', e, 'infrastructure: ' + v.problems[0], true)
  if (v.broken) return done('BLOCKED', e, 'check broken: ' + v.problems[0], true)
  return done(v.pass ? 'PASS' : 'BLOCKED', e, v.pass ? '' : 'rejected after escalation', true)
}

// ---------- verify-only: re-check and re-verify tasks after the orchestrator fixed the plan (a broken check) ----------
// args.tasks: [{ id, author, round, files, previous }]; the work is already in the files, nobody is sent to redo it;
// the verdict is merged into `previous` (the row of results.json) - see verifyOutcome.
if (mode === 'verify') {
  const results = await Promise.all(tasks.map(async t => {
    const n = Number(t.round) || 1, author = t.author || step2
    try {
      const v = await gate(t, n, author)
      return verifyOutcome(t, v, author, n, t.previous || null)
    } catch (err) {
      return verifyFailed(t, author, n, err, t.previous || null)
    }
  }))
  log(`verify-only: PASS ${results.filter(r => r.status === 'PASS').length} of ${results.length}`)
  return results
}

// ---------- scheduling: every task starts as soon as its own dependencies PASS (no waves) ----------
const byId = Object.fromEntries(tasks.map(t => [t.id, t]))
for (const t of tasks) for (const d of t.depends_on || []) if (!byId[d]) throw new Error(`${t.id} depends on unknown ${d}`)
const seen = {}
const visit = (id, path) => {
  if (path.includes(id)) throw new Error(`dependency cycle: ${[...path, id].join(' -> ')}`)
  if (seen[id]) return
  seen[id] = true
  for (const d of byId[id].depends_on || []) visit(d, [...path, id])
}
for (const t of tasks) visit(t.id, [])

const started = {}
function run(id) {
  if (!started[id]) {
    const t = byId[id]
    started[id] = Promise.all((t.depends_on || []).map(d => run(d))).then(async deps => {
      const bad = deps.filter(d => d.status !== 'PASS').map(d => d.task)
      if (bad.length) {
        log(`${id}: not started, depends on ${bad.join(', ')}`)
        return outcome(t, 'BLOCKED_DEP', null, [], `depends on ${bad.join(', ')}`)
      }
      try {
        return await runTask(t)
      } catch (err) {
        return outcome(t, 'BLOCKED', null, [], `workflow error: ${err && err.message ? err.message : err}`)
      }
    })
  }
  return started[id]
}

const results = await Promise.all(tasks.map(t => run(t.id)))
const count = s => results.filter(r => r.status === s).length
const strayTotal = results.reduce((n, r) => n + r.stray_files.length, 0)
log(`PASS ${count('PASS')}, BLOCKED ${count('BLOCKED')}, BLOCKED_DEP ${count('BLOCKED_DEP')}, escalated ${results.filter(r => r.escalated).length}` +
  (strayTotal ? `, files reported outside task lists: ${strayTotal} (see stray_files)` : ''))
return results
