# 공통 검증·실험 실행기

최종 인계 이후의 별도 실험 세션은 [반복 실험·집계·그래프·해석 인수인계](EXPERIMENT_HANDOFF.md)를 시작점으로 사용한다. 미리 작성한 안내이며 실제 인계 완료·실험 시작 승인은 아니다.

B1 Z의 개발 중 연결은 [B1 XYZ 실행 안내](B1_XYZ_RUNBOOK.md)를 따른다.
PR #9의 `collateral` 0.4.0과 새 `b1-xyz` 환경을 대상으로 하며 실제 공통 XYZ는 NOT_RUN이다.

상태: 준비 검증과 후속 1~5번의 실행기 코드·자체 시험을 구현했다. 현재 명령·증거 연결 형식은 제14절을 따른다. 실제 A·B 구현·적재·인증·구현 로그·Canton 원본 이벤트 연결은 미완료이며 실제 인수·실험 결과는 없다.

## 1. 목적과 담당 범위

공통 실행기는 이서진이 관리하며 다음을 수행한다.

1. A·B1·B2에 동일한 테스트 입력을 전달하고 업무 정합성을 검증한다.
2. 필수 테스트 통과 여부를 확인하여 최종 개발 완료를 판정한다.
3. 공통 실험 조건으로 반복 실행하고 원본 관측값을 보존한다.
4. 원본 관측값에서 수치 요약표와 발표·리서치용 그래프를 생성한다.

업무 처리와 권리·의무의 관리는 각 구현이 수행한다. 실행기는 공통 API로 명령을 전달하고 결과·상태·이력을 관찰한다.

| 기준 문서 | 실행기의 적용 내용 |
|---|---|
| [business.md](../../spec/business.md) | 업무·권리·의무·상태 전이 |
| [interface.md](../../spec/interface.md) | 공통 명령·응답·조회·권한 |
| [acceptance.md](../../spec/acceptance.md) | 테스트·판정·최종 완료 조건 |
| [measurement.md](../../spec/measurement.md) | 계측·실험 조건·통계·증빙 |
| [shared 안내](../../shared/README.md) | Schema·fixture·정규화 결과 |

이 문서는 공통 명세를 중복 정의하거나 변경하지 않는다. 충돌은 연구 총괄에게 보고한다.

## 2. 사용 흐름

| 용도 | 사용 시점 | 수행 내용 | 결과 |
|---|---|---|---|
| 개발 중 검증 | 구현 담당자가 기능을 추가·수정했을 때 | 모델·단계·테스트를 선택하여 확인 | 해당 범위의 통과·실패와 원인 |
| 최종 완료 검증 | 담당 구현을 인계하기 전 | 필수 목록·변형·반복·증빙 전체 확인 | 최종 인계 가능 여부 |
| 본 실험 | 정합성 검증을 통과한 버전 확보 후 | T1~T4를 정해진 조건으로 실행 | 원본 로그와 실행별 측정값 |
| 분석·출력 | 실험 완료 후 | 저장된 결과를 검증·집계·시각화 | 수치 요약표와 그래프 |

개발 중 일부 테스트의 통과를 최종 완료로 표시하지 않는다. 분석·출력은 시스템을 다시 실행하지 않고 저장된 원본 결과로 재생성할 수 있어야 한다.

명령으로 실행하는 프로그램부터 구현한다. 웹 화면·대시보드는 이번 범위에 추가하지 않는다. 실제 실행 명령은 구현 후 검증된 명령으로 문서화한다.

## 3. 실행 전 준비

구현 담당자가 자기 시스템을 기동한다. 실행기는 연결·초기 상태를 확인한다. 모델별 기동 자동화는 필요할 때 별도 검토한다.

구현 담당자는 다음을 제공한다.

- 모델별 기동·종료 방법과 X·Y·Z 접속 정보
- 역할별 인증 설정 방법
- DB 구조·초기 데이터 생성 경로 또는 Daml 계약 생성·Party·Synchronizer 연결 방법. 초기 데이터 적재 코드 작성은 연구 총괄 담당
- 구현 버전과 실행 환경 정보
- 검증에 필요한 구현 로그·이력의 수집 방법

새 `run_id` 생성, fixture 선택, 모델별 초기 데이터 적재 기능 작성과 실행기 연결은 **이서진이 담당**한다. 구현 담당자는 내부 구조·설정·생성 방법을 제공하고 변경 시 알린다. 담당자에게 매번 수동 적재를 요청하는 방식으로 운영하지 않는다.

준비 기능은 새 `run_id`와 fixture 경로를 입력받고 성공·실패를 반환하는 최소 구성으로 만든다. A는 독립 DB별 초기 데이터를, B는 실제 초기 계약과 필요한 권한·할당을 준비한다. 사용 중인 run_id는 덮어쓰지 않으며 준비 실패 시 업무 측정을 시작하지 않는다. 준비 후에는 실행기가 공통 snapshot으로 초기 상태를 확인한다. 적재·준비 시간은 거래 성능 측정에서 분리한다. 기존 공통 API에 관리 서버나 삭제·초기화 API를 추가하지 않는다. 정확한 호출 명령은 구현별 구조를 확인한 뒤 기록한다.

실행기는 요청 전 연결 상태와 설정을 확인하고, 준비 실패와 업무 처리 실패를 구분해 기록한다. 초기 데이터 적재 방법은 모델별로 달라도 되지만, 초기 상태의 의미는 공통 fixture와 같아야 한다. 비밀값은 저장소·결과물에 포함하지 않는다.

## 4. 개발 완료 판정

| 모델 | 필수 검증 |
|---|---|
| A | AC-01~11 |
| B1 | AC-01~11 |
| B2 | AC-01·02·12 |

필수 변형·반복·증빙까지 모두 충족해야 최종 완료로 판정한다.

- 필수 항목에 FAIL·INCONCLUSIVE·NOT_RUN 또는 증빙 누락이 있으면 최종 인계 불가다.
- 기대된 업무 거절을 올바르게 처리한 경우 테스트는 PASS일 수 있다.
- 코드·명세·fixture가 변경되면 영향을 받는 검증을 다시 수행한다.
- 통과 보고서에는 검증 대상 버전과 실행 ID를 포함한다.
- A·B1의 AC-12는 NOT_APPLICABLE이다. B2의 나머지 공통 테스트는 수행 여부를 별도 명시하며 미실행을 통과로 표시하지 않는다.

최종 완료 확인 기능은 필수 조건을 모두 충족할 때만 종료 코드 0, 미충족이면 0이 아닌 코드로 종료한다. 이는 구현 요구사항이며 현재 자동 판정·GitHub 제출 차단 기능은 없다.

## 5. 본 실험과 결과 처리

기능 검증과 성능·비용 측정 결과를 구분한다.

- T1 정상 거래
- T2 중복·경합
- T3 응답 지연·복구
- T4 B2 재할당

반복 수·워밍업·조회 간격·시간 계산·통계 방식은 기존 계측 명세를 그대로 따른다. B2의 정상 실험에서 재할당 관측을 함께 수집하여 불필요한 중복 실험을 줄인다.

실험 중 정합성 위반이 발견되면 해당 배치를 정상 비교 결과로 확정하지 않고, 실패 증거를 보존하여 수정·재검증한다.

실패·미완료·계측 무효 실행도 시도 수와 사유를 남긴다. 성공 건수를 채우기 위한 자동 보충 실행은 하지 않는다. 추가 실험은 별도 배치로 구분한다.

## 6. 보관할 산출물

저장소 루트 기준 로컬 `results/<배치ID>/`에 배치마다 고유한 보관 폴더를 만든다. 배치 ID는 여러 개별 `run_id`를 묶는 식별자다. 기존 배치 결과를 덮어쓰지 않는다. 세부 파일명은 후속 구현 설계에서 정한다.

| 산출물 | 내용 |
|---|---|
| 실행 정보 | 배치 ID, 코드·명세·fixture 기준, 실행 순서, 환경, 비밀값을 제외한 설정 |
| 원본 기록 | 공통 관측 이벤트, 요청·응답, snapshot, 필요한 구현 로그 |
| 검증 보고서 | 테스트·변형별 판정, 기대/실제 결과, 증거 위치 |
| 실행별 결과표 | 각 실행의 측정값, 성공 여부, 유효성, 제외 사유 |
| 집계표 | 표본 수, P50·P95·최솟값·최댓값, 전체 시도와 상태별 건수 |
| 그래프 | 동일 집계표와 실행별 데이터에서 생성한 비교 그림 |
| 한계·미측정 목록 | 환경 제약, 미측정 지표, 수집하지 못한 증거 |

기계가 다시 읽는 기록은 JSON·JSONL, 수치표는 CSV, 사람이 읽는 요약은 Markdown을 기본으로 한다. 그래프는 발표용 PNG와 확대·편집용 SVG로 내보낸다.

발표용 확정 결과는 원본 배치·계산 규칙을 추적할 수 있도록 선별하여 GitHub에 보관한다. 대용량 원본은 별도 공유 보관소에 보존하며, 위치·접근 방법은 후속 확정한다. 로컬 전체 결과나 대용량 원본을 일괄 커밋하지 않는다. 커밋·푸시는 실행 전 사용자 확인을 받는다.

## 7. 기본 결과표와 그래프

자유롭게 구성하는 대시보드 대신 고정된 표·그래프를 자동 생성한다.

