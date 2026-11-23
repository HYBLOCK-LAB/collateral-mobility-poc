# B1 XYZ 개발 중 증거 검토 — 2026-11-23

[PR #9 결과 공유](https://github.com/HYBLOCK-LAB/collateral-mobility-poc/pull/9#issuecomment-7091583988)를
총괄이 원본 ZIP과 현재 공통 검사기로 대조했다. 최종 기준 버전 보존·인계·병합 승인은 아니다.

## 소스와 실제 결과

- 공통 실행 구현: `adc9837b1fc78f2e468ddafece766ebfc3630f5b`.
- 초기 적재·연결: PR #11 `ad0e6af58d96120f604e6466bc454aaaad4d9244`의 6개 파일.
  ZIP의 checkout 기록에 있는 6개 SHA-256을 실제 Git 객체와 대조했다.
- 최신 구현 `fe875e2db4afedfaf97200fc925fca25321e47a3`는 README와
  `selftest/xyz_stage3_selftest.py`만 추가·변경했다. 어댑터·Daml·환경 기동 코드는 공통 실행 때와 같다.
- 실제 공통 준비 PASS, AC-01/02 **4 PASS**, 전체 XYZ **71 PASS**를 원본에서 확인했다.
  전체 배치는 `batch-c382281356d1462abcae551c0b9d9425`다.
- 75개 독립 공통 검사 run, 초기 snapshot 228개, 실제 이벤트 파일 76개와
  이벤트 880개를 대조했다. 필수 변형·반복·거래/권리 이력·파일 해시·원본 로그 연결이
  최신 검사기(`96f37e0`)의 검사도 통과했다. 감사는 업무를 재실행하지 않았다.
- 예비 prepare의 PASS는 거래 테스트 PASS와 구분한다. 필터 4건과 전체 71건은 별도 run이며
  독립 검증 범위의 수를 합쳐 새로운 인수 조건으로 사용하지 않는다.

## 원본 무결성

| 원본 | ZIP SHA-256 | Manifest 항목 |
|---|---|---:|
| `b1-common-xyz-20261123.zip` | `d1b2fd13b79113cececd21b8b5821f6409c68c934efda04f4f1ce5f413ee9c83` | 1,766 |
| `b1-z-selftest-20261123.zip` | `1bcc5d7f606d29a08ac7eeb69bd3055ba5d62404273c818d6457632d08605d7c` | 106 |

두 ZIP과 Manifest 해시, 기록된 모든 파일 해시가 일치했다. 공통 배치의 spec·shared·실행기
소스 해시도 선언된 구현+연결 조합의 Git 파일 62개와 대조했다. 초기 snapshot의 S0와
개별 구현 원본 JSONL이 실행기에 보존된 본문·해시와 같은지도 확인했다.
로컬 원본·감사 스크립트·결과는 `results/b1-common-xyz-audit-20261123/`에 보존했다.
원본 파일이나 담당자의 판정은 수정하지 않았다.

## 자체 시험과 확인 범위

- 25건 자체 배치 두 개의 보고서와 새 clone의 25건 보고서가 모두 PASS이며,
  재기동을 선택한 두 배치의 추가 어댑터 재기동 사례도 PASS로 기록됐다.
- 별도 XY 12건·XYZ 정상 18건·XY 추가 33건 회귀 보고서와 예비 7건 보고서도 확인했다.
- 자체 시험 JSONL의 이벤트 1,364개는 Schema 검사를 통과했다.
  자체 시험의 모든 업무 판정을 총괄이 재실행한 것으로 표현하지 않는다.
- 재기동은 확정·거절 결과의 어댑터 재기동 후 조회·재전송 범위다. 미확정 요청,
  Canton·PostgreSQL 재기동 및 Z 자체 응답 지연까지 통과한 것으로 확대하지 않는다.
- Z의 버전 충돌·내용 충돌 등 추가 시험은 미실행으로 보존한다. 현재 필수 공통 AC-05의
  오래된 버전과 AC-07 내용 충돌은 Y 사례이며, 이를 임의로 Z 전체 사례로 확대하지 않는다.

## 남은 단계

1. 계측 명세 §8.3에 필요한 어댑터↔Participant의 실제 업무 호출·응답 기록을 보완한다.
   이번 원본에는 MESSAGE_SENT/RECEIVED가 없어 업무 메시지 수는 미계측이다.
   과거 이벤트를 추정해 채우거나 0건으로 보고하지 않는다.
2. 보완된 실제 구현·B1 연결·공통 실행기를 고정하고 환경·자원·내구성·XY 기준·Z 변경
   근거를 `context.json` 및 원본으로 연결한다. 공통 실행기 리뷰 보완은 PR #10
   `96f37e0`이며 기존 공통 배치에 사용된 실행기와 구분한다.
3. 해당 최종 조합에서 `check --final`을 실행하고 원본을 대조한다. 기존 71 PASS를
   `--final` 실행으로 소급하거나 `final_handoff_eligible`을 직접 바꾸지 않는다.
4. 사용자 검토·병합 승인과 main 확인 후 A·B1의 계측 준비를 함께 확인한다.
   별도 반복 실험 세션의 시작은 그때 안내한다. B2는 A·B1 검증·계측 확보 후 착수한다.

## Participant 호출 기록 보완 공유 후 검토

[후속 공유](https://github.com/HYBLOCK-LAB/collateral-mobility-poc/pull/9#issuecomment-7094025479)의
`b182550cfe8789b2f0c5bd8d85d38dacb444f1b2`는 명령에 귀속된 실제 거래 제출·상태 조회·폴링·
재확인 및 로컬 반영 확인의 MESSAGE_SENT/RECEIVED를 추가했다. 양하님은 새 환경에서
준비·필터 4건·전체 XYZ 71건과 호출 기록 검사 통과를 보고했다. 새 배치 원본은 해당
코멘트에 아직 첨부되지 않았으므로, 위 원본 감사 결과와 별개인 **보고된 결과**로 둔다.

코드에서 `get_command`, readiness 처리, `get_snapshot`이 `prepared_run(run_id)`를
Trail 없이 호출함을 확인했다. 이 호출은 내부 `active_contracts`에서 Participant를
조회하지만 메시지 기록은 남기지 않는다. 외부 실행기→어댑터 HTTP 기록과
어댑터→Participant 호출은 별개이며, 외부 HARNESS 기록으로 내부 호출 원본을 대신하지 않는다.
기존 event Schema 안에서 GET 유래 조회 목적을 구분해 기록을 보완하고, 최종 계수에서
업무 호출과 관측용 호출을 분리할 필요가 있다. 기동·헬스체크·Canton 합의 메시지까지
새로 계측하라는 요청으로 확대하지 않는다.

이미 기록된 `local-check`의 OPERATIONAL 분류는 구현자의 제안으로 보존한다.
관련 원자료가 남아 있으므로 현재 분류만을 이유로 재구현을 요구하지 않는다.
최종 집계의 포함 범위는 A·B의 실제 호출 경계와 명세 §8.3을 함께 대조해 총괄이 확정한다.

다음 자료는 새 배치 3개·실제 JSONL·호출 검사 출력·Manifest의 공유가 필요하다.
추가 기록은 작은 정상 흐름으로 먼저 확인하고, 최종 전체 검증은 소스·환경 증빙을 고정한
`--final`로 묶어 진행한다. `b182550` 결과를 미리 최종 인계로 바꾸지 않는다.

## GET 조회 기록 보완과 원본 추가 감사

[추가 공유](https://github.com/HYBLOCK-LAB/collateral-mobility-poc/pull/9#issuecomment-7095047740)의
`3f56a7bc0e58cc2507bd0f2cb17428d9b2551bba`는 결과 조회·readiness·snapshot에
답하는 Participant 조회를 기록한다. GET을 명령 이벤트로 만들지 않으며, HARNESS 분류는
구현 제안으로 보존한다. 실행에 사용했다고 보고한 server.py SHA-256은 해당 Git 파일과 일치한다.
준비 PASS·AC-01/02 4 PASS 및 자체 시험 45·18 PASS는 보고된 결과다. 최신 작은 배치의
원본은 이번 ZIP에 포함되지 않았으므로 독립 원본 감사는 아직 하지 않았다.

첨부 ZIP은 이전 `b182550`의 준비·4건·71건 원본이다. ZIP SHA-256
`ce84429488ac5cdd89aeeab4d9f977a28f0c68d540e84a9281c24df17616e4fc` 및
Manifest 1,754개 파일이 모두 일치했다. 소스 62개 파일, 초기 snapshot 228개,
구현 이벤트 2,098개와 원본 JSONL/수집 본문을 대조했다. 기존 75개 사례는 최신 검사기
`96f37e0`로 재검사했으며, 전체 71건 원본의 업무 요청·응답 각 428개와 OPERATIONAL
각 163개가 보고와 일치했다. 업무 재실행·반복 실험은 수행하지 않았다.
감사 결과는 `results/b1-get-message-review-20261123/audit.json`에 보존했다.

총괄 실행기는 초기 snapshot에 PREPARE·고유 attempt_id를 보내고 이를 기존
`evidence/snapshot-<P>.json`에 method/platform/suffix와 함께 보존하도록 보완했다.
보고서는 외부 HARNESS/REQUEST·RESPONSE와 내부 HARNESS/IMPLEMENTATION/REQUEST·RESPONSE를
구분한다. 공통 Schema·업무 판정·메시지 분류 정책은 바꾸지 않았다.

연결 시 주의: 현재 구현의 message_log_check.py는 `call-*.json`만 읽는다. 새 PREPARE
조회도 호출과 대조하려면 `snapshot-<P>.json`을 함께 읽어야 한다. 해당 수정은 구현 담당
자체 검사기 범위이며, 새 연결 소스 공유와 함께 안내한다. 기존 원본은 수정하지 않는다.
최신 작은 배치 원본을 받으면 이를 먼저 확인하고, 소스·환경·XY 기준·Z 변경 증빙을
고정한 뒤 전체 `--final`로 진행한다. 전체 71건의 중간 재실행은 추가 요구하지 않는다.
