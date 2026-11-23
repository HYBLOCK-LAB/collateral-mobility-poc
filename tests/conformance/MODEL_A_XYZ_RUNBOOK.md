# Model A Z 공통 연결

대상은 Draft [PR #8](https://github.com/HYBLOCK-LAB/collateral-mobility-poc/pull/8)의
`b2bd66d63e9d8274a769ce7274612ac478e5a68e`와 총괄의 Z 적재·수집 연결 코드다.
최소 정상 흐름의 개발 중 연결이며 최종 인계·병합 승인·본 실험 시작을 의미하지 않는다.
기존 X→Y 기준은 `83a4b5da61db49a72711694980c1ff03d175a7ae`와
[보존 기록](baselines/model-a-xy-20261122.md)을 유지한다.

## 1. 환경과 자격

- 구현 소스는 PR #8의 커밋으로 고정하고 그 위에 총괄 소유 `prepare.py`,
  `collect_events.py`, 공통 실행기를 적용한다. 기존 미커밋 파일을 덮어쓰지 않는다.
- 새 XYZ 시험 환경을 준비한다. 기존 XY 볼륨에는 제약조건 변경이 자동 이행되지 않으므로
  재사용하지 않는다. 기존 데이터를 삭제하거나 `down -v`로 초기화하지 않는다.
- Compose는 구현 README의 `compose.yaml`과 `compose.z.yaml`을 함께 사용한다.
  DB는 각각 PostgreSQL 17의 `model_a_x`, `model_a_y`, `model_a_z`다.
- API의 네 가지 `POC_*_TOKEN`을 실행기 환경에도 설정한다. 내부 XY·YZ 토큰은
  API 간 통신용이며 실행기·수집기에 전달하지 않는다.
- 초기 적재 자격은 `MODEL_A_X_SEED_DSN`, `MODEL_A_Y_SEED_DSN`,
  `MODEL_A_Z_SEED_DSN`, 수집 자격은 각 플랫폼의 `MODEL_A_*_EVIDENCE_DSN`이다.
  비밀값은 설정 JSON·문서·결과에 기록하지 않는다.

Z 적재 계정은 `CONNECT`, 스키마 `USAGE`, `runs`의 `SELECT`,
`runs,principals,bundles,margin_calls`의 `INSERT`만 필요하다.
수집 계정은 `runs,requests,transfers,commit_observations,events`의 `SELECT`와
접속·스키마 사용 권한만 필요하다. 수집은 읽기 전용 트랜잭션으로 수행한다.
X·Y의 기존 최소 권한은 [실행기 README 제17절](README.md#17-a-xy-초기-적재이벤트-연결--pr-4)을 따른다.

DSN은 프로그램 실행 위치에서 실제 접근 가능한 주소여야 한다. Compose의 DB 포트는
호스트에 공개되지 않는다. 전용 DB 네트워크 안에서 연결 프로그램을 실행한다면
Python·공통 모듈·Schema·fixture의 읽기 경로와 결과 쓰기 경로를 준비하고,
아래 준비/수집 argv를 해당 컨테이너의 `docker exec ... python ...`으로 바꾼다.
적재 컨테이너와 읽기 전용 수집 컨테이너의 자격은 분리한다.

## 2. 준비와 실행

기존 로컬 설정을 보존하면서 예시를 복사한다.

```sh
cp -n tests/conformance/config.a.xyz.example.json tests/conformance/config.a.xyz.local.json
```

실제 API 주소·Python 경로·`evidence_dir`를 확인한다. Python에는 기존 의존성과
`psycopg[binary]==3.3.3`이 필요하다. 준비 명령의 마지막 `xyz`가 Z 적재를 선택한다.
수집 명령에도 같은 위치 인자 `xyz`를 붙인다. 위치 인자를 사용하므로 실행기의
`--collect-command` argv와 옵션 구분이 충돌하지 않는다.

```sh
.venv/bin/python tests/conformance/runner.py prepare \
  --config tests/conformance/config.a.xyz.local.json --observer-auth bearer \
  --platforms X Y Z --fixture shared/fixtures/initial/f0-normal.json

.venv/bin/python tests/conformance/runner.py check \
  --config tests/conformance/config.a.xyz.local.json --observer-auth bearer \
  --stage xyz --case AC-01 AC-02 \
  --collect-command .venv/bin/python implementations/db-api/collect_events.py xyz

.venv/bin/python tests/conformance/runner.py check \
  --config tests/conformance/config.a.xyz.local.json --observer-auth bearer \
  --stage xyz \
  --collect-command .venv/bin/python implementations/db-api/collect_events.py xyz
```

적재는 모든 선택 DB의 run_id를 먼저 확인하고, Z에는 버전 0의 묶음과 fixture의
OPEN 증거금 요구·수용·적격성 조건만 넣는다. 후속 권리·배정·확정 거래를 주입하지 않는다.
일부 DB만 적재된 실패 실행은 보존하고 새 run_id를 사용한다.
수집 파일은 `<evidence_dir>/<run_id>/events.jsonl`이며 실패한 업무 실행의 진단용
이벤트도 남긴다. 미확정 작업이 남으면 수집 성공으로 처리하지 않는다.

## 3. X→Y 회귀와 완료 구분

기존 XY 설정의 준비 명령에는 `xyz`를 붙이지 않고, 수집 명령에서도 생략한다.
이 기본 경로는 Z 접속 없이 X·Y만 처리한다. `check --stage xy`로 전체 50건을 재확인한다.
XYZ 설정을 그대로 쓰며 실행기만 `--stage xy`로 바꾸면 적재는 XYZ로 남으므로
반드시 XY 전용 설정을 사용한다.

실행기는 준비 전에 저장소의 A/B1 적재기와 A 수집기의 argv 단계를 확인한다.
실행기의 `--stage`(prepare는 `--platforms`)와 적재·수집 명령의 마지막 `xy`/`xyz`가
다르면 INPUT 단계에서 실패하며 적재 프로세스를 호출하지 않는다. 생략한 단계는 `xy`다.
직접 Python 호출과 절대 경로를 사용하는 `docker exec`를 지원한다. 이 저장소의
프로그램 경로 뒤에는 선택적 단계 인자만 둔다. 사용자 정의 wrapper의 내부 동작은
추정하지 않으므로 해당 명령 자체에서 단계 일치를 보장해야 한다.

공통 검증과 별도로 수인님의 자체 58건 전체 회귀, Z 거절·경합·재시작 검증과
원본 증거를 확인해야 한다. Z readiness의 내부 Y 조회 메시지 계측도 아직 별도 확인 대상이다.
최종 인계에는 모든 필수 공통 항목과 실제 `context.json`의 버전·환경·기준 커밋·Z 변경 기록이
필요하다. 일부 정상 흐름 PASS나 필터 실행의 종료 코드 0으로 전체 완료를 선언하지 않는다.

## 4. 총괄 연결 검증 기록 — 2026-11-22

PR #8의 위 커밋을 임시 작업 폴더에 보존하고 총괄 적재·수집 코드와 조합했다.
macOS의 별도 Compose 프로젝트 `model-a-review-4254aecc`에서 실제 빌드·기동 후
API 세 개와 PostgreSQL 세 개가 모두 healthy임을 확인했다.
런타임은 Python 3.12.16, DB 세 개 모두 PostgreSQL 17.12였다.
DB 포트는 호스트에 공개하지 않았고 적재/수집 컨테이너의 자격을 분리했다.

| 실제 확인 | 결과 |
|---|---|
| XYZ F0 적재·초기 snapshot | PASS |
| 공통 XYZ AC-01~11 필수 변형·반복 | 71 PASS / 0 FAIL / 0 INCONCLUSIVE |
| Z 추가 코드에서 기존 XY 기본 적재·수집 경로 회귀 | 50 PASS / 0 FAIL / 0 INCONCLUSIVE |
| 실행기 자체 시험 | 61 PASS. 모의 시험을 실제 업무 결과와 합산하지 않음 |
| 의존성·변경 공백 검사 | `pip check`, `git diff --check` PASS |

원본은 로컬 `results/model-a-pr8-z-connection-20261122/`에 보존한다.
`verification.json`, `verified-source.tar.gz`, `source-sha256.json`, 기동·정리 로그와
개별 응답·snapshot·구현 이벤트를 포함한다. XYZ 배치는
`batch-d57905feecf24fe7a21a7d0435c352fd`, XY 배치는
`batch-afa9658f01b94043bb7e61f9b0133741`이다. 원본의 Git/외부 업로드는 하지 않았다.
기존 데이터는 건드리지 않았고 이번에 만든 임시 프로젝트만 정리했다.

검증 중 최종 완료 검사가 조회·인증 거절 실행의 빈 로그까지 무조건 거부하는
불일치를 확인했다. 수집 단계와 동일하게 AC-02/03의 수락된 명령 결과가 없는 경우에만
허용하도록 보완했다. 수정된 최종 검사로 기존 XYZ 71건의 원본을 다시 검사해 누락이
없음을 확인했다(`gate-recheck.json`). 실제 업무 배치는 이 최종 검사 수정 전의
보존된 실행기를 사용했고, 수정본의 재검사는 모델을 재실행하지 않은 원본 검사다.
최종 로컬 변경 파일은 별도 `local-connection-source.tar.gz`와 해시로 구분한다.

이 배치 시점에는 `context.json`의 구현 버전·환경·XY 기준·Z 변경 기록 연결과 수인님의 추가 자체
검증 확인이 남아 있었다. `--final` 실행과 최종 인계 승인은 하지 않았으며 두 배치 모두
`final_handoff_eligible=false`다. Z 내부 조회 메시지 계측 및 본 실험도 미완료다.

검토 초점은 fixture 조건의 무변형 Z 적재, 수집기의 쓰기 자격 배제,
XY 기본 경로 보존과 빈 로그 허용 범위다. 공통 spec·Schema·fixture 및
수인님의 업무 처리 소스는 변경하지 않았다.

## 5. 환경·소스 증빙 연결 — 2026-11-23

[Z 변경 기록](MODEL_A_Z_CHANGE_RECORD.md)에 실제 XY→Z 소스 변경과 이유,
인터페이스·메시지·예외 경로 및 대응 검증을 기록했다. 메시지 수치 집계는 미수행이다.

이전 배치의 자원·내구성 설정을 소급하여 채우지 않았다. 새 격리 환경에서 호스트와
Docker VM의 CPU·메모리, 컨테이너별 실제 제한, PostgreSQL의 `fsync`,
`synchronous_commit`, `full_page_writes`, 네트워크·이미지·볼륨 구성을 기록했다.
해당 환경의 `context.json`에는 실제 구현 파일 해시, 보존된 XY 커밋,
위 Z 변경 기록을 연결했다. 원본과 문서·실행 코드 버전을 별도로 식별한다.

새 프로젝트 `model-a-review-e439ab9f`의 준비·공통 XYZ **71 PASS**를 확인했다.
배치는 `batch-cd7cd60238894f22a6a2904ec79b52fe`이며 원본은 로컬
`results/model-a-pr8-provenance-rerun-20261123/`에 보존한다.
`environment-capture.json`, `implementation/context.json`, `postcheck.json`에서
실제 환경·소스 해시·기준 커밋·필수 변형/반복/증빙 대조에 누락이 없음을 확인했다.
모든 DB의 위 세 내구성 설정은 `on`이었다. 컨테이너별 CPU·메모리 제한은 별도 지정하지
않았고, Docker VM·호스트 자원 경계는 따로 기록했다. 이 검증을 동일 자원 본 실험으로
표현하지 않는다. 검증 종료 후 이번 프로젝트와 새 볼륨만 정리했다.

첫 증빙 시도는 Git 정보가 없는 임시 폴더에서 기준 커밋을 찾지 못해 준비·업무 시험 전에
중단됐다. 검증용 Git 읽기 경로를 연결한 뒤 새 프로젝트로 재실행했다.
첫 시도 기록도 `results/model-a-pr8-provenance-20261123/`에 보존했다.
이전 2026-11-22 원본 3,626개 파일의 해시가 유지됨을 다시 확인했다.

증빙 형식·소스 대조 통과는 최종 개발 완료 승인이 아니다. 수인님이 보고한
Z 자체 시험의 2 INCONCLUSIVE와 XY 자체 시험의 1 INCONCLUSIVE는 원래 판정을
보존하고 원본을 대조해야 한다. 공통 경합·복구 PASS로 해당 판정을 임의 변경하지 않는다.
최종 `--final`과 병합, 본 실험은 이 검토 이후 별도로 진행한다.

## 6. PR #10 연결 리뷰 보완 — 2026-11-23

수인님 리뷰의 단계 불일치와 빈 로그 필수 필드 처리를 총괄이 대조해 보완했다.
위 단계 검사는 적재 전에 수행하며, 최종 검사의 `command_results`는 명시적인 목록이어야 한다.
누락·null·잘못된 형식을 빈 목록으로 취급하지 않는다. AC-02/03의 정상 빈 목록은 계속 허용한다.

자체 회귀 63건이 PASS했다. 단계 불일치 시 준비 프로세스를 호출하지 않는 시험,
로컬/컨테이너 argv의 정상 선택, 누락·잘못된 형식의 결과 필드 거부를 포함한다.
이전 실제 XYZ 71건의 원본도 수정된 최종 검사로 다시 대조해 증빙 누락이 없음을 확인했다.
업무 시스템을 재실행하거나 과거 배치·판정을 수정한 것은 아니다.
원본은 로컬 `results/model-a-pr10-review-20261123/`에 보존한다.
