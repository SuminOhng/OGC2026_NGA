# Obj1 Reduction Experiments

This note records the latest attempt to reduce `obj1` without accepting any
worse official objective.

## Goal

The weighted objective is:

```text
objective = w1 * obj1 + w2 * obj2 + w3 * obj3
```

The user goal is not to reduce `w1`. The official `w1` is fixed by each
instance. The real goal is:

```text
reduce obj1 as much as possible
accept no move that worsens the official objective
```

The `w1 * obj1 / objective` share used to be very high. Reaching 70% naturally
requires near-zero tardiness. For example, using typical secondary penalty
size:

```text
prob_1  needs obj1 roughly 30-35
prob_20 needs obj1 roughly 100
```

The latest verified `prob_20` 600s run reaches this regime with `obj1 = 4`.
That means the target is no longer a cosmetic scoring target; it is achieved
only when the schedule/placement avoids nearly all exit-path induced tardiness.

## Implemented Changes

1. Objective-safe obj1 polish
   - Runs near the end of `solve_alns`.
   - Evaluates candidates with the official `check_feasibility`.
   - Accepts only if:

```text
candidate is feasible
candidate objective <= current objective
candidate obj1 < current obj1
```

2. Collision pair cache
   - Caches repeated pairwise `check_collisions` calls inside one solve.
   - Key: bay id + two block geometry keys.
   - Cleared at each `solve_alns` call.
   - This does not change feasibility logic; it only avoids repeated geometry
     computation.

3. Long-budget operator policy
   - Small instances (`n_blocks < 150`) keep adaptive operator selection for
     long budgets.
   - Larger instances use the fixed operator schedule that performed better
     on `prob_20`.

4. Guarded reinsertion seed for larger long-budget runs
   - The aggressive seed can be repaired either by conservative stabilization
     or by violation-block reinsertion.
   - Reinsertion may change bay, orientation, position, and timing, so it can
     reduce `obj1` more than simply delaying blocks until an empty bay window.
   - The candidate seed is still filtered by official feasibility/objective
     before it can become the incumbent.

5. Release-time batch seed for larger instances
   - For larger instances, attempt to place blocks at their release time first.
   - Same-time entries are ordered by construction sequence in the operation
     list, then official feasibility validates the final sequence.
   - The batch build stops early and reserves time for `stabilize_solution`,
     because building until the last second can create Stage 3 exit
     obstructions.

6. Same-time ENTRY sequence preservation
   - `stabilize_solution` and ALNS assignment parsing now preserve the existing
     ENTRY list order as `_seq`.
   - This prevents stabilized or reparsed batch seeds from silently reverting
     same-time ENTRY order to `block_id` order.
   - The change is especially important for Stage 5, where operations at the
     same time are replayed in list order.

7. Objective-safe exit-blocking penalty in final polish
   - During the final obj1 polish only, insertion candidates get a soft penalty
     if the new placement pairwise blocks a critical existing block's desired
     exit path.
   - The penalty is not used in the main seed construction because full
     geometry checks there slow seed construction and can worsen raw seed
     quality.
   - The final acceptance gate remains official feasibility plus
     `objective <= incumbent objective` and `obj1 < incumbent obj1`.

8. Actual exit-blocker cluster removal in final polish
   - For the most tardy blocks, inspect the desired exit time and call
     `check_exit` on the actual blocks present in that bay.
   - Add the target plus the detected blockers as an objective-safe destroy
     candidate.
   - This is only used in final polish so extra geometry work cannot degrade
     the incumbent unless the official objective improves.

9. Short/mid-budget exit-blocker chain candidate
   - For budgets below 240s, add a 4-block chain candidate:
     target, actual blockers, and one same-bay pressure neighbor.
   - In the same final-polish path, reinsert the most tardy removed block
     first so the target gets an exit-safe placement before its blockers.
   - This is disabled for longer budgets because 300s verification was better
     with the direct-blocker path only.

10. Mid-budget ordered deep exit repair
   - For large instances and effective budgets between about 90s and 210s,
     actual exit-blocker candidates preserve their destroy order during repair.
   - The target block is reinserted before its blockers, so the repair keeps
     the intent of `[target, blocker, ...]` instead of immediately sorting it
     away by due date.
   - Chain candidates can add a second-hop blocker by checking whether a direct
     blocker's own desired exit is obstructed.
   - The richer repair is disabled below 90s because it is too expensive, and
     disabled above 210s because 240s/300s tests were worse than the direct
     blocker path.

