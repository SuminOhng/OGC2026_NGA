# OGC Solver Objective Analysis

이 문서는 OGC solver의 목적함수 3개 항을 문제 정의 관점에서 정리하고, 각 항을 어떤 결정과 ALNS operator로 개선할 수 있는지 남겨두기 위한 개발 보고서다.

공식 feasibility checker 기준 목적함수는 다음과 같다.

```text
objective = w1 * obj1 + w2 * obj2 + w3 * obj3
```

- `obj1`: total tardiness
- `obj2`: normalized bay workload imbalance
- `obj3`: bay preference penalty

중요한 점은 이 문제의 좋은 해가 단순히 block을 빨리 배치한 해가 아니라는 것이다. 좋은 해는 due date가 빡빡한 block의 출구를 열어두면서, bay별 workload를 면적 기준으로 적절히 나누고, 가능한 한 선호 bay를 유지하는 해다.

## 1. Total Tardiness

```text
obj1 = sum_i max(0, exit_time_i - due_date_i)
```

각 block의 `EXIT` 시간이 due date를 넘긴 만큼 누적된다. 현재 train instance들을 보면 `w1`이 큰 경우가 많아서, objective 대부분은 이 항에서 결정된다. 따라서 solver의 1차 목표는 feasible한 해를 만드는 것에서 끝나지 않고, tardy block의 exit를 앞으로 당기는 것이어야 한다.

### 영향을 주는 결정

- block의 `bay_id`
- block의 `entry_time`, `exit_time`
- 같은 bay 안에서 동시에 존재하는 block들의 위치 관계
- crane entry path와 exit path를 막는 block의 존재
- release time, processing time, due date가 촘촘한 block의 처리 우선순위

특히 tardiness는 block 하나만의 문제가 아닐 때가 많다. 어떤 block이 늦게 나가는 이유는 그 block 자체의 배치가 나빠서라기보다, 그 block의 exit path를 다른 block들이 막고 있기 때문일 수 있다.

### 개선 전략

- `worst_tardiness_removal`: 현재 해에서 tardiness가 큰 block을 제거한다.
- `access_blocker_removal`: tardy block과 같은 bay/time window에서 entry 또는 exit를 막는 주변 block을 함께 제거한다.
- small destroy 반복: 현재 구현에서는 많은 block을 한꺼번에 제거하면 Stage 2/3 infeasible로 무너지기 쉬웠다. 작은 수의 block을 반복적으로 제거/복구하는 편이 더 안정적이었다.
- due-date-aware repair: due date가 빠르고 slack이 작은 block을 repair 단계에서 우선한다.
- schedule compression: 같은 placement를 유지하되 entry/exit를 앞으로 당길 수 있는지 확인한다.
- regret insertion: 지금 넣지 않으면 나중에 선택지가 급격히 나빠지는 block을 먼저 삽입한다.

### ALNS 관점

Tardiness는 ALNS의 중심 operator가 되어야 한다.

```text
destroy target = tardy block + its access blockers
repair score   = delta tardiness + access feasibility risk + secondary penalties
```

초기 단계에서는 `obj2`, `obj3`보다 `obj1` 개선을 강하게 우선하는 것이 합리적이다. `w1`이 크면 tardiness 1 단위가 preference나 workload penalty 여러 단위를 압도할 수 있기 때문이다.

## 2. Normalized Bay Workload Imbalance

공식 구현 기준으로 bay별 workload 합을 bay 면적으로 정규화한 뒤, bay들 사이의 최대 차이를 본다.

```text
bay_load[j] = sum workload_i for blocks assigned to bay j
u[j]        = average_bay_area / bay_area[j]
obj2        = floor(max_{j1 != j2} abs(u[j1] * bay_load[j1] - u[j2] * bay_load[j2]))
```

즉 이 항은 단순히 bay마다 같은 workload를 넣으라는 뜻이 아니다. 큰 bay는 같은 workload를 받아도 덜 혼잡한 것으로 평가되므로, 면적 정규화 후의 workload가 비슷해지도록 배분해야 한다.

### 영향을 주는 결정

- block의 `bay_id`
- workload가 큰 block을 어느 bay에 넣는가
- 큰 bay를 얼마나 적극적으로 사용하는가
- 선호도 또는 tardiness 때문에 특정 bay에 block이 몰리는가

위치, orientation, entry/exit time은 `obj2`에 직접 들어가지 않는다. 그러나 특정 bay로 옮긴 block이 실제로 들어갈 수 있어야 하므로, geometry와 access feasibility가 간접적으로 큰 영향을 준다.

### 개선 전략

- `imbalance_removal`: normalized load가 가장 높은 bay에서 일부 block을 제거한다.
- `pair transfer`: 높은 normalized load bay에서 낮은 normalized load bay로 block 이동을 시도한다.
- `large_workload_relocation`: workload가 큰 block은 `obj2` 변화량이 크므로 우선 이동 후보로 둔다.
- repair scoring에 `delta_obj2`를 포함한다.
- 삽입 후 `max normalized load gap`이 커지는 bay 선택에는 penalty를 준다.

### ALNS 관점

`obj2`는 global max 기반이라 작은 이동이 점수에 바로 드러나지 않을 수 있다. 따라서 단순 local score보다 다음과 같은 operator가 더 효과적이다.

```text
find overloaded bay by normalized load
remove high-workload / low-preference / low-tardiness-risk blocks
try moving them to underloaded bay
accept only if feasibility remains and total objective improves
```

