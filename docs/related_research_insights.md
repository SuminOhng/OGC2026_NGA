# Related Research Insights for OGC Spatial Block Scheduling

Date: 2026-06-15

This note summarizes prior work that is structurally close to the OGC 2026
spatial block scheduling problem. I did not find a public paper that matches
the OGC problem exactly: irregular ship blocks, bay assignment, integer
placement, orientation, entry/exit timing, vertical access-path clearance,
tardiness, bay workload balance, and bay preference penalties all in one model.

The useful literature is therefore indirect. The closest families are:

- integrated container terminal planning,
- container/block relocation and pre-marshalling,
- continuous berth allocation with space-time assignment,
- constraint-satisfaction repair heuristics.

The recurring lesson is consistent with the current solver direction in this
repo: build a feasible incumbent, decompose the large coupled problem, use
small exact/semiexact subproblems only as candidate generators, and accept moves
only after strong feasibility and objective checks.

## OGC Problem Fit

OGC combines several hard decisions:

- choose a bay for every block,
- choose integer `(x, y)` placement and orientation,
- choose `ENTRY` and `EXIT` times,
- avoid same-time spatial overlap,
- preserve crane access paths for entry and exit,
- minimize weighted tardiness, bay workload imbalance, and bay preference
  penalty.

This is closer to an integrated logistics/yard scheduling problem than to a
plain 2D packing problem. The hard coupling is that a spatial decision can
create a future timing failure: a block that fits geometrically may block
another block's exit path and cause tardiness.

## Closest Research Families

### 1. Integrated Container Terminal Operations

Kizilay, Eliiyi, and Van Hentenryck study an integrated port terminal problem
combining quay crane assignment/scheduling, yard location assignment, and
vehicle dispatching. They formulate MIP and CP models and report that the MIP
handles only small instances, while CP scales better on realistic instances.
They also emphasize that terminal subproblems have conflicting objectives and
that solving them separately can hurt system-level performance.

Source:
https://arxiv.org/abs/1712.05302

Relevant transfer to OGC:

- A monolithic MIP for all block bay/position/time decisions is unlikely to be
  competitive for 100-300 blocks under short time limits.
- Use exact models selectively: bay assignment MIP, local scheduling MIP, or
  bounded repair windows.
- Keep the full official objective as the acceptance gate. Improving a local
  subproblem, such as bay preference, can damage global tardiness.
- CP-style feasibility reasoning is attractive for access-path and no-overlap
  constraints, even if the final implementation is a custom checker.

### 2. Relocation and Delay in Container Terminals

Borjian, Manshadi, Barnhart, and Jaillet model crane moves in a storage yard
with service-time concerns, relocation moves, wait time, and retrieval. They
consider repositioning moves during off-peak periods and flexible service
policies such as out-of-order retrieval, showing that flexibility can reduce
both relocation and delay.

Source:
https://arxiv.org/abs/1503.01535

Relevant transfer to OGC:

- A block causing tardiness is often not the late block itself but a blocker in
  its exit path. This matches relocation-delay logic.
- OGC does not allow arbitrary relocation after entry, but the same idea applies
  during construction: place potential blockers differently before they become
  active.
- Late-stage repair should target "delay-causing blockers", not only blocks
  with high individual tardiness.
- Flexible timing matters. When two operations compete, trying small shifts in
  entry/exit dates can be cheaper than moving many blocks.

### 3. Block Relocation Problem Local Search

Feillet, Parragh, and Tricoire study the unrestricted block relocation problem
for stacked terminal bays. Their local-search heuristic uses a carefully
defined state space and dynamic programming to find locally optimal move
sequences, reporting large improvements over constructive heuristics.

Source:
https://arxiv.org/abs/1809.08201

Relevant transfer to OGC:

- The improvement opportunity is often in a small local neighborhood around
  blocked retrieval/exit events.
- Instead of random reinsertions only, define bay-level local subproblems:
  "given this exit-path conflict chain, find a better placement/order for these
  5-20 involved blocks."
- A strong local move can be more valuable than a broad weak ALNS move.
- State compression is important. For OGC, local state could be simplified to
  active blocks in one bay over a short time window plus candidate empty zones.

### 4. Pre-Marshalling with Multiple Access Directions

Pfrommer, Meyer, and Tierney study pre-marshalling in block stacking storage
systems with multiple access directions. They combine network flow and A*
search, and show that additional access directions reduce sorting effort and
access time.

Source:
https://arxiv.org/abs/2207.09118

Relevant transfer to OGC:

- Access-path structure is first-order, not a minor feasibility detail.
- Blocks should be placed with future exits in mind, especially early-due
  blocks.
- A useful score feature is "access risk": how many active or soon-active blocks
  can block a vertical path for an early-due block.
- During construction, reserve cleaner access corridors for early due dates,
  even if that worsens bay preference locally.

### 5. ALNS for Continuous Berth Allocation

