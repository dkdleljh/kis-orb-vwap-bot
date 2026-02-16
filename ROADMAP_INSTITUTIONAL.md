# Institutional Upgrade Roadmap (Recommended)

브랜치: `institutional`

목표: 개인 운용(최대 1,000만원) 환경에서 **기관 투자자 수준의 엔진 품질(재현성/체결·회계/리스크/운영)**을 단계적으로 달성합니다.

> 중요: 무료 데이터 + 브로커 API 제약으로 "전 종목 실시간 초단타"는 현실적으로 불가능합니다.
> 따라서 **전 종목은 후보 생성/랭킹(배치·저빈도)**, **실시간 정적 추적은 상위 N만 수행**하는 구조가 추천값입니다.

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

### 현재 상태 (코드 기준)
- ✅ 이벤트 타입 정의 완료 (`core/events.py`: Signal, RiskDecision, OrderIntent, OrderSubmitted, OrderAck, Fill, PositionSnapshot)
- ✅ EventStore 구현 완료 (`core/event_store.py`)
- ⚠️ main.py에서 일부 이벤트 로깅 (entry만, exit 미포함)
- ⚠️ OMS 스켈레톤 존재 (`core/oms.py`) but 미연결
- ⚠️ OrderAck 미기록, PositionSnapshot 미발행

### 작업 티켓

#### P3-1: 엔진에서 Signal/RiskDecision/OrderIntent/OrderSubmitted/Ack/Fill 이벤트를 실제 append
**수정 파일:** `main.py`, `core/events.py`  
**완료 조건:** 특정 주문 1건의 entire lifecycle (entry+exit)이 이벤트 로그로 end-to-end 추적 가능

- P3-1a: entry뿐 아니라 exit도 동일한 이벤트 흐름을 남기기 (`main.py`)
- P3-1b: RiskDecision을 "거부만"이 아니라 "허용/거부 모두" 남기기 (`main.py`)
- P3-1c: OrderAck 의미를 확정하고 기록하기 (REST 응답으로 order_id를 받는 시점을 Ack로 간주) (`core/events.py`, `main.py`)
- P3-1d: 이벤트 간 연결 필드(idempotency_key, correlation_id) 규칙을 payload에 일관되게 포함 (`core/events.py`, `main.py`)

#### P3-2: 주문 idempotency_key 규칙 확정(재시도 시 동일 키 유지)
**수정 파일:** `main.py`, `core/oms.py`, `ROADMAP_INSTITUTIONAL.md`  
**완료 조건:** 동일 intent에 대해 중복 주문 제출 시 브로커에서 rejection/error 없이 처리됨

- P3-2a: idempotency_key 생성 규칙을 "초 단위"에서 "run_id + monotonic seq" 또는 "uuid"로 변경 (`main.py`)
- P3-2b: 재시도 시 동일 키 유지 강제 (`main.py`, `core/oms.py`)
- P3-2c: idempotency가 "브로커 API 레벨"가 아니라 "엔진 중복 제출 방지(OMS)"임을 문서에 명시

#### P3-3: `KIS_INSTITUTIONAL_OMS=1`일 때만 OMS를 주문 제출 경로에 연결
**수정 파일:** `main.py`, `core/oms.py`, `tests/test_oms.py`  
**완료 조건:** OMS ON 시 주문 상태 전이가 OMS에 기록됨, OFF 시 기존 경로 비파괴

- P3-3a: TradingEngine이 OMS 인스턴스 생성 (플래그 ON 시) (`main.py`, `core/oms.py`)
- P3-3b: intent→risk→submit→ack→fill 전이를 OMS에 반영 (`core/oms.py`, `main.py`)
- P3-3c: OMS가 broker_order_id로 레코드 역추적 가능하도록 인덱스 확장 (`core/oms.py`)

### 완료 조건
- 특정 주문 1건의 entire lifecycle (entry+exit)이 이벤트 로그로 end-to-end 추적 가능
- OMS ON/OFF 모두 ruff/pytest 통과 + 기존 실행 경로 비파괴

---

## Phase 4 — Ledger/리플레이 고도화 + 3모듈 단일 엔진화(추천)
**목표:** Fill 기반 회계(ledger)로 포지션/현금을 단일 진실로 만들고, scalp/day/swing가 동일 엔진 위에서 동작.

### 현재 상태 (코드 기준)
- ⚠️ Ledger 스켈레톤 존재 (`core/ledger.py`: Position, Ledger classes) - long 포지션만 지원
- ⚠️ Fill 기반 ledger 실제 적용 미완료
- ⚠️ Replay CLI 기본 존재 but deterministic 재현 미확보
- ⚠️ 전략 모듈 인터페이스 표준화 미진행
- ⛔ Fill 소스(REST 체결내역 폴링) 미구현 (**핵심 선행조건**)

### 작업 티켓

#### P4-1: Fill 이벤트 기반 ledger 적용 + PositionSnapshot 이벤트 발행 (`KIS_INSTITUTIONAL_LEDGER=1`에서만)
**수정 파일:** `main.py`, `core/ledger.py`, `core/events.py`, `kis_rest_orders.py`  
**완료 조건:** Ledger 기반 포지션/현금과 기존 포지션 계산이 일치

