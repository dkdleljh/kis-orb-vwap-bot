# KIS ORB/VWAP Bot (KR/US)

한국투자증권(KIS) OpenAPI 기반으로 **국장(KR)** 과 **미장(US)** 을 운용할 수 있는 자동매매/데이터 수집 봇입니다.

- 국장: ORB/VWAP 중심 + 리스크 관리 + (선택) 스윙 모듈
- 미장: ORB/VWAP 단타 모듈 + 15분 기반 스윙 모듈
- 공통: 동적 유니버스, 동적 임계값(추천 threshold), 프리장 데이터 수집/분석

> ⚠️ 중요 고지
> - 본 프로젝트는 **투자 조언이 아니며**, 손실에 대한 책임은 사용자에게 있습니다.
> - 실계좌 연결 전 **모의/소액/충분한 테스트**를 권장합니다.
> - API 키/토큰/계좌번호 등 **민감정보는 절대 GitHub에 커밋하지 마세요.** (자세한 가이드: `SECURITY.md`)

---

## 0) 가장 쉬운 시작(요약)

```bash
cd kis_orb_vwap_bot
python -m venv venv
source venv/bin/activate
pip install -r requirements-prod.full.txt -r requirements-dev.txt

cp .env.example .env   # .env에 키 입력(로컬에만)

# US 모듈 실행(권장 스크립트)
bash scripts/run_us_modules.sh

# KR 모듈 실행(권장 스크립트)
bash scripts/run_kr_modules.sh
```

---

## 1) 요구사항

- Python 3.10+ (권장 3.11~3.12)
- Linux/macOS 권장 (Windows도 가능하나 운영은 WSL 권장)

---

## 2) 설치(권장: full)

```bash
python -m venv venv
source venv/bin/activate

# 운영+개발 의존성(테스트 포함)
pip install -r requirements-prod.full.txt -r requirements-dev.txt
```

설치 확인:
```bash
./venv/bin/python -m pytest -q
```

---

## 3) 민감정보(보안) 설정: `.env`

1) 예시 파일 복사
```bash
cp .env.example .env
```

2) `.env`에 아래 값을 채웁니다(절대 커밋 금지)
- `KIS_APP_KEY`
- `KIS_APP_SECRET`
- (필요 시) `KIS_HTS_ID`

> 팁
> - `.env`는 기본적으로 `.gitignore`에 포함돼 있어야 합니다.
> - 커밋 전 항상 `git status`로 `.env`가 스테이징되지 않았는지 확인하세요.

---

## 4) 설정 파일

이 저장소는 **시장별 설정을 분리**해서 사용합니다.

- `config.kr.json` : 국장(KR) 모듈 설정
- `config.us.json` : 미장(US) 모듈 설정

핵심 포인트:
- 계좌번호를 파일에 직접 적기 싫으면, 환경변수로 주입 가능합니다.
  - `KIS_ACCOUNT_NO`
  - `KIS_ACCOUNT_PRODUCT_CODE`

`time_rules` 스키마(형식: `HH:MM:SS`, 누락 시 기본값 사용):
- `observe_start` (기본 `08:59:00`)
- `or_start` (기본 `09:00:00`)
- `or_end` (기본 `09:05:00`)
- `entry_start` (기본 `09:05:05`)
- `early_exit` (기본 `15:00:00`)
- `force_exit` (기본 `15:15:00`)

운영 동작:
- `15:00` 이후: 조기 청산(`early_exit`)
- `15:15` 이후: 강제 청산(`force_exit`)
- `force_exit + 3분` 이후: 긴급 시장가 청산(예: `15:18`)
- 누락/잘못된 값은 런타임 로그에 어떤 키가 기본값으로 대체됐는지 기록됩니다.
- 관련 구현: `core/session_rules.py` (time_rules 파싱/기본값 + exit phase 계산)

---

## 5) 실행 방법(모듈 시스템)

이 프로젝트는 `main.py --modules` 모드에서 **설정 파일에 enabled 된 모듈만** 로드합니다.

### 5-1) US 실행(권장)
```bash
bash scripts/run_us_modules.sh
```
- `config.us.json`을 사용
- 기본으로 로그는 `logs/nohup_us_modules.log`에 쌓입니다.

중지:
```bash
bash scripts/stop_us_modules.sh
```

### 5-2) KR 실행(권장)
```bash
bash scripts/run_kr_modules.sh
```

