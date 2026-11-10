# AI DEV DIRECTOR V3 — TRIAD RESEARCH MODE implementation plan

Branch: `v0.3-triad-research` (created from tag `v0.2.4.7`, commit `cef0a0a5eb46...`).
Status: milestones V3.0-A through V3.0-G implemented and regression-tested. V3.0-H has reached the real
Builder gate with all three subscription CLIs, but the final uninterrupted E2E PASS is pending a Codex
quota-window reset. the trading sample project remains blocked until that PASS exists.

## 0. What TRIAD is, in one paragraph

NORMAL mode is `USER → Codex HEAD → Antigravity BUILDER → Verifier → Codex REVIEWER → HEAD decision`,
one linear pipeline per `run`. TRIAD is a different, higher-level unit — a **Research Session** made of
**Research Cycles** — where Codex, Antigravity, and Claude are all active RESEARCHER participants who
think independently, critique each other, synthesize a hypothesis, and only then hand off to the existing
NORMAL-mode safety pipeline (worktree isolation, Scope Guard, Verifier, Reviewer, human approval) to
actually build and verify an experiment. TRIAD sits *above* NORMAL mode and reuses it for the "build and
verify" step. Real H testing required narrow fixes to the existing Antigravity adapter: truthfully fail an
exit-0 no-op, use file-level Git status, and keep Windows headless Builder execution file-tool-only.

## 1. What the codebase already gives us (from the architecture read, this session)

Confirmed by reading `core/orchestrator.py`, `core/state_machine.py`, `core/verifier.py`, `core/db_path.py`,
`core/provider_manager.py`, `core/safety.py`, `core/result_manager.py`, `dashboard_server.py`,
`dashboard_i18n.py`, `agents/*.py`, `core/process_runner.py`, `schemas/order.py`, `schemas/provenance.py`,
`tests/conftest.py`:

- **No existing "mode" concept fits.** `FailoverMode` (NORMAL/DEGRADED/STOP) is about provider health, not
  run type. The `runs` table has no `mode`/`kind` column. `config.yaml` is an unstructured dict read via
  `.get(key, default)` everywhere — an unrecognized top-level key (`triad:`) is silently ignored today, so
  it's safe to add later without touching NORMAL-mode config parsing.
- **`State` is a plain string-holder, not `enum.Enum`**, and `VALID_NORMAL_TRANSITIONS`/`TERMINAL_STATES`
  are separate module-level structures. Research Cycles get their **own** state model in a **new** module,
  not new members bolted onto `core.state_machine.State` — this matches PHASE 25's instruction directly and
  avoids ever making `runs.current_state` mean two different things.
- **The additive-sidecar-table pattern already exists twice** (`human_action_state`, `result_state` — both
  1:1 with `runs`, added in v0.2.4.7 without touching `runs`' own schema or any `SELECT * FROM runs` call
  site). TRIAD reuses the same pattern: new tables, same `data/director.db` file, owned by a new class, zero
  changes to `runs`/`state_transitions`/`audit_logs`.
- **Claude is hardcoded as "BACKUP"** in exactly one place worth knowing:
  `ProviderManager.resolve_roles()` (`core/provider_manager.py:621-654`) writes the literal string
  `"BACKUP"` into `provider_health.current_role` for ANTHROPIC whenever it holds no NORMAL-mode role. TRIAD
  must **never** write to `current_role` — that column is NORMAL mode's. A concurrent TRIAD cycle and a
  NORMAL run must not fight over what a provider's `current_role` field says. TRIAD participant status is
  tracked in TRIAD's own tables only.
