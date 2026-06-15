# Local Scripts

Scripts in this folder support local development and packaging. They are not
part of the submitted solver unless the packaging script explicitly includes
them.

## Common Commands

Run one instance:

```powershell
.\.codex_workspace\ogc2026_env\Scripts\python.exe -B scripts\run_solver.py train\prob_1.json --timelimit 300
```

Run one instance and save the returned solution:

```powershell
.\.codex_workspace\ogc2026_env\Scripts\python.exe -B scripts\run_solver.py train\prob_20.json --timelimit 3000 --save-solution .codex_workspace\results\prob20_solution.json
```

Build a submission archive:

```powershell
.\.codex_workspace\ogc2026_env\Scripts\python.exe -B scripts\make_submission.py
```

## Notes

Use local runs to compare visible training behavior, but keep conclusions
general. Hidden instances are the real target. A saved solution for `prob_20`
is a useful diagnostic artifact, not a strategy for submission.
