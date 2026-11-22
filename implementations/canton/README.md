# Model B1·B2 — Canton/Daml 구현

담당: **마양하**. 이 문서는 Canton/Daml의 기술 스택·로컬 구성·실행·자체 검증을 기록하는 곳입니다. 공통 업무·판정 규칙은 [루트 안내](../../README.md), [AGENTS.md](../../AGENTS.md), [spec](../../spec/), [shared](../../shared/README.md)를 따릅니다.

**현재 상태:** 1단계(환경과 기본 연결)를 마쳤습니다(2026-11-18). 2단계는 총괄이 정한 공유 시점 2(X→Y 최소 정상 흐름, Draft PR #5)에 이어 시점 3(X→Y 거절·중복·경합·복구 검증 준비)까지 구현했습니다(2026-11-21). 2026-11-22에는 총괄이 승인한 범위에서 권리 계약에 공동 서명자를 추가했습니다(§3 확정 거래). 업무 템플릿(Daml 패키지 `collateral` 0.3.0), 초기 계약 생성 방법(§3), X `CONFIRM_REPO`와 Y `CONFIRM_LOAN`의 확정과 업무 거절, 같은 요청의 재전송과 동시 제출, 요청 ID 충돌, 다른 요청 사이의 경합, 응답을 놓친 뒤의 결과 조회, Y readiness, X·Y snapshot, 이벤트 로그가 실제 Canton에서 동작합니다(§3 끝 결과 상태와 멱등성). 공통 실행기 검증은 아직 하지 않았습니다(NOT_RUN). 이 상태는 개발 중 공유용이며 X→Y 기준 버전이 아닙니다. 아래 표에서 3단계 이후로 적은 항목은 아직 정하지 않은 것입니다. B1을 먼저 완성·검증한 뒤 B2까지 반드시 구현합니다. B1과 B2를 동시에 개발하지 않습니다.

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
| 계약·공통 ID | Daml 패키지 `collateral` 0.3.0(`daml/collateral`) | 템플릿·필드·서명자·생성 방법·가시성은 이 절 끝에 적었습니다. 공통 업무 ID(`right_id`, `repo_id`, `loan_id`, `agreement_id`)는 계약 필드에 그대로 두고, 실제 Contract ID는 이벤트의 `contract_id`에 적습니다 |
| 중복·경합·복구 | 요청 키마다 작업 하나, 거부 기록 파일, 원장의 `TradeContract`, Canton `commandId` 중복 제거 | 이 절 끝의 결과 상태와 멱등성. 1단계 확인: 같은 계약을 P_X와 P_Y에서 동시에 소진하면 한 번만 확정되고, 진 쪽은 409 `LOCAL_VERDICT_LOCKED_CONTRACTS`를 받습니다 |
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

**초기 계약 생성 방법(2단계 확정 2026-11-21, 공동 서명자 반영 2026-11-22).** S0 상태(`shared/fixtures/expected/s0.json`)는 Daml 패키지 `collateral` 0.3.0의 다음 계약으로 만듭니다. 템플릿은 패키지 이름 참조 `#collateral:<모듈>:<템플릿>`으로 지정합니다. 값은 JSON Ledger API v2의 인코딩을 따릅니다: Int는 문자열(`"0"`), Optional의 없음은 `null`, enum은 이름 문자열입니다.

| fixture 항목 | 템플릿 | 필드(fixture에서 옮기는 값) | 서명자 | 제출 Participant |
|---|---|---|---|---|
| `bundles[]`와 `initial_holder` | `#collateral:Collateral.Right:UseRight` | `runId`, `bundle`{`bundleId`, `securityId`, `quantity`{`value`, `unit`}, `referenceValue`{`amount`, `currency`}}, `rightId` = `{bundle_id}:right:0`, `holder` = `initial_holder`의 Party, `cosigner` = 그 묶음 `REPO` 합의의 `buyer` Party(fixture에 따로 없는 값입니다), `createdVersion` = `"0"`, `status` = `"AVAILABLE"`, `allowedCommands` = `["CONFIRM_REPO"]`, `predecessorRightId`·`sourceRecordId` = `null` | `holder`, `cosigner`(BANK_A, BANK_B) | P_X |
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
| X `CONFIRM_REPO` | `RepoAgreement.ConfirmRepo`(인자 `requestId`, `repoId`, `expectedVersion`, `rightCid`). P_X에서 actAs [BANK_A] | 권리의 run·묶음, 보유자가 seller, `AVAILABLE`, `createdVersion` = `expectedVersion`, `CONFIRM_REPO` 허용 | right:0을 `UseRight.Consume`(인자 `nextHolder` = BANK_B)으로 `CONSUMED` 계약으로 다시 만들고, right:1(보유 BANK_B, 공동 서명 BANK_A), `TradeContract`(REPO), `Obligation` 2건(Repo 환매 대금, 증권 반환)을 만듭니다 |
| Y `CONFIRM_LOAN` | `LoanAgreement.ConfirmLoan`(인자 `requestId`, `loanId`, `expectedVersion`, `rightCid`). P_Y에서 actAs [BANK_B] | 별도 담보의 `provided`·`conditionsMet`, 권리의 `cosigner`가 차입자(BANK_C)가 아닌지, 그다음 위와 같은 권리 확인(`CONFIRM_LOAN` 허용) | right:1 소진(`nextHolder` = BANK_C), right:2(보유 BANK_C, 공동 서명 BANK_B), `TradeContract`(LOAN), `Obligation` 2건(증권 반환, 별도 담보 반환·해제) |

- `UseRight`는 보유자(`holder`)와 공동 서명자(`cosigner`)가 함께 서명합니다. 확정 거래가 만드는 다음 권리의 공동 서명자는 그 권리를 넘겨준 이전 보유자이고, right:0의 공동 서명자는 Repo 매수자입니다. 두 Party가 같은 권리는 만들 수 없습니다(`ensure`). `Consume`은 다음 보유자(`nextHolder`)를 인자로 받고 보유자와 다음 보유자가 함께 행사하며, 두 Party가 같으면 실패합니다. 확정 choice 안에서는 상대방의 권한이 사전 합의 계약의 서명에서 옵니다. 그래서 한 기관 혼자서는 Ledger API로 권리를 만들거나 `Archive`하거나 `Consume`할 수 없습니다. `ConfirmLoan`은 사용하는 권리의 공동 서명자가 차입자이면 거절합니다. 이 검사가 없으면 B·C가 함께 만든 권리로 Repo 없이 대차를 확정할 수 있습니다. 이 검사는 Repo 매도자와 대차 차입자가 다르다고 전제합니다. 공통 fixture 7개는 모두 Repo 매도자가 BANK_A, 차입자가 BANK_C입니다. 둘이 같은 fixture라면 정상 대차도 이 검사에서 거절됩니다. B·C가 함께 만든 right:1(보유 BANK_B, 공동 서명 BANK_C)이 있는 run에 어댑터로 `CONFIRM_LOAN`을 보내면 원장이 거절하고, 어댑터는 상태로 설명할 수 없는 원장 거절이므로 503 `TEMPORARILY_UNAVAILABLE`로 답합니다.
- **신뢰 범위.** 이 PoC는 초기 적재·원장 관리 권한을 신뢰하는 범위에서 동작합니다. 한 Participant가 함께 호스팅하는 두 기관(P_X의 BANK_A·BANK_B, P_Y의 BANK_B·BANK_C)이 Ledger API로 함께 제출하면, 둘이 서명한 권리를 거래 없이 만들거나 없애거나 소진할 수 있습니다. A·B가 Repo 뒤 right:1을 `nextHolder` = BANK_A로 `Consume`하면 거래 기록 없이 묶음 버전이 오르고, `Archive`하면 소진 기록 없이 권리가 사라집니다. A·B가 Repo 없이 right:1(보유 BANK_B, 공동 서명 BANK_A)을 만들면 B는 그 권리로 Repo 없이 대차를 확정할 수 있습니다. B·C는 right:2를 거래 없이 소진할 수 있습니다. 두 서명자가 함께 제출하면 확정 choice 안과 같은 권한을 갖기 때문에, 계약 권한만으로는 거래를 거친 소진과 직접 소진을 구별하지 못합니다. 이 PoC에서 공동 제출을 쓰는 곳은 초기 적재뿐이고, 어댑터는 인증한 기관 하나만 actAs에 넣습니다. 공동 제출 전체를 막는 설계는 이번 범위에 넣지 않았습니다(PR #5, 2026-11-22 총괄 승인).
- `TradeContract`에는 확정한 요청의 `requestId`와 `actor`를 둡니다. 결과 조회(`GET /runs/{run_id}/commands/{request_id}`)는 이 계약으로 COMMITTED 결과를 다시 구성하므로 어댑터를 재기동해도 조회됩니다. 거절되거나 처리 중인 요청의 조회는 이 절 끝의 결과 상태와 멱등성을 따릅니다.

**가시성.** 계약은 서명자를 호스팅하는 Participant에만 보입니다(A=P_X, B=P_X와 P_Y, C=P_Y). 권리 계약은 모두 BANK_B가 서명하므로 두 Participant에 모두 보입니다. 0.2.0에서는 right:0이 P_Y에, right:2가 P_X에 보이지 않았습니다.

| 계약 | X(P_X) | Y(P_Y) |
|---|---|---|
| right:0 | 보임 | 보임(BANK_B) |
| right:1 | 보임 | 보임 |
| right:2 | 보임(BANK_B) | 보임 |
| Repo 합의와 Repo 기록(`TradeContract`·`Obligation`) | 보임 | 보임(BANK_B) |
| 대차 합의와 대차 기록 | 보임(BANK_B) | 보임 |

- Party 단위로 읽으면 BANK_A는 Repo 합의·Repo 기록·right:0·right:1만 보고, BANK_C는 대차 합의·대차 기록·right:2만 봅니다. 대차 거래에서 BANK_A는 right:1의 `Consume`과 그 결과인 `CONSUMED` right:1만 봅니다. BANK_A가 right:1의 서명자이기 때문입니다. `Consume`의 인자 `nextHolder`와 행사 Party에 BANK_C가 있으므로, BANK_A는 대차의 차입자가 누구인지 알게 됩니다. 0.2.0에서는 BANK_A가 대차 거래를 보지 못했습니다. 어댑터는 호스팅 Party 둘을 함께 읽으므로 snapshot은 위 표의 Participant 단위를 따릅니다. 0.3.0에서는 X·Y snapshot의 `rights`가 S0에서 right:0, S2에서 right:0~2로 같아졌습니다.

**snapshot 변환.** 어댑터는 snapshot을 요청받을 때마다 자기 Participant의 ACS를 같은 offset에서 페이지 단위로 끝까지 읽고(호스팅 Party 둘, 모든 템플릿), 그 run의 계약만 공통 형식으로 바꿉니다. 행은 ID 순서로 정렬합니다.

- `rights`: `UseRight` 하나가 한 행입니다. 소진된 권리도 `CONSUMED` 계약으로 남아 있어서 그대로 나옵니다.
- `bundles`: 보이는 권리마다 `createdVersion`(소진된 권리는 +1)을 구하고 그 최댓값을 묶음 버전으로 씁니다. 보이는 권리가 없으면 묶음을 내지 않습니다.
- `contracts`: `TradeContract`. `contract_id`는 업무 ID(`repo_id`, `loan_id`)이고, 상태는 `ACTIVE`입니다.
- `obligations`: `Obligation`. `target`은 Daml variant(`AgreementRef`, `Securities`, `CollateralRef`)를 공통 형식으로 바꿉니다.
- `margin_calls`, `allocations`는 Z가 아직 없으므로 `[]`입니다. `pending_operations`는 원장의 응답을 기다리는 요청입니다(아래 결과 상태와 멱등성).
- 사전 합의 계약은 snapshot에 내지 않습니다. 공통 snapshot 형식(`shared/schemas/snapshot.schema.json`)에 합의 항목이 없기 때문입니다. `snapshot_seq`는 플랫폼·run별 응답 순번입니다(interface §13). 어댑터 프로세스 안에서 세므로 재기동하면 0부터 다시 셉니다.

**readiness(Y `CONFIRM_LOAN`).** 원장을 읽기만 합니다. 조회한 `agreement_id`·`bundle_id`의 대차 합의가 없으면 대상 미존재이므로 404 `NOT_FOUND`로 응답합니다(shared/README §5). 별도 담보가 제공되지 않았거나 조건을 충족하지 않으면 `PRECONDITION_NOT_MET`입니다. BANK_B가 그 묶음의 권리를 하나도 보유하지 않으면(Repo 확정 전) `PRECONDITION_NOT_MET`, 보유한 권리가 모두 소진되었으면 `RIGHT_UNAVAILABLE`, `AVAILABLE` 권리가 `CONFIRM_LOAN`을 허용하지 않으면 `USE_NOT_PERMITTED`입니다. `observed_version`은 `AVAILABLE` 권리의 `createdVersion`이고, 그런 권리가 없으면 `null`입니다. 원장을 읽을 수 없으면 추정하지 않고 503 `TEMPORARILY_UNAVAILABLE`로 응답합니다.

**이벤트.** 정상 확정 한 건은 `COMMAND_RECEIVED` → `INTERNAL_COMMITTED` → `RIGHT_CONSUMED` → `RIGHT_CREATED` → `LOCAL_APPLIED` → `COMMAND_FINISHED`(COMMITTED)를 남깁니다. 확정 이벤트는 원장 확정 응답(`submit-and-wait-for-transaction`)에 실제로 들어 있는 계약으로만 만듭니다.

| 이벤트 | `contract_id` | 기록 시점 |
|---|---|---|
| `INTERNAL_COMMITTED` | 그 거래가 만든 `TradeContract` | 어댑터가 확정 응답을 받은 직후. 원장 안에서 확정된 시각이 아니라 어댑터 시계로 잰 시각입니다 |
| `RIGHT_CONSUMED` | 소진된 권리를 다시 만든 `CONSUMED` 계약 | 같은 시점(권리 소진이 새 권리 생성보다 먼저입니다) |
| `RIGHT_CREATED` | 새 권리 계약 | 같은 시점 |
| `LOCAL_APPLIED` | 새 권리 계약 | 그 Participant의 ACS에 새 권리가 보인 때(50ms 간격으로 최대 2초 대기). 끝까지 보이지 않으면 기록하지 않고 경고 로그만 남깁니다 |

`COMMAND_FINISHED`에는 응답의 `outcome`(COMMITTED·REJECTED·PENDING·UNKNOWN)과 `error_code`를 적습니다. 기록된 결과로 답한 재전송에는 `INTERNAL_COMMITTED`·`RIGHT_*`·`LOCAL_APPLIED`를 다시 쓰지 않습니다.

**결과 상태와 멱등성(3단계, 2026-11-21).** 공유 시점 3(X→Y 거절·중복·경합·복구)의 구현입니다.

- 요청 키는 (run_id, 인증된 기관, `request_id`)입니다. 같은 키의 내용은 `expected_version`을 정수로 맞춘 정렬 JSON으로 비교합니다.
- 키 하나는 한 번에 작업(asyncio Task) 하나만 처리합니다. 같은 키가 처리 중에 다시 오면, 내용이 다를 때는 409 `REQUEST_ID_CONFLICT`로 답합니다. 내용이 같을 때는 앞 작업이 끝나기를 기다린 뒤 처음부터 다시 판정하므로, 앞 작업이 남긴 기록을 찾아 `replayed=true`로 답합니다.
- 판정은 기존 기록을 먼저 보고 그다음 상태를 봅니다(interface §6.3). 기존 기록은 셋입니다. 거부 기록은 `$CANTON_STATE_DIR/requests/<P>/<run_id>.jsonl`에 응답하기 전에 써 둡니다(fsync). 확정 기록은 원장의 `TradeContract`(그 요청의 `requestId`·`actor`)입니다. 원장 응답을 잃은 제출 표시는 어댑터 프로세스 메모리에만 둡니다. 기록과 내용이 다르면 409 `REQUEST_ID_CONFLICT`, 같으면 기록된 결과에 `replayed=true`를 붙여 답합니다.
- 기록이 없으면 상태를 Daml choice와 같은 순서로 판정합니다. 소진된 권리도 `CONSUMED` 계약으로 남으므로 표의 둘째 줄(권리를 보유한 적 없음)과 셋째 줄(모두 소진됨)은 구분됩니다.

| 상태 | 거절 코드 |
|---|---|
| 그 묶음의 사전 합의가 없음, 제출 기관이 확인 당사자(seller·lender)가 아님, (대차) 별도 담보가 제공되지 않았거나 조건 미충족 | `PRECONDITION_NOT_MET` |
| 제출 기관이 그 묶음의 권리를 하나도 보유하지 않음. 앞 거래가 아직 반영되지 않았을 수 있어 1초 동안 50ms 간격으로 다시 읽은 뒤 판정합니다 | `PRECONDITION_NOT_MET` |
| 보유한 권리가 모두 소진됨 | `RIGHT_UNAVAILABLE` |
| `AVAILABLE` 권리의 `createdVersion` ≠ `expected_version` | `VERSION_CONFLICT` |
| 그 권리의 `allowedCommands`에 명령이 없음 | `USE_NOT_PERMITTED` |

- 합의나 `AVAILABLE` 권리가 둘 이상이면 준비 데이터가 모순이므로 503 `INTERNAL_ERROR`로 답합니다.
- 원장 제출의 `commandId`는 `confirm-<P>-` 뒤에 sha256(run_id|기관|request_id)의 앞 32자를 붙인 값입니다. Canton은 이미 받은 `commandId`를 거절합니다(2026-11-21 실측: 확정 뒤 다시 보내면 409 `DUPLICATE_COMMAND`, 처리 중에 보내면 409 `SUBMISSION_ALREADY_IN_FLIGHT`). 같은 요청을 다시 제출해도 두 번 확정되지 않는 것은 이 중복 제거 덕분입니다. 경합으로 거절된 `commandId`는 다시 쓸 수 있습니다(같은 날 실측).
- 원장 응답은 다음처럼 처리합니다.
  - 200: 확정입니다. 위 표의 이벤트를 남기고 COMMITTED로 답합니다. 이벤트를 쓰다가 실패하면 그 이벤트를 빼고 어댑터 출력에 남기며, 응답은 그대로 COMMITTED입니다. 응답 본문에서 이 요청의 `TradeContract`를 찾지 못하면 상태에서 찾습니다. 이때는 거절 코드를 판정하지 않으며, 찾지 못하면 아래 원장 무응답과 같이 처리합니다.
  - 원장이 처리하지 않았다고 답한 거절: 오류 본문의 `grpcCodeValue`가 INVALID_ARGUMENT(3)·NOT_FOUND(5)·ALREADY_EXISTS(6)·PERMISSION_DENIED(7)·FAILED_PRECONDITION(9)·ABORTED(10)·OUT_OF_RANGE(11)·UNIMPLEMENTED(12)·UNAUTHENTICATED(16)인 응답입니다(실측: 경합에서 진 쪽의 409 `LOCAL_VERDICT_LOCKED_CONTRACTS`는 10, 소진된 계약의 `CONTRACT_NOT_FOUND`는 5, Daml assert 실패 `DAML_FAILURE`는 9). 같은 run·묶음에서 제출 중인 다른 요청이 원장 응답을 받을 때까지 최대 2초 기다리고, 이어서 최대 2초 동안 상태를 다시 읽습니다. 이 요청의 `TradeContract`가 보이면 COMMITTED, 상태 판정이 거절 코드를 내면 그 코드로 REJECTED를 기록합니다. 둘 다 아니면 기록 없이 503 `TEMPORARILY_UNAVAILABLE`로 답합니다. 원장이 처리하지 않았다고 답했으므로 다시 보내도 안전합니다. 원장 오류 원문은 어댑터 출력에 남깁니다.
  - `DUPLICATE_COMMAND`·`SUBMISSION_ALREADY_IN_FLIGHT`: 앞 작업이나 다른 프로세스가 이 요청을 이미 제출했다는 뜻이므로 상태에서 이 요청의 `TradeContract`를 찾습니다. 찾지 못하면 아래 원장 무응답과 같이 처리합니다.
  - 원장 무응답(요청을 보낸 뒤 연결이 끊기거나 60초를 넘김)과 처리 여부를 알 수 없는 응답(위 `grpcCodeValue`가 아니거나 없는 오류 본문, JSON이 아닌 본문. 예: DEADLINE_EXCEEDED·INTERNAL·UNAVAILABLE): 원장이 처리했을 수 있으므로 상태를 최대 2초 다시 읽고, 설명되지 않으면 1초 쉰 뒤 같은 `commandId`로 다시 제출합니다. 첫 제출부터 10초가 지나도 설명되지 않으면 503 UNKNOWN(`OUTCOME_UNKNOWN`)으로 답하고 무응답 표시를 남깁니다. 연결 자체가 되지 않아 요청이 나가지 않았으면 503 `TEMPORARILY_UNAVAILABLE`입니다. 어댑터가 스스로 다시 제출한 횟수는 실행기의 외부 재시도와 구분하여 어댑터 출력(`$CANTON_STATE_DIR/logs/adapter-<P>.out`)에 남깁니다(measurement.md §7.2).
- 제출 응답은 최종 결과를 `CANTON_RESPONSE_WAIT`초(`env.example`)까지 기다립니다. 기본값 4.0초는 개발용 값이며, 공통 응답 대기 조건은 총괄이 Model A와 함께 확인한 뒤 정합니다. 그 안에 끝나지 않으면 202 PENDING으로 답하고 작업은 계속합니다. 클라이언트가 연결을 끊어도 작업은 계속되어 이벤트를 남깁니다. 끊긴 호출에는 보낸 응답이 없으므로 `COMMAND_FINISHED`를 남기지 않습니다.
- 결과 조회(`GET /runs/{run_id}/commands/{request_id}`)는 거부 기록이 있으면 REJECTED, `TradeContract`가 있으면 COMMITTED, 처리 중이면 202 PENDING, 무응답 표시가 있으면 503 UNKNOWN, 모두 없으면 404 `NOT_FOUND`로 답합니다. 원래 결과를 돌려주는 것이므로 `replayed=false`입니다.
- snapshot의 `pending_operations`에는 원장에 제출하고 응답을 기다리는 요청이 `{request_id, bundle_id, kind, blocks_use: true}`로 나옵니다. `kind`는 `PROCESSING`이고, 원장 무응답 뒤 다시 제출하는 중이거나 확정 응답을 읽지 못해 상태에서 찾는 중이면 `RECOVERING`입니다. Canton은 거래를 확인하는 동안 그 권리 계약을 잠급니다. 503 UNKNOWN으로 답한 요청도 원장이 아직 확정할 수 있는 동안 `RECOVERING`으로 남깁니다. 상태에 그 요청의 `TradeContract`가 없고 지금 상태로 다시 판정해도 거절 사유가 없는 경우입니다. 같은 요청을 다시 보내 판정이 끝나거나, 상태에 거래가 보이거나, 다른 거래가 그 권리를 쓰거나 바꿔 상태 판정에서 거절되면 빠집니다.
- 어댑터가 원장의 확정 응답을 받지 못하거나 읽지 못한 채 상태에서 확정을 찾은 요청에는 `INTERNAL_COMMITTED`·`RIGHT_*`·`LOCAL_APPLIED` 이벤트가 없습니다. 어댑터와 Participant 사이의 연결이 끊긴 경우, 앞서 제출한 어댑터 프로세스가 멈춘 경우, 처리 여부를 알 수 없는 오류 응답을 받은 경우가 여기에 해당합니다. 어댑터가 실제 확정 시각을 모르기 때문이며, 시각을 지어 채우지 않습니다(AGENTS.md §6, measurement.md §2.2). 그래서 이 요청은 공통 검증기의 `history_check`에서 `internal commit evidence missing`(INCONCLUSIVE)이 됩니다. 실행기가 HTTP 응답을 기다리다 연결을 끊는 경우는 여기에 해당하지 않습니다. 어댑터는 원장 응답을 그대로 받으므로 이벤트가 남습니다(AC-11 자체 시험에서 확인).
- 거부 기록 파일의 마지막 줄이 줄바꿈 없이 끊겨 있으면(기록을 쓰다가 어댑터가 멈춘 경우) 그 run의 기록을 처음 읽을 때 그 줄을 지웁니다. 줄 전체를 fsync한 뒤에야 답하므로 그 거부는 응답된 적이 없고, 같은 요청이 다시 오면 처음부터 판정합니다.
- 어댑터를 재기동하면 거부 기록(파일)과 확정(원장)은 그대로 조회됩니다. 처리 중 표와 무응답 표시는 메모리에만 있으므로 사라집니다. 그 뒤 무응답이었던 요청을 조회하면, 원장이 확정했으면 COMMITTED이고 확정하지 않았으면 404 `NOT_FOUND`입니다. 같은 요청을 다시 보내면 처음부터 판정하고, `commandId`가 같으므로 원장이 두 번 확정하지 않습니다.

## 4. 재현 절차 — 실제 명령 확인 후 작성

| 순서 | 반드시 기록할 내용 | 현재 상태 |
|---|---|---|
| 설치 | SDK·런타임·필수 의존성·환경 설정 | `brew install openjdk@21 postgresql@17 python@3.12` → `cp env.example .env`(경로만 조정) → `scripts/install-toolchain.sh` → `scripts/adapter.sh setup`(`.venv` 생성) → `scripts/adapter.sh tokens`(자격 토큰 생성, 아래 자격 설정). 모두 처음 한 번만 실행합니다 |
| 노드 기동 | S1·Participant·플랫폼 서비스의 순서·명령·주소 | `scripts/db.sh init`(처음 한 번) → `scripts/db.sh start` → `scripts/canton.sh start`(부트스트랩까지 대기, 약 60초) → `scripts/adapter.sh start`(X `http://127.0.0.1:8201`, Y `http://127.0.0.1:8202`) |
| 계약 준비 | 빌드·패키지·Party·권한·필요 연결 준비 | Party와 호스팅은 Canton 부트스트랩이 만듭니다. `adapter.sh start`가 DAR(`collateral` 0.3.0, 업무 템플릿 포함)를 빌드하고, 각 어댑터가 기동할 때 DAR를 올린 뒤 예열 거래를 두 번 실행합니다 |
| 자격 설정 | 기관·관측기와 Party·API 권한 연결. 비밀값 제외 | 어댑터를 처음 기동하기 전에 실행합니다(설치 순서의 마지막). 토큰 파일이 없으면 `scripts/adapter.sh start`가 기동하지 않고 멈춥니다. `scripts/adapter.sh tokens`가 `POC_BANK_A_TOKEN`·`POC_BANK_B_TOKEN`·`POC_BANK_C_TOKEN`·`POC_OBSERVER_TOKEN`을 `$CANTON_STATE_DIR/secrets/tokens.env`(권한 600)에 한 번 만듭니다. `scripts/env.sh`를 source하면 환경변수로 들어갑니다. 실행기 설정의 `auth_env`와 이름이 같습니다 |
| 새 데이터 준비 | 새 run_id와 fixture 경로로 초기 자산·합의 준비 | 준비 프로그램은 총괄이 작성합니다. 계약 생성 방법은 §3 끝에 있고, `selftest/xy_selftest.py`의 `prepare()`가 같은 방법으로 새 run_id의 S0 계약을 만듭니다 |
| 기능 확인 | 실제 Repo·증권대차·배정·readiness·결과 조회·snapshot | `selftest/xy_selftest.py`가 새 run_id로 S0 → Y readiness → X Repo → S1 → Y readiness → Y 증권대차 → S2 → 결과 조회 → 이벤트 이력을 실제 Canton에서 확인합니다. `selftest/xy_stage3_selftest.py`는 공통 실행기의 X·Y 사례(AC-04~09, AC-11, `runner.case_plan("B1", "xy")`)를 같은 fixture·반복 수로 실행합니다. CCP 배정은 Z(4단계) |
| 자체 테스트 | 실제 Canton 기능·권한·경합·의무 유지 검증 명령 | `. scripts/env.sh` → `(cd daml/stage1-probe && dpm build)`(확인용 DAR 빌드. 저장소에는 DAR가 없습니다) → `python3.12 -I selftest/stage1_probe.py`(Participant 간 가시성·제출 권한·경합)와 `.venv/bin/python -I selftest/adapter_selftest.py`(어댑터 인증·권한·오류 응답·이벤트 로그), `.venv/bin/python -I selftest/xy_selftest.py`(X→Y 정상 흐름), `.venv/bin/python -I selftest/authz_check.py`(권리 서명자·단독 제출 거절·Party별 가시성, §3 확정 거래와 가시성). 3단계 시험은 실행기와 같은 저장소 루트 `.venv`로 실행합니다. 처음 한 번 저장소 루트에서 `python3.12 -m venv .venv && .venv/bin/python -m pip install -r tests/conformance/requirements.txt`를 실행하고, 이 폴더에서 `../../.venv/bin/python -I selftest/xy_stage3_selftest.py`(X→Y 거절·중복·경합·복구, 33건 약 2분)를 실행합니다. 테스트 ID(`AC-08` 등)를 인수로 주면 그 사례만 실행하고 보고서에 선택한 ID를 적습니다. 보고서는 `logs/stage1/`, `logs/adapter-selftest/`, `logs/xy-selftest/`, `logs/authz-check/`, `logs/xy-stage3-selftest/` |
| 공통 테스트 | 총괄 제공 실행기 연결·실행 결과 | NOT_RUN. 총괄이 초기 적재와 로그 수집을 연결한 뒤 함께 실행합니다. 자체 테스트는 판정에 실행기의 `tests/conformance/checks.py`(`initial_state`, `state_check`, `check_result`, `history_check`)를 그대로 씁니다 |
| 로그 확인 | 내부 확정·로컬 반영·업무 ID·Contract ID·시계 연결 | 어댑터 이벤트 `logs/<run_id>/events.jsonl`, 어댑터 출력 `$CANTON_STATE_DIR/logs/adapter-<P>.out`, 노드 로그 `$CANTON_STATE_DIR/logs/canton.log`. 업무 ID(`record_id`, `right_id`)와 실제 Contract ID(`contract_id`)는 같은 이벤트에 함께 적습니다(§3 끝) |
| 종료·정리 | 노드 종료·데이터 정리·주의사항 | `scripts/adapter.sh stop` → `scripts/canton.sh stop` → `scripts/db.sh stop`. `scripts/db.sh wipe --yes`는 노드 DB를 지우고 다시 만들며, 어댑터의 거부 기록(`$CANTON_STATE_DIR/requests`)도 지웁니다. 어댑터와 Canton을 멈춘 뒤 실행합니다. 되돌릴 수 없고, 다음 기동 때 Party 식별자가 바뀝니다 |
| B2 실행 | S2 준비·대상 계약·재할당·이벤트 수집 방법 | B2 착수 후 작성 |

새 실행 준비는 기존 진행 중인 run_id를 덮어쓰지 않습니다. 실제 발생한 확정·로컬 반영·재할당 이벤트만 기록하고 다른 프로세스의 단조 시계 값을 직접 빼지 않습니다.

## 5. 검증 결과·인계 — 개발 중 갱신

- 현재 단계·구현한 기능·코드 기준: 1단계 완료(2026-11-18). 2단계는 공유 시점 3(X→Y 거절·중복·경합·복구 검증 준비)까지 구현했습니다(2026-11-21, 시점 2 커밋 `a87cd3e` 위). 2026-11-22에는 총괄이 승인한 범위에서 `UseRight`에 공동 서명자를 추가하고, 패키지 이름 `collateral`을 유지한 채 버전을 0.3.0으로 올렸습니다(§3 확정 거래). 개발 중 공유용이며 X→Y 기준 버전이 아닙니다.
- X→Y 기준 버전과 확인한 테스트:
- Z 추가 변경 목록·사유·메시지·예외 경로:
- 자체 및 공통 테스트의 테스트 ID·run_id·PASS/FAIL/NOT_RUN·로그: 2026-11-22 `collateral` 0.3.0으로 자체 테스트를 다시 실행했습니다. 기존 원장과 증거를 보존하려고 별도 `CANTON_STATE_DIR`와 PostgreSQL 포트로 띄운 새 원장에서 실행했습니다. `authz_check.py` 배치 `52057beb` 21건 통과(run_id `authz-52057beb-r1`·`authz-52057beb-r2`, 보고서 `logs/authz-check/52057beb.json`), `xy_stage3_selftest.py` 배치 `906f1552` 33건 통과(AC-04 3, AC-05 2, AC-06 2, AC-07 1, AC-08 10, AC-09 10, AC-11 5. run_id `xy-stage3-906f1552-01`~`33`, 보고서 `logs/xy-stage3-selftest/906f1552.json`). 같은 코드로 회귀 `xy-selftest-9ff80e9dcbee` 12건, `selftest-474e6444225f` 42건, `stage1-20261122T031525Z-0a9b` 29건 통과. 아래 2026-11-21 결과는 0.2.0과 기존 원장 기준입니다. 2026-11-21 자체 테스트 `xy_stage3_selftest.py` 배치 `23d130cf` 33건 통과(AC-04 3, AC-05 2, AC-06 2, AC-07 1, AC-08 10, AC-09 10, AC-11 5. run_id `xy-stage3-23d130cf-01`~`33`, 보고서 `logs/xy-stage3-selftest/23d130cf.json`). 같은 코드로 회귀 `xy-selftest-056be28701e1` 12건, `selftest-e18d441b3d42` 42건, `stage1-20261121T121000Z-d0f5` 29건 통과. 새 시험은 공통 실행기의 X→Y 목록 50건 가운데 AC-04~09·AC-11의 33건을 `tests/conformance/checks.py`의 판정 함수로 확인한 것이며, AC-01~03 17건은 기존 자체 시험이 다른 형식으로 확인합니다. 공통 테스트는 NOT_RUN입니다(총괄의 연결 코드 PR #6은 시점 2 `a87cd3e` 기준이며 실제 Canton에서 아직 실행하지 않았습니다. right:0의 `cosigner`는 총괄이 연결 코드에 반영합니다).
- Party·Participant·계약 가시성과 결과 정규화 방식: §3 끝의 가시성 표와 snapshot 변환.
- 응답 지연 후 원래 결과 확인·안전한 재전송의 재현 방법: `../../.venv/bin/python -I selftest/xy_stage3_selftest.py AC-11`. 공통 실행기의 `fault_proxy.ResponseRelay`를 Y 어댑터 앞에 두고 Y `CONFIRM_LOAN`의 응답을 10초 붙잡습니다. 실행기와 같은 5초 HTTP 한도에서 호출이 시간 초과되면 결과 조회(GET)로 원래 결과 COMMITTED를 확인하고, 같은 요청을 다시 보내 COMMITTED·`replayed=true`와 S2 상태를 확인합니다. 붙잡혔던 첫 응답도 200 COMMITTED여야 합니다. 2026-11-21 배치 `23d130cf`에서 5회 모두 이 순서로 끝났고, 확정 이벤트(`INTERNAL_COMMITTED`)는 그 요청에 한 건만 남았습니다.
- B2 필수 테스트 통과 결과·계약별 재할당·사용 불가 구간·그 밖의 테스트 수행 여부:
- 알려진 제약·명세 충돌·총괄이 재현할 환경·남은 조치: 권리의 두 서명자가 함께 제출하는 경로는 막지 않습니다(§3 신뢰 범위). 총괄 PR #6 §18이 적은 단독 변경 제약(거래 없이 권리·버전을 바꿀 수 있음)은 2026-11-22 공동 서명자 추가로 막았습니다. 0.3.0은 `UseRight`와 `Consume` 인자에 Optional이 아닌 필드를 더했으므로 0.2.0의 업그레이드가 아닙니다. `dpm upgrade-check --both`가 `NOT_VALID_UPGRADE_PACKAGE`로 판정했고(`logs/authz-check/upgrade-check-0.2.0-to-0.3.0.txt`), 이 검사기는 Participant가 DAR를 올릴 때 쓰는 `PackageUpgradeValidator`입니다. 그래서 0.2.0이 올라간 원장에는 0.3.0을 올릴 수 없다고 보며, 0.3.0은 0.2.0이 없는 새 원장에서 실행합니다(기존 원장에 올리는 시도는 하지 않았습니다). 0.3.0에서는 P_X에만 호스팅된 BANK_A가 대차 거래에서 소진되는 right:1의 서명자이므로, Y `CONFIRM_LOAN` 확정에 P_X의 확인도 필요하다고 추정합니다(Canton 확인 규칙에 따른 추정이며 실측하지 않았습니다). 이 추정이 맞으면 P_X가 응답하지 않을 때 Y 확정도 끝나지 않습니다. 원장이 거절했지만 어댑터가 읽은 상태로 까닭을 설명하지 못하는 경우(§3의 B·C 공동 생성 권리)는 503 `TEMPORARILY_UNAVAILABLE`로 답합니다. 이런 상태는 어댑터를 거친 명령으로는 생기지 않고, 두 기관이 Ledger API로 함께 제출할 때만 생깁니다. 읽기마다 호스팅 Party의 ACS 전체를 읽으므로 원장에 run이 쌓일수록 느려집니다. 명령은 판정 전에 ACS 전체를 한 번 읽고, 확정 뒤 `LOCAL_APPLIED`를 확인할 때 최대 2초 동안 50ms 간격으로 다시 읽은 뒤에 답합니다. 거절 경로에서도 권리가 보이지 않으면 최대 1초, 원장이 거절하면 다른 제출의 응답을 최대 2초 기다린 뒤 최대 2초 동안 상태를 다시 읽습니다. 명령 응답이 이 읽기를 기다리므로, 공통 실행기가 측정하는 응답 시간에도 이 비용이 들어갑니다. 이 비용은 따로 재지 않았습니다. 지금은 최적화하지 않고 현재 동작과 측정 영향만 적어 둡니다. `INTERNAL_COMMITTED` 시각은 어댑터가 확정 응답을 받은 시각입니다. `snapshot_seq`는 재기동하면 0부터 셉니다. 어댑터와 Participant 사이의 `MESSAGE_SENT`·`MESSAGE_RECEIVED`는 기록하지 않습니다. readiness는 원장 상태만 읽으므로 처리 중인 요청을 반영하지 않습니다. 구현했지만 자체 시험에서 일어나지 않은 분기는 202 PENDING(33건 모두 응답 대기 4.0초 안에 답했습니다), 원장 무응답 뒤 재제출과 `RECOVERING`, 503 UNKNOWN, 연결 실패의 503 `TEMPORARILY_UNAVAILABLE`, 준비 데이터 모순의 503 `INTERNAL_ERROR`, 처리 여부를 알 수 없는 원장 오류, 확정 뒤 이벤트 기록 실패와 확정 응답을 읽지 못한 경우, 끊긴 거부 기록 줄, 503 UNKNOWN 요청의 `RECOVERING` 표시, 재기동 뒤 조회입니다. 프로세스 재기동 복구 시험(§3에 적은 재기동 동작)은 필수 공통 검증과 구분되는 추가 검증이며, 아직 실행하지 않았습니다. 응답 대기 4.0초(`CANTON_RESPONSE_WAIT`)는 개발용 값입니다. interface §6.2가 응답 대기 한도를 모든 모델에 같게 실험 조건에서 확정한다고 정하므로, 공통 조건은 총괄이 Model A와 함께 확인한 뒤 정합니다.

완료 기준은 [acceptance.md](../../spec/acceptance.md)를 따릅니다. B1의 AC-01~11과 후속 B2의 AC-01·02·12를 공통 실행기로 모두 PASS하고 증빙을 갖춰야 최종 개발 완료로 제출·인계할 수 있습니다. 필수 항목의 FAIL·INCONCLUSIVE·NOT_RUN·증거 누락은 인계 불가이며, 실행기 미준비나 자체 테스트만 수행한 상태도 최종 완료가 아닙니다. 공통 명세·기대 결과를 바꾸어 통과시키지 않습니다. 커밋·푸시는 실행 전에 사용자에게 확인받습니다.