- **No concurrent multi-provider call path exists**, but nothing prevents building one:
  `ProcessTreeRunner.run()` (`core/process_runner.py`) is a stateless classmethod per call (`shell=False`,
  its own `Popen` + two reader threads), safe to call from three threads at once, **except**
  `AntigravityBuilder.run()` holds a process-wide + cross-process lock because it mutates the *global*
  `~/.gemini/antigravity-cli/settings.json` for the run duration. TRIAD's independent-research calls are
  **read-only** (no file writes, no builder permissions setup) — they must call `agy`/`codex`/`claude`
  directly through `ProcessTreeRunner.run()`, the same low-level primitive the agent classes use, **not**
  through `AntigravityBuilder.run()`. This sidesteps the lock entirely for the research stages; only the
  later EXPERIMENT_BUILDING stage (milestone E) goes through the real `AntigravityBuilder`.
- **`BASELINE_FAILED` is triggered purely by `Verifier.verify()["passed"] == False`**, i.e. a non-zero exit
  from the configured `test_command` (`core/orchestrator.py:327-351`). If no `test_command` is configured,
  that stage auto-passes with `test_counts={"passed":0,...}` — already harmless. The failure case is a
  *configured* `test_command` (e.g. `pytest`) finding zero tests and exiting non-zero (pytest exit code 5).
  GREENFIELD mode (milestone D) needs a **new** evidence state, not a change to this function's behavior for
  existing callers — `run_baseline_if_required` stays untouched; a new sibling method is added for the
  GREENFIELD path and only TRIAD (and, later, an explicit user "new project" flow) calls it.
- **Test isolation is automatic.** `tests/conftest.py`'s `isolate_provider_db` fixture is `autouse=True` —
  every new TRIAD test gets an isolated DB, stubbed CLIs, and the `RealCliCallDuringUnitTest` guard for
  free. TRIAD unit tests need no special markers. Real-CLI TRIAD smoke tests (milestone H) get
  `@pytest.mark.integration_cli`, matching the existing convention.
- **`build_status_payload()`'s key set has no collision risk** — no existing key is named `research`,
  `triad`, or anything `mode`-prefixed beyond `system_mode*`. A new `research` key is a safe, additive
  dashboard payload field.

## 2. Isolation contract (non-negotiable, re-verified at every milestone)

1. Zero edits to `core/state_machine.py`'s `State`, `VALID_NORMAL_TRANSITIONS`, `TERMINAL_STATES`, or any
   method signature already used by NORMAL mode.
2. Zero edits to `core/orchestrator.py`'s existing stage methods (`run_baseline_if_required`,
   `run_builder_stage`, `validate_post_build`, `run_reviewer_with_failover`,
   `run_decision_with_failover`, `_finalize`, `run`, `resume`). TRIAD calls them (for the experiment step)
   but does not modify them.
3. Zero edits to `core/safety.py`'s `APPROVABLE_ENTRY`, `APPROVABLE_POST_BUILD`, or control-plane constants
   (`CONTROL_PLANE_DIRS/FILES/PATTERNS`) — confirmed unchanged (byte-identical to baseline) as of the H
   audit on 2026-09-10. **Exception, recorded here rather than left as a silent contract violation**: H
   testing found `is_path_allowed`/`is_path_protected`/`normalize_rel_path` never actually matched the
   documented `dir/**` glob convention (pure literal-prefix matching, so `"src/**"` matched nothing) —
   this affects NORMAL mode too, since these are NORMAL's own Scope Guard methods, not TRIAD-only. It was
   fixed with a new `_matches_path_rule` classmethod (segment-aware, glob-only where a glob is present,
   identical literal-prefix behavior otherwise) and is strictly *stricter* than the old code (it rejects
   sibling-directory bleed a naive prefix match would have allowed) — independently safety-audited SAFE,
   not a weakening, but this is a real edit to a Scope Guard method and the "zero edits" wording above no
   longer holds literally. TRIAD's experiment step is still subject to the exact same (now more correct)
   Safety Gate as a NORMAL run.
4. Zero writes to `provider_health.current_role` from TRIAD code. TRIAD participant status lives only in
   TRIAD's own tables.
