# Local Scripts

Scripts in this folder support local development and packaging. They are not
part of the submitted solver unless the packaging script explicitly includes
them.

## Common Commands

In this workspace, the checked OGC evaluation environment is:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe
```

It was created from the `ogc2026/ogc2026_env.yml` requirement shape using a
local Windows Python 3.12 venv, with `shapely>=2.1.0` installed for the
official feasibility checker. A full conda environment may still be preferable
for GUI/tester use, but solver scoring does not need the heavyweight notebook,
PyQt, Torch, TensorFlow, or commercial solver packages.

Run one instance:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\run_solver.py train\prob_1.json --timelimit 300
```

Build the optional C++ accelerator for local smoke testing on Windows:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\build_cpp_accelerator.py
$env:OGC_CPP_ACCEL_PATH = ".codex_workspace\cpp\ogc_fast_solver.exe"
```

For a C++-enabled competition submission, build on Ubuntu 24.04 so the bundled
binary is compatible with the evaluation server:

```bash
python scripts/build_cpp_accelerator.py --submission
python scripts/make_submission.py
```

Run one instance and save the returned solution:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\run_solver.py train\prob_20.json --timelimit 3000 --save-solution .codex_workspace\results\prob20_solution.json
```

Run selected training instances and report objective components plus a
conservative lower bound/gap:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\batch_eval.py --timelimit 60 --instances prob_1 prob_25 prob_20 --output .codex_workspace\results\batch_eval.jsonl
```

For stronger visible gap reporting on 3+ bay instances, add an exact relaxed
CP-SAT lower bound for the weighted secondary assignment objective:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\batch_eval.py --bounds-only --instances prob_2 prob_26 --secondary-cp-time-limit 5 --secondary-cp-workers 8
```

Score an already saved solution with the same lower-bound stack:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\score_solution.py train\prob_26.json .codex_workspace\results\prob26_current_130s.solution.json --secondary-cp-time-limit 5 --output .codex_workspace\results\prob26_current_130s_score.json
```

Compare two `batch_eval.py` JSONL outputs by instance:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\compare_batch_results.py .codex_workspace\results\baseline.jsonl .codex_workspace\results\candidate.jsonl --output .codex_workspace\results\comparison.jsonl
```

The lower bound is deliberately optimistic. It currently combines mandatory
release/processing tardiness, continuous and energetic bay-area/time
relaxations, exact `Z2+Z3` assignment relaxation for two-bay instances, and an
independent best-bay `Z3` bound for larger bay counts.

Compute only the conservative lower bounds without importing the official
checker:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\batch_eval.py --bounds-only --instances prob_1 prob_25 prob_20
```

Diagnose whether tardiness is associated with exit-path blockers at each
block's ideal due/release-based exit time:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\diagnose_tardiness.py train\prob_1.json --timelimit 5 --output .codex_workspace\results\prob1_tardiness_diag.json
```

Summarize one or more diagnostic JSON files and classify whether they look like
good candidates for tail obj1 repair. The output includes both the geometry-
based diagnostic trigger, a Shapely-free SAT approximation of the ideal-blocked
trigger, and a cheap `obj1`/tardy-block proxy:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\summarize_tardiness_diagnostics.py .codex_workspace\results\prob26_tardiness_diag_130s.json .codex_workspace\results\prob29_tardiness_diag_130s.json --output .codex_workspace\results\tail_obj1_trigger_summary.json
```

For large three-bay tail-repair experiments, prefer
`cheap_ideal_trigger_tail_obj1_repair` over the older
`cheap_trigger_tail_obj1_repair`. The older proxy turned on `prob_28`; the SAT
ideal-block proxy rejected it in saved-solution probes.

Compute an analysis-only relaxed CP-SAT lower bound for `obj1` by replacing
spatial geometry with bay-level cumulative area resources:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\lower_bound_cp_sat.py train\prob_1.json --time-limit 30 --output .codex_workspace\results\prob1_cp_lb.json
```

Compute an analysis-only projection-resource energetic lower bound for `obj1`.
This checks simple 2D packing necessary conditions for very tall or very wide
blocks over due windows:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\lower_bound_projection_cp.py train\prob_26.json --output .codex_workspace\results\prob26_projection_lb.json
```

Compute an analysis-only geometry-aware CP-SAT lower bound. This adds only
pairwise same-bay non-overlap constraints that can be proven by exhaustive
integer placement checks; expensive unknown pairs are left relaxed:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\lower_bound_geometry_cp_sat.py train\prob_25.json --time-limit 30 --max-pair-checks 1000000 --output .codex_workspace\results\prob25_geometry_cp_lb.json
```

