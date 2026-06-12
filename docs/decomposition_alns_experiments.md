# Decomposition + Early-Exit ALNS Experiments

이 문서는 subsolver decomposition과 early-exit 보호 operator를 추가한 실험 기록이다.

## Motivation

`obj1` tardiness는 수식상 scheduling 항이지만, 실제 병목은 placement와 access path인 경우가 많다. 먼저 나가야 하는 block이 나중에 나가도 되는 block에 의해 exit path가 막히면, scheduler는 더 이른 infeasible exit 후보를 버리고 늦은 feasible exit를 선택할 수밖에 없다.

따라서 이번 개선 방향은 다음 두 가지였다.

```text
1. ALNS master 안에 decomposed subsolver policy를 둔다.
2. 먼저 나가야 하는 block의 exit를 막지 않는 placement/operator를 추가한다.
```

## Implemented Changes

### 1. Subsolver policy package

새 패키지를 추가했다.

```text
ogc_solver/ogc_solver/subsolvers/
  __init__.py
  decomposition.py
```

현재는 완전한 독립 solver라기보다, ALNS master가 호출하는 focused policy helper다.

- `should_run_schedule_polish(total_budget)`
  - schedule-only compression을 실행할 충분한 시간이 있는지 판단한다.
- `early_exit_protection_removal(prob_info, assignments, count)`
  - tardy early-due block과 그 block의 더 이른 exit를 막을 가능성이 큰 later-due blocker를 함께 제거한다.

### 2. Early-exit destroy operator

ALNS loop에 early-exit protection destroy를 추가했다.

핵심 아이디어:

```text
target = due date가 빠르고 실제로 tardy인 block
blocker = 같은 bay에서 target의 더 이른 exit 후보를 막는 later-due block
destroy = target + blocker
repair = bay/orientation/x/y/time을 함께 재삽입
```

초기에는 모든 instance에 자주 적용했지만, `prob_1` 같은 작은 instance에서 장시간 성능이 나빠지는 경향이 있었다. 그래서 최종적으로는 block 수가 150 이상인 instance에서만 early-exit destroy를 켜도록 조정했다.

```text
use_early_exit_destroy = len(blocks) >= 150
```

### 3. Placement-level early-exit protection

destroy operator뿐 아니라 repair 후보 평가에도 보호 로직을 추가했다.

새 block을 삽입할 때, 그 block이 이미 배치된 earlier-due block의 예정 exit 시점에 함께 존재하고, `check_exit()` 기준으로 그 earlier-due block의 exit를 막으면 그 후보를 버린다.

즉 repair 단계에서 다음 구조를 피한다.

```text
late-due block placed over/around early-due block
early-due block's scheduled exit becomes blocked
```

이 변경은 `prob_1`, `prob_6`, `prob_12`에서 30초 성능을 크게 개선했다.

### 4. Deadline-aware search

Code critic agent가 지적한 timeout 위험도 반영했다.

- `_ranked_insertions()` 내부 bay/orientation/position loop에 deadline guard 추가
- `stabilize_solution()`에 deadline 인자 추가
- seed build deadline과 stabilize deadline을 분리
- top-level solver에서 timelimit safety guard 적용

짧은 제한에서 일부 instance가 5초를 살짝 넘던 문제가 줄었다.

## Experiment Results

### Full train, timelimit 5s

최종 solver:

```text
feasible = 20 / 20
max elapsed in batch ≈ 4.85s
```

대표 objective:

```text
prob_1  =   252,087,935
prob_6  =   501,106,072
prob_12 =   501,696,499
prob_20 = 1,268,131,677
```

짧은 제한에서는 schedule polish보다 seed + small destroy 안정성이 중요하다.

### Focus set, timelimit 30s

```text
prob_1  =    40,659,658
prob_6  =   232,392,370
prob_12 =   280,216,319
prob_20 = 1,103,698,403
```

이전 같은 계열 run과 비교하면 early-exit placement protection이 특히 hard case에서 효과가 컸다.

```text
prob_1  roughly 52.4M -> 40.7M
prob_6  roughly 275.3M -> 232.4M
prob_12 roughly 342.7M -> 280.2M
prob_20 roughly 1.109B -> 1.104B
```

### Representative 60s runs

```text
prob_1  =  29,914,960
prob_20 = 858,472,714
```

`prob_20`은 early-exit protection과 placement guard가 큰 도움이 됐다. `prob_1`은 이전 best run보다 조금 나빠졌는데, 작은 instance에서는 early-exit destroy가 과보호가 될 수 있다는 신호로 봤다. 그래서 destroy operator는 큰 instance에만 켜고, placement-level protection은 유지하는 정책을 선택했다.

## Lessons

### What worked

- 먼저 나가야 하는 block을 막지 않는 placement guard
- tardy early-due block과 later-due blocker를 같이 제거하는 destroy
- subsolver policy를 ALNS master 안에 넣는 구조
- deadline-aware insertion/stabilization

### What was risky

- early-exit destroy를 너무 자주 적용하면 작은 instance에서 탐색 다양성이 줄어든다.
- deadline guard를 너무 강하게 잡으면 aggressive seed가 infeasible 상태로 끝나 fallback으로 떨어진다.
- schedule compression은 긴 timelimit에서는 좋지만, 짧은 timelimit에서는 탐색 시간을 잡아먹는다.

## Current Recommendation

현재 가장 타당한 구조는 다음과 같다.

```text
Master ALNS
  - small destroy count for stability
  - worst tardiness removal
  - access blocker removal
  - early-exit protection removal for larger instances
  - placement-level early-exit guard
  - schedule compression only when time budget is sufficient
  - official feasibility check for accepted/best/final candidates
```

다음 개선 후보는 완전한 bay/schedule/placement solver 분리가 아니라, 각 subsolver가 후보와 비판 신호를 만들고 ALNS master가 공식 objective/feasibility로 평가하는 방향이 좋다.
