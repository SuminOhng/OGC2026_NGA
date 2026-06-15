# Fast Feasibility Oracle Plan

This note is for future AI agents working on the OGC 2026 solver. The current
pain point is that official `utils.check_feasibility(prob_info, solution)` is
too expensive to call for every candidate. The goal is not to replace the
official checker. The goal is to call it much less often.

## Core Principle

Use the organizer checker as the final authority, but put a fast internal
oracle in front of it:

```text
cheap filters -> local exact pair checks -> proxy objective -> occasional official check
```

This matches the usual pattern in irregular packing, nesting, and collision
detection literature:

- Broad phase: reject impossible pairs with time, bay, layer, and AABB filters.
- Narrow phase: call expensive polygon/crane checks only for surviving pairs.
- Cache: reuse pairwise geometry decisions inside one solve.
- Candidate reduction: search only contact/corner/integer anchor points.
- Official validation: keep `check_feasibility` for accepted, best, and final
  candidates.

Useful references for this direction:

- Bennell and Oliveira, "The geometry of nesting problems: A tutorial",
  European Journal of Operational Research, 2008.
- Burke et al., "A new bottom-left-fill heuristic algorithm for the
  two-dimensional irregular packing problem", Operations Research, 2006.
- Guttman, "R-Trees: A Dynamic Index Structure for Spatial Searching",
  SIGMOD, 1984.
- Gottschalk et al., "OBBTree: A Hierarchical Structure for Rapid Interference
  Detection", SIGGRAPH, 1996.
- Wascher et al., "An improved typology of cutting and packing problems",
  European Journal of Operational Research, 2007.

The practical message is: filter hard, refine rarely.

## Current Code Situation

Main files:

- `ogc_solver/ogc_solver/alns/engine.py`
  - owns most search logic.
  - already has `_COLLISION_CACHE`.
  - already has `_cached_pair_collision()`.
  - already has `_candidate_result_with_periodic_full_check()`.
  - already has `_local_candidate_result()` and `_local_feasibility_check()`.
  - already has `_candidate_positions()`.
  - still calls official `check_feasibility` in several polish loops.
- `ogc_solver/ogc_solver/state.py`
  - has `orientation_bbox()`, `fits_in_bay()`, and `Placement`.
  - good home for small geometry helpers, but not for utils-dependent logic.
- `ogc_solver/ogc_solver/solver.py`
  - final safety comparison via `_better_checked_solution()`.
- `scripts/run_solver.py` and `scripts/batch_eval.py`
  - local evaluation entrypoints.

Existing good pieces to keep:

- `_cached_pair_collision(bay, block_a, block_b)`
- `_block_geometry_key(block)`
- `_safe_slots()`
- `_blocks_earlier_due_exits()`
- `_candidate_result_with_periodic_full_check()`
- `_objective_result_from_assignments()`

The next implementation should extend these instead of replacing the solver.

## Target Architecture

Add a submitted module:

```text
ogc_solver/ogc_solver/fast_feasibility.py
```

This module should provide a small class:

```python
class FastFeasibilityOracle:
    def __init__(self, prob_info: dict):
        ...

    def candidate_result(
        self,
        incumbent_assignments: dict[int, dict],
        candidate_assignments: dict[int, dict],
        changed_ids: set[int],
    ) -> dict:
        ...

    def local_feasible(
        self,
        incumbent_assignments: dict[int, dict],
        candidate_assignments: dict[int, dict],
        changed_ids: set[int],
    ) -> bool:
        ...
```

Do not import this from outside the submission root. It must live under
`ogc_solver/ogc_solver/`.

## Implementation Details

### 1. Precompute Shape Bounds

In `fast_feasibility.py`, precompute orientation/layer bounding boxes once.
This avoids repeated `Block(...).bounding_rect()` and repeated layer scanning.

Suggested structures:

