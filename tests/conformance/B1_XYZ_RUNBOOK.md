# B1 0.4.0 공통 XYZ 연결

작성: 2026-11-23. 대상은 Draft PR #9 `adc9837b1fc78f2e468ddafece766ebfc3630f5b`와
총괄의 XYZ 적재 연결이다. 실제 Canton 공통 XYZ 검증은 아직 NOT_RUN이다.
연결 버전은 이 안내가 포함된 `feat/b1-z-integration`의 실제 커밋으로 고정하여 실행한다.
기존 A 연결 PR #10과 B1 Z 구현 PR #9의 병합·완료 승인은 별도다.

## 1. 실행할 환경

- 기존 B1 XY 기준 main `204eb5e`와 원장·증거는 보존한다.
- 별도 checkout에서 PR #9의 `collateral` 0.4.0, P_X·P_Y·P_Z·S1인
  `b1-xyz`를 준비한다. PR #9 구현 README §4의 새 상태 디렉터리·새 DB 절차를 따른다.
  이 연결을 위해 기존 XY 원장을 업그레이드·삭제하거나 BANK_C 호스팅을 변경하지 않는다.
- 기존 스택과 Canton/API 포트가 겹치면 정상 종료 절차로 조정한다. 다른 프로세스를
  강제 종료하지 않는다. DB 포트만 변경해서는 Canton/API 충돌이 해소되지 않는다.
- `$CANTON_STATE_DIR/ids.json`에 `topology=b1-xyz`, `parties.CCP`,
  `json_api.P_Z`가 필요하다. C은행은 P_Y/P_Z, CCP는 P_Z에 호스팅돼야 한다.
  XYZ 적재기는 topology가 다르면 제출 전에 실패한다.
- 어댑터는 X 8201·Y 8202·Z 8203, JSON Ledger API는 P_X 4513·P_Y 4523·P_Z 4533이다.
  준비 프로그램은 로컬 무인증 Ledger API만 사용하며 외부 주소를 허용하지 않는다.

## 2. 설정과 실행

기존 작업 폴더 대신 별도 checkout에서 두 소스를 조합한다. 아래 브랜치가 게시된 뒤
실행하며 `B1_Z_CONNECTION_COMMIT`의 실제 값을 결과에 남긴다. 실행자에게 커밋·푸시·병합은
필요하지 않다. 기존 폴더의 미커밋 변경 위에서 `git restore`를 실행하지 않는다.

```sh
B1_Z_RUN_ROOT="$(mktemp -d "$HOME/b1-common-xyz.XXXXXX")"
export B1_Z_RUN_ROOT
git clone --branch feat/canton-b1-z \
  https://github.com/HYBLOCK-LAB/collateral-mobility-poc.git "$B1_Z_RUN_ROOT/repo"
cd "$B1_Z_RUN_ROOT/repo"
git fetch origin feat/b1-z-integration
B1_Z_CONNECTION_COMMIT="$(git rev-parse FETCH_HEAD)"
printf 'B1 Z connection commit: %s\n' "$B1_Z_CONNECTION_COMMIT"
git switch --detach adc9837b1fc78f2e468ddafece766ebfc3630f5b
git restore --source="$B1_Z_CONNECTION_COMMIT" --worktree -- \
  implementations/canton/prepare.py \
  tests/conformance/test_canton_connection.py \
  tests/conformance/config.b1.xyz.example.json \
  tests/conformance/B1_XYZ_RUNBOOK.md \
  tests/conformance/README.md \
  tests/conformance/EXPERIMENT_HANDOFF.md
```

준비된 새 checkout에서 `.venv`에 공통 실행기 의존성을 설치하고 실제 구현·연결 커밋을
기록한다. 그 checkout의 `implementations/canton/scripts/env.sh`로 같은 상태 경로와
네 가지 API 자격 환경변수를 불러온다. 비밀값은 기록하거나 커밋하지 않는다.

```sh
source implementations/canton/scripts/env.sh
cp -n tests/conformance/config.b1.xyz.example.json tests/conformance/config.b1.xyz.local.json
```

