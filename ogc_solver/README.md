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
