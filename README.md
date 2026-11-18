# collateral-mobility-poc
기관 간 국채 담보의 연속 활용을 시나리오로, 기존 DB/API 인프라와 Canton Network의 거래 처리·상태 일관성·플랫폼 연계 비용을 비교하는 PoC 연구

## 범위

- **필수:** A (DB/API), B1 (Canton, 1 Synchronizer)
- **확장:** B2 (Canton, 2 Synchronizers 간 재할당)
- A와 B1은 동일한 업무 요구사항을 대상으로 공통 테스트와 계측을 적용합니다.
- 공통 명세는 연구 총괄의 승인을 거쳐 확정합니다.
- 일정: 1주일 마감

## 구조

- `spec/`: 공통 업무·인터페이스·인수·계측 명세 (작성 예정)
- `shared/schemas/`, `shared/fixtures/`: 공통 스키마와 테스트 데이터 (작성 예정)
- `implementations/db-api/`, `implementations/canton/`: 구현별 작업 공간
- `tests/conformance/`: 공통 적합성 테스트 (작성 예정)