11. Long-budget milestone incumbent
   - For large instances with timelimit at least 260s, run one 180s ALNS pass
     first and keep it as a protected incumbent.
   - Then use the remaining time for the normal long-budget ALNS pass.
   - The final return still uses official feasibility/objective comparison, so
     the long pass cannot overwrite a better 180s incumbent.

12. Short-budget candidate-position cap
   - For large instances with effective budget below 90s, cap generated
     placement candidate positions at 20 instead of 80.
   - The intent is not to improve geometry quality directly. It reduces the
     chance that the release-time batch seed spends most of the 60s budget on
     a small prefix of blocks and sends the rest to serial fallback.
   - This is only enabled for short large-instance runs. 120s+ runs keep the
     wider candidate set because global candidate narrowing worsened 120s
     experiments.

13. Long-budget fast release placement
   - For large instances with timelimit at least 240s, the solver tags the
     copied problem with `_use_fast_release_seed`.
   - The release-time batch seed then tries a cheap placement first:
     lower-left, center, right edge, and top edge positions.
   - If the cheap placement passes entry, exit, and collision checks, it skips
     the expensive full candidate scan for that block.
   - This is intentionally disabled for 120s-style runs because the same idea
     worsened 120s verification, but it improved the 300s/600s long-budget
     path where the organizer is expected to provide a more reasonable solve
     time for large instances.

14. Objective-safe long-budget left-shift polish
   - For long runs, after the exit-blocker final polish, try shifting tardy
     blocks earlier while preserving their bay, position, orientation, and
     processing time.
   - Every candidate is still checked by the official `check_feasibility`.
   - The acceptance gate is intentionally strict:

```text
candidate objective <= current objective
candidate obj1 < current obj1
```

   - This is useful because many remaining `prob_20` tardy blocks are only
     slightly late; a same-placement schedule shift can remove tardiness
     without reopening the harder placement problem.
   - The long-run final reserve is currently capped at 30s for large 600s-class
     runs. A 45s reserve was tested and rejected because it worsened objective
     and created an excessive wall-time overrun.

15. Shadow-aware hybrid fast-release placement
   - The fast-release seed still tries only a few cheap positions, but no
     longer accepts the first feasible position blindly.
   - If a cheap placement blocks an earlier-due block's exit path, it is
     skipped.
   - If the placement has no estimated exit-blocking penalty, it is accepted
     immediately, preserving the old fast behavior.
   - If all cheap placements have some penalty, the least risky one is used.
   - This is the current strongest long-run change: it keeps the speed of the
     fast seed while adding the user's proposed vertical-shadow conflict
     signal to most important same-bay pairs.

## Representative Results

All results below are official `check_feasibility` results.

| Instance | Timelimit | Objective | obj1 | obj2 | obj3 | obj1 share |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| prob_1 | 20s | 30,747,934 | 1,043 | - | - | 98.68% |
| prob_1 | 60s | 29,797,027 | 1,010 | 731 | 2,050 | 98.61% |
| prob_1 | 600s | 22,231,301 | 749 | 1,906 | 2,144 | 98.01% |
| prob_20 | 20s | 939,613,226 | 35,188 | - | - | 99.87% |
| prob_20 | 60s | 506,794,070 | 18,963 | 7,104 | 8,521 | 99.78% |
| prob_20 | 300s | 294,437,710 | 10,999 | 8,042 | 8,633 | 99.62% |
| prob_20 | 600s | 81,612,088 | 3,018 | 5,597 | 8,780 | 98.61% |

Release-time batch seed verification before sequence/reserve tuning:

| Instance | Timelimit | Objective | obj1 | obj2 | obj3 | obj1 share |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| prob_20 | 60s | 348,739,392 | 13,027 | 11,043 | 10,257 | 99.61% |
| prob_20 | 120s | 423,262,268 | 15,820 | 11,013 | 10,594 | 99.67% |

Release-time batch seed after preserving same-time ENTRY order and using a
52s batch cap with roughly 6s stabilization reserve:

