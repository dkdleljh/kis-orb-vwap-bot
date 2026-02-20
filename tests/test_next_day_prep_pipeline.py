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
