# B1 0.3.0 공통 X→Y 검증 실행 안내

작성: 2026-11-22. 양하님의 정상 Canton 환경에서 총괄의 초기 적재·공통 실행기를 연결하는 절차다. 실제 실행 전 작성한 안내이며 이 명령으로 PASS를 확인한 기록은 아직 없다. PR 병합·기존 원장 초기화·Z 착수는 하지 않는다.

**첫 실행 결과와 후속 수정:** 초기 연결본 `8452122`에서는 prepare와 정상 X→Y, ready-Y-1이 통과했지만 ready-Y-0은 이벤트 파일 누락으로 INCONCLUSIVE였다. 아래는 준비 성공 시 빈 업무 로그를 초기화하는 후속 연결본 기준이다. 제안 브랜치 `feat/b1-readonly-evidence`가 실제로 공유된 뒤 실행하고, 가져온 실제 커밋을 기록한다. 수정본의 실제 Canton 재검증은 아직 NOT_RUN이다.

## 1. 대상과 실행 장소

- 실행자·장소: Canton 3.7.3 / dpm 1.1.1 / SDK 3.7.1 / JDK 21 / PostgreSQL 17 / Python 3.12가 설치된 양하님 macOS 환경.
- 구현: PR #5 `1d369e1c6931d255c1baa4715566cec43074d56d`, `collateral` 0.3.0.
- 총괄 연결: `feat/b1-readonly-evidence`에서 가져온 실제 커밋. PR #6은 병합됐으므로 이 후속 수정은 별도 PR로 공유한다.
- 두 커밋을 별도 clone에서 조합한다. 업무 계약·어댑터는 PR #5 그대로 두고 총괄의 후속 연결 파일 네 개만 작업 트리에 가져온다. 검증 실행자에게 커밋·푸시·병합은 필요하지 않다.
- 기존 0.2.0 원장뿐 아니라 0.3.0 자체 시험 원장도 보존한다. 이번 절차는 **별도 새 상태 폴더·새 DB 클러스터**를 사용한다. 이전 Party ID·토큰을 복사하지 않는다.
- 각 블록은 같은 터미널에서 순서대로 실행한다. 명령 실패 시 그 지점에서 중단하고 결과를 공유한다. 성공으로 간주해 다음 단계로 넘어가지 않는다.

## 2. 검증용 checkout 준비

`mktemp`로 홈 아래 고유 폴더를 만든다. 임시 시스템 폴더가 아니므로 결과를 보존하기 쉽다. 아래 폴더는 검증·공유가 끝날 때까지 삭제하지 않는다.

```sh
B1_RUN_ROOT="$(mktemp -d "$HOME/b1-common-xy.XXXXXX")"
export B1_RUN_ROOT
git clone --branch feat/canton-b1-xy \
  https://github.com/HYBLOCK-LAB/collateral-mobility-poc.git "$B1_RUN_ROOT/repo"
cd "$B1_RUN_ROOT/repo"
git fetch origin feat/b1-readonly-evidence
B1_CONNECT_COMMIT="$(git rev-parse FETCH_HEAD)"
printf 'B1 connection commit: %s\n' "$B1_CONNECT_COMMIT"
git switch --detach 1d369e1c6931d255c1baa4715566cec43074d56d
git restore --source="$B1_CONNECT_COMMIT" --worktree -- \
  implementations/canton/prepare.py \
  tests/conformance/runner.py \
  tests/conformance/config.b1.example.json \
  tests/conformance/test_canton_connection.py
git diff --stat
```

이 clone의 네 연결 파일이 미커밋으로 표시되는 것은 의도된 상태다. 기존 작업 폴더를 바꾸지 않는다. 커밋을 찾을 수 없으면 중단하고 공유한다. 최신 브랜치로 임의 대체하지 않는다.

## 3. 환경과 포트 확인

기존 스택이 켜져 있으면 원래 작업 폴더·환경의 정상 종료 절차로 먼저 멈춘다. 다른 프로세스를 강제로 종료하거나 원장을 wipe하지 않는다. DB 포트만 바꿔도 Canton/API 고정 포트는 공유되므로 두 스택을 동시에 띄우지 않는다.

```sh
lsof -nP -iTCP -sTCP:LISTEN | \
  awk 'NR == 1 || /:(4401|4402|4412|4511|4512|4513|4521|4522|4523|8201|8202|5436)([[:space:]]|$)/'
```

