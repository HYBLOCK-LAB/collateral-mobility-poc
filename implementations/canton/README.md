# Model B1·B2 — Canton/Daml 구현

담당: **마양하**. 이 문서는 Canton/Daml의 기술 스택·로컬 구성·실행·자체 검증을 기록하는 곳입니다. 공통 업무·판정 규칙은 [루트 안내](../../README.md), [AGENTS.md](../../AGENTS.md), [spec](../../spec/), [shared](../../shared/README.md)를 따릅니다.

**현재 상태:** 1단계(환경과 기본 연결)를 마쳤습니다(2026-11-18). 2단계는 총괄이 정한 공유 시점 2(X→Y 최소 정상 흐름)까지 구현했습니다(2026-11-21). 업무 템플릿(Daml 패키지 `collateral` 0.2.0), 초기 계약 생성 방법(§3), X `CONFIRM_REPO`와 Y `CONFIRM_LOAN`의 정상 확정, 결과 조회, Y readiness, X·Y snapshot, 기본 이벤트 로그가 실제 Canton에서 동작합니다. 중복·재전송·경합·응답 지연 복구와 정상 경로 밖의 거절 코드는 아직 구현하지 않았습니다. 그래서 정상 경로에서 벗어난 명령에는 임시로 503 `INTERNAL_ERROR`로 응답합니다. 이 상태는 개발 중 공유용이며 X→Y 기준 버전이 아닙니다. 아래 표에서 3단계 이후로 적은 항목은 아직 정하지 않은 것입니다. B1을 먼저 완성·검증한 뒤 B2까지 반드시 구현합니다. B1과 B2를 동시에 개발하지 않습니다.

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
| 계약·공통 ID | Daml 패키지 `collateral` 0.2.0(`daml/collateral`) | 템플릿·필드·서명자·생성 방법·가시성은 이 절 끝에 적었습니다. 공통 업무 ID(`right_id`, `repo_id`, `loan_id`, `agreement_id`)는 계약 필드에 그대로 두고, 실제 Contract ID는 이벤트의 `contract_id`에 적습니다 |
| 중복·경합·복구 | 3단계에서 작성 | 1단계 확인: 같은 계약을 P_X와 P_Y에서 동시에 소진하면 한 번만 확정되고, 진 쪽은 409 `LOCAL_VERDICT_LOCKED_CONTRACTS`를 받습니다 |
| 로그·자체 테스트 | 이벤트는 `$CANTON_EVIDENCE_DIR/<run_id>/events.jsonl`(기본 `logs/`). producer는 `canton-adapter-<P>`, clock은 프로세스 기동마다 새로 만듭니다 | 두 어댑터가 같은 파일에 덧붙여 씁니다(flock). 이벤트 종류와 `contract_id`의 뜻은 이 절 끝에 적었습니다. 자체 테스트는 §4 |
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

**초기 계약 생성 방법(2단계 확정, 2026-11-21).** S0 상태(`shared/fixtures/expected/s0.json`)는 Daml 패키지 `collateral` 0.2.0의 다음 계약으로 만듭니다. 템플릿은 패키지 이름 참조 `#collateral:<모듈>:<템플릿>`으로 지정합니다. 값은 JSON Ledger API v2의 인코딩을 따릅니다: Int는 문자열(`"0"`), Optional의 없음은 `null`, enum은 이름 문자열입니다.

| fixture 항목 | 템플릿 | 필드(fixture에서 옮기는 값) | 서명자 | 제출 Participant |
|---|---|---|---|---|
| `bundles[]`와 `initial_holder` | `#collateral:Collateral.Right:UseRight` | `runId`, `bundle`{`bundleId`, `securityId`, `quantity`{`value`, `unit`}, `referenceValue`{`amount`, `currency`}}, `rightId` = `{bundle_id}:right:0`, `holder` = `initial_holder`의 Party, `createdVersion` = `"0"`, `status` = `"AVAILABLE"`, `allowedCommands` = `["CONFIRM_REPO"]`, `predecessorRightId`·`sourceRecordId` = `null` | `holder`(BANK_A) | P_X |
| `agreements[]` 중 `REPO` | `#collateral:Collateral.Agreement:RepoAgreement` | `runId`, `agreementId`, `bundleId`, `seller`·`buyer`(Party), `reuseAllowed` | `seller`, `buyer`(BANK_A, BANK_B) | P_X |
| `agreements[]` 중 `LOAN` | `#collateral:Collateral.Agreement:LoanAgreement` | `runId`, `agreementId`, `bundleId`, `lender`·`borrower`(Party), `ccpUseAllowed`, `collateral`{`collateralId`, `conditionsMet`, `provided`} | `lender`, `borrower`(BANK_B, BANK_C) | P_Y |
| `margin_calls[]` | 4단계에서 정합니다 | | | P_Z(4단계) |

