# KIS ORB VWAP Bot

한국투자증권(KIS) Open API 기반의 **오프닝 레인지 브레이크아웃(ORB) + VWAP 지표** 중심 자동매매 봇(연구/실험용)입니다.

> ⚠️ 중요 고지
> - 이 프로젝트는 **투자 조언이 아니며**, 손실에 대한 책임은 사용자에게 있습니다.
> - 실전 계좌에 연결하기 전에 **모의투자/소액/충분한 테스트**를 권장합니다.
> - API 키/토큰/계좌번호 등 민감정보는 절대 깃허브에 올리지 마세요. (자세한 내용: `SECURITY.md`)

---

## 1) 빠른 시작(로컬)

### 요구사항

- Python 3.10+ (권장 3.11)
- (선택) Docker / docker-compose

### 설치

```bash
# 1) 의존성 설치
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# (옵션) 운영/추가 의존성
pip install -r requirements-prod.txt
```

### 환경변수(.env) 설정

1) `.env.example` 를 복사해서 `.env` 를 만듭니다.

```bash
cp .env.example .env
```

2) `.env` 에 본인 KIS 키를 입력합니다.

- `KIS_APP_KEY`
- `KIS_APP_SECRET`
- (필요시) 모의/실전 도메인 설정

> `.env` 는 `.gitignore` 로 기본 제외되지만, **실수로 커밋되지 않게 항상 확인**하세요.

### 설정(config.json)

- `config.json`의 `account.account_no` 는 예시 값(`YOUR_ACCOUNT_NO`)로 되어 있습니다.
- 실사용 시에는 **계좌번호를 공개 저장소에 남기지 말고**, 로컬 전용 설정 파일을 쓰는 방식을 권장합니다.
  - 예: `config.local.json` 을 만들어 `.gitignore` 에 추가

### 실행

```bash
python main.py
```

### KIS Daily Trade Report (cron 예시)

```bash
python scripts/daily_trade_report.py --date "$(date +%F)"
python scripts/recommend_next_day.py --date "$(date +%F)"
python scripts/verify_next_day_prep.py --date "$(date +%F)"
```

- cron 알림에서는 마지막 줄 `KIS Daily Trade Report Verification: PASS|FAIL ...` 요약 라인을 확인하세요.

---

## 2) 주요 개념

- **ORB (Opening Range Breakout)**: 장 시작 직후 일정 구간(예: 09:00~09:05)의 고가/저가 범위를 정의하고, 이후 돌파 시 진입
- **VWAP (Volume Weighted Average Price)**: 거래량 가중 평균 가격. 가격이 VWAP 위/아래에 있는지로 추세/수급을 보조 판단
- **리스크 관리**
  - 손절/익절
  - 일 손실 한도
  - ATR 기반 스탑/포지션 사이징(설정에 따라)
  - (추가) **포트폴리오 총 노출 상한**: `trading.max_total_position_pct` (기본 0.60)
  - (추가) **종목별 노출 상한**: `trading.max_symbol_position_pct` (기본 0.08)
  - (추가) **현금 최소 보유 비율**: `trading.cash_reserve_pct` (기본 0.20)
  - (추가) **신규 진입 속도 제한**: `trading.max_new_entries_per_minute` (기본 2)

### 동적 임계값 산출(공통 KR/US)

- 일일 추천 스크립트(`scripts/recommend_thresholds_daily.py`)는 KR/US 모두 같은 파이프라인으로 점수를 계산합니다.
- 입력 feature는 `prev_day`, `news`, `chart`, `vix`, `fg`, `atr` 6개입니다.
- 각 feature는 0~100의 부분 점수로 변환되고, 최종 점수(`score`)도 0~100으로 clamp 됩니다.
- 가중치는 고정값이 아니라, feature별 `신뢰도/커버리지/변동성`으로 자동 계산됩니다.
- 결측/NaN/표본 부족이면 해당 feature 가중치는 0으로 떨어지고 나머지로 자동 재분배됩니다.
- VIX/FG 값이 존재하면 최소 가중치를 확보하고(없으면 0), 최종 합은 항상 1로 normalize 됩니다.
- 전일 대비 가중치 급변은 제한하여(일일 변화 폭 제한) 과민 반응을 줄입니다.
- `score -> adjustment`는 선형 매핑으로 변환되며 보정폭은 최대 `+/-8`입니다.
- 점수가 낮을수록 `+adj`(더 엄격), 점수가 높을수록 `-adj`(더 완화)로 적용됩니다.
- 최종 임계값은 `base_threshold + adj` 후 `50..90` 범위로 clamp 됩니다.
- 산출 결과는 `logs/dynamic_thresholds.json`에 `thresholds`, `scoring(score/weights/components)`, `inputs` 요약으로 저장됩니다.
- 런타임에서는 `strategy_profiles.get_recommended_threshold()`가 해당 파일을 우선 읽고, 파일이 없으면 기본 임계값으로 fallback 합니다.
- 추가 연결: 일일 추천 스크립트는 당일 프리장 메트릭 캐시(`premarket_metrics_{kr,us}`)와 뉴스 캐시(`news_cache_{kr,us}`)를 우선 반영합니다.
- 프리장 메트릭은 `sample_count >= 10`인 경우에만 추천 입력(`prev_day/chart/atr`)에 반영됩니다.

### 동적 유니버스 운영(공통 KR/US)

