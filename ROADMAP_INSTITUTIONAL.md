# Institutional Upgrade Roadmap

목표: 개인 운용 → 기관 투자자 수준의 엔진 품질 달성

---

## 100점 정의

### 기관 수준 (이벤트 추적)
- Signal→Risk→Order→Ack→Fill→Ledger→PositionSnapshot 완전 추적
- 브로커 기반 OMS/정합성으로 포지션/현금 불일치 자동 감지

### 무인 자동화 (알림 없는 환경)
- 이상 발생 시 **무인 안전정지** 100% 발동
- 신규 진입 즉시 차단, kill-switch 자동 ON
- health_status + 이벤트로 "왜 멈췄는지" 로컬 기록
- 다음 영업일 자동 재가동

---

## 기본 정책 (자본 1,000만원)

### 리스크 제한
| 항목 | 값 |
|------|-----|
| 일 손실 제한 | -1.2% |
| 종목당 최대 노출 | 12% |
| 총 노출 최대 | 35% |
| scalp 1트레이드 | 0.25% |
| day 1트레이드 | 0.35% |
| swing 1트레이드 | 0.50% |

### 유니버스
| 전략 | 실시간 후보 |
|------|----------|
| scalp | 10~25 |
| day | 20~40 |
| swing | 50~200 |

### Feature Flags (기본 OFF)
```
KIS_INSTITUTIONAL_OMS=0
KIS_INSTITUTIONAL_LEDGER=0
KIS_INSTITUTIONAL_RECONCILE=0
```

---

## Phase 3 — OMS + 이벤트

**목표:** Signal→RiskDecision→Order→Ack→Fill 완전 추적

### 완료 조건
- 주문 1건 전체 라이프사이클이 이벤트 로그로 추적 가능

### 작업 항목

| 항목 | 설명 |
|------|------|
| P3-1 | entry + exit 이벤트 로깅 |
| P3-2 | idempotency_key 규칙 변경 (run_id + seq) |
| P3-3 | OMS 모듈 연결 (플래그 ON 시) |

---

## Phase 4 — Ledger + Replay

**목표:** Fill 기반 회계로 포지션/현금 단일 진실

### 완료 조건
- Ledger 기반 포지션과 기존 계산 일치

### 작업 항목

| 항목 | 설명 |
|------|------|
| P4-1 | Fill 이벤트 기반 ledger 적용 |
| P4-2 | replay deterministic 고도화 |
| P4-3 | 이벤트 스키마 정책 문서화 |
| P4-4 | 전략 모듈 인터페이스 표준화 |

### 핵심 선행조건
- **REST 체결내역 폴링** 구현 (P4-1e)

---

## Phase 5 — Reconcile + 운영

**목표:** 불일치 자동 감지 + 자동 차단 + 리포트

### 완료 조건
- 장애 시 자동 차단/복구/설명 가능

### 작업 항목

| 항목 | 설명 |
|------|------|
| P5-1 | reconcile 주기 실행 |
| P5-2 | 불일치 정책 (차단/경고) |
| P5-3 | 일일 리포트 자동 생성 |
| P5-4 | health_status 메트릭 확장 |

---

## 의존성 순서

```
Phase 3 → Phase 4 → Phase 5
  ↓          ↓          ↓
이벤트     Ledger    Reconcile
```

---

## 완료 정의

| Phase | 조건 |
|-------|------|
| Phase 3 | 주문 라이프사이클 완전 추적 |
| Phase 4 | Ledger 기반 포지션 일치 |
| Phase 5 | 자동 차단/복구 가능 |