해당 포트가 사용 중이면 원인을 확인하고 비워진 뒤 진행한다. 5436은 이번 절차의 DB 포트이며 충돌하면 사용 가능한 별도 포트로 바꾸고 실제 값을 기록한다.

```sh
export CANTON_STATE_DIR="$B1_RUN_ROOT/state"
export CANTON_PG_PORT=5436
export CANTON_EVIDENCE_DIR="$B1_RUN_ROOT/repo/implementations/canton/logs"
export CANTON_RESPONSE_WAIT=4.0
source implementations/canton/scripts/env.sh
command -v dpm
java -version
"$CANTON_PG_BIN/postgres" --version
python3.12 --version
```

4.0초는 기존 자체 시험과 같은 개발용 값이며 공통 실험 조건의 최종 승인값이 아니다. 이 clone에는 `.env`가 없다. 기존 설치가 기본 경로와 다르면 `CANTON_TOOLS_DIR`, `CANTON_JAVA_HOME`, `CANTON_PG_BIN`, `CANTON_PYTHON`의 실제 경로를 설정하고 `env.sh`를 다시 읽는다. 기존 `.env`·비밀값을 통째로 복사하지 않는다.

## 4. 새 원장 기동과 자격 설정

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r tests/conformance/requirements.txt
implementations/canton/scripts/adapter.sh setup
implementations/canton/scripts/db.sh init
implementations/canton/scripts/adapter.sh tokens
source implementations/canton/scripts/env.sh
implementations/canton/scripts/canton.sh start
implementations/canton/scripts/adapter.sh start
```

`db.sh init`은 새 클러스터를 만들고 기동한다. 이미 존재한다는 오류가 나면 wipe로 해결하지 말고 상태 폴더가 이번 고유 폴더인지 확인한다. `adapter.sh start`가 0.3.0 DAR를 빌드·업로드하고 API를 준비한다. 빌드·업로드·기동 오류는 그대로 보존한다.

`env.sh`는 새 상태 폴더의 `postgres.env`와 `tokens.env`를 읽어 자격을 설정한다. 비밀값을 터미널·댓글·결과 파일에 출력하지 않는다. 실제 Party ID와 Ledger API 주소는 새 원장의 `ids.json`에서 적재기가 읽는다.

```sh
cp tests/conformance/config.b1.example.json tests/conformance/config.b1.local.json
.venv/bin/python - <<'PY'
import json
import os
from pathlib import Path
p = Path('tests/conformance/config.b1.local.json')
config = json.loads(p.read_text())
config['evidence_dir'] = os.environ['CANTON_EVIDENCE_DIR']
p.write_text(json.dumps(config, indent=2) + '\n')
required = ['POC_BANK_A_TOKEN', 'POC_BANK_B_TOKEN', 'POC_BANK_C_TOKEN', 'POC_OBSERVER_TOKEN']
assert all(os.environ.get(name) for name in required), 'Required token environment variable missing'
print('Bearer credentials present; values not printed')
PY
```

설정은 X=8201, Y=8202, Z=8203인 예시를 사용한다. X/Y만 검사하므로 Z 기동은 필요 없다. `prepare_command`는 루트 `.venv/bin/python implementations/canton/prepare.py`이며 Bearer 환경변수는 위 네 개를 사용한다. 이벤트 경로는 기동한 어댑터와 일치시킨다.

## 5. 초기 적재 → 정상·readiness → X→Y 전체

저장소 루트에서 실행한다. 각 명령은 새 배치·run_id를 만들며, 준비 전용 실행의 run_id를 재사용하지 않는다. 실제 처리·이벤트는 Canton에서 나온 것만 사용한다. A용 `--collect-command`는 붙이지 않는다.

```sh
.venv/bin/python tests/conformance/runner.py prepare \
  --config tests/conformance/config.b1.local.json --observer-auth bearer \
  --fixture shared/fixtures/initial/f0-normal.json --platforms X Y \
  --output "$B1_RUN_ROOT/results"

.venv/bin/python tests/conformance/runner.py check \
  --config tests/conformance/config.b1.local.json --observer-auth bearer \
  --stage xy --case AC-01 AC-02 --output "$B1_RUN_ROOT/results"