| 연구 항목 | 기본 출력 |
|---|---|
| 담보 사용 가능·거래 완료·연속 활용 지연시간 | A·B1 비교표, 실행별 분포와 P50·P95 표시 |
| 상태 일관성·이중 배정 | 테스트별 판정과 위반 건수표, 위반 시 증거 |
| 장애 복구 비용 | 복구시간·조회 수·재시도 수·미완료 건수표 |
| 플랫폼 연결 복잡성 | 인터페이스·변경 모듈·메시지·예외 경로별 근거표 |
| Synchronizer 재할당 비용 | B1·B2 비교표, 계약별 재할당 비용과 관련 시간 그래프 |

정상·경합·장애·재할당 결과는 구분한다. 그래프에는 단위·표본 수·조건을 표시한다. 서로 다른 지표의 P95를 더해 전체 P95로 표시하지 않는다. 정상 성공 표본의 시간 통계와 전체 시도·실패·미완료 수를 함께 제시한다.

플랫폼 연결 복잡성은 실행 로그만으로 완성하지 않는다. 담당자가 제공한 구조·변경 내역과 근거를 총괄이 검토한다.

## 8. 구현 순서와 다음 확정 항목

1. 공통 API 연결, 초기 상태 확인, 원본 기록 저장
2. X→Y 단계 검증을 제공하여 팀원 개발에 활용
3. Z·권한·중복·경합·복구 검증과 최종 완료 판정
4. 정해진 반복 실험과 계측
5. 저장된 결과의 집계·그래프 출력
6. B2 구현에 맞춰 재할당 검증·계측 연결

실행기 자체도 잘못된 상태·누락된 증거를 통과시키지 않는지 확인한다. 시간 계산과 집계는 기대값을 아는 작은 데이터로 검증한다. 모의 응답으로 실행기를 검증한 결과는 실제 A·B1·B2의 통과·측정 결과로 사용하지 않는다.

다음 설계에서는 아래 항목을 구체화하고 연구 총괄 검토 후 구현한다. 아래 순서는 개발 순서이며 후속 기능도 전체 구현 범위에 포함된다.

- 의존성의 실제 검증 버전·설치 방법
- 모델·플랫폼 주소·역할별 인증·새 run_id 및 fixture 준비·로그 수집 연결 방식
- 개발 중 검증·최종 완료 검증·본 실험·분석의 실제 명령과 설정 형식
- 배치 폴더의 파일 구성·원본에서 표·그래프로 이어지는 추적 방법
- 최종 그래프 목록·표현 방식과 원본 공유 보관소

## 9. 최소 기술 구성과 프로그램 구조

| 항목 | 선택 | 용도 |
|---|---|---|
| 언어 | Python 3.12 | 공통 실행·검증·집계 |
| HTTP | httpx | API 호출·동시 요청·타임아웃 |
| 형식 검증 | jsonschema | 기존 JSON Schema 활용 |
| 그래프 | matplotlib | PNG·SVG 출력 |
| 설정·저장·통계·명령 처리 | Python 표준 라이브러리 | JSON·JSONL·CSV·P50/P95·명령 해석 |

의존성의 정확한 버전은 실제 설치·검증 후 고정한다. 별도 DB·메시지 큐·웹 화면·플러그인 구조는 추가하지 않는다. 동시 요청은 경합 테스트와 관측·복구에 필요한 범위에서 사용한다.

프로그램은 `runner.py`(명령·호출·관측·반복), `checks.py`(상태·이력·완료·재할당 검사), `report.py`(집계·그래프), 자체 시험 파일들, `requirements.txt`, `config.example.json`으로 구성한다. `fault_proxy.py`는 첫 Y 응답만 보류하는 작은 로컬 중계다. 모델별 데이터 준비 코드는 해당 구현 폴더의 실제 구조를 확인하여 총괄이 작성하고, 별도 실행기 프레임워크로 확장하지 않는다.

설정에는 모델 이름·X/Y/Z 주소·역할별 인증 환경변수 이름·로그 및 준비 기능 연결 정보를 둔다. 비밀값은 저장하지 않는다. 공통 반복·관측·재시도 조건은 계측 명세를 적용하며 모델별로 임의 변경하지 않는다.

## 10. 명령과 결과 파일의 설계 방향

다음은 명령 역할의 제안이며 실행 가능한 명령이 아니다. 실제 인자·설정 형식은 구현 전 검토한다.

| 역할 | 명령 형태 |
|---|---|
| 선택한 개발 테스트 | `check --model A --case AC-09` |
| 필수 조건 전체 완료 확인 | `check --model A --final` |
| 명세 조건의 반복 실험 | `experiment --model A --suite T1` |
| 원본 기반 재집계·그래프 출력 | `report --batch <배치ID>` |

최종 완료 검사와 일부 테스트 선택은 함께 허용하지 않는다. A·B1·B2는 같은 공통 API와 검증 기능을 사용하고 대상 설정으로 구분한다.

배치 결과는 `manifest.json`(기준·환경·설정), `events.jsonl`(원본 관측), `evidence/`(응답·snapshot·구현 로그), `checks.json`(판정·증거 참조), `runs.csv`(실행별 측정·상태·제외 사유), `summary.csv`(집계), `report.md`(요약), `figures/`(PNG·SVG)를 기본 구성으로 한다. 실제 필드와 출력 형식은 구현 전 검토한다. report 재생성은 원본 관측·증거를 변경하지 않는다. 중단된 배치도 기록을 보존한다.

## 11. 지연 주입·B2 증빙의 결정과 구현 책임

응답 지연 주입과 B2 계약별 증빙의 요구 동작·필수 기록·판정 조건은 연구 총괄이 정한다. 구현 담당자는 필요한 내부 처리·실제 이벤트 수집 경로를 제공하고 기술적 제약이 있으면 근거와 대안을 보고한다. 공통 요구사항을 임의로 완화하지 않는다.

지연 주입 기능은 총괄의 공통 도구에서 제어하며 최초 Y 응답만 지연한다. T3에서 최초 Y 제출의 응답 전달 경계에 작은 로컬 중계 기능을 사용한다. Y의 응답을 받은 뒤 10초 동안 보류하여 요청자에게 실제 타임아웃이 발생하게 하고, 조회·재전송은 정상 전달한다. 요청자 연결 종료로 업무 명령을 취소하지 않으며 보류 시작·해제 기록을 보존한다. 첫 응답이 PENDING이거나 실제 확정 증거가 부족하면 확정 결과 응답 지연을 재현했다고 인정하지 않는다. 실제 동작은 실행기 자체 시험과 구현 로그로 검증한다. 기존 10,000ms 추가 지연·5,000ms 응답 대기·조회/재전송 비차단·업무 처리 비중단 조건은 그대로 따른다.

B2는 기존 event Schema의 계약·재할당·source/target·시계·이벤트 유형을 재사용한다. 필요 계약 전체의 목록·선정 이유, 공통 업무 참조와 실제 Contract ID, 재할당 ID·실제 source/target·Unassigned/Assigned 원본 이벤트, 시계·사용 가능성 관측을 연결한다. 원본 참조는 배치 evidence/에 보존한다. Ledger API의 실제 필드·수집 경로와 사용 가능성 증명은 사용할 Canton 버전에서 확인한다. 계약별 수집을 논리 경계 전환 1회나 Y readiness 하나로 대신하지 않으며, 서로 다른 시계의 값으로 구간을 계산하지 않는다.

## 12. 설정 파일·준비 명령의 입출력

다음은 승인된 설정 형식이다. 주소·준비 프로그램·로그 경로는 예시이며 현재 존재하거나 실행 가능하다는 뜻은 아니다. 실제 구현 연결 후 값을 채운다.

```json
{
  "model": "A",
  "platforms": {
    "X": "http://localhost:8101",
    "Y": "http://localhost:8102",
    "Z": "http://localhost:8103"
  },
  "auth_env": {
    "bank-a": "POC_BANK_A_TOKEN",
    "bank-b": "POC_BANK_B_TOKEN",
    "bank-c": "POC_BANK_C_TOKEN",
    "experiment-observer": "POC_OBSERVER_TOKEN"
  },
  "prepare_command": [
    "python3",
    "implementations/db-api/prepare.py"
  ],
  "evidence_dir": "implementations/db-api/logs"
}
```

A·B1·B2 모두 같은 설정 형식을 사용한다. 인증 비밀값은 환경변수에서 읽고 명령·설정·결과물에 기록하지 않는다. 모델별 실제 인증 전달 방법은 interface.md의 권한 의미를 유지하며 연결 시 확인한다.

### 준비 명령 입력

실행기는 설정의 prepare_command 인자 목록 뒤에 다음을 붙인다.

```text
--run-id <새 실행 ID> --fixture <공통 fixture 파일의 절대 경로>
```

실행 기준 폴더는 저장소 루트다. 셸 문자열을 조합하지 않고 인자 목록으로 실행한다. 모델별 적재 코드는 연구 총괄이 각 구현에 맞춰 작성한다.

### 준비 명령 출력

성공 시 종료 코드 0, 표준 출력에는 다음 JSON 하나만 반환한다.

```json
{"run_id": "run-example-001", "status": "PREPARED"}
```

실패 시 종료 코드는 0이 아니며, 가능한 경우 다음 JSON을 반환한다. reason은 원인 식별용이며 아래 값은 기존 run_id 사용 시의 예다.

```json
{"run_id": "run-example-001", "status": "FAILED", "reason": "RUN_ALREADY_EXISTS"}
```