Compute an analysis-only pair-relation CP-SAT lower bound. This keeps any
FIFO or nested entry/exit order that is possible for a pair in a bay, but still
relaxes global multi-block coordinate consistency:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\lower_bound_pair_relation_cp_sat.py train\prob_26.json --time-limit 30 --max-pair-checks 200000 --output .codex_workspace\results\prob26_pair_relation_lb.json
```

Compute an analysis-only triplet-packing CP-SAT lower bound. This adds only
certified three-block same-bay incompatibilities and leaves expensive or
uncertified triplets relaxed:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\lower_bound_triplet_packing_cp_sat.py train\prob_26.json --time-limit 30 --max-triplets 2000 --max-triplet-checks 50000 --output .codex_workspace\results\prob26_triplet_lb.json
```

Compute a stronger selected-window shared-placement lower bound. This enumerates
all integer placements for a small selected block subset and keeps one shared
placement choice per block. It can become very large; if conflict enumeration
is incomplete, it reports no bound:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\lower_bound_window_placement_cp_sat.py train\prob_26.json --selection few-options --max-blocks 3 --max-options-per-block 10000 --max-conflict-checks 10000000 --time-limit 30 --output .codex_workspace\results\prob26_window_placement_lb.json
```

Add `--compress-signatures` to group placement options with identical conflict
patterns inside the selected subset. This is exact but may not reduce large
instances much:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\lower_bound_window_placement_cp_sat.py train\prob_26.json --selection few-options --max-blocks 3 --max-options-per-block 10000 --max-conflict-checks 10000000 --compress-signatures --time-limit 30
```

Compute a mandatory time-slice placement certificate. This searches times where
selected blocks must be present if they are on time, then checks whether those
blocks can be placed simultaneously:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\lower_bound_timeslice_placement_cp_sat.py train\prob_26.json --selection few-options --max-times 3 --max-blocks 2 --max-options-per-block 3500 --max-conflict-checks 3000000 --time-limit 3
```

Compute an exact relaxed bay-assignment lower bound for the secondary
`obj2+obj3` contribution. This ignores placement and timing, so it remains a
valid lower bound:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\lower_bound_secondary_cp_sat.py train\prob_28.json --time-limit 5 --output .codex_workspace\results\prob28_secondary_cp_lb.json
```

Optimize only the schedule while keeping the solver's bay, position, and
orientation fixed. This is not a global lower bound, but it tells whether a
solution's remaining tardiness is mostly schedule-side or placement-side:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\fixed_placement_schedule_probe.py train\prob_1.json --timelimit 5 --cp-time-limit 20 --output .codex_workspace\results\prob1_fixed_schedule_probe.json
```

Build a conservative vertical-prism surrogate solution. This ignores detailed
tier geometry during option selection by using orientation bounding boxes, then
checks the emitted solution with the official feasibility checker:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_footprint_probe.py train\prob_1.json --time-limit 20 --grid-step 12 --max-options-per-block 2 --output .codex_workspace\results\prob1_surrogate_opt2.json --save-solution .codex_workspace\results\prob1_surrogate_opt2.solution.json
```

Use lane mode for a more scalable conservative model. It fixes each block to a
left-wall vertical lane under the same bounding-box footprint assumption and
also reports/saves a multi-start greedy conflict-graph schedule if CP-SAT finds
no incumbent. The greedy fallback tries due-date, release, slack, workload, and
conflict-degree-aware orders:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_footprint_probe.py train\prob_1.json --mode lanes --lane-step 4 --max-options-per-block 8 --time-limit 5 --output .codex_workspace\results\prob1_surrogate_lanes8.json --save-solution .codex_workspace\results\prob1_surrogate_lanes8.solution.json
```

Add a fixed-option schedule CP polish to test whether the conservative bay/lane
choices are good but the greedy timing is still weak. For limits above three
seconds, the script tries the hint, a short 3-second CP, and the requested
longer CP, then saves the best official feasible candidate:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_footprint_probe.py train\prob_1.json --mode lanes --lane-step 4 --max-options-per-block 8 --time-limit 1 --greedy-schedule-cp-limit 3 --output .codex_workspace\results\prob1_surrogate_lanes8_schedcp.json --save-solution .codex_workspace\results\prob1_surrogate_lanes8_schedcp.solution.json
```

