# Long-Horizon ALNS Experiments

이 문서는 `timelimit=60`과 `timelimit=600`을 비교하고, 긴 계산 시간에서 solver가 어떤 방향으로 개선되어야 하는지 정리한 기록이다.

## Why 600 Seconds Matters

`60s`는 빠른 비교에는 좋지만, 이 문제는 bay assignment, irregular placement, access feasibility, scheduling이 얽힌 통합 최적화 문제다. 따라서 hidden instance에서 몇 분 이상의 시간이 주어진다면, solver는 `timelimit`을 실제로 활용할 수 있어야 한다.

핵심 질문은 다음이다.

```text
600초를 주면 objective가 계속 내려가는가?
아니면 60초 이후 탐색이 정체되는가?
```

이번 실험에서는 600초가 실제로 의미 있었다.

## Literature Signals

이번 개선 방향은 다음 문헌 흐름과 맞닿아 있다.

- Ropke & Pisinger, 2006, "An Adaptive Large Neighborhood Search Heuristic for the Pickup and Delivery Problem with Time Windows"  
  여러 destroy/repair operator를 경쟁시키고, 성공률에 따라 선택 확률을 조정하는 ALNS의 대표적 접근이다. 구현에는 adaptive operator weighting을 반영했다. DOI: https://doi.org/10.1287/trsc.1050.0135

- Shaw, 1998, "Using Constraint Programming and Local Search Methods to Solve Vehicle Routing Problems"  
  related removal의 출발점이다. 이 문제에서는 같은 bay, time overlap, due date 관계, 실제 exit obstruction을 relatedness로 볼 수 있다.

- Feillet, Parragh, Tricoire, "A Local-Search Based Heuristic for the Unrestricted Block Relocation Problem"  
  access를 막는 block relocation 구조를 local search로 개선한다는 점이 OGC 문제의 exit blocker critic과 잘 맞는다. arXiv: https://arxiv.org/abs/1809.08201

- Martin-Iradi, Pacino, Ropke, "An Adaptive Large Neighborhood Search Heuristic for the Multi-Port Continuous Berth Allocation Problem"  
  berth position과 schedule을 함께 다루며 ALNS와 local search를 결합한다. bay/position/time을 완전히 분리하지 않고 ALNS master가 통합 평가하는 현재 방향과 유사하다. arXiv: https://arxiv.org/abs/2302.02356

## Implemented Long-Horizon Changes

### 1. Phase budget separation

600초에서 seed construction이 시간을 과하게 먹지 않도록 phase budget을 분리했다.

```text
if total_budget >= 120s:
  seed phase <= min(60s, 15% of total_budget)
  main ALNS gets the remaining search budget
else:
  keep short-budget behavior
```

짧은 timelimit에서는 aggressive seed와 stabilization이 중요하므로, 기존처럼 seed repair 여지를 유지한다.

### 2. Adaptive operator selection

600초급에서는 고정 순환 operator보다 adaptive operator selection을 사용한다.

현재 operator pool:

```text
worst2   : tardiness 큰 block 2개 제거
access2  : access blocker 기반 제거
random2  : random diversification
early2   : early-exit protection removal
worst3   : long-budget larger tardiness destroy
access3  : long-budget larger access destroy
```

operator reward:

```text
new global best      +12
accepted improvement +6
accepted non-improve +1
```

segment마다 operator weight를 업데이트한다. 이 구조는 긴 시간에서 어떤 destroy가 해당 instance에 잘 맞는지 학습하기 위한 것이다.

### 3. Deadline-aware inner loops

긴 시간뿐 아니라 짧은 시간 안정성을 위해 다음에 deadline guard를 넣었다.

```text
_ranked_insertions()
stabilize_solution()
top-level solve()
```

## 60s vs 600s Results

대표 instance에서 같은 solver를 60초와 600초로 비교했다.

| Instance | Timelimit | Feasible | Objective | obj1 | obj2 | obj3 | Elapsed |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| prob_1 | 60s | True | 29,914,960 | 1,015 | 685 | 1,914 | 58.073s |
| prob_1 | 600s | True | 22,748,395 | 767 | 2,114 | 2,104 | 587.252s |
| prob_20 | 60s | True | 594,900,665 | 22,266 | 8,603 | 8,653 | 58.669s |
| prob_20 | 600s | True | 340,773,858 | 12,736 | 6,741 | 8,820 | 587.485s |

## Interpretation

### prob_1

```text
60s  objective = 29,914,960
600s objective = 22,748,395
improvement    ≈ 24.0%
```

`obj1`이 `1,015 -> 767`로 줄었다. `obj2`, `obj3`는 약간 악화되었지만, `w1*obj1` 감소가 훨씬 커서 총 objective가 내려갔다.

### prob_20

```text
60s  objective = 594,900,665
600s objective = 340,773,858
improvement    ≈ 42.7%
```

large/hard instance에서는 600초 효과가 매우 컸다. `obj1`이 `22,266 -> 12,736`으로 줄면서 총 objective가 크게 내려갔다.

## Current Conclusion

600초는 적절할 수 있다. 특히 block 수가 많고 bay가 여러 개인 instance에서는 long-run ALNS가 실제로 더 좋은 해를 찾는다.

다만 단순히 같은 loop를 오래 돌리는 것만으로는 부족하다. 긴 시간에서는 다음 구조가 필요하다.

```text
1. seed 시간을 제한한다.
2. adaptive operator weighting을 켠다.
3. larger destroy를 가끔 허용한다.
4. early-exit / access blocker critic을 활용한다.
5. 마지막에는 schedule compression으로 obj1을 polish한다.
```

## Next Work

다음 성능 점프 후보는 다음과 같다.

1. Geometry/collision cache
   - 반복되는 `Block` 생성, `check_collisions`, `check_entry`, `check_exit` 비용을 줄인다.

2. Better telemetry
   - best objective timeline: 60s, 120s, 300s, 600s
   - operator call/accept/improve count
   - official feasibility check count

3. Related removal v2
   - Shaw-style relatedness를 공식화한다.
   - same bay, time overlap, due date distance, geometric distance, exit obstruction을 함께 본다.

4. Regret-k repair refinement
   - long horizon에서는 regret-3 또는 regret-4를 더 자주 써볼 수 있다.

5. Solution pool
   - 600초 후반에는 좋은 bay별 partial pattern을 저장하고 재조합하는 방식도 가능하다.

현재 단계의 결론은 명확하다.

```text
60초 solver만으로 충분하지 않다.
600초 예산이 주어지면 solver는 실제로 더 좋은 해를 찾는다.
따라서 long-horizon ALNS 전략은 유지할 가치가 있다.
```
