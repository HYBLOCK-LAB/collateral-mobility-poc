# 공통 데이터 계약과 테스트 예시

연구 총괄이 검토한 공통 JSON 구조를 반영한 문서다. 두 구현은 이 데이터 계약을 따르되, 내부 DB 테이블·Daml 계약 구조까지 동일하게 만들 필요는 없다. 아직 구현·실험 완료나 spec-v1.0 고정을 의미하지 않는다.

## 1. 파일 구성

| 위치 | 내용 |
|---|---|
| `schemas/common.schema.json` | 공통 식별자·기관·수량·금액·권리·계약·의무·배정 정의 |
| `schemas/command.schema.json` | 네 가지 명령과 명령별 payload |
| `schemas/result.schema.json` | 논리 요청 결과와 접수 전·조회 오류 |
| `schemas/readiness.schema.json` | 후속 거래 준비 상태 |
| `schemas/snapshot.schema.json` | 플랫폼별 업무 상태 |
| `schemas/event.schema.json` | 계측·권리 이력·재할당·메시지 이벤트 |
| `schemas/fixture.schema.json` | 최초 자산·사전 합의·권한 데이터 |
| `schemas/expectation.schema.json` | S0~S3의 공통 기대 결과 |
| `fixtures/initial/` | 정상·조건 거부·경합용 최초 데이터 |
| `fixtures/commands/` | Repo·증권대차·CCP 배정·B2 재할당 명령 |
| `fixtures/results/` | 성공·업무 거부·결과 미확정·접근 거부 응답 예시 |
| `fixtures/readiness/` | Y의 준비 완료 응답 예시 |
| `fixtures/snapshots/` | Y의 증권대차 확정 직후 로컬 상태 예시 |
| `fixtures/events/` | 공통 관측기의 확정 관측 이벤트 예시 |
| `fixtures/expected/` | 실행 전체를 대상으로 확인할 S0~S3 기대 결과 |
| `fixtures/invalid/` | 명령 Schema 검증에 실패해야 하는 입력 |

Schema는 JSON Schema Draft 2020-12를 사용한다. `$id`의 `https://hyblock.invalid/schemas/`는 식별용 주소이며 실제 서비스가 아니다. 모든 참조는 이 폴더의 Schema를 로컬 등록하여 해결하고 외부 네트워크 조회에 의존하지 않는다.

## 2. 공통 형식

- 식별자는 1~128자의 영문·숫자·`-`·`_`·`.`·`:` 문자열이다.
- 기관은 `BANK_A`, `BANK_B`, `BANK_C`, `CCP`, 플랫폼은 `X`, `Y`, `Z`, 모델은 `A`, `B1`, `B2`다.
- 업무 버전은 0 이상의 정수다. boolean을 정수로 취급하지 않는다.
- 금액·액면 수량은 정수의 십진수 문자열이며 소수점·음수·불필요한 선행 0을 허용하지 않는다. 자산 수량은 양수다.
- 이번 수량 단위는 `KRW_FACE_VALUE`, 통화는 `KRW`다. 수량과 기준 평가액을 별도 관리한다.
- 단조 시각은 나노초 정수 문자열이다.
- 정의한 nullable 필드만 `null`을 허용하며, 빈 목록은 `[]`다.
- 미정의 필드는 원칙적으로 거부한다. 모델 고유 진단 정보는 별도 로그에 둔다.
- JSON 필드 순서·배열 순서는 업무상 차이로 비교하지 않는다.
- 업무 ID의 유일성은 `run_id` 범위다. 내부 DB 키나 Canton Contract ID를 공통 업무 ID로 대체하지 않는다.

Schema는 형식과 필수 필드를 검사한다. 인증·권한·자산 참조·선행 거래·경합 안전성은 업무 로직과 공통 검증기로 별도 확인한다.

## 3. 최초 데이터와 실험 가정

대표 입력은 `fixtures/initial/f0-normal.json`이다.