Allow a small set of tardy blocks to change conservative lane/bay options after
the fixed-option schedule CP. This tests whether the remaining tardiness is due
to option choice rather than timing alone. The script evaluates neighborhood
sizes `4`, `8`, and the requested maximum, then saves the best official
feasible candidate:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_footprint_probe.py train\prob_1.json --mode lanes --lane-step 4 --max-options-per-block 8 --time-limit 1 --greedy-schedule-cp-limit 3 --neighborhood-cp-limit 5 --neighborhood-blocks 8 --output .codex_workspace\results\prob1_surrogate_lanes8_neigh8.json --save-solution .codex_workspace\results\prob1_surrogate_lanes8_neigh8.solution.json
```

Use edge-lane mode to keep the same conservative bounding-box footprint model
while allowing each vertical lane to sit on a small number of coarse x anchors.
The default `--x-anchor-count 3` means left wall, center, and right wall.
Higher values such as `5` add intermediate anchors. `--edge-cap-strategy
balanced` keeps a more even mix of y-lanes and x-anchors when the option cap is
active. This is still not actual tier-geometry relaxation; it only tests whether
coarse x-position structure matters before opening the real shape model:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_footprint_probe.py train\prob_1.json --mode edge_lanes --lane-step 4 --max-options-per-block 12 --time-limit 1 --greedy-schedule-cp-limit 3 --neighborhood-cp-limit 5 --neighborhood-blocks 8 --output .codex_workspace\results\prob1_surrogate_edge_lanes12.json --save-solution .codex_workspace\results\prob1_surrogate_edge_lanes12.solution.json
```

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_footprint_probe.py train\prob_1.json --mode edge_lanes --lane-step 4 --x-anchor-count 5 --edge-cap-strategy balanced --max-options-per-block 18 --time-limit 1 --greedy-schedule-cp-limit 3 --neighborhood-cp-limit 5 --neighborhood-blocks 8 --output .codex_workspace\results\prob1_surrogate_edge_lanes18_x5_balanced.json --save-solution .codex_workspace\results\prob1_surrogate_edge_lanes18_x5_balanced.solution.json
```

For diagnostics, `--neighborhood-objective objective` makes the small
tardy-block neighborhood CP include the same coarse `obj1 + obj2 + obj3`
structure used by the full surrogate CP. `both` tries both the default
tardiness-focused model and the objective-focused model, then saves the best
official feasible candidate:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_footprint_probe.py train\prob_2.json --mode edge_lanes --lane-step 8 --x-anchor-count 5 --edge-cap-strategy balanced --max-options-per-block 18 --time-limit 1 --greedy-schedule-cp-limit 3 --neighborhood-cp-limit 5 --neighborhood-blocks 8 --neighborhood-objective both --output .codex_workspace\results\prob2_surrogate_edge_lanes18_x5_balanced_both.json
```

Run the same conservative surrogate setting over a visible-instance matrix and
write compact JSONL output:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_batch.py --instances prob_1 prob_2 prob_3 prob_4 prob_21 prob_22 prob_23 prob_24 prob_25 prob_26 prob_27 prob_28 prob_29 prob_30 --max-blocks 100 --mode edge_lanes --lane-step 8 --x-anchor-count 5 --edge-cap-strategy balanced --max-options-per-block 18 --time-limit 1 --greedy-schedule-cp-limit 3 --neighborhood-cp-limit 5 --neighborhood-blocks 8 --output .codex_workspace\results\surrogate_visible_100_balanced_conflict_modes.jsonl
```

For full 150-block visible instances, raise the conflict-pair cap:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_batch.py --instances prob_5 prob_6 prob_7 prob_8 prob_26 prob_27 prob_28 prob_29 prob_30 --mode edge_lanes --lane-step 8 --x-anchor-count 5 --edge-cap-strategy balanced --max-options-per-block 18 --max-conflict-pairs 1500000 --time-limit 1 --greedy-schedule-cp-limit 3 --neighborhood-cp-limit 5 --neighborhood-blocks 8 --output .codex_workspace\results\surrogate_visible_150_full_balanced_conflict_modes.jsonl
```