진행 로그는 표준 오류 출력으로 분리한다. JSON 누락·형식 오류·실행 중단·입력과 다른 run_id·종료 코드와 status의 불일치는 준비 성공으로 인정하지 않는다. 준비 실패 정보에서도 비밀값을 노출하지 않는다.

| 조건 | 처리 |
|---|---|
| 이미 사용한 run_id | 실패, 덮어쓰지 않음 |
| fixture 형식 오류 | 적재 전 실패 |
| 일부 적재 후 실패 | 실패 기록 보존, 해당 실행으로 측정하지 않음 |
| 준비 명령 성공 | 공통 snapshot으로 초기 상태 추가 확인 |
| 초기 상태 검증 실패 | 업무 요청·측정 시작하지 않음 |

PREPARED는 적재 기능의 성공 보고이며 테스트 PASS가 아니다. 초기 상태 확인 후 거래를 시작한다. fixture 경로·내용 해시·준비 결과를 배치 기록에 남긴다. 준비·검증 실패를 업무 처리 실패나 성능 표본으로 집계하지 않는다.

## 13. 첫 구현 사용법과 검증 범위 — 2026-11-18

첫 구현 범위는 **설정·fixture 검증 → 새 run_id → 준비 프로그램 → 초기 snapshot → 기록 저장**이다. `prepare`는 준비 단계만 검사하며 업무 명령을 제출하지 않는다. 이후 추가한 `check`, `check --final`, `experiment`, `report`는 제14절을 따른다.

### 설치·자체 시험

저장소 루트에서 실행한다. 확인한 환경은 macOS arm64, Python **3.12.13**, httpx **0.28.1**, jsonschema **4.26.0**, referencing **0.37.0**, matplotlib **3.11.2**다. 전역 Python 대신 저장소의 가상환경을 사용한다.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r tests/conformance/requirements.txt
.venv/bin/python -m unittest discover -s tests/conformance -p 'test_*.py' -v
```

자체 시험은 임시 준비 프로그램과 `127.0.0.1` 임시 포트의 HTTP 서버를 사용한다. 로컬 포트 실행 권한이 필요하다. 실제 DB/API·Canton 결과나 성능 표본이 아니다. 테스트의 임시 데이터는 종료 후 정리된다.

이번 환경에서 **자체 시험 16개 PASS**, `pip check`와 `git diff --check`도 통과했다. 잘못된 입력·참조·권한 매핑, 준비 실패/불일치/프로그램 부재, 초기 상태 오류·복제 모순, 비밀값 가림, 배치 충돌, X/Y 부분 관측, 전체 HTTP 시간 제한을 확인했다. 정상 예시 22개는 해당 Schema에 통과하고 의도적 오류 예시 3개는 거부했다. 시험 기록은 로컬 `results/self-tests/first-slice.json`에 있다. 예시 설정을 사용한 CLI 실행도 관측 자격 누락으로 적재 전에 종료 코드 1을 반환하고 실패 배치를 보존했다. 실제 A·B 연결 검증은 `NOT_RUN`이다.

### 실제 모델 연결 후 실행할 명령

`config.example.json`을 `config.local.json`으로 복사한 뒤 실제 주소·적재 명령·로그 경로·환경변수 **이름**을 채운다. 예시의 `prepare.py`는 존재하지 않는다. 실행기에서 준비 명령은 저장소 루트를 cwd로 사용하고, fixture의 절대 경로와 새 run_id를 전달한다. 모델별 실제 구조가 없으므로 이번에는 가짜 적재 코드를 추가하지 않았다.

HTTP 인증은 아직 실제 모델과 합의되지 않았다. 현재 지원하는 전달 방식은 `Authorization: Bearer <환경변수 값>`이며 `--observer-auth bearer`로 명시적으로 선택해야 한다. 이는 공통 인증 계약 확정이 아니며 실제 서비스가 이 방식을 지원하는지 연결 시 확인한다. 자격의 비밀값은 셸 기록·문서에 복사하지 말고 로컬 환경으로 제공한다. 준비 프로그램이 필요한 은행 자격은 같은 환경에서 읽는다.

```sh
.venv/bin/python tests/conformance/runner.py prepare \
  --config tests/conformance/config.local.json \
  --fixture shared/fixtures/initial/f0-normal.json \
  --observer-auth bearer \
  --platforms X Y
```

`--platforms` 생략 시 X/Y/Z를 모두 관측한다. 초기 X→Y 단계는 X/Y만 지정한다. 설정 형식은 동일하게 X/Y/Z 주소를 포함하되 선택하지 않은 플랫폼에는 호출하지 않는다. Z가 없는 준비 확인에서는 증거금 요구 전체의 가시성을 요구하지 않으며, 관측된 요구의 내용은 fixture와 비교한다. 거부 fixture도 해당 파일을 그대로 지정한다.

### 기록·판정·재현

- 매번 새 UUID 기반 배치·run_id를 생성한다. 기존 배치 폴더와 충돌하면 덮어쓰지 않고 실패한다. 원격의 기존 run_id 거부·부분 적재 정리는 실제 준비 프로그램 책임이며 실행기는 실패한 실행을 재사용하지 않는다.
- `results/<batch-id>/manifest.json`: 모델·플랫폼 범위, 비밀값을 뺀 설정, 입력 해시, Python/OS/CPU·의존성 버전, 명세·Schema·fixture·실행기 소스 해시. 해시는 미커밋 작업 트리 기준이며 `spec_frozen=false`다. 실제 DB·Canton·메모리 제한·내구성 설정은 연결 전 미수집이다.
- `evidence/fixture.json`, `prepare.json`, `snapshot-X.json` 등: 선택한 입력, 준비 종료 코드·stdout/stderr, HTTP 상태·응답 본문과 관측 시각. 응답 헤더와 인증 헤더는 저장하지 않는다. 환경에서 알려진 자격 값은 가리며, 가림 후 자료를 원문 그대로라고 해석하지 않는다. 구현 프로그램도 비밀값을 출력하지 않아야 한다.
- `checks.json`: 단계별 판정·증거 위치, 모든 AC의 `NOT_RUN`, `final_handoff_eligible=false`. 초기 상태 검증의 `PASS`와 종료 코드 0은 **선택한 준비 범위만** 성공했다는 뜻이다. 적재 실패·HTTP 관측 실패는 `INCONCLUSIVE`, 잘못된 입력(관측 자격 누락 포함)·초기 상태 불일치는 `FAIL`이며 0이 아닌 코드로 종료한다.
- `events.jsonl`: 유효한 모델 설정을 확보한 실행의 `RUN_FINISHED` 이벤트만 공통 Schema로 기록한다. 설정 자체가 잘못돼 모델을 알 수 없으면 이벤트를 꾸미지 않고 manifest/checks에 실패를 남긴다. 준비 관측 시각을 거래 성능 값으로 사용하지 않는다.
- 강제 중단 전에는 manifest를 저장한다. 준비 도중 중단·실행 실패 시 `prepare.incomplete`가 남을 수 있다. 중단된 원격 적재는 복구·재사용하지 말고 실제 구현에서 상태를 확인한다. 준비 시간 제한은 공통 명세에 없으므로 임의의 모델별 제한을 추가하지 않았다. HTTP snapshot은 호출 전체에 5초 한도를 적용한다.
- `prepare` 단독 실행에서는 구현 로그·업무 계측·CSV·그래프를 생성하지 않는다. 후속 명령의 로그 수집과 파생 출력은 제14절을 따른다.

초기 검사는 선택한 플랫폼들의 로컬 기록을 공통 ID로 연결한다. 동일 복제본은 한 번 세며, 중복 ID·모순된 복제본·잘못된 권리자/허용 업무·버전·수량·추가 계약/의무/배정/진행 상태를 거부한다. 전체 범위에서는 fixture의 모든 묶음·초기 권리·증거금 요구를 확인한다. 정상 F0의 조건을 거부 fixture에 강제하지 않는다.

**관측 한계:** snapshot에는 사전 합의와 principal 적재 결과가 없다. 입력의 참조·기관/관측기 의미는 검증하지만 실제 합의·자격 연결과 권한 격리, B2의 초기 할당은 아직 증명하지 못한다. 이후 업무·권한 테스트와 실제 구현 증거가 필요하다. 준비 `PASS`를 금융 업무·전체 AC 통과로 사용하지 않는다.

### Review Context

이번 변경은 실행기 파일·자체 시험·사용법과 로컬 결과/자격 파일의 ignore만 추가한다. 공통 명세·Schema·fixture·A/B 구현은 바꾸지 않는다. 검토 시 실제 모델의 준비 프로토콜, 로컬 snapshot 가시성 범위, 인증 전달 방식과 로그 연결을 확인한다. 커밋·푸시는 실행하지 않았다.

## 14. 후속 1~5 구현·사용법 — 2026-11-18

| 단계 | 구현·자체 검증 | 실제 연결 상태 |
|---|---|---|
| 1. X→Y | 정상·거부·멱등성·경합·조회/복구, 실제 소켓에서 응답 보류·타임아웃 시험 | A/B의 업무 처리·내부 이력은 미연결 |
| 2. Z·완료 판정 | AC-01~11 변형, 경합 각 10회·복구 5회, 누락·중복·증거 훼손 차단 | 실제 전체 AC는 NOT_RUN |
| 3. 반복·집계 | 워밍업 3+본 시도 30, T2 30, T3 5, 모델 순서 교대, 실패 보충 금지, 같은 시계·nearest-rank | 실제 비교 실험은 NOT_RUN |
| 4. 고정 그래프 | 지연·상태 판정·복구·재할당 PNG/SVG, 원본을 보존하는 재생성 | 자체 시험 그림에 SYNTHETIC 표기 |
| 5. B2 | 필요 계약·의존성·공통 참조·원본 참조, 실제 호출과 재시도, 계약별 시간/전체 구간 구분 | Canton 버전·수집 프로그램·실제 사건은 미연결 |

X/Y 실행기 시험 후 Z 추가 전에 `results/development-baselines/xy-runner/`에 당시 소스와 시험 근거를 보존했다. 이는 실행기 개발 기록이며 실제 모델의 X/Y 기준 커밋을 대체하지 않는다. 실제 모델의 Z 추가 변경 기록·기준 버전은 여전히 필요하다. 사용자 승인 없이 커밋·푸시하지 않는다.

### 검증·완료 확인 명령

먼저 실제 모델을 사람이 기동하고 제12절 형식의 설정과 환경 자격을 준비한다. 아래 로컬 설정 파일은 실제 모델 연결 후 만들어야 한다. 예시의 prepare.py는 여전히 존재하지 않는다.

```sh
# X/Y의 선택 개발 검증. 각 경합은 새 run_id로 10회 수행한다.
.venv/bin/python tests/conformance/runner.py check \
  --config tests/conformance/config.a.local.json --model A \
  --observer-auth bearer --stage xy --case AC-04 AC-06 AC-09

