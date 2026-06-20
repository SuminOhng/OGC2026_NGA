# Improvement Opportunity Analysis

Date: 2026-06-15

This note converts the related-research survey into concrete improvement
opportunities for the current OGC solver. It is based on:

- `docs/related_research_insights.md`
- current solver modules under `ogc_solver/ogc_solver/`
- existing experiment notes in `docs/objective_improvement_experiments.md`,
  `docs/decomposition_alns_experiments.md`,
  `docs/obj1_reduction_experiments.md`, and
  `docs/fast_feasibility_oracle_plan.md`

The main conclusion is not "add a totally new solver." The current architecture
already follows the right broad pattern: feasible seed, ALNS, local repair,
fast feasibility oracle, and official objective-safe acceptance. The remaining
improvement opportunity is to make that architecture more diagnostic,
access-blocker-aware, and robust across instance sizes.

## Current State

The code already has many of the right building blocks:

- `solver.py` keeps a protected fallback and returns only an official
  objective-improving feasible candidate.
- `alns/engine.py` owns most of the search, including access-aware removal,
  obj1 polish, preference polish, same-bay relayout, exit-path blocker clusters,
  due-window repair, and release-time seed logic.
- `fast_feasibility.py` provides a local candidate oracle with official
  verification sampling.
- `subsolvers/decomposition.py` has an early-exit protection removal policy.
- `subsolvers/edd_feedback.py` can produce conflict signals from EDD-like
  scheduling feedback.
- `subsolvers/hierarchical.py` has bay-assignment MIP style seed logic and
  conflict penalty hooks.
- `subsolvers/scheduling_mip.py` provides a fixed-placement scheduling MIP
  experiment path.

The code also has one obvious gap:

- `repair.py` is still a placeholder. Dedicated repair behavior lives inside
  `alns/engine.py` and subsolver modules instead of behind a small reusable
  repair interface.

That is not automatically wrong, but it makes experiments harder to isolate.

## Improvement Priorities

### P1. Add Better Telemetry Before More Heuristics

Expected value: high

Risk: low

Why this matters:

The latest experiment notes show strong but unstable gains, especially on
`prob_20`. Some changes produce excellent single runs but degrade repeatability
or consume too much final-polish time. The related research points to adaptive
large-neighborhood search, but adaptation is only useful when the solver can
measure which operator actually created improvements.

Current evidence:

- `alns/engine.py` already supports `OGC_ALNS_TRACE`.
- `fast_feasibility.py` already exposes oracle summary counters.
- `scripts/run_solver.py` reports objective, `obj1`, `obj2`, `obj3`, and
  `obj1_share`.
- `scripts/batch_eval.py` currently reports only objective and feasibility,
  not objective components.

Recommended work:

1. Extend local experiment output, not submitted solver behavior.
2. Track per-run:
   - seed objective and components,
   - best objective timeline,
   - operator attempts,
   - operator accepted moves,
   - operator best updates,
   - final polish attempts and wins,
   - oracle rejects by category.
3. Add a compact CSV/JSONL experiment summary under `.codex_workspace/`.

Why this should come first:

Without telemetry, the next heuristic can look good on one run while actually
reducing useful search time or overfitting to one instance. The docs already
contain several examples of this pattern.

### P2. Diagnose Tardiness Cause: Own Slack vs Exit-Path Blocker

Expected value: high

Risk: low to medium

Why this matters:

The research survey and existing experiments both identify the same mechanism:
many late exits are caused by other active blocks blocking the exit path. But
the solver should measure this directly per instance.

Current evidence:

- `subsolvers/decomposition.py` has `_geometry_exit_blockers()`.
- `alns/engine.py` has actual exit-blocker chain and due-window repair logic.
- `docs/obj1_reduction_experiments.md` shows the best large-instance result
  came from hybrid shadow-aware fast-release placement.

Recommended work:

1. For each tardy block in a final solution, compute:
   - tardiness amount,
   - earliest possible exit ignoring access blockers,
   - detected blockers at desired exit,
   - whether the blocker has a later due date,
   - same-bay active density near the exit.
2. Aggregate:
   - share of tardiness with direct blockers,
   - top blocker pairs,
   - top blocker bays,
   - repeated blocker motifs across runs.

Resulting decision:

- If most residual `Z1` is blocker-caused, invest in blocker-chain repair.
- If most residual `Z1` is pure capacity/timing pressure, invest in schedule
  compression or fixed-placement scheduling MIP.

### P3. Make Access-Risk Scoring a First-Class Local Feature

Expected value: high

Risk: medium

Why this matters:

The strongest reported improvement is "shadow-aware hybrid fast-release".
That suggests the access-risk signal is useful, but it currently appears as
specialized logic inside seed/polish paths rather than a shared scoring concept.

Current evidence:

- `alns/engine.py` has `_exit_blocking_penalty_units()` and
  `_blocks_earlier_due_exits()`.
- `docs/obj1_reduction_experiments.md` reports `prob_20` 600s reaching
  `obj1 = 4` with hybrid shadow fast release.
- `docs/decomposition_alns_experiments.md` says placement-level early-exit
  protection improved hard cases.

Recommended work:

1. Define a shared "access risk" helper:
   - only penalize blocking earlier-due exits,
   - use cheap geometry first,
   - keep it soft except where official feasibility would fail.
2. Use it in:
   - release-time seed placement,
   - `_ranked_insertions()`,
   - same-bay relayout,
   - preference recovery candidate scoring.
3. Keep objective-safe acceptance unchanged.

Important caution:

Do not turn access-risk into a heavy exact check everywhere. Existing notes show
that full scoring and expanded candidate scans can worsen time-limited runs.
The first version should be cheap and gated by instance size/timelimit.

### P4. Extract Small Reusable Repair Kernels

Expected value: medium to high

Risk: medium

Why this matters:

Research on relocation and min-conflicts suggests complete-solution repair is
effective when conflicts are local. The current code has many repair-like paths,
but they are embedded in `alns/engine.py`. `repair.py` is empty, so it cannot
yet serve as a clean place to test local repair policies.

Current evidence:

- `repair.py` returns the solution unchanged.
- `alns/engine.py` contains many narrow repair/polish functions:
  `_due_window_chain_repair`, `_same_bay_cluster_relayout`,
  `_exit_path_blocker_cluster_relayout`, `_exact_tardy_reinsert_step`,
  `_objective_safe_left_shift_polish`, and others.

Recommended work:

1. Do not move everything at once.
2. Start with one reusable kernel:
   - input: incumbent assignments, removed block ids, repair order, deadline,
     scoring mode,
   - output: candidate assignments or `None`.
3. Use it from one existing polish path first.
4. Verify no behavior change before adding new repair modes.

Potential benefit:

- Easier A/B experiments.
- Less duplication around candidate scoring and official validation.
- Cleaner path for min-conflicts style repair.

### P5. Improve Small-Instance Search Separately From Large-Instance Search

Expected value: medium

Risk: low to medium

Why this matters:

The solver has several policies gated by block count and timelimit. Existing
notes show that early-exit destroy can help larger instances but hurt small
ones, and long-budget release-time logic is tuned heavily around `prob_20`.

Current evidence:

- `solver.py` enables fast release seed only for `len(blocks) >= 150` and
  `timelimit >= 240`.
- `subsolvers/decomposition.py` notes early-exit destroy is large-instance
  oriented.
- `prob_1`, `prob_25`, `prob_4`, `prob_2`, and `prob_3` are the smallest
  100-block starting set.

Recommended work:

1. Treat small instances as a separate regime:
   - more official validation,
   - deeper local relayout,
   - fewer broad destroy moves,
   - stronger preference recovery when `Z1` is stable.
2. Treat large instances as:
   - seed quality + speed,
   - cheap oracle,
   - blocker-aware placement,
   - limited final polish.

Initial smoke set:

```text
small-2bay: prob_1, prob_25, prob_4, prob_22, prob_23
small-3bay: prob_2, prob_3, prob_21, prob_24
medium:     prob_27, prob_30, prob_6
large:      prob_20
```

### P6. Make Preference Recovery Conflict-Memory Driven

Expected value: medium

Risk: medium

Why this matters:

The literature and current docs agree that `Z3` recovery is dangerous because
preferred-bay moves can recreate exit-path conflicts. The current subsolver
README already recommends local bay reassignment MIP with conflict penalties.

Current evidence:

- `subsolvers/hierarchical.py` has `_soft_conflict_penalties()`.
- `subsolvers/edd_feedback.py` can convert conflicts to penalties.
- `subsolvers/README.md` explicitly says bay-assignment MIP should be a
  candidate generator, not the final answer.

Recommended work:

1. Store failed preference-recovery attempts as conflict memory:
   - moved block,
   - victim block,
   - bay,
   - event type: entry, exit, collision,
   - severity from objective/tardiness damage.
2. Feed that memory into:
   - local bay reassignment MIP,
   - preference relocation ordering,
   - candidate rejection/tie-breaks.
3. Apply only when current `obj1` is low enough.

Acceptance rule:

```text
candidate feasible
candidate objective < incumbent objective
candidate obj1 <= incumbent obj1
```

For preference-only polish, this stricter `obj1` guard is still justified.

### P7. Strengthen Batch Evaluation Output

Expected value: medium

Risk: low

Why this matters:

The project already records objective components manually in docs, but the
batch script only emits total objective. For performance work, objective
components must be visible every time.

Current evidence:

- `scripts/run_solver.py` reports `obj1`, `obj2`, `obj3`, and `obj1_share`.
- `scripts/batch_eval.py` reports only `objective`.

Recommended work:

Add to `batch_eval.py`:

- `obj1`
- `obj2`
- `obj3`
- `obj1_share`
- maybe `weights`

This is local tooling only and does not affect submitted solver behavior.

## Suggested Experiment Sequence

The next development should avoid jumping directly to a large heuristic change.
Use this sequence:

### Step 1. Measurement Upgrade

Implement local reporting improvements:

- batch objective components,
- optional JSONL trace summary,
- per-operator acceptance/best-update counts.

Pass condition:

- No submitted solver behavior changes.
- Existing runs still produce feasible output.

Current progress:

- `scripts/batch_eval.py` now reports `obj1`, `obj2`, `obj3`,
  `obj1_share`, conservative lower bounds, absolute gaps, and relative gaps.
- `--instances` allows focused visible-set matrices.
- `--bounds-only` computes lower bounds without importing the official checker.
- `--output` writes `.jsonl` or `.csv` result files.
- `scripts/compare_batch_results.py` compares two `batch_eval.py` JSONL files
  by instance and reports objective/`obj1` improvements plus aggregate deltas.

Lower-bound components currently implemented:

- `obj1` release/processing lower bound:
  `max(0, release_time + processing_time - due_date)`.
- `obj1` continuous area-time relaxation over compatible bay subsets.
- `obj1` energetic/compulsory-processing area-time relaxation over compatible
  bay subsets.
- two-bay exact assignment relaxation for weighted `Z2 + Z3`.
- fallback independent best-bay `Z3` lower bound for 3+ bay instances.

The two-bay assignment relaxation is useful for `prob_1`, `prob_25`, and
`prob_4`, but the current visible small-instance 5s gap is still dominated by
`Z1`, not secondary terms.

### Step 2. Baseline Matrix

Run a small matrix:

```text
instances: prob_1, prob_25, prob_4, prob_2, prob_3, prob_27, prob_30, prob_6, prob_20
timelimits: 5s, 20s, 60s
```

For long-run logic, add:

```text
prob_20 at 120s, 300s, 600s
```

Record:

- objective,
- `obj1`, `obj2`, `obj3`,
- `obj1_share`,
- elapsed,
- feasibility,
- trace summary if enabled.

First 5s small-instance baseline with the new gap tracker:

| Instance | Feasible | Objective | obj1 | Lower bound | LB method |
| --- | --- | ---: | ---: | ---: | --- |
| prob_1 | yes | 443,789,555 | 15,236 | 1,499 | two-bay assignment DP |
| prob_25 | yes | 31,987,243 | 47,875 | 5,547 | two-bay assignment DP |
| prob_4 | yes | 336,714,607 | 15,339 | 15,946 | two-bay assignment DP |
| prob_2 | yes | 272,387,403 | 9,343 | 0 | independent preference |
| prob_3 | yes | 262,828,728 | 9,834 | 0 | independent preference |

The lower bounds are intentionally optimistic. The important signal is that
the secondary-term lower bounds are tiny compared with the observed objective.
On this short small-instance baseline, reducing tardiness is still the main
gap-closing route.

Follow-up upper-bound probes:

| Instance | Timelimit | Objective | obj1 | Lower bound | Interpretation |
| --- | ---: | ---: | ---: | ---: | --- |
| prob_1 | 20s | 30,747,934 | 1,043 | 1,499 | 5s solution is very weak; 20s closes a large visible gap. |
| prob_25 | 20s | 31,987,243 | 47,875 | 5,547 | No improvement over 5s; current search stalls early. |
| prob_25 | 60s | 2,077,836 | 3,054 | 5,547 | Large improvement appears only with longer search. |