5. Zero new PAYG/API-key/SDK call paths. Every TRIAD provider call goes through
   `ProcessTreeRunner.run()` with the same `REAL_CLI_BASENAMES` guard already enforced today.
6. Every pre-existing regression remains unmarked and passing. Assertions that previously called an
   exit-0/no-change Builder result `COMPLETED` now expect the truthful fail-closed
   `BUILDER_NO_CHANGES`; no successful NORMAL behavior was weakened.

## 3. Data model

New file `core/research_state.py`, new class `ResearchStateMachine`, using the same `get_db_path()` /
`sqlite3.Row` pattern as `StateMachine`, in the **same** `data/director.db` file (or `TEST_DB_PATH` under
`DIRECTOR_TESTING=1` — identical mechanism, zero new isolation code needed).

```
research_sessions
  session_id        TEXT PRIMARY KEY
  project_path      TEXT NOT NULL
  moonshot          TEXT NOT NULL         -- the user's raw challenge text, verbatim
  project_type      TEXT NOT NULL         -- "EXISTING" | "GREENFIELD" (milestone D classifies this)
  status            TEXT NOT NULL         -- ACTIVE | MILESTONE_COMPLETE | RESEARCH_STOPPED | HUMAN_REQUIRED
  cycle_budget      INTEGER NOT NULL      -- bounded-autonomy cap (PHASE 17), default from config
  cycles_completed  INTEGER NOT NULL DEFAULT 0
  stopped_reason    TEXT                  -- set when status leaves ACTIVE
  created_at        TEXT NOT NULL
  updated_at        TEXT NOT NULL

research_cycles
  cycle_id          TEXT PRIMARY KEY
  session_id        TEXT NOT NULL REFERENCES research_sessions(session_id)
  cycle_number      INTEGER NOT NULL
  stage             TEXT NOT NULL         -- ResearchState.* (see below)
  created_at        TEXT NOT NULL
  updated_at        TEXT NOT NULL
  UNIQUE(session_id, cycle_number)

research_cycle_transitions                -- audit trail, mirrors state_transitions
  id                INTEGER PRIMARY KEY AUTOINCREMENT
  cycle_id          TEXT NOT NULL
  from_stage        TEXT NOT NULL
  to_stage          TEXT NOT NULL
  details           TEXT
  timestamp         TEXT NOT NULL

research_participants                     -- TRIAD's OWN role ledger; never touches provider_health
  cycle_id          TEXT NOT NULL
  domain            TEXT NOT NULL         -- OPENAI | ANTHROPIC | GOOGLE
  role              TEXT NOT NULL         -- "RESEARCHER" (extensible later)
  status            TEXT NOT NULL         -- PENDING | ACTIVE | COMPLETE | UNAVAILABLE
  PRIMARY KEY (cycle_id, domain)
```

Milestones B/C/F add sibling tables (`research_proposals`, `research_critiques`, `research_experiments`,
`research_interpretations`, `research_memory`) under the same `session_id`/`cycle_id` keys — not designed
in full here; each gets its own milestone plan note before implementation, per PHASE 30.

### `ResearchState` stage constants (PHASE 25, exact list adopted verbatim)