- Participant마다 `POST /v2/commands/submit-and-wait-for-transaction`을 한 번 호출하고, 그 안에 `CreateCommand`를 모두 넣습니다. P_X는 actAs [BANK_A, BANK_B]로 `UseRight`와 `RepoAgreement`를 만들고, P_Y는 actAs [BANK_B, BANK_C]로 `LoanAgreement`를 만듭니다. `commandId`는 `prepare-<run_id>-<participant>`입니다. `selftest/xy_selftest.py`의 `prepare()`가 이 방법을 그대로 실행하는 예입니다.
- `principals[]`로는 계약을 만들지 않습니다. 인증 주체와 기관의 연결은 어댑터 설정에서 다룹니다. 업무 계약과 의무는 S0에서 비어 있고 명령으로만 생깁니다.
- 모든 계약에 `runId` 필드가 있습니다. 어댑터는 X에서 그 run의 `RepoAgreement`나 `UseRight`, Y에서 `LoanAgreement`가 보이면 준비된 run으로 봅니다. 보이지 않으면 404 `NOT_FOUND`로 응답합니다.
- P_X와 P_Y의 계약을 한 번의 거래로 만들 수는 없습니다. BANK_A와 BANK_C를 함께 호스팅하는 Participant가 없기 때문입니다. 그래서 P_X 제출만 성공하는 일부 적재가 생길 수 있고, 이 경우는 [실행기 README §12](../../tests/conformance/README.md)에 따라 실패로 보고하고 그 run_id로 측정하지 않습니다.
- LF 2.1에는 contract key가 없어서 원장이 run_id의 유일성을 강제하지 못합니다.
- **초안에서 바뀐 점.** 초안은 대차를 확정할 때 Repo 합의의 `reuse_allowed`를 읽는다고 적었습니다. 구현에서는 Repo 확정 choice가 `reuseAllowed`를 읽어 right:1의 `allowedCommands`를 정합니다(참이면 `["CONFIRM_LOAN"]`, 거짓이면 `[]`). 대차 확정은 right:1의 `allowedCommands`만 확인합니다. `ccpUseAllowed`도 같은 방식으로 right:2의 `allowedCommands`(`["ALLOCATE_MARGIN"]` 또는 `[]`)를 정합니다. 공통 예상 상태(`expected_state`)가 바꾸는 값과 같습니다.

**확정 거래.** 두 명령은 각각 원장 거래 하나로 확정됩니다. 사전 합의 계약의 choice는 nonconsuming이라 합의 계약은 남고, 같은 권리로 두 번 확정하려 하면 권리가 이미 소진되어 실패합니다.

| 명령 | 행사하는 choice와 제출 | choice 안에서 확인하는 것 | 거래가 만드는 계약 |
|---|---|---|---|
| X `CONFIRM_REPO` | `RepoAgreement.ConfirmRepo`(인자 `requestId`, `repoId`, `expectedVersion`, `rightCid`). P_X에서 actAs [BANK_A] | 권리의 run·묶음, 보유자가 seller, `AVAILABLE`, `createdVersion` = `expectedVersion`, `CONFIRM_REPO` 허용 | right:0을 `UseRight.Consume`으로 `CONSUMED` 계약으로 다시 만들고, right:1(보유 BANK_B), `TradeContract`(REPO), `Obligation` 2건(Repo 환매 대금, 증권 반환)을 만듭니다 |
| Y `CONFIRM_LOAN` | `LoanAgreement.ConfirmLoan`(인자 `requestId`, `loanId`, `expectedVersion`, `rightCid`). P_Y에서 actAs [BANK_B] | 별도 담보의 `provided`·`conditionsMet`, 그다음 위와 같은 권리 확인(`CONFIRM_LOAN` 허용) | right:1 소진, right:2(보유 BANK_C), `TradeContract`(LOAN), `Obligation` 2건(증권 반환, 별도 담보 반환·해제) |

- `UseRight`는 보유자 단독 서명입니다. 상대방의 권한은 사전 합의 계약의 서명에서 옵니다. 한계: 보유자는 Ledger API로 권리 계약을 혼자 만들 수 있고, 거래 없이 `Consume`이나 `Archive`를 혼자 행사할 수도 있습니다. `Consume`을 단독으로 행사하면 거래 기록 없이 묶음 버전이 올라갑니다. 원장은 이런 단독 행사를 막지 못합니다.
- `TradeContract`에는 확정한 요청의 `requestId`와 `actor`를 둡니다. 결과 조회(`GET /runs/{run_id}/commands/{request_id}`)는 이 계약으로 COMMITTED 결과를 다시 구성하므로 어댑터를 재기동해도 조회됩니다. 확정되지 않은 요청은 404 `NOT_FOUND`입니다.

