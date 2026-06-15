# ALNS Search

This folder contains the main adaptive large-neighborhood search engine.

The ALNS operates on complete block assignments:

- bay id,
- integer `(x, y)`,
- orientation,
- entry time,
- exit time.

## Operator Families

The useful operator themes are:

- worst tardiness removal,
- access blocker removal,
- early exit protection removal,
- preference removal,
- random removal,
- regret/insertion repair,
- left-shift and schedule compression polish,
- due-window blocker chain repair,
- zero-tardiness `Z3` recovery.

## Current Invariant

The search may explore risky candidates, but accepted candidates must be
feasible and objective-safe. For late-stage preference recovery, `Z1` should not
regress just to lower `Z3`; the weight on tardiness is usually too large.

## Why Z3 Recovery Is Difficult

`Z3` wants blocks in preferred bays. Low `Z1` wants early-due blocks to have
clear exit paths at the right time. These goals can conflict. A single block
move can force a chain of placement changes because the moved block may block
another block's vertical path. Therefore preference recovery should be local,
chain-aware, and checked by the fast oracle before official validation.