- 유니버스 산정은 `universe_builder.build_universe()` 단일 파이프라인(후보→정규화/필터→always_include→중복제거→cap→품질필터)으로 처리합니다.
- 기본 동작은 항상 ON이며, 각 모듈의 `dynamic_universe.enabled`는 `true`를 권장/기본값으로 사용합니다.
- 긴급 중지 스위치: `KIS_DYNAMIC_UNIVERSE=0`이면 KR/US 전체 동적 유니버스를 즉시 비활성화합니다.
- SCALP 기본값: `top_n=60`, `cap=30`, `min_price=5`, `scan_interval_sec=300`, `min_liquidity=1,000,000`, 변동성 상위 선호.
- SWING 기본값: `top_n=120`, `cap=60`, `min_price=3`, `scan_interval_sec=600`, `min_liquidity=500,000`, 안정 추세 선호.
- US 심볼은 `A-Z0-9.-` 형식/길이 제한으로 검증하며, KR 심볼은 6자리 숫자로 검증합니다.
- KR의 `exclude_spac=true` 설정 시 스팩 종목은 best-effort로 제외됩니다.
- 스캐너가 비어 있거나 미설정이면 모듈의 `symbols`를 안전한 fallback 유니버스로 사용합니다.

---

## 3) 보안/개인정보

- 비밀키/토큰/계좌번호/로그는 커밋 금지
- 자세한 가이드는 `SECURITY.md` 참고

## 3-1) KR/US 프리마켓 데이터 수집(수집/분석 전용)

- `modules.mijang`(US)와 `modules.kukjang`(KR)은 프리마켓 구간에서 주문/전략/포지션 호출 없이 데이터 수집/뉴스/메트릭만 수행합니다.
- KR 프리마켓 기준 시간(권장): 평일 `08:00~09:00` KST
- US 프리마켓 기준 시간: `get_us_market_status().is_pre_market`
- 시세 샘플(1분 단위 권장):
  - US: `data/premarket_us/YYYYMMDD/<SYMBOL>.jsonl`
  - KR: `data/premarket_kr/YYYYMMDD/<SYMBOL>.jsonl`
- 뉴스 점수 캐시:
  - US: `data/news_cache_us/YYYYMMDD.json`
  - KR: `data/news_cache_kr/YYYYMMDD.json`
- 프리마켓 지표(변동률/ATR% 추정/추세):
  - US: `data/premarket_metrics_us/YYYYMMDD.json`
  - KR: `data/premarket_metrics_kr/YYYYMMDD.json`
- 동적 유니버스가 켜져 있으면 프리마켓에도 `scan_interval_sec` 기준으로 갱신됩니다.
- US 환경변수:
  - `KIS_US_PREMARKET_COLLECT`(기본 `1`)
  - `KIS_US_PREMARKET_POLL_SEC`(기본 `60`)
  - `KIS_US_PREMARKET_MAX_SYMBOLS`(기본 유니버스 cap)
  - `KIS_US_PREMARKET_SUMMARY_SEC`(기본 `600`)
  - `KIS_US_PREMARKET_NEWS_SEC`(기본 `300`)
  - `KIS_US_PREMARKET_METRICS_SEC`(기본 `300`)
- KR 환경변수:
  - `KIS_KR_PREMARKET_COLLECT`(기본 `1`)
  - `KIS_KR_PREMARKET_POLL_SEC`(기본 `60`)
  - `KIS_KR_PREMARKET_MAX_SYMBOLS`(기본 `dynamic_universe.max_symbols`, 없으면 유니버스 길이)
  - `KIS_KR_PREMARKET_SUMMARY_SEC`(기본 `600`)
  - `KIS_KR_PREMARKET_NEWS_SEC`(기본 `300`)
  - `KIS_KR_PREMARKET_METRICS_SEC`(기본 `300`)

---

## 3-2) 멀티 포지션 설계 메모

- 엔진은 단일 `position` 대신 심볼별 `positions` 맵을 기준으로 동작합니다.
- 기본 동시 보유 한도는 `trading.max_concurrent_positions`(기본값 `20`)이며, 활성 포지션 수가 한도에 도달하면 신규 진입을 차단합니다.
- TP/SL/ATR/퀵익절/프로핏락/트레일링/브레이크이븐 로직은 심볼별 포지션 상태(`tp1_done`, `profit_locked`, `peak_pnl`)를 독립적으로 평가합니다.
- 시간 기반 청산(`early_exit`/`force_exit`/`emergency`)은 모든 오픈 포지션에 대해 일괄 청산을 수행합니다.
- `health_status.json`에는 기존 `position`(호환용)과 함께 `positions`, `active_positions_count`, `max_concurrent_positions`가 기록됩니다.

---

## 4) 릴리즈(태그) 자동화

이 저장소는 `release-please` 기반으로 태그/릴리즈를 자동화합니다.

- `main` 브랜치에 커밋이 쌓이면, release-please가 **릴리즈 PR** 을 생성합니다.
- PR을 머지하면 자동으로 `vX.Y.Z` 태그와 GitHub Release가 생성됩니다.

권장 커밋 메시지 예시(Conventional Commits 스타일):

- `feat: add US swing module`
- `fix: prevent duplicate orders on reconnect`
- `docs: improve quickstart`

---

## 5) 디렉토리 구조(요약)

- `main.py` : 엔트리포인트
- `core/` : 이벤트/OMS/원장/리컨실 등 핵심 로직
- `modules/` : 전략/모듈(국장/미장/스윙 등)
- `scripts/` : 운영 스크립트(모니터링/스냅샷/헬스체크 등)
- `dashboard/` : 대시보드(있을 경우)

---

## 6) 면책 조항

본 소프트웨어는 교육/연구 목적이며, 어떠한 수익도 보장하지 않습니다.
실거래 사용에 따른 모든 책임은 사용자에게 있습니다.