| Instance | Timelimit | Objective | obj1 | obj2 | obj3 | obj1 share |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| prob_20 | 60s | 191,655,488 | 7,134 | 9,310 | 10,858 | 99.26% |
| prob_20 | 60s repeat | 180,020,670 | 6,699 | 9,427 | 10,575 | 99.23% |
| prob_20 | 120s | 118,028,991 | 4,374 | 8,693 | 10,683 | 98.82% |
| prob_20 | 180s | 59,931,004 | 2,196 | 8,587 | 10,550 | 97.71% |
| prob_20 | 300s | 68,744,926 | 2,528 | 7,000 | 10,310 | 98.06% |

Exit-blocking final polish verification:

| Instance | Timelimit | Objective | obj1 | obj2 | obj3 | obj1 share |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| prob_20 | 60s | 168,842,829 | 6,280 | 8,574 | 10,581 | 99.19% |
| prob_20 | 120s | 135,258,181 | 5,022 | 8,397 | 10,289 | 99.01% |
| prob_20 | 180s | 46,750,252 | 1,704 | 10,489 | 9,974 | 97.20% |

Actual exit-blocker cluster verification:

| Instance | Timelimit | Objective | obj1 | obj2 | obj3 | obj1 share |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| prob_20 | 60s | 161,755,311 | 6,014 | 8,058 | 10,653 | 99.15% |
| prob_20 | 120s | 60,826,682 | 2,230 | 9,337 | 10,426 | 97.77% |
| prob_20 | 180s | 19,523,968 | 683 | 7,547 | 10,121 | 93.29% |
| prob_20 | 300s | 15,769,375 | 543 | 7,074 | 9,974 | 91.82% |

Short/mid-budget chain candidate verification:

| Instance | Timelimit | Objective | obj1 | obj2 | obj3 | obj1 share |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| prob_20 | 60s best observed | 95,866,049 | 3,544 | 7,346 | 10,513 | 98.58% |
| prob_20 | 120s | 47,153,113 | 1,719 | 7,965 | 10,118 | 97.22% |
| prob_20 | 180s best observed | 17,285,963 | 599 | 7,905 | 10,120 | 92.41% |
| prob_20 | 300s chain disabled latest | 15,822,867 | 545 | 7,267 | 9,966 | 91.85% |

Ordered deep exit repair and long-budget milestone verification:

| Instance | Timelimit | Objective | obj1 | obj2 | obj3 | obj1 share |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| prob_1 | 60s | 9,138,247 | 314 | 239 | 10 | 99.96% |
| prob_20 | 60s | 93,255,966 | 3,446 | 7,414 | 10,536 | 98.54% |
| prob_20 | 120s latest | 26,104,162 | 930 | 7,642 | 10,064 | 95.01% |
| prob_20 | 120s best observed | 9,273,445 | 298 | 7,884 | 10,235 | 85.69% |
| prob_20 | 180s | 13,929,822 | 474 | 7,694 | 9,948 | 90.74% |
| prob_20 | 300s with 180s milestone | 12,758,217 | 430 | 7,672 | 9,963 | 89.88% |
| prob_20 | 600s with 180s milestone | 13,644,717 | 463 | 6,566 | 10,068 | 90.49% |

The 120s best observed run is not yet stable; repeated 120s runs can land in
the 26M-40M objective range. The 300s milestone result is currently the best
more stable long-budget result.

Short-budget cap verification:

| Instance | Timelimit | Objective | obj1 | obj2 | obj3 | obj1 share |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| prob_20 | 60s cap 80 comparison | 527,818,433 | 19,743 | 9,517 | 10,198 | 99.75% |
| prob_20 | 60s cap 20 comparison | 242,596,563 | 9,047 | 8,744 | 10,302 | 99.45% |
| prob_20 | 60s cap 20 best repeat | 124,612,050 | 4,624 | 7,557 | 10,068 | 98.95% |

Long-budget fast release verification:

| Instance | Timelimit | Objective | obj1 | obj2 | obj3 | obj1 share |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| prob_20 | 300s | 12,399,186 | 418 | 6,355 | 9,714 | 89.90% |
| prob_20 | 600s | 10,321,926 | 341 | 5,809 | 9,549 | 88.10% |
| prob_20 | 900s | 10,953,840 | 363 | 4,974 | 9,951 | 88.37% |

Long-budget left-shift polish verification:

