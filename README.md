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