The current lower bound is useful for secondary terms but still cannot prove a
positive `Z1` lower bound on these examples. Therefore, if the goal is to judge
how close an example solution is to optimum, the next lower-bound work should
target `Z1` more directly:

- pairwise or clique incompatibility under on-time windows,
- zero-tardiness feasibility checks on simplified placement models,
- bay-specific interval graph relaxations using mandatory overlap windows,
- exact/CP feasibility probes for selected small instances outside the
  submitted solver.

CP-SAT relaxed scheduling probe:

- `scripts/lower_bound_cp_sat.py` was added as an analysis-only tool.
- It replaces irregular geometry and crane paths with bay-level cumulative
  footprint-area resources, then minimizes total tardiness.
- This is a valid relaxation, so the solver objective bound is a conservative
  `obj1` lower bound for the true OGC problem.
- OR-Tools 9.15.6755 was installed in `.codex_workspace/ogc2026_win_env` for
  this analysis, matching the environment file.

Initial relaxed CP-SAT results:

| Instance | Time limit | Status | CP `obj1` bound | CP incumbent | Interpretation |
| --- | ---: | --- | ---: | ---: | --- |
| prob_1 | 10s | OPTIMAL | 0 | 0 | Cumulative area relaxation allows zero tardiness. |
| prob_25 | 10s | OPTIMAL | 0 | 0 | Cumulative area relaxation allows zero tardiness. |
| prob_20 | 10s | OPTIMAL | 0 | 0 | Cumulative area relaxation allows zero tardiness. |

This is useful negative evidence. The large observed `Z1` values are not forced
by release/due windows, processing durations, bay assignment, or relaxed area
capacity alone. The missing difficulty is actual 2D placement plus vertical
entry/exit path geometry. Stronger `Z1` lower bounds must therefore encode at
least some geometry/path incompatibility, not just cumulative area.

Geometry-aware pairwise CP-SAT lower-bound probe:

- `scripts/lower_bound_geometry_cp_sat.py` was added as an analysis-only tool.
- It starts from the cumulative area CP-SAT relaxation.
- For each same-bay block pair, it tries to prove that the two blocks cannot
  coexist in any integer orientation/position pair while allowing either
  entry/exit order.
- Only proven impossible pairs become no-overlap constraints. Pairs whose
  exhaustive check would exceed `--max-pair-checks` are left relaxed, so the
  bound remains conservative.

Initial results:

| Instance | Time limit | Max pair checks | Proven incompatible bay-pairs | Unknown pair tests | Geometry CP `obj1` bound |
| --- | ---: | ---: | ---: | ---: | ---: |
| prob_1 | 10s | 1,000,000 | 0 | 3,358 | 0 |
| prob_25 | 10s | 1,000,000 | 0 | 7,945 | 0 |

This is another useful negative result. Simple pairwise coexistence
incompatibility is not enough to explain the visible tardiness gap. The hard
part appears to be multi-block path congestion and placement choices, not
isolated two-block impossibility. The next stronger lower-bound direction
should therefore target:

- multi-block corridor/path capacity,
- compulsory occupancy near due windows,
- fixed-bay or fixed-layout infeasibility certificates,
- or CP/MIP probes that preserve more of the 2D path structure for selected
  visible examples.

Secondary assignment CP-SAT lower-bound probe:

- `scripts/lower_bound_secondary_cp_sat.py` was added as an analysis-only tool.
- It solves the relaxed bay-assignment problem for `obj2+obj3`: each block is
  assigned to a geometrically compatible bay, while placement, timing, and
  access constraints are ignored.
- The official normalized load imbalance formula is modeled with exact rational
  scaling and the same floor semantics, so the weighted secondary value is a
  valid lower bound.

Initial results:

| Instance | Bays | Blocks | Status | Base secondary LB | CP secondary LB | Current objective | Relative gap using this LB |
| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: |
| prob_2 | 3 | 100 | OPTIMAL | 0 | 3,690 | 1,371,109 | 0.997 |
| prob_26 | 3 | 150 | OPTIMAL | 0 | 74,030 | 37,848,869 | 0.998 |
| prob_28 | 3 | 150 | OPTIMAL | 0 | 17,238 | 13,372,274 | 0.999 |
| prob_29 | 3 | 150 | OPTIMAL | 0 | 34,782 | 3,644,691 | 0.990 |

Interpretation:

- This closes a clear reporting gap for three-bay instances: secondary lower
  bounds no longer need to be zero.
- However, the impact on total relative gap is tiny because visible objectives
  remain dominated by `w1 * obj1`.
- The updated goal of reaching a gap near 0.2 therefore cannot be met by
  secondary assignment bounds alone. Stronger `obj1` lower bounds must capture
  multi-block spatial/path congestion or fixed-window infeasibility.

Projection-resource energetic lower-bound probe:

- `scripts/lower_bound_projection_cp.py` was added as another analysis-only
  `obj1` lower-bound attempt.
- It strengthens the pure area relaxation with simple 2D packing necessary
  conditions:
  - blocks taller than half of a bay consume x-width projection capacity,
  - blocks wider than half of a bay consume y-height projection capacity.
- This remains conservative because it only uses necessary packing conditions
  over compulsory on-time processing intervals.

Initial results:

| Instance | Bays | Blocks | Base obj1 LB | Projection x LB | Projection y LB | Combined obj1 LB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| prob_2 | 3 | 100 | 0 | 0 | 0 | 0 |
| prob_26 | 3 | 150 | 0 | 0 | 0 | 0 |
| prob_28 | 3 | 150 | 0 | 0 | 0 | 0 |
| prob_29 | 3 | 150 | 0 | 0 | 0 | 0 |

Interpretation:

- This is a useful negative result. Even projection-strengthened cumulative
  resources do not explain the observed `obj1` on the tested three-bay
  instances.
- The missing lower-bound structure is not just aggregate area, width, or
  height capacity. It is likely path/order congestion: a block can fit and the
  bay can have enough aggregate capacity, yet the entry/exit path sequence can
  force delays.
- To approach a relative gap near 0.2, the next `obj1` lower-bound attempt
  should model fixed-window entry/exit path infeasibility or multi-block
  ordering conflicts, not another aggregate resource relaxation.

Fixed-placement schedule probe:

- `scripts/fixed_placement_schedule_probe.py` was added as an analysis-only
  tool.
- It runs the submitted solver or reads a saved solution, then keeps
  `bay_id`, `x`, `y`, and `orient_idx` fixed.
- It builds a CP-SAT scheduling model with pairwise separation constraints for
  same-bay placements that collide or obstruct entry/exit paths.
- The resulting candidate is checked with the official feasibility checker.
- The same idea is now also available as an optional submitted-solver polish
  under `ogc_solver/ogc_solver/subsolvers/scheduling_cp.py`. It is gated to
  small long-budget instances and only runs when OR-Tools is importable. Lower
  bound search remains analysis-only and is not part of the submitted solver.

First fixed-placement probe results on 5s solutions:

| Instance | Original obj1 | Best fixed-placement obj1 found | CP bound for fixed placement | Official feasible |
| --- | ---: | ---: | ---: | --- |
| prob_1 | 15,236 | 12,671 | 3,783 | yes |
| prob_25 | 47,875 | 41,382 | 17,456 | yes |

Interpretation:

- The 5s solutions have schedule-side slack: even without moving any block,
  CP-SAT can improve the schedule.
- But schedule-only improvement is far smaller than the 60s full-solver gain on
  `prob_25` (`obj1 = 3,054`), so placement/access decisions remain the larger
  bottleneck.
- The CP bound inside the fixed placement is still much lower than the feasible
  schedule found in 20s, so if we want to exploit a fixed layout, schedule
  compression or CP-guided schedule repair may still help.

Submitted-solver CP polish smoke result:

| Instance | Bays | Timelimit | CP off | CP on | obj1 with CP on | Decision |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| prob_1 | 2 | 80s | 29,535,208 | 19,295,176 | 649 | keep enabled |
| prob_4 | 2 | 80s | 9,936,476 | 5,492,621 | 230 | keep enabled |
| prob_22 | 2 | 80s | 10,658,800 | 7,134,429 | 455 | keep enabled |
| prob_23 | 2 | 80s | 33,474,229 | 20,538,670 | 1,483 | keep enabled |
| prob_25 | 2 | 80s | 1,621,186 | 1,295,733 | 1,879 | keep enabled |
| prob_2 | 3 | 80s | 1,371,109 | 1,466,972 | 42 | disable for 3+ bays |

This improvement is narrower than the standalone 60s solver + 20s
fixed-placement probe result, but it confirms that reserving final time for
CP-SAT schedule repair improved every visible 100-block two-bay long-budget
run checked here without changing placement. The `prob_2` result shows the
same reserve can hurt three-bay search, so the submitted gate is deliberately
restricted to two-bay instances.

The same forced-polish A/B was then run on every visible 150-block two-bay
instance at 120s:

| Instance | Bays | Blocks | Timelimit | CP off | CP forced | obj1 with CP forced | Decision |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| prob_8 | 2 | 150 | 120s | 1,054,392 | 994,392 | 18 | enable by default |
| prob_27 | 2 | 150 | 120s | 108,775,903 | 89,985,809 | 6,621 | enable by default |
| prob_30 | 2 | 150 | 120s | 40,212,419 | 30,632,103 | 2,239 | enable by default |

Because the 150-block two-bay subgroup also improved 3/3, the submitted gate
was extended from `blocks <= 120` to `blocks <= 150` while keeping the two-bay
restriction. Larger two-bay runs can still be tested with
`OGC_FORCE_CP_SCHEDULE_POLISH`, but they are not enabled by default.

Current 150-block two-bay 120s batch baseline after gate extension:

| Group | Instances | Feasible | Sum objective | Sum obj1 | Sum lower bound |
| --- | ---: | ---: | ---: | ---: | ---: |
| 150-block 2-bay | 3 | 3 | 121,252,313 | 8,851 | 553,716 |

Per-instance snapshot:

| Instance | Objective | obj1 | Lower bound |
| --- | ---: | ---: | ---: |
| prob_8 | 994,392 | 18 | 11,252 |
| prob_27 | 90,545,795 | 6,663 | 526,212 |
| prob_30 | 29,712,126 | 2,170 | 16,252 |

The extended gate also has initial support at the shorter 80s budget:

| Instance | Bays | Blocks | Timelimit | CP off | CP on | obj1 with CP on | Decision |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| prob_8 | 2 | 150 | 80s | 6,569,012 | 5,479,012 | 470 | keep enabled |
| prob_27 | 2 | 150 | 80s | 329,914,734 | 243,996,882 | 18,164 | keep enabled |

These checks reduce the risk that the 150-block extension only works because
the 120s budget leaves abundant final-polish time.

Small three-bay opportunistic CP schedule polish:

- The reserved CP schedule polish is still disabled for three-bay instances,
  because reserving time at 80s hurt `prob_2`.
- A separate opportunistic hook now exists for `blocks <= 100`, three-bay,
  `timelimit >= 120s` runs.
- It does not reserve time up front. It runs only if the normal search leaves
  enough final-polish time, and the candidate is accepted only if the official
  objective improves.
- `OGC_DISABLE_CP_SCHEDULE_POLISH` disables this hook too, which makes A/B
  experiments unambiguous.

130s A/B results:

| Instance | Bays | Blocks | CP disabled | CP on | obj1 with CP on | Decision |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| prob_2 | 3 | 100 | 9,227,269 | 2,760,967 | 87 | keep opportunistic |
| prob_3 | 3 | 100 | 1,590,569 | 892,417 | 21 | keep opportunistic |
| prob_21 | 3 | 100 | 9,554,438 | 8,447,799 | 593 | keep opportunistic |
| prob_24 | 3 | 100 | 7,988,737 | 5,152,622 | 339 | keep opportunistic |

The 100-block three-bay visible subgroup improved 4/4 at 130s. This still does
not justify a reserved three-bay CP budget, because the 80s reserve experiment
hurt `prob_2`. It supports only using otherwise-unused tail time for an
objective-safe fixed-placement schedule candidate.

Aggregated with `scripts/compare_batch_results.py`, the 130s opportunistic CP
gate improved this subgroup by:

- sum objective: 11,107,208 lower.
- sum `obj1`: 546 lower.
- sum objective improvement: 39.16%.
- sum `obj1` improvement: 34.43%.

Current small-visible 80s batch baseline after CP schedule-polish gating:

| Group | Instances | Feasible | Sum objective | Sum obj1 | Sum lower bound |
| --- | ---: | ---: | ---: | ---: | ---: |
| 100-block 2-bay | 5 | 5 | 54,144,634 | 4,653 | 102,927 |
| 100-block 3-bay | 4 | 4 | 20,774,699 | 1,329 | 26,850 |
| Total | 9 | 9 | 74,919,333 | 5,982 | 129,777 |