```
RESEARCH_INTAKE, INDEPENDENT_RESEARCH, CROSS_CRITIQUE, SYNTHESIS, EXPERIMENT_PLANNING,
EXPERIMENT_BUILDING, EXPERIMENT_VERIFYING, EXPERIMENT_REVIEW, RESULT_INTERPRETATION, NEXT_CYCLE,
MILESTONE_COMPLETE, HUMAN_REQUIRED, RESEARCH_STOPPED
```
`TERMINAL_CYCLE_STAGES = {MILESTONE_COMPLETE, HUMAN_REQUIRED, RESEARCH_STOPPED}`. Forward graph mirrors the
listed order; `HUMAN_REQUIRED`/`RESEARCH_STOPPED` reachable from any non-terminal stage (same "terminal
escape" rule `state_machine.py` already uses for NORMAL mode, reimplemented independently here rather than
imported, so a future change to NORMAL's rule can never silently affect TRIAD).

## 4. TRIAD provider readiness (milestone A)

New pure function (new file `core/triad_provider.py`), read-only, no DB writes beyond what
`ProviderManager` already does for its own health bookkeeping:

```python
def assess_triad_readiness(pm: ProviderManager) -> dict:
    # usable = HEALTHY, or RECOVERING with a claimable/claimed trial (existing pm.usable_domains())
    usable = set(pm.usable_domains())
    all3 = {"OPENAI", "ANTHROPIC", "GOOGLE"}
    missing = all3 - usable
    if not missing:
        return {"state": "TRIAD_READY", "usable": sorted(usable), "missing": []}
    if len(missing) == 1:
        return {"state": "TRIAD_DEGRADED", "usable": sorted(usable), "missing": sorted(missing)}
    return {"state": "TRIAD_BLOCKED", "usable": sorted(usable), "missing": sorted(missing)}
```

`TRIAD_DEGRADED` is never silently treated as success — PHASE 3 requires **explicit human approval** to run
a 2-provider session at all, and the session/cycle record must carry `degraded=True` / list which domain
was missing, forever, in its evidence. That approval gate and the "explicitly degraded" record wiring is
part of milestone B (session intake), not A — milestone A only builds and tests the readiness computation
itself.

## 5. Milestone plan (PHASE 30, adopted with one clarification)

| Milestone | Scope |
|---|---|
| **V3.0-A — complete** | `ResearchStateMachine` schema + stage transitions; `assess_triad_readiness()` |
| **V3.0-B — complete** | Session intake, explicit degraded approval, finite budget, contamination-safe independent prompts |
| **V3.0-C — complete** | Three concurrent read-only official CLI runners, independent research, cross critique, HEAD synthesis, strict schemas and provenance |
| **V3.0-D — complete** | Evidence-based GREENFIELD qualification and explicit `NOT_APPLICABLE_GREENFIELD` baseline state |
| **V3.0-E — complete** | Synthesis → strict experiment ORDER → existing worktree/Builder/Verifier/Reviewer pipeline |
| **V3.0-F — complete** | Three independent interpretations, cross critique, Research Memory, repeated-hypothesis/no-progress/cycle-budget gates |
| **V3.0-G — complete** | Korean NORMAL/TRIAD switch and read-only Research Center payload/view |
| **V3.0-H — in progress** | Real three-provider E2E test exists and all provider research/critique/synthesis stages have succeeded. Final uninterrupted Builder→Verifier→Reviewer→interpretation→memory PASS remains pending after quota reset. |

Each milestone: implement → new tests → full regression pytest → report (files changed, tests, real
behavior observed, problems found) → only then start the next milestone. the trading sample project stays untouched
until H is complete and the user explicitly starts a TRIAD session on it from the dashboard (PHASE 28).

## 6. Success criteria (PHASE 31, unchanged, restated as the acceptance bar for milestone H)

The ten criteria in PHASE 31 are E2E-provable only once milestones A–H are all done; they are not
individually checkable after milestone A alone. This plan tracks which milestone first makes each criterion
checkable:

1–2 (independent thinking, real critique) → C. 3 (synthesis) → C. 4 (real experiment) → E. 5 (3-way result
interpretation) → F. 6 (failure remembered) → F. 7 (evidence-driven next direction) → F/G. 8 (greenfield
start) → D+H. 9 (HEAD-only UX) → G. 10 (provenance+safety intact throughout) → verified at every milestone,
not just H.

## 7. Current verification ledger (2026-09-10)

- V3.0-G full regression: `355 passed, 1 deselected`, warnings 0.
- Post-H adapter fixes targeted regression: `199 passed`.
- Current default full regression after the H adapter fixes: `357 passed, 2 deselected` in 900.18s,
  warnings 0, hang 0. The deselections are the opt-in real CLI H test and the existing integration dashboard test.
- Actual official subscription CLIs observed: Codex `0.153.0-alpha.5`, Claude Code `2.1.220`,
  Antigravity CLI `1.1.28`; API-key policy remained disabled.
- Real independent research and real cross critique completed successfully for OPENAI, ANTHROPIC, and
  GOOGLE in the same GREENFIELD session; real Codex synthesis and experiment planning also completed.
- Actual H exposed and closed three headless Builder defects: Windows command permission matching,
  sandbox-incompatible path grants, and exit-0/no-change truthfulness. A file-tool-only real Builder smoke
  now passes and produces exact file-level provenance.
- The latest full-H attempt stopped fail-closed at the Builder boundary before changes because Windows AGY
  requested interactive terminal elevation. Builder terminal access is now denied entirely; Git inspection
  and pytest remain the Director/Verifier's responsibility.
- Codex's five-hour window reached 53% after the real H attempts. The next automatic reset is
  2026-09-10 13:31 KST. No reset credit is consumed without explicit user authorization.

Release gate remains: one fresh, uninterrupted `test_v300h_real_three_provider_greenfield_cycle` PASS,
followed by the default full regression. Until then V3.0-H is not complete and the trading sample project must not run.

## 8. Claude handoff audit + two further real H attempts (2026-09-10, later same day)

Independently re-verified the A-G/H claims above against the actual working-tree diff and code (not just
this doc) using two parallel read-only audits, plus direct inspection of all 13 persisted
`research/v3h-e2e/TRIAD-*/` artifact trees from every real H attempt run so far. Findings:

- Diff-vs-plan consistency: confirmed accurate with one correction now folded into §2 item 3 above (the
  `core/safety.py` Scope Guard glob-matching fix is a real, safety-tightening edit, not the "zero edits"
  the plan originally claimed).
- Safety audit of every modified pre-existing file (`core/safety.py`, `dashboard_server.py`,
  `agents/codex_head.py`, `agents/codex_reviewer.py`, `agents/antigravity_builder.py`, and the five
  touched NORMAL-mode regression tests): all SAFE. No weakened assertion, no new PAYG/API/key path, no
  dashboard endpoint behavior change, dashboard bind/auth untouched.
- B-G re-verification: all CONFIRMED against the real code (session intake's degraded-approval gate,
  structural contamination guards for both independent research and independent interpretation, genuine
  `ThreadPoolExecutor` concurrency, `TriadResearchRunner` bypassing `AntigravityBuilder`'s global lock
  entirely for read-only research calls, GREENFIELD's fail-closed predicates and still-mandatory real
  post-build Verifier, `TriadExperimentOrchestrator` overriding only 3 baseline-related methods and
  otherwise calling unmodified `Orchestrator.run()`, real no-progress/repeated-hypothesis/cycle-budget
  enforcement, and a read-only-GET-only `/research` dashboard route with no session-start capability
  exposed anywhere in the UI).
- Cross-checking all 13 persisted H attempts (2026-09-08 23:40 through 2026-09-10, before this round)
  showed failures scattered non-deterministically across different providers and different stages -
  not one reproducible blocker. One new root cause was found and is now fixed (below); everything else
  observed is real subscription-CLI call variance across a long chain (15-20 real calls per full attempt),
  not a design defect - every failure correctly halted to `HUMAN_REQUIRED` with no silent retry, no
  fabricated success, and no partial-success accumulation.

**Real bug found and fixed this round**: a captured real Claude/Anthropic independent-research response
(`TRIAD-260910133559-.../independent_research/anthropic_raw.txt`) was a well-formed, on-topic, complete
proposal that Pydantic rejected only because `assumptions` (declared `List[str]`) was returned as one bare
string instead of a one-item list - a real-world LLM output-shape slip, not malformed/unsafe content.
`ResearchOrchestrator._coerce_list_fields()` (new, `core/research_orchestrator.py`) now widens a bare
non-empty string into `[string]` **only** for fields a schema itself declares `List[str]`, applied at all
six research-schema validation sites (proposal, critique, synthesis, interpretation, interpretation
critique, memory). It never adds/drops a key, never touches a non-list field, and a genuinely wrong type
(dict/number/list-of-non-string) still fails closed exactly as before - verified by replaying the exact
captured payload through the fix, and by two new regression tests (`test_8b`/`test_8c` in
`tests/test_v300c_research_pipeline.py`) proving both the widening and its limits. Full regression after
the fix: `360 passed, 2 deselected`, 0 failures.

**Two further real H attempts under this fix, both still HUMAN_REQUIRED, neither counted as partial
progress toward a PASS**:
1. `TRIAD-260910133559-...` (pre-fix): OPENAI and GOOGLE independent research succeeded; ANTHROPIC's
   response was the bare-string `assumptions` case above - this is the attempt whose artifact drove the fix.
2. `TRIAD-260910135321-...` (post-fix, ~20 min later): ANTHROPIC and GOOGLE independent research both
   succeeded (confirming the fix works on a live call, not just the replayed capture); OPENAI's call
   produced no captured output at all (`ResearchProviderCallError` raised before any raw response existed),
   correctly halting the whole session to `HUMAN_REQUIRED` per the "never substitute Claude into the Codex
   slot, never fall back to API/SDK" rule. Root cause not further diagnosed in-session (doing so would mean
   more real Codex calls without knowing whether quota is the cause); most likely explanation is Codex
   five-hour quota exhaustion from the volume of real H attempts run today (13 attempts before this round
   plus 2 more this round), consistent with the 53%-usage reading already on record above, but this is not
   confirmed and no further real attempts were made pending a quota-state check.

