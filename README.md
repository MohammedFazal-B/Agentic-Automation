# ITSAR agent — experimental prototype

**Do not use this prototype to assess real compliance or operate a production DUT.** The provided report and device are synthetic; no approved ITSAR report, real target, credentials or approved report template were supplied. The report output is illustrative and is not an approved ITSAR format or a compliance finding.

## Run the mock example

Requires Python 3.10+; no third-party libraries are needed for this example.

```sh
cd itsar-agent
python3 demo/run.py
python3 -m unittest discover -s tests -v
```

The demo prints a fresh run directory, containing a SQLite database, Markdown plans, generated Python script, synthetic terminal-image evidence and an HTML/Markdown/JSON illustrative report. It runs phases 1, 2, 3 and 5 on one mock DUT. The sole example scenario is deliberately small; its heuristic PASS is not a compliance determination. Phase 4 code exists; a separate manual smoke run on a second mock firmware exercised one service-recovery path, but the complete UI-drift and rebind scenarios have not been validated.

## Layout

- `itsar/schema/`: PostgreSQL schema and SQLite development schema.
- `itsar/parsing/`: flexible JSON input parser; `demo/placeholder_report.json` is a synthetic example.
- `itsar/planner/`, `itsar/executor.py`, `itsar/healing.py`: experimental planning, execution and bounded healing.
- `itsar/tools/`: mock, SSH, and browser adapter sketches.
- `itsar/evidence.py`: state-lock and evidence rejection prototype.
- `itsar/scripts/`: experimental script generation and family reuse.
- `itsar/report.py`: illustrative reporting, not an approved template.

## Important limitations before any real use

- PostgreSQL connectivity and triggers, real SSH, browser/MCP integration, and Firefox fallback were **not tested** in this environment. PostgreSQL needs `psycopg[binary]` and an external server. The mock example uses SQLite.
- The code is **not a secure production harness**: real adapters, credential handling, log redaction, input validation, permissions and injection safety need security review. `scripts/reuse.py` executes generated script content with `exec`; never load untrusted database contents. Real-device automation may change configuration; explicit authorization, approval gates, rollback and vendor-specific safeguards are required.
- Script reuse currently lacks per-action audit/evidence parity with normal execution, and generated/rebound files are not yet a reliable production deployment mechanism. Matching sibling scripts by scenario number alone can conflate clauses. Phase 4 needs further implementation and tests.
- Verdicts use a crude text/number heuristic, not a formal requirements evaluator; fail criteria are not comprehensively evaluated. Some locks rely on text rather than a verified DOM element; error-page signatures are not exhaustive. Screenshot rejection is not proof of a perfect state.
- Filesystem output filenames may collide on repeated runs; use a fresh output directory per run as the demo does. Database append-only triggers do not secure files against external tampering. Concurrency/version allocation and PostgreSQL parity need testing.
- Structured step hints are not fully retained by the schema. Not every MCP action or discovery call is individually logged; absent capabilities cannot safely be inferred from an unknown tool or menu. These gaps must be closed against the user's detailed specification before claiming compliance.
- There is no real approved report format, actual DUT profile, validated procedure, orchestrator integration or production CLI provided. Use the mock demo and inspect source as a starting point only.
