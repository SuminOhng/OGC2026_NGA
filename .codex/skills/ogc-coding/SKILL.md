---
name: ogc-coding
description: Implement, refactor, debug, and test OGC 2026 solver code. Use when Codex edits `ogc_solver/myalgorithm.py`, modules under `ogc_solver/ogc_solver`, local runner scripts, packaging scripts, or algorithmic code paths while preserving the competition submission contract.
---

# OGC Coding

## Overview

Use this skill for code-producing work in the OGC solver repository. Pair it
with `ogc-solver-dev` for repository rules and with `ogc-caveman` when the
implementation idea needs to be simplified before coding.

## Coding Rules

- Keep `ogc_solver/myalgorithm.py` thin; it should import and call solver code.
- Put solver behavior under `ogc_solver/ogc_solver/`.
- Keep submitted code self-contained inside top-level `ogc_solver/`.
- Avoid runtime dependence on files above the submission root.
- Prefer small, testable functions over large planner rewrites.
- Make all output solution values deterministic and integer where required.
- Preserve `algorithm(prob_info, timelimit=60)`.
- Keep generated artifacts under `.codex_workspace/`.

## Workflow

1. Inspect current files and `git status --short`.
2. Identify the smallest module boundary for the change.
3. Edit with `apply_patch`.
4. Run a direct import or one-instance solver smoke test.
5. Run feasible checks when `shapely` is available.
6. Rebuild the submission zip after submitted code changes.

## Preferred Commands

```powershell
.\.codex_workspace\.venv\Scripts\python.exe -B scripts\run_solver.py train\prob_1.json --timelimit 5
.\.codex_workspace\.venv\Scripts\python.exe -B scripts\batch_eval.py --timelimit 5
.\.codex_workspace\.venv\Scripts\python.exe -B scripts\make_submission.py
```

## Review Before Finishing

- Does the code still import from the submission root?
- Does the solution format still match the problem statement?
- Did the change improve feasibility, objective, speed, or maintainability?
- Did any local-only dependency leak into submitted code?