Martin-Iradi, Pacino, and Ropke solve a multi-port continuous berth allocation
problem with MIP plus ALNS and local search. The problem has continuous
space-time allocation characteristics and exact methods struggle on large
instances, motivating adaptive destroy/repair search.

Source:
https://arxiv.org/abs/2302.02356

Relevant transfer to OGC:

- OGC has the same broad shape: assign objects to space and time under dense
  interactions.
- ALNS is appropriate, but the operator set matters more than the framework.
- Destroy operators should be event-aware: worst tardiness, access blockers,
  early-due conflicts, preference regret, and overloaded bays.
- Repair should be objective-aware: minimize tardiness first, then preference
  and workload only when `Z1` is protected.

### 6. Min-Conflicts and Heuristic Repair

Min-conflicts is a classic repair heuristic for constraint satisfaction:
start from a complete assignment, repeatedly select a conflicted variable, and
move it to a value that reduces conflicts. It has been applied to large
scheduling-style CSPs, including Hubble observation scheduling.

Source:
https://en.wikipedia.org/wiki/Min-conflicts_algorithm

Relevant transfer to OGC:

- Keep complete solutions and repair them, rather than frequently returning to
  partial construction.
- Maintain a cheap conflict score for candidate moves:
  overlap conflicts, access-path conflicts, release/due violations, and
  objective regression.
- Min-conflicts is especially useful after a preference-improving move creates a
  small number of new blockers.

## Practical Design Insights for This Repo

### Keep the Current Priority: Feasibility, Then Z1

Most related work confirms that integrated yard problems are too coupled for
single-term optimization. For OGC, `Z1` tardiness should remain protected
because a small `Z1` regression can dominate large `Z3` improvements.

Implementation implication:

- Keep objective-safe acceptance.
- Do not accept preference or workload improvements that increase `Z1`, unless
  a controlled experiment proves the weighted objective improves.

### Treat MIP as a Candidate Generator

The terminal-operation literature repeatedly finds that full MIP models do not
scale well for realistic integrated operations.

Implementation implication:

- Use bay-assignment MIP for selected blocks only.
- Add soft penalties for known bad blocker pairs.
- Always run placement repair and feasibility checks after MIP suggestions.

### Add Blocker-Chain Neighborhoods

Relocation and pre-marshalling papers point to the same idea: access blockers
are the real cause of many delays.

Implementation implication:

- When a block exits late, identify blocks active in the same bay that intersect
  its vertical access path near the intended exit date.
- Destroy/repair the late block plus those blockers as one neighborhood.
- Prefer neighborhoods with 5-20 blocks so repair remains fast.

### Add Access-Risk Features During Construction

The solver should avoid creating future path conflicts before ALNS has to clean
them up.

Possible construction tie-breakers:

- penalize placing a long-stay block above or in the vertical corridor of an
  earlier-due block,
- reserve exit corridors for blocks with small due slack,
- prefer placements where an early-due block has at least one clean vertical
  path over much of its active interval.

### Use Local Exact/DP-Like Repair Where Geometry Is Small

The block relocation paper suggests that a carefully designed local state can
outperform broad random search.

Possible OGC adaptation:

- For one bay and a short time window, enumerate a limited set of alternative
  placements for the involved blocks.
- Score candidates by official objective delta plus access-conflict delta.
- Validate the best few with the fast feasibility oracle and periodic official
  checker.

### Make ALNS Operator Weights Instance-Sensitive

Small 100-block, 2-bay instances and 300-block, 5-bay instances likely need
different operator mixes.

Possible policy:

- small instances: spend more on official validation and deeper local repair,
- medium instances: ALNS with blocker-chain repair and schedule compression,
- large instances: cheap construction, cheap feasibility oracle, fewer official
  checks, and targeted repair only.

## Suggested Next Experiments

1. Baseline measurement
   - Run `prob_1`, `prob_25`, `prob_4`, `prob_2`, and `prob_3` at a short
     timelimit.
   - Record objective, `Z1`, `Z2`, `Z3`, feasibility, and ALNS iterations.

2. Blocker-chain logging
   - For every tardy block, log the top blockers in its exit path.
   - Count how often tardiness is caused by own due slack versus path blockers.

3. Access-risk construction tie-breaker
   - Add a cheap placement penalty for blocking earlier-due exit corridors.
   - Compare against current construction on 100-block problems first.

4. Local blocker-chain destroy/repair
   - Destroy the tardy block plus path blockers.
   - Reinsert by due-date order with access-risk scoring.
   - Accept only if official objective improves and feasibility holds.

5. Preference recovery with blocker memory
   - When a `Z3`-improving bay move fails, store the conflict pair.
   - Penalize that pair in future bay-assignment MIP or repair scoring.

## Bottom Line

The literature supports the current high-level architecture: feasible
construction, ALNS, local repair, and objective-safe acceptance. The strongest
new direction is to make the search more explicitly access-blocker-aware.
Rather than treating tardiness, placement, and preference as separate effects,
the solver should learn and repair the small blocker chains that convert a
geometrically valid placement into a late exit.

