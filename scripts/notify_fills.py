import json
import os
import sys
from datetime import datetime

LOG_FILE = "logs/fill_alerts.jsonl"

def format_currency(value):
    return f"{int(value):,}"

def main():
    if not os.path.exists(LOG_FILE):
        print("OK (no fills log)")
        return

    # 파일 읽기 및 비우기 (Atomic하게 처리하려면 rename 후 읽는 게 좋음)
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        # 파일 내용 지우기
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            pass
            
    except Exception:
        return

    if not lines:
        print("OK (no new fills)")
        return

    messages = []
    for line in lines:
        try:
            data = json.loads(line)
            ts = datetime.fromisoformat(data["ts"]).strftime("%H:%M:%S")
            symbol = data["symbol"]
            qty = data["qty"]
            price = format_currency(data["price"])
            
            if data["type"] == "BUY":
                reason = data["reason"]
                if isinstance(reason, list):
                    reason = ", ".join(reason)
                msg = (
                    f"🚀 **[매수 체결] {symbol}**\n"
                    f"⏰ {ts}\n"
                    f"📦 {qty}주 @ {price}원\n"
                    f"💡 이유: {reason}"
                )
            elif data["type"] == "SELL":
                pnl_pct = data["pnl"] * 100
                revenue = format_currency(data["revenue"])
                emoji = "💰" if data["pnl"] > 0 else "💧"
                
                msg = (
                    f"{emoji} **[매도 체결] {symbol}**\n"
                    f"⏰ {ts}\n"
                    f"📦 {qty}주 @ {price}원\n"
                    f"📊 수익률: **{pnl_pct:+.2f}%**\n"
                    f"💵 손익금: **{revenue}원**\n"
                    f"📝 사유: {data['reason']}"
                )
            else:
                continue
                
            messages.append(msg)
        except Exception:
            continue

    if messages:
        print("\n---\n".join(messages))

if __name__ == "__main__":
    # 작업 디렉토리 변경 (상대 경로 문제 방지)
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    main()
