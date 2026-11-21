# Model B1·B2 — Canton/Daml 구현

담당: **마양하**. 이 문서는 Canton/Daml의 기술 스택·로컬 구성·실행·자체 검증을 기록하는 곳입니다. 공통 업무·판정 규칙은 [루트 안내](../../README.md), [AGENTS.md](../../AGENTS.md), [spec](../../spec/), [shared](../../shared/README.md)를 따릅니다.

**현재 상태:** 1단계(환경과 기본 연결)를 마쳤습니다(2026-11-18). 툴체인 버전, S1·P_X·P_Y 구성, Party 호스팅, 어댑터 X·Y의 인증·권한·오류 응답·이벤트 로그까지 실제 Canton에서 확인했습니다. 업무 계약과 명령 처리는 2단계에서 구현합니다. 그래서 지금은 인증과 권한을 통과한 요청에 모두 404 `NOT_FOUND`로 응답합니다. 아래 표에서 2단계 이후로 적은 항목은 아직 정하지 않은 것입니다. B1을 먼저 완성·검증한 뒤 B2까지 반드시 구현합니다. B1과 B2를 동시에 개발하지 않습니다.

## 1. 완성할 범위

### 필수 B1

- 독립 플랫폼과 관련 Participant, 공통 Synchronizer S1, 실제 Daml 업무 계약.
- Repo → 증권대차 → CCP 배정·잠금의 권리·의무, 권한·중복·경합·복구.
- 공통 HTTP 명령·결과 조회·Y/Z readiness·관측용 snapshot·공통 로그.
- 공통 업무 ID와 실제 Daml Contract ID의 연결, 계약 교체 후에도 유지되는 업무 계약·의무의 조회.
- 공통 fixture로 새 run_id 준비, 자체 테스트, 재현 가능한 로컬 환경.

### 필수 후속 구현 B2

A·B1의 검증·계측 확보 후 B1의 업무 계약·테스트·로그를 활용하여 S2를 추가합니다. 필요한 계약을 X→Y에서 한 번 S1→S2로 재할당하고 Y→Z는 S2에서 처리합니다.

계약별 재할당·사용 불가 구간·횟수를 기록합니다. 법적 소유권 이전·반환 의무 소멸·실제 Global Synchronizer 구축으로 표현하지 않습니다. B2를 위해 필수 B1의 기능·측정·공정성을 희생하지 않습니다.

## 2. 단계별 개발

1. SDK·런타임 버전을 정하고 로컬 S1·Participant 연결을 확인합니다. 작은 실행으로 다른 Participant에서 필요한 계약의 가시성·제출 권한을 확인합니다.
2. Party 호스팅·당사자 권한·계약 공유·관측 범위를 정리하고 B1의 X·Y를 구현합니다.
3. S0→S1→S2와 관련 거부·중복·경합·조회·로그를 검증합니다.
4. 총괄 검토와 사용자 승인 후 X→Y 기준 커밋을 보존합니다. 이 확인 전에는 Z를 구현하지 않습니다.
5. Z를 추가하고 AC-01~11·회귀 검증·연결 변경 근거를 확보합니다.
6. A·B1 검증 후 선행 조건 충족을 확인하고 S2·재할당을 구현합니다. B2의 AC-01·02·12를 통과해야 B1·B2 담당 전체를 완료할 수 있습니다.

B은행의 X/Y 참여, C은행의 Y/Z 참여를 일관된 업무 주체로 표현합니다. Party·Participant·플랫폼·기관을 같은 개념으로 취급하지 않습니다. 호스팅·가시성·권한 구성은 아래 표로 드러내고, 공통 업무를 충족할 수 없는 제약은 총괄에게 보고합니다.

공통 실행기가 아직 없으면 실제 Canton 환경에서 자체 기능 확인을 수행하고 공통 검증은 NOT_RUN으로 보고합니다. 모의 응답이나 계약 예시만으로 실제 거래가 동작한다고 보고하지 않습니다.

## 3. 기술 스택·구성 — 구현 시 작성

