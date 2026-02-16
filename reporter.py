import os
import re
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional


class KisReporter:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.log_dir = os.path.join(base_dir, "logs")
        self.report_dir = os.path.join(base_dir, "reports")
        os.makedirs(self.report_dir, exist_ok=True)

    def parse_trade_from_log(self, line: str) -> Optional[Dict[str, Any]]:
        """로그 라인에서 거래 정보 파싱"""
        trade = {}

        # 시간 추출 (형식: 2026-02-14 09:30:25)
        time_match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if time_match:
            trade["time"] = time_match.group(1)

        # PnL 추출
        pnl_match = re.search(r"pnl=([-+]?[\d.]+)", line)
        if pnl_match:
            trade["pnl"] = float(pnl_match.group(1))

        # 심볼 추출
        symbol_match = re.search(r"symbol=(\w+)", line)
        if symbol_match:
            trade["symbol"] = symbol_match.group(1)

        # 매수/매도 추출
        if "entry" in line.lower():
            trade["type"] = "ENTRY"
        elif "exit" in line.lower():
            trade["type"] = "EXIT"

        # 수량 추출
        qty_match = re.search(r"qty=(\d+)", line)
        if qty_match:
            trade["qty"] = int(qty_match.group(1))

        # 청산 이유 추출
        reason_match = re.search(r"reason=(\w+)", line)
        if reason_match:
            trade["reason"] = reason_match.group(1)

        return trade if trade else None

    def analyze_trades(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """거래 분석"""
        if not trades:
            return {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "best_trade": 0.0,
                "worst_trade": 0.0,
                "profit_factor": 0.0,
            }

        wins = [t["pnl"] for t in trades if t.get("pnl", 0) > 0]
        losses = [t["pnl"] for t in trades if t.get("pnl", 0) <= 0]

        total_pnl = sum(t.get("pnl", 0) for t in trades)

        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(trades) * 100 if trades else 0,
            "total_pnl": total_pnl,
            "avg_win": sum(wins) / len(wins) if wins else 0,
            "avg_loss": sum(losses) / len(losses) if losses else 0,
            "best_trade": max(t.get("pnl", 0) for t in trades),
            "worst_trade": min(t.get("pnl", 0) for t in trades),
            "profit_factor": abs(sum(wins) / sum(losses))
            if losses and sum(losses) != 0
            else 0,
        }

    def generate_daily_report(self, target_date: Optional[str] = None) -> tuple:
        """일일 리포트 생성"""
        if target_date:
            today = target_date
        else:
            today = datetime.now().strftime("%Y-%m-%d")

        date_obj = datetime.strptime(today, "%Y-%m-%d")
        log_date = date_obj.strftime("%Y%m%d")

        # 로그 파일 분석
        log_file = os.path.join(self.log_dir, f"bot_{log_date}.log")

        trades = []

        if os.path.exists(log_file):
            with open(log_file, "r", encoding="utf-8") as f:
                for line in f:
                    # 청산 파싱
                    if "exit done" in line:
                        trade = self.parse_trade_from_log(line)
                        if trade:
                            trades.append(trade)

        # 분석
        analysis = self.analyze_trades(trades)

        # 리포트 생성
        report_path = os.path.join(self.report_dir, f"KIS_Report_{today}.md")

        with open(report_path, "w", encoding="utf-8") as f:
            f.write("# 📈 KIS Trading Report\n")
            f.write(f"## {today}\n\n")

            # 요약
            f.write("## 📊 Summary\n")
            f.write("| Metric | Value |\n")
            f.write("| --- | --- |\n")
            f.write(f"| Total Trades | {analysis['total_trades']} |\n")
            f.write(f"| Wins | {analysis['wins']} |\n")
            f.write(f"| Losses | {analysis['losses']} |\n")
            f.write(f"| Win Rate | {analysis['win_rate']:.1f}% |\n")
            f.write(f"| Total PnL | {analysis['total_pnl'] * 100:.2f}% |\n")
            f.write(f"| Avg Win | {analysis['avg_win'] * 100:.2f}% |\n")
            f.write(f"| Avg Loss | {analysis['avg_loss'] * 100:.2f}% |\n")
            f.write(f"| Best Trade | {analysis['best_trade'] * 100:.2f}% |\n")
            f.write(f"| Worst Trade | {analysis['worst_trade'] * 100:.2f}% |\n")
            f.write(f"| Profit Factor | {analysis['profit_factor']:.2f} |\n\n")

            # 거래 내역
            f.write("## 📝 Trade History\n")
            if trades:
                f.write("| Time | Symbol | PnL (%) | Reason |\n")
                f.write("| --- | --- | --- | --- |\n")
                for t in trades:
                    time = t.get("time", "N/A")
                    symbol = t.get("symbol", "N/A")
                    pnl = t.get("pnl", 0) * 100
                    reason = t.get("reason", "N/A")
                    icon = "🟢" if pnl > 0 else "🔴"
                    f.write(f"| {time} | {symbol} | {icon} {pnl:+.2f}% | {reason} |\n")
            else:
                f.write("No trades executed.\n")

        return report_path, analysis

    def generate_weekly_report(self, weeks_ago: int = 0) -> tuple:
        """주간 리포트 생성"""
        end_date = datetime.now() - timedelta(weeks=weeks_ago * 7)
        start_date = end_date - timedelta(days=7)

        all_trades = []

        for i in range(7):
            date = start_date + timedelta(days=i)
            date_str = date.strftime("%Y-%m-%d")
            try:
                _, analysis = self.generate_daily_report(date_str)
                all_trades.append(
                    {
                        "date": date_str,
                        "trades": analysis["total_trades"],
                        "pnl": analysis["total_pnl"],
                    }
                )
            except Exception:
                pass

        # 주간 리포트
        week_start = start_date.strftime("%Y-%m-%d")
        week_end = end_date.strftime("%Y-%m-%d")
        report_path = os.path.join(self.report_dir, f"KIS_Weekly_{week_start}.md")

        total_pnl = sum(d["pnl"] for d in all_trades)
        total_trades = sum(d["trades"] for d in all_trades)

        with open(report_path, "w", encoding="utf-8") as f:
            f.write("# 📈 KIS Weekly Report\n")
            f.write(f"## {week_start} ~ {week_end}\n\n")
            f.write("## 📊 Weekly Summary\n")
            f.write("| Metric | Value |\n")
            f.write("| --- | --- |\n")
            f.write(f"| Total Trades | {total_trades} |\n")
            f.write(f"| Total PnL | {total_pnl * 100:.2f}% |\n\n")

            f.write("## 📅 Daily Breakdown\n")
            f.write("| Date | Trades | PnL |\n")
            f.write("| --- | --- | --- |\n")
            for d in all_trades:
                f.write(f"| {d['date']} | {d['trades']} | {d['pnl'] * 100:+.2f}% |\n")

        return report_path, {"total_trades": total_trades, "total_pnl": total_pnl}


# 호환성 유지용 함수
def generate_daily_report(base_dir: str) -> str:
    """일일 리포트 생성 (하위 호환)"""
    reporter = KisReporter(base_dir)
    path, _ = reporter.generate_daily_report()
    return path