```python
orientation_bounds[(block_id, orient_idx)] = (min_x, min_y, max_x, max_y)
layer_bounds[(block_id, orient_idx)] = [
    (min_x, min_y, max_x, max_y),
    ...
]
```

Use existing `state.resolve_layers()` and `state.orientation_bbox()` where
possible. Keep this code independent from local-only files.

### 2. Build Active Rows By Bay

For a candidate with only a few changed blocks, only affected bays need to be
checked.

Affected bays:

```text
old bay of each changed block
new bay of each changed block
```

For each affected bay, build rows:

```python
(block_id, assignment, block_object, placed_bbox, layer_bboxes)
```

Only create `utils.Block` objects for rows in affected bays. Later this can be
optimized further, but this alone keeps the change safe.

### 3. Cheap Filters Before Exact Checks

For every candidate pair in an affected bay:

1. skip if their time intervals do not overlap:

```text
a.entry < b.exit and b.entry < a.exit
```

2. skip if union orientation AABBs do not overlap after translation.
3. skip if no same-level layer AABBs overlap.
4. only then call `_cached_pair_collision()`.

This should become a helper:

```python
def maybe_pair_collision(self, bay_id, row_a, row_b) -> bool:
    if not intervals_overlap(...):
        return False
    if not bbox_overlap(...):
        return False
    if not any(layer_bbox_overlap(...)):
        return False
    return self.cached_pair_collision(...)
```

Important: AABB filters may reject only when they prove non-overlap. They must
never approve a pair as feasible if exact geometry is still needed.

### 4. Crane Checks Need Similar Filtering

Current code often calls `check_entry()` and `check_exit()` with all blocks
present at that time. Before doing that, reduce the present list:

- same bay only,
- present at the operation time,
- layer/AABB can vertically interfere with the moving block.

Add helpers:

```python
def relevant_for_entry(self, moving_row, present_rows) -> list:
    ...

def relevant_for_exit(self, moving_row, present_rows) -> list:
    ...
```

Then call:

```python
check_entry(bay, relevant_blocks, moving_block, fast=True)
check_exit(bay, [moving_block, *relevant_blocks], moving_block, fast=True)
```

Keep the exact semantics conservative. If unsure whether a row is relevant,
include it.

### 5. Replace Engine Local Check With Oracle

In `ogc_solver/ogc_solver/alns/engine.py`:

1. Import the oracle near other local imports:

```python
from ..fast_feasibility import FastFeasibilityOracle
```

2. In `solve_alns()`, create it after `_COLLISION_CACHE.clear()`:

```python
oracle = FastFeasibilityOracle(prob_info)
```

3. Change `_candidate_result_with_periodic_full_check()` signature:

```python
def _candidate_result_with_periodic_full_check(
    prob_info,
    incumbent_assignments,
    candidate_solution,
    changed_ids,
    iteration,
    check_feasibility,
    oracle,
):
```

4. Inside it, keep periodic official checks:

```python
if _FULL_CHECK_INTERVAL > 0 and iteration % _FULL_CHECK_INTERVAL == 0:
    return check_feasibility(prob_info, candidate_solution), True
candidate_assignments = _assignments_from_solution(candidate_solution)
return oracle.candidate_result(
    incumbent_assignments,
    candidate_assignments,
    set(changed_ids),
), False
```

5. Delete or leave unused `_local_candidate_result()` only after the oracle is
stable. During the first implementation, keep the old function for fallback.

### 6. Use Oracle In Polish Loops

Official checks in final polish are expensive. Do not remove all of them at
once. Convert only the high-volume loops first:

- `_objective_safe_preference_polish()`
- `_objective_safe_preference_swap_polish()`
- `_objective_safe_fixed_schedule_preference_swap_polish()`
- `_objective_safe_cluster_left_shift_polish()`

Pattern:

```python
candidate_assignments = ...
fast_result = oracle.candidate_result(
    assignments,
    candidate_assignments,
    changed_ids={...},
)
if not objective_safe_better(current_result, fast_result):
    continue

if official_budget_gate(...):
    candidate_result = check_feasibility(prob_info, candidate)
```