The 2-bay group uses the reserved CP schedule polish. The 3-bay group is at
80s, below the `timelimit >= 120s` opportunistic CP gate, and therefore checks
that the short-budget three-bay path remains unchanged.

150-block three-bay current baseline and fixed-placement probe:

The remaining visible 150-block three-bay cases were first evaluated with the
current submitted solver path at 130s:

| Instance | Bays | Blocks | Timelimit | Objective | obj1 | obj2 | obj3 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| prob_26 | 3 | 150 | 130s | 40,054,164 | 2,933 | 1,675 | 6,245 |
| prob_28 | 3 | 150 | 130s | 13,372,274 | 905 | 1,003 | 4,343 |
| prob_29 | 3 | 150 | 130s | 3,644,691 | 144 | 1,913 | 5,730 |

Then all visible 150-block three-bay cases were probed with 130s solver time
plus 20s fixed-placement CP schedule time. The `Original` columns are from the
probe's own solver run, so they should not be treated as identical to the
separate `batch_eval.py` baseline above.

| Instance | Original objective | CP candidate objective | Original obj1 | CP candidate obj1 | CP status | Official feasible | Interpretation |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- |
| prob_5 | 3,340,107 | 1,580,107 | 156 | 46 | OPTIMAL | yes | schedule-only CP helps |
| prob_6 | 1,061,441 | 3,757,771 | 6 | 97 | OPTIMAL | yes | schedule-only CP hurts |
| prob_7 | 864,999 | 1,540,563 | 3 | 41 | OPTIMAL | yes | schedule-only CP hurts |
| prob_26 | 37,848,869 | 30,475,720 | 2,766 | 2,213 | FEASIBLE | yes | schedule-only CP helps but needs time |
| prob_28 | 25,855,726 | 21,922,491 | 1,837 | 1,542 | FEASIBLE | yes | schedule-only CP helps but needs time |
| prob_29 | 3,805,903 | 4,485,886 | 154 | 205 | OPTIMAL | yes | schedule-only CP hurts |

Across these six probe runs, fixed-placement CP improved three instances and
worsened three. The total probe objective still improved because `prob_26` and
`prob_28` are large, but both of those improvements consumed the full 20s CP
limit and returned only `FEASIBLE`. That makes the submitted-gate decision
different from the analysis result: a late CP candidate is objective-safe if it
finishes, but attempting it can steal time from final polish or risk deadline
pressure.

The submitted opportunistic three-bay gate therefore remains capped at
`blocks <= 100`. For 150-block three-bay instances, placement/local polish and
better access-risk decisions should remain the priority until a cheap trigger
can predict when fixed-schedule CP is likely to help and when enough tail time
is truly available.

An attempted late-tail implementation of a high-`obj1` trigger did not have
enough reliable time at 130s: after normal final polish, `prob_5` reached the
final check with too little tail time to run CP safely. Lowering the time guard
would risk missing the deadline, while reserving time would recreate the earlier
three-bay reserve problem. Therefore the submitted gate remains unchanged.

### Step 3. Tardiness-Cause Audit

Add diagnostic-only code to analyze final solutions:

- direct exit blockers,
- later-due blockers,
- repeated blocker pairs,
- tardiness not explained by direct blockers.

Pass condition:

- The audit can classify most tardiness on small and medium instances.
- It identifies whether the next code change should target placement or timing.

Current progress:

- `scripts/diagnose_tardiness.py` runs a solver or reads a saved solution, then
  checks tardy blocks against their ideal due/release-based exit time.
- It reports direct blockers at the current desired exit, ideal-exit blockers,
  entry-delay share, top blocker blocks, and top victim/blocker pairs.

First diagnostic signal:

| Instance | Timelimit | obj1 | Tardy blocks | Ideal-blocked tardiness share | Entry-delay tardiness share |
| --- | ---: | ---: | ---: | ---: | ---: |
| prob_1 | 5s | 15,236 | 98 | 86.4% | 100.0% |
| prob_25 | 5s | 47,875 | 98 | 92.1% | 100.0% |
| prob_26 | 130s | 2,766 | 90 | 74.9% | 100.0% |
| prob_28 | 130s | 905 | 76 | 33.8% | 100.0% |
| prob_29 | 130s | 144 | 32 | 22.9% | 100.0% |

Interpretation:

- In the current short-budget small-instance solutions, blocks are not late
  because their current exit is directly blocked; they are late because entry
  itself happens very late.
- The same pattern still appears on a 150-block three-bay long-budget solution:
  `prob_26` has no direct exit blockers at the delayed exit time, but 54 tardy
  blocks and 74.9% of tardiness are associated with blockers at their ideal
  due/release-based exit time.
- `prob_29`, the case where blind tail reserve badly hurt, has a different
  signature: low `obj1`, only 2 ideal-blocked tardy blocks, and 22.9%
  ideal-blocked tardiness share.
- `prob_28` is intermediate: high enough `obj1` and many tardy blocks, but only
  33.8% ideal-blocked tardiness share. A saved-solution 30s
  `_objective_safe_obj1_polish` probe improved it modestly from objective
  13,372,274 / `obj1` 905 to objective 12,972,284 / `obj1` 875.
- However, if each tardy block is tested at its ideal due/release-based exit
  time using the current placement, most tardiness is associated with an
  exit-path obstruction.
- That points to earlier placement/entry decisions causing future exit-path
  conflicts. The next solver changes should therefore target access-risk during
  seed construction and insertion, not only final left-shift.

Trigger-summary tool:

- `scripts/summarize_tardiness_diagnostics.py` was added to classify diagnostic
  JSON files using visible, analysis-only thresholds.
- With defaults `obj1 >= 500`, ideal-blocked tardiness share `>= 0.60`, and at
  least 20 ideal-blocked tardy blocks, it flags `prob_26` as a tail obj1 repair
  candidate and rejects `prob_28` and `prob_29`.
- The same tool also reports a cheap proxy based only on `obj1 >= 500` and at
  least 50 tardy blocks. That proxy flags `prob_26` and `prob_28`, while still
  rejecting `prob_29`.
- This is not submitted as runtime logic yet. Computing the diagnostic requires
  many geometry/path checks, and the correct place to spend that time is still
  unproven. For now it is evidence for designing a cheaper incumbent trigger.

### Step 4. Access-Risk Scoring Experiment

Promote a cheap access-risk helper into shared use in one path first:

1. release-time seed placement, or
2. `_ranked_insertions()`.

Pass condition:

- Improves or does not degrade the small/medium matrix.
- Does not slow large short-budget runs enough to erase seed quality.

First attempted shared-use experiment:

- Change tested: enable the existing `_exit_blocking_penalty_units()` score
  during main-loop repair after access-blocker removal. The change was gated to
  `total_budget >= 60s` and used multi-slot repair only at `total_budget >= 120s`.
- Result: rejected and reverted.

| Instance | Timelimit | Baseline objective | Tested objective | Baseline obj1 | Tested obj1 | Decision |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| prob_1 | 80s | 19,498,813 | 19,556,995 | 656 | 658 | worse |
| prob_2 | 80s | 1,371,109 | 1,371,109 | 39 | 39 | neutral |
| prob_26 | 130s | 40,054,164 | 41,707,456 | 2,933 | 3,057 | worse |

Interpretation:

- Pairwise exit-blocking penalty is useful in objective-safe late polish, but
  too blunt as a general repair ranking signal.
- It likely spends extra time on local path checks and over-penalizes feasible
  placements that would later be repaired by compression or CP schedule polish.
- The next access-risk attempt should be cheaper and more selective: e.g.,
  use it only as a tie-break among near-equal insertions, only for high-`obj1`
  victims, or only inside a bounded local repair kernel after diagnostics name
  a specific blocker chain.

Tail obj1 polish reserve experiment:

- `scripts/due_window_chain_probe.py` was added as an analysis-only probe for
  the internal due-window chain repair on saved incumbents.
- On saved `prob_26` 130s incumbent, direct `_due_window_chain_repair` with 20s
  found no candidate.
- On the same saved incumbent, `_objective_safe_obj1_polish` with 30s improved
  objective from 37,848,869 to 36,422,238 and `obj1` from 2,766 to 2,659.
- This shows the existing local obj1 polish can help if enough tail time exists,
  but it does not prove that reserving that time during the full run is good.

Full-run reserve A/B:

| Instance | Timelimit | Baseline objective | Reserve objective | Baseline obj1 | Reserve obj1 | Decision |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| prob_26 | 130s | 40,054,164 | 40,054,164 | 2,933 | 2,933 | neutral |
| prob_29 | 130s | 3,644,691 | 9,172,406 | 144 | 554 | reject |

Interpretation:

- Reserving 18s for 150-block three-bay tail obj1 polish did run as intended.
  Trace on `prob_26` showed search ending at objective 41,707,456 / `obj1`
  3,057, then tail polish recovering to 40,054,164 / `obj1` 2,933.
- That only matched the no-reserve baseline, meaning the reserved time mostly
  replaced useful search time.
- On `prob_29`, the shorter search hurt badly. The reserve policy was therefore
  reverted.
- The next viable path is not a blind reserve. It needs a cheap trigger from
  the incumbent state, such as high `obj1` plus strong ideal-exit blocker share,
  and it must use otherwise-idle tail time or a very small bounded repair.

### Step 5. Local Repair Kernel

Only after diagnostics show repeated local blocker motifs, extract one repair
kernel and connect it to one existing final-polish path.

Pass condition:

- Same or better objective on smoke set.
- Less duplicated repair logic.
- No new dependency outside `ogc_solver/`.

## What Not To Prioritize Yet

### Full Monolithic MIP

The literature and current instance sizes argue against a full bay/placement/
time MIP. Use MIP locally only.

### Global Z3 Reassignment

Global preference recovery can destroy low-`Z1` structure. Keep it local,
conflict-memory-aware, and objective-safe.

### Heavy Exact Geometry Scoring Everywhere

Several existing experiments worsened because extra exact checks consumed
useful search time. Exact geometry should remain targeted and cached.

### Large Refactor of `alns/engine.py` Before Measurement

The engine is large, but it contains many verified behaviors. Refactoring first
would make it harder to distinguish algorithm improvement from accidental
behavior drift.

## Bottom Line

The best near-term improvement path is:

```text
measure better
diagnose tardiness causes
make access-risk reusable
repair small blocker chains
only then polish preference/load balance
```

This path follows the external research pattern and the repo's own experiment
history. It also keeps the main invariant intact: never trade away feasibility
or `Z1` quality for a local-looking `Z2` or `Z3` gain.

## Lower-Bound Follow-Up: Pairwise Access Relations

The user clarified that heavy lower-bound work is acceptable for judging visible
examples, but heavy lower-bound logic does not need to go into the submitted
runtime algorithm.

Two analysis-only geometry lower-bound tools were tightened:

- `scripts/lower_bound_geometry_cp_sat.py` now treats co-presence as feasible
  if any FIFO or nested entry/exit order is possible for a pair. The previous
  checker only accepted FIFO-like overlap, which was too pessimistic for a
  valid lower-bound relaxation.
- `scripts/lower_bound_pair_relation_cp_sat.py` was added. It builds a CP-SAT
  tardiness relaxation with cumulative bay area plus pairwise entry/exit order
  relation masks. Exact multi-block coordinate consistency is still relaxed, so
  the bound remains conservative.

Measured results:

| Instance | Pair tests | Unknown pair tests | Constrained pair relations | Obj1 LB |
| --- | ---: | ---: | ---: | ---: |
| prob_2 | 5,145 | 0 | 0 | 0 |
| prob_26 | 21,695 | 0 | 0 | 0 |

Interpretation:

- Pairwise access geometry is not enough to explain the current tardiness.
- For every tested on-time-overlapping pair in `prob_2` and `prob_26`, some
  integer placement pair supports every coarse entry/exit relation.
- The missing lower bound must involve at least one of:
  - global consistency of one block's coordinate choice across many neighbors,
  - three-or-more-block blocker structures,
  - a certificate tied to an incumbent placement, which is useful diagnostically
    but is not a global lower bound unless the coordinate/bay choices are also
    proven unavoidable.

Next lower-bound direction:

```text
pairwise relation LB is exhausted
move to multi-block coordinate consistency or incumbent-specific certificates
keep submitted solver free of heavy proof logic
```

## Lower-Bound Follow-Up: Triplet Packing Consistency

`scripts/lower_bound_triplet_packing_cp_sat.py` was added as the smallest
multi-block extension after pairwise checks failed.

Safe relaxation rule:

- If three blocks assigned to the same bay have no collision-free integer
  placement triple in that bay, then their intervals cannot share a common time
  point in that bay.
- Triplets that are too expensive to certify are ignored. This weakens the
  bound but keeps it valid.
- Entry/exit path ordering is deliberately ignored in this first triplet tool.
  A collision-free triple might still be inaccessible, but treating it as
  feasible is conservative for a lower bound.

Measured results:

