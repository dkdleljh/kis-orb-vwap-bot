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

---

## 2) 주요 개념

- **ORB (Opening Range Breakout)**: 장 시작 직후 일정 구간(예: 09:00~09:05)의 고가/저가 범위를 정의하고, 이후 돌파 시 진입
- **VWAP (Volume Weighted Average Price)**: 거래량 가중 평균 가격. 가격이 VWAP 위/아래에 있는지로 추세/수급을 보조 판단
- **리스크 관리**
  - 손절/익절
  - 일 손실 한도
  - ATR 기반 스탑/포지션 사이징(설정에 따라)

---

## 3) 보안/개인정보

- 비밀키/토큰/계좌번호/로그는 커밋 금지
- 자세한 가이드는 `SECURITY.md` 참고

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