# A의 필수 변형·반복·증거 전체. --final과 --case/--stage xy는 함께 허용하지 않는다.
.venv/bin/python tests/conformance/runner.py check \
  --config tests/conformance/config.a.local.json --observer-auth bearer --final
```

`check --stage xy`는 부분 결과이며 AC 전체 PASS·최종 인계로 표시하지 않는다. `--case`를 생략하면 해당 단계의 전체 목록을 실행한다. 필수 목록은 `case_plan()`이 관리하며 사용자가 반복 수를 줄이는 옵션은 없다. AC-03은 X/Y/Z의 인증 누락·오류/명령 권한/타 기관 결과/snapshot 접근, 형식 오류 3종과 잘못된 플랫폼을 포함한다. AC-04·05는 모든 승인된 조건 변형, AC-06은 각 명령의 재전송을 검사한다.

초기 준비가 실패하면 `preparation_verdict`를 보존하고 업무는 `functional_verdict=NOT_RUN`, 인수 판정은 `INCONCLUSIVE`로 남긴다. 업무 응답과 상태가 맞지만 구현 이력이 없으면 `functional_verdict=PASS`와 `verdict=INCONCLUSIVE`를 구분한다. 최종 판정은 필수 변형·반복·새 run_id·원본 해시·이력·인계 근거가 모두 충족될 때만 종료 코드 0이다. 부분 검증의 종료 코드 0은 그 선택 범위만 의미한다.

### 실제 구현 로그의 로컬 연결 형식

공통 HTTP·업무 Schema는 변경하지 않았다. 아래는 실행기의 **로컬 파일 연결 규약**이며 실제 DB/원장 수집기는 모델 구조가 확인된 뒤 총괄이 연결한다.

- `<evidence_dir>/<run_id>/events.jsonl`: 기존 공통 event Schema를 따르는 실제 구현 이벤트. run_id/model/test_id를 유지한다. 준비 이벤트의 test_id=PREPARE도 허용한다.
- 업무 요청에는 진단용 `X-Test-ID`, `X-Attempt-ID`를 보낸다. 권한은 기존 인증에서 결정한다. 구현은 물리 호출의 attempt를 로그에 연결하고 producer/clock/sequence를 유지해야 한다.
- 확정 결과의 내부 효과와 `INTERNAL_COMMITTED`, 관련 `RIGHT_CONSUMED/CREATED/LOCKED`, `LOCAL_APPLIED`를 대조한다. 원래 권리 소진→새 권리 생성은 같은 생산자 시계의 순서 또는 메시지 ID로 연결된 실제 송·수신 인과 증거로 확인하며 시계 값을 직접 빼지 않는다.
- 경합은 실제 `COMMAND_RECEIVED/FINISHED`의 같은 생산자 시계에서 겹친 서로 다른 호출을 확인한다. 같은 요청 ID의 경합도 attempt를 나눠야 한다. 단순히 두 HTTP 응답이 같다는 이유로 동시 처리 안전성을 통과시키지 않는다.
- 구현 로그의 수집 종료·flush는 해당 모델에서 보장해야 한다. 누락·잘못된 필드·다른 실행·모순 이력은 통과하지 않는다. 원본은 비밀값을 가려 배치 evidence에 보존하고 가리기 전 해시도 기록한다.
- `<evidence_dir>/context.json`: `implementation_version`, 현재 구현 파일의 `implementation_files` 경로→SHA256, 보존된 `xy_baseline_commit`, `z_change_record` 경로, `environment`가 필요하다. environment에는 os/cpu/memory/runtime/database_or_canton/topology/resource_limits/durability/network/service_restart를 실제 값으로 기록한다. 기준 커밋 존재·현재 파일 해시·변경 근거 파일을 확인한다. 커밋을 대신 만들지 않는다.
- 선택적인 `complexity`의 interfaces/changed_units/exceptions 목록은 각 항목의 id/kind/evidence_ref를 보존한다. 유형별 수는 **검토용 제공 자료**이며 코드가 구조적 타당성까지 승인한 것은 아니다. 실제 메시지는 event의 message_id/kind/category로 별도 계수한다. 복제된 송·수신은 중복 계산하지 않는다.

실제 로그의 정규화가 올바른지는 원 DB·Canton 원본과 함께 연결 검증해야 한다. `context.json`의 문서 내용이나 어떤 프로그램이 출력한 JSON만으로 실제 구현의 진실성을 자동 인증할 수는 없다.

### 반복 실험과 재분석

실험 전에 같은 코드·명세·fixture·설정·구현 버전으로 완료된 `check --final` 배치를 `--validation-batch`로 제공한다. 없거나 근거가 바뀌었으면 업무를 보내지 않고 계획 항목을 NOT_RUN으로 보존한다.

```sh
.venv/bin/python tests/conformance/runner.py experiment --suite T1 \
  --config tests/conformance/config.a.local.json tests/conformance/config.b1.local.json \
  --observer-auth bearer \
  --validation-batch results/ACTUAL_A_FINAL_BATCH \
  --validation-batch results/ACTUAL_B1_FINAL_BATCH \
  --a-collect-command .venv/bin/python implementations/db-api/readiness_evidence.py collect \
    http://127.0.0.1:8103 /run/secrets/model_a_readiness_evidence \
    implementations/db-api/collect_events.py xyz