> **⚠️ 핵심 선행조건:** Fill 이벤트의 신뢰 가능한 생성 경로(REST 체결내역 폴링 등)가 선행되어야 함

- P4-1a: Ledger 인스턴스 생성 및 Fill 이벤트를 ledger에 적용 (`main.py`, `core/ledger.py`)
- P4-1b: Fill 적용 직후 PositionSnapshot 이벤트 발행 (현금/수량/평단) (`main.py`, `core/events.py`)
- P4-1c: Fill 중복 방지 키 정의(브로커 체결ID 사용, 없으면 합성키) (`core/ledger.py`)
- P4-1d: 수수료/세금 반영 경로 확정 (FeeCalculator 사용 또는 0고정) (`fee_calculator.py`, `core/ledger.py`)
- P4-1e: **REST 체결내역 폴링 구현** - 기존 "포지션 조회로 Fill 추정" 방식 개선 (`kis_rest_orders.py`) ← **Phase 4 전체의 선행조건**

#### P4-2: replay를 deterministic하게 고도화(최소 Bar→Signal→RiskDecision 재현)
**수정 파일:** `tools/replay.py`, `core/ledger.py`  
**완료 조건:** replay가 동일한 시드 데이터로 동일한 결과를 재현

- P4-2a: 이벤트 타입 필터 정리 (현재 "Bar" → "Bar1mClosed" 등으로 정확한 이벤트 타입 매핑) (`tools/replay.py`)
- P4-2b: replay가 "Bar1mClosed→Signal→OrderIntent→(가짜 RiskDecision)→(가짜 Fill)"을 재생해 ledger 결과 재현/검증 (`tools/replay.py`, `core/ledger.py`)

#### P4-3: 이벤트 스키마 호환성 정책 문서화(필드 추가만 허용)
**수정 파일:** `ROADMAP_INSTITUTIONAL.md`  
**완료 조건:** 이벤트 스키마 evolution 정책 문서화

- P4-3a: "필드 추가만 허용, 기존 필드 삭제/변경 금지" 원칙을 문서에 명시
- P4-3b: 이벤트 버전 관리 정책 (major/minor) 결정

#### P4-4: 전략 모듈 인터페이스 표준화(`strategies/*`로 분리, 엔진은 공통)
**수정 파일:** `main.py`, `strategy_state_machine.py`, `modules/base.py`, `modules/*.py`  
**완료 조건:** scalp/day/swing 모듈이 동일한 OMS/Risk/Ledger/Event 위에서 운용 가능

- P4-4a: 표준 전략 인터페이스 결정 (후보: `strategy_state_machine.py` 또는 `modules/base.py`) (`main.py`)
- P4-4b: 엔진이 "전략은 신호만, 실행/로깅/OMS/Ledger는 공통" 구조로 분리 (`main.py`, `strategy_state_machine.py` 또는 `modules/*`)

### 완료 조건
- Ledger ON 시 포지션/현금이 Fill 기반으로 일관
- scalp/day/swing 모듈이 동일 OMS/Risk/Ledger/Event 위에서 운용 가능

---

## Phase 5 — 정합성(Reconcile) + 운영/관측/리포트(추천)
**목표:** 재시작/장애/불일치 상황에서 자동 정합성 확인 + 위험 시 자동 중지 + 리포트.

### 현재 상태 (코드 기준)
- ⚠️ Reconcile 스켈레톤 존재 (`core/reconcile.py`: reconcile_positions 함수) - qty 비교만
- ⛔ 실제 reconciliation 로직 미구현
- ⛔ 불일치 정책 미정의
- ⛔ 일일 리포트 자동 생성 미구현
- ⛔ health_status 운영 메트릭 확장 미진행
- ⛔ 브로커 어댑터 표준화 미진행

### 작업 티켓

#### P5-1: 브로커 어댑터 표준화 후 reconcile 주기 실행(기본 OFF)
**수정 파일:** `core/reconcile.py`, `main.py`, `kis_rest_orders.py`, `ROADMAP_INSTITUTIONAL.md`  
**완료 조건:** 내부 ledger/OMS 상태와 브로커 보고 상태의 periodic 비교가 동작

> **⚠️ 선행조건:** Ledger(P4-1)가 "단일 진실"로 동작해야 reconcile이 의미 있음

- P5-1a: 브로커 어댑터가 제공해야 할 최소 메서드 목록 정의 (positions, open_orders, executions/fills) (`ROADMAP_INSTITUTIONAL.md`)
- P5-1b: kis_rest_orders.py에 "전체 포지션 맵" 반환 메서드 추가 (reconcile 입력용) (`kis_rest_orders.py`)
- P5-1c: reconcile 주기적 실행 및 결과를 이벤트로 남김 (`main.py`, `core/reconcile.py`, `core/events.py`)

#### P5-2: 불일치 정책(추천: 신규 진입 차단 + 경고/리포트, 심각하면 kill-switch)
**수정 파일:** `core/reconcile.py`, `main.py`, `ROADMAP_INSTITUTIONAL.md`  
**완료 조건:** 불일치 발생 시 정의된 정책에 따라 자동 조치 실행