V3.0-H is still not complete. the trading sample project was not touched. `AI_DEV_DIRECTOR_MASTER_STATE.md`/
`MASTER_STATE.txt` are intentionally left as Codex last wrote them - they get updated only after an actual
H PASS, per the standing instruction.

## 9. V3.0-H final hardening: bounded transient provider retry (2026-09-10, later same day)

No existing retry/recovery policy was found in `AI_DEV_DIRECTOR_MASTER_STATE.md` or anywhere earlier in
this doc (confirmed by search before designing anything) - the design below is new, scoped exactly to the
bounds the user specified, and lives in exactly one place: `core/research_runner.py`.

**Problem**: one research cycle needs ~15-20 real subscription-CLI calls (3 independent researchers x
[research + cross-critique + result-interpretation] + synthesis + experiment-plan + 1 interpretation-cross
+ 1 memory-consolidation call, each a single real inference). Before this round, any single transient
transport/runtime hiccup on any one of those calls discarded the entire cycle to `HUMAN_REQUIRED` -
correctly fail-closed, but too fragile for a long-running autonomous research loop.

**Design** (`core/research_runner.py`, `TriadResearchRunner`):
- `MAX_ATTEMPTS = 2` (initial attempt + exactly 1 bounded retry, the smaller of the two examples the user
  offered - never more, no exponential backoff, no infinite loop).
