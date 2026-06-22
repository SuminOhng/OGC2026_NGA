# Solver Notes

This directory contains design history, experiment summaries, and planning
notes for the OGC solver. These files are not part of the submission zip unless
explicitly copied by a packaging script.

Read these notes as context, not as binding truth. Hidden instances may behave
differently from the visible training set. Do not hard-code instance names,
known results, or training-specific block IDs based on anything in this folder.

## Recommended Reading Order

1. `objective_analysis.md`
2. `blocker_graph_schedule_evaluator.md`
3. `blocker_aware_placement_order.md`
4. `objective_improvement_experiments.md`
5. `decomposition_alns_experiments.md`
6. `long_horizon_alns_experiments.md`
7. `obj1_reduction_experiments.md`
8. `fast_feasibility_oracle_plan.md`
9. `related_research_insights.md`
10. `improvement_opportunity_analysis.md`

## Current Interpretation

The objective has three parts:

- `Z1`: total tardiness.
- `Z2`: normalized bay workload imbalance.
- `Z3`: bay preference penalty.

Most current work treats `Z1` as the first-order term. This is not because
`Z2` and `Z3` are unimportant, but because even a small `Z1` regression can
erase a large preference improvement. A low-`Z1` layout often uses non-preferred
bays to keep early-due blocks unblocked. That makes later `Z3` recovery hard.

The promising direction is local, objective-safe recovery:

1. keep the incumbent low-`Z1` solution,
2. identify high `Z3` regret blocks,
3. use bay-assignment MIP or ALNS operators to propose a small reassignment,
4. repair only affected bays,
5. use fast feasibility checks before official validation,
6. accept only objective-safe moves.