| Instance | Timelimit | Objective | obj1 | obj2 | obj3 | obj1 share |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| prob_20 | 600s fast release + left-shift, 12s reserve | 9,970,495 | 326 | 5,238 | 9,965 | 87.19% |
| prob_20 | 600s left-shift, 30s reserve, top25 | 8,958,075 | 289 | 6,302 | 9,708 | 86.03% |
| prob_20 | 600s left-shift, 30s reserve, top40 to release | 8,200,672 | 261 | 4,785 | 9,695 | 84.87% |
| prob_20 | 600s left-shift, 60s reserve | 9,556,588 | 311 | n/a | n/a | 86.78% |
| prob_20 | 600s left-shift, 45s reserve | 17,905,312 | 624 | 5,059 | 9,878 | 92.93% |

Shadow-aware hybrid fast-release verification:

| Instance | Timelimit | Objective | obj1 | obj2 | obj3 | obj1 share |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| prob_20 | 300s hybrid shadow fast release | 8,433,706 | 269 | 4,193 | 9,881 | 85.06% |
| prob_20 | 600s hybrid shadow fast release | 1,349,218 | 4 | 3,675 | 9,764 | 7.91% |

The 600s hybrid-shadow result is the current best long-budget result. It
achieves the original share target by a wide margin: weighted `obj1` is no
longer near 70% of the objective; it is below 10% on this verified run.

Recent guarded-reinsertion smoke result:

| Instance | Timelimit | Objective | obj1 | obj2 | obj3 | obj1 share |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| prob_20 | 120s best observed | 370,382,385 | 13,849 | 7,892 | 8,190 | 99.71% |
| prob_20 | 120s latest verification | 520,623,304 | 19,483 | 8,253 | 8,165 | 99.79% |

The guarded-reinsertion seed is useful but still time-sensitive. It should be
kept behind official feasibility/objective checks and should not be treated as
a completed route to the 70% share target.

## Comparison To Previous Long-Horizon Record

Previous representative records:

| Instance | Timelimit | Previous objective | Previous obj1 |
| --- | ---: | ---: | ---: |
| prob_1 | 600s | 22,748,395 | 767 |
| prob_20 | 600s | 340,773,858 | 12,736 |

Latest:

| Instance | Timelimit | Latest objective | Latest obj1 |
| --- | ---: | ---: | ---: |
| prob_1 | 600s | 22,231,301 | 749 |
| prob_20 | 600s hybrid shadow fast release | 1,349,218 | 4 |
| prob_20 | 120s best observed deep repair | 9,273,445 | 298 |

The biggest gain is on `prob_20`. The result suggests that speedup plus
release-time batch construction is better for large instances than the
previous adaptive operator policy. The latest jump comes from adding the
vertical-shadow conflict signal to the fast-release seed without fully
abandoning the old first-good-placement behavior.

## Remaining Risk

The share target is achieved on the latest verified large-instance run:

```text
prob_1 600s  obj1 share = 98.01%
prob_20 600s obj1 share = 7.91% with hybrid shadow fast release
prob_20 120s obj1 share = 85.69% best observed with deep repair
```

The remaining concern is robustness across other instances and repeated long
runs, not whether this single representative large instance can cross the 70%
share threshold.

A very optimistic matching check shows why same-time/batch entry matters:
if each bay is restricted to one ENTRY per integer time and geometry is ignored,
`prob_20` already has an `obj1` lower bound of about `1249`. The latest result
beats that simplified bound because the solver exploits same-time/batch entries
and better exit-path protection.

## Next Candidates

1. Robustness checks
   - Repeat the 600s run and test more hidden-like train instances.
   - The new hybrid shadow fast release is strong, but long runs remain
     time-cutoff sensitive.

2. Safer same-time entry exploration
   - Same-time entry can theoretically help zero-slack blocks.
   - A direct implementation hurt objective, so it needs an isolated
     objective-safe neighborhood rather than main repair integration.

3. More geometry caching
   - Pair collision cache helped.
   - Entry/exit obstruction caching may provide another speedup, but the key
     must include the full present-block set.

4. Telemetry
   - Track operator calls, accepted moves, best updates, and best timeline.
   - This is needed to understand why long-budget search improves or stalls.

## Additional Attempts

The following ideas were tested after the first obj1-polish pass and were not
kept because they worsened the official objective or did not reliably reduce
`obj1`.

