# OGC2026_NGA

This repository develops the National Grasshopper Association solver for
OGC 2026. The source problem statement is `problem.pdf`.

## AI Read This First

The job of this repository is to solve the OGC 2026 spatial block scheduling
problem in `problem.pdf` and package a valid submission. This is not generic
Python application development: every code change should be judged by whether
it improves a competition solver while preserving the submission contract.

The real score is measured by the organizers on hidden problem instances that
are not in this repository. Local `train/` results are only a proxy for hidden
performance. Do not hard-code training instances, local file paths, known
solutions, or assumptions that only hold for the visible data.

The important code is the solver under `ogc_solver/`, and the competition
evaluator imports `ogc_solver/myalgorithm.py` and calls:

```python
algorithm(prob_info, timelimit=60)
```

The returned solution must be feasible under the provided `utils.py`
`check_feasibility` logic. A faster or lower-looking objective is useless if the
official checker rejects the solution.

## Problem In One Page

For every block, the solver must decide all of these at once:

1. which bay receives the block,
2. the integer `(x, y)` placement inside that bay,
3. the block orientation,
4. the `ENTRY` time and `EXIT` time.

The key feasibility rules from `problem.pdf` are:

- every block must be assigned exactly once,
- every block must have exactly one `ENTRY` and one `EXIT`,
- `ENTRY >= release_time`,
- `EXIT - ENTRY >= processing_time`,
- a block occupies its bay during `[ENTRY, EXIT)`,
- blocks in the same bay at the same time must be layer-collision-free,
- crane `ENTRY` and `EXIT` operations must have a clear vertical path,
- within a day, `EXIT` operations must come before `ENTRY` operations,
- output indices and coordinates are 0-based and integer-valued.

The objective is:

```text
minimize w1 * Z1 + w2 * Z2 + w3 * Z3
```

- `Z1`: total tardiness, `sum(max(0, EXIT_i - due_date_i))`
- `Z2`: normalized bay workload imbalance
- `Z3`: bay preference penalty

Current experiments show that weighted `Z1` usually dominates the score. The
default solver strategy should therefore be:

1. produce a feasible solution first,
2. reduce tardiness aggressively,
3. protect early-due blocks from exit-path blockers,
4. use workload balance and preference mostly as tie-breakers or late polish,
5. never accept a `Z2` or `Z3` improvement that worsens the official objective,
6. keep strict time guards because hidden timelimits vary.

Good solver ideas in this repo include due-date-aware construction, small
destroy/repair ALNS, access-blocker removal, early-exit protection, objective-
safe final polish, and schedule compression when the time budget is large
enough.

## Repository Map

- `problem.pdf`: official problem statement. This is the source of truth.
- `train/`: training problem instances.
- `ogc_solver/`: submission root. This is the main area to edit.
- `ogc_solver/myalgorithm.py`: required competition entrypoint.
- `ogc_solver/ogc_solver/solver.py`: top-level solver orchestration.
- `ogc_solver/ogc_solver/planner.py`: initial construction and planning.
- `ogc_solver/ogc_solver/alns/`: adaptive destroy/repair search.
- `ogc_solver/ogc_solver/heuristics/`: constructive and local heuristics.
- `ogc_solver/ogc_solver/subsolvers/`: focused helper policies.
- `ogc_solver/ogc_solver/scoring.py`: objective and scoring helpers.
- `ogc_solver/ogc_solver/state.py`: lightweight geometry/state utilities.
- `scripts/`: local runner, batch evaluation, and submission packaging.
- `docs/`: experiment notes and current design reasoning.
- `baseline/` and `ogc2026/`: provided references and tester code. Do not edit
  these unless the task explicitly asks for it.
- `.codex_workspace/`: local caches, logs, generated zips, and scratch results.

## Development Workflow

Before changing solver logic:

1. Read this file and `ogc_solver/README.md`.
2. Check `git status --short`; this repo may already contain user changes.
3. Edit the narrowest relevant module under `ogc_solver/ogc_solver/`.
4. Keep `ogc_solver/myalgorithm.py` thin.
5. Do not make submitted code depend on files outside `ogc_solver/`.
6. Validate with at least one local run when dependencies are available.
7. Rebuild the submission zip after submitted code changes.

Useful commands from the repository root:

```powershell
.\.codex_workspace\.venv\Scripts\python.exe -B scripts\run_solver.py train\prob_1.json --timelimit 5
.\.codex_workspace\.venv\Scripts\python.exe -B scripts\batch_eval.py --timelimit 5
.\.codex_workspace\.venv\Scripts\python.exe -B scripts\make_submission.py
```

If `shapely` or the provided checker dependencies are missing, feasibility
checks may fail locally even when the code imports.

## Experiment Notes

Read the documents in `docs/` as design history, not as submission files.
Recommended order:

1. `docs/objective_analysis.md`
2. `docs/objective_improvement_experiments.md`
3. `docs/decomposition_alns_experiments.md`
4. `docs/long_horizon_alns_experiments.md`
5. `docs/obj1_reduction_experiments.md`

The most important current lesson is simple: make the solution feasible, then
drive down `Z1` without breaking feasibility or worsening the official
objective.

## Current Solver Doctrine

The strongest current search direction is not "make every term better at once."
The solver first protects `Z1`, because `w1 * Z1` usually dominates the score.
Only after tardiness is near zero should the search spend serious budget on
`Z2` and `Z3`.

The hard part is lowering `Z3` while preserving `Z1`. `Z3` improves when blocks
move back toward preferred bays, but those moves can destroy the exit paths and
time windows that made the low-`Z1` solution possible. A block can be late not
because its own placement is bad, but because another active block in the same
bay blocks its vertical entry or exit path. Therefore a good secondary move is
not just "move block to preferred bay"; it is:

1. move a high-preference-regret block,
2. repair the affected bay placements,
3. protect early-due exit paths,
4. keep conflicting pairs apart when they caused tardiness before,
5. accept only if the official objective improves and `Z1` does not regress.

The existing bay-assignment MIP is useful in this role as a local candidate
generator. It should suggest lower-`Z3` bay reassignments for selected blocks,
with soft penalties for known bad conflict pairs and workload imbalance. The
MIP assignment should then be validated by placement repair, the fast
feasibility oracle, and finally the official checker for promising candidates.
Avoid using a global obj3-only reassignment as a final answer: it can look good
on preferences while quietly rebuilding a high-tardiness layout.

Current long-horizon experiments on visible training data show that reaching
`Z1 = 0` is possible on hard instances, but the remaining gap is often `Z3`.
This is a training insight, not a license to hard-code `prob_20` or any visible
instance. Hidden instances remain the target.
