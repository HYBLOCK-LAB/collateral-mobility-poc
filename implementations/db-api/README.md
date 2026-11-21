# Model A — DB/API

담당: **연수인**. 현재 개발 범위는 X(Repo)·Y(증권대차)의 S0→S1→S2 흐름이다. 공통 기준은 [루트 안내](../../README.md), [AGENTS.md](../../AGENTS.md), [업무·인터페이스 명세](../../spec/), [공통 데이터 계약](../../shared/README.md)을 따른다.

## 기술 스택과 구성

| 항목 | 버전 및 구성 |
|---|---|
| 런타임 | Docker 이미지 `python:3.12-slim` |
| API | FastAPI 0.135.1, Uvicorn 0.41.0 |
| DB 및 드라이버 | Docker 이미지 `postgres:17`, SQLAlchemy 2.0.48, psycopg 3.3.3 |
| 실행 도구 | Docker Compose v2. Windows 검증 환경의 CLI는 v2.29.1-desktop.1 |
| 내부 HTTP·Schema | httpx 0.28.1, jsonschema 4.26.0. 공통 Schema는 읽기 전용 마운트 |

Python과 PostgreSQL 이미지 태그는 마이너·패치 업데이트를 허용한다. 실제 실험 환경을 기록할 때는 컨테이너에서 확인한 런타임 버전과 이미지 식별자를 사용한다.

| 플랫폼 | API | DB 서비스와 DB 이름 | 전용 네트워크와 볼륨 |
|---|---|---|---|
| X | `http://127.0.0.1:8101` | `db-x` / `model_a_x` | `x` / `x-data` |
| Y | `http://127.0.0.1:8102` | `db-y` / `model_a_y` | `y` / `y-data` |

두 API는 같은 Dockerfile에서 빌드하며, 각 프로세스는 자기 DB의 전용 네트워크와 계정으로 연결한다. API 포트는 로컬 호스트에 바인딩하고 DB 포트는 Compose 네트워크 안에서만 사용한다. 별도의 `xy` 네트워크에는 두 API만 참여한다. `GET /health/db`는 SQL로 DB 이름, 접속 사용자, 서버 버전을 확인한다.

## 실행 환경과 자격 설정

실제 2단계 자체 검증은 Windows Python API 두 개와 WSL Ubuntu의 PostgreSQL 17.12 인스턴스 두 개를 사용했다. Compose 구성은 별도로 제공한다.

### 실제 통합 검증을 재현하는 방법

1. 로컬에서 독립 PostgreSQL 17 인스턴스 두 개를 준비한다. 검증에서는 WSL `/tmp/model-a-xy-pg17` 아래에 PGDG 서명을 확인한 패키지를 풀고 SCRAM 인증으로 초기화했다. X DB는 `127.0.0.1:15432`의 `model_a_x`, Y DB는 `127.0.0.1:15433`의 `model_a_y`였다. DB 사용자 이름은 각각 DB 이름과 같다. 두 인스턴스는 검증 종료 후 정상 종료했다.
2. 저장소의 `implementations/db-api/`에서 Python 3.12 가상환경에 `python -m pip install -r requirements.txt`를 실행한다. X·Y API 프로세스 모두에 같은 기관·관측기 토큰과 내부 토큰을 넣고, 각 프로세스의 `DB_PASSWORD`에는 자기 DB 자격을 넣는다. 비밀값은 로컬 프로세스 환경에 둔다.
3. X·Y를 각각 다른 PowerShell에서 실행한다. 공통 환경으로 `DB_HOST=127.0.0.1`, `MODEL_A_SCHEMA_DIR`에 `../../shared/schemas`의 절대 경로, `MODEL_A_X_URL=http://127.0.0.1:18201`을 설정한다. 플랫폼별 환경과 명령은 아래 표를 따른다.

| 세션 | 환경 변수 | 기동 명령 |
|---|---|---|
| X | `PLATFORM=X`, `DB_PORT=15432`, `DB_NAME=model_a_x`, `DB_USER=model_a_x` | `python -m uvicorn app:app --host 127.0.0.1 --port 18201` |
| Y | `PLATFORM=Y`, `DB_PORT=15433`, `DB_NAME=model_a_y`, `DB_USER=model_a_y` | `python -m uvicorn app:app --host 127.0.0.1 --port 18202` |

4. 두 주소의 `GET /health/db`에서 `status=ok`, 예상 DB 이름과 PostgreSQL 버전을 확인한다. 아래의 F0 매핑에 따라 두 DB에 새 `run_id`를 준비하고 자체 테스트를 실행한다.

```powershell
python selftest.py <준비된-run_id> --x http://127.0.0.1:18201 --y http://127.0.0.1:18202
```

### Docker Compose 실행 방법