| Instance | Candidate order | Checked triplets | Unknown | Certified incompatible | Obj1 LB |
| --- | --- | ---: | ---: | ---: | ---: |
| prob_2 | product-asc | 200 | 0 | 0 | 0 |
| prob_25 | product-asc | 500 | 8 | 0 | 0 |
| prob_26 | product-asc | 500 | 0 | 0 | 0 |
| prob_25 | area-desc | 200 | 1 | 0 | 0 |
| prob_26 | area-desc | 200 | 0 | 0 | 0 |

Interpretation:

- Even the smallest multi-block packing check did not produce a useful obj1
  lower bound on these representatives.
- The visible tardiness is probably not caused by static inability for small
  groups of blocks to coexist. It is more likely caused by time-dependent
  accessibility, chained relocations, and the need for one block's coordinate
  choice to satisfy many future exits simultaneously.
- The next valid global lower-bound attempt should model either:
  - incumbent-independent access-path certificates over blocker chains, or
  - a stronger exact/CP relaxation for a selected bay/time window with shared
    placement variables.
- The next practical solver-improvement attempt should remain diagnostic-led:
  use current-solution blocker chains to target local repair, but do not count
  those incumbent-specific diagnostics as global lower bounds.

## Lower-Bound Follow-Up: Shared Placement Window CP

`scripts/lower_bound_window_placement_cp_sat.py` was added to test a stronger
selected-window relaxation.

Safe relaxation rule:

- Select a small subset of blocks.
- Enumerate every integer placement option for each selected block across all
  compatible bays.
- Choose exactly one placement per selected block.
- If two chosen placements collide, force the two time intervals not to
  overlap.
- Ignore all unselected blocks and all crane access-path constraints.

This is still a valid lower bound for selected-block tardiness because it is
more permissive than the real problem. It is stronger than pair/triplet tools
because one block's placement choice is shared consistently across all pair
conflicts.

First result:

| Instance | Selection | Blocks | Options | Candidate option pairs | Exact conflict pairs | CP status | Obj1 LB |
| --- | --- | ---: | ---: | ---: | ---: | --- | ---: |
| prob_26 | few-options | 3 | 7,716 | 10,877,872 | 3,037,682 | UNKNOWN | 0 |
| prob_26 | few-options + signature compression | 3 | 7,360 | 10,877,872 | 3,001,680 | UNKNOWN | 0 |

Interpretation:

- The exact shared-placement relaxation is much stronger structurally, but even
  a 3-block subset can create millions of binary conflict implications.
- Conflict enumeration is now complete for this case after caching option
  geometry, but the resulting CP-SAT model is too large for a short solve.
- Simple identical-conflict-signature compression barely helped on the first
  hard subset: options shrank by only 4.6% and conflict pairs by only 1.2%.
- The next lower-bound engineering step should compress the conflict graph
  before modeling, for example by grouping equivalent placement intervals,
  using maximal conflict cliques, or proving window infeasibility through a
  SAT-style placement feasibility check at selected time slices instead of
  full interval scheduling.

Solver implication:

- Do not enable heavier 150-block three-bay CP schedule polish by default yet.
  The existing tail window is too short for the fixed-placement CP to find
  reliable improvements on large three-bay instances, while previous blind
  reserve experiments degraded `prob_29`.
- A safer future solver change needs a cheap incumbent trigger and should use
  otherwise-idle tail time only, or be guarded by objective-safe acceptance
  after the main final-polish path.

Follow-up rejected trigger experiment:

- Tested change: allow opportunistic fixed-placement CP schedule polish for
  150-block three-bay instances only when incumbent `obj1 >= 500` and at least
  50 blocks are tardy. No extra reserve was added.
- `prob_29` 130s correctly skipped the trigger and preserved the known baseline
  `3,644,691 / obj1 144`.
- `prob_26` 130s triggered CP and improved the search incumbent
  `41,707,456 / obj1 3057` to `38,040,881 / obj1 2782`.
- However, the same run with CP disabled reached `37,848,869 / obj1 2766`
  through the existing final polish. The triggered CP therefore blocked a
  better incumbent path and was reverted.

Conclusion: do not insert large three-bay fixed-placement CP before the current
final obj1 polish. If revisited, it should run after final obj1 polish and only
with objective-safe acceptance against that final incumbent, not against the
pre-polish search incumbent.

Post-final CP probe and rejected submission experiment:

- Analysis-only fixed-placement CP on saved final incumbents with 3 seconds:
  - `prob_26`: `37,848,869 / obj1 2766` -> `34,662,282 / obj1 2527`
  - `prob_28`: `13,372,274 / obj1 905` -> `11,118,997 / obj1 736`
  - `prob_29`: `3,644,691 / obj1 144` -> `4,444,671 / obj1 204` (worse)
- This suggested post-final CP could help high-obj1 cases if gated.
- Tested submitted-path change: reserve 5 seconds for 150-block three-bay
  instances, run post-final CP only if pre-polish incumbent has `obj1 >= 500`
  and at least 50 tardy blocks.
- `prob_26` improved to `35,269,754 / obj1 2573`, better than the current
  baseline.
- `prob_29` correctly skipped and stayed at `3,644,691 / obj1 144`.
- `prob_28` degraded badly to `19,946,864 / obj1 1388`, because the extra
  reserve changed the search/final-polish path before CP could help.

Decision: reverted. Saved-solution probes are not enough; reserving time changes
the incumbent distribution. A safe implementation would need an otherwise-idle
tail window or an adaptive trigger that distinguishes `prob_26`-like geometry
from `prob_28`-like geometry before reducing search time.

No-reserve post-final CP hook experiment:

- Tested change: after final feasibility check, run fixed-placement CP only if
  enough time naturally remains, with no search-time reserve and objective-safe
  acceptance.
- On 130s visible large three-bay runs, the hook did not fire because the final
  polish path already consumed the tail window.
- Since it produced no visible benefit and added submitted-code complexity, the
  hook was reverted.

Conclusion: post-final fixed-placement CP is still best kept as an analysis
probe until either an otherwise-idle tail window exists or a stronger trigger
can justify reserving time without hurting `prob_28`-like cases.

Cheap ideal-blocked trigger probe:

- `scripts/diagnose_tardiness.py` now also reports a Shapely-free
  `cheap_ideal_blocked_*` proxy. It tests the same ideal-exit idea as the exact
  diagnostic, but uses layer bounding boxes plus a convex-polygon
  separating-axis test instead of organizer `check_exit`.
- A whole-layer AABB proxy was tested first and rejected. It overestimated
  blocking badly enough to turn on `prob_28` and `prob_29`.
- The SAT proxy separates the known 150-block three-bay saved incumbents:

| Instance | Obj1 | Exact ideal share | SAT proxy share | Exact trigger | SAT proxy trigger | Old cheap trigger |
| --- | ---: | ---: | ---: | --- | --- | --- |
| prob_26 | 2,766 | 0.749 | 0.808 | true | true | true |
| prob_28 | 905 | 0.338 | 0.435 | false | false | true |
| prob_29 | 144 | 0.229 | 0.493 | false | false | false |

Interpretation:

- The SAT proxy is a viable candidate gate for future tail obj1 repair or
  post-final fixed-placement CP experiments because it keeps the `prob_26`
  signal while rejecting the `prob_28` case that made the previous reserve
  experiment unsafe.
- This is not yet submitted solver behavior. The next solver experiment should
  test whether adding this gate around any reserved tail work preserves the
  current `prob_28` search path and still improves `prob_26`.

## Long-Horizon Performance Checks

User guidance: periodically run representative instances with 3000 seconds to
check whether conclusions from short probes still hold with ample computation
time.

First 3000-second result on current `prob_26` exposed a large-regime failure:

| Instance | Solver state | Time | Objective | Obj1 | Obj2 | Obj3 | Notes |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| prob_26 | 130s saved incumbent | 130s | 37,848,869 | 2,766 | 2,513 | 6,348 | prior reference |
| prob_26 | pre-guard long path | 3000s | 59,486,860 | 4,456 | 10,716 | 0 | much worse |
| prob_26 | long-reference guard | 3000s | 37,069,433 | 2,706 | 1,955 | 6,511 | best of these |
| prob_28 | 130s saved incumbent | 130s | 13,372,274 | 905 | 1,003 | 4,343 | prior reference |
| prob_28 | long-reference guard | 3000s | 11,397,110 | 836 | 3,274 | 803 | improved |
| prob_29 | 130s saved incumbent | 130s | 3,644,691 | 144 | 1,913 | 5,730 | prior reference |
| prob_29 | long-reference guard | 3000s | 3,529,552 | 115 | 3,719 | 6,617 | improved |

Diagnosis:

- The 3000-second run was not "the same ALNS for longer." In `solver.py`,
  `timelimit >= 1500` enters `solve_hierarchical_preference`, while 130-second
  runs use the regular ALNS path.
- The pre-guard long path converged to a lower secondary/preference solution
  with much worse access/tardiness (`ideal_blocked_tardiness_share` 0.951).
- A long-run reference guard was added for 150+ block, 3-bay, 1500+ second runs:
  build a 60-130 second ALNS reference incumbent first, then run the long
  hierarchical path with the remaining time, and return only the official
  objective-best feasible solution.
- The guard is disabled by `OGC_DISABLE_LONG_REFERENCE_ALNS` for A/B tests.
- A second long-run check on `prob_28` improved both objective and obj1, so the
  guard did not reproduce the earlier `prob_28` reserve-regression failure.
- A third long-run check on low-tardiness `prob_29` also improved objective and
  obj1. This is important because `prob_29` was the case where fixed-placement
  CP probes tended to damage the solution.

Current conclusion:

- Long timelimits must be treated as a separate regime, not a free improvement.
- For hidden robustness, long paths should be incumbent-protected by a known
  reliable shorter-budget profile before any specialized large-instance path
  spends the rest of the time.

## ALNS Iteration And Placement-Candidate Diagnostics

Question: are ALNS iterations too few, and should placement choose positions by
masking already occupied bay regions?

Trace support:

- `OGC_ALNS_TRACE=1` already reported ALNS iterations, iterations per second,
  local checks, and full checks.
- Additional trace-only insertion counters were added:
  `position_candidates`, `position_candidates_visited`, `slot_candidates`,
  `slot_candidates_visited`, `collision_pairs_checked`, and rejection counters
  for entry access, exit access, existing entry times, earlier-due exits, and
  collisions.

Representative `prob_26` 30-second trace before safe slot pruning:

| Metric | Value |
| --- | ---: |
| Seed seconds | 14.703 |
| Search seconds | 13.703 |
| ALNS iterations | 34 |
| Search iterations/sec | 2.481 |
| Position candidates visited | 78,877 |
| Slot candidates visited | 1,531,933 |
| Existing-entry slot rejects | 1,181,037 |
| Collision pairs checked | 442,968 |

After pruning slot candidates that are already occupied by another block's entry
time before the slot loop:

| Metric | Value |
| --- | ---: |
| Search seconds | 13.406 |
| ALNS iterations | 34 |
| Search iterations/sec | 2.536 |
| Slot candidates visited | 350,896 |
| Existing-entry slot candidates pruned | 1,270,605 |
| Collision pairs checked | 442,968 |

Interpretation:

- The pruning is safe because those slots were already rejected later by the
  same `_safe_slots` function. It changes loop cost, not the feasible search
  space.
- In this short trace it dramatically reduced slot-loop visits but did not
  increase ALNS iterations, so the bottleneck is not only that loop. Seed time,
  access checks, and collision checks still dominate.
- Occupancy masking is promising as a placement-candidate idea, but a hard
  rectangular mask can be too conservative for OGC shapes because blocks are
  layered polygons, not always axis-aligned rectangles. A bounding-box mask may
  discard positions that official polygon collision checks would allow.

Next safe experiment:

- Do not use mask overlap as a hard rejection unless it is exact for the shape
  representation.
- Test a limited "free-anchor" generator instead: use an occupancy grid or
  rectangle mask only to propose a few extra candidate anchor points, then still
  pass them through official `contains_block`, `check_entry`, `check_exit`, and
  collision checks.
- Compare with `OGC_ALNS_TRACE=1` using iterations/sec, accepted objective, and
  candidate rejection counters before considering submission-default behavior.

Free-anchor probe:

- Added env-gated `OGC_FREE_ANCHOR_CANDIDATES=1`. It does not delete any
  existing edge candidates. It only appends a small number of candidate anchors
  that look free under placed-block bounding boxes, and all such candidates
  still pass through the official geometry/access checks.
- `prob_26` 30-second trace with free anchors:

| Metric | Pruning-only | Free-anchor flag |
| --- | ---: | ---: |
| ALNS iterations | 34 | 32 |
| Search iterations/sec | 2.536 | 2.424 |
| Free-anchor candidates added | 0 | 1,346 |
| Position candidates visited | 78,877 | 79,203 |
| Collision pairs checked | 442,968 | 445,555 |
| Objective | 100,525,240 | 104,949,322 |
| Obj1 | 7,468 | 7,799 |

Decision:

- Do not enable free anchors by default. On this trace they reduced iterations
  and worsened objective.
