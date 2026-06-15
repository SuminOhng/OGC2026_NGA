# Subsolvers

This folder contains helper solvers used by the ALNS or by long-horizon seed
construction. They should produce candidates, feedback, or repairs; they should
not bypass official feasibility validation.

## Modules

- `decomposition.py`: removal policies and geometry-driven conflict helpers.
- `edd_feedback.py`: fast EDD-style schedule feedback and conflict reporting.
- `hierarchical.py`: bay-assignment and simple placement seed builders.
- `scheduling_mip.py`: fixed-placement scheduling MIP experiments.

## Bay Assignment MIP Role

The bay-assignment MIP is best used as a candidate generator for `Z3`
improvement. It can optimize workload balance and preference cost while adding
soft penalties for known conflict pairs. It does not fully solve placement,
entry-path, exit-path, or final schedule feasibility.

Recommended use:

1. select a small set of high-regret blocks,
2. keep critical low-`Z1` structure mostly fixed,
3. solve a local bay reassignment MIP with conflict penalties,
4. repair affected bay placements,
5. evaluate with the fast feasibility oracle,
6. call official feasibility only on promising candidates.

Do not treat a global low-`Z3` assignment as automatically good. It may undo the
spatial/time structure that made the incumbent low-tardiness.