For tighter 100/150-block probes, use more x anchors and a larger option cap.
The greedy multi-start also includes due-spread score variants that softly
discourage assigning close-due blocks to crowded/conflicting positions in the
same bay:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_footprint_probe.py train\prob_27.json --mode edge_lanes --lane-step 8 --x-anchor-count 7 --edge-cap-strategy balanced --max-options-per-block 24 --max-conflict-pairs 1500000 --time-limit 1 --greedy-schedule-cp-limit 3 --neighborhood-cp-limit 5 --neighborhood-blocks 8 --output .codex_workspace\results\prob27_full_surrogate_lanestep8_x7_cap24_duespread.json
```

Run the same richer two-bay setting as a matrix:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_batch.py --instances prob_1 prob_4 prob_22 prob_23 prob_25 prob_8 prob_27 prob_30 --mode edge_lanes --lane-step 8 --x-anchor-count 7 --edge-cap-strategy balanced --max-options-per-block 24 --max-conflict-pairs 1500000 --time-limit 1 --greedy-schedule-cp-limit 3 --neighborhood-cp-limit 5 --neighborhood-blocks 8 --output .codex_workspace\results\surrogate_2bay_x7_cap24_duespread_matrix.jsonl
```

Run the same richer setting on representative three-bay 100/150-block cases:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_batch.py --instances prob_2 prob_3 prob_21 prob_24 prob_5 prob_6 prob_7 prob_26 prob_28 prob_29 --mode edge_lanes --lane-step 8 --x-anchor-count 7 --edge-cap-strategy balanced --max-options-per-block 24 --max-conflict-pairs 1500000 --time-limit 1 --greedy-schedule-cp-limit 3 --neighborhood-cp-limit 5 --neighborhood-blocks 8 --output .codex_workspace\results\surrogate_3bay_x7_cap24_duespread_matrix.jsonl
```

For visible 200-block cases, keep x7/cap24 but raise the conflict-pair cap:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_batch.py --instances prob_9 prob_10 prob_11 prob_12 prob_31 prob_32 prob_33 prob_34 prob_35 --mode edge_lanes --lane-step 8 --x-anchor-count 7 --edge-cap-strategy balanced --max-options-per-block 24 --max-conflict-pairs 3000000 --time-limit 1 --greedy-schedule-cp-limit 3 --neighborhood-cp-limit 5 --neighborhood-blocks 8 --output .codex_workspace\results\surrogate_200_x7_cap24.jsonl
```

For visible 250-block cases, keep the same option setting and raise the cap
further:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_batch.py --instances prob_13 prob_14 prob_15 prob_16 prob_36 prob_37 prob_38 prob_39 prob_40 --mode edge_lanes --lane-step 8 --x-anchor-count 7 --edge-cap-strategy balanced --max-options-per-block 24 --max-conflict-pairs 5000000 --time-limit 1 --greedy-schedule-cp-limit 3 --neighborhood-cp-limit 5 --neighborhood-blocks 8 --output .codex_workspace\results\surrogate_250_x7_cap24.jsonl
```

For visible 300-block cases:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_batch.py --instances prob_17 prob_18 prob_19 prob_20 --mode edge_lanes --lane-step 8 --x-anchor-count 7 --edge-cap-strategy balanced --max-options-per-block 24 --max-conflict-pairs 8000000 --time-limit 1 --greedy-schedule-cp-limit 3 --neighborhood-cp-limit 5 --neighborhood-blocks 8 --output .codex_workspace\results\surrogate_300_x7_cap24.jsonl
```

Combine the x7/cap24 visible sweep outputs into one coverage-checked CSV and
Markdown summary:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\summarize_surrogate_results.py --inputs .codex_workspace\results\surrogate_2bay_x7_cap24_duespread_matrix.jsonl .codex_workspace\results\surrogate_3bay_x7_cap24_duespread_matrix.jsonl .codex_workspace\results\surrogate_200_subset_x7_cap24.jsonl .codex_workspace\results\surrogate_200_rest_x7_cap24.jsonl .codex_workspace\results\surrogate_250_subset_x7_cap24.jsonl .codex_workspace\results\surrogate_250_rest_x7_cap24.jsonl .codex_workspace\results\surrogate_300_x7_cap24.jsonl --csv-output .codex_workspace\results\surrogate_visible_x7_cap24_summary.csv --md-output .codex_workspace\results\surrogate_visible_x7_cap24_summary.md --expect-visible-40
```

The tardy-option neighborhood can also test a wider flexible set that includes
current conservative conflict-overlap blockers of the tardiest blocks. Keep the
default `--neighborhood-selection tardy` unless a focused A/B run shows the
blocker variant helps the target instance:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_batch.py --instances prob_31 prob_38 prob_40 --mode edge_lanes --lane-step 8 --x-anchor-count 7 --edge-cap-strategy balanced --max-options-per-block 24 --max-conflict-pairs 5000000 --time-limit 1 --greedy-schedule-cp-limit 3 --neighborhood-cp-limit 12 --neighborhood-blocks 12 --neighborhood-selection both --output .codex_workspace\results\surrogate_hard_selection_both_probe.jsonl
```