중지:
```bash
bash scripts/stop_kr_modules.sh
```

---

## 6) 실거래(라이브) 안전장치

### 6-1) 전역 긴급 중단
아래 파일이 존재하면 **매수/매도 주문을 차단**합니다.
```bash
touch STOP_TRADING.flag
```
해제:
```bash
rm -f STOP_TRADING.flag
```

### 6-2) US 실주문 게이트
US는 **명시적 동의 플래그** 없이는 실주문을 내지 않습니다.
- `KIS_US_LIVE_CONFIRM=YES` 일 때만 실주문 허용
- `KIS_KILL_SWITCH=1` 이면 즉시 주문 차단

> 현재 `scripts/run_us_modules.sh`는 사용자가 실거래를 허용한 상태를 기준으로 `KIS_US_LIVE_CONFIRM=YES`, `KIS_KILL_SWITCH=0`를 세팅합니다.

---

## 7) 동적 유니버스(항상 ON, KR/US 동일 로직)

유니버스(매수 후보 종목)는 `universe_builder.build_universe()`의 단일 파이프라인으로 산정합니다.

- 후보 생성 → 정규화/필터 → always_include → 중복 제거 → cap → 품질 필터
- 기본: 항상 ON
- 긴급 OFF:
  - `KIS_DYNAMIC_UNIVERSE=0`

스타일별 추천값(기본):
- **SCALP(단타)**: top_n=60, cap=30, min_price=5, min_liquidity=1,000,000, scan_interval=300s, 변동성 상위 선호
- **SWING(스윙)**: top_n=120, cap=60, min_price=3, min_liquidity=500,000, scan_interval=600s, 안정 추세 선호

---

## 8) 동적 추천 임계값(추천 threshold) — KR/US 공통

`scripts/recommend_thresholds_daily.py`는 아래 입력을 바탕으로 **0~100점 score**를 만들고,
그 score로 **진입 임계값(threshold)을 동적으로 보정**합니다.

입력 feature (6개):
- 전일 데이터(prev_day)
- 뉴스(news)
- 차트/지표(chart)
- VIX(vix)
- Fear&Greed(fg)
- ATR(atr)

특징:
- 가중치 고정이 아니라, **커버리지/신뢰도/변동성**에 따라 자동 조정
- 결측이면 해당 feature 가중치 0, 나머지로 자동 재분배
- score → adjustment 보정폭 최대 ±8
- 결과 저장: `logs/dynamic_thresholds.json` (v2)

---

## 9) 프리장 데이터 수집/분석(수집 전용)

프리장 구간에는 **절대 주문하지 않고**, 수집/분석만 수행합니다.

- US 프리마켓: `modules.mijang` 프리마켓 모드에서 수집
- KR 장전: `modules.kukjang`에서 08:00~09:00(KST) 수집

저장 구조:
- 시세 샘플(JSONL)
  - `data/premarket_us/YYYYMMDD/<SYMBOL>.jsonl`
  - `data/premarket_kr/YYYYMMDD/<SYMBOL>.jsonl`
- 뉴스 캐시
  - `data/news_cache_us/YYYYMMDD.json`
  - `data/news_cache_kr/YYYYMMDD.json`
- 메트릭(변동률/ATR% 추정/추세)
  - `data/premarket_metrics_us/YYYYMMDD.json`
  - `data/premarket_metrics_kr/YYYYMMDD.json`

프리장 메트릭은 **sample_count >= 10**일 때만 추천 임계값 산출에 반영합니다.

---

## 10) 통합증거금(Integrated Margin) 모드(US)

USD 현금 조회가 불안정하거나 0으로 나올 때를 대비해,
KRW 기반으로 US 매수여력을 추정하는 모드입니다.

- `KIS_US_USE_INTEGRATED_MARGIN=1` 활성
- `ord_psbl_qty`가 0/미확인인 경우에도 추정치가 충분하면 **1회 주문 시도 허용**
- 주문 실패 시 심볼별 쿨다운(기본 120초)

---

## 11) US 멀티 포지션(동시 보유) 지원

이제 US는 단일 포지션이 아니라 **심볼별 포지션 맵**으로 동작합니다.

추천 기본값:
- `KIS_US_MAX_POSITIONS=5`

원칙:
- 이미 보유한 심볼은 신규 진입 금지
- 보유 심볼 수가 max에 도달하면 신규 진입 금지

---

