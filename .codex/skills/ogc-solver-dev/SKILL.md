---
name: ogc-solver-dev
description: Develop, refactor, package, and locally validate the OGC 2026 solver submission in this repository. Use when Codex modifies `ogc_solver/myalgorithm.py`, the `ogc_solver/ogc_solver` package, submission packaging scripts, local evaluation scripts, or feasibility-check workflows for OGC solver experiments.
---

# OGC Solver Dev

## Overview

Use this skill to work on the project solver while preserving the competition
submission contract. Treat top-level `ogc_solver/` as the submission root and
`.codex_workspace/` as the isolated local experiment area.

## Companion Skills And Agents

Use companion skills when the task narrows:

- `ogc-coding`: implementation, refactoring, debugging, and test commands.
- `ogc-caveman`: simplifying ideas, exposing assumptions, and designing quick
  sanity checks.

Project-local agent role cards live under `.codex/agents/`:

- `problem-definition-agent.md`: define the solver problem before coding.
- `problem-definition-critic-agent.md`: challenge the definition.
- `code-writing-agent.md`: implement the accepted change.
- `code-critic-agent.md`: review the code.
- `result-check-agent.md`: run and summarize experiments.
- `result-and-final-critic-agent.md`: decide whether to keep or iterate.

## Repository Contract

- Keep `ogc_solver/myalgorithm.py` at the root of the submission folder.
- Preserve `def algorithm(prob_info, timelimit=60)`.
- Put implementation code under `ogc_solver/ogc_solver/`.
- Do not make submitted code depend on files outside the decompressed submission folder.
- Do not include `baseline/baseline/utils.py` in the generated submission zip unless explicitly requested; the evaluator supplies and may overwrite `utils.py`.
- Keep Codex-only runtimes, generated zips, caches, logs, and scratch files under `.codex_workspace/`.

Detailed submission and validation notes live in
`references/submission-and-validation.md`.

## Workflow

1. Inspect `git status --short` and relevant solver files before editing.
2. Modify the narrowest relevant files in `ogc_solver/ogc_solver/` or
   `ogc_solver/myalgorithm.py`.
3. Keep local runner and packaging changes in `scripts/`.
4. Run a lightweight import or single-instance execution after structural edits.
5. Run feasible checks with `scripts/run_solver.py` when `shapely` is available
   in `.codex_workspace/.venv`.
6. Rebuild the submission zip with `scripts/make_submission.py` after changing
   submitted files.

## Commands

Use the repository-local virtual environment when possible:

```powershell
.\.codex_workspace\.venv\Scripts\python.exe -B scripts\run_solver.py train\prob_1.json --timelimit 5
.\.codex_workspace\.venv\Scripts\python.exe -B scripts\batch_eval.py --timelimit 5
.\.codex_workspace\.venv\Scripts\python.exe -B scripts\make_submission.py
```

If `shapely` is missing, feasible checks using provided `utils.py` will fail.
Install dependencies only inside `.codex_workspace/.venv` and request approval
before network package installation.

## Solver Shape

- `ogc_solver/myalgorithm.py`: submission entrypoint only.
- `ogc_solver/ogc_solver/solver.py`: top-level orchestration.
- `ogc_solver/ogc_solver/planner.py`: initial solution construction.
- `ogc_solver/ogc_solver/heuristics/`: constructive and improvement methods.
- `ogc_solver/ogc_solver/repair.py`: infeasibility repair hooks.
- `ogc_solver/ogc_solver/scoring.py`: scoring and penalty helpers.
- `ogc_solver/ogc_solver/state.py`: lightweight data and geometry helpers.

Prefer adding solver behavior behind these modules rather than growing
`myalgorithm.py`.

## Packaging

`scripts/make_submission.py` must create a zip whose root contains
`myalgorithm.py`, `ogc_solver/`, `config/`, and any submitted support files.
The default output belongs under `.codex_workspace/dist/`.
