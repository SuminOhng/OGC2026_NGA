# Blocker-Aware Placement Order Plan

This note records the proposed block insertion order for future C++
implementation. It is a placement-construction rule, not implemented code.

The intended implementation target is:

```text
ogc_solver/ogc_solver/cpp_accel/fast_solver.cpp
```

Relevant current functions:

```text
build_order(...)
construct_solution(...)
construct_release_batch_seed(...)
due_order_sequence(...)
release_due_sequence(...)
repair_sequence(...)
repair_sequence_obj1(...)
```

## Motivation

Most existing construction and repair sequences are early-due or early-release
first. That can place urgent blocks before the long-stay structure around them
is known.

The proposed direction is the opposite for placement construction:

```text
place blocks that can leave later first
place early-exit blocks later, after the bay structure is known
```

The intent is to reserve cleaner access paths for early-due and low-slack
blocks instead of burying them behind late-exit blocks.

## Proposed Sort Key

For each block `i`, define:

```text
targetExit_i = max(release_i + processing_i, due_i)
footprint_i = max orientation bounding-box area
```

The proposed placement order is descending:

```text
1. larger targetExit_i first
2. larger due date first
3. larger processing time first
4. larger footprint first
5. smaller block_id first as deterministic tie-break
```

In comparator form:

```text
if targetExit_a != targetExit_b:
    targetExit_a > targetExit_b
else if due_a != due_b:
    due_a > due_b
else if processing_a != processing_b:
    processing_a > processing_b
else if footprint_a != footprint_b:
    footprint_a > footprint_b
else:
    a < b
```

## Difference From Current C++ Order

The current C++ `build_order(...)` mainly uses early-first variants:

```text
due ascending
release ascending
slack ascending
processing descending then due ascending
footprint descending then due ascending
latest entry ascending
```

The large-instance release batch seed currently uses:

```text
release ascending
due ascending
footprint descending
```

The proposed order should be added as a new explicit mode first, not silently
replace every existing sequence. This makes A/B testing possible.

## Intended Use

Use the order for:

```text
initial construction candidate
large release-batch alternative
ALNS repair sequence alternative
blocker-chain repair sequence alternative
```

Do not use it as the only sequence until it is tested. It should compete
against the existing early-first orders in the seed pool.

## Expected Benefit

If the hypothesis is correct, the resulting layout should have fewer bad
blocker edges of the form:

```text
late-exit block -> early-exit block
```

That should reduce:

```text
early block exit blocking
long tardiness chains
same-bay late block interference with urgent blocks
```

The strongest signal should be improvement in:

```text
obj1
number of tardy blocks
largest tardiness
blocker-chain length
official feasible objective
```

## Failure Modes

This order can fail if late-exit large blocks consume too much good space before
urgent blocks are placed.

Watch for:

```text
more empty-bay fallback use
more collision/entry/exit infeasibility during construction
worse obj1 on small/tight instances
better Z3 but worse obj1, which is usually unacceptable
```

The placement score must still penalize blocking early-due blocks. The order is
only a construction prior; it is not a substitute for blocker-aware placement
scoring.

## Minimum Test

Add the order as a separate C++ `order_mode`.

Run visible A/B checks against the current best C++ path:

```text
prob_1
prob_20
prob_38
prob_39
prob_40
```

Track:

```text
runtime
official feasibility
objective
obj1
number of tardy blocks
largest tardiness
```

Keep the mode if it improves or gives useful diverse candidates. Remove or
disable it if it consistently consumes time without producing a competitive
candidate.