이 항은 초반보다는 중후반 개선에 적합하다. 먼저 tardiness를 줄인 뒤, tardiness를 크게 악화시키지 않는 범위에서 workload balance를 맞추는 것이 안전하다.

## 3. Bay Preference Penalty

```text
obj3 = sum_i (max_preference_i - preference_i[bay_i])
```

각 block이 가장 선호하는 bay에 배정되면 penalty가 0이다. 덜 선호하는 bay에 갈수록 penalty가 커진다.

### 영향을 주는 결정

- block의 `bay_id`
- 선호도가 높은 bay에 실제로 placement가 가능한가
- 선호 bay가 이미 혼잡하거나 access path obstruction을 만드는가

orientation, x/y, entry/exit time은 preference 값 자체에는 직접 영향을 주지 않는다. 하지만 선호 bay 안에서 feasible placement가 가능해야 하므로, 실제로는 geometry와 시간 결정이 preference 개선 가능성을 제한한다.

### 개선 전략

- `preference_removal`: preference penalty가 큰 block을 제거 후 재삽입한다.
- repair에서 preference가 높은 bay를 우선 후보로 검사한다.
- tardiness가 없는 block은 preference 개선 move를 더 적극적으로 허용한다.
- tardy block은 preference보다 exit time 개선을 우선한다.
- objective 개선이 작더라도 feasibility와 tardiness를 해치지 않는 preference move는 후반 local search로 적용한다.

### ALNS 관점

Preference는 마지막 품질 개선에 적합하다. 초반부터 preference만 쫓으면 due date가 촘촘한 block이 늦어지고, workload도 한 bay에 몰릴 수 있다.

추천 우선순위는 다음과 같다.

```text
1. feasible solution 확보
2. obj1 tardiness 감소
3. obj2 workload imbalance 보정
4. obj1을 악화시키지 않는 obj3 preference 개선
```

## 항들 사이의 충돌

### Tardiness vs Preference

가장 선호도가 높은 bay가 이미 혼잡하면, 그 bay에 넣는 순간 exit가 늦어질 수 있다. 이 경우 `obj3`는 줄지만 `obj1`이 크게 증가한다.

따라서 due date가 촉박한 block은 선호 bay보다 시간 feasibility와 access path를 우선해야 한다.

### Tardiness vs Workload Balance

납기를 줄이려면 큰 bay나 빈 bay에 block을 몰아넣는 것이 유리할 수 있다. 하지만 그러면 normalized workload imbalance가 커질 수 있다.

따라서 ALNS는 초반에는 tardiness 중심, 후반에는 imbalance 보정 중심으로 operator 비중을 조절하는 것이 좋다.

### Preference vs Workload Balance

여러 block이 같은 bay를 선호하면 preference를 줄일수록 workload가 한쪽으로 몰릴 수 있다. 반대로 balance를 맞추려면 일부 block을 덜 선호하는 bay로 보내야 한다.

이 경우 workload가 작은 block은 preference를 따르고, workload가 큰 block은 balance를 고려하는 식의 차등 전략이 필요하다.

### Objective vs Feasibility

표면적으로 objective가 좋아 보이는 bay assignment라도 crane entry/exit를 막으면 infeasible하거나 큰 delay를 만든다. 특히 exit path obstruction은 tardiness를 폭발시킨다.

따라서 insertion cost는 단순 objective 추정치가 아니라 다음을 함께 봐야 한다.

- entry feasibility
- exit feasibility
- 같은 bay 내 spatial collision
- 나중에 꺼낼 block의 path를 막는 정도
- 해당 bay의 normalized workload 변화
- bay preference penalty 변화

## Solver Design Implications

ALNS destroy/repair operator는 목적함수 세 항을 각각 겨냥해야 한다.

| Objective term | Main risk | Useful destroy operator | Useful repair idea |
| --- | --- | --- | --- |
| `obj1` tardiness | due date 초과 | tardy block + access blocker removal | earliest feasible insertion, due-aware repair |
| `obj2` workload imbalance | normalized load 편차 | overloaded bay removal | transfer to underloaded bay |
| `obj3` preference penalty | low-preference bay assignment | high preference-penalty removal | preferred-bay reinsertion |
| feasibility | crane/path/spatial obstruction | geometry/access conflict removal | official local access checks |

현재 solver에서 가장 중요한 방향은 다음과 같다.

1. 먼저 feasible하고 tardiness가 낮은 해를 만든다.
2. 늦은 block 하나만 보지 말고, 그 block의 entry/exit를 막는 blocker를 함께 재배치한다.
3. 큰 destroy가 infeasible을 만들면 작은 destroy를 여러 번 반복한다.
4. 어느 정도 tardiness가 낮아지면 normalized bay workload imbalance를 보정한다.
5. 마지막으로 tardiness를 악화시키지 않는 preference 개선 move를 적용한다.

## Practical Checklist

향후 solver를 개선할 때는 각 후보 move에 대해 다음 질문을 확인한다.

- 이 move가 `obj1`을 얼마나 줄이거나 늘리는가?
- tardy block의 직접 원인이 block 자체인가, 주변 access blocker인가?
- 이 move가 normalized bay load gap을 줄이는가?
- preference penalty 개선이 tardiness 악화를 정당화할 만큼 큰가?
- official feasibility stage 2/3/4/5 중 어느 위험을 만드는가?
- 큰 destroy보다 작은 destroy 반복이 더 안정적인 instance인가?

이 체크리스트를 통과하는 operator가 단순 greedy보다 나은 해를 만들 가능성이 높다.
