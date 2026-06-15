# Heuristics

This folder contains simpler constructive and local-search helpers.

The useful baseline is intentionally blunt: build a feasible solution first,
then improve it only when the candidate remains feasible and improves the
official objective. A fast infeasible layout is worse than a slower feasible
fallback.

Heuristics here should stay general:

- no visible-instance hard-coding,
- no stored block-specific fixes,
- integer times and coordinates only,
- one `ENTRY` and one `EXIT` per block,
- `EXIT` before `ENTRY` when operations share a time key.

When a heuristic tries to improve `Z3`, it must remember that preferred bay
moves can create exit-path blockers and increase `Z1`.
