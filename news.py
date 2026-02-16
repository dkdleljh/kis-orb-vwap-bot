import logging
import re
import aiohttp
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

LOGGER = logging.getLogger(__name__)


class NewsSentimentAnalyzer:
    def __init__(self, max_news_age_hours: int = 3):
        self.max_news_age_hours = max_news_age_hours
        self.cache: dict[str, dict] = {}
        self.cache_ttl_seconds = 300

        # 호재 키워드 (가중치)
        self.KEYWORDS_GOOD = {
            "체결": 20,
            "계약": 20,
            "수주": 20,
            "공급": 15,
            "흑자": 15,
            "최대": 10,
            "급등": 5,
            "상한가": 20,
            "인수": 15,
            "합병": 15,
            "특허": 10,
            "승인": 10,
            "개발": 5,
            "성공": 5,
            "자사주": 10,
            "소각": 10,
            "취득": 5,
            "호실적": 15,
            "서프라이즈": 15,
            "MOU": 5,
            "제휴": 5,
            "협력": 5,
            "공시": 2,
            "전망 상향": 25,
            "목표가 상향": 25,
            "매수상향": 25,
            "호실적 전망": 20,
            "수주 계약": 25,
            "글로벌": 10,
            "AI": 15,
            "반도체": 10,
            "수익성 개선": 15,
        }

        # 악재 키워드 (가중치)
        self.KEYWORDS_BAD = {
            "유상증자": -30,
            "유증": -20,
            "감자": -30,
            "횡령": -50,
            "배임": -50,
            "적자": -10,
            "손실": -10,
            "급락": -5,
            "하한가": -20,
            "거절": -15,
            "중단": -15,
            "해지": -20,
            "불성실": -20,
            "지연": -10,
            "반려": -15,
            "이탈": -10,
            "매도": -5,
            "경고": -5,
            "주의": -5,
            "과열": -5,
            "전망 하향": -25,
            "목표가 하향": -25,
            "매수하향": -25,
            "적자 확대": -20,
            "매출 감소": -15,
            "운영상 문제": -20,
        }

        self.KEYWORDS_US_GOOD = {
            "beat": 15,
            "surge": 10,
            "rally": 10,
            "gain": 8,
            "upgrade": 20,
            "outperform": 15,
            "buy": 15,
            "profit": 10,
            "growth": 10,
            "record": 10,
            "high": 5,
            "bullish": 10,
            "dividend": 10,
            "bonus": 10,
            "acquisition": 15,
            "merger": 15,
            "contract": 10,
            "partnership": 8,
            "innovation": 5,
            "beat estimates": 20,
            "raise": 15,
            "exceed": 15,
            "breakout": 10,
        }

        self.KEYWORDS_US_BAD = {
            "cut": -15,
            "downgrade": -20,
            "sell": -15,
            "bearish": -10,
            "loss": -10,
            "plunge": -15,
            "drop": -8,
            "fear": -5,
            "lawsuit": -20,
            "investigation": -15,
            "recall": -15,
            "scandal": -20,
            "bankruptcy": -30,
            "default": -20,
            "crisis": -15,
            "warning": -10,
            "miss": -10,
            "weak": -5,
            "concern": -5,
            "risk": -5,
            "lower guidance": -25,
            "cut estimates": -20,
            "miss estimates": -20,
        }

    def _is_cache_valid(self, symbol: str) -> bool:
        if symbol not in self.cache:
            return False
        cache_time = self.cache[symbol].get("cached_at", 0)
        return (datetime.now().timestamp() - cache_time) < self.cache_ttl_seconds

    async def get_sentiment_score(self, symbol: str) -> dict:
        """종목 코드로 네이버 금융 뉴스를 조회하여 호재/악재 점수를 반환"""
        if self._is_cache_valid(symbol):
            return self.cache[symbol]

        if symbol.startswith("2") or symbol.startswith("1"):
            result = {"score": 0, "title": "", "summary": "ETF_SKIP"}
            self.cache[symbol] = result
            return result

        url = f"https://finance.naver.com/item/news_news.naver?code={symbol}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, headers={"User-Agent": "Mozilla/5.0"}, timeout=3
                ) as resp:
                    if resp.status != 200:
                        result = {"score": 0, "title": "", "summary": "HTTP_ERROR"}
                        self.cache[symbol] = result
                        return result
                    html = await resp.text(encoding="euc-kr", errors="replace")

            soup = BeautifulSoup(html, "html.parser")

            titles = []
            dates = []
            blocks = soup.select(".tit")
            date_blocks = soup.select(".date")

            for b in blocks[:10]:
                text = b.get_text().strip()
                text = re.sub(r"\[.*?\]", "", text)
                titles.append(text)

            for d in date_blocks[:10]:
                date_text = d.get_text().strip()
                try:
                    if "시간 전" in date_text or "분 전" in date_text:
                        dates.append(datetime.now())
                    elif "일 전" in date_text:
                        match = re.search(r"(\d+)일", date_text)
                        if match:
                            days = int(match.group(1))
                            dates.append(datetime.now() - timedelta(days=days))
                        else:
                            dates.append(datetime.now())
                    else:
                        dates.append(datetime.now())
                except Exception:
                    dates.append(datetime.now())

            if not titles:
                result = {"score": 0, "title": "", "summary": "NO_NEWS"}
                self.cache[symbol] = result
                return result

            total_score: float = 0.0
            hit_keywords = []
            news_count = 0

            for i, title in enumerate(titles):
                # 시간 필터: 3시간 이상 된 뉴스는 가중치 감소
                news_age: float = 0.0
                if i < len(dates):
                    age_delta = datetime.now() - dates[i]
                    news_age = age_delta.total_seconds() / 3600

                age_factor = 1.0
                if news_age > self.max_news_age_hours:
                    age_factor = 0.3  # 오래된 뉴스는 가중치 감소
                elif news_age > 1:
                    age_factor = 0.7

                # 호재 검색
                for kw, weight in self.KEYWORDS_GOOD.items():
                    if kw in title:
                        total_score += weight * age_factor
                        hit_keywords.append(f"+{kw}")
                        news_count += 1

                # 악재 검색
                for kw, weight in self.KEYWORDS_BAD.items():
                    if kw in title:
                        total_score += weight * age_factor
                        hit_keywords.append(f"{kw}")
                        news_count += 1

            # 평균 점수 (뉴스 수로 정규화하되 너무 낮지 않게)
            if news_count > 0:
                avg_score = total_score / min(news_count, 5)
                final_score = max(-100, min(100, int(avg_score)))
            else:
                final_score = 0

            summary = f"Score:{final_score} Hits:{len(hit_keywords)} News:{news_count}"
            top_title = titles[0] if titles else ""

            result = {"score": final_score, "title": top_title, "summary": summary}
            result["cached_at"] = datetime.now().timestamp()
            self.cache[symbol] = result
            return result

        except Exception as e:
            LOGGER.error(f"News fetch failed for {symbol}: {e}")
            result = {"score": 0, "title": "", "summary": "ERROR"}
            self.cache[symbol] = result
            return result

    async def get_us_sentiment_score(self, symbol: str) -> dict:
        """미국 주식 뉴스 분석 (Yahoo Finance)"""
        if self._is_cache_valid(f"US_{symbol}"):
            return self.cache[f"US_{symbol}"]

        url = f"https://finance.yahoo.com/quote/{symbol}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, headers={"User-Agent": "Mozilla/5.0"}, timeout=3
                ) as resp:
                    if resp.status != 200:
                        result = {"score": 0, "title": "", "summary": "HTTP_ERROR"}
                        return result
                    html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")

            titles = []
            news_blocks = soup.select("h3")
            for b in news_blocks[:10]:
                text = b.get_text().strip()
                if text and len(text) > 10:
                    titles.append(text)

            total_score: float = 0.0
            hit_keywords = []

            for title in titles:
                for kw, weight in self.KEYWORDS_US_GOOD.items():
                    if kw.lower() in title.lower():
                        total_score += weight
                        hit_keywords.append(f"+{kw}")

                for kw, weight in self.KEYWORDS_US_BAD.items():
                    if kw.lower() in title.lower():
                        total_score += weight
                        hit_keywords.append(f"{kw}")

            final_score = max(-100, min(100, total_score))
            summary = f"US Score:{final_score} Hits:{len(hit_keywords)}"
            top_title = titles[0] if titles else ""

            result = {"score": final_score, "title": top_title, "summary": summary}
            result["cached_at"] = datetime.now().timestamp()
            self.cache[f"US_{symbol}"] = result
            return result
        except Exception as e:
            LOGGER.error(f"US News fetch failed for {symbol}: {e}")
            result = {"score": 0, "title": "", "summary": "ERROR"}
            return result
