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

API 시작 시 [storage.py](storage.py)의 SQLAlchemy 메타데이터가 각 DB에 아래 10개 테이블을 생성한다. 두 DB의 스키마는 같으며, X는 Repo와 예약을, Y는 증권대차와 복구 진행을 담당한다. `runs`를 제외한 모든 테이블은 `run_id`를 외래 키로 참조한다.

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
| `events` | `run_id, event_id` | 공통 이벤트 객체를 `payload` JSONB에 저장한다. |

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

권리 변경 이벤트는 해당 DB 변경과 함께 커밋된다. 확정·로컬 반영 시각은 DB 트랜잭션 성공을 확인한 뒤 기록한다. DB 커밋 직후 프로세스가 종료되어 시각 증빙이 누락되면 가짜 이벤트를 복원하지 않는다. 원본 수집기의 자동 연결과 전체 장애·경합 계측은 후속 검증 항목이다.

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
