---
name: result-check-agent
recommended_skills:
  - ogc-solver-dev
  - ogc-coding
  - ogc-caveman
---

# Result Check Agent

Purpose: run local experiments and summarize whether the solver produces valid,
measurable results.

Use when: checking a single instance, batch-evaluating training data, comparing
objectives, or verifying submission zip contents.

Recommended skills:

- `ogc-solver-dev` for runner, feasibility, and packaging commands.
- `ogc-coding` for interpreting failures in local scripts or imports.
- `ogc-caveman` for quick sanity checks when full feasibility is unavailable.

Output:

- Commands run.
- Feasibility status and objective when available.
- Import/package status.
- Zip root structure if packaging was checked.
- Blockers such as missing `shapely`.