- Do not keep the env-gated implementation in submitted code either. Even
  trace-only/statistics and disabled candidate experiments added enough hot-loop
  overhead to perturb limited-time search.
- The submitted default was reverted to the original edge-candidate generator
  and original `_safe_slots` loop.
- Confirmation after reverting the instrumentation/free-anchor/slot-pruning
  experiment: `prob_26` 130s returned to `37,848,869 / obj1 2,766`, matching the
  previous reference incumbent.

Final decision:

- ALNS iteration diagnostics are useful, but they should be gathered with
  analysis-only probes or temporary patches, not carried in the submitted hot
  path.
- Mask/free-anchor placement is not rejected conceptually, but the tested
  implementation is rejected for submitted default behavior.

## Lower-Bound Follow-Up: Mandatory Time-Slice Placement

`scripts/lower_bound_timeslice_placement_cp_sat.py` was added to test a smaller
certificate than the full shared-placement interval model.

Safe rule:

- At integer time `t`, a block must be present in every on-time schedule if
  `release + processing > t` and `t + processing > due_date`.
- If a selected set of such mandatory-at-`t` blocks cannot be placed
  simultaneously in any integer placements, at least one selected block must
  start at or after `t`.
- That gives an obj1 lower bound of at least
  `min(t + processing_i - due_i)` over the selected infeasible set.

Measured results:

| Instance | Selection | Times | Blocks per slice | Complete slices | Certificate | Obj1 LB |
| --- | --- | ---: | ---: | ---: | --- | ---: |
| prob_26 | few-options | 3 | 2 | 3 | none, placement feasible | 0 |
| prob_25 | few-options | 3 | 2 | 0 | conflict enumeration incomplete | 0 |

Representative complete `prob_26` slices used blocks `[81, 144]` at times
`28`, `29`, and `30`. Each slice had 5,682 placement options and 1,142,396
exact placement conflicts, but the simultaneous placement SAT was feasible.

Interpretation:

- The mandatory time-slice certificate is globally valid and much smaller than
  full interval shared-placement CP, but it still has not produced a positive
  obj1 lower bound on the tested visible instances.
- This further supports the current diagnosis: the visible tardiness is not
  explained by simple static co-placement impossibility. The hard part is
  dynamic access, ordering, and future exit preservation.

## Lower-Bound Integration: CP-SAT Secondary Assignment in Batch Eval

`scripts/batch_eval.py` now supports an optional exact relaxed CP-SAT lower
bound for the weighted secondary contribution `w2*obj2 + w3*obj3`:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\batch_eval.py --bounds-only --instances prob_2 prob_26 --secondary-cp-time-limit 5
```

This keeps submitted solver behavior unchanged. It only strengthens visible
gap reporting. The CP model ignores placement, timing, and access constraints,
so it is a valid relaxation for secondary objective quality.

Measured results:

| Instance | Old secondary LB | CP secondary LB | Status |
| --- | ---: | ---: | --- |
| prob_2 | 0 | 3,690 | OPTIMAL |
| prob_21 | 0 | 91,750 | OPTIMAL |
| prob_24 | 0 | 24,755 | OPTIMAL |
| prob_26 | 0 | 74,030 | OPTIMAL |
| prob_28 | 0 | 17,238 | OPTIMAL |
| prob_29 | 0 | 34,782 | OPTIMAL |

Notes:

- `lower_bound_obj2` and `lower_bound_obj3` remain conservative component
  lower bounds. For 3+ bay CP runs, the weighted secondary bound is stronger
  than any component decomposition, so CP solution components are reported as
  incumbent metadata only.
- This improves objective-gap accounting, but it does not solve the main gap:
  obj1 lower bounds remain the bottleneck on high-tardiness examples.

## Saved-Solution Gap Scoring

`scripts/score_solution.py` was added to score an existing solution file with
the same conservative lower-bound stack used by `batch_eval.py`.

Example:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\score_solution.py train\prob_26.json .codex_workspace\results\prob26_current_130s.solution.json --secondary-cp-time-limit 5
```

Representative saved-solution results with CP-SAT secondary bounds:

| Instance | Objective | obj1 | Weighted LB | Relative gap | obj1 LB |
| --- | ---: | ---: | ---: | ---: | ---: |
| prob_26 | 37,848,869 | 2,766 | 74,030 | 0.9980 | 0 |
| prob_28 | 13,372,274 | 905 | 17,238 | 0.9987 | 0 |
| prob_29 | 3,644,691 | 144 | 34,782 | 0.9905 | 0 |

Interpretation:

- The secondary bound integration makes gap accounting more honest, but the
  reported total relative gaps are still near 1.0 because obj1 lower bounds are
  zero on these cases.
- This confirms that future lower-bound work must target dynamic tardiness
  certificates, not only assignment or static packing relaxations.

## Conservative Footprint Surrogate Probe

Development hypothesis: before exploiting detailed tier geometry, test whether
a simpler vertical-prism world can produce good bay assignments and schedules.
Each block orientation is represented by its full orientation bounding box.
Co-present blocks in the same bay may overlap in time only when their selected
surrogate rectangles do not overlap.