| 항목 | 선택·버전 | 근거·기록할 내용 |
|---|---|---|
| Daml SDK·Canton | Canton 오픈소스 3.7.3, dpm 1.1.1, Daml SDK 3.7.1(LF 2.1), OpenJDK 21, PostgreSQL 17 | `scripts/install-toolchain.sh`가 배포 파일의 SHA-256을 대조합니다. LF 2.1에는 contract key가 없습니다. macOS arm64에서만 확인했습니다 |
| API 연결·런타임 | 어댑터 `adapter/server.py`: Python 3.12, aiohttp 3.14.5, jsonschema 4.26.0. 플랫폼마다 프로세스 하나(X 8201, Y 8202) | 어댑터는 자기 Participant의 JSON Ledger API v2(P_X 4513, P_Y 4523)에만 접속합니다. Ledger API는 localhost에만 열고 인증을 두지 않았습니다(한계) |
| Participant·S1 | S1(reference sequencer와 mediator 각 1개), P_X, P_Y를 Canton 프로세스 하나로 실행 | `canton/b1-xy.conf`, `canton/bootstrap-b1-xy.canton`. 노드마다 PostgreSQL DB를 따로 둡니다(127.0.0.1:5433). 상태 파일은 저장소 밖 `$CANTON_STATE_DIR`에 둡니다 |
| Party·권한·가시성 | 아래 Party 표 | 두 Participant에서 제출 권한과 가시성을 확인했습니다(자체 테스트 `selftest/stage1_probe.py`, §4). 관측 주체는 Party가 아니라 어댑터의 읽기 전용 자격으로 구분합니다 |
| 계약·공통 ID | 2단계에서 작성 | 초기 계약 생성 방법 초안은 이 절 끝에 적었습니다 |
| 중복·경합·복구 | 2단계에서 작성 | 1단계 확인: 같은 계약을 P_X와 P_Y에서 동시에 소진하면 한 번만 확정되고, 진 쪽은 409 `LOCAL_VERDICT_LOCKED_CONTRACTS`를 받습니다 |
| 로그·자체 테스트 | 이벤트는 `$CANTON_EVIDENCE_DIR/<run_id>/events.jsonl`(기본 `logs/`). producer는 `canton-adapter-<P>`, clock은 프로세스 기동마다 새로 만듭니다 | 두 어댑터가 같은 파일에 덧붙여 씁니다(flock). 자체 테스트는 §4 |
| S2·재할당 | B2 착수 후 작성 | 연결·패키지·호스팅·대상 계약·재할당 절차 |

Party 구성은 구현 시 다음 열로 작성합니다. 실제 키·토큰은 넣지 않습니다.

| 기관·역할 | Party 식별자 | 호스팅 Participant·Synchronizer | 제출·확인·가시성 범위 |
|---|---|---|---|
| A은행 | `BANK_A::<P_X 키 지문>` | P_X, S1 | Submission. X에서 `CONFIRM_REPO`를 제출합니다 |
| B은행 | `BANK_B::<P_X 키 지문>` | P_X와 P_Y, S1 | 두 곳 모두 Submission. Y에서 `CONFIRM_LOAN`을 제출하고, X에서는 Repo 상대방으로 서명합니다 |
| C은행 | `BANK_C::<P_Y 키 지문>` | P_Y, S1 | Submission. 대차 상대방으로 서명합니다 |
| CCP | 4단계에서 작성 | P_Z(4단계) | Z를 추가할 때 정합니다 |
| 관측 주체 | Party 없음 | | 어댑터의 snapshot만 읽고 거래 권한이 없습니다 |

실제 식별자는 부트스트랩이 `$CANTON_STATE_DIR/ids.json`에 씁니다. 키 지문은 Participant 키를 만들 때 정해지므로 머신마다 다릅니다.

초기 데이터 적재 코드와 공통 실행기의 자동 준비 연결은 **이서진 연구 총괄이 이 구현 폴더에서 작성**합니다. 구현 담당자는 DB 구조 또는 실제 계약 생성·Party·권한·Synchronizer 연결 방법과 필요한 설정을 제공하고 변경 시 알립니다. 아래 데이터 준비 항목은 총괄이 만든 기능의 실제 사용법을 함께 기록합니다. 초기 상태는 공통 fixture와 같아야 하며 사용 중인 run_id를 덮어쓰지 않습니다.

**초기 계약 생성 방법 초안.** 업무 템플릿은 2단계에서 정하므로, 여기서는 템플릿 이름 대신 계약의 역할로 적습니다. 2단계에서 확정하면 이 내용을 고치고 총괄에게 알립니다. S0 상태(`shared/fixtures/expected/s0.json`)는 다음 계약으로 만듭니다.

