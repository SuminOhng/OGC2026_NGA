# Objective Improvement Experiments

이 문서는 목적함수 3개 항을 어떻게 개선할지에 대해 agent 피드백과 실제 실험 결과를 합쳐 정리한 기록이다.

## Agent Consensus

두 agent의 결론은 거의 같았다.

1. 현재 objective는 대부분 `obj1` total tardiness가 지배한다.
2. `obj2` workload imbalance와 `obj3` preference penalty는 중요하지만, `obj1`을 조금이라도 악화시키면 대부분 손해다.
3. 따라서 solver는 먼저 tardiness를 낮추고, 이후에 tardiness를 해치지 않는 범위에서 `obj2`, `obj3`를 보정해야 한다.
4. 특히 좋은 operator는 `schedule_compression`이다. bay, position, orientation을 유지하면서 entry/exit time만 더 빠르게 재배치하려고 하므로 `obj2`, `obj3`를 직접 악화시키지 않는다.

## Objective Contribution

최종 후보 solver를 train 전체에 `timelimit=5`로 실행한 결과, 20개 instance 모두 feasible이었다.

```text
feasible      = 20 / 20
sum objective = 9,649,825,667
sum w1*obj1   = 9,629,689,995
sum w2*obj2   =       705,005
sum w3*obj3   =    19,430,667
```

비율로 보면 `w1*obj1`이 전체 objective의 거의 전부다.

```text
w1*obj1 ≈ 99.79%
w2*obj2 ≈  0.01%
w3*obj3 ≈  0.20%
```

따라서 현재 단계의 개선 우선순위는 명확하다.

```text
1. obj1 tardiness 감소
2. feasibility 유지
3. obj2 workload imbalance 보정
4. obj3 preference polishing
```

## Implemented Experiment: Schedule Compression

추가한 아이디어는 `schedule_compression`이다.

핵심은 다음과 같다.

```text
현재 feasible solution에서 각 block의 bay/x/y/orientation은 유지한다.
block들을 due-date 중심 순서로 다시 본다.
각 block을 같은 placement에서 가능한 가장 빠른 safe entry/exit slot에 다시 넣는다.
공식 check_feasibility를 통과하고 objective가 낮아질 때만 채택한다.
```

이 방식은 bay assignment를 바꾸지 않으므로 `obj2`, `obj3`를 직접 건드리지 않는다. 목표는 같은 배치 구조 안에서 `obj1`만 낮추는 것이다.

## Result: prob_1 at 60s

`prob_1`, `timelimit=60`에서 schedule compression 적용 후 결과는 다음과 같다.

```text
feasible  = True
objective = 27,585,224
obj1      = 935
obj2      = 677
obj3      = 1,902
elapsed   = 55.518s
```

weighted term으로 보면 다음과 같다.

```text
w1*obj1 = 27,200,085
w2*obj2 =      4,739
w3*obj3 =    380,400
```

이전 같은 계열 ALNS 실행에서 `prob_1`, 60초 objective가 약 `29,476,139`였으므로, schedule compression은 이 대표 instance에서 약 `1,890,915` objective를 줄였다.

```text
relative improvement ≈ 6.4%
```

개선은 거의 전부 `obj1` 감소에서 나온다.

## Short Timelimit Observation

`timelimit=5`에서는 schedule compression을 켜면 오히려 손해가 났다. 이유는 5초 제한에서는 compression 자체가 ALNS destroy/repair 반복 시간을 잡아먹기 때문이다.

그래서 최종 구현에서는 다음 정책을 사용한다.

```text
timelimit >= 20s: schedule_compression 사용
timelimit <  20s: schedule_compression 생략
```

짧은 제한에서는 aggressive seed와 small destroy ALNS가 더 중요하다.

## Reverted Experiment: Exact Obj2 Scoring

한 번은 repair score에서 `obj2`를 공식 목적함수처럼 모든 bay pair의 normalized max gap으로 더 정확히 계산하도록 바꿔 보았다.

이론적으로는 더 맞는 scoring이지만, 실제로는 짧은 제한에서 objective가 나빠졌다.

원인은 `obj2` weight가 너무 작다는 점이다. repair가 workload balance를 더 신경 쓰는 순간, due-date slot 선택이 흐려지고 `obj1`이 증가한다. 현재 weight 구조에서는 `obj2`를 조금 더 잘 맞추는 것보다 tardiness 1을 줄이는 것이 훨씬 중요하다.

따라서 이 변경은 되돌렸다.

## Current Design Rule

현재 solver 개선 방향은 다음 규칙으로 정리할 수 있다.

```text
Primary objective:
  Reduce obj1 aggressively.

Do not:
  Improve obj2 or obj3 if it increases obj1.

Use obj2/obj3:
  As tie-breakers or late-stage polishing terms.

Best low-risk improvement:
  schedule_compression for timelimit >= 20s.
```

## Next Candidates

다음으로 시도할 만한 objective-targeted 개선은 다음 순서가 좋다.

1. `critical_tardy_removal`
   - 단순 tardiness가 아니라 `w1 * tardiness`가 큰 block을 제거한다.

2. `exit_blocker_cascade_removal`
   - 늦은 block의 더 이른 feasible exit를 막는 실제 blocker를 찾아 함께 제거한다.

3. `schedule_compression_v2`
   - 현재는 due-date/order 기반 재삽입이다.
   - 다음 버전은 bay별로 tardy block 우선, non-tardy block은 기존 순서 보존을 섞어볼 수 있다.

4. `slack_safe_preference_move`
   - tardiness가 없는 block 중 slack이 충분한 block만 preferred bay로 옮긴다.

5. `late_stage_load_transfer`
   - normalized overloaded bay에서 underloaded bay로 옮기되, `delta_obj1 <= 0`일 때만 허용한다.

현재 결론은 분명하다. 이 문제에서 objective 개선은 세 항을 동일하게 최적화하는 것이 아니라, `obj1`을 중심에 놓고 `obj2`, `obj3`를 조심스럽게 보정하는 방식이 가장 타당하다.