.venv/bin/python tests/conformance/runner.py report --batch results/ACTUAL_EXPERIMENT_BATCH
```

Model A를 포함하면 `--a-collect-command`에 최종 기능 검증 때 연결한 실제 수집 argv를
지정한다. 위 주소와 파일 경로는 로컬 실행 예시다. Compose에서는 검증 때와 같은 읽기 전용
수집 컨테이너·Z 제어 주소·토큰 파일 경로를 사용한다. 수집기는 A의 업무 본문 뒤에만
동작하고, 실패하면 해당 사례를 증거 불완전으로 기록한다. Canton만 실행할 때는 이 옵션을 생략한다.

대문자 배치 경로는 실제 실행에서 출력된 경로로 바꾼다. T2는 AC-08/09/10 각 10회, T3는 AC-11 5회다. T1은 모델마다 33회의 **계획된 시도**이며 실패 후 보충하지 않는다. 모델이 여러 개면 매 반복마다 실행 순서를 교대하고 한 번에 한 모델만 실행한다. 준비는 성능 측정 밖이다. T1 측정 흐름에는 확정→후속 readiness 사이에 검증용 snapshot 폴링을 끼우지 않는다.

배치에는 manifest/events/checks와 실행별 원본, `runs.csv`, `summary.csv`, `reassignment.csv`, `complexity.csv`, `report.md`, 계산 버전을 담은 `report-provenance.json`, figures의 PNG/SVG 4종을 보관한다. null은 CSV의 빈 셀로 출력한다. 준비·계측 실패/미완료/미실행과 워밍업을 남기며 정상 시간 표본에서 제외한 사유를 표시한다. 구간별 P95를 더하지 않는다. report는 원본 해시를 검사하고 원본·모델을 변경/재실행하지 않는다. 파생 파일만 다시 생성한다.

### B2 연결과 필수 증거

2026-11-23 사용자 승인: B1 코드·환경·증거를 고정하고 B2를 별도 브랜치·상태·DB·로그에서
병행 개발한다. 개발용 확인은 최종 완료와 구분한다. 개발·기능 인수의 조기 진행으로
AC-01·02·12의 필수 변형·30회 재할당 검증·실제 계약/이벤트 증빙을 줄이지 않는다.
B1의 최종 검증·측정 중에는 B2 실행 부하를 분리하거나 실행 시간을 나눈다.
B2 수정으로 B1을 고쳐야 하면 고정 기준에 미치는 영향을 확인하고 관련 검증을 다시 한다.


B2 개발용 `check`(비-final)는 A/B1 선행 배치 없이 실행할 수 있으며 최종 인계로 표시하지 않는다. B2 `check --final`에는 A/B1의 실제 최종 검증 배치를 `--prerequisite-batch`로 제공한다. T1 계측 배치는 기능 인수의 조건이 아니다. B2 비교 실험(T4)에는 A/B1 최종 검증 및 T1 계측 배치를 모두 제공해야 한다. 단일 교대 실험 배치가 A/B1의 계측을 함께 담아도 된다. B2의 실험에는 별도로 B2의 `--validation-batch`도 필요하다. B2 정상 흐름에서 재할당을 한 번 거친 뒤 Y/Z를 진행하고, T1 선택 시 T4로 구분하여 워밍업 3+본 시도 30에서 함께 관측한다. B2의 필수 AC는 01/02/12이며 나머지는 NOT_RUN을 명시한다. AC-12의 정상 재할당은 30회 검사한다.

`<evidence_dir>/<run_id>/contracts.json`에는 다음 실제 자료가 필요하다.

- run_id와 `synchronizers: {S1: 실제 ID, S2: 실제 ID}`.
- `required_contracts`: 각 contract_id, 선택 reason, 공통 business_refs, 후속 사용에 필요한 dependencies의 Contract ID 목록. 적어도 B은행의 현재 사용 권리에 연결되어야 한다. 필요하지 않은 모든 경제적 기록을 무조건 옮기도록 강제하지 않는다.
- 각 계약의 `unassigned_evidence`/`assigned_evidence`: 공통 정규화 event_id와 실제 원본 파일의 path/SHA256. 원본 파일은 해당 run 로그 폴더 안에 있어야 한다.
- `api_calls`: 실제 호출별 call_id, operation=UNASSIGN 또는 ASSIGN, contract_ids, retry_of(null 또는 원 호출 ID), evidence(event_id/path/SHA256). call_id는 실제 MESSAGE_SENT의 message_id와 연결한다. 계약 수·논리 경계 1회·API 요청/배치/재시도 수를 서로 대체하지 않는다.
- 정규화 이벤트는 계약별 UNASSIGNMENT_STARTED/COMPLETED, ASSIGNMENT_COMPLETED, SOURCE_UNUSABLE, TARGET_USABLE을 포함하고 실제 contract_id/reassignment_id와 S1/S2 별칭을 연결한다. 생성한 예시 시각을 채우지 않는다.

계약 선택 목록은 재할당 전에 보존한다. 재할당 후 원본 참조/API 호출 기록을 보완할 수 있지만 선정 계약·이유·의존성·실제 source/target을 바꾸면 실패한다. 필요한 계약 전부의 증거를 확인하기 전에는 Y readiness를 인정하지 않는다. 계약별 구간은 같은 생산자 시계에서만 계산하고, 계약들 간 시계가 다르면 전체 준비 구간은 null로 남긴다. 재할당 구간은 E2E에 중복 가산하지 않는다.

현재 사용할 Canton 버전과 실제 원본 메시지 형식을 모른다. 따라서 버전별 Ledger API 호출·원본→공통 이벤트 변환기·Party/권한/할당 연결은 **미완료**다. 위 파일 소비·검사는 구현했지만 이를 실제 Canton 지원 확인으로 해석하지 않는다.

### 검증과 Review Focus

후속 구현의 최종 회귀는 **44개 자체 시험 PASS (11.337초)**이며 `pip check`, `git diff --check`도 통과했다. 시험 stdout/stderr와 단계별 결과는 로컬 `results/self-tests/followup-verification/`에 보존한다. 이 수치는 실행기 자체 시험 결과이며 실제 A/B 인수 통과 수가 아니다. 원래 AGENTS/spec/Schema/fixture/모델 README의 내용 해시가 유지되는 것도 확인했다.

자체 시험은 모의 대상을 사용하며 실제 A/B 통과·금융 업무·연구 성능 결과가 아니다. `test_runner`는 준비, `test_execution`은 모든 X/Y/Z 변형, `test_relay`는 실제 로컬 소켓 타임아웃, `test_history`는 권리/인과/경합/증거 훼손, `test_batch`는 준비-업무 연결·고정 일정·모델 교대, `test_report`는 계산·재생성과 원본 불변, `test_reassignment`는 B2 계약·시간·호출 증거를 검사한다.

실제 연결 시 우선 확인할 것은 준비 프로그램의 DB/계약 적재, 인증 방식, 로그의 원본 정합성·flush·attempt 연결, 모델 환경·기준 커밋·Z 변경 기록, B2 실제 버전과 원본 이벤트 수집이다. 공통 spec/Schema/fixture와 모델 구현은 이번 후속 작업에서 변경하지 않았다. 최종 인계는 실제 필수 증거가 채워진 이후에만 가능하다.

## 15. B1 1단계 연결 준비 — 2026-11-19

검토 기준은 [PR #2](https://github.com/HYBLOCK-LAB/collateral-mobility-poc/pull/2)의 `69e21d9824b40d98829a53521a3523d19a576919`다. 검토 시 PR은 미병합이며, 환경·기본 연결과 어댑터 골격만 제공한다. 실제 업무·초기 적재·snapshot 연결과 공통 테스트는 **NOT_RUN**이다. 아래 설정은 실행 준비 중인 예시이며 연결 완료를 의미하지 않는다.

`config.b1.example.json`은 B1의 X `127.0.0.1:8201`, Y `127.0.0.1:8202`, Bearer 자격 환경변수 이름과 이벤트 경로를 담는다. Z `8203`은 향후 예정 주소다. 설정 형식은 X/Y/Z를 모두 요구하지만 `prepare --platforms X Y`, `check --stage xy`는 X/Y만 관측한다.

로컬 설정이 없을 때 다음과 같이 복사한다. 기존 로컬 설정은 덮어쓰지 않는다.

```sh
cp -n tests/conformance/config.b1.example.json tests/conformance/config.b1.local.json
```

- 1단계 검토 당시 `prepare_command`의 `implementations/canton/prepare.py`는 없는 예정 경로였다. PR #5 템플릿을 기준으로 작성한 현재 연결 코드와 미검증 범위는 제18절을 따른다.
- 토큰 값은 설정에 넣지 않는다. B1 구현을 사용하는 환경에서 `scripts/adapter.sh tokens`를 어댑터 기동 전에 실행하고, 실행기와 적재 프로그램을 호출할 셸에도 `scripts/env.sh`로 자격을 불러온다. 이 경로들은 `implementations/canton/` 기준이다.
- 기본 `evidence_dir`는 `implementations/canton/logs`다. 어댑터에 `CANTON_EVIDENCE_DIR`를 지정했다면 로컬 설정도 같은 실제 경로로 맞춘다. 이 환경변수 자체가 실행기 설정을 바꾸지는 않는다.
- `X-Test-ID`·`X-Attempt-ID`, producer/clock/sequence는 현재 연결 규약과 맞는다. 1단계의 COMMAND_RECEIVED/FINISHED만으로 업무 확정·권리 이력을 검증할 수는 없다.

| 다음 연결 시점 | 총괄 후속 작업·필요한 입력 |
|---|---|
| X→Y 템플릿·snapshot 준비 | 실제 템플릿 ID·필드·생성 권한·Party/Participant 연결을 받아 fixture 적재 작성. 새 run_id·기존 실행 거부·부분 실패를 검증하고 초기 snapshot 대조 |
| X→Y 업무 로그 준비 | 실제 Contract ID와 공통 업무 ID, 내부 확정·권리 이력·로컬 반영 원본을 연결하고 `check --stage xy` 실행. 이력 부족은 INCONCLUSIVE |
| X→Y 검증 후 Z 추가 전 | 실제 기준 버전·검증 배치 보존. 커밋은 사용자 승인 후 수행 |
| Z·전체 검증 준비 | Z 적재·설정과 context.json의 실제 환경·파일 해시·기준 커밋·Z 변경 근거 연결 후 필수 전체 검증 |

현재 설정 검증은 JSON 형식·필드 일치만 확인한다. Canton의 가동·토큰 유효성·업무 처리·보고된 자체 테스트 PASS의 재현을 대신하지 않는다. B2 실제 재할당 연결과 반복 실험은 제14절의 선행 검증 조건을 그대로 따른다.

## 16. A 1단계 연결 준비 — 2026-11-21

검토 기준은 [PR #3](https://github.com/HYBLOCK-LAB/collateral-mobility-poc/pull/3)의 `43c6e656ebc701c7b5641990aaf521bf92264841`이다. 검토 시 PR은 미병합이며 X·Y API와 독립 PostgreSQL 환경, DB 연결 확인만 제공한다. 공통 업무 API·인증·초기 적재·snapshot·공통 이벤트 연결과 인수 테스트는 **NOT_RUN**이다.

기존 A용 `config.example.json`을 재사용한다. 로컬 설정이 없을 때만 복사하며, X/Y 주소는 PR의 `http://127.0.0.1:8101`, `http://127.0.0.1:8102`로 맞춘다. Z `8103`은 기존 예시 값으로, 수인님과 아직 확인하지 않은 예정 주소다.

```sh
cp -n tests/conformance/config.example.json tests/conformance/config.a.local.json
```

