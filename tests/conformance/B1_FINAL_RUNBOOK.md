# B1 최종 공통 검증 실행 준비

2026-11-23. **게시할 연결 커밋이 확정된 뒤 실행한다.** 이 문서는 실제 최종 PASS가 아니다.
구현은 PR #9 `4b827d661551ef63033b76da401b273135aa2bdf`, 연결은 이 문서가 포함된
PR #11의 실제 커밋으로 고정한다. 과거 4·71 PASS를 최종 검증으로 소급하지 않는다.
자체 검사기·공통 판정 보완을 포함한 한 조합으로 준비와 전체 `--final`을 수행한다.
PR #10의 빈 이력·잘못된 명령/결과 증거 판정 보완은 이 연결의 runner.py/checks.py에도
반영했다. A 전용 적재·수집 파일을 B1에 적용할 필요는 없다.

## 1. 별도 checkout과 소스 조합

양하님이 확인한 macOS arm64 네이티브 환경에서 실행한다. 기존 XY·Z 상태·로그와
미커밋 작업을 보존한다. 아래 경로는 이번 검증 전용이다. git commit/push/merge는 하지 않는다.

```sh
B1_FINAL_ROOT="$(mktemp -d "$HOME/b1-final.XXXXXX")" || exit 1
export B1_FINAL_ROOT
git clone --branch feat/canton-b1-z \
  https://github.com/HYBLOCK-LAB/collateral-mobility-poc.git "$B1_FINAL_ROOT/repo" || exit 1
cd "$B1_FINAL_ROOT/repo" || exit 1
git fetch origin feat/b1-z-integration || exit 1
B1_FINAL_CONNECTION_COMMIT="$(git rev-parse FETCH_HEAD)"
export B1_FINAL_CONNECTION_COMMIT
git switch --detach 4b827d661551ef63033b76da401b273135aa2bdf || exit 1
git restore --source="$B1_FINAL_CONNECTION_COMMIT" --worktree -- \
  AGENTS.md README.md spec/business.md spec/interface.md spec/acceptance.md spec/measurement.md \
  implementations/canton/prepare.py \
  tests/conformance/runner.py tests/conformance/checks.py tests/conformance/report.py \
  tests/conformance/test_runner.py tests/conformance/test_history.py \
  tests/conformance/test_report.py tests/conformance/test_canton_connection.py \
  tests/conformance/test_reassignment.py tests/conformance/test_batch.py \
  tests/conformance/test_execution.py \
  tests/conformance/b1_final_context.py tests/conformance/test_b1_final_context.py \
  tests/conformance/config.b1.xyz.example.json \
  tests/conformance/B1_XYZ_RUNBOOK.md tests/conformance/B1_FINAL_RUNBOOK.md \
  tests/conformance/baselines/model-b1-xyz-review-20261123.md \
  tests/conformance/README.md tests/conformance/EXPERIMENT_HANDOFF.md \
  tests/conformance/IMPLEMENTATION_HANDOFF.md || exit 1
```

오류가 나면 중단한다. 새 도구·문서가 없으면 아직 최종 준비 버전이 게시되지 않은 것이다.
실제 연결 SHA는 공유 메시지에 적힌 SHA와 대조한다. 이후 브랜치가 움직여도 이 값을 유지한다.
이번 B2 조기 개발 승인으로 명세·실행기의 선행 조건이 갱신됐다. 과거 `a7cd3cd`만
적용하지 않고, 위 공통 명세를 포함한 게시된 최신 조합으로 최종 검증한다.
과거 원본·판정은 보존하며 새 명세의 최종 통과로 소급하지 않는다.

## 2. 새 환경 기동

이번 checkout에만 `.env`를 만들고 기존 툴체인을 재사용할 수 있다. 다음의 5440은
PostgreSQL 새 포트 제안이며 사용 중이면 빈 포트를 선택한다. Canton·어댑터 포트도
기존 실행과 충돌하지 않아야 한다. 기존 실행의 정상 종료는 소유자와 확인하고 담당
scripts로만 수행한다. wipe·강제 종료·기존 상태 삭제는 하지 않는다.