**가시성.** 계약은 서명자를 호스팅하는 Participant에만 보입니다(A=P_X, B=P_X와 P_Y, C=P_Y).

| 계약 | X(P_X) | Y(P_Y) |
|---|---|---|
| right:0 | 보임 | 보이지 않음 |
| right:1 | 보임 | 보임 |
| right:2 | 보이지 않음 | 보임 |
| Repo 합의와 Repo 기록(`TradeContract`·`Obligation`) | 보임 | 보임(BANK_B) |
| 대차 합의와 대차 기록 | 보임(BANK_B) | 보임 |

**snapshot 변환.** 어댑터는 snapshot을 요청받을 때마다 자기 Participant의 ACS를 같은 offset에서 페이지 단위로 끝까지 읽고(호스팅 Party 둘, 모든 템플릿), 그 run의 계약만 공통 형식으로 바꿉니다. 행은 ID 순서로 정렬합니다.

- `rights`: `UseRight` 하나가 한 행입니다. 소진된 권리도 `CONSUMED` 계약으로 남아 있어서 그대로 나옵니다.
- `bundles`: 보이는 권리마다 `createdVersion`(소진된 권리는 +1)을 구하고 그 최댓값을 묶음 버전으로 씁니다. 보이는 권리가 없으면 묶음을 내지 않습니다.
- `contracts`: `TradeContract`. `contract_id`는 업무 ID(`repo_id`, `loan_id`)이고, 상태는 `ACTIVE`입니다.
- `obligations`: `Obligation`. `target`은 Daml variant(`AgreementRef`, `Securities`, `CollateralRef`)를 공통 형식으로 바꿉니다.
- `margin_calls`, `allocations`는 Z가 아직 없으므로 `[]`입니다. `pending_operations`도 지금은 항상 `[]`입니다. 명령이 원장의 확정 응답을 기다리는 동안에도 처리 중(`PROCESSING`)으로 보고하지 않으므로, 그동안 받은 snapshot은 처리 중인 상태를 보여 주지 않습니다. 처리 중 상태 보고는 3단계(응답 지연·복구)에서 구현합니다.
- 사전 합의 계약은 snapshot에 내지 않습니다. 공통 snapshot 형식(`shared/schemas/snapshot.schema.json`)에 합의 항목이 없기 때문입니다. `snapshot_seq`는 플랫폼·run별 응답 순번입니다(interface §13). 어댑터 프로세스 안에서 세므로 재기동하면 0부터 다시 셉니다.

**readiness(Y `CONFIRM_LOAN`).** 원장을 읽기만 합니다. 조회한 `agreement_id`·`bundle_id`의 대차 합의가 없으면 대상 미존재이므로 404 `NOT_FOUND`로 응답합니다(shared/README §5). 별도 담보가 제공되지 않았거나 조건을 충족하지 않으면 `PRECONDITION_NOT_MET`입니다. BANK_B가 그 묶음의 권리를 하나도 보유하지 않으면(Repo 확정 전) `PRECONDITION_NOT_MET`, 보유한 권리가 모두 소진되었으면 `RIGHT_UNAVAILABLE`, `AVAILABLE` 권리가 `CONFIRM_LOAN`을 허용하지 않으면 `USE_NOT_PERMITTED`입니다. `observed_version`은 `AVAILABLE` 권리의 `createdVersion`이고, 그런 권리가 없으면 `null`입니다. 원장을 읽을 수 없으면 추정하지 않고 503 `TEMPORARILY_UNAVAILABLE`로 응답합니다.

**이벤트.** 정상 확정 한 건은 `COMMAND_RECEIVED` → `INTERNAL_COMMITTED` → `RIGHT_CONSUMED` → `RIGHT_CREATED` → `LOCAL_APPLIED` → `COMMAND_FINISHED`(COMMITTED)를 남깁니다. 확정 이벤트는 원장 확정 응답(`submit-and-wait-for-transaction`)에 실제로 들어 있는 계약으로만 만듭니다.