- `prepare_command`의 `implementations/db-api/prepare.py`는 아직 없다. 인증의 `POC_*_TOKEN` 환경변수 이름과 Bearer 전달, `evidence_dir`의 `implementations/db-api/logs`도 실행기 예시이며 A 구현과 확인이 필요하다. 실제 연결 전에는 이 설정으로 prepare/check/experiment를 실행하지 않는다.
- `/health/db`의 HTTP 200은 자기 DB에 SQL을 실행하고 DB·사용자 식별을 확인했다는 뜻이다. 공통 snapshot·권한·금융 업무 검증이나 초기 적재 성공을 대신하지 않는다.
- DB 5432 포트는 호스트에 게시되지 않는다. 총괄의 적재 프로그램이 호스트에서 `localhost:5432`로 직접 연결한다고 가정하지 않는다. 업무 테이블·권한이 정해지면 기존 Compose 안에서 적재를 실행하는 방법 등 실제 접근 경로를 담당자와 확인한다. 적재 편의를 위해 DB 포트를 임의로 공개하지 않는다.
- DB 비밀번호인 `POSTGRES_X_PASSWORD`·`POSTGRES_Y_PASSWORD`와 은행·관측기 API 자격은 별개다. 값을 설정·로그·문서에 기록하지 않는다.

다음 공유 시 수인님에게 받을 정보는 X/Y 업무 테이블·초기 데이터 생성 방법·적재 권한과 실행 위치, 공통 명령·조회·snapshot, API 자격과 권한 매핑, 실제 이벤트 파일 위치·필드·호출 식별자·flush 방법이다. 이 정보가 준비되면 총괄이 fixture 적재와 증빙을 연결하고 `check --stage xy`를 수행한다. 기존 공통 규칙·기대 결과를 바꾸어 연결하지 않는다.

PR의 Windows Docker 실행 결과는 담당자 보고다. 이번 검토에서는 Compose 설정 검사, Python 구문 검사, DB 결과를 대체한 health 처리 함수의 정상·DB 불일치·사용자 불일치·SQL 오류 네 경로만 확인했다. 실제 이미지 빌드·컨테이너 기동·DB 접속·네트워크 격리는 재실행하지 않았다. X→Y 기준 버전 보존·Z 추가·전체 검증·실험은 제14절의 순서와 선행 조건을 따른다.

## 17. A X→Y 초기 적재·이벤트 연결 — PR #4

