# KIS ORB/VWAP Bot (KR/US) — 사용자가 바로 돌릴 수 있는 자동매매/수집 봇

한국투자증권(KIS) OpenAPI 기반으로 **국장(KR)** 과 **미장(US)** 을 운용할 수 있는 자동매매/데이터 수집 봇입니다.

- 국장: ORB/VWAP 중심 + 리스크 관리 + (선택) 스윙 모듈
- 미장: ORB/VWAP 단타 모듈 + 15분 기반 스윙 모듈
- 공통: 동적 유니버스, 동적 임계값(추천 threshold), 프리장 데이터 수집/분석

> ⚠️ 면책
> - 투자 조언이 아닙니다.
> - 실계좌 연결 전 **모의/소액/충분한 테스트**를 권장합니다.
> - API 키/토큰/계좌번호 등 **민감정보는 절대 GitHub에 커밋하지 마세요.**

---

## 문서
- 초보자용 사용설명서: `사용설명서.md`
- 실행 안내서(기존): `실행_안내서.md`
- 보안 가이드: `SECURITY.md`

---

## 빠른 시작(요약)

```bash
cd ~/Desktop/kis_orb_vwap_bot
python -m venv venv
source venv/bin/activate
pip install -r requirements-prod.full.txt -r requirements-dev.txt

cp .env.example .env
nano .env  # KIS_APP_KEY/KIS_APP_SECRET 입력

# KR 실행
bash scripts/run_kr_modules.sh

# US 실행
bash scripts/run_us_modules.sh
```

---

## 매매 전략(요약)

> 이 섹션은 **전략 아이디어를 이해하기 위한 개요**입니다. 실제 매매 규칙/예외/게이트는 `config.kr.json`, `config.us.json` 및 각 모듈 구현을 따릅니다.

### KR (국장)
- 기본 아이디어: **장 초반 변동(ORB) + VWAP(체결가의 거래대금 가중 평균가) 기반**으로 추세/복귀를 구분해 진입/청산을 설계합니다.
- 동작 형태:
  - 정해진 시간대에 관찰/진입/조기청산/강제청산 단계로 진행(예: 15:15 전량 청산)
  - 리스크 게이트(예산/쿨다운/STOP_TRADING/kill switch)를 통과해야만 주문

### US (미장)
- 기본 아이디어: **ORB/VWAP 단타 모듈 + (선택) 15분 기반 스윙 모듈**을 조합합니다.
- 운영 특징:
  - 실주문은 `KIS_US_LIVE_CONFIRM=YES` 등 명시적 동의 플래그가 있어야만 허용
  - 통합증거금 추정/주문가능수량 0 처리 등 실전 이슈를 완화하는 안전 로직 포함

### 공통(전략 운영을 도와주는 기능)
- **동적 유니버스**: 유동성/가격/변동성 기준으로 매수 후보를 자동 구성
- **동적 임계값(추천 threshold)**: 뉴스/차트/변동성 등 컨텍스트로 진입 임계값을 자동 보정
- **프리장/장전 수집**: 주문 없이 데이터만 수집해 다음날/당일 의사결정에 활용

---

## 실행/중지(가장 많이 쓰는 명령)

- KR 실행: `bash scripts/run_kr_modules.sh`
- KR 중지: `bash scripts/stop_kr_modules.sh`
- US 실행: `bash scripts/run_us_modules.sh`
- US 중지: `bash scripts/stop_us_modules.sh`

로그:
- KR: `logs/nohup_kr_modules.log`
- US: `logs/nohup_us_modules.log`

---

## 실거래 안전장치(중요)

1) 전역 긴급 중단:
```bash
touch STOP_TRADING.flag
```

2) (US) 실주문 게이트(환경변수)
- `KIS_US_LIVE_CONFIRM=YES` 일 때만 실주문
- `KIS_KILL_SWITCH=1`이면 주문 즉시 차단

---

## 설정 파일

- `config.kr.json`: 국장 설정
- `config.us.json`: 미장 설정

모듈은 `enabled`로 제어합니다.

---

## 이 프로젝트가 잘하는 것

- 운영 친화적인 모듈 구조(`main.py --modules`)
- 레이트리밋/토큰/승인키 캐시
- 동적 유니버스 + 동적 임계값 추천
- 안전한 실주문 게이트 + STOP_TRADING

---

## 지원/운영 팁

문제가 생기면 먼저 아래를 확인하세요.
1) `STOP_TRADING.flag`
2) `.env` 실주문 confirm/kill switch
3) `config.kr.json / config.us.json`에서 모듈 enable
4) 로그 파일 tail

<!-- BEGIN RELEASE STATUS -->
## 최신 배포 정보

- 저장소 버전: `v2026.10.10.1`
- [변경사항과 검증 범위](RELEASE_NOTES.md)
- [GitHub 릴리즈](https://github.com/dkdleljh/kis-orb-vwap-bot/releases/latest)
<!-- END RELEASE STATUS -->