| fixture 항목 | 계약 | 서명자 | 제출 Participant와 actAs |
|---|---|---|---|
| `bundles[]`와 `initial_holder` | 권리 `{bundle_id}:right:0`(보유 BANK_A, 버전 0, 허용 명령 `CONFIRM_REPO`) | BANK_A | P_X, [BANK_A] |
| `agreements[]` 중 `REPO` | Repo 사전 합의(매도 BANK_A, 매수 BANK_B, `reuse_allowed`) | BANK_A, BANK_B | P_X, [BANK_A, BANK_B] |
| `agreements[]` 중 `LOAN` | 대차 사전 합의(대여 BANK_B, 차입 BANK_C, `ccp_use_allowed`, `collateral_condition`) | BANK_B, BANK_C | P_Y, [BANK_B, BANK_C] |
| `margin_calls[]` | 증거금 요구(제공 BANK_C, 수취 CCP) | 4단계에서 정합니다 | P_Z(4단계) |

- `principals[]`로는 계약을 만들지 않습니다. 인증 주체와 기관의 연결은 어댑터 설정에서 다룹니다. 업무 계약과 의무는 S0에서 비어 있고 명령으로만 생깁니다.
- B은행이 P_Y에서 대차를 확정할 때 Repo 합의의 `reuse_allowed`를 읽습니다. BANK_B가 Repo 합의의 서명자이고 P_Y에도 호스팅되므로 P_Y에서 이 계약이 보입니다.
- 모든 계약에 `runId` 필드를 둡니다. Participant마다 계약 전부를 제출 한 번으로 만들고, `commandId`는 `prepare-<run_id>-<participant>`로 정합니다.
- P_X와 P_Y의 계약을 한 번의 거래로 만들 수는 없습니다. BANK_A와 BANK_C를 함께 호스팅하는 Participant가 없기 때문입니다. 그래서 P_X 제출만 성공하는 일부 적재가 생길 수 있고, 이 경우는 실행기 README §12에 따라 실패로 보고하고 그 run_id로 측정하지 않습니다.
- LF 2.1에는 contract key가 없어서 원장이 run_id의 유일성을 강제하지 못합니다. 그래서 준비 프로그램이 계약을 만들기 전에 같은 run_id의 계약을 ACS에서 찾고, 있으면 `RUN_ALREADY_EXISTS`로 실패하는 방식을 제안합니다.

## 4. 재현 절차 — 실제 명령 확인 후 작성

