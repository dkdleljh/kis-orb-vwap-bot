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

## 12) 주인님 운영 기준(권장 기본 조합)

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

## 13) 디렉토리 구조(요약)

- `main.py` : 엔트리포인트(모듈 시스템 포함)
- `modules/` : 전략/모듈(kukjang, kr_swing, mijang, us_swing, hwanjeon)
- `scripts/` : 운영 스크립트(추천 임계값, 모니터링 등)
- `data/` : 수집/캐시 데이터(premarket, 뉴스 캐시 등)
- `logs/` : 로그

---

## 13) 면책 조항

본 소프트웨어는 교육/연구 목적이며, 어떠한 수익도 보장하지 않습니다.
실거래 사용에 따른 모든 책임은 사용자에게 있습니다.