## 12) 무인 학습/자동 튜닝(Next-day Auto Tuning)

이 프로젝트는 **매일 장 마감 후 데이터(이벤트/리포트)를 학습**하고, 다음날 장 시작 전에
**전략 파라미터를 자동으로 업데이트**할 수 있습니다.

### 12-1) 파이프라인 구성
1) 일일 리포트 생성
```bash
./venv/bin/python scripts/daily_trade_report.py --date YYYY-MM-DD
```
- 입력: `logs/events/YYYYMMDD/events.jsonl`
- 출력: `reports/trade_report_YYYY-MM-DD.md`

2) 다음날 추천값 생성(ML 학습+추론)
```bash
./venv/bin/python scripts/recommend_next_day.py --date YYYY-MM-DD
```
- 입력: `logs/events/...` + `reports/trade_report_...`
- 출력: `reports/next_day_reco_YYYY-MM-DD.(json|md)`
- 특징: 과거 N일 학습(기본 30일), 모델 아티팩트 저장(`reports/models/`), 신뢰도/설명(importance) 포함 추천 생성.
- 추가: walk-forward 검증(최근 K일, prior-days train)으로 `MAE`/`directional_accuracy`를 기록하고 추천 게이트에 반영.

2-0) walk-forward 포함 실행 예시
```bash
./venv/bin/python scripts/recommend_next_day.py --date YYYY-MM-DD --lookback-days 45 --walk-forward-k 10 --wf-min-evals 4
```

2-1) 준비 검증(자동 복구 + immutable enrichment)
```bash
./venv/bin/python scripts/verify_next_day_prep.py --date YYYY-MM-DD --auto-enrich
./venv/bin/python scripts/verify_next_day_prep.py --date YYYY-MM-DD --auto-enrich --strict-signal-context
./venv/bin/python scripts/verify_next_day_prep.py --date YYYY-MM-DD --auto-enrich --strict-signal-context --strict-require-enriched-no
```
- 순서: 아티팩트 자동생성 → 이벤트 품질검사 → 컨텍스트 결함 시 1회 enrichment → 재검증
- 이벤트 envelope는 `schema_version` 필드를 포함하며, 구버전 로그도 역호환 처리됩니다.
- 원본 이벤트는 불변 유지, 파생 파일 생성:
  - `logs/events/YYYYMMDD/events.enriched.jsonl`
  - `logs/events/YYYYMMDD/events.enrichment_patches.jsonl`
- 검증 메트릭: `reports/next_day_prep_metrics_YYYY-MM-DD.json`
- `--strict-signal-context` 사용 시 Signal 컨텍스트를 절대 개수(>=1)가 아니라 비율 기준으로 검사합니다.
  - 기본 임계값: `min_score >= 30%`, `reasons >= 10%` (옵션으로 조정 가능)
- `--strict-require-enriched-no`를 함께 쓰면 strict 모드에서 `enriched_used==NO`를 추가로 요구합니다(기본값은 비활성, 하위호환 유지).

3) 추천값 자동 적용(무인)
```bash
./venv/bin/python scripts/apply_next_day_reco.py --yesterday
```
- 적용 대상: **화이트리스트 키만**
  - `trading.cash_reserve_pct`
  - `trading.entry_budget_pct`
  - `trading.scoring.kr_scalp_entry_threshold`
  - `trading.symbol_cooldown_sec`
  - `trading.atr_min_percent`
- 안전장치:
  - 안전 범위(clamp) + 최소 신뢰도(confidence) 통과 시에만 적용
  - 적용 전 설정 백업 생성
  - 적용 내역 `reports/applied/`에 감사 로그 저장
  - 적용 후 `scripts/smoke_test.sh` + `scripts/healthcheck_kis.py` 실행
  - 실패 시 **자동 롤백 + 서비스 재시작**

> 운영 팁
> - 무인 모드에서 `next_day_reco_*.json`이 없으면 `apply_next_day_reco.py`는 **OK로 스킵**합니다.

### 12-2) 자동 실행(권장)
현재 운영은 systemd timer(OpenClaw)로 **매일 00:05(KST)** 에 다음을 수행하도록 구성할 수 있습니다.
- `scripts/apply_next_day_reco.sh` 실행(전날 추천 적용)

역할 분리 권장:
- `systemd/scheduler`: 실행 시각 보장, 재시도, 프로세스 재기동
- 봇/스크립트(`main.py`, `scripts/*.py`): 거래 로직, 검증, 리스크 가드

