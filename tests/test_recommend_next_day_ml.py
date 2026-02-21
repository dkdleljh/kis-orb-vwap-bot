import datetime as dt
import json
from pathlib import Path


def _write_events(path: Path) -> None:
    rows = [
        {"type": "Signal", "symbol": "005930", "payload": {"context": {"min_score": 60, "reasons": ["a"]}}},
        {"type": "RiskDecision", "symbol": "005930", "payload": {"allowed": False, "reason": "qty=0", "context": {}}},
        {"type": "Fill", "symbol": "005930", "payload": {"fee": 10.0, "broker_order_id": "OID1"}},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _write_report(path: Path, pnl: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"- 당일 손익(추정, KRW): **{pnl:,}원**\n", encoding="utf-8")


def test_extract_day_features_uses_prep_metrics_json(tmp_path, monkeypatch):
    import scripts.recommend_next_day as r

    monkeypatch.setattr(r, "ROOT", tmp_path)

    events_path = tmp_path / "logs" / "events" / "20260219" / "events.jsonl"
    _write_events(events_path)

    prep_path = tmp_path / "reports" / "next_day_prep_metrics_2026-02-19.json"
    prep_path.parent.mkdir(parents=True, exist_ok=True)
    prep_path.write_text(
        json.dumps(
            {
                "events": {"parse_error_rate": 0.02, "enriched_used": True},
                "quality": {
                    "qty0_total": 10,
                    "qty0_missing_context": 3,
                    "signal_total": 8,
                    "signal_with_min_score": 6,
                    "signal_with_reasons": 5,
                },
            }
        ),
        encoding="utf-8",
    )

    feats = r._extract_day_features(events_path, report_path=None, prep_metrics_path=prep_path)
    assert feats["prep_parse_error_rate"] == 0.02
    assert feats["prep_qty0_missing_ratio"] == 0.3
    assert feats["prep_signal_min_score_coverage"] == 0.75
    assert feats["prep_signal_reasons_coverage"] == 0.625
    assert feats["prep_enriched_used"] == 1.0


def test_discover_training_rows_builds_dataset_store(tmp_path, monkeypatch):
    import scripts.recommend_next_day as r

    monkeypatch.setattr(r, "ROOT", tmp_path)

    d = dt.date(2026, 2, 18)
    ymd = d.strftime("%Y%m%d")
    _write_events(tmp_path / "logs" / "events" / ymd / "events.jsonl")
    _write_report(tmp_path / "reports" / f"trade_report_{d.isoformat()}.md", 1200)

    x, y, days = r._discover_training_rows(dt.date(2026, 2, 20), lookback_days=5)
    assert len(x) == 1
    assert y == [1200.0]
    assert days == ["2026-02-18"]

    ds = tmp_path / "reports" / "datasets" / "next_day_features_2026-02-18.json"
    assert ds.exists()
    payload = json.loads(ds.read_text(encoding="utf-8"))
    assert payload["label_daily_pnl"] == 1200.0
    assert "features" in payload


def test_gating_blocks_parameter_changes_when_wf_quality_fails(tmp_path, monkeypatch):
    import scripts.recommend_next_day as r

    monkeypatch.setattr(r, "ROOT", tmp_path)

    cfg = {
        "trading": {
            "entry_budget_pct": 0.2,
            "cash_reserve_pct": 0.2,
            "max_new_entries_per_minute": 2,
            "symbol_cooldown_sec": 900,
            "atr_min_percent": 0.35,
            "scoring": {"kr_scalp_entry_threshold": 60},
        }
    }
    (tmp_path / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    for d, pnl in [(dt.date(2026, 2, 17), 1000), (dt.date(2026, 2, 18), -900), (dt.date(2026, 2, 19), 1200)]:
        _write_events(tmp_path / "logs" / "events" / d.strftime("%Y%m%d") / "events.jsonl")
        _write_report(tmp_path / "reports" / f"trade_report_{d.isoformat()}.md", pnl)

    target = dt.date(2026, 2, 20)
    _write_events(tmp_path / "logs" / "events" / target.strftime("%Y%m%d") / "events.jsonl")
    _write_report(tmp_path / "reports" / f"trade_report_{target.isoformat()}.md", 0)

    monkeypatch.setattr(
        "sys.argv",
        [
            "recommend_next_day.py",
            "--date",
            target.isoformat(),
            "--lookback-days",
            "10",
            "--min-train-rows",
            "2",
            "--walk-forward-k",
            "2",
            "--wf-min-evals",
            "1",
            "--wf-directional-acc-min",
            "1.0",
            "--wf-hit-rate-min",
            "1.1",
        ],
    )
    assert r.main() == 0

    out = tmp_path / "reports" / f"next_day_reco_{target.isoformat()}.json"
    data = json.loads(out.read_text(encoding="utf-8"))
    gate = data["inputs"]["model"]["ml_reco_gate"]
    assert gate["ok"] is False
    assert data["recommendations"][0]["key"] == "keep_defaults"


def test_parse_daily_pnl_prefers_prep_metrics_json(tmp_path):
    import scripts.recommend_next_day as r

    report = tmp_path / "reports" / "trade_report_2026-02-20.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("- 당일 손익(추정, KRW): **1,000원**\n", encoding="utf-8")

    prep = tmp_path / "reports" / "next_day_prep_metrics_2026-02-20.json"
    prep.write_text(
        json.dumps({"report": {"daily_pnl_est": 4321.0}}),
        encoding="utf-8",
    )

    got = r._parse_daily_pnl(report, prep_metrics_path=prep)
    assert got == 4321.0