```sh
cp implementations/canton/env.example implementations/canton/.env
printf '\nCANTON_STATE_DIR=%s\nCANTON_EVIDENCE_DIR=%s\nCANTON_PG_PORT=5440\n' \
  "$B1_FINAL_ROOT/state" "$B1_FINAL_ROOT/events" >> implementations/canton/.env
source implementations/canton/scripts/env.sh
cd implementations/canton
scripts/install-toolchain.sh
scripts/adapter.sh setup
scripts/adapter.sh tokens
scripts/db.sh init
scripts/db.sh start
scripts/canton.sh start
scripts/adapter.sh start
cd ../..
source implementations/canton/scripts/env.sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r tests/conformance/requirements.txt
.venv/bin/python -m unittest discover -s tests/conformance -p 'test_*.py'
```

각 명령이 실패하면 먼저 원인을 확인하고 다음 단계로 넘어가지 않는다.
명령은 새 시스템에 fixture를 적재하기 전 환경을 준비하는 것이며 기존 원장을 사용하지 않는다.

```sh
.venv/bin/python - <<'PY'
import json, os
from pathlib import Path
config = json.loads(Path('tests/conformance/config.b1.xyz.example.json').read_text())
config['evidence_dir'] = os.environ['CANTON_EVIDENCE_DIR']
path = Path('tests/conformance/config.b1.final.local.json')
with path.open('x') as out:
    json.dump(config, out, indent=2)
Path(os.environ['B1_FINAL_ROOT'], 'source-combination.txt').write_text(
    'implementation=4b827d661551ef63033b76da401b273135aa2bdf\nconnection=' +
    os.environ['B1_FINAL_CONNECTION_COMMIT'] + '\n')
PY
```

## 3. 실제 환경 증빙과 사전 확인

아래 세 환경변수는 실행자가 **현재 서비스에 적용된 실제 조건**으로 설정한다.
자원 정책에는 JVM heap·별도 CPU/메모리 제한 여부·경쟁 작업을 적고, 재기동 정책에는
실행 중 수동/자동 재기동·외부 supervisor 여부를 적는다. 네트워크에는 서비스 배치·
localhost 여부·프록시/중계 여부를 적는다. 모르면 확인하며 예시 문구를 사실로 복사하지 않는다.
이 확인은 같은 조건의 반복 실험 설정 승인을 대신하지 않는다.

```sh
: "${B1_RESOURCE_POLICY:?실제 JVM heap/서비스 자원 제한/경쟁 작업을 설정하세요}"
: "${B1_RESTART_POLICY:?이번 검증의 실제 서비스 재기동 정책을 설정하세요}"
: "${B1_NETWORK_NOTES:?실제 서비스 배치/프록시 조건을 설정하세요}"
.venv/bin/python tests/conformance/b1_final_context.py \
  --config tests/conformance/config.b1.final.local.json \
  --resource-policy "$B1_RESOURCE_POLICY" \
  --restart-policy "$B1_RESTART_POLICY" \
  --network-notes "$B1_NETWORK_NOTES"
```

도구는 읽기 전용 status·버전·호스트 정보를 수집하고, 고정된 구현 원본 및 실제 DAR와
적재기의 SHA-256을 기록한다. context.json·final-host-evidence.json이 있으면 덮어쓰지 않는다.
실패한 수집 결과도 보존한다. 새 증거 경로가 필요하면 어댑터를 정상 종료한 뒤 `.env`와
설정을 함께 바꾸고 재기동한다. 동작 중인 어댑터의 로그 경로만 실행기에서 바꾸지 않는다.

**실행 전 확인:** DB status는 클러스터 관리자 세션의 설정이다. Canton 연결에 별도
내구성 override가 없는지도 구현 설정과 대조한다. 구현 README §5는 Z 변경 근거이고
보존 기준은 main `204eb5e`다. host 증빙·context와 실제 시작 조건이 다르면 수정/재수집 후
진행한다. 부팅 후 소스·DAR·설정·자원을 바꿨으면 새 조합으로 다시 고정한다.