- `bundle-001`은 테스트 종목 `TEST-KTB-001`의 전체 묶음이다.
- 예시는 액면 100억 원과 기준 평가액 100억 원을 고정한 테스트 조건이다. 실제 국채 가격이 항상 액면과 같다는 뜻은 아니다.
- Repo는 B은행의 재사용을 허용한다.
- 증권대차는 C은행의 CCP 제공을 허용한다.
- 별도 대차 담보는 `separate-collateral-001`로 구분하며 실제 교환·평가는 모사하지 않는다.
- `whole_bundle_accepted`는 고정된 평가·헤어컷·수용 조건을 전제로 전체 묶음을 수용한다는 모사 결과다. 실제 CCP 계산을 구현하지 않는다.
- 사전 합의가 유효하면 업무 명세의 환매·반환 의무가 발생한다. 별도 boolean으로 의무를 생략할 수 없다.
- fixture에 비밀값을 넣지 않는다. 실제 자격 정보는 실행 설정에서 `principal_id`에 연결한다.
- 기본 업무 주체는 X Repo의 A은행, Y 증권대차의 B은행, Z CCP 배정의 C은행, B2 재할당의 B은행이다.
- `observer=true`인 주체는 전체 snapshot 검증을 위한 읽기 전용 주체다. 관측 권한을 거래 제출 권한으로 사용하지 않는다.
- `run_id`는 적재 시 지정한다. 초기 A은행 권리와 버전 0만 준비하고 후속 상태는 실제 명령으로 생성한다.

| 최초 데이터 | F0와 다른 업무 조건 | 용도 |
|---|---|---|
| `f0-normal.json` | 없음 | 정상·중복·Y 경합·T3 |
| `no-reuse.json` | Repo `reuse_allowed=false` | 재사용 불허 |
| `collateral-not-provided.json` | 대차 담보 `provided=false` | 담보 미제공 |
| `collateral-condition-failed.json` | 대차 담보 `conditions_met=false` | 담보 조건 미충족 |
| `no-ccp-use.json` | 증권대차 `ccp_use_allowed=false` | C 후속 사용 불허 |
| `ineligible.json` | 증거금 요구 `eligibility_passed=false` | CCP 적격성 미충족 |
| `two-margin-calls.json` | 같은 묶음을 수용하는 M2 추가 | 잠금 검증·배정 경합 |

변형 데이터는 식별 메타데이터 외에 표의 업무 조건만 바꾼 완성된 파일이다. 동적 상속·패치 해석기를 만들지 않는다. B2의 금융 데이터는 F0와 동일하고 S1·S2 연결은 환경 설정에서 구분한다.

## 4. 명령과 요청 결과

명령은 해당 플랫폼의 `POST /runs/{run_id}/commands`에 제출한다. 요청자 기관은 인증에서 확인하며 payload의 값만 신뢰하지 않는다.

| 예시 | 플랫폼·주체 | 정상 전후 버전 |
|---|---|---|
| `fixtures/commands/repo.json` | X·A은행 | 0→1 |
| `fixtures/commands/loan.json` | Y·B은행 | 1→2 |
| `fixtures/commands/margin.json` | Z·C은행 | 2→3 |
| `fixtures/commands/reassignment.json` | X·B은행, B2만 | 1→1 |

재할당 성공의 `record_type`은 `REASSIGNMENT`다. 공통 `record_id`는 해당 재할당 요청 ID를 사용하고 실제 계약별 재할당 ID는 별도 이벤트에 기록한다.

- COMMITTED는 효과와 null 오류, REJECTED는 null 효과와 업무 오류를 가진다.
- PENDING은 효과·오류가 모두 null이다.
- UNKNOWN은 null 효과와 `OUTCOME_UNKNOWN` 또는 `TEMPORARILY_UNAVAILABLE`을 가진다.
- 타임아웃으로 응답이 없으면 실행기가 UNKNOWN을 관측한다. 서버가 응답했다고 가정하지 않는다.
- `message` 문구·언어는 판정 대상이 아니며 공통 오류 코드와 실제 업무 상태를 검사한다.
- 같은 요청의 재제출은 원래 ID와 내용을 유지하고 `replayed=true`를 반환한다. 일반 결과 조회는 명령 재제출이 아니다.
- 기존 요청 처리 확인보다 현재 버전 검사를 먼저 수행하지 않는다.
- 접수 전 오류와 요청 ID 충돌은 기존 요청의 원래 결과를 덮어쓰지 않는다.

