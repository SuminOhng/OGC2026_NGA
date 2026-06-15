# Solver Package

This package contains the submitted implementation behind `myalgorithm.py`.
The public entrypoint is `solver.solve(prob_info, timelimit)`.

## Main Modules

- `solver.py`: top-level orchestration and fallback handling.
- `planner.py`: constructive feasible solution builders.
- `fast_feasibility.py`: lightweight local candidate checker with periodic
  official validation.
- `scoring.py`: objective and score helpers.
- `state.py`: geometry and state utilities.
- `repair.py` and `stabilize.py`: feasibility-preserving repair helpers.
- `alns/`: main adaptive destroy/repair search.
- `subsolvers/`: decomposition helpers such as bay assignment, EDD feedback,
  and fixed-placement scheduling experiments.
- `heuristics/`: smaller constructive and local-search routines.

## Current Search Shape

The solver should protect `Z1` first. Once tardiness is near zero, secondary
search tries to lower `Z3` without damaging `Z1`. This is hard because the
preferred bay for a block may be exactly where it blocks another block's entry
or exit path.

The safe pattern is:

1. start from a feasible incumbent,
2. propose a small reassignment or placement repair,
3. check local geometry and access-path feasibility,
4. periodically verify with official `check_feasibility`,
5. accept only objective-improving candidates.

Avoid any code path that assumes a visible training instance, a fixed block ID,
or a known cached answer.