```sh
.venv/bin/python - <<'PY'
import os, sys
from pathlib import Path
sys.path.insert(0, 'tests/conformance')
from checks import read_json
from runner import review_evidence
root = Path(os.environ['B1_FINAL_ROOT']) / 'preflight'
(root / 'evidence').mkdir(parents=True, exist_ok=False)
errors = review_evidence(read_json('tests/conformance/config.b1.final.local.json'), root)
print({'provenance_blockers': errors})
raise SystemExit(bool(errors))
PY
```

이 사전 확인은 소스 해시·기준 커밋·필수 문맥을 검사하며, 실제 운영 조건이 진실인지
자동 인증하지 않는다. 이를 포함해 실제 환경 사실을 실행자와 총괄이 확인한다.

## 4. 준비 → 전체 최종 검증 → 호출 대조

```sh
.venv/bin/python tests/conformance/runner.py prepare \
  --config tests/conformance/config.b1.final.local.json --observer-auth bearer \
  --platforms X Y Z --fixture shared/fixtures/initial/f0-normal.json \
  --output "$B1_FINAL_ROOT/batches"
```

준비 PASS를 확인한 뒤 아래 전체 검증을 **한 번** 실행한다. 중간 필터 4건의 추가 실행은
요구하지 않는다. 실패 결과를 감추거나 PASS할 때까지 실행을 덧붙이지 않는다.

```sh
.venv/bin/python tests/conformance/runner.py check \
  --config tests/conformance/config.b1.final.local.json --observer-auth bearer \
  --stage xyz --final --output "$B1_FINAL_ROOT/batches"
```

명령별 콘솔 출력·종료 코드를 보존한다. `checks.json`에서 status=FINISHED,
AC-01~11=PASS, 필수 71건 PASS, not_run/blockers 없음, final_handoff_eligible=true를
확인한다. AC-12는 B1에 NOT_APPLICABLE이다. 71 PASS만으로 최종 인계를 선언하지 않는다.

```sh
implementations/canton/.venv/bin/python -I \
  implementations/canton/selftest/message_log_check.py \
  --json "$B1_FINAL_ROOT/final-message-check.json" \
  "$B1_FINAL_ROOT/batches" "$CANTON_EVIDENCE_DIR"
```

호출 검사 종료 0·문제 0을 확인한다. 준비 snapshot 3개와 사례 초기 snapshot 213개에
추적이 있어야 하며, `snapshot-file` 대조가 총 216개인지 확인한다(완주한 준비 1+71건 기준).
추적 없는 snapshot·대조 못한 증거 폴더가 있으면 사유를 확인한다. 결과 조회 GET은 실제
조회가 있는 필수 사례에서 대조한다. 보조 검사기의 종료 코드만으로 최종 인계를 대체하지 않는다.

## 5. 공유할 증거