| 이벤트 | `contract_id` | 기록 시점 |
|---|---|---|
| `INTERNAL_COMMITTED` | 그 거래가 만든 `TradeContract` | 어댑터가 확정 응답을 받은 직후. 원장 안에서 확정된 시각이 아니라 어댑터 시계로 잰 시각입니다 |
| `RIGHT_CONSUMED` | 소진된 권리를 다시 만든 `CONSUMED` 계약 | 같은 시점(권리 소진이 새 권리 생성보다 먼저입니다) |
| `RIGHT_CREATED` | 새 권리 계약 | 같은 시점 |
| `LOCAL_APPLIED` | 새 권리 계약 | 그 Participant의 ACS에 새 권리가 보인 때(최대 5초 대기). 끝까지 보이지 않으면 기록하지 않고 경고 로그만 남깁니다 |

정상 경로에서 벗어난 명령(합의나 `AVAILABLE` 권리를 찾지 못함, 원장 거절, 원장 무응답)에는 3단계에서 정할 거절·재확인 처리 대신 임시로 503 `INTERNAL_ERROR`를 답하고 `COMMAND_FINISHED`에 같은 코드를 남깁니다. 코드의 해당 위치에는 `TEMPORARY (stage 3)` 주석이 있습니다.

## 4. 재현 절차 — 실제 명령 확인 후 작성

| 순서 | 반드시 기록할 내용 | 현재 상태 |
|---|---|---|
| 설치 | SDK·런타임·필수 의존성·환경 설정 | `brew install openjdk@21 postgresql@17 python@3.12` → `cp env.example .env`(경로만 조정) → `scripts/install-toolchain.sh` → `scripts/adapter.sh setup`(`.venv` 생성) → `scripts/adapter.sh tokens`(자격 토큰 생성, 아래 자격 설정). 모두 처음 한 번만 실행합니다 |
| 노드 기동 | S1·Participant·플랫폼 서비스의 순서·명령·주소 | `scripts/db.sh init`(처음 한 번) → `scripts/db.sh start` → `scripts/canton.sh start`(부트스트랩까지 대기, 약 60초) → `scripts/adapter.sh start`(X `http://127.0.0.1:8201`, Y `http://127.0.0.1:8202`) |
| 계약 준비 | 빌드·패키지·Party·권한·필요 연결 준비 | Party와 호스팅은 Canton 부트스트랩이 만듭니다. `adapter.sh start`가 DAR(`collateral` 0.2.0, 업무 템플릿 포함)를 빌드하고, 각 어댑터가 기동할 때 DAR를 올린 뒤 예열 거래를 두 번 실행합니다 |
| 자격 설정 | 기관·관측기와 Party·API 권한 연결. 비밀값 제외 | 어댑터를 처음 기동하기 전에 실행합니다(설치 순서의 마지막). 토큰 파일이 없으면 `scripts/adapter.sh start`가 기동하지 않고 멈춥니다. `scripts/adapter.sh tokens`가 `POC_BANK_A_TOKEN`·`POC_BANK_B_TOKEN`·`POC_BANK_C_TOKEN`·`POC_OBSERVER_TOKEN`을 `$CANTON_STATE_DIR/secrets/tokens.env`(권한 600)에 한 번 만듭니다. `scripts/env.sh`를 source하면 환경변수로 들어갑니다. 실행기 설정의 `auth_env`와 이름이 같습니다 |
| 새 데이터 준비 | 새 run_id와 fixture 경로로 초기 자산·합의 준비 | 준비 프로그램은 총괄이 작성합니다. 계약 생성 방법은 §3 끝에 있고, `selftest/xy_selftest.py`의 `prepare()`가 같은 방법으로 새 run_id의 S0 계약을 만듭니다 |
| 기능 확인 | 실제 Repo·증권대차·배정·readiness·결과 조회·snapshot | `selftest/xy_selftest.py`가 새 run_id로 S0 → Y readiness → X Repo → S1 → Y readiness → Y 증권대차 → S2 → 결과 조회 → 이벤트 이력을 실제 Canton에서 확인합니다. CCP 배정은 Z(4단계) |
| 자체 테스트 | 실제 Canton 기능·권한·경합·의무 유지 검증 명령 | `. scripts/env.sh` → `(cd daml/stage1-probe && dpm build)`(확인용 DAR 빌드. 저장소에는 DAR가 없습니다) → `python3.12 -I selftest/stage1_probe.py`(Participant 간 가시성·제출 권한·경합)와 `.venv/bin/python -I selftest/adapter_selftest.py`(어댑터 인증·권한·오류 응답·이벤트 로그), `.venv/bin/python -I selftest/xy_selftest.py`(X→Y 정상 흐름). 보고서는 `logs/stage1/`, `logs/adapter-selftest/`, `logs/xy-selftest/` |
| 공통 테스트 | 총괄 제공 실행기 연결·실행 결과 | NOT_RUN. 총괄이 초기 적재와 로그 수집을 연결한 뒤 함께 실행합니다. 자체 테스트는 판정에 실행기의 `tests/conformance/checks.py`(`initial_state`, `state_check`, `check_result`, `history_check`)를 그대로 씁니다 |
| 로그 확인 | 내부 확정·로컬 반영·업무 ID·Contract ID·시계 연결 | 어댑터 이벤트 `logs/<run_id>/events.jsonl`, 어댑터 출력 `$CANTON_STATE_DIR/logs/adapter-<P>.out`, 노드 로그 `$CANTON_STATE_DIR/logs/canton.log`. 업무 ID(`record_id`, `right_id`)와 실제 Contract ID(`contract_id`)는 같은 이벤트에 함께 적습니다(§3 끝) |
| 종료·정리 | 노드 종료·데이터 정리·주의사항 | `scripts/adapter.sh stop` → `scripts/canton.sh stop` → `scripts/db.sh stop`. `scripts/db.sh wipe --yes`는 노드 DB를 지우고 다시 만듭니다. 되돌릴 수 없고, 다음 기동 때 Party 식별자가 바뀝니다 |
| B2 실행 | S2 준비·대상 계약·재할당·이벤트 수집 방법 | B2 착수 후 작성 |