- P5-2a: mismatch severity 분류 정의
  - qty mismatch: 즉시 차단
  - avg_price mismatch: 허용오차 내 허용, 초과 시 경고
  - cash mismatch: 경고만
  - symbol set 차이: 신규 진입 차단 (`core/reconcile.py`, `ROADMAP_INSTITUTIONAL.md`)
- P5-2b: 조치 구현 (신규 진입 차단: 엔진 레벨 가드, kill-switch: STOP_TRADING.flag 생성) (`main.py`)

#### P5-3: 일일 리포트 자동 생성(모듈별 성과/체결/오류/리스크 차단)
**수정 파일:** `reporter.py`, `core/event_store.py`, `ROADMAP_INSTITUTIONAL.md`  
**완료 조건:** 매일(或는 정기) 리포트 자동 생성, 최소 포함 항목: fills, risk blocks, reconcile issues, errors, 플래그 상태

- P5-3a: 리포트 데이터 소스를 "로그 파싱"에서 "이벤트 스토어 기반"으로 전환/병행 (`reporter.py`, `core/event_store.py`)
- P5-3b: 리포트 필수 포함 항목 고정 (fills, risk blocks, reconcile issues, errors, 플래그 상태) (`ROADMAP_INSTITUTIONAL.md`)

#### P5-4: health_status 운영 메트릭 확장(이벤트 타입 분포/오류 카운트/플래그 상태)
**수정 파일:** `main.py`, `core/event_store.py`, `ROADMAP_INSTITUTIONAL.md`  
**완료 조건:** health.json에 이벤트 타입 분포/최근 오류 카운트/플래그 상태/최근 reconcile 결과 포함

- P5-4a: health에 추가 메트릭 포함 (이벤트 타입 분포, 최근 오류 카운트, OMS/Ledger/Reconcile 플래그 상태, 최근 reconcile 결과) (`main.py`, `core/event_store.py`)
- P5-4b: 비용 폭증 방지 (매 healthcheck마다 전체 JSONL 스캔 금지) - "최근 N 이벤트 샘플링/캐시" 설계 (`core/event_store.py`)

### 완료 조건
- 불일치/장애 시 자동 차단/복구/설명 가능
- 리포트만으로 "왜 이런 결과가 났는지" 판단 가능

---

## 의존성 그래프 (실행 순서)

```
Phase 3 (OMS + 이벤트) ──► Phase 4 (Ledger) ──► Phase 5 (Reconcile)
       │                        │                      │
       │                        │                      │
       ▼                        ▼                      ▼
  - P3-1 (이벤트 로깅)    - P4-1e (Fill 소스)    - P5-1 (Reconcile)
  - P3-2 (idempotency)   - P4-1 (Ledger 적용)   - P5-2 (불일치 정책)
  - P3-3 (OMS 연결)       - P4-2 (Replay)        - P5-3 (리포트)
                          - P4-4 (전략 표준화)    - P5-4 (헬스 메트릭)
```

### 핵심 선행조건 (문서에 강조)
1. **Fill 소스 확정은 Ledger/Reconcile의 선행조건**
   - Phase 4 전체가 Fill의 신뢰성에 의존
   - REST 체결내역 폴링(`kis_rest_orders.py`)이 P4-1e로 반드시 선행되어야 함
2. **Ledger가 "단일 진실"이 되어야 Phase 5 Reconcile이 의미 있음**
   - 내부 상태가 흔들리면 reconcile이 전부 false positive

---

## 완료 정의 (DoD)

| Phase | 완료 조건 |
|-------|----------|
| Phase 3 | 특정 주문 1건의 entire lifecycle (entry+exit)이 이벤트 로그로 end-to-end 추적 가능 |
| Phase 4 | Ledger 기반 포지션/현금과 기존 포지션 계산이 일치, scalp/day/swing이 동일 엔진 위에서 동작 |
| Phase 5 | 불일치/장애 시 자동 차단/복구/설명 가능, 리포트만으로 "왜 이런 결과가 났는지" 판단 가능 |

---

## 수정 파일 맵 (티켓별)

| 티켓 | 수정 파일 (후보) |
|------|-----------------|
| P3-1 | `main.py`, `core/events.py` |
| P3-2 | `main.py`, `core/oms.py` |
| P3-3 | `main.py`, `core/oms.py`, `tests/test_oms.py` |
| P4-1 | `main.py`, `core/ledger.py`, `core/events.py`, `kis_rest_orders.py` |
| P4-2 | `tools/replay.py`, `core/ledger.py` |
| P4-3 | `ROADMAP_INSTITUTIONAL.md` |
| P4-4 | `main.py`, `strategy_state_machine.py`, `modules/base.py`, `modules/*.py` |
| P5-1 | `core/reconcile.py`, `main.py`, `kis_rest_orders.py` |
| P5-2 | `core/reconcile.py`, `main.py` |
| P5-3 | `reporter.py`, `core/event_store.py` |
| P5-4 | `main.py`, `core/event_store.py` |