| Attempt | Result |
| --- | --- |
| Larger final reserve for compression/polish | Worsened `prob_20` 300s; final reserve time reduced main search too much. |
| Fixed-placement left shift for tardy blocks | Objective-safe but found little/no improvement; 300s run worsened due to extra end-phase cost. |
| Exact top-k single relocation | Feasible, but did not reliably improve 300s objective/obj1. |
| Expanded candidate position anchors | Increased search cost and worsened 60s smoke result. |
| Large-bay-first bay order | Worsened 60s smoke result; load-balanced order is still better. |
| Same-time ENTRY without sequence handling | Feasible but worse objective/obj1 on `prob_20`. |
| Same-time ENTRY with insertion sequence replay | Feasible but still worse on 60s smoke; needs a stronger grouping/ordering model before retrying. |
| Relaxed bay-capacity preferred bay heuristic | Pure-Python LP-style approximation, but too coarse; worsened 60s smoke result. |
| Provided Greedy simple repair as large-instance seed | Timed out on `prob_20`; not useful as a submitted seed path. |
| Batch seed without stabilization reserve | Can become Stage 3 infeasible; reserve a few seconds for stabilization. |
| Greedy same-time EXIT ordering | Worsened 60s objective; exit ordering is geometry-sensitive and consumed useful seed/search time. |
| Slack-first global batch order | Worsened 60s objective/obj1; release order still matters more than coarse LP-style slack priority. |
| Longer seed phase for long budgets | An 80s isolated batch seed improved the raw seed, but integrated 180s performance worsened because ALNS search time was reduced. |
| Release/due-ordered serial fallback | Worsened direct batch seed; the previous `block_id` fallback order is accidentally but consistently safer on `prob_20`. |
| Shortest-processing-time serial fallback | Worsened direct batch seed; it increased raw Stage 2 infeasibility and produced larger stabilized obj1. |
| Same-time fallback insertion | Worsened direct batch seed; exact release placement can use same-time entries, but fallback insertion becomes too aggressive. |
| Main-loop remove count 3 | Worsened 120s result; large destroy sets disrupted feasible structure more than they helped exit-path repair. |
| Extra `_seq` propagation in ALNS repair | Worsened 120s smoke result; preserving sequence globally made repair order too rigid. Keep `_seq` preservation limited to solution parsing/stabilization. |
| Active-density exact-placement tie-breaker | Reduced obj2 but worsened obj1 badly; simple bay-area balancing is not a reliable proxy for crane exit-path safety. |
| Short-budget seed cap reduction | Worsened `prob_20` 60s; cutting release-batch construction time saved search time but destroyed seed quality. |
| Mid-budget final-polish reserve | Worsened `prob_20` 120s; reserving 10-16 seconds for obj1 polish reduced main search/seed quality too much. |
| Cheap-first final-polish ordering | Worsened `prob_20` 120s; the expensive actual-chain candidate is often the useful candidate, so delaying it can miss the only strong repair. |
| Final-polish beam variants | Worsened `prob_20` 120s; evaluating several first-insertion variants consumed too much cutoff time despite the official objective-safe gate. |
| Extra 90s/120s milestones | Worsened 180s/600s tests; short milestone incumbents can be poor and consume the time needed by the stronger single pass. |
| Stabilizer target-only or blocker-only repair | Sometimes improved the raw stabilized seed, but did not reliably improve full 60s/120s solver runs. The default all-mentioned-block repair remains the safer submitted path. |
| 45s long-run final polish reserve | Worsened `prob_20` 600s to objective 17,905,312 / obj1 624 and overran wall time to about 1847s; 30s remains the safer large-instance reserve. |
| Full fast-release shadow scoring | Improved `prob_20` 300s to objective 7,238,203 / obj1 223, but the 600s result was 7,086,971 / obj1 217, worse than the best long-run path. The retained hybrid accepts the first zero-risk cheap placement and only scores when all cheap placements carry exit-blocking risk. |
| Same-bay suffix left shift | Worsened `prob_20` 300s to objective 11,992,769 / obj1 404; shifting a large suffix by one common amount was too blunt. |
| Suffix fixed-placement reschedule | Worsened `prob_20` 600s to objective 7,766,728 / obj1 245; rebuilding suffixes with fixed placements was too conservative and consumed useful final-polish time. |
| 600s long-chain blocker repair | Worsened `prob_20` 600s to objective 9,215,778 / obj1 299 and caused a severe wall-time overrun; direct small clusters remain safer. |

PuLP was installed in the local `.codex_workspace` venv for experimentation,
but no submitted code depends on it. A real LP/MIP-assisted approach should
remain an experiment artifact unless the contest runtime dependency contract is
confirmed.