`$B1_FINAL_ROOT`에서 **batches/**, **events/**(context·host 증빙·원본 JSONL 포함),
**preflight/**, **source-combination.txt**, **final-message-check.json**, 각 명령의 콘솔
출력·종료 코드와 실제 비밀값 없는 로컬 설정을 골라 별도 공유 폴더에 복사한다.
repo/·state/ 전체를 ZIP에 넣지 않는다. 특히 state/secrets는 제외한다.
공유 폴더의 모든 파일 Manifest와 ZIP SHA-256을 만들고 원본은 로컬에 별도로 보존한다.
총괄이 원본을 대조한 뒤 최종 인계·병합을 사용자에게 요청한다.
이 문서·스크립트의 자체 시험 통과는 실제 B1 최종 검증 PASS가 아니다. 반복 실험은
A·B1 최종 검증과 동일 조건의 계측 준비 확인 뒤 별도 세션에서 진행한다.

## 6. 8d084b6 최종 시도의 실행기 표현 결함 보완

기존 `batch-98284d2baf394869bd3feddf3264ce87`은 71개 사례가 PASS였지만,
메모리의 명령/결과 튜플과 JSON 리스트를 비교해 최종 차단 60건이 발생했다.
그 배치의 종료 1·final_handoff_eligible=false는 원본대로 보존한다.
오프라인 증거 검사는 통과했지만 원본의 필드를 true로 직접 수정하지 않는다.
보완은 실행기의 결과 생성 한 줄이며 업무 구현·검증 조건·명세는 바뀌지 않는다.

이 최종 시도에 사용한 검증 전용 환경의 구현·설정·자원 조건이 그대로이고 서비스도
같은 상태라면 설치·DB 초기화·기동·중간 4건을 반복할 필요는 없다. 기존 원본을 별도로
보존한 후 수정 실행기에서 전체 --final만 새 batch/run_id로 한 번 실행한다.

이미 8d084b6 조합을 적용한 그 checkout에서, 담당자의 추가 편집이 없는지 먼저 확인한다.
아래 비교가 실패하면 중단하고 미커밋 변경을 보존한다. 기존 B2 개발 checkout에 적용하지 않는다.

```sh
git diff --exit-code 8d084b69948b16f2ed7db3816536ea1781a2470e -- \
  tests/conformance/runner.py tests/conformance/test_execution.py \
  tests/conformance/test_history.py || exit 1
git fetch origin feat/b1-z-integration || exit 1
B1_FINAL_FIX_COMMIT="$(git rev-parse FETCH_HEAD)"
export B1_FINAL_FIX_COMMIT
git restore --source="$B1_FINAL_FIX_COMMIT" --worktree -- \
  tests/conformance/runner.py tests/conformance/test_execution.py \
  tests/conformance/test_history.py tests/conformance/B1_FINAL_RUNBOOK.md \
  tests/conformance/baselines/model-b1-xyz-review-20261123.md || exit 1
```

게시된 수정 커밋과 위 SHA가 같은지 확인하고 자체 시험을 실행한다. 이전 콘솔·소스 조합
파일을 덮어쓰지 않고 새 연결 SHA·변경 내역을 별도 이름으로 기록한다. 구현·DAR·환경과
context의 일치를 다시 확인하고, 제4절의 --final 명령에 **새 output 디렉터리**를 준다.
호출 검사는 그 새 디렉터리와 같은 실제 이벤트 폴더를 함께 지정한다. 준비 명령을 다시
실행하지 않았다면 snapshot-file은 사례 71×3=213개가 기대값이다. 과거 준비 3개를
새 output에 복사하거나 두 배치의 반복 수를 합쳐 인수 조건을 채우지 않는다.
새 배치 원본·현재 context·실제 JSONL·호출 검사·명령별 종료 코드·Manifest를 공유한다.
환경/구현 조건이 달라졌으면 제1~4절의 새 조합 고정 절차를 따른다.

같은 검증 전용 checkout/상태를 유지한 경우의 실행 명령이다. `B1_FINAL_ROOT`는 이전
검증의 실제 전용 루트이며, 모르면 이전 경로를 확인한다. 임의의 다른 폴더로 채우지 않는다.

```sh
: "${B1_FINAL_ROOT:?이전 검증 전용 루트를 확인하세요}"
source implementations/canton/scripts/env.sh
.venv/bin/python -m unittest discover -s tests/conformance -p 'test_*.py'
B1_FINAL_RETRY_OUT="$(mktemp -d "$B1_FINAL_ROOT/final-tuple-fix.XXXXXX")" || exit 1
printf 'implementation=4b827d661551ef63033b76da401b273135aa2bdf\nconnection=%s\n' \
  "$B1_FINAL_FIX_COMMIT" > "$B1_FINAL_RETRY_OUT/source-combination.txt"
.venv/bin/python tests/conformance/runner.py check \
  --config tests/conformance/config.b1.final.local.json --observer-auth bearer \
  --stage xyz --final --output "$B1_FINAL_RETRY_OUT"
```

종료 코드와 출력 배치의 checks.json을 보존·확인한 뒤 호출 검사를 별도로 실행한다.

```sh
implementations/canton/.venv/bin/python -I \
  implementations/canton/selftest/message_log_check.py \
  --json "$B1_FINAL_RETRY_OUT/message-check.json" \
  "$B1_FINAL_RETRY_OUT" "$CANTON_EVIDENCE_DIR"
```