For hard conservative-model cases, repeated incumbent-preserving tardy
neighborhoods are a more promising first escalation than blocker selection. The
next round starts from the best official feasible result from the previous
round:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_batch.py --instances prob_31 prob_38 prob_40 --mode edge_lanes --lane-step 8 --x-anchor-count 7 --edge-cap-strategy balanced --max-options-per-block 24 --max-conflict-pairs 5000000 --time-limit 1 --greedy-schedule-cp-limit 3 --neighborhood-cp-limit 12 --neighborhood-blocks 12 --neighborhood-selection tardy --neighborhood-rounds 2 --output .codex_workspace\results\surrogate_hard_tardy12_round2_probe.jsonl
```

For the hardest 250-block cases, a third round and a larger neighborhood CP
budget can matter:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_batch.py --instances prob_38 prob_39 --mode edge_lanes --lane-step 8 --x-anchor-count 7 --edge-cap-strategy balanced --max-options-per-block 24 --max-conflict-pairs 5000000 --time-limit 1 --greedy-schedule-cp-limit 3 --neighborhood-cp-limit 24 --neighborhood-blocks 12 --neighborhood-selection tardy --neighborhood-rounds 3 --output .codex_workspace\results\surrogate_250_hard_tardy12_round3_tl24.jsonl
```

For high-obj1 conservative cases, a richer x-anchor/option-cap setting is
often stronger than only increasing neighborhood rounds:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_batch.py --instances prob_31 prob_37 prob_33 prob_39 prob_36 --mode edge_lanes --lane-step 8 --x-anchor-count 9 --edge-cap-strategy balanced --max-options-per-block 30 --max-conflict-pairs 8000000 --time-limit 1 --greedy-schedule-cp-limit 3 --neighborhood-cp-limit 12 --neighborhood-blocks 12 --neighborhood-selection tardy --neighborhood-rounds 2 --output .codex_workspace\results\surrogate_high_x9_cap30_round2.jsonl
```

The next mid-obj1 batch also benefits from the same richer setting:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_batch.py --instances prob_34 prob_32 prob_35 prob_26 prob_30 prob_25 prob_28 --mode edge_lanes --lane-step 8 --x-anchor-count 9 --edge-cap-strategy balanced --max-options-per-block 30 --max-conflict-pairs 8000000 --time-limit 1 --greedy-schedule-cp-limit 3 --neighborhood-cp-limit 12 --neighborhood-blocks 12 --neighborhood-selection tardy --neighborhood-rounds 2 --output .codex_workspace\results\surrogate_mid_x9_cap30_round2.jsonl
```

For lower-but-still-material obj1 cases, x9/cap30 remains useful for most
instances but can hurt individual cases such as `prob_20`; keep best-objective
dedupe enabled:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_batch.py --instances prob_20 prob_23 prob_29 prob_21 prob_14 prob_13 prob_18 prob_24 --mode edge_lanes --lane-step 8 --x-anchor-count 9 --edge-cap-strategy balanced --max-options-per-block 30 --max-conflict-pairs 8000000 --time-limit 1 --greedy-schedule-cp-limit 3 --neighborhood-cp-limit 12 --neighborhood-blocks 12 --neighborhood-selection tardy --neighborhood-rounds 2 --output .codex_workspace\results\surrogate_lowmid_x9_cap30_round2.jsonl
```

The low-obj1 follow-up shows that x9/cap30 can still improve many small-tardy
cases, but `prob_4` and `prob_8` remain better with the x7/cap24 candidate:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_batch.py --instances prob_12 prob_11 prob_6 prob_22 prob_1 prob_15 prob_4 prob_19 --mode edge_lanes --lane-step 8 --x-anchor-count 9 --edge-cap-strategy balanced --max-options-per-block 30 --max-conflict-pairs 8000000 --time-limit 1 --greedy-schedule-cp-limit 3 --neighborhood-cp-limit 12 --neighborhood-blocks 12 --neighborhood-selection tardy --neighborhood-rounds 2 --output .codex_workspace\results\surrogate_low_x9_cap30_round2.jsonl

.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\surrogate_batch.py --instances prob_9 prob_17 prob_5 prob_10 prob_16 prob_7 prob_3 prob_2 prob_8 --mode edge_lanes --lane-step 8 --x-anchor-count 9 --edge-cap-strategy balanced --max-options-per-block 30 --max-conflict-pairs 8000000 --time-limit 1 --greedy-schedule-cp-limit 3 --neighborhood-cp-limit 12 --neighborhood-blocks 12 --neighborhood-selection tardy --neighborhood-rounds 2 --output .codex_workspace\results\surrogate_verylow_x9_cap30_round2.jsonl
```

