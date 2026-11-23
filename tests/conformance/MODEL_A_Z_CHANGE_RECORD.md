# Model A Z 추가 변경 근거

작성일: 2026-11-23. 총괄이 소스와 공통 검증으로 확인한 개발 중 기록이다.
수인님의 자체 검증 원본 대조와 최종 인계·병합 승인은 별도다.

## 비교 기준

- 보존된 XY main: `83a4b5da61db49a72711694980c1ff03d175a7ae`.
- Z 구현: PR #8 `b2bd66d63e9d8274a769ce7274612ac478e5a68e`.
- PR #8의 개발 기반 main: `204eb5e3be1575f6d2741881e99a6850052059e2`.
- 총괄 적재·수집·실행기는 미커밋 연결 변경을 적용하고 별도 SHA-256으로 식별한다.
  이를 PR #8 단독 커밋의 기능이라고 표현하지 않는다.
- 공통 spec·Schema·fixture·기대 상태는 변경하지 않았다.

## 기존 X·Y 변경과 신규 Z 구현

| 단위 | 이전 역할 → Z 추가 후 역할 | 변경 이유 |
|---|---|---|
| `implementations/db-api/app.py` | X/Y 공통 API → Z 명령·결과·snapshot·readiness 분기, Y 내부 권리 조회·잠금 API 추가 | C은행의 Z 배정과 Y의 권리 권한 확인을 연결 |
| `implementations/db-api/workflow.py` | REPO/LOAN 결과 매핑 → ALLOCATION 결과와 버전 2→3 매핑 추가 | 기존 공통 결과 형식을 Z에서도 사용 |
| `implementations/db-api/storage.py` | XY 상태·예약 → 버전 3·LOCKED 및 margin_calls/allocations/allocation_locks 추가 | Y 잠금 결정과 Z 배정의 영속성·유일성 보장 |
| 신규 `implementations/db-api/allocation.py` | Y의 조건부 권리 잠금과 결정 재전달, Z의 요청·배정·복구 | 원격 응답 유실 시 새 배정을 만들지 않고 원래 요청을 계속 확인 |
| `selftest.py` | 기존 XY 시험 유지, 선택적 Z 시험·시험용 적재 추가 | S3·멱등성·readiness의 비변경성 확인 |
| `Dockerfile`, `.dockerignore`, `env.example`와 신규 `compose.z.yaml` | XY 기본 구성 유지, XYZ 별도 Compose 구성·새 모듈 패키징 | 독립 Z DB/API와 Y↔Z 전용 네트워크·자격 제공 |

새 테이블만 만드는 `create_all`은 기존 XY 볼륨의 제약조건을 이행하지 않는다.
이 검증은 새 프로젝트·새 DB를 사용하며 기존 XY 데이터의 migration 완료를 주장하지 않는다.
위 목록은 변경 단위의 근거이며 파일 수·변경 줄 수를 복잡성 총점으로 사용하지 않는다.

## 인터페이스와 메시지

- 공통 `POST /runs/{run_id}/commands`의 Z `ALLOCATE_MARGIN`,
  Z 결과 조회·readiness·snapshot이 실제 구현에 연결됐다. 공통 계약 자체의 신규 정의는 아니다.
- 신규 Y 내부 `GET /internal/runs/{run_id}/ccp-rights/{bundle_id}`는 C은행 권리의
  버전·사용 가능 여부를 확인한다. 상태를 바꾸지 않으며 YZ 전용 Bearer 자격을 사용한다.
- 신규 Y 내부 `POST /internal/runs/{run_id}/allocation-lock`은 권리 잠금과 영속 결정을
  기록한다. 동일 요청 재전송은 같은 결정을 반환하고 내용 충돌은 거부한다.
- Y는 `RIGHT_LOCKED`와 영속 확인 메시지를 기록하고 Z는 해당 확인을 받은 뒤
  배정 한 건·증거금 충족·버전 3을 확정한다. 기존 Repo·대차 계약과 반환 의무는 유지한다.
- 요청·응답의 MESSAGE_SENT/RECEIVED와 영속 확인을 연결하지만, Z readiness의 내부
  GET은 현재 송수신 이벤트가 없다. 따라서 전체 업무 메시지 수는 **미계측**이다.
  공통 실행기 HTTP 호출과 내부 업무 메시지를 합쳐 단일 수치로 제시하지 않는다.

## 오류·경합·복구 경로와 공통 검사

| 경로 | 구현 책임 | 공통 검증 연결 |
|---|---|---|
| 인증·기관 권한·형식 | 공통 API의 Z 분기와 별도 YZ 내부 토큰 | AC-03의 Z 변형 포함 |
| CCP 사용 금지·부적격 | Y의 `right_authority`, Z의 `blocking_reason` | AC-04 `no-ccp-use`, `ineligible` |
| 선행 권리 부재·잠금·버전 충돌 | Y의 권위 있는 권리 확인과 잠금, Z의 선행 조건 확인 | AC-05 `absent-Z`, `locked-Z` 등 |
| 동일 요청 재전송 | Z에 저장된 요청·결과와 Y 영속 잠금 결정 재사용 | AC-06 `replay-Z` |
| 서로 다른 배정 경합 | Y 묶음 잠금 결정 유일성, Z 배정 유일성과 트랜잭션 | AC-10 `race-margin` 10회 |
| Y 잠금 후 Z 응답 유실·재시작 | Z는 PENDING을 유지하고 원래 요청으로 복구 | 수인님 추가 자체 시험 원본 대조 필요 |
| 공통 Y 확정 응답 지연 | 업무를 취소하지 않고 원래 결과 조회·재전송 | AC-11 5회. Z 재시작 시험을 대체하지 않음 |

## 검증과 남은 확인

- 총괄의 2026-11-22 격리 Compose: 공통 XYZ 71 PASS, XY 회귀 50 PASS.
  `results/model-a-pr8-z-connection-20261122/`에 소스·개별 증거·해시를 보존했다.
- 수인님은 같은 `b2bd66d`에서 Z 추가 자체 18 PASS·2 INCONCLUSIVE,
  XY 자체 57 PASS·1 INCONCLUSIVE를 보고했다. 보고된 원인은 원본으로 재확인한다.
  [결과 공유](https://github.com/HYBLOCK-LAB/collateral-mobility-poc/pull/8#issuecomment-7080970972).
- 우리 공통 AC-10/11의 통과를 수인님 미확정 실행의 판정 변경으로 사용하지 않는다.
  원래 실행은 보존하고 같은 조건·관측 범위를 확인한 뒤 재검증 필요 여부를 정한다.
- 본 기록은 최종 인계 승인·성능 실험·수치·그래프·메시지 집계 결과를 대신하지 않는다.