- Retry classification (`_is_retryable`) is a narrow allowlist, built on the existing, already-tested
  `core.provider_manager.ErrorCategory.classify()` text classifier (reused, not reimplemented, so quota/
  auth/provider-unavailable phrase-matching can never drift out of sync between NORMAL and TRIAD):
  - `GOOGLE_STREAM_EMPTY_RESPONSE` (Antigravity terminal SUCCESS, empty response - the exact case the user
    observed) - always retryable.
  - `GOOGLE_STREAM_RESULT_MISSING` (malformed/incomplete NDJSON stream, no `result` event at all) - always
    retryable.
  - A genuine process/CLI timeout (`ProcessTreeRunner`'s own timeout, or a non-zero exit whose stderr/stdout
    classifies as `ErrorCategory.TIMEOUT`) - retryable.
  - Everything else - `QUOTA`, `AUTH`, `PROVIDER_UNAVAILABLE`, `CONFIG_ERROR`, `EXECUTION_ERROR`, `UNKNOWN`,
    `RESEARCH_READ_ONLY_VIOLATION` (the provider wrote to its isolated read-only workspace - a policy
    violation, not a transient failure) - **never retried**, fails closed on the first attempt exactly as
    before. `UNKNOWN` is deliberately excluded rather than included: the user's own framing was "a *clear*
    transient failure" (명확한), and an unclassified error is by definition not positively known to be
    transient.