Build a best-known visible summary by combining the original all-40 x7/cap24
sweep with repeated-neighborhood follow-up probes. Duplicate instances are
deduplicated by best official objective:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\summarize_surrogate_results.py --inputs .codex_workspace\results\surrogate_2bay_x7_cap24_duespread_matrix.jsonl .codex_workspace\results\surrogate_3bay_x7_cap24_duespread_matrix.jsonl .codex_workspace\results\surrogate_200_subset_x7_cap24.jsonl .codex_workspace\results\surrogate_200_rest_x7_cap24.jsonl .codex_workspace\results\surrogate_250_subset_x7_cap24.jsonl .codex_workspace\results\surrogate_250_rest_x7_cap24.jsonl .codex_workspace\results\surrogate_300_x7_cap24.jsonl .codex_workspace\results\surrogate_hard_tardy12_round2_probe.jsonl .codex_workspace\results\surrogate_200_tail_tardy12_round2.jsonl .codex_workspace\results\surrogate_250_tail_tardy12_round2.jsonl .codex_workspace\results\surrogate_250_hard_tardy12_round3_tl24.jsonl .codex_workspace\results\surrogate_prob40_tardy12_round2_probe.jsonl .codex_workspace\results\surrogate_unrounded_high_tardy12_round2_objboth.jsonl .codex_workspace\results\surrogate_hard_x9_cap30_round2.jsonl .codex_workspace\results\surrogate_high_x9_cap30_round2.jsonl .codex_workspace\results\surrogate_mid_x9_cap30_round2.jsonl .codex_workspace\results\surrogate_lowmid_x9_cap30_round2.jsonl .codex_workspace\results\surrogate_low_x9_cap30_round2.jsonl .codex_workspace\results\surrogate_verylow_x9_cap30_round2.jsonl --dedupe best-objective --csv-output .codex_workspace\results\surrogate_visible_best_known_summary.csv --md-output .codex_workspace\results\surrogate_visible_best_known_summary.md --expect-visible-40
```

Probe the internal due-window chain repair on a saved solution without rerunning
the whole solver:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\due_window_chain_probe.py train\prob_26.json --solution .codex_workspace\results\prob26_current_130s.solution.json --time-limit 20 --output .codex_workspace\results\prob26_due_window_chain_probe.json
```

Disable the submitted solver's optional CP schedule polish for A/B testing.
This turns off both the reserved two-bay polish and the opportunistic small
three-bay polish:

```powershell
$env:OGC_DISABLE_CP_SCHEDULE_POLISH='1'
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\batch_eval.py --timelimit 80 --instances prob_1
Remove-Item Env:\OGC_DISABLE_CP_SCHEDULE_POLISH
```

Force the same polish for analysis on larger two-bay instances without changing
the default submitted gate:

```powershell
$env:OGC_FORCE_CP_SCHEDULE_POLISH='1'
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\batch_eval.py --timelimit 120 --instances prob_27
Remove-Item Env:\OGC_FORCE_CP_SCHEDULE_POLISH
```

Build a submission archive:

```powershell
.\.codex_workspace\ogc2026_win_env\Scripts\python.exe -B scripts\make_submission.py
```

## Notes

Use local runs to compare visible training behavior, but keep conclusions
general. Hidden instances are the real target. A saved solution for `prob_20`
is a useful diagnostic artifact, not a strategy for submission.