New analysis tool:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_footprint_probe.py train\prob_1.json --time-limit 20 --grid-step 12 --max-options-per-block 2
```

The script is intentionally outside submitted solver code. It builds placement
options, solves option selection plus start/end times with CP-SAT, emits an
official solution when CP finds one, and always reports a serial conservative
baseline for comparison.

Initial measurements:

| Instance | Scope | Options/block | Time | CP status | Serial obj1 | CP obj1 | Official feasible |
| --- | --- | ---: | ---: | --- | ---: | ---: | --- |
| prob_1 | earliest 20 blocks | 10 | 10s | FEASIBLE | n/a | 184 | yes |
| prob_1 | all 100 blocks | 1 | 5s | FEASIBLE | 13,123 | 13,123 | yes |
| prob_1 | all 100 blocks | 2, diverse bay cap | 20s | FEASIBLE | 32,785 | 13,111 | yes |
| prob_1 | all 100 blocks | 4, diverse bay cap | 30s | UNKNOWN | 32,785 | n/a | serial yes |
| prob_26 | earliest 50 blocks | 3, diverse bay cap | 30s | FEASIBLE | 17,387 | 4,653 | yes |

Follow-up: the full grid option model becomes hard when options/block grows.
Added `--mode lanes`, which keeps the same conservative bbox-footprint
assumption but fixes `x` to the left wall and varies only vertical lane
positions. The option cap now preserves bay and lane diversity. A multi-start
greedy conflict-graph list scheduler also reports a feasible conservative
schedule even when CP-SAT does not find an incumbent. It tries several block
orders and option scoring modes, then applies a conservative tardy-block
relocation pass.

| Instance | Scope | Mode | Options/block | Time | CP status | Serial obj1 | Greedy obj1 | CP obj1 | Official feasible |
| --- | --- | --- | ---: | ---: | --- | ---: | ---: | ---: | --- |
| prob_1 | all 100 blocks | lanes, step 6 | 4 | 20s | FEASIBLE | 32,785 | n/a | 6,014 | yes |
| prob_1 | all 100 blocks | lanes, step 4 | 8 | 5s | UNKNOWN | 32,785 | 5,500 | n/a | greedy yes |
| prob_1 | all 100 blocks | lanes, step 4 + conflict order | 8 | 1s | UNKNOWN | 32,785 | 5,432 | n/a | greedy yes |
| prob_1 | all 100 blocks | lanes, step 4 + fixed-option schedule CP | 8 | 3s CP | UNKNOWN | 32,785 | 5,432 | 4,460 | yes |
| prob_26 | earliest 50 blocks | lanes, step 8 | 6 | 20s | FEASIBLE | 17,387 | n/a | 4,281 | yes |
| prob_26 | earliest 100 blocks | lanes, step 8 | 6 | 1s | UNKNOWN | 74,002 | 19,378 | n/a | greedy yes |
| prob_26 | earliest 100 blocks | lanes, step 8 + fixed-option schedule CP | 6 | 3s CP | UNKNOWN | 74,002 | 19,378 | 16,572 | yes |

Interpretation:

- The conservative surrogate can produce official feasible schedules; bbox
  nonoverlap is strong enough to remove most entry/exit-path ambiguity.
- On `prob_26` early-due subset, CP-SAT reduced conservative serial tardiness
  from 17,387 to 4,653. This supports the user's decomposition: even before
  detailed geometry relaxation, bay assignment and scheduling matter.
- On full `prob_1`, the surrogate is still much worse than the current solver
  (`obj1` in the tens of thousands versus hundreds in the actual solver). This
  means a pure bounding-box vertical-prism model is too conservative for some
  two-bay instances, or the option/search design needs stronger decomposition.
- Increasing full-grid options per block makes the CP harder quickly. Lane
  decomposition plus greedy conflict-graph scheduling is more robust and gives
  immediate feasible incumbents, but still leaves a large gap to the actual
  solver on `prob_1`.
- The multi-start greedy selected EDD/objective on `prob_1`, and EDD/tardiness
  on `prob_26` earliest-100. The added relocate pass did not improve these two
  representative settings, suggesting that order-level changes are currently
  more important than single-block post-relocation under the fixed lane set.
- Adding conflict-degree-aware block orders improved `prob_1` all-100 from
  `obj1=5,500` to `obj1=5,432` with strategy `due_conflict/objective`. It did
  not beat EDD/tardiness on `prob_26` earliest-100.
- Fixing the conservative bay/lane choices and re-optimizing only start/end
  times with CP-SAT reduced `prob_1` all-100 from `obj1=5,432` to `4,460`, and
  `prob_26` earliest-100 from `19,378` to `16,572`. This confirms that, even in
  the simplified footprint world, schedule optimization remains a major
  bottleneck before actual geometry relaxation should be considered.
- The script now protects saved output by comparing official objective across
  greedy, fixed-option schedule CP, and full option CP candidates. A full option
  CP feasible incumbent can be worse than the fixed-option schedule CP fallback;
  `prob_29` earliest-100 exposed this and now saves the better schedule-CP
  candidate.
- Broader representative runs all selected `fixed_option_schedule_cp` as the
  best saved source by official objective:

| Instance | Scope | Serial obj1 | Greedy obj1 | Fixed-option schedule CP obj1 | Best greedy strategy |
| --- | --- | ---: | ---: | ---: | --- |
| prob_1 | all 100 | 32,785 | 5,432 | 4,460 | due_conflict/objective |
| prob_2 | all 100 | 33,154 | 3,984 | 3,118 | due_conflict/objective |
| prob_21 | all 100 | 99,494 | 24,884 | 21,325 | edd/balance |
| prob_24 | all 100 | 74,437 | 14,811 | 12,715 | edd/objective |
| prob_26 | earliest 100 | 74,002 | 19,378 | 16,572 | edd/tardiness |
| prob_27 | earliest 100 | 94,591 | 26,280 | 24,248 | due_conflict/tardiness |
| prob_28 | earliest 100 | 76,132 | 14,015 | 11,687 | tight_slack/tardiness |
| prob_29 | earliest 100 | 49,897 | 6,477 | 5,529 | due_conflict/objective |

- This broader table strengthens the diagnosis: conservative bay/lane selection
  is useful, but timing compression after option selection is consistently
  important. Actual tier-geometry relaxation is still not the next bottleneck.
- A longer best-of schedule CP pass tries the hint, a short 3-second CP, and
  the requested longer CP, then saves the best official candidate. Representative
  best-observed obj1 values:

| Instance | Scope | 3s fixed-option CP obj1 | Best-of 20s obj1 | Note |
| --- | --- | ---: | ---: | --- |
| prob_1 | all 100 | 4,460 | 4,412 | improved |
| prob_2 | all 100 | 3,118 | 3,062 | improved |
| prob_26 | earliest 100 | 16,572 | 16,304 | improved |
| prob_28 | earliest 100 | 11,687 | 11,687 | 20s rerun was worse, keep best observed |

- CP-SAT feasible incumbents are not perfectly monotone across separate runs,
  especially with parallel workers. For analysis, compare official objective
  across saved candidates rather than assuming a longer run dominates a shorter
  one.
- Added a small tardy-option neighborhood CP. It starts from the fixed-option
  schedule-CP solution, lets only the top tardy blocks change conservative
  lane/bay options, and re-optimizes timing under the same bbox-footprint
  conflict graph. This still does not use actual tier geometry.

| Instance | Scope | Greedy obj1 | Fixed-option schedule CP obj1 | Tardy-option neighborhood obj1 | Best source |
| --- | --- | ---: | ---: | ---: | --- |
| prob_1 | all 100 | 5,432 | 4,474 | 4,410 | neighborhood |
| prob_2 | all 100 | 3,984 | 3,108 | 3,038 | neighborhood |
| prob_26 | earliest 100 | 19,378 | 16,438 | 15,978 | neighborhood |
| prob_28 | earliest 100 | 14,015 | 11,767 | 11,421 | neighborhood |

- The neighborhood results show that, after schedule compression, conservative
  option selection is still a live bottleneck. The right next step remains
  conservative-model improvement: expand/change lane options for a small tardy
  set and schedule them jointly. Actual tier-geometry relaxation is still a
  later phase.
- Increasing the tardy-option neighborhood is useful but not monotone. A
  16-block / 10-second neighborhood improved `prob_1` to `obj1=4,405`, `prob_2`
  to `3,053`, and `prob_26` earliest-100 to `16,098`, but worsened `prob_28`
  versus the 8-block best. The script now tries multiple neighborhood sizes
  (`4`, `8`, requested max) inside one run and saves the best official candidate.
- Best observed conservative-lane values so far:

| Instance | Scope | Best observed obj1 | Source |
| --- | --- | ---: | --- |
| prob_1 | all 100 | 4,360 | best-of neighborhood sizes |
| prob_2 | all 100 | 3,038 | 8-block neighborhood |
| prob_26 | earliest 100 | 15,978 | 8-block neighborhood |
| prob_28 | earliest 100 | 11,421 | 8-block neighborhood |

- Added `--mode edge_lanes`, which keeps the same conservative bbox-footprint
  model but no longer forces every lane to the left wall. For each
  bay/orientation/y-lane, it tries coarse bbox-left positions. The default
  `--x-anchor-count 3` gives left wall, center, and right wall; larger values
  add intermediate x anchors. This is still not actual tier-geometry
  relaxation; the only new freedom is coarse x-position choice under the
  vertical-prism footprint.

Paired checks with the same short schedule/neighborhood CP limits:

| Instance | Scope | Mode | Options/block | Greedy obj1 | Fixed-option schedule CP obj1 | Tardy-option neighborhood obj1 | Official feasible |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| prob_1 | all 100 | lanes, step 4 | 8 | 5,432 | 4,454 | 4,452 | yes |
| prob_1 | all 100 | edge_lanes, step 4 | 12 | 3,010 | 2,541 | 2,541 | yes |
| prob_2 | all 100 | lanes, step 8 | 8 | 2,253 | 1,711 | 1,709 | yes |
| prob_2 | all 100 | edge_lanes, step 8 | 12 | 1,463 | 1,269 | 1,269 | yes |
| prob_26 | earliest 100 | lanes, step 8 | 8 | 17,013 | 14,660 | 14,656 | yes |
| prob_26 | earliest 100 | edge_lanes, step 8 | 12 | 6,172 | 5,231 | 5,225 | yes |
| prob_28 | earliest 100 | lanes, step 8 | 8 | 10,347 | 8,856 | 8,598 | yes |
| prob_28 | earliest 100 | edge_lanes, step 8 | 12 | 6,318 | 5,509 | 5,479 | yes |

- This is a stronger diagnosis than the earlier left-wall lane probe. A large
  portion of the residual conservative-model tardiness was caused by an
  over-restricted x-position candidate set, not by actual tier geometry.
- Increasing edge-lane x anchors from 3 to 5, with a matching option cap
  increase from 12 to 18, improved all four paired checks:

| Instance | Scope | 3-anchor edge-lanes obj1 | 5-anchor edge-lanes obj1 | Official feasible |
| --- | --- | ---: | ---: | --- |
| prob_1 | all 100 | 2,541 | 1,318 | yes |
| prob_2 | all 100 | 1,269 | 1,004 | yes |
| prob_26 | earliest 100 | 5,225 | 3,222 | yes |
| prob_28 | earliest 100 | 5,479 | 4,247 | yes |

- The conservative scheduler is therefore not saturated yet. Better coarse
  footprint option generation still pays off before any actual tier-shape
  relaxation is introduced.
- Added `--edge-cap-strategy balanced` for `edge_lanes`. The previous ranked
  cap could over-sample low y-lanes because edge-lane options are sorted by
  `bottom` before x diversity. The balanced cap keeps part of each bay quota
  spread over y-lanes and the rest spread over x anchors before filling by rank.

With `--x-anchor-count 5` and `--max-options-per-block 18`, balanced cap
improved all paired checks:

| Instance | Scope | Ranked cap obj1 | Balanced cap obj1 | Official feasible |
| --- | --- | ---: | ---: | --- |
| prob_1 | all 100 | 1,318 | 628 | yes |
| prob_2 | all 100 | 1,004 | 119 | yes |
| prob_26 | earliest 100 | 3,222 | 2,438 | yes |
| prob_28 | earliest 100 | 4,247 | 2,230 | yes |

- This is strong evidence that the current simplified-world bottleneck is not
  only timing CP. The conservative option generator and cap policy determine
  whether the schedule even has good bay/position choices available.
- A broader balanced-cap check kept the same short schedule/neighborhood CP
  limits and stayed official-feasible on all tested cases:

| Instance | Scope | Greedy obj1 | Schedule CP obj1 | Neighborhood obj1 | Neighborhood objective |
| --- | --- | ---: | ---: | ---: | ---: |
| prob_1 | all 100 | 661 | 650 | 628 | 18,653,643 |
| prob_2 | all 100 | 123 | 123 | 119 | 4,029,839 |
| prob_21 | all 100 | 3,044 | 2,850 | 2,827 | 38,321,081 |
| prob_24 | all 100 | 1,894 | 1,739 | 1,727 | 23,800,021 |
| prob_26 | earliest 100 | 2,660 | 2,444 | 2,438 | 33,069,603 |
| prob_27 | earliest 100 | 5,456 | 5,215 | 5,195 | 70,736,687 |
| prob_28 | earliest 100 | 2,547 | 2,244 | 2,230 | 30,516,397 |
| prob_29 | earliest 100 | 1,243 | 1,021 | 1,014 | 14,458,095 |

- Longer timing optimization helps, but less dramatically than the candidate
  generation and cap changes. With the same balanced options, increasing
  fixed-option schedule CP to 20 seconds and the flexible neighborhood to 16
  blocks changed obj1 as follows:

| Instance | Short obj1 | Longer CP/neighborhood obj1 | Note |
| --- | ---: | ---: | --- |
| prob_1 | 628 | 600 | schedule CP reached optimal for fixed options |
| prob_2 | 119 | 119 | already optimal for fixed options |
| prob_26 | 2,438 | 2,414 | small improvement |
| prob_28 | 2,230 | 2,162 | small improvement |

- This separates two effects: the conservative scheduler still benefits from
  more timing search, but the large jump came from retaining better coarse
  footprint options. The next high-value work is therefore option-aware
  construction/search under the conservative footprint model, not actual
  tier-shape relaxation yet.
- Added `--neighborhood-objective objective` and `both` as a diagnostic for
  the tardy-option neighborhood CP. The objective mode adds the coarse
  `obj1 + obj2 + obj3` surrogate terms to the flexible-option CP objective.
  `both` tries the tardiness-focused and objective-focused neighborhoods and
  keeps the best official feasible candidate.

Short objective-neighborhood checks did not beat the existing tardiness-focused
neighborhood on the representative set:

| Instance | Tardiness-neighborhood obj1/objective | Objective-neighborhood obj1/objective | Interpretation |
| --- | ---: | ---: | --- |
| prob_1 | 628 / 18,653,643 | 635 / 18,857,280 | worse |
| prob_2 | 119 / 4,029,839 | 123 / 4,137,833 | worse |
| prob_26 | 2,438 / 33,069,603 | 2,444 / 33,149,601 | worse |
| prob_28 | 2,230 / 30,516,397 | 2,238 / 30,622,476 | worse |

- This suggests the current coarse objective approximation is not the limiting
  issue inside the small neighborhood. Keeping the neighborhood pressure mostly
  on tardiness is better for now, while official objective ranking remains the
  acceptance rule across generated candidates.
- Added conflict-aware greedy score variants:
  `objective_conflict`, `tardiness_conflict`, and `balance_conflict`. These keep
  the same primary greedy score but use the conservative conflict-graph degree
  of each option as a tie-break. They are included as additional multi-start
  candidates, not as a replacement for the original score modes.

Short balanced-cap checks after adding these variants:

| Instance | Prior balanced obj1/objective | With conflict-aware modes obj1/objective | Selected greedy score |
| --- | ---: | ---: | --- |
| prob_1 | 628 / 18,653,643 | 634 / 18,828,189 | objective |
| prob_2 | 119 / 4,029,839 | 94 / 3,264,074 | balance_conflict |
| prob_21 | 2,827 / 38,321,081 | 2,707 / 36,730,871 | tardiness_conflict |
| prob_24 | 1,727 / 23,800,021 | 1,751 / 24,132,393 | objective |
| prob_26 | 2,438 / 33,069,603 | 2,207 / 30,061,742 | tardiness_conflict |
| prob_27 | 5,195 / 70,736,687 | 5,176 / 70,483,360 | balance |
| prob_28 | 2,230 / 30,516,397 | 2,133 / 29,574,084 | balance_conflict |
| prob_29 | 1,014 / 14,458,095 | 943 / 13,566,577 | objective_conflict |

- The signal is useful but mixed: five of eight improved clearly, two worsened
  slightly, and one improved only marginally. Because CP-SAT incumbents vary
  across separate runs, the safe interpretation is that conflict-aware option
  scoring is another good multi-start candidate, not a universally dominant
  policy.
- Added `scripts/surrogate_batch.py` to run the conservative surrogate over an
  instance matrix and save compact JSONL summaries. The visible training set
  size distribution is:
  - 100 blocks: `prob_1`-`prob_4`, `prob_21`-`prob_25`
  - 150 blocks: `prob_5`-`prob_8`, `prob_26`-`prob_30`
  - 200 blocks: `prob_9`-`prob_12`, `prob_31`-`prob_35`
  - 250 blocks: `prob_13`-`prob_16`, `prob_36`-`prob_40`
  - 300 blocks: `prob_17`-`prob_20`

Balanced edge-lane matrix on all 100-block visible instances plus earliest-100
prefixes of `prob_26`-`prob_30`:

| Instance | Scope | Selected greedy score | Neighborhood obj1 | Neighborhood objective |
| --- | --- | --- | ---: | ---: |
| prob_1 | all 100 | balance_conflict | 479 | 14,553,646 |
| prob_2 | all 100 | balance_conflict | 94 | 3,264,074 |
| prob_3 | all 100 | balance_conflict | 190 | 5,644,550 |
| prob_4 | all 100 | objective_conflict | 482 | 11,014,295 |
| prob_21 | all 100 | tardiness_conflict | 2,747 | 37,264,191 |
| prob_22 | all 100 | tardiness_conflict | 1,405 | 20,040,288 |
| prob_23 | all 100 | objective_conflict | 3,331 | 45,658,251 |
| prob_24 | all 100 | objective | 1,732 | 23,879,066 |
| prob_25 | all 100 | objective_conflict | 4,525 | 3,069,026 |
| prob_26 | earliest 100 | tardiness_conflict | 2,199 | 29,959,775 |
| prob_27 | earliest 100 | balance | 5,149 | 70,123,369 |
| prob_28 | earliest 100 | balance_conflict | 2,104 | 29,176,132 |
| prob_29 | earliest 100 | objective_conflict | 962 | 13,849,958 |
| prob_30 | earliest 100 | tardiness | 2,024 | 27,504,524 |

- All 14 runs were official feasible. Average obj1 across this matrix was
  1,958.79, with a range from 94 to 5,149.
- Conflict-aware greedy variants were selected in 11 of 14 runs, supporting
  their use as multi-start candidates. The three non-conflict selections show
  why they should remain candidates rather than become a hard rule.
- This matrix is stronger evidence for the staged plan: the conservative
  footprint model can already produce coherent bay/schedule structures over a
  broad visible 100-block set. Remaining work should broaden and stabilize this
  simplified-world search before actual geometry relaxation is introduced.
- Full 150-block visible check with a higher conflict-pair cap
  (`--max-conflict-pairs 1500000`) also stayed official-feasible and did not
  truncate conservative conflicts:

| Instance | Bays | Selected greedy score | Conflict pairs | Neighborhood obj1 | Neighborhood objective |
| --- | ---: | --- | ---: | ---: | ---: |
| prob_5 | 3 | balance_conflict | 474,498 | 217 | 4,247,065 |
| prob_6 | 3 | balance | 460,255 | 1,407 | 42,567,918 |
| prob_7 | 3 | balance_conflict | 488,179 | 477 | 9,380,762 |
| prob_8 | 2 | tardiness | 639,934 | 7 | 340,464 |
| prob_26 | 3 | tardiness_conflict | 453,687 | 5,838 | 78,817,915 |
| prob_27 | 2 | tardiness | 693,911 | 12,622 | 170,281,806 |
| prob_28 | 3 | balance_conflict | 442,533 | 5,429 | 74,062,406 |
| prob_29 | 3 | balance_conflict | 354,793 | 2,685 | 37,787,223 |
| prob_30 | 2 | tardiness | 716,287 | 5,096 | 68,808,960 |

- All 9 full 150-block runs were official feasible, with zero conflict-pair
  truncation. Average obj1 was 3,753.11, ranging from 7 to 12,622.
- The 150-block check is encouraging for feasibility and structure, but it also
  exposes the next bottleneck: some two-bay or tighter instances, especially
  `prob_27`, still need stronger simplified-world option search and schedule
  compression before actual geometry relaxation is useful.
- Focused `prob_27` probes separated three possible bottlenecks:

| Variant | Neighborhood obj1 | Neighborhood objective | Conflict pairs | Interpretation |
| --- | ---: | ---: | ---: | --- |
| baseline x5/cap18/step8 | 12,622 | 170,281,806 | 693,911 | reference |
| y step 4, x5/cap18 | 13,352 | 180,182,478 | 765,894 | worse |
| x7/cap24, y step 8 | 9,543 | 129,328,245 | 1,123,701 | best structural change |
| x5/cap18, longer CP | 12,532 | 168,992,350 | 693,911 | small improvement |

- For this tight two-bay case, increasing x-anchor diversity and option cap was
  much more useful than making y-lanes denser or spending more schedule CP time.
  This points to conservative option availability, not actual tier geometry, as
  the next bottleneck.
- User-suggested due-date spreading was tested as a soft greedy multi-start
  signal, not a hard rule. New score variants
  `objective_due_spread`, `tardiness_due_spread`, and `balance_due_spread`
  penalize placing close-due blocks into the same bay when their conservative
  options also conflict. Initial two-bay 150-block checks:

| Instance | x7/cap24 obj1/objective | With due-spread candidates obj1/objective | Selected score |
| --- | ---: | ---: | --- |
| prob_27 | 9,543 / 129,328,245 | 9,501 / 128,768,259 | balance_conflict |
| prob_30 | 4,109 / 55,563,581 | 4,022 / 54,382,134 | balance_due_spread |
| prob_8 | 2 / 67,804 | 1 / 65,736 | tardiness_due_spread |

- The due-spread idea is promising as an additional multi-start signal. It
  should remain soft because close due dates are not automatically bad; they are
  risky mainly when combined with same-bay conservative conflicts.
- The richer two-bay setting was then checked across every visible two-bay case
  in the 100-block set plus full 150-block two-bay cases. Compared with the
  previous x5/cap18 balanced setting, x7/cap24 improved all eight runs:

| Instance | Blocks | x5/cap18 obj1/objective | x7/cap24 obj1/objective | Selected score |
| --- | ---: | ---: | ---: | --- |
| prob_1 | 100 | 479 / 14,553,646 | 360 / 10,865,261 | objective_conflict |
| prob_4 | 100 | 482 / 11,014,295 | 164 / 4,286,479 | balance_due_spread |
| prob_22 | 100 | 1,405 / 20,040,288 | 638 / 10,122,451 | tardiness_conflict |
| prob_23 | 100 | 3,331 / 45,658,251 | 2,733 / 37,680,352 | balance_due_spread |
| prob_25 | 100 | 4,525 / 3,069,026 | 3,886 / 2,662,833 | balance_conflict |
| prob_8 | 150 | 7 / 340,464 | 1 / 65,736 | tardiness_due_spread |
| prob_27 | 150 | 12,622 / 170,281,806 | 9,430 / 127,826,346 | balance_conflict |
| prob_30 | 150 | 5,096 / 68,808,960 | 4,021 / 54,368,801 | balance_due_spread |

- None of these richer two-bay runs truncated the conservative conflict graph.
  This is the clearest setting-specific conclusion so far: two-bay conservative
  surrogate search benefits from more x anchors and a larger option cap. This
  should be treated as a two-bay policy, not automatically applied to all
  3+ bay cases without a separate cap/runtime check.
- The same x7/cap24 setting was then checked on representative three-bay
  100/150-block instances. It improved every tested 3-bay case without conflict
  truncation:

| Instance | Blocks | x5/cap18 obj1/objective | x7/cap24 obj1/objective | Selected score |
| --- | ---: | ---: | ---: | --- |
| prob_2 | 100 | 94 / 3,264,074 | 10 / 480,800 | objective_due_spread |
| prob_3 | 100 | 190 / 5,644,550 | 18 / 1,148,156 | balance_due_spread |
| prob_21 | 100 | 2,747 / 37,264,191 | 1,539 / 21,244,067 | balance_conflict |
| prob_24 | 100 | 1,732 / 23,879,066 | 1,056 / 14,782,323 | objective |
| prob_5 | 150 | 217 / 4,247,065 | 47 / 1,125,290 | objective_due_spread |
| prob_6 | 150 | 1,407 / 42,567,918 | 651 / 19,996,698 | objective_due_spread |
| prob_7 | 150 | 477 / 9,380,762 | 33 / 957,788 | objective_due_spread |
| prob_26 | 150 | 5,838 / 78,817,915 | 4,120 / 56,029,696 | balance |
| prob_28 | 150 | 5,429 / 74,062,406 | 3,667 / 50,668,297 | balance_conflict |
| prob_29 | 150 | 2,685 / 37,787,223 | 1,788 / 25,425,354 | tardiness |

- The richer x-anchor/cap policy is therefore not only a two-bay fix on the
  tested 100/150-block visible cases. Within this size range, x7/cap24 with
  balanced edge lanes is the stronger conservative-surrogate default. The scope
  caveat is still important: 200+ block instances need separate conflict-count
  and runtime checks before adopting the same cap.
- Visible 200-block cases were then checked with the same x7/cap24 setting and
  a higher conflict-pair cap (`--max-conflict-pairs 3000000`). All nine runs
  were official feasible and none truncated the conservative conflict graph:

| Instance | Bays | Selected score | Conflict pairs | Neighborhood obj1 | Neighborhood objective |
| --- | ---: | --- | ---: | ---: | ---: |
| prob_9 | 3 | tardiness_due_spread | 1,333,041 | 59 | 1,324,412 |
| prob_10 | 4 | objective_due_spread | 1,190,936 | 46 | 1,119,191 |
| prob_11 | 4 | balance_conflict | 1,042,403 | 708 | 17,180,837 |
| prob_12 | 4 | balance_due_spread | 1,109,058 | 831 | 19,259,959 |
| prob_31 | 4 | objective_conflict | 1,093,524 | 9,130 | 124,710,526 |
| prob_32 | 3 | objective_conflict | 1,455,541 | 5,776 | 20,504,678 |
| prob_33 | 3 | balance_conflict | 1,287,983 | 7,936 | 54,093,202 |
| prob_34 | 4 | objective_conflict | 959,706 | 7,002 | 24,766,969 |
| prob_35 | 3 | tardiness_due_spread | 1,505,466 | 4,768 | 64,576,964 |

- Average obj1 over these 200-block cases was 4,028.44, with max obj1 9,130
  and average elapsed time about 29.5 seconds per instance. The setting remains
  feasible and non-truncated at 200 blocks, but the later visible cases
  (`prob_31`-`prob_35`) still show substantial tardiness. The next simplified
  bottleneck is therefore not feasibility; it is stronger schedule compression
  or larger option-neighborhood repair under the conservative footprint model.
- Visible 250-block cases were checked with the same x7/cap24 setting and
  `--max-conflict-pairs 5000000`. Again, all nine runs were official feasible
  and none truncated the conflict graph:

| Instance | Bays | Selected score | Conflict pairs | Neighborhood obj1 | Neighborhood objective |
| --- | ---: | --- | ---: | ---: | ---: |
| prob_13 | 4 | objective_conflict | 1,558,253 | 1,258 | 24,369,590 |
| prob_14 | 4 | objective_conflict | 1,706,467 | 1,306 | 24,102,927 |
| prob_15 | 4 | objective | 1,522,059 | 172 | 3,373,198 |
| prob_16 | 4 | tardiness_due_spread | 1,717,232 | 39 | 753,205 |
| prob_36 | 4 | balance_conflict | 1,548,689 | 7,520 | 5,180,470 |
| prob_37 | 3 | objective_due_spread | 2,068,752 | 8,493 | 29,711,525 |
| prob_38 | 3 | balance_conflict | 2,027,816 | 19,435 | 262,179,003 |
| prob_39 | 3 | balance_due_spread | 1,849,952 | 7,522 | 101,948,074 |
| prob_40 | 4 | balance_due_spread | 1,749,643 | 17,405 | 11,782,439 |

- Average obj1 over the 250-block set was 7,016.67, with max obj1 19,435 and
  average elapsed time about 43.8 seconds per instance. This confirms that the
  conservative surrogate is still structurally feasible at 250 blocks, but
  tardiness grows. The next bottleneck is now clearly stronger scheduling or
  broader option-neighborhood repair, not actual geometry relaxation.
- Visible 300-block cases were checked with `--max-conflict-pairs 8000000`.
  All four were official feasible and non-truncated:

| Instance | Bays | Selected score | Conflict pairs | Neighborhood obj1 | Neighborhood objective |
| --- | ---: | --- | ---: | ---: | ---: |
| prob_17 | 4 | objective_due_spread | 2,416,029 | 47 | 985,302 |
| prob_18 | 4 | balance_conflict | 2,397,569 | 1,187 | 17,246,547 |
| prob_19 | 4 | tardiness_due_spread | 2,440,050 | 141 | 2,320,834 |
| prob_20 | 5 | balance_due_spread | 1,785,725 | 2,959 | 79,879,511 |

- Average obj1 over the 300-block set was 1,083.5, with max obj1 2,959 and
  average elapsed time about 43.0 seconds. Across visible 100/150/200/250/300
  sizes, the conservative bbox surrogate now produces official-feasible,
  non-truncated schedules with the x7/cap24 edge-lane setting. This strongly
  supports the staged plan: actual tier geometry is not the next required
  source of feasibility. Remaining improvements should focus on simplified
  schedule compression, stronger option-neighborhood repair, and better
  instance-size-specific search budgets.
- The next conservative-model work should therefore improve option generation
  and option-aware scheduling under bbox footprints. Actual tier-shape
  relaxation remains a later phase, after the simplified scheduler and bay
  allocator are much stronger.
- These values are still much worse than the current actual-geometry solver on
  some full visible instances, but that is expected: the point is diagnosis, not
  final performance. The diagnosis is now stable: conservative scheduling and
  conservative option search both matter before geometry relaxation.
- The next useful step is not to add actual tier geometry yet, but to improve
  surrogate search over the conservative conflict graph: better fixed-option
  schedule optimization, local reinsert/swap that changes options, and adaptive
  lane selection.

Roadmap consequence:

1. Keep actual tier-geometry relaxation out of the near-term objective.
2. First build a stronger conservative-footprint scheduler/bay allocator.
3. Use actual geometry later as an extra degree of freedom after surrogate
   obj1 is competitive.

### Consolidated x7/cap24 visible sweep summary

- A final coverage-checked summary was generated from the non-duplicated
  x7/cap24 visible sweep files:
  `.codex_workspace/results/surrogate_visible_x7_cap24_summary.md` and
  `.codex_workspace/results/surrogate_visible_x7_cap24_summary.csv`.
- The summary command was run with `--expect-visible-40`, which verifies that
  `prob_1` through `prob_40` each appear exactly once.
- Result: 40/40 rows were official feasible and 0/40 rows truncated the
  conservative conflict graph.
- Overall obj1 average/min/max was 3,448 / 1 / 19,435. Overall elapsed
  average/max was 28.39 / 63.31 seconds. Average/max conflict pairs was
  1,182,567 / 2,440,050.
- Due-spread greedy score variants were selected in 20/40 rows, which supports
  the user's proposed due-date-spacing signal as a useful soft multi-start
  component rather than a hard bay-assignment rule.

| Blocks | Runs | Feasible | Truncated | Avg obj1 | Min obj1 | Max obj1 | Avg elapsed s | Max conflicts |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 100 | 9 | 9 | 0 | 1,156 | 10 | 3,886 | 13.18 | 546,601 |
| 150 | 9 | 9 | 0 | 2,640 | 1 | 9,430 | 20.51 | 1,155,034 |
| 200 | 9 | 9 | 0 | 4,028 | 46 | 9,130 | 29.53 | 1,505,466 |
| 250 | 9 | 9 | 0 | 7,017 | 39 | 19,435 | 43.84 | 2,068,752 |
| 300 | 4 | 4 | 0 | 1,084 | 47 | 2,959 | 42.99 | 2,440,050 |

Completion implication for the staged plan:

- The conservative bbox/footprint surrogate is now feasible and non-truncated
  across all visible instances with the x7/cap24 edge-lane setting.
- Therefore, actual tier-shape geometry relaxation is not required yet to make
  the staged model structurally workable.
- The remaining gap is quality inside the simplified model, especially schedule
  compression and option-changing neighborhood repair on hard 200/250-block
  cases such as `prob_31`-`prob_40`.
- Actual geometry should remain the later relaxation phase, used after the
  conservative scheduler and bay allocator become stronger.

### Blocker-aware neighborhood A/B

- A blocker-aware neighborhood selection was added as an analysis-only option:
  `--neighborhood-selection blockers` or `--neighborhood-selection both`.
- The intended hypothesis was that some large obj1 cases are not fixed by
  changing only the tardy blocks, because their conservative conflict-overlap
  blockers may need to move too.
- The implementation preserves the original `tardy` default. In `blockers`
  mode, the top tardy blocks are kept first, and additional flexible slots
  beyond the top eight are filled with current overlap-conflict neighbors.
  `both` tries `tardy` and `blockers` as candidate neighborhoods and keeps the
  best official feasible result.

Focused A/B results on hard cases:

| Instance | Original all-40 x7/cap24 obj1/objective | Tardy-12 obj1/objective | Blockers-12 obj1/objective | Both-12 obj1/objective | Keep? |
| --- | ---: | ---: | ---: | ---: | --- |
| prob_31 | 9,130 / 124,710,526 | 9,197 / 125,633,678 | 9,263 / 126,492,179 | 9,177 / 125,315,745 | no |
| prob_38 | 19,435 / 262,179,003 | 19,475 / 262,715,487 | 19,431 / 262,107,499 | 19,468 / 262,622,156 | no |
| prob_40 | 17,405 / 11,782,439 | 17,382 / 11,765,744 | 17,410 / 11,786,015 | 17,373 / 11,759,741 | maybe |

- The blocker-aware idea is not reliable enough to become the conservative
  default. It helped `prob_40` slightly when used as a candidate, but it hurt
  or failed to recover the original all-40 result on `prob_31` and `prob_38`.
- This is still useful information: the main hard-case gap is not simply that
  blocker blocks were excluded from the small CP neighborhood. The next
  conservative-model improvement should focus on broader schedule compression
  or a stronger option-changing local search that can preserve incumbent
  quality while exploring larger moves.

### Repeated incumbent-preserving tardy neighborhood

- A second conservative-model escalation was added as
  `--neighborhood-rounds`. The default remains one round.
- Each round starts from the best official feasible assignment found so far.
  A round is accepted only if it improves the official objective, so the
  repeated search is incumbent-preserving within a run.
- This is still a conservative bbox/footprint experiment; it does not use
  actual tier-shape relaxation.

Focused hard-case results with `--neighborhood-selection tardy`,
`--neighborhood-blocks 12`, `--neighborhood-cp-limit 12`, and
`--neighborhood-rounds 2`:

| Instance | Original all-40 x7/cap24 obj1/objective | Tardy-12 one-round obj1/objective | Tardy-12 two-round obj1/objective | Change vs original |
| --- | ---: | ---: | ---: | ---: |
| prob_31 | 9,130 / 124,710,526 | 9,197 / 125,633,678 | 8,968 / 122,599,057 | -162 obj1 |
| prob_38 | 19,435 / 262,179,003 | 19,475 / 262,715,487 | 19,382 / 261,456,770 | -53 obj1 |
| prob_40 | 17,405 / 11,782,439 | 17,382 / 11,765,744 | 17,306 / 11,719,652 | -99 obj1 |

- Unlike blocker-aware selection, repeated tardy neighborhoods improved all
  three focused hard cases. The gains are modest but directionally consistent.
- This supports the staged plan: the next useful work is still inside the
  conservative model, especially incumbent-preserving repeated schedule/option
  repair. Actual tier geometry should remain a later relaxation phase.

The same two-round setting was then expanded to the 200-block tail set
`prob_31`-`prob_35`. It improved all five cases relative to the all-40 x7/cap24
summary:

| Instance | Original obj1/objective | Round2 obj1/objective | Obj1 change |
| --- | ---: | ---: | ---: |
| prob_31 | 9,130 / 124,710,526 | 9,108 / 124,456,695 | -22 |
| prob_32 | 5,776 / 20,504,678 | 5,722 / 20,380,736 | -54 |
| prob_33 | 7,936 / 54,093,202 | 7,905 / 53,892,105 | -31 |
| prob_34 | 7,002 / 24,766,969 | 6,997 / 24,750,304 | -5 |
| prob_35 | 4,768 / 64,576,964 | 4,761 / 64,475,183 | -7 |

The 250-block tail set was more mixed with only two rounds and the same
12-second neighborhood budget:

| Instance | Original obj1/objective | Round2 obj1/objective | Obj1 change |
| --- | ---: | ---: | ---: |
| prob_36 | 7,520 / 5,180,470 | 7,369 / 5,077,273 | -151 |
| prob_37 | 8,493 / 29,711,525 | 8,493 / 29,711,525 | 0 |
| prob_38 | 19,435 / 262,179,003 | 19,443 / 262,291,105 | +8 |
| prob_39 | 7,522 / 101,948,074 | 7,533 / 102,042,841 | +11 |
| prob_40 | 17,405 / 11,782,439 | 17,373 / 11,763,647 | -32 |

Increasing the hard 250-block search to three rounds with a 24-second
neighborhood budget recovered improvements on the two mixed cases:

| Instance | Original obj1/objective | Round3/tl24 obj1/objective | Obj1 change |
| --- | ---: | ---: | ---: |
| prob_38 | 19,435 / 262,179,003 | 19,333 / 260,841,423 | -102 |
| prob_39 | 7,522 / 101,948,074 | 7,482 / 101,407,786 | -40 |

Current conservative-model conclusion:

- Repeated incumbent-preserving tardy neighborhoods are a real improvement
  direction under the bbox/footprint surrogate.
- The gain is still modest, so this is not the final scheduler, but it is a
  better next step than actual tier-shape relaxation.
- Search budget now matters: 200-block hard cases improve with two rounds,
  while some 250-block hard cases need a larger neighborhood CP budget.

### Best-known conservative visible summary after repeated neighborhoods

- A best-known visible summary was generated with
  `scripts/summarize_surrogate_results.py --dedupe best-objective`.
- Inputs were the original x7/cap24 all-40 sweep plus repeated-neighborhood
  follow-up probes and focused x9/cap30 hard/mid/low-case probes. The command
  loaded 97 rows, selected one best official
  objective per instance, and rechecked visible coverage.
- Output artifacts:
  `.codex_workspace/results/surrogate_visible_best_known_summary.md` and
  `.codex_workspace/results/surrogate_visible_best_known_summary.csv`.
- Coverage remained 40/40 visible instances, all official feasible, with 0/40
  conservative conflict truncations.

Compared with the first all-40 x7/cap24 summary:

| Metric | Original all-40 x7/cap24 | Best-known after repeated neighborhoods |
| --- | ---: | ---: |
| Loaded rows | 40 | 97 |
| Selected rows | 40 | 40 |
| Feasible | 40/40 | 40/40 |
| Truncated | 0/40 | 0/40 |
| Avg obj1 | 3,448 | 2,925 |
| Max obj1 | 19,435 | 17,314 |
| Avg objective | 33,277,652 | 28,201,285 |
| Max objective | 262,179,003 | 233,422,670 |
| Avg elapsed s | 28.39 | 38.27 |

By block count:

| Blocks | Original avg obj1 | Best-known avg obj1 | Original max obj1 | Best-known max obj1 |
| ---: | ---: | ---: | ---: | ---: |
| 100 | 1,156 | 953 | 3,886 | 3,115 |
| 150 | 2,640 | 2,242 | 9,430 | 8,133 |
| 200 | 4,028 | 3,408 | 9,130 | 8,308 |
| 250 | 7,017 | 5,989 | 19,435 | 17,314 |
| 300 | 1,084 | 918 | 2,959 | 2,959 |

Interpretation:

- The conservative model is no longer just structurally feasible; it has a
  measurable, reproducible quality-improvement path that does not require
  actual tier geometry.
- The `prob_37` follow-up is a useful caveat: its best-known row improves the
  official objective slightly while increasing obj1 from 8,493 to 8,497. The
  dedupe criterion is therefore official objective, not obj1 alone.
- The focused x9/cap30 probes are the strongest new signal: 37/40 visible
  instances now select x9/cap30 best-known rows, all without conflict
  truncation. That means conservative option diversity was still a real
  bottleneck, not only the schedule optimizer.
- The improvement is modest, so this does not close the performance goal by
  itself. It does, however, confirm that the next bottleneck is still
  conservative schedule/option search quality, not missing actual geometry.
- Actual geometry relaxation should remain postponed until the conservative
  scheduler and bay allocator are stronger.

Focused x9/cap30 hard-case probes:

| Instance | Previous best obj1/objective | x9/cap30 obj1/objective | Conflict pairs | Truncated |
| --- | ---: | ---: | ---: | --- |
| prob_27 | 9,430 / 127,826,346 | 8,133 / 110,494,163 | 1,665,473 | false |
| prob_31 | 8,968 / 122,599,057 | 8,308 / 113,788,954 | 2,007,792 | false |
| prob_32 | 5,722 / 20,380,736 | 4,685 / 16,639,485 | 2,149,104 | false |
| prob_33 | 7,905 / 53,892,105 | 7,472 / 50,762,394 | 1,957,799 | false |
| prob_34 | 6,997 / 24,750,304 | 5,575 / 19,920,731 | 1,684,622 | false |
| prob_35 | 4,761 / 64,475,183 | 3,635 / 49,535,280 | 2,373,339 | false |
| prob_13 | 1,258 / 24,369,590 | 356 / 7,492,427 | 2,583,004 | false |
| prob_14 | 1,306 / 24,102,927 | 973 / 18,085,709 | 2,901,364 | false |
| prob_18 | 1,187 / 17,246,547 | 634 / 9,477,857 | 4,173,404 | false |
| prob_19 | 141 / 2,320,834 | 58 / 1,099,308 | 4,164,825 | false |
| prob_20 | 2,959 / 79,879,511 | 3,112 / 84,308,702 | 3,069,005 | false |
| prob_21 | 1,539 / 21,244,067 | 1,312 / 18,163,936 | 492,106 | false |
| prob_22 | 638 / 10,122,451 | 467 / 7,372,852 | 583,889 | false |
| prob_23 | 2,733 / 37,680,352 | 2,226 / 30,583,454 | 772,818 | false |
| prob_24 | 1,056 / 14,782,323 | 986 / 13,974,578 | 537,753 | false |
| prob_25 | 3,886 / 2,662,833 | 3,115 / 2,127,647 | 828,641 | false |
| prob_26 | 4,120 / 56,029,696 | 3,877 / 52,617,783 | 1,113,282 | false |
| prob_28 | 3,667 / 50,668,297 | 2,894 / 40,272,992 | 1,127,656 | false |
| prob_30 | 4,021 / 54,368,801 | 3,277 / 44,396,557 | 1,690,304 | false |
| prob_1 | 360 / 10,865,261 | 291 / 8,803,660 | 750,961 | false |
| prob_2 | 10 / 480,800 | 6 / 329,746 | 449,210 | false |
| prob_3 | 18 / 1,148,156 | 7 / 452,659 | 558,106 | false |
| prob_4 | 164 / 4,286,479 | 183 / 4,502,921 | 717,398 | false |
| prob_5 | 47 / 1,125,290 | 41 / 952,244 | 1,285,741 | false |
| prob_6 | 651 / 19,996,698 | 388 / 12,363,044 | 1,189,998 | false |
| prob_7 | 33 / 957,788 | 22 / 752,345 | 1,381,948 | false |
| prob_8 | 1 / 65,736 | 0 / 66,896 | 1,483,326 | false |
| prob_9 | 59 / 1,324,412 | 46 / 1,053,398 | 2,052,526 | false |
| prob_10 | 46 / 1,119,191 | 15 / 507,016 | 1,946,863 | false |
| prob_11 | 708 / 17,180,837 | 446 / 10,763,336 | 1,803,750 | false |
| prob_12 | 831 / 19,259,959 | 487 / 11,469,952 | 1,896,956 | false |
| prob_15 | 172 / 3,373,198 | 79 / 1,746,480 | 2,707,010 | false |
| prob_16 | 39 / 753,205 | 11 / 452,930 | 2,999,127 | false |
| prob_17 | 47 / 985,302 | 19 / 606,946 | 4,183,784 | false |
| prob_36 | 7,369 / 5,077,273 | 5,559 / 3,877,918 | 2,726,537 | false |
| prob_37 | 8,497 / 29,699,489 | 8,325 / 29,194,945 | 3,090,210 | false |
| prob_38 | 19,333 / 260,841,423 | 17,314 / 233,422,670 | 3,238,253 | false |
| prob_39 | 7,482 / 101,407,786 | 6,480 / 88,002,858 | 2,868,312 | false |
| prob_40 | 17,306 / 11,719,652 | 14,803 / 10,022,897 | 2,925,407 | false |

This strongly changes the diagnosis for the remaining conservative obj1 cases.
Repeated neighborhood repair helped, but richer x-anchor and option cap settings
helped much more across nearly the full visible set. `prob_20`, `prob_4`, and
`prob_8` are the only visible cases where x7/cap24 remains the best official
candidate, so the practical conservative policy is now x9/cap30 by default with
x7/cap24 retained as a fallback candidate. This is still entirely within the
conservative footprint model; actual tier-geometry relaxation remains a later
phase.
