# Submission And Validation

## Competition submission contract

- Send one zip file as the submission artifact.
- The decompressed root must contain `myalgorithm.py`.
- `myalgorithm.py` must define `algorithm(prob_info, timelimit=60)`.
- The function returns a solution dictionary with one top-level key:
  `operations`.
- Time keys are strings and operation values are lists.
- At the same time key, `EXIT` operations must appear before `ENTRY`
  operations.
- `ENTRY` operations require integer `block_id`, `bay_id`, `x`, `y`, and
  `orient_idx`.
- `EXIT` operations require integer `block_id` and `bay_id`.
- The evaluation runtime has no external internet access and no access to
  parent directories above the execution folder.
- The submission zip size limit is 15 MB.

## Local validation

Use `scripts/run_solver.py` for one instance:

```powershell
.\.codex_workspace\.venv\Scripts\python.exe -B scripts\run_solver.py train\prob_1.json --timelimit 5
```

Use `scripts/batch_eval.py` for all training instances:

```powershell
.\.codex_workspace\.venv\Scripts\python.exe -B scripts\batch_eval.py --timelimit 5
```

Both scripts import the submitted entrypoint from top-level `ogc_solver/` and
import `check_feasibility` from `baseline/baseline/utils.py`.

If `ModuleNotFoundError: No module named 'shapely'` appears, install Shapely
only into `.codex_workspace/.venv` after receiving approval for network access:

```powershell
.\.codex_workspace\.venv\Scripts\python.exe -m pip install "shapely>=2.1.0"
```

## Packaging

Build a submission zip with:

```powershell
.\.codex_workspace\.venv\Scripts\python.exe -B scripts\make_submission.py
```

Expected archive root:

```text
myalgorithm.py
ogc_solver/
config/
README.md
```
