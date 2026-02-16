# Institutional Upgrade Roadmap (Recommended)

브랜치: `institutional`

목표: 개인 운용(최대 1,000만원) 환경에서 **기관 투자자 수준의 엔진 품질(재현성/체결·회계/리스크/운영)**을 단계적으로 달성합니다.

> 중요: 무료 데이터 + 브로커 API 제약으로 “전 종목 실시간 초단타”는 현실적으로 불가능합니다.
> 따라서 **전 종목은 후보 생성/랭킹(배치·저빈도)**, **실시간 정밀 추적은 상위 N**만 수행하는 구조가 추천값입니다.

---

## 고정 전제
- 계좌: KIS 1개
- 데이터: 무료(US 분석은 yfinance 가능), 주문/체결은 KIS
- 전략 모듈: 초단타(scalp) + 데이(day, ≤1일) + 스윙(swing, ≤5거래일)
- 대상: 주식/ETF 전 종목(후보 생성 기준)

---

## 추천 기본값(정책)
### 자금 1,000만원 기준 리스크(초기 보수)
- 일 손실 제한(자동 중지): **-1.2% (약 -12만원)**
- 종목당 최대 노출: **12% (약 120만원)**
- 총 노출(gross) 최대: **35% (약 350만원)**
- 1트레이드 리스크 예산:
  - scalp: **0.25% (2.5만원)**
  - day: **0.35% (3.5만원)**
  - swing: **0.50% (5만원)**

### 유니버스(무료/쿼터 현실)
- scalp 실시간 후보: **10~25**
- day 실시간 후보: **20~40**
- swing 후보(일봉/저빈도): **50~200**

### 이벤트 로그(추천)
- **Tick 로그 기본 OFF**: `KIS_EVENT_LOG_TICKS=0`
- Bar/Signal/Order/Risk/Fills/PositionSnapshot은 ON

### Feature Flags (추천 기본 OFF)
- `KIS_INSTITUTIONAL_OMS=0`
- `KIS_INSTITUTIONAL_LEDGER=0`
- `KIS_INSTITUTIONAL_RECONCILE=0`

---

## 현황(완료)
### Phase 1~2 착수/기반 완료
- 이벤트 스키마 확장(표준 이벤트 타입 helper)
- 이벤트 저장소: UTC 일자 파티션 + 최근 이벤트 카운트(헬스 요약)
- 리플레이 CLI 스켈레톤(`python -m tools.replay ...`)
- health_status.json에 이벤트/유니버스 요약 포함
- OMS/ledger/reconcile 스켈레톤 추가(기본 OFF)

---

## Phase 3 — 실제 로깅/OMS 연결(추천)
**목표:** Signal→RiskDecision→OrderIntent→OrderSubmitted/Ack→Fill이 이벤트로 연결되어 추적 가능.

### 작업 티켓
- P3-1: 엔진에서 Signal/RiskDecision/OrderIntent/OrderSubmitted/Ack/Fill/PositionSnapshot 이벤트를 실제 append
- P3-2: 주문 idempotency_key 규칙 확정(재시도 시 동일 키 유지)
- P3-3: `KIS_INSTITUTIONAL_OMS=1`일 때만 OMS를 주문 제출 경로에 연결

### 완료 조건
- 특정 주문 1건을 이벤트 흐름으로 end-to-end 추적 가능
- OMS ON/OFF 모두 ruff/pytest 통과 + 기존 실행 경로 비파괴

---

## Phase 4 — Ledger/리플레이 고도화 + 3모듈 단일 엔진화(추천)
**목표:** Fill 기반 회계(ledger)로 포지션/현금을 단일 진실로 만들고, scalp/day/swing가 동일 엔진 위에서 동작.

### 작업 티켓
- P4-1: Fill 이벤트 기반 ledger 적용 + PositionSnapshot 이벤트 발행 (`KIS_INSTITUTIONAL_LEDGER=1`에서만)
- P4-2: replay를 deterministic하게 고도화(최소 Bar→Signal→RiskDecision 재현)
- P4-3: 이벤트 스키마 호환성 정책 문서화(필드 추가만 허용)
- P4-4: 전략 모듈 인터페이스 표준화(`strategies/*`로 분리, 엔진은 공통)

### 완료 조건
- Ledger ON 시 포지션/현금이 Fill 기반으로 일관
- scalp/day/swing 모듈이 동일 OMS/Risk/Ledger/Event 위에서 운용 가능

---

## Phase 5 — 정합성(Reconcile) + 운영/관측/리포트(추천)
**목표:** 재시작/장애/불일치 상황에서 자동 정합성 확인 + 위험 시 자동 중지 + 리포트.

### 작업 티켓
- P5-1: 브로커 어댑터 표준화 후 reconcile 주기 실행(기본 OFF)
- P5-2: 불일치 정책(추천: 신규 진입 차단 + 경고/리포트, 심각하면 kill-switch)
- P5-3: 일일 리포트 자동 생성(모듈별 성과/체결/오류/리스크 차단)
- P5-4: health_status 운영 메트릭 확장(이벤트 타입 분포/오류 카운트/플래그 상태)

### 완료 조건
- 불일치/장애 시 자동 차단/복구/설명 가능
- 리포트만으로 “왜 이런 결과가 났는지” 판단 가능