| 상황 | HTTP 상태 |
|---|---:|
| COMMITTED·REJECTED | 200 |
| 접수 후 PENDING | 202 |
| 형식·잘못된 플랫폼 | 400 |
| 인증 실패 | 401 |
| 인증된 주체의 권한 부족 | 403 |
| 조회 대상 미존재 | 404 |
| 같은 요청 ID에 다른 내용 | 409 |
| 현재 처리·결과 확인 불가 | 503 |

404에는 `NOT_FOUND`를 사용한다. 요청 조회의 404는 원래 거래의 확정 실패를 증명하지 않는다. 내부 오류·연결 실패를 근거 없이 확정 거부로 바꾸지 않는다.

인증·형식·조회 오류는 `run_id`, `request_id`, `error` 형식이며 해석할 수 없는 ID는 null이다.

## 5. Readiness

Y는 `command_type=CONFIRM_LOAN`, `bundle_id`, `agreement_id`를 조회 인자로 받는다. Z는 `command_type=ALLOCATE_MARGIN`, `bundle_id`, `margin_call_id`를 받는다. 주체는 인증에서 확인한다.

- ready=true이면 확인한 버전과 빈 `blocking_reasons`를 제공한다.
- ready=false이면 업무상 방해 사유를 공통 코드 목록으로 제공한다. 확인한 버전이 없으면 null이다.
- 인증·권한·대상 미존재·권위 있는 조회의 통신 오류는 별도 오류 응답이다.
- 조회 실패를 임의로 ready=false나 true로 변환하지 않는다.
- 조회는 예약·소진·버전 변경을 하지 않는다.

## 6. Snapshot과 정규화 기록

Snapshot은 `run_id`, `platform`, `snapshot_seq`, `bundles`, `rights`, `contracts`, `obligations`, `margin_calls`, `allocations`, `pending_operations`를 가진다.

`snapshot_seq`는 같은 플랫폼·실행에서 증가하는 응답 순번이다. 최신성 보증이나 플랫폼 간 공통 순서가 아니다.

각 플랫폼은 조회 가능한 기록만 반환한다. 전역 복제를 요구하지 않으며 snapshot을 업무상 중앙 장부로 사용하지 않는다. `fixtures/snapshots/y-after-loan.json`은 Y의 로컬 예시이고 전역 S2 전체 데이터가 아니다.

### 권리

| 권리 | 공통 ID |
|---|---|
| 초기 A은행 | `{bundle_id}:right:0` |
| Repo 후 B은행 | `{bundle_id}:right:1` |
| 증권대차 후 C은행 | `{bundle_id}:right:2` |

C은행의 같은 논리 권리는 CCP 배정 후 LOCKED가 되고 허용 명령은 빈 배열이다. 내부 계약이 교체돼도 공통 권리 ID는 유지한다.

소진된 권리도 CONSUMED와 빈 허용 명령으로 표현한다. AVAILABLE 권리의 허용 명령은 합의에 따라 빈 배열일 수 있다. 예약·복구·재할당의 사용 제한까지 함께 확인해야 한다.

### 의무

| 관계 | 공통 ID | 유형 |
|---|---|---|
| A→B 환매대금 | `{repo_id}:repurchase-payment` | `REPO_REPURCHASE_PAYMENT` |
| B→A 동등증권 | `{repo_id}:securities-return` | `REPO_SECURITIES_RETURN` |
| C→B 동등증권 | `{loan_id}:securities-return` | `LOAN_SECURITIES_RETURN` |
| B→C 별도 담보 반환·해제 | `{loan_id}:collateral-return` | `LOAN_COLLATERAL_RETURN_OR_RELEASE` |

