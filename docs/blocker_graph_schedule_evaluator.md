# Blocker-Graph Schedule Evaluator Plan

This note records the agreed scheduling direction before implementation. It is
not implemented code. The intended implementation target is the C++ solver in
`ogc_solver/ogc_solver/cpp_accel/fast_solver.cpp`.

## Purpose

For a fixed layout and fixed entry times, scheduling should be evaluated by a
deterministic blocker graph, not by an EDD proxy and not by a Python fallback.

The ALNS should search over:

```text
bay assignment
integer x/y position
orientation
entry time E_i
```

The schedule evaluator should then compute:

```text
exit time X_i
same-day EXIT order
obj1 total tardiness
infeasibility caused by blocker cycles
```

## Fixed Inputs

The evaluator assumes each block already has:

```text
bay_id
x
y
orient_idx
entry_time E_i
```

The problem data provides:

```text
release date R_i
processing time P_i
due date D_i
bay geometry
block geometry
```

The evaluator must preserve the basic invariant:

```text
E_i >= R_i
C_i = E_i + P_i
X_i >= C_i
```

## Blocker Graph Definition

Create a directed graph on blocks.

```text
edge k -> i
```

means:

```text
block i cannot EXIT before block k exits
```

Equivalently, `k` is a predecessor of `i`:

```text
k in Pred(i)
```

The edge should come from the exact OGC exit-access rule for the fixed layout.
The current assumption is that the blocker criterion is explicit enough that we
do not need a deliberately over-conservative approximation.

## Schedule Computation

For each block:

```text
C_i = E_i + P_i
```

Then build the blocker precedence graph.

If the graph has a cycle, the fixed layout plus entry-time assignment is
infeasible for this evaluator:

```text
A -> B -> C -> A
```

means:

```text
A before B
B before C
C before A
```

No same-day ordering can satisfy that.

If the graph is acyclic, compute a topological order. Then:

```text
for i in topological order:
    X_i = max(C_i, max X_k for k in Pred(i))
```

If `Pred(i)` is empty:

```text
X_i = C_i
```

The obj1 contribution is:

```text
T_i = max(0, X_i - D_i)
obj1 = sum_i T_i
```

## Same-Day Operations

Multiple EXIT operations may share the same integer time. They are not treated
as physically simultaneous; they are an ordered list at the same time key.

For every time `t`:

```text
all EXIT operations at t come before ENTRY operations at t
EXIT order at t follows the blocker topological order restricted to blocks with X_i = t
ENTRY order at t should remain deterministic and feasibility-preserving
```

The important rule is:

```text
if k -> i and X_k == X_i, then EXIT(k) must appear before EXIT(i)
```

## C++ Implementation Target

The expected C++ pieces are:

```text
build_blocker_graph(...)
detect_cycle_or_topological_order(...)
compute_exit_times_from_precedence(...)
build_operations_with_same_day_exit_order(...)
evaluate_obj1_from_exit_times(...)
```

These should live in the C++ runtime path, not in Python. Python should remain
only the entrypoint, serializer/parser, and optional final validation bridge.

The existing Python scheduling helpers under
`ogc_solver/ogc_solver/subsolvers/` and `scripts/` are analysis references, not
the submitted runtime direction.

## ALNS Consequences

With this evaluator, ALNS should not try to make a good schedule by only sorting
EXIT operations with EDD. Instead, ALNS should improve the blocker graph itself.

High-value destroy targets:

```text
blocks with large tardiness
blocks with many outgoing blocker edges
blocks that sit early in long blocker chains
blocks that block early-due victims
blocks involved in blocker cycles
```

High-value repair actions:

```text
move target block to another bay
change orientation
change integer position
adjust entry time E_i
repair blocker chain around the target
```

The evaluator gives a clear reason for tardiness:

```text
late because E_i is late
late because P_i makes C_i late
late because predecessor exits force X_i later
```

## Minimum Smoke Tests

Before using this evaluator inside ALNS, test these cases.

1. No blockers

```text
Pred(i) = empty for all i
expected X_i = E_i + P_i
```

2. Simple chain

```text
A -> B -> C
expected X_B >= X_A
expected X_C >= X_B
```

3. Same-day blocker order

```text
A -> B
X_A == X_B
expected same-day EXIT order: A before B
```

4. Cycle

```text
A -> B
B -> A
expected infeasible
```

5. Release and processing invariant

```text
E_i >= R_i
X_i >= E_i + P_i
```

6. Official checker comparison

For a few visible instances, emit operations from the evaluator and confirm the
official checker accepts the solution. The evaluator may be fast, but the
official checker remains the final authority.

## Non-Goals

Do not implement an LP/MIP scheduler as the submitted runtime path for this
step.

Do not reintroduce Python ALNS fallback when C++ fails.

Do not treat EDD as the primary schedule evaluator. EDD may remain a seed or
diagnostic idea, but the main schedule evaluation should be blocker-graph based.

Do not accept a fast but infeasible candidate. A feasible baseline is better
than an aggressive candidate that fails the official checker.