새 실행 준비는 기존 진행 중인 run_id를 덮어쓰지 않습니다. 실제 발생한 확정·로컬 반영·재할당 이벤트만 기록하고 다른 프로세스의 단조 시계 값을 직접 빼지 않습니다.

## 5. 검증 결과·인계 — 개발 중 갱신

- 현재 단계·구현한 기능·코드 기준: 1단계 완료(2026-11-18). 2단계는 공유 시점 2(X→Y 최소 정상 흐름)까지 구현했습니다(2026-11-21, 브랜치 기준 main `a64e694`). 개발 중 공유용이며 X→Y 기준 버전이 아닙니다.
- X→Y 기준 버전과 확인한 테스트:
- Z 추가 변경 목록·사유·메시지·예외 경로:
- 자체 및 공통 테스트의 테스트 ID·run_id·PASS/FAIL/NOT_RUN·로그: 2026-11-21 자체 테스트 `xy-selftest-20b9506d237a` 12건, `selftest-736c700a09e8` 42건, `stage1-20261121T090426Z-6d42` 29건 통과(`stage1_probe.py`는 어댑터를 거치지 않으므로 어댑터 수정 뒤 다시 돌리지 않았습니다). 공통 테스트는 NOT_RUN(총괄의 초기 적재·로그 수집 연결 전).
- Party·Participant·계약 가시성과 결과 정규화 방식: §3 끝의 가시성 표와 snapshot 변환.
- 응답 지연 후 원래 결과 확인·안전한 재전송의 재현 방법:
- B2 필수 테스트 통과 결과·계약별 재할당·사용 불가 구간·그 밖의 테스트 수행 여부:
- 알려진 제약·명세 충돌·총괄이 재현할 환경·남은 조치: 정상 경로 밖의 명령은 임시 503 `INTERNAL_ERROR`(3단계에서 거절 코드·중복·재전송·경합·복구 구현). `UseRight` 보유자 단독 서명의 한계(§3). 읽기마다 호스팅 Party의 ACS 전체를 읽으므로 원장에 run이 쌓일수록 느려집니다. 명령도 확정 전에 ACS 전체를 한 번 읽고, 확정 뒤 `LOCAL_APPLIED`를 확인할 때 최대 5초 동안 50ms 간격으로 다시 읽은 뒤에 답합니다. 처리 중인 명령은 `pending_operations`에 나오지 않습니다. 명령 응답이 이 읽기를 기다리므로, 공통 실행기가 측정하는 응답 시간에도 이 비용이 들어갑니다. 측정(measurement.md)을 시작하기 전에 고쳐야 합니다. `INTERNAL_COMMITTED` 시각은 어댑터가 확정 응답을 받은 시각입니다. `snapshot_seq`는 재기동하면 0부터 셉니다.

완료 기준은 [acceptance.md](../../spec/acceptance.md)를 따릅니다. B1의 AC-01~11과 후속 B2의 AC-01·02·12를 공통 실행기로 모두 PASS하고 증빙을 갖춰야 최종 개발 완료로 제출·인계할 수 있습니다. 필수 항목의 FAIL·INCONCLUSIVE·NOT_RUN·증거 누락은 인계 불가이며, 실행기 미준비나 자체 테스트만 수행한 상태도 최종 완료가 아닙니다. 공통 명세·기대 결과를 바꾸어 통과시키지 않습니다. 커밋·푸시는 실행 전에 사용자에게 확인받습니다.
