# Training Instances

This folder contains visible problem instances used for local experiments.
Files `prob_21.json` through `prob_40.json` may be untracked if they were added
as extra local training data.

Training scores are diagnostics only. The final solver is evaluated on hidden
instances, so solver code must not depend on:

- instance names,
- exact block counts,
- known objective values,
- cached solutions,
- specific block IDs from visible problems.

When reporting results, include the timelimit, feasibility status, objective,
`Z1`, `Z2`, and `Z3`. For long-horizon work, also track how many ALNS
iterations were actually executed, because expensive seed or polish phases can
consume the search budget.