| 순서 | 반드시 기록할 내용 | 현재 상태 |
|---|---|---|
| 설치 | SDK·런타임·필수 의존성·환경 설정 | `brew install openjdk@21 postgresql@17 python@3.12` → `cp env.example .env`(경로만 조정) → `scripts/install-toolchain.sh` → `scripts/adapter.sh setup`(`.venv` 생성) → `scripts/adapter.sh tokens`(자격 토큰 생성, 아래 자격 설정). 모두 처음 한 번만 실행합니다 |
| 노드 기동 | S1·Participant·플랫폼 서비스의 순서·명령·주소 | `scripts/db.sh init`(처음 한 번) → `scripts/db.sh start` → `scripts/canton.sh start`(부트스트랩까지 대기, 약 60초) → `scripts/adapter.sh start`(X `http://127.0.0.1:8201`, Y `http://127.0.0.1:8202`) |
| 계약 준비 | 빌드·패키지·Party·권한·필요 연결 준비 | Party와 호스팅은 Canton 부트스트랩이 만듭니다. `adapter.sh start`가 DAR를 빌드하고, 각 어댑터가 기동할 때 DAR를 올린 뒤 예열 거래를 두 번 실행합니다. 업무 계약은 2단계 |
| 자격 설정 | 기관·관측기와 Party·API 권한 연결. 비밀값 제외 | 어댑터를 처음 기동하기 전에 실행합니다(설치 순서의 마지막). 토큰 파일이 없으면 `scripts/adapter.sh start`가 기동하지 않고 멈춥니다. `scripts/adapter.sh tokens`가 `POC_BANK_A_TOKEN`·`POC_BANK_B_TOKEN`·`POC_BANK_C_TOKEN`·`POC_OBSERVER_TOKEN`을 `$CANTON_STATE_DIR/secrets/tokens.env`(권한 600)에 한 번 만듭니다. `scripts/env.sh`를 source하면 환경변수로 들어갑니다. 실행기 설정의 `auth_env`와 이름이 같습니다 |
| 새 데이터 준비 | 새 run_id와 fixture 경로로 초기 자산·합의 준비 | 준비 프로그램은 총괄이 작성합니다. 계약 생성 방법 초안은 §3 끝에 있습니다 |
| 기능 확인 | 실제 Repo·증권대차·배정·readiness·결과 조회·snapshot | 2단계 이후 |
| 자체 테스트 | 실제 Canton 기능·권한·경합·의무 유지 검증 명령 | `. scripts/env.sh` → `(cd daml/stage1-probe && dpm build)`(확인용 DAR 빌드. 저장소에는 DAR가 없습니다) → `python3.12 -I selftest/stage1_probe.py`(Participant 간 가시성·제출 권한·경합)와 `.venv/bin/python -I selftest/adapter_selftest.py`(어댑터 인증·권한·오류 응답·이벤트 로그). 보고서는 `logs/stage1/`, `logs/adapter-selftest/` |
| 공통 테스트 | 총괄 제공 실행기 연결·실행 결과 | 2단계 이후. 1단계 자체 테스트는 응답 판정에 실행기의 `tests/conformance/checks.py`를 그대로 씁니다 |
| 로그 확인 | 내부 확정·로컬 반영·업무 ID·Contract ID·시계 연결 | 어댑터 이벤트 `logs/<run_id>/events.jsonl`, 어댑터 출력 `$CANTON_STATE_DIR/logs/adapter-<P>.out`, 노드 로그 `$CANTON_STATE_DIR/logs/canton.log`. 업무 ID와 Contract ID의 연결은 2단계 |
| 종료·정리 | 노드 종료·데이터 정리·주의사항 | `scripts/adapter.sh stop` → `scripts/canton.sh stop` → `scripts/db.sh stop`. `scripts/db.sh wipe --yes`는 노드 DB를 지우고 다시 만듭니다. 되돌릴 수 없고, 다음 기동 때 Party 식별자가 바뀝니다 |
| B2 실행 | S2 준비·대상 계약·재할당·이벤트 수집 방법 | B2 착수 후 작성 |

새 실행 준비는 기존 진행 중인 run_id를 덮어쓰지 않습니다. 실제 발생한 확정·로컬 반영·재할당 이벤트만 기록하고 다른 프로세스의 단조 시계 값을 직접 빼지 않습니다.

## 5. 검증 결과·인계 — 개발 중 갱신

- 현재 단계·구현한 기능·코드 기준: 1단계 완료(2026-11-18), 명세 기준 main `26519fb`. 개발 중 공유용이며 X→Y 기준 버전이 아닙니다.
- X→Y 기준 버전과 확인한 테스트:
- Z 추가 변경 목록·사유·메시지·예외 경로:
- 자체 및 공통 테스트의 테스트 ID·run_id·PASS/FAIL/NOT_RUN·로그: 자체 테스트 `stage1-20261119T115738Z-ba05` 29건 통과, `selftest-526006a83f1e` 42건 통과. 공통 테스트는 NOT_RUN(업무 처리 미구현).
- Party·Participant·계약 가시성과 결과 정규화 방식:
- 응답 지연 후 원래 결과 확인·안전한 재전송의 재현 방법:
- B2 필수 테스트 통과 결과·계약별 재할당·사용 불가 구간·그 밖의 테스트 수행 여부:
- 알려진 제약·명세 충돌·총괄이 재현할 환경·남은 조치:

완료 기준은 [acceptance.md](../../spec/acceptance.md)를 따릅니다. B1의 AC-01~11과 후속 B2의 AC-01·02·12를 공통 실행기로 모두 PASS하고 증빙을 갖춰야 최종 개발 완료로 제출·인계할 수 있습니다. 필수 항목의 FAIL·INCONCLUSIVE·NOT_RUN·증거 누락은 인계 불가이며, 실행기 미준비나 자체 테스트만 수행한 상태도 최종 완료가 아닙니다. 공통 명세·기대 결과를 바꾸어 통과시키지 않습니다. 커밋·푸시는 실행 전에 사용자에게 확인받습니다.