- A retry always resends the byte-identical original `prompt` string into a fresh `_invoke_once` call, in
  the same isolated worktree - never the previous attempt's output, never another provider's output, and
  never a different provider's CLI. Contamination guards (`core/research_prompts.py`) and the concurrency
  model (`core/research_orchestrator.py`'s `ThreadPoolExecutor`) are untouched; retry is entirely internal
  to one already-dispatched call.
- Every attempt - failed or final - is preserved. `research_call_provenance` gained two columns
  (`attempt_number`, `retry_reason`, additive `ALTER TABLE` migration) and `ResearchOrchestrator._record_call`
  now writes one row per prior failed attempt (executable/version/exit_code/duration/error/attempt_number)
  before the final row, whose own `retry_reason` names the prior attempt's failure. Nothing scope-relevant
  outside `research_runner.py`/`_record_call` changed: `core/safety.py`, `core/orchestrator.py`, and every
  Reviewer/Builder agent file were checked and confirmed to not import `core.research_runner` at all, so
  Scope Guard, protected-path checks, the Reviewer's `changes_required` verdict, and any experiment-result
  failure remain exactly as immediate/non-retryable as before - retry cannot reach them by construction.
- Never in scope, and not built: retrying experiment/Builder/Verifier/Reviewer failures, retrying a schema
  validation failure (that is `core/research_orchestrator.py`'s `_coerce_list_fields`/Pydantic territory,
  a content problem, not a transport one), or any cross-provider substitution on any retry.

**Tests** (`tests/test_v300h_transient_retry.py`, 8 tests covering the user's 9-item list - tests 5 and 6
share one structural test since both reduce to the same code-import fact): empty-response-then-success
succeeds on attempt 2; empty-response-twice raises with both attempts recorded; quota and auth errors each
fail on attempt 1 with zero retries; a genuine timeout is retried while an explicitly-named
provider-unavailable error is not (one test, both directions); the real `REAL_CLI_CALL_DURING_UNIT_TEST`
guard still fires on the very first attempt (proving retry adds no second path to a real CLI); Scope Guard/
Reviewer/Builder files structurally never import the retrying runner; and an orchestrator-level test proves
the independent-research contamination guard and full per-attempt provenance both survive a retried domain
sitting alongside two non-retried domains in the same cycle. All 8 new tests pass; the full existing V3 test
suite (A through H, 85 tests together) passes unchanged; full default regression after this round:
`368 passed, 2 deselected` in 650.31s, 0 failures (360 from section 8 + these 8 new retry tests).

V3.0-H is still not complete - this is hardening for the *next* real attempt, not a retroactive pass. No
real H E2E was run in this round (explicitly deferred until Codex quota is confirmed available, per the
user's instruction not to repeat H immediately). the trading sample project was not touched.

## 10. Failure-learning loop correction + H frozen OPEN, Moonshot begins (2026-09-11/12)

**Real H run (fresh session)**: reached BUILDING -> VERIFYING -> REVIEWING -> DECIDING for real, twice, both
landing on Reviewer `changes_required` with real P1-only findings (Decimal.normalize() context-dependent
overflow + unhandled UnicodeDecodeError; then NaN/Infinity non-standard JSON constants + 1/1.0/1e0
representation collapse). NORMAL's `apply_final_pass_gate` correctly escalated both to `HUMAN_REQUIRED`
(NORMAL's own design: any non-`pass` verdict needs a human in a live single-operator session) - but TRIAD's
`execute_experiment()` treated that identically to a Scope Guard violation, immediately ending the whole
research session instead of treating it as research data.

**Correction implemented** (`core/research_orchestrator.py::execute_experiment`, minimal, TRIAD-side only):
a second `safe_research_failure` predicate, `reviewer_found_defect`, alongside the pre-existing
`verifier_rejected` one - `builder.status==COMPLETED and verification.passed is True and scope_violations==0
and review.verdict=="changes_required" and not review.p0`. `scope_violations==0` is proven (by tracing every
`_stop()` call site in `core/orchestrator.py`) to occur only when `_finalize()` was reached, i.e. a genuine
HEAD/Reviewer decision was rendered - this structurally excludes REVIEWER_EXCEPTION-synthesized verdicts,
the Same-Provider Independence Guard demotion, and any DECIDING-stage provider failure. P0 stays an
unconditional immediate escalation (severity floor). Zero edits to `core/orchestrator.py`/`core/safety.py`/
any agent. `tests/test_v300i_failure_learning_loop.py` (15 new tests, A-O per the user's spec) covers both
directions: ordinary Reviewer defects now reach Result Interpretation -> Cross Critique -> Research Memory ->
`start_next_cycle()` with only compact memory facts (no raw cycle-1 leakage, checked byte-for-byte); Scope
Guard/protected-path/P0/CONFIG_ERROR/cycle-budget/no-progress all still escalate exactly as before. Full
regression after this change: `383 passed, 2 deselected`, 0 failures.

**Real multi-cycle H rewritten and re-attempted 3x** (`tests/test_v300h_real_three_provider_e2e.py` now
loops cycles generically via `run_through_synthesis`/`plan_experiment`/`execute_experiment`/interpretation/
`start_next_cycle`, writing the full trajectory - not just the last cycle - to `latest_evidence.json`). All
three fresh real attempts died at the *same* point: the real Antigravity Builder's very first real call.

Two real bugs found and fixed in `agents/antigravity_builder.py` (NORMAL-mode code, never exercised against
the real `agy` CLI before this session):
1. Missing `read_file` grants for the Builder's own injected `GEMINI.md`/`.agents/hooks.json` - fixed,
   confirmed working (attempt 2 of the original single-cycle H round got a real Builder success from this).
2. `agy` auto-updated `1.2.0 -> 1.2.1` and started requiring a `ListDir` tool confirmation that the
   `read_file`-analogous `list_dir(<dir>)` grants (worktree root + every ancestor of every allowed path,
   verified present in the applied settings.json via the CLI's own log) do not satisfy - log always shows
   `tool_confirmation_manager.go:188] Print mode: soft-denying tool confirmation "ListDir" at step 14`,
   unchanged across three separate real attempts with the fix in place. **Not resolved this round.**

Per explicit user instruction (2026-09-12, "STOP INFRASTRUCTURE LOOP / START REAL MOONSHOT"): the fresh
attempt above was declared the final timeboxed real H attempt. It did not PASS. V3.0-H is frozen
**OPEN / NON-BLOCKING** (see `AI_DEV_DIRECTOR_MASTER_STATE.md`) rather than pursued further - this is a
third-party CLI version-drift limitation in one Builder permission surface, not a safety/architecture defect;
every safety invariant (Scope Guard, containment, Reviewer fail-closed, subscription-CLI-only) was
independently re-verified intact. the trading sample project research/architecture work now proceeds per the same
instruction; real-money/production-credential/irreversible actions remain blocked pending separate approval
regardless of H's status.