로컬 설정의 Python 경로·API 주소를 확인한다. 어댑터의 `CANTON_EVIDENCE_DIR`를
별도로 지정했다면 설정의 `evidence_dir`도 같은 실제 경로로 맞춘다.
`prepare_command`의 마지막 `xyz`가 Z 적재를 선택한다. 기본 XY 설정은 그대로 유지한다.

```sh
.venv/bin/python tests/conformance/runner.py prepare \
  --config tests/conformance/config.b1.xyz.local.json --observer-auth bearer \
  --platforms X Y Z --fixture shared/fixtures/initial/f0-normal.json

.venv/bin/python tests/conformance/runner.py check \
  --config tests/conformance/config.b1.xyz.local.json --observer-auth bearer \
  --stage xyz --case AC-01 AC-02

.venv/bin/python tests/conformance/runner.py check \
  --config tests/conformance/config.b1.xyz.local.json --observer-auth bearer \
  --stage xyz
```

각 명령의 종료 코드와 출력 배치 경로를 보존한다. 준비나 초기 필터 검증이 실패하면
먼저 원인을 확인한다. 기대 결과·필수 반복 수를 바꾸거나 실패 실행을 덮어쓰지 않는다.
`--final`은 버전·환경·XY 기준·Z 변경 기록과 원본 증빙 확인 후 별도로 실행한다.

## 3. 적재·수집 계약

기존 X·Y S0 CreateCommand는 유지한다. `xyz`에서는 모든 선택 Participant의 ACS에서
run_id 중복을 확인한 뒤 로컬 독점 claim을 남기고 P_X→P_Y→P_Z 순서로 제출한다.

P_Z는 `actAs=[BANK_C, CCP]`로 fixture의 모든 `margin_calls[]`를 한 제출 안에서
`#collateral:Collateral.Margin:MarginCall`로 만든다. `wholeBundleAccepted`,
`eligibilityPassed`, `status=OPEN`과 개별 ID를 그대로 보존한다. 다중 margin call도
하나만 고르거나 합치지 않는다. Allocation·잠긴 권리·확정 거래는 생성하지 않는다.

일부 제출 성공 후 실패·타임아웃이면 claim과 이미 생성된 계약을 보존한다. 같은 run_id를
재사용하거나 적재를 성공으로 표시하지 않는다. 전체 준비가 성공한 뒤에만 실제
`events.jsonl`을 빈 파일로 초기화하며 기존 파일은 자르지 않는다.

세 어댑터가 `<evidence_dir>/<run_id>/events.jsonl`에 직접 추가하므로 별도 DB 수집기는
필요 없다. 공통 실행기가 이 파일을 보존·검사한다. Y/Z에서 같은 margin call이나
allocation이 보이는 것은 BANK_C 공동 호스팅에 따른 가시성이다. 테스트를 통과시키기
위해 정상 snapshot에서 해당 행을 삭제하지 않는다.

## 4. 검증 경계와 다음 인계

총괄 로컬에는 이 버전의 Canton·Daml SDK·JDK 21·PostgreSQL 17 환경이 없으므로
이번 수정의 실제 원장 제출과 공통 XYZ는 NOT_RUN이다. 적재기 자체 시험의 모의 Ledger API
통과를 실제 B1 통과로 표시하지 않는다.

양하님은 Z 거절·중복·경합·복구 자체 검증을 같은 Draft PR에서 이어간다. 총괄 연결을
공유한 뒤 위 명령으로 실제 환경에서 준비→정상·readiness→전체 XYZ를 확인할 수 있다.
구현/연결 커밋·run_id·실행 명령·판정·snapshot·events와 manifest를 함께 보존한다.
공통 검증으로 확인한 범위와 추가 재시작 등 자체 시험 범위를 구분한다.

원장 호출 MESSAGE_SENT/RECEIVED가 아직 없으므로 전체 업무 메시지 수는 미계측이다.
이를 0으로 기록하지 않는다. B2는 A·B1 최종 검증·계측 확보 후 착수한다.
