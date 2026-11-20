# Model A — DB/API

담당: **연수인**. 현재 단계는 X(Repo)·Y(증권대차)의 환경과 기본 DB 연결이다. 실제 거래·권리 전이·Z는 후속 단계에 해당한다. 공통 기준은 [루트 안내](../../README.md), [AGENTS.md](../../AGENTS.md), [업무·인터페이스 명세](../../spec/), [공통 데이터 계약](../../shared/README.md)을 따른다.

## 기술 스택과 구성

| 항목 | 버전 및 구성 |
|---|---|
| 런타임 | Docker 이미지 `python:3.12-slim` |
| API | FastAPI 0.135.1, Uvicorn 0.41.0 |
| DB 및 드라이버 | Docker 이미지 `postgres:17`, SQLAlchemy 2.0.48, psycopg 3.3.3 |
| 실행 도구 | Docker Compose v2. Windows 검증 환경의 CLI는 v2.29.1-desktop.1 |

Python과 PostgreSQL 이미지 태그는 마이너·패치 업데이트를 허용한다. 실제 실험 환경을 기록할 때는 컨테이너에서 확인한 런타임 버전과 이미지 식별자를 사용한다.

| 플랫폼 | API | DB 서비스와 DB 이름 | 전용 네트워크와 볼륨 |
|---|---|---|---|
| X | `http://127.0.0.1:8101` | `db-x` / `model_a_x` | `x` / `x-data` |
| Y | `http://127.0.0.1:8102` | `db-y` / `model_a_y` | `y` / `y-data` |

두 API는 같은 Dockerfile에서 빌드하며, 각 프로세스는 자기 DB의 전용 네트워크와 계정으로 연결한다. API 포트는 로컬 호스트에 바인딩하고 DB 포트는 Compose 네트워크 안에서만 사용한다. `GET /health/db`는 DB에서 직접 SQL을 실행해 DB 이름과 접속 사용자를 확인한 뒤 플랫폼, DB 이름, DB 서버 버전을 반환한다. 연결이나 DB 식별 확인에 실패하면 HTTP 503을 반환한다.

## 환경 변수와 실행 방법

Windows PowerShell에서 Docker Desktop의 Linux 엔진이 정상 응답하는지 확인한다. 저장소 루트에서 다음을 실행한다.

```powershell
Set-Location implementations/db-api
docker version --format '{{.Server.Version}}'
docker compose version
Copy-Item env.example .env
```

`Copy-Item`은 최초 설정 때 실행한다. `.env`의 `POSTGRES_X_PASSWORD`, `POSTGRES_Y_PASSWORD`에 각각 다른 로컬 비밀번호를 설정한다. `.env`는 Git에서 제외한다. Compose는 해당 값을 각 DB의 `POSTGRES_PASSWORD`와 연결 API의 `DB_PASSWORD`에 전달한다. `PLATFORM`, `DB_HOST`, `DB_NAME`, `DB_USER`, `POSTGRES_DB`, `POSTGRES_USER`는 Compose 서비스별로 설정한다.

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

## 환경 PR 범위

| 파일 | 내용 |
|---|---|
| `compose.yaml` | X·Y API와 PostgreSQL 서비스, 전용 네트워크·볼륨, healthcheck |
| `Dockerfile`, `.dockerignore`, `requirements.txt` | Python 이미지 빌드, 빌드 컨텍스트 제한, Python 의존성 버전 |
| `app.py` | 플랫폼별 SQL 연결과 DB health API |
| `env.example` | 비밀번호 변수 이름과 빈 값 |
| `README.md` | 기동·종료 절차와 검증 결과 |

거래 기능, 초기 데이터 적재 프로그램 및 공통 인수 테스트는 아래 후속 단계에서 진행한다.

## 이후 단계와 인계

초기 데이터 적재 프로그램은 연구 총괄이 작성한다. Model A 담당자는 플랫폼별 DB 구조와 권한, 기동·로그 수집 방법을 제공한다. 이후 X→Y에서 공통 API, readiness, snapshot, 결과 조회, 권리·의무·멱등성·경합·복구와 공통 이벤트를 검증한다. 총괄 검토와 사용자 승인에 따른 X→Y 기준 버전 보존을 마친 뒤 Z를 추가한다. 최종 인계에는 [공통 실행기](../../tests/conformance/README.md)의 AC-01~11 필수 변형·반복·증빙 통과가 필요하다.