2단계 Compose 설정 검사는 통과했다. 이미지 빌드와 컨테이너 검증은 [검증 결과](#2단계-검증-결과)의 상태를 따른다. Docker Desktop의 Linux 엔진이 응답하면 저장소 루트에서 시작한다.

```powershell
Set-Location implementations/db-api
docker version --format '{{.Server.Version}}'
docker compose version
if (!(Test-Path -LiteralPath .env)) { Copy-Item env.example .env }
```

`Copy-Item`은 최초 설정 때 실행한다. 기존 1단계 `.env`를 사용하는 경우 아래 토큰 항목을 추가한다. 실제 비밀값은 Git 제외 대상인 `.env`에 설정한다.

| 환경 변수 | 용도 |
|---|---|
| `POSTGRES_X_PASSWORD`, `POSTGRES_Y_PASSWORD` | 각 DB와 해당 API의 DB 자격 |
| `POC_BANK_A_TOKEN`, `POC_BANK_B_TOKEN`, `POC_BANK_C_TOKEN`, `POC_OBSERVER_TOKEN` | F0의 `bank-a`, `bank-b`, `bank-c`, `experiment-observer`에 매핑되는 Bearer 토큰 |
| `MODEL_A_INTERNAL_TOKEN` | Y가 X의 내부 API를 호출할 때 쓰는 별도 Bearer 토큰 |
| `MODEL_A_X_PORT`, `MODEL_A_Y_PORT` | 호스트 API 포트. 기본값 8101·8102 |

API 토큰 5개는 서로 다른 값으로 설정한다. Compose가 각 DB 이름·사용자와 플랫폼을 설정한다.

```powershell
docker compose config --quiet
docker compose up --build --wait --wait-timeout 120
docker compose ps
Invoke-RestMethod http://127.0.0.1:8101/health/db
Invoke-RestMethod http://127.0.0.1:8102/health/db
```

기대 결과는 두 요청 모두 HTTP 200과 `status=ok`를 반환하고, 각 응답의 `platform`과 `database`가 각각 `X`/`model_a_x`, `Y`/`model_a_y`인 것이다. `postgresql_version`은 `17.`로 시작한다.

```powershell
docker compose logs --tail 100 api-x api-y db-x db-y
docker compose down
```

`down`은 DB 볼륨을 보존한다. PostgreSQL 이미지의 초기 비밀번호 설정은 빈 볼륨을 초기화할 때 적용된다. 기존 볼륨을 재사용할 때는 기존 DB 자격 설정을 사용한다.

## 2단계 DB 구조와 초기 적재 연결

API 시작 시 [storage.py](storage.py)의 SQLAlchemy 메타데이터가 각 DB에 아래 13개 테이블을 생성한다. 두 DB의 스키마는 같으며, X는 Repo와 예약을, Y는 증권대차와 복구 진행을 담당한다. `runs`를 제외한 모든 테이블은 `run_id`를 외래 키로 참조한다.

| 테이블 | 기본 키 | 주요 열과 제약조건 |
|---|---|---|
| `runs` | `run_id` | 실행 단위 |
| `principals` | `run_id, principal_id` | `actor, observer`. 관측기라면 `actor`가 NULL이며 업무 주체는 공통 기관 값 중 하나여야 한다. |
| `bundles` | `run_id, bundle_id` | `security_id, quantity, reference_amount, version`. 묶음 ID는 1~120자, 수량은 양의 십진 정수 문자열, 평가액은 0 이상의 십진 정수 문자열, 버전은 0~2다. |
| `agreements` | `run_id, agreement_id` | 해당 실행의 묶음을 참조한다. `kind`는 `REPO` 또는 `LOAN`, `terms`는 JSONB다. |
| `rights` | `run_id, bundle_id, created_version` | 해당 실행의 묶음을 참조한다. 버전 0·1·2의 주체는 각각 A·B·C은행이다. 버전 0의 `source_record_id`는 NULL이고 후속 권리는 원거래 ID가 필요하다. 소진된 권리의 `allowed_commands`는 빈 JSON 배열이다. 부분 유일 인덱스로 묶음당 `AVAILABLE` 행을 하나만 허용한다. |
| `contracts` | `run_id, contract_id` | `agreement_id, bundle_id, kind, parties`. 같은 실행의 합의와 묶음을 복합 외래 키로 참조하고, 묶음과 계약 유형 조합은 중복될 수 없다. |
| `obligations` | `run_id, obligation_id` | 같은 실행의 계약을 참조한다. 계약당 같은 유형의 의무는 한 건이며, `debtor, creditor, target`으로 당사자와 대상을 기록한다. |
| `requests` | `run_id, actor, request_id` | 명령 원문, 결과, 이벤트 연결 정보, 수락한 합의, `RESERVE/CONFIRM/ABORT/DONE`을 저장한다. `DONE`은 `PENDING`이 아닌 결과와 짝을 이루며, `ABORT`에는 거부 사유, `CONFIRM`에는 수락한 합의가 필요하다. 부분 유일 인덱스로 동일 실행의 `PENDING/COMMITTED` 거래 ID 재사용을 막는다. |
| `transfers` | `run_id, request_id` | X의 예약 상태 `DENIED/RESERVED/CONSUMED/RELEASED`를 저장한다. `DENIED`에만 사유가 있어야 한다. 부분 유일 인덱스로 묶음당 활성 예약 하나, 거래 ID당 예약 또는 소진 기록 하나를 허용한다. |
| `transfer_confirmations` | `run_id, request_id` | X에서 `transfers`의 소진 결과에 연결하는 영속 확인 메시지다. 실행 내 고유 `message_id`와 Repo의 `source_record_id`를 저장한다. |
| `events` | `run_id, event_id` | 공통 이벤트 객체를 `payload` JSONB에 저장한다. |
| `snapshot_counters` | `run_id` | 플랫폼의 각 실행에서 발급한 마지막 `snapshot_seq`를 저장한다. API 재시작 후에도 DB의 순번을 이어 쓴다. |
| `commit_observations` | `run_id, actor, request_id` | 확정된 `requests` 행에 연결하는 영속 대기열이다. 확정·로컬 반영 이벤트 기록이 끝나면 항목을 비운다. |

### F0 초기 적재 순서

서비스 기동 후 연구 총괄의 준비 프로그램이 DB별 트랜잭션으로 다음 순서를 적용한다. X·Y에 같은 새 `run_id`를 사용한다. 어느 한쪽이라도 같은 ID가 있거나 일부 적재만 성공한 경우, 기존 행을 덮어쓰지 않고 해당 실행을 실패로 기록한다.

| 순서 | [공통 F0 fixture](../../shared/fixtures/initial/f0-normal.json) 매핑 |
|---|---|
| 1. `runs` | 양쪽에 새 `run_id`를 등록한다. |
| 2. `principals`, `bundles` | 양쪽에 F0의 `principal_id, actor, observer`를 복사한다. `bundle-001`과 `TEST-KTB-001`을 매핑하고 `quantity.value → quantity`, `reference_value.amount → reference_amount`를 문자열 `10000000000`으로 넣는다. `version=0`을 사용한다. 공통 응답이 단위 `KRW_FACE_VALUE`와 통화 `KRW`를 붙인다. |
| 3. `agreements` | X에는 `repo-agreement-001`, Y에는 `loan-agreement-001`을 넣는다. `agreement_id, bundle_id, type → kind`를 분리한다. Repo의 `seller, buyer, reuse_allowed`와 Loan의 `lender, borrower, ccp_use_allowed, collateral_condition`을 각각 `terms` JSONB에 보존한다. |
| 4. `rights` | X에만 F0의 `initial_holder=BANK_A`를 사용해 `created_version=0, status=AVAILABLE, allowed_commands=["CONFIRM_REPO"], source_record_id=NULL`을 넣는다. |
| 5. S0 확인 | 양쪽 snapshot에서 버전 0, X의 A은행 권리 `bundle-001:right:0`, Y의 빈 권리 목록을 확인한다. `contracts, obligations, requests, transfers, events`는 업무 실행 전까지 비어 있다. |

X·Y의 `margin_calls`와 `allocations` snapshot 배열은 비어 있다. 확정된 거래, 후속 권리, 의무는 공통 명령으로 생성한다.

연구 총괄은 두 DB의 접속 정보를 준비 프로그램에 연결하고, 공통 실행기의 `platforms`에 X·Y API 주소와 `auth_env`의 환경 변수 **이름**을 설정한다. 실행기는 `prepare_command`에 `--run-id`와 `--fixture`를 붙여 호출한다. 성공 시 종료 코드 0과 `{"run_id":"<입력 ID>","status":"PREPARED"}` 한 줄을 표준 출력으로 반환한다. 실패는 비영 종료 코드로 남긴다. 입력·출력 상세는 [공통 실행기 준비 명령](../../tests/conformance/README.md#12-설정-파일준비-명령의-입출력)을 따른다.

공통 ID 규칙은 권리와 의무의 접미사를 고정하지만 Schema는 생성된 ID에도 128자 제한을 둔다. 접미사를 포함해 제한을 넘는 명령은 `INVALID_REQUEST`로 처리한다. 총괄의 초기 적재에서도 생성될 권리 ID 길이를 확인해야 한다. 최대 길이 입력에 대한 공통 정책은 총괄 확인 항목이다.

## X·Y 명령과 권리 인계

| HTTP 경로 | 플랫폼과 사용 주체 |
|---|---|
| `POST /runs/{run_id}/commands` | X의 `CONFIRM_REPO`는 A은행, Y의 `CONFIRM_LOAN`은 B은행. 공통 명령 JSON을 사용하고, `PENDING`은 HTTP 202, 확정 성공과 업무 거부는 HTTP 200으로 응답한다. |
| `GET /runs/{run_id}/commands/{request_id}` | 원래 제출 플랫폼과 인증된 원래 기관에서 저장된 결과 조회. 조회 자체는 재실행을 시작하지 않는다. 동일 명령의 재제출은 원래 `request_id`를 사용하며 `replayed=true`를 반환한다. |
| `GET /runs/{run_id}/readiness` | Y에서 B은행이 `command_type=CONFIRM_LOAN`, `bundle_id`, `agreement_id`를 전달한다. `ready`, `observed_version`, `blocking_reasons`를 반환하고 업무 상태를 변경하지 않는다. |
| `GET /runs/{run_id}/snapshot` | X·Y의 로컬 묶음, 권리, 계약, 의무, 진행 상태를 관측기 토큰으로 조회한다. 공유 기록은 같은 공통 ID로 연결하고, 로컬 `snapshot_seq`를 플랫폼 공통 버전으로 해석하지 않는다. |

공통 API 인증에는 `Authorization: Bearer <토큰>`을 사용한다. Y→X 내부 토큰은 공통 실행기나 관측기에 제공할 필요가 없다. 테스트의 명령 입력은 [Repo](../../shared/fixtures/commands/repo.json)와 [증권대차](../../shared/fixtures/commands/loan.json) fixture를 사용했다.

- X Repo는 하나의 DB 트랜잭션에서 A 권리를 소진하고 B 권리, 계약, 의무, 버전 1과 확정 결과를 기록한다.
- Y는 대차 조건을 확인하고 원래 요청을 `PENDING`으로 저장한 뒤 X에 예약을 요청한다. X는 B 권리를 유지하면서 해당 묶음의 추가 사용을 차단한다. 이때 버전은 그대로다.
- 예약 후 Y는 진행 결정을 먼저 저장한다. 업무상 거부가 필요하면 X에 예약 해제를 요청하고, 해제가 확인된 뒤 `REJECTED`를 저장한다. 예약 해제 시 B 권리는 그대로 가용하다.
- 진행을 결정하면 X는 하나의 로컬 트랜잭션에서 예약을 종료하고 B 권리 소진을 기록한다. Y는 이 결과를 확인한 뒤 한 트랜잭션으로 C 권리, 대차 계약, 의무, 버전 2와 `COMMITTED`를 기록한다. X의 로컬 묶음 기록은 버전 1을 유지하며 B 권리의 소진을 나타낸다.
- 응답 유실이나 Y의 DB 오류가 생기면 진행 단계를 유지한다. Y는 기동 시와 실행 중에 원래 요청으로 같은 내부 단계를 재시도한다. X의 소진 이후에는 성공 방향으로만 재개한다. 장애가 해소되기 전까지 C 권리는 생성되지 않으며, 최종 성공에는 X의 활성 예약과 Y의 진행 항목이 남지 않는다.

## 자체 테스트와 로그 연결

초기 상태를 준비한 새 F0 `run_id`로만 `selftest.py`를 실행한다. 테스트는 입력 데이터 적재를 수행하지 않으며, 공통 명령 fixture를 실제 API에 제출해 S0→S1→S2의 값을 비교한다.

```powershell
python -m pip install -r requirements.txt
python selftest.py <준비된-run_id>
```

호스트에서 실행할 때는 은행·관측기 토큰을 프로세스 환경에 설정한다. 별도 Compose 포트를 사용했다면 `--x`, `--y`를 지정한다. 테스트가 시작 전 요구하는 상태는 위 초기 적재 매핑과 같다.

이벤트 원본은 두 DB의 `events.payload`(JSONB)에 저장한다. 연구 총괄의 수집기는 별도의 읽기 전용 DB 자격으로 두 DB에서 같은 `run_id`를 조회한다. 아래 SQL의 `:run_id`에 실행 ID를 바인딩하고, 각 결과 행의 JSON 객체 하나를 `<evidence_dir>/<run_id>/events.jsonl`의 한 줄로 내보낸다. 수집 대상은 DB가 정상 반영을 마친 실행이다.

```sql
SELECT payload::text
FROM events
WHERE run_id = :run_id
ORDER BY payload->>'producer_id', (payload->>'sequence')::bigint
```

| 연결 대상 | 필드·이벤트 |
|---|---|
| 공통 이벤트 형식 | [event Schema](../../shared/schemas/event.schema.json)를 사용한다. `model=A`, `platform`, `run_id`, `event_id`, `event_type`을 보존한다. |
| 요청과 재시도 | `request_id`, `attempt_id`, `test_id`, `bundle_id`, `record_id`로 연결한다. 명령 요청의 `X-Test-ID`와 `X-Attempt-ID`를 기록하고, 복구는 영속화한 원래 요청 문맥으로 연결한다. |
| 로컬 시각 | `producer_id, clock_id, sequence, mono_ns`. 단조 시각과 순번은 프로세스·시계 범위에서 해석한다. |
| 확정과 상태 반영 | `COMMAND_RECEIVED/COMMAND_FINISHED`, `INTERNAL_COMMITTED/LOCAL_APPLIED`, `RIGHT_CREATED/RIGHT_CONSUMED`. 버전, 권리 ID·주체·상태를 이벤트 유형에 맞춰 기록한다. |
| X→Y 인과관계 | `MESSAGE_SENT/MESSAGE_RECEIVED`의 `message_id, source_id, destination_id, message_kind, message_category`로 메시지를 연결한다. 같은 시계에서 X의 소진→송신과 Y의 수신→생성 순서를 각각 확인한다. |

### X 재시작 이후의 소진 증거

X는 B 권리 소진과 같은 트랜잭션에서 Y 앞으로 소진 확인 메시지를 발행한다. `transfer_confirmations` 행과 해당 `MESSAGE_SENT`는 소진이 커밋된 경우에만 함께 남는다. 이 `EVENT/PROTOCOL` 이벤트는 DB 채널에 메시지를 발행했다는 의미다. 실제 HTTP 요청과 응답은 기존 `REQUEST/RESPONSE` 및 `BUSINESS_INTEGRATION` 이벤트로 시도마다 별도 ID를 기록한다.

재시작한 X는 동일 `run_id`·`request_id`의 소진 결과를 조회할 때 원래 확인 메시지를 새 HTTP 응답에 담는다. Y가 받아 `run_id`, `request_id`, 원거래 ID를 검증한 뒤에만 같은 영속 메시지 ID로 수신 이벤트를 남긴다. 이로써 이전 X 시계의 소진→메시지 발행과 Y 시계의 실제 수신→권리 생성 사이를 연결한다. 기존 이벤트 시각과 시계 ID는 그대로 보존한다.

도입 전에 이미 소진된 요청은 영속 확인 메시지를 소급 생성하지 않는다. 원래 결과를 읽어 업무 복구는 계속할 수 있으며, 인과관계 증거는 당시 남은 관측 범위에서 판정한다.

권리 변경 이벤트와 `commit_observations` 항목은 각각 해당 업무 변경과 함께 커밋된다. X·Y는 확정 결과가 DB에 반영됐음을 확인한 프로세스의 `clock_id`·`sequence`·`mono_ns`로 `INTERNAL_COMMITTED`와 `LOCAL_APPLIED`를 남긴다. 이벤트 두 건과 대기열 제거는 한 트랜잭션이다. 기록 전에 서버가 종료되면 다음 프로세스가 대기열을 처리한다. 이 경우 기록 시각은 복구 프로세스가 확정을 확인한 시각이며 원래 DB 커밋 순간은 복원하지 않는다. 공통 관측기의 응답 수신 시각은 별도로 유지한다.

snapshot의 업무 데이터는 읽기 전용 트랜잭션에서 조회한다. 응답 순번은 이후 로컬 DB의 별도 카운터를 원자적으로 증가시켜 발급한다. 카운터는 업무 버전이나 권리·계약·의무에 영향을 주지 않는다. 이벤트의 순번은 생산자·시계별로 관리되며 API 재시작 시 새로운 식별자를 사용한다.

## 3단계 자체 검증 실행

검증은 Windows의 임시 X·Y API 프로세스와 WSL의 독립 PostgreSQL 17 인스턴스 두 개를 사용한다. 위의 DB 접속 정보와 같은 별도의 **테스트 전용 DB**를 준비한다. 기존 Docker 데이터나 운영 DB는 테스트 대상으로 사용하지 않는다. 검증기는 `XY_PG_X_PASSWORD`와 `XY_PG_Y_PASSWORD`를 프로세스 환경에서 읽고 API 토큰은 검증 실행마다 자체 생성한다.

```powershell
Set-Location implementations/db-api
$evidence = 'logs/stage3-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
.\.venv\Scripts\python.exe stage3_verify.py --output $evidence --wsl-db-restarts
```

- `--wsl-db-restarts`는 이 검증에 새로 준비한 `/tmp/model-a-xy-pg17/x/data`와 `y/data`의 인스턴스에서만 사용한다. 옵션을 생략하면 해당 두 재시작 케이스는 환경 미확인으로 기록된다.
- `--case AC-08 --case AC-09`처럼 범위를 선택해도 각 경합 케이스는 새 실행 10회를 유지한다. AC-11의 X→Y 부분은 공통 지연 중계기를 사용해 첫 응답 10초 보류와 5초 호출 한도를 5회 적용한다.
- 매 실행은 공통 fixture에서 S0만 준비한다. Repo와 대차는 실제 HTTP로 실행한다. DB 트리거와 통신 중계기를 사용한 추가 복구·회귀 검증은 별도 실행으로 기록한다.
- `summary.json`에서 기능 판정과 증거를 포함한 최종 판정을 확인한다. 실행별 `events.jsonl`은 X·Y DB에서 내보낸 공통 이벤트 원본이고 `snapshots.jsonl`은 응답 원본이다. `http.jsonl`에는 인증 역할과 호출 ID만 남기며 인증 헤더 값은 기록하지 않는다. `faults.jsonl`은 주입 시점과 장애 진단을 기록한다.
- 실제 처리 구간의 중첩이나 권리 소진→생성 인과관계가 증명되지 않으면 최종 판정은 `INCONCLUSIVE`다. 전체 결과에 `FAIL` 또는 `INCONCLUSIVE`가 있으면 자체 검증기는 비영 종료 코드를 반환한다. 이 자체 검증의 판정 범위는 X→Y 개발 검증이다.

### 3단계 실제 PostgreSQL 검증 결과

2026-11-21에 Windows API 두 개와 WSL의 PostgreSQL 17.12 테스트 인스턴스 두 개에서 새 실행 ID로 58회를 수행했다. 검증 종료 후 두 테스트 DB를 정상 종료했다.

| 범위 | 실행 결과 |
|---|---|
| AC-03~05 거절 | **19 PASS**. 인증·권한·형식·업무 조건·업무 버전 거절과 업무 상태 보존을 확인했다. |
| AC-06·07 중복·충돌 | **3 PASS**. X·Y의 동일 요청 재전송과 Y의 요청 ID 충돌 후 원래 결과 보존을 확인했다. |
| AC-08·09 경합 | **20 PASS**. 같은 요청, 서로 다른 요청을 각각 10회 동시에 제출했다. 서버 이벤트로 처리 구간 중첩을 확인했고, 각 실행에서 Y 거래 한 건만 확정됐다. |
| AC-11의 X→Y 부분 | **5 PASS**. Y 첫 확정 응답을 10초 동안 보류했으며, 원래 결과 조회와 동일 요청 재전송으로 S2를 확인했다. |
| Model A 추가 복구 | **4 PASS, 1 INCONCLUSIVE**. 내부 응답 유실, 예약 후 Y API 재시작, X·Y PostgreSQL 재시작에서 동일 요청 복구를 확인했다. X API 재시작 케이스는 S2에 도달했지만 아래 증거 공백이 남았다. |
| 관측 안정성 | **2 PASS**. X와 Y 각각에서 업무 커밋 후 이벤트 쓰기를 막고 API를 종료했다. 재시작 후 `snapshot_seq`가 이어졌고, 남아 있던 대기열에서 확정·반영 이벤트가 각각 한 번 기록됐다. |
| 2단계 회귀 | **4 PASS**. 정상 S0→S1→S2, 예약·소진 직후 쓰기 실패에서의 재개, 예약 해제 후 최종 거부를 다시 확인했다. |

`a-recovery-restart-X-api-39cb793857`에서는 B권리 소진 후 첫 확인 응답을 Y에 전달하지 않고 X를 재시작했다. 재시작 후 같은 요청이 정상 확정됐지만, 새 X 시계에서 전달된 응답을 이전 X 시계의 소진 이벤트와 연결할 공통 메시지 증거가 없었다. 기능 판정은 `PASS`, 최종 판정은 `INCONCLUSIVE`이며 배치는 종료 코드 1을 반환했다.

로컬 증거는 `logs/stage3-20261121T070603593Z/`에 있다. `summary.json`은 58회 개별 판정과 실행 ID를 담는다. `manifest.json`은 구현·공통 입력·증거 파일의 SHA-256을 담는다. 이벤트 1,279개와 snapshot 248개를 공통 Schema로 다시 확인했고, 비밀값 검사를 거친 HTTP 기록 461개를 보존했다. 2단계 검증 기록은 그대로 유지했다. Docker Compose와 공통 인수 실행은 이번 배치 범위 밖이며 미실행이다.

### X 재시작 인과관계 보완 재검증

기존 `a-recovery-restart-X-api-39cb793857`는 재시작 전 X 시계의 순번 572에서 소진을 기록했다. 이후 Y가 받은 응답에는 새 X 시계에서 생성한 다른 HTTP `message_id`가 사용됐다. Y의 결과 조회와 snapshot은 단일 확정과 S2를 보여 주지만 이 둘만으로 이전 시계의 소진과 Y의 권리 생성을 연결할 수 없었다. 이전 배치의 판정과 증거는 보존했다.

보완 후 PostgreSQL 17.12 대상 재검증은 **5 PASS**였다. `a-recovery-restart-X-api-4440b646a0`에서 기존 X 시계의 소진(순번 40)과 영속 확인 발행(41), Y 시계의 실제 확인 수신(22)과 권리 생성(23)을 같은 영속 `message_id=2c7094e388464222b2be4ed196084cad`로 연결했다. 새 X 시계가 전달한 HTTP 응답에는 별도 ID가 있었다. 대상 배치의 원본과 파일 해시는 `logs/stage3-causal-target-20261121T072029620Z/`에 보존했다.

대상 검증이 끝난 뒤 같은 구현으로 기존 58개 케이스를 모두 재실행했다. **58 PASS, 0 INCONCLUSIVE, 0 FAIL**이었고 검증기는 종료 코드 0을 반환했다. 전체 배치의 `a-recovery-restart-X-api-03135df415`에서도 X 소진(순번 608)→발행(609)과 Y 수신(22)→권리 생성(23)을 영속 `message_id=f7b1b9f28bd44d499bcdf84af25b9ee6`로 연결했다. 서로 다른 요청과 같은 요청의 경합을 각각 10회 다시 수행해 모든 실행에서 서버 처리 중첩과 단일 거래 확정을 확인했다.

전체 회귀 배치는 `logs/stage3-causal-full-20261121T072029620Z/`에 보존했다. 이벤트 원본 1,412개와 snapshot 248개를 공통 Schema로 확인했고, 인증값을 제외한 HTTP 기록 448개를 검증했다. 두 배치 모두 `summary.json`에 판정과 실행 ID, `manifest.json`에 구현·공통 입력·원본 증거의 SHA-256이 있다. 두 임시 PostgreSQL 서버는 정상 종료했다. 이 결과는 X→Y 자체 검증에 해당하며 Docker Compose 및 공통 인수 실행은 미실행이다.

[3단계 검증 요약 JSON](verification/xy-stage3-postgresql-2026-11-21.json)에서 전체 58건의 판정과 검증 환경, 원본 요약·manifest의 SHA-256을 확인할 수 있다. 원본 로그는 로컬에 보존하며, 기록된 해시를 로그와 대조하려면 로컬 원본이 필요하다.

## 2단계 검증 결과

Windows Python 3.12.2의 X·Y API를 WSL Ubuntu 26.04.1의 서로 독립된 PostgreSQL 17.12와 연결했다. 네 실행 모두 새 `run_id`에서 공통 F0와 명령 fixture를 사용했다. SQL로 준비한 것은 S0까지이며 Repo와 증권대차는 실제 API로 제출했다. 장애 주입은 해당 임시 DB와 실행에만 한정했다.

| 실행 ID | 판정 | 확인한 결과 |
|---|---|---|
| `xy-normal-7699df9a` | PASS | S0→S1→S2, 원래 요청 결과 조회, readiness의 S0 거짓·S1 참 및 조회 전후 snapshot 동일성, X·Y snapshot, 계약 2건·의무 4건. DB 이벤트 24개의 공통 Schema와 X 소진→Y 생성 인과관계를 확인했다. |
| `xy-reserved-fault-86d63335` | PASS | X에서 예약 후 소진을 기록하는 쓰기를 실패시켰다. B은행 권리는 보존된 채 사용이 제한됐고 C은행 권리와 새 버전은 생기지 않았다. 장애 해제 후 동일 요청이 한 번만 확정됐으며 재제출은 같은 결과를 반환했다. 이벤트 24개를 검증했다. |
| `xy-consumed-fault-a3d1e778` | PASS | X에서 B은행 권리를 소진한 뒤 Y의 C은행 권리 쓰기를 실패시켰다. 결과는 `PENDING`이었고 C은행 권리와 버전 2는 아직 없었다. 장애 해제 후 동일 요청으로 S2에 도달했고 활성 예약과 진행 상태가 없어졌다. 이벤트 29개를 검증했다. |
| `xy-abort-fac865ff` | PASS | 예약 전 쓰기를 막는 동안 Y의 테스트용 합의 행에서 담보 제공 조건을 무효화했다. 이후 X 예약의 `RELEASED`를 확인한 뒤 Y가 `REJECTED`로 끝났다. B은행 권리는 다시 사용 가능했고 버전은 1로 유지됐다. |

실행별 판정, 입력 fixture, 출력 발췌, 검증 시점 구현 파일 해시를 [Git 보존용 검증 기록](verification/xy-postgresql-2026-11-21.json)에 담았다. 당시 공통 Schema 검증은 실제 DB의 이벤트를 조회해 수행했다. 검증 기록에는 해당 검사 결과와 콘솔 출력 발췌를 보존했다. 개별 이벤트 원본 JSONL, HTTP 원본 응답, fault driver는 저장하지 않았다.

| 검증 | 결과 |
|---|---|
| Compose 구성 및 Python 구문 | PASS. `docker compose config --quiet`, Python 3.12.2 구문 검사 |
| DB 없는 검사 | PASS. PostgreSQL DDL 10개·부분 유일 인덱스 4개 컴파일, HTTP 400 입력 검사 5건, 논리 결과 6개·이벤트 객체 14개 Schema 검사, 모의 상태 전이 8건. |
| 2단계 Compose 이미지 빌드 | FAIL. Docker BuildKit이 `metadata_v2.db` 쓰기 중 `read-only file system`을 반환했다. Docker Desktop WSL에서 블록 장치 I/O 오류도 관측됐다. |
| 2단계 Compose 기동·거래 재검증 | NOT_RUN. Compose 이미지 빌드 중단. |
| 공통 인수 테스트 | NOT_RUN. 연구 총괄의 초기 적재·이벤트 수집 연결이 남아 있다. |
| 전체 경합·API/DB 재시작 후 복구 | NOT_RUN. 위 장애 테스트는 프로세스가 기동된 상태에서 지정한 DB 쓰기를 막고 해제한 사례다. |

WSL 첫 초기화 시도에서는 권한 전환 후 `/dev/stdin` 비밀번호 파일을 다시 열지 못했다. 새 임시 경로에서 소유자가 읽을 수 있는 `0600` 파일을 사용해 재실행했고, 두 DB의 SCRAM 인증 접속을 확인했다.

## 1단계 검증 결과

2026-11-21, Windows의 Docker Desktop·WSL2 환경에서 확인했다. Docker Server는 27.1.1, Compose CLI는 v2.29.1-desktop.1, X API 컨테이너의 Python은 3.12.16이다.

| 항목 | 결과 |
|---|---|
| `docker compose config --quiet` | PASS |
| Python 3.12.2에서 `app.py` 구문 파싱 | PASS |
| 최초 이미지 빌드·컨테이너 기동 | PASS. 담당자가 `docker compose up -d --build`를 실행해 최초 이미지 빌드와 컨테이너 기동을 확인했다. 이후 `docker inspect`에서 API·PostgreSQL 컨테이너 4개가 모두 healthy임을 확인했다. |
| X `GET /health/db` | PASS. HTTP 200, `status=ok`, `platform=X`, `database=model_a_x`, PostgreSQL 17.12 |
| Y `GET /health/db` | PASS. HTTP 200, `status=ok`, `platform=Y`, `database=model_a_y`, PostgreSQL 17.12 |
| 네트워크·볼륨·포트 격리 | PASS. `docker inspect`에서 X 쌍은 `db-api_x`, Y 쌍은 `db-api_y`에만 연결됐다. DB 볼륨은 각각 `db-api_x-data`, `db-api_y-data`였고 두 DB의 5432 포트는 호스트에 게시되지 않았다. API는 각각 루프백의 8101·8102에 게시됐다. |
| DB 접근 격리 | PASS. `docker exec`로 각 API 안에서 TCP 5432를 검사했다. 자기 DB 서비스는 연결됐다. 상대 DB는 서비스 이름 조회에 실패했고, `docker inspect`에서 얻은 컨테이너 IP로 직접 연결을 시도했을 때도 양방향 모두 타임아웃됐다. |
| Git 자격 정보 확인 | PASS. `.env`와 `.env.*`는 Git 제외 대상이고 인덱스에 없다. `env.example`의 두 값은 비어 있으며, 로컬 `.env`의 두 비밀번호 값은 환경 PR 후보 7개 파일에 없다. |
| 공통 인수 테스트 | NOT_RUN. 금융 거래와 연구 총괄의 초기 적재 프로그램·실행기 연결이 후속 단계에 해당한다. |

이전 자동 실행에서는 Docker 파이프 접근 오류와 HTTP 500이 발생했다. 이번에는 담당자가 기동한 환경에서 HTTP 점검과 권한 승인 후 Docker 런타임 검사를 수행했다.

## 이후 단계와 인계

이번 결과는 **2단계 최소 흐름 개발 공유**다. 연구 총괄의 초기 적재와 이벤트 수집 연결 뒤 공통 실행기에서 X→Y 필수 항목을 검증한다. 총괄 검토와 사용자 승인에 따른 X→Y 기준 버전 보존을 마친 뒤 Z를 추가한다. 최종 인계에는 [공통 실행기](../../tests/conformance/README.md)의 AC-01~11 필수 변형·반복·증빙 통과가 필요하다.