검토 기준은 [Draft PR #4](https://github.com/HYBLOCK-LAB/collateral-mobility-poc/pull/4)의 `285dfb09900aeede8b0398ff218d00fd75c04e73`이다. 제16절의 1단계 상태와 달리 이 버전에는 업무 테이블·명령·조회·snapshot·DB 이벤트가 있다. 아래 총괄 소유의 연결 프로그램은 이 스키마를 대상으로 한다. PR 검토용 코드를 임시 경로에서 사용했으며 저장소 브랜치를 바꾸거나 PR을 병합하지 않았다.

- `implementations/db-api/prepare.py`: 공통 fixture 검증 후 X에는 REPO 합의와 A 초기 권리, Y에는 LOAN 합의를 적재한다. principals·bundles는 양쪽에 넣고 버전은 0이다. 거부 fixture의 합의 조건도 그대로 보존한다. 업무 거래·후속 권리·의무는 주입하지 않는다. Z 전용 margin_calls는 X/Y에 적재하지 않는다.
- `implementations/db-api/collect_events.py`: 두 DB의 공통 이벤트 원본을 읽기 전용 트랜잭션으로 조회·검증해 하나의 JSONL로 원자 교체한다. DB를 수정하거나 이벤트를 만들지 않는다. 요청·예약·확정 관측 대기열이 남으면 최대 10초 동안 다시 확인하고, 끝내 정리되지 않으면 수집 실패다. 서로 다른 생산자 시계의 시간을 빼거나 전역 순서로 재해석하지 않는다.

접속 정보는 환경변수로만 제공한다. 각 DSN은 libpq 연결 문자열 또는 PostgreSQL URI이며 코드·설정·명령 인자에 비밀번호를 넣지 않는다.

| 환경변수 | 자격·권한 |
|---|---|
| `MODEL_A_X_SEED_DSN`, `MODEL_A_Y_SEED_DSN` | 각 DB의 `runs` SELECT 및 `runs/principals/bundles/agreements/rights` INSERT 권한. 업무 테이블 생성은 서비스가 먼저 수행 |
| `MODEL_A_X_EVIDENCE_DSN`, `MODEL_A_Y_EVIDENCE_DSN` | 별도 읽기 전용 자격. `runs/requests/transfers/commit_observations/events` SELECT 권한 |
| `POC_BANK_A_TOKEN`·`POC_BANK_B_TOKEN`·`POC_BANK_C_TOKEN`·`POC_OBSERVER_TOKEN` | API 프로세스와 실행기에 같은 자격 제공. PR #4의 Bearer 방식과 일치 |

실제 DB 이름은 `model_a_x`, `model_a_y`, 서버는 PostgreSQL 17이어야 한다. DSN은 실제 접근 가능한 경로로 설정한다. Compose의 DB 포트를 공개한다고 가정하지 않으며, Compose 안에서 연결 프로그램을 실행하려면 이 스크립트·공통 checks/Schema/fixture를 읽을 수 있는 실행 위치와 각 DB 네트워크 접근을 준비해야 한다. 공개 포트나 추가 권한을 프로그램이 자동 생성하지 않는다.

기존 실행기 가상환경에는 PR에서 사용하는 `psycopg[binary]==3.3.3`이 추가로 필요하다. `config.a.local.json`의 `prepare_command`를 `[".venv/bin/python", "implementations/db-api/prepare.py"]`로 맞추고 실제 API 주소·evidence_dir를 확인한다. 별도 가상환경을 쓰면 두 명령 모두 해당 Python 경로를 사용한다. 로컬 설정은 덮어쓰지 않고 필요한 필드만 확인한다.

```sh
.venv/bin/python tests/conformance/runner.py prepare \
  --config tests/conformance/config.a.local.json --observer-auth bearer \
  --fixture shared/fixtures/initial/f0-normal.json --platforms X Y

.venv/bin/python tests/conformance/runner.py check \
  --config tests/conformance/config.a.local.json --observer-auth bearer --stage xy \
  --collect-command .venv/bin/python implementations/db-api/collect_events.py
```

`--collect-command`는 check의 선택적인 로컬 연결 옵션이다. 기존 설정 형식·업무 API·판정 규칙은 유지한다. 업무 본문이 시작된 시나리오는 성공·실패 모두 argv에 `--run-id`, `--output <evidence_dir>/<run_id>/events.jsonl`을 붙여 수집기를 호출한다. 셸 문자열로 실행하지 않으며, 30초 한도를 적용한다. 기능 검사가 성공해도 수집·증거가 부족하면 PASS로 만들지 않는다. 원래 시나리오가 실패했으면 그 판정·원인을 유지하고 수집·이력 오류는 `diagnostic_errors`에 별도 기록한다. 준비 단계 실패는 업무 본문 미실행으로 구분하며 이 후처리 대상이 아니다.

수집 프로세스에는 `MODEL_A_X_EVIDENCE_DSN`, `MODEL_A_Y_EVIDENCE_DSN`을 전달하고, 적재 DSN·API 자격 및 TOKEN/SECRET/PASSWORD/API_KEY/DSN 이름의 다른 비밀 환경변수는 제외한다. 실행기 부모 환경은 변경하지 않는다. DSN 전체 값도 로그 마스킹 대상이며, 완료된 수집 호출의 명령·종료 코드·stdout/stderr는 비밀값을 가려 `evidence/collection.json`에 저장한다. 이 환경 필터는 별도 컨테이너·중계 프로그램 안에 이미 저장된 자격까지 제거하지 않는다. `docker exec`로 수집한다면 수집 컨테이너에는 읽기 전용 DSN만 제공하고 적재 컨테이너와 분리한다.

수집기는 미정리 요청이 남아 있어도 관측된 유효 이벤트를 진단용으로 저장하고, 정리 여부를 최대 10초 재확인한다. 한도까지 미정리이면 비영 종료하며 완료 증거로 승인하지 않는다. 실행기는 수집 오류가 있어도 이미 저장된 이력의 보관·검사를 시도하고 기존 Schema·권리 이력·경합·인과관계 검사를 유지한다. 연결 불가·잘못된 이벤트 등으로 원본 자체를 읽지 못하면 수집을 보장하지 않는다. experiment에는 이 옵션을 추가하지 않았다. Z와 최종 검증·환경 증빙이 준비되면 실험 수집 연결을 별도로 완료한다.

준비 프로그램은 쓰기 전에 양쪽 run_id 존재를 확인하고, 각 DB의 기본 키로 중복·경합을 거부한다. 두 DB의 커밋은 원자적이지 않다. X만 적재된 실패 실행은 지우거나 재사용하지 않고 새 run_id로 다시 시작한다. 실패 시 비영 종료와 FAILED JSON을 남기며, DB 오류 원문·DSN을 출력하지 않는다.

실제 연결에서 확인한 실행기 수정은 두 가지다. 초기 준비에서 검증한 S0를 이전 상태로 유지하여, Repo 이후에도 아직 갱신되지 않은 Y의 S0 로컬 기록을 허용한다. 또한 수락된 업무 결과가 없는 AC-02 readiness 조회와 AC-03 사전 거부에 한해 실제 수집된 빈 로그 파일을 허용한다. 파일 누락·수집 실패·업무 결과가 있는 실행의 빈 이력은 계속 INCONCLUSIVE다. 새 이벤트나 성공 결과를 대신 생성하지 않는다.

### 실제 연결 검증 결과

임시 Docker PostgreSQL **17.12** 두 개와 PR #4의 Python API 두 개로 검증했다. 기존 DB·서비스는 사용하지 않았다. 표준 Compose 전체를 기동한 시험은 아니며 임시 DB 포트는 루프백에만 열었다. 이벤트 수집에는 별도 SELECT 자격을 사용했다.

- X→Y 공통 실행 `--stage xy`: **50 PASS, 0 FAIL, 0 INCONCLUSIVE**, 종료 코드 0. 경합 두 종류 각 10회와 응답 지연 복구 5회를 포함한다. 배치는 `results/model-a-pr4-connection-20261121/batches/batch-7417a92190294bd1b6035c3376db37e3/`다. `final_handoff_eligible=false`이며 전체 AC·Z·성능 검증은 아니다.
- 초기 적재: 동일 run_id 재사용 거부, X 커밋 후 Y 권한 오류 시 FAILED와 X 데이터 보존, 부분 실패 run_id 재사용 거부를 실제 DB에서 확인했다. 준비만 한 실행의 이벤트는 0건으로 내보내며 가짜 이벤트를 만들지 않았다.
- 실행기 자체 시험 **49 PASS**와 `git diff --check` 통과. `results/model-a-pr4-connection-20261121/`에 실행 결과·소스 해시·소스 복사본·검증 드라이버·자체 시험 출력을 보존했다. 수정 전 실패 배치 두 개도 남겼다.
- 검증 후 API와 임시 DB 컨테이너를 종료·정리했다. 담당자의 Windows API/DB 재시작 자체 시험 58건 전체와 Compose 업무 흐름은 독립 재실행하지 않았다. 새 연결 프로그램은 미커밋이며, 팀 공유 및 실제 X→Y 기준 커밋 보존에는 사용자 승인이 필요하다.

## 18. B1 X→Y 초기 적재 연결 — PR #5

검토 기준은 [Draft PR #5](https://github.com/HYBLOCK-LAB/collateral-mobility-poc/pull/5)의 `a87cd3ec56276d400c02b8ebde0d07bd541360b5`, `collateral` 0.2.0이다. 이전 토큰 생성 순서와 확인용 DAR 빌드 안내는 반영됐다. 정상 X→Y 계약·명령·결과 조회·snapshot·readiness·이벤트가 제공되지만, 거절·중복·경합·복구는 여전히 3단계 대상이다. 이 절의 연결 코드 시험은 **모의 Ledger API 시험**이며 실제 B1 공통 검증은 **NOT_RUN**이다.

총괄의 `implementations/canton/prepare.py`는 공통 fixture에서 `UseRight`의 A 초기 권리와 Repo 합의를 P_X에, Loan 합의를 P_Y에 만든다. 각 Participant당 한 번 제출하며 actAs는 각각 A/B, B/C다. 공통 fixture를 변경하지 않고 `reuse_allowed`, `ccp_use_allowed`, 담보 조건도 그대로 옮긴다. 거래 choice나 후속 권리·의무를 직접 생성하지 않는다. `principals`는 기존 API 토큰 매핑으로 다루고 Z 전용 데이터는 만들지 않는다.

사용하려면 PR #5의 서비스를 기동하고 DAR를 올린 뒤, 그 환경의 `scripts/env.sh`를 source해 `CANTON_STATE_DIR`와 `POC_*_TOKEN`을 실행기에도 제공한다. 준비 프로그램은 `CANTON_STATE_DIR/ids.json`의 실제 Party ID와 P_X/P_Y JSON Ledger API 주소를 사용하며 localhost HTTP 주소만 허용한다. 초기 적재는 고권한 작업이므로 이 JSON Ledger API를 외부에 공개하지 않는다. 기본 설정과 로그 경로는 제15절을 재사용하고 `CANTON_EVIDENCE_DIR` 변경 시 실행기의 evidence_dir도 함께 맞춘다.

```sh
.venv/bin/python tests/conformance/runner.py prepare \
  --config tests/conformance/config.b1.local.json --observer-auth bearer \
  --fixture shared/fixtures/initial/f0-normal.json --platforms X Y

.venv/bin/python tests/conformance/runner.py check \
  --config tests/conformance/config.b1.local.json --observer-auth bearer \
  --stage xy --case AC-01 AC-02
```

위 명령은 실제 Canton 연결 후 실행한다. B1은 어댑터가 이미 `<evidence_dir>/<run_id>/events.jsonl`을 쓰므로 A용 `--collect-command`를 붙이지 않는다. 아직 없는 거절·멱등성·경합·복구를 정상 통과로 대체하지 않고, 해당 단계 구현 뒤 전체 X→Y 공통 목록을 수행한다.

준비 전 양쪽 ACS에서 기존 runId를 확인하고, 제출 전 `CANTON_STATE_DIR/conformance-preparations/<run_id>.json`을 배타적으로 생성·동기화한다. 이 로컬 표식은 성공·부분 실패·타임아웃 뒤에도 보존한다. P_X만 성공했거나 P_Y 응답을 잃으면 FAILED로 기록하고 같은 run_id를 재사용하지 않는다. 로컬 표식은 이 준비 프로그램의 중복 제출을 막는 수단이며, 여러 머신·수동 원장 제출까지 전역 유일성을 보장하지 않는다. 원장 계약 키나 업무 권한·멱등성의 대체물이 아니다.

확인한 것은 5개 자체 시험이다: 실제 스키마의 fixture 조건 매핑, Participant/actAs별 S0 생성 요청, 기존 run 거부, 부분 성공 뒤 응답 미확정 시 표식 보존·재사용 거부, 외부 Ledger URL 거부. 실제 Daml 컴파일·Canton 제출·공통 정상 흐름은 이 환경에 툴체인·원장 상태가 없어 재실행하지 않았다.

3단계에서 우선 대조할 항목은 원장 권한과 유효 권리 생성 경로, 업무 거절 코드와 동일 요청 멱등성·충돌, 실제 진행 상태·결과 미확정·복구, 재기동 후 snapshot 순번과 로그 증빙, 어댑터↔Participant 메시지 기록이다. 현재 UseRight의 보유자 단독 생성·Consume/Archive 경로와 snapshot의 소진 권리 기반 버전 계산은 거래 없이 권리·버전을 바꿀 수 있는 제약이다. 정상 경로 자체 시험으로 이 제약이 해소됐다고 판단하지 않는다. 계약 권한을 바꾸는 설계가 필요하면 담당자가 최소안과 업무·적재 영향을 먼저 공유하고 승인 후 반영한다.

## 19. A Compose 통합 재검증 — 2026-11-22

수인님 환경의 Docker I/O·읽기 전용 저장소 오류와 별도로, 총괄의 정상 Docker 환경에서 PR #4 `285dfb09900aeede8b0398ff218d00fd75c04e73`와 PR #6 `339d016803c8b1d9dd5008f2f88f586700ba5d49`를 임시 폴더에 조합해 검증했다. 브랜치 병합이나 기존 DB 사용 없이 원래 Compose 파일과 Dockerfile로 새 프로젝트·새 볼륨을 기동했다.

- Docker Engine 29.8.2, Linux 컨테이너 Python 3.12.16, X·Y PostgreSQL 17.12. Compose 빌드·기동 성공 및 API/DB 네 개 모두 healthy.
- DB 포트는 호스트에 공개하지 않았다. 별도 검증용 연결 컨테이너만 두 DB 네트워크에 참여하고, 적재에는 S0 테이블 INSERT·runs SELECT 자격, 수집에는 필요한 테이블 SELECT 자격을 사용했다. API의 원래 네트워크는 변경하지 않았다.
- 호스트 실행기에서 연결 컨테이너의 준비·수집 프로그램을 호출해 초기 snapshot 확인과 **X→Y 공통 50건 PASS**, 종료 코드 0을 확인했다. 경합 각 10회·응답 지연 복구 5회를 포함하며 `final_handoff_eligible=false`다.
- 결과는 `results/model-a-compose-pr4-pr6-20261122-final/verification.json`, 공통 배치는 그 아래 `batches/batch-580211b706b74306874ffd2202252a1f/`에 있다. 소스 복사본·SHA-256·검증 드라이버·Compose 로그도 함께 보존했다. 초기 연결 시도 두 번의 경로 매핑 실패 기록은 별도 결과 폴더에 보존했다. 모델 코드·fixture·판정 조건은 바꾸지 않았다.
- 검증 후 이번 임시 프로젝트의 컨테이너·네트워크·볼륨만 정리했다. 수인님 원본 데이터와 현재 작업 브랜치의 구현 코드는 변경하지 않았다. Z·전체 최종 인수·성능 실험은 이 검증 범위 밖이다.

수인님 PC의 Docker 저장소 복구는 별도 환경 문제로 남긴다. 복구를 위해 기존 데이터를 초기화하도록 요구하지 않고, 위 정상 환경의 재현 결과를 X→Y 기준 버전 검토 근거로 사용한다. 코드·명세·fixture·증거를 맞춰 기준 커밋을 확정하는 절차와 PR 병합은 사용자 승인 후 진행한다.

## 20. B1 공동 서명자 계약의 초기 적재 연결 — 2026-11-22

PR #5 `1d369e1c6931d255c1baa4715566cec43074d56d`의 `collateral` 0.3.0을 기준으로 초기 적재를 갱신했다. 제18절은 0.2.0 당시의 기록이다. 승인된 계약은 `UseRight`의 `cosigner`와 공동 서명, `Consume`의 다음 보유자 권한, 대차 입력 권리의 공동 서명자가 차입자가 아닌지 확인하는 검사를 포함한다. 두 서명자가 함께 원장 API를 직접 쓰는 경로는 승인된 신뢰 범위의 한계로 남는다.

- `implementations/canton/prepare.py`는 right:0의 `cosigner`에 BANK_B의 실제 Party ID를 넣는다. 기존 Repo 매수자 BANK_B·초기 보유자 BANK_A 호스팅 검증과 actAs A/B·B/C, fixture 조건, 중복 run 거부·실패 표식 보존은 유지한다. 패키지 이름도 `collateral`이므로 템플릿 참조·ACS 필터는 유지한다.
- 0.2.0 원장에 이 적재기를 사용하지 않는다. 담당자가 0.3.0을 검증한 새 원장의 상태 폴더·현재 `ids.json`과 자격을 사용하거나, 원본을 보존한 별도 새 원장을 마련한다. 현재 Party ID를 이전 원장에서 복사하지 않는다. 0.2.0→0.3.0은 담당자 upgrade-check에서 비호환으로 보고됐다.
- 권리 가시성은 Y의 S0 right:0, X의 S2 right:2까지 늘어난다. 이 복제 관측은 새 업무 권리로 중복 집계하지 않는다. 실행기의 기존 S0 이력 초기화는 유지하며 공통 기대 상태나 판정을 완화하지 않는다.
- 모의 Ledger API 연결 시험은 0.3.0 필수 `cosigner` 누락을 검출하도록 보강했다. 공통 초기 fixture 전체에서 매수자 Party 매핑·보유자와의 구분·원본 조건 보존을 확인한다. 실제 Canton 제출 시험과 구분한다.
- 다음 실제 연결 검증은 제18절의 `prepare` → `check --stage xy --case AC-01 AC-02` → `check --stage xy` 순서다. B1은 어댑터가 이벤트 파일을 쓰므로 별도 A 수집 옵션은 붙이지 않는다. 실행기 `evidence_dir`와 실제 이벤트 경로를 일치시킨다.
- 이 로컬에는 Canton/dpm·JDK 21·PostgreSQL 17의 확인된 실행 환경과 원장 상태가 없어 실제 공통 X→Y 검증은 **NOT_RUN**이다. 담당자 자체 시험 21/33/12/42/29 통과 보고를 공통 검증 PASS로 대체하지 않는다. 원본 증거도 담당자 환경에 있으며 아직 전달받지 않았다.

`CANTON_RESPONSE_WAIT=4.0`은 개발용 값이다. 공통 실험 조건 확정, 어댑터↔Participant 메시지 계측, 실제 X→Y 기준 버전·증거 보존, Z·B2는 별도 후속 작업이다. 이 연결 코드 수정이나 문서 작성은 PR 병합·Z 착수 승인이 아니다.

## 21. A 연결 리뷰 보완과 재검증 — 2026-11-22

수인님이 제기한 두 사항을 실제 코드와 대조해 수용했다. 기존 수집 호출은 부모 환경의 적재 DSN을 상속할 수 있었고, 시나리오 오류가 먼저 발생하면 수집을 건너뛰었다. 실제 자격 유출이나 업무 결함이 발생했다고 판단한 것은 아니다.

- 실행기: 수집 자식 환경에서 적재 DSN·업무 자격을 제외하고 읽기 전용 DSN을 유지한다. DSN 전체 값도 로그 마스킹에 포함한다. 업무 본문 실패 후에도 수집·원본 이력 보관을 시도하고 최초 기능 판정·원인을 유지한다. 추가 수집 오류는 별도 진단 항목이다.
- A 수집기: 미정리 작업이 있으면 관측 이벤트를 진단용으로 보존하고 재확인한다. 계속 미정리이면 비영 종료하므로 이를 완료 증거로 사용하지 않는다. 정상 fixture·업무 처리·공통 판정 기준은 바꾸지 않았다.
- 자체 시험 **57건 PASS**. 실제 자식 프로세스의 환경 제외·DSN 마스킹, 시나리오 오류 3종 뒤 수집 시도와 원인 보존, 미정리 상태의 진단 파일 보존·비성공 종료를 포함한다. 미정리 DB 응답은 모의 연결 시험이며 실제 DB 장애 재현으로 보고하지 않는다.
- PR #4 `285dfb09900aeede8b0398ff218d00fd75c04e73` 구현과 수정된 실행기·수집기를 별도 Compose 프로젝트에서 조합했다. 적재·수집 컨테이너를 분리하고 수집 컨테이너에 읽기 전용 DSN만 있음을 확인했다. 준비 확인 및 공통 X→Y **50건 PASS / 0 FAIL / 0 INCONCLUSIVE**, 종료 코드 0, `final_handoff_eligible=false`였다.
- 증거: `results/model-a-collector-review-20261122T151008/verification.json`, 소스 복사본·`source-sha256.json`·검증 드라이버·기동/종료 로그, 하위 `batches/batch-f28f5267a3964d029699a8c8d76982f0/`. 이번 프로젝트 자원만 정리했고 원래 데이터는 사용·변경하지 않았다.

이 결과는 A의 X→Y 회귀 검증이다. B1 실제 연결·Z·전체 최종 인수·성능 실험을 대신하지 않는다. 현재 코드·문서와 맞는 기준 버전 보존 및 공유는 [Model A X→Y 기준 기록](baselines/model-a-xy-20261122.md)의 절차로 진행한다.

## 22. B1 최초 공통 검증과 읽기 전용 실행의 로그 초기화 — 2026-11-22

양하님이 PR #5 `1d369e1`과 연결본 `8452122`를 실제 Canton 3.7.3 새 원장에서 실행했다. 준비 전용 PASS, AC-01 normal PASS, AC-02 ready-Y-1 PASS였으나 ready-Y-0은 기능 검사가 PASS인 상태에서 이벤트 파일이 없어 INCONCLUSIVE였다. 안내대로 중단해 X→Y 전체 50건은 NOT_RUN이다. 이를 B1 기준 보존이나 Z 착수 승인으로 처리하지 않는다.

- 원본 ZIP SHA-256 `88a47f7462c68308a45ec200032636a766bc9380b2525689473479b99b4ab5cd`와 Manifest 84개 파일 해시가 일치했다. 실행별 증거 해시·snapshot Schema·확정 거래 2개 실행의 이벤트 Schema/이력을 재검사했다. 기록은 `results/b1-common-xy-evidence-20261122/audit.json`이다.
- 어댑터는 최초 업무 이벤트를 기록할 때만 파일을 만들고 readiness는 업무 상태를 바꾸지 않는다. 거래 없는 ready-Y-0에서는 초기 적재도 이벤트 파일을 만들지 않아 파일이 없는 상태가 됐다. 실행기의 누락 파일 거부는 유지한다.
- 총괄 적재기는 두 Participant의 준비 트랜잭션 성공 후 해당 새 run의 로그를 append 방식으로 초기화한다. 이벤트·시각·거래를 만들어 넣지 않고 기존 파일을 자르지 않는다. 부분 적재 실패·미확정이면 로그 초기화까지 완료한 것으로 보고하지 않는다. 준비 프로그램과 어댑터, 실행기의 이벤트 경로는 같아야 한다.
- 기존 규칙대로 실제 빈 이력이 허용되는 것은 수락된 업무 결과 없는 AC-02/03뿐이다. 확정 거래의 빈 이력·파일 누락·잘못된 이벤트는 계속 실패 또는 INCONCLUSIVE다. 원래 실패 배치를 수동으로 고치지 않고 새 실행으로 재검증한다.
- [실행 안내](B1_XY_RUNBOOK.md)를 후속 연결본과 기존 0.3.0 검증 환경 재사용 절차에 맞췄다. PR #6은 이미 병합됐으므로 수정은 별도 PR로 공유한다. 실제 Canton에서 수정본을 제출·재검증하는 작업은 아직 NOT_RUN이다.

## 23. B1 공통 X→Y 재검증과 기준 검토 — 2026-11-22

제22절의 수정본 `5695aa0`을 양하님이 PR #5 `1d369e1`의 실제 Canton 0.3.0 환경에서 재검증했다. 초기 준비 PASS, AC-01·02 3 PASS, X→Y 전체 **50 PASS / 0 FAIL / 0 INCONCLUSIVE**다. 앞서 실패한 배치와 원본은 별도 보존했고 새 run_id를 사용했다.

총괄이 공유 ZIP과 Manifest 993개 파일의 해시, 연결 파일 네 개의 커밋별 해시, 공통 50건의 필수 목록·반복, 원본 이력·snapshot·명령 결과·증거 해시를 대조했다. 오류는 발견되지 않았다. 대조 보고서는 `results/b1-common-xy-rerun-audit-20261122/audit.json`이다. 이는 원본 재검사이며 총괄 로컬에서 Canton을 직접 다시 실행한 결과는 아니다.

ready-Y-0의 실제 빈 이벤트 파일이 생성됐고, 확정 거래의 이벤트는 존재한다. 상위 `acceptance=INCONCLUSIVE`와 `final_handoff_eligible=false`는 X→Y 부분 검증을 XYZ 전체 인수와 구분한 표시다. 전체 완료로 바꾸지 않는다.

[B1 X→Y 기준 검토 기록](baselines/model-b1-xy-20261122.md)에 코드·환경·증거·병합 후 확인·Z/B2 순서를 정리했다. 현재 PR #5·#7은 병합 전이며 기준 확정·Z 착수는 아직 승인하지 않았다. 추가 실행기 로직 수정은 필요하지 않은 것으로 판단했다.

Model A XYZ의 실제 초기 적재, readiness 저장 완료 확인과 최종 수집 명령은
[Model A XYZ 실행 안내](MODEL_A_XYZ_RUNBOOK.md)를 따른다.