Keep official checks for the best proxy candidate before accepting it into
`current` or `best`.

### 7. Add Telemetry

The code already traces ALNS events when `OGC_ALNS_TRACE` is set. Add counters:

```text
oracle_calls
oracle_rejects_time
oracle_rejects_bbox
oracle_rejects_layer_bbox
oracle_exact_collision_calls
oracle_collision_cache_hits
official_checks
official_mismatches
```

For safety, periodically compare oracle result with official result:

```text
if oracle says feasible but official says infeasible:
    count mismatch
    print stage and changed_ids
    do not accept the candidate
```

The first goal is not speed alone. The first goal is zero unsafe accepts.

## Candidate Reduction Upgrade

Current `_candidate_positions()` uses lower-left, center, and x/y anchors from
existing block bounding rectangles. Extend it carefully:

1. Add right-contact and top-contact anchors:

```text
x = other.left - candidate.max_x
y = other.bottom - candidate.max_y
```

2. Add small integer offsets around contact points:

```text
-1, 0, +1
```

3. Sort positions by cheap score before truncating:

```text
tardiness proxy, y/top height, x, y
```

4. Keep `_CANDIDATE_POSITION_LIMIT` and deadline guards.

Do not add full grid search unless a specific experiment proves it helps.

## Safety Rules

- Never remove final official validation in `solver._better_checked_solution()`.
- Never accept a candidate into `best` based only on an oracle result until the
  oracle has been mismatch-tested.
- Any cheap filter must be one-sided:
  - allowed: "definitely impossible, reject"
  - not allowed: "probably feasible, accept"
- Preserve integer output values.
- Preserve same-day operation order: EXIT before ENTRY.
- Preserve `algorithm(prob_info, timelimit=60)`.
- Keep all submitted dependencies inside `ogc_solver/`.

## Suggested Implementation Order

1. Create `fast_feasibility.py` with precomputed bounds and pair AABB filters.
2. Wire it only into `_candidate_result_with_periodic_full_check()`.
3. Run `prob_1` and one large instance with `OGC_ALNS_TRACE=1`.
4. Add mismatch sampling: every N local-feasible candidates, run official check.
5. If zero mismatches, use the oracle as a prefilter in preference polish loops.
6. Extend `_candidate_positions()` with contact offsets.
7. Only after that, consider heavier ideas such as no-fit polygons or raster
   bitsets.

## Validation Commands

Use the repository-local environment:

```powershell
.\.codex_workspace\.venv\Scripts\python.exe -B scripts\run_solver.py train\prob_1.json --timelimit 5
.\.codex_workspace\.venv\Scripts\python.exe -B scripts\run_solver.py train\prob_20.json --timelimit 20
.\.codex_workspace\.venv\Scripts\python.exe -B scripts\batch_eval.py --timelimit 5
```

For tracing:

```powershell
$env:OGC_ALNS_TRACE='1'
.\.codex_workspace\.venv\Scripts\python.exe -B scripts\run_solver.py train\prob_20.json --timelimit 20
```

Track at least:

- elapsed time,
- objective,
- obj1,
- official check count,
- oracle call count,
- oracle/official mismatches.

## Non-Goals For First Pass

- Do not implement full NFP preprocessing immediately.
- Do not replace `utils.check_entry()` or `utils.check_exit()` semantics.
- Do not remove official checks from final acceptance.
- Do not tune objective weights to hide tardiness.
- Do not hard-code training instances or known local solutions.

## Expected Benefit

The current solver already contains the right high-level ALNS machinery. The
next speedup should come from reducing repeated exact geometry work:

```text
fewer official full checks
fewer pairwise polygon checks
fewer full present-block crane checks
more ALNS iterations per second
more time left for obj1 polish
```

If implemented conservatively, the oracle should improve runtime without
changing the competition contract.
