# OGC Solver Submission Root

This directory is shaped like the root of the final submission archive.

Submission zip layout:

```text
submission.zip
+-- myalgorithm.py
+-- ogc_solver/
+-- config/
```

The competition evaluator imports `myalgorithm.py` from the decompressed root
and calls:

```python
algorithm(prob_info, timelimit=60)
```

Keep `myalgorithm.py` at this directory root. Put implementation code inside
the `ogc_solver` package so the solver can grow without changing the required
submission entrypoint.

Use `scripts/make_submission.py` from the repository root to create a zip that
contains only this directory's contents.

## Solver Contract

Do not add dependencies on files outside this directory. Hidden evaluation
will decompress only the submitted archive and call `algorithm(prob_info,
timelimit)`. The solver may use bundled Python modules inside this submission
root, but not local caches, experiment outputs, or training-only files.

The current implementation strategy is:

1. build a feasible fallback,
2. use long-budget large-instance paths when the instance and timelimit justify
   them,
3. reduce `Z1` before spending time on secondary terms,
4. use ALNS and local feasibility checks for candidate repair,
5. use bay-assignment MIP only as a candidate generator unless the downstream
   placement and official feasibility checks agree.

`Z3` recovery is deliberately conservative. Moving a block to a preferred bay
can recreate exit-path conflicts and make `Z1` worse. Any preference-improving
move must pass placement repair and objective-safe acceptance.

## Contents

- `myalgorithm.py`: required evaluator entrypoint.
- `ogc_solver/`: implementation package.
- `config/`: submitted configuration files.