---

## 13) 실전 운영 체크리스트(장 시작 전/중/후)

### 12-1) 장 시작 전(필수)
- [ ] `STOP_TRADING.flag`가 **의도대로** 설정되어 있는지 확인
  - 실거래를 막고 싶으면: `touch STOP_TRADING.flag`
  - 실거래를 허용할 거면: `rm -f STOP_TRADING.flag`
- [ ] (US) 실주문 게이트 확인
  - `KIS_US_LIVE_CONFIRM=YES`
  - `KIS_KILL_SWITCH=0`
- [ ] 프로세스/로그가 살아있는지 확인
  - US: `ps -p $(cat .kis_us_pid) -o pid,etime,cmd`
  - KR: `ps -p $(cat .kis_kr_pid) -o pid,etime,cmd`
- [ ] 동적 추천 임계값 갱신(권장)
  - `./venv/bin/python scripts/recommend_thresholds_daily.py`
  - 결과 파일: `logs/dynamic_thresholds.json`
- [ ] (선택) 프리장/장전 수집이 정상인지 확인
  - `data/premarket_us/YYYYMMDD/` / `data/premarket_kr/YYYYMMDD/`

### 12-2) 장중(모니터링)
- [ ] 주문/시그널 로그 확인
  - `logs/nohup_us_modules.log` / `logs/nohup_kr_modules.log`
- [ ] “매수가 안 됨” 상황 점검 순서
  1) STOP_TRADING.flag
  2) US 실주문 게이트(US)
  3) 최대 포지션(`KIS_US_MAX_POSITIONS`)
  4) 시그널(`US signal`) 존재 여부
  5) 주문 리젝트/쿨다운 로그

### 12-3) 장 끝/야간(정리)
- [ ] 필요 시 수동으로 주문 차단(안전)
  - `touch STOP_TRADING.flag`
- [ ] 다음 날 대비: `logs/dynamic_thresholds.json` 및 프리장 캐시가 생성되는지 확인

---

## 14) 주인님 운영 기준(권장 기본 조합)

현재 운영(추천값) 기준으로는 아래 조합이 가장 안정적입니다.

### US(미장): `us_swing`만 ON, `mijang` OFF
- 이유: `mijang`(단타)은 빈번한 호가/1분 데이터/주문가능조회 이슈에 민감하고,
  `us_swing`은 상대적으로 신호가 덜 빈번해서 운영 안정성이 좋습니다.

설정 확인(`config.us.json`):
- `modules.us_swing.enabled = true`
- `modules.mijang.enabled = false`

### KR(국장): `kukjang` ON + `kr_swing`(선택)
- 기본은 `kukjang`만으로도 운용 가능
- 스윙을 같이 쓰면 종목 수/리스크/동시 포지션을 보수적으로 조절하는 걸 권장

설정 확인(`config.kr.json`):
- `modules.kukjang.enabled = true`
- `modules.kr_swing.enabled = true` (원치 않으면 false)

---

## 15) 디렉토리 구조(요약)

- `main.py` : 최소 엔트리포인트(`engine.core_engine.TradingEngine` 실행/모듈 모드 부팅)
- `engine/core_engine.py` : TradingEngine 오케스트레이터/루프
- `engine/strategy.py` : 시그널 생성, 바 핸들러, TP/SL 평가
- `engine/io_adapters.py` : REST/WS 연동 상태 래퍼
- `engine/state.py` : 상태머신 연동/진입·청산 glue
- `core/entry_gates.py` : 진입 게이트(노출/예산/쿨다운/리스크 이벤트)
- `core/order_executor.py` : 주문 실행 래퍼(KISRestOrders 호출 + 주문/체결 이벤트)
- `core/position_manager.py` : 포지션 상태/복구/스냅샷 오케스트레이션
- `core/session_rules.py` : 시간 규칙/exit phase 계산
- `modules/` : 전략/모듈(kukjang, kr_swing, mijang, us_swing, hwanjeon)
- `scripts/` : 운영 스크립트(추천 임계값, 모니터링 등)
- `data/` : 수집/캐시 데이터(premarket, 뉴스 캐시 등)
- `logs/` : 로그

---

## 16) 면책 조항

본 소프트웨어는 교육/연구 목적이며, 어떠한 수익도 보장하지 않습니다.
실거래 사용에 따른 모든 책임은 사용자에게 있습니다.
