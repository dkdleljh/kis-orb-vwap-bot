"""LLM-based market analysis module.

Provides AI-powered market analysis using LLM APIs.
"""

import os
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class LLMMarketAnalyzer:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.enabled = bool(self.api_key)

    def analyze_market(self, market_data: dict) -> dict:
        """Analyze market conditions using LLM.

        Args:
            market_data: Dictionary containing market indicators

        Returns:
            Analysis result with sentiment and recommendations
        """
        if not self.enabled:
            return {
                "sentiment": "NEUTRAL",
                "confidence": 0.5,
                "recommendation": "hold",
                "reasoning": "LLM not configured",
            }

        prompt = self._build_prompt(market_data)

        try:
            response = self._call_llm(prompt)
            return self._parse_response(response)
        except Exception as e:
            logger.warning(f"LLM analysis failed: {e}")
            return {
                "sentiment": "NEUTRAL",
                "confidence": 0.5,
                "recommendation": "hold",
                "reasoning": f"Analysis error: {e}",
            }

    def _build_prompt(self, market_data: dict) -> str:
        indicators = market_data.get("indicators", {})

        prompt = f"""Analyze the current market conditions and provide a trading recommendation.

Current Market Data:
- RSI: {indicators.get("rsi", "N/A")}
- MACD Histogram: {indicators.get("macd_hist", "N/A")}
- Price vs MA20: {indicators.get("ma20", "N/A")}
- Volume Power: {indicators.get("volume_power", "N/A")}
- ATR %: {indicators.get("atr_percent", "N/A")}

Market Regime: {market_data.get("market_regime", "NEUTRAL")}

Provide a brief analysis with:
1. Sentiment (BULL/BEAR/NEUTRAL)
2. Confidence (0-1)
3. Recommendation (buy/sell/hold)
4. Brief reasoning
"""
        return prompt

    def _call_llm(self, prompt: str) -> str:
        try:
            from openai import OpenAI

            client = OpenAI(api_key=self.api_key)

            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "You are a professional stock market analyst.",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=200,
                temperature=0.3,
            )

            return response.choices[0].message.content
        except ImportError:
            raise RuntimeError("openai package not installed")

    def _parse_response(self, response: str) -> dict:
        lines = response.strip().split("\n")

        sentiment = "NEUTRAL"
        confidence = 0.5
        recommendation = "hold"
        reasoning = response

        for line in lines:
            line_lower = line.lower()
            if "sentiment" in line_lower:
                if "bull" in line_lower:
                    sentiment = "BULL"
                elif "bear" in line_lower:
                    sentiment = "BEAR"
            elif "recommendation" in line_lower or "recommend" in line_lower:
                if "buy" in line_lower:
                    recommendation = "buy"
                elif "sell" in line_lower:
                    recommendation = "sell"
            elif "confidence" in line_lower:
                try:
                    confidence = float(
                        "".join(filter(lambda x: x.isdigit() or x == ".", line))
                    )
                    confidence = max(0, min(1, confidence))
                except:
                    pass

        return {
            "sentiment": sentiment,
            "confidence": confidence,
            "recommendation": recommendation,
            "reasoning": reasoning,
        }


_llm_analyzer: Optional[LLMMarketAnalyzer] = None


def get_llm_analyzer() -> LLMMarketAnalyzer:
    global _llm_analyzer
    if _llm_analyzer is None:
        _llm_analyzer = LLMMarketAnalyzer()
    return _llm_analyzer
