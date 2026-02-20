import json
import datetime as dt
from pathlib import Path


def test_events_always_emit_non_empty_context():
    from core.events import RiskDecision, Signal

    s = Signal(symbol="005930", side="BUY", context=None).to_event()
    r = RiskDecision(symbol="005930", allowed=False, reason="qty=0", context=None).to_event()

    sctx = (s.payload or {}).get("context")
    rctx = (r.payload or {}).get("context")

    assert isinstance(sctx, dict) and len(sctx) > 0
    assert isinstance(rctx, dict) and len(rctx) > 0


def test_backfill_enrichment_is_immutable(tmp_path, monkeypatch):
    import scripts.backfill_event_context as bec

    day = dt.date(2026, 2, 19)
    ymd = day.strftime("%Y%m%d")

    root = tmp_path
    events_dir = root / "logs" / "events" / ymd
    events_dir.mkdir(parents=True)
    events_path = events_dir / "events.jsonl"

    rows = [
        {
            "ts": "2026-02-19T01:00:00Z",
            "type": "RiskDecision",
            "symbol": "005930",
            "payload": {"allowed": False, "reason": "qty=0"},
        },
        {
            "ts": "2026-02-19T01:00:01Z",
            "type": "Signal",
            "symbol": "005930",
            "payload": {"side": "BUY", "strength": 1.0},
        },
    ]
    events_path.write_text("\n".join(json.dumps(x) for x in rows) + "\n", encoding="utf-8")

    (root / "config.json").write_text(
        json.dumps({"trading": {"scoring": {"kr_scalp_entry_threshold": 58}}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(bec, "ROOT", Path(root))
    monkeypatch.setattr("sys.argv", ["backfill_event_context.py", "--date", day.isoformat()])

    rc = bec.main()
    assert rc == 0

    enriched = events_dir / "events.enriched.jsonl"
    patches = events_dir / "events.enrichment_patches.jsonl"
    manifest = events_dir / "events.enrichment_manifest.json"

    assert enriched.exists()
    assert patches.exists()
    assert manifest.exists()

    # original immutable
    original = events_path.read_text(encoding="utf-8")
    assert "events.enriched" not in original

    enriched_rows = [json.loads(x) for x in enriched.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert isinstance(enriched_rows[0]["payload"].get("context"), dict)
    assert isinstance(enriched_rows[1]["payload"].get("context"), dict)


def test_verify_strict_require_enriched_no_flag(tmp_path, monkeypatch):
    import scripts.verify_next_day_prep as vnp

    day = dt.date(2026, 2, 20)
    ymd = day.strftime("%Y%m%d")
    day_s = day.isoformat()

    root = tmp_path
    events_dir = root / "logs" / "events" / ymd
    events_dir.mkdir(parents=True)
    events_path = events_dir / "events.jsonl"
    enriched_path = events_dir / "events.enriched.jsonl"

    # base: strict signal-context fails (min_score/reasons missing)
    base_rows = [
        {
            "type": "Signal",
            "symbol": "005930",
            "payload": {"side": "BUY", "context": {}},
        }
    ]
    events_path.write_text("\n".join(json.dumps(x) for x in base_rows) + "\n", encoding="utf-8")

    # enriched: strict signal-context passes
    enriched_rows = [
        {
            "type": "Signal",
            "symbol": "005930",
            "payload": {"side": "BUY", "context": {"min_score": 60, "reasons": ["x"]}},
        }
    ]
    enriched_path.write_text(
        "\n".join(json.dumps(x) for x in enriched_rows) + "\n",
        encoding="utf-8",
    )

    reports = root / "reports"
    reports.mkdir(parents=True)
    (reports / f"trade_report_{day_s}.md").write_text("ok\n", encoding="utf-8")
    (reports / f"next_day_reco_{day_s}.json").write_text("{}\n", encoding="utf-8")
    (reports / f"next_day_reco_{day_s}.md").write_text("ok\n", encoding="utf-8")

    monkeypatch.setattr(vnp, "ROOT", Path(root))
    monkeypatch.setattr(vnp, "_run", lambda cmd: (True, "ok"))
    monkeypatch.setattr(
        "sys.argv",
        [
            "verify_next_day_prep.py",
            "--date",
            day_s,
            "--auto-enrich",
            "--strict-signal-context",
            "--strict-require-enriched-no",
        ],
    )

    rc = vnp.main()
    assert rc == 2
    metrics = json.loads((reports / f"next_day_prep_metrics_{day_s}.json").read_text(encoding="utf-8"))
    assert metrics["events"]["enriched_used"] is True
    assert "strict_requires_enriched_no" in metrics["failure_reasons"]


def test_recommend_next_day_report_features_and_walk_forward(tmp_path):
    from scripts.recommend_next_day import _extract_trade_report_features, _walk_forward_validate

    report = tmp_path / "trade_report.md"
    report.write_text(
        "\n".join(
            [
                "## 실행 품질(슬리피지) 요약",
                "- KR slippage: count=11, avg=-1.6364, best=-10.0000, worst=0.0000",
                "## 합계",
                "- 당일 손익(추정, KRW): **-2,070원**",
                "### A",
                "- 실현손익(추정, FIFO): 100원",
                "### B",
                "- 실현손익(추정, FIFO): -50원",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    feats = _extract_trade_report_features(report)
    assert feats["report_realized_pnl_est"] == -2070.0
    assert feats["report_slippage_count"] == 11.0
    assert feats["report_slippage_avg"] == -1.6364
    assert feats["report_win_count"] == 1.0
    assert feats["report_loss_count"] == 1.0

    x = [[1.0], [2.0], [3.0], [4.0], [5.0]]
    y = [100.0, 200.0, 300.0, 400.0, 500.0]
    days = ["2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19"]
    wf = _walk_forward_validate(x, y, days, min_train_rows=2, eval_last_k=2)
    assert wf["n_eval"] == 2
    assert wf["mae"] is not None
    assert wf["directional_accuracy"] is not None
