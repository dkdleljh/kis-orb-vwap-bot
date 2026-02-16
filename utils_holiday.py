from datetime import date
import holidays

# 대한민국 공휴일 로드 (관공서 공휴일 규정)
# years 인자를 주지 않으면 호출 시점의 연도를 자동 계산하지만,
# 안전하게 범위를 넉넉히 잡습니다.
kr_holidays = holidays.KR()  # type: ignore[attr-defined]


def is_market_open(target_date: date) -> bool:
    """해당 날짜가 한국 증시 개장일인지 확인합니다."""

    # 1. 주말 체크 (토=5, 일=6)
    if target_date.weekday() >= 5:
        return False

    # 2. 법정 공휴일 체크 (신정, 삼일절, 어린이날, 석가탄신일, 현충일, 광복절, 개천절, 성탄절, 한글날)
    # + 음력 휴일 (설날, 추석) 자동 계산됨
    if target_date in kr_holidays:
        return False

    # 3. 근로자의 날 (5월 1일) - 한국 증시 휴장
    if target_date.month == 5 and target_date.day == 1:
        return False

    # 4. 연말 휴장일 (12월 31일) - 한국 증시 휴장
    if target_date.month == 12 and target_date.day == 31:
        return False

    return True


def get_holiday_name(target_date: date) -> str | None:
    """휴일인 경우 휴일 명칭을 반환합니다."""
    if target_date.weekday() >= 5:
        return "Weekend"

    name = kr_holidays.get(target_date)
    if name:
        return name

    if target_date.month == 5 and target_date.day == 1:
        return "Labor Day (Stock Market Closed)"

    if target_date.month == 12 and target_date.day == 31:
        return "Year-End Closing Day"

    return None