증권 반환 대상은 종목·수량이며 최초의 동일 묶음 자체를 반환해야 한다는 의미가 아니다. 환매대금은 합의 참조, 별도 담보는 담보 ID·합의 참조로 표현하고 실제 금액 계산·반환은 실행하지 않는다.

이번 정상 경로의 의무는 OUTSTANDING이다. 만기 도래나 불이행이라는 의미는 아니다.

계약의 ACTIVE는 업무상 유효 상태다. Daml 계약의 활성 여부와 혼동하지 않는다. 배정은 LOCKED, 증거금 요구는 OPEN 또는 SATISFIED다.

진행 상태는 요청·묶음·종류(PROCESSING·RECOVERING·REASSIGNING)와 `blocks_use`를 기록한다. 내부 처리 단계 수는 강제하지 않는다.

## 7. 기대 결과 비교

`fixtures/expected/s0.json`부터 `s3.json`까지 업무상 전체 기대 결과다. S1은 Repo, S2는 증권대차, S3는 CCP 배정 완료 상태다.

기대 결과는 버전·권리·계약·의무·증거금 요구의 상세 값과 비교할 ID 목록을 함께 가진다. 실행기는 ID뿐 아니라 주체·대상·권한·수량·계약 참조까지 검증한다.

- 동일 실행의 동일 공통 ID는 한 논리 기록으로 연결한다.
- 같은 ID의 권리자·수량 등이 모순되면 단순 복제본으로 취급하지 않는다.
- 과거 복제본과 현재 상태는 버전·전이 이력으로 구분한다.
- 기대하지 않은 추가 유효 권리·계약·의무·배정도 검사한다.
- 내부 진단 기록이나 동일 경제적 계약의 내부 조각을 공통 업무 기록으로 중복 계산하지 않는다.
- 배열 순서·내부 ID·시각 값 자체를 A·B 동등성으로 비교하지 않는다.
- 정상 최종 상태와 경합 중 이력 검증은 별개다.

## 8. 이벤트

`fixtures/events/commit-observed.json`은 공통 관측기의 예시이며 시각은 실제 측정값이 아니다.

생산 프로세스·시계 ID·순번을 유지한다. 재제출 시 요청 ID는 유지하고 호출 시도 ID는 바꾼다. 결과 조회도 별도 시도 ID를 사용한다.

내부 이벤트를 수집할 때 원래 `clock_id`·`mono_ns`를 관측기 시각으로 덮어쓰지 않는다. 관측기 수신 시각은 선택적인 `observation`에 별도 저장한다. 다른 시계의 시각을 직접 빼지 않는다.

최초 확정 관측은 같은 논리 요청에 한 번 기록한다. 실행 종료는 요청·플랫폼 등이 null일 수 있다. 이벤트별 조건부 필수 값은 event Schema에서 확인한다.

권리 이벤트는 권리·주체·상태를, 재할당 이벤트는 실제 계약·재할당 ID·source·target을, 메시지 이벤트는 메시지 ID·종류·분류·발신·수신 주체를 기록한다. 추가 진단 정보는 별도 로그에 보존한다.

## 9. 검증과 미구현 범위

JSON Schema 검증에서 정상 예시는 통과해야 하고 아래 세 파일은 명령 Schema에 실패해야 한다.

- `fixtures/invalid/missing-bundle-id.json`
- `fixtures/invalid/wrong-version-type.json`
- `fixtures/invalid/extra-permission-field.json`

정상 금융 형식의 거부용 fixture는 Schema를 통과해야 한다. 재사용 불허 같은 업무상 거부는 실행 단계에서 검증한다.

Schema 외에도 참조 정합성, 데이터 변형 범위, 공통 ID 생성, 권리 소진·잠금, 의무 보존과 코드 일치를 확인한다. 유효한 형식이라는 이유만으로 업무 정합성을 인정하지 않는다.

공통 실행기·검증기의 실제 A·B 호출, 초기화 연결, T3 장애 주입 장치, 결과 집계·보관은 후속 구현 대상이다. 이 예시 파일은 실제 실행 로그나 측정 결과가 아니다.