.venv/bin/python tests/conformance/runner.py check \
  --config tests/conformance/config.b1.local.json --observer-auth bearer \
  --stage xy --output "$B1_RUN_ROOT/results"
```

앞의 두 명령이 PASS인지 확인한 뒤 전체를 실행한다. X→Y 전체 목록은 현재 `case_plan('B1', 'xy')`의 50건이다. AC-01도 X→Y 부분 확인이며 Z를 포함한 AC 전체나 최종 완료를 의미하지 않는다. `--final`은 붙이지 않는다. `final_handoff_eligible=false`는 이 부분 검증에서 의도된 표시다.

FAIL·INCONCLUSIVE·준비 실패가 나오면 종료 코드, 출력된 배치 경로, 해당 `checks.json`과 오류를 공유한다. 설정이나 기대값을 바꿔 통과시키지 않는다.

## 6. 종료와 공유

```sh
implementations/canton/scripts/adapter.sh stop
implementations/canton/scripts/canton.sh stop
implementations/canton/scripts/db.sh stop
```

이 새 checkout에서 기동한 것만 종료한다. 상태·결과·로그는 삭제하지 않는다.

공유할 항목:

- 실제 실행 코드의 두 커밋, 연결한 네 파일, 실행 환경·포트·응답 대기 설정.
- prepare, AC-01/02, X→Y 전체 각각의 종료 코드·배치 경로·건수·판정·미완료 항목.
- `$B1_RUN_ROOT/results/` 아래 전체 배치 원본. Manifest·checks·관측 이벤트·run별 응답/snapshot/구현 이벤트를 포함한다.
- `$CANTON_EVIDENCE_DIR`의 해당 run 이벤트, 필요시 어댑터 출력·Canton 로그. 공유 전 비밀값 포함 여부를 확인한다. `state/secrets`나 원장 DB 자체는 ZIP에 넣지 않는다.

ZIP·파일 공유 위치는 사람이 선택하며 자동 업로드하지 않는다. 원본 증거를 보존한 채 공유용 사본을 만들고 Manifest와 SHA-256을 첨부한다. 총괄이 원본과 판정을 확인한 뒤 X→Y 기준 보존·PR 병합·Z 착수를 별도로 판단한다.

## 7. 첫 실행 환경을 보존한 재검증

이미 0.3.0 전용 검증 checkout·상태·자격이 있다면 새 원장을 다시 만들 필요는 없다. 그 checkout의 저장소 루트에서 아래처럼 연결본만 갱신한 뒤 **기존에 사용한 상태 폴더·DB 포트·이벤트 경로**로 환경을 불러온다. 첫 실행의 INCONCLUSIVE 배치나 누락 파일을 수동으로 수정하지 않는다.

```sh
git fetch origin feat/b1-readonly-evidence
B1_CONNECT_COMMIT="$(git rev-parse FETCH_HEAD)"
printf 'B1 connection commit: %s\n' "$B1_CONNECT_COMMIT"
git restore --source="$B1_CONNECT_COMMIT" --worktree -- \
  implementations/canton/prepare.py \
  tests/conformance/runner.py \
  tests/conformance/config.b1.example.json \
  tests/conformance/test_canton_connection.py
source implementations/canton/scripts/env.sh
implementations/canton/scripts/db.sh start
implementations/canton/scripts/canton.sh start
implementations/canton/scripts/adapter.sh start
```

위 명령 전 원래 `CANTON_STATE_DIR`, `CANTON_PG_PORT`, `CANTON_EVIDENCE_DIR`, 개발용 응답 대기 설정을 같은 터미널에 다시 설정한다. 경로는 자신의 첫 실행 기록에서 확인하고 다른 사람의 경로를 복사하지 않는다. 기존 로컬 config와 준비 명령·자격·이벤트 경로도 대조한다. 새 터미널이면 첫 실행의 `B1_RUN_ROOT`도 실제 검증 폴더로 다시 설정한다.

그다음 제5절을 다시 실행한다. 모든 명령은 새 run_id·새 배치를 생성한다. prepare → AC-01·02를 먼저 확인하고 통과했을 때만 X→Y 전체 50건을 실행한다. 종료·공유는 제6절을 따른다. 빈 로그의 초기화는 준비가 성공한 **새 실행의 수명주기 처리**이며, 이미 실패한 실행의 증거를 사후 작성하거나 누락 이력을 PASS로 바꾸는 절차가 아니다.
