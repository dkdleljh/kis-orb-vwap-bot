#!/usr/bin/env python3
"""Enrich missing event context into a derived stream (immutable source).

This script never mutates the original append-only events stream.

Input:
- logs/events/YYYYMMDD/events.jsonl

Outputs:
- logs/events/YYYYMMDD/events.enriched.jsonl
- logs/events/YYYYMMDD/events.enrichment_patches.jsonl
- logs/events/YYYYMMDD/events.enrichment_manifest.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _now_iso_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _load_default_min_score() -> float | None:
    for name in ("config.kr.json", "config.json"):
        p = ROOT / name
        if not p.exists():
            continue
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
            v = (((cfg.get("trading") or {}).get("scoring") or {}).get("kr_scalp_entry_threshold"))
            return float(v) if v is not None else None
        except Exception:
            continue
    return None


def _context_or_none(payload: dict[str, Any]) -> dict[str, Any] | None:
    ctx = payload.get("context")
    if isinstance(ctx, dict) and ctx:
        return ctx
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (KST). default=today", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    kst = dt.timezone(dt.timedelta(hours=9))
    day = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(tz=kst).date()
    ymd = day.strftime("%Y%m%d")

    base_dir = ROOT / "logs" / "events" / ymd
    events_path = base_dir / "events.jsonl"
    enriched_path = base_dir / "events.enriched.jsonl"
    patch_path = base_dir / "events.enrichment_patches.jsonl"
    manifest_path = base_dir / "events.enrichment_manifest.json"

    if not events_path.exists():
        print(f"NO_EVENTS: {events_path}")
        return 0

    default_min_score = _load_default_min_score()
    lines = events_path.read_text(encoding="utf-8", errors="replace").splitlines()

    out_lines: list[str] = []
    patches: list[dict[str, Any]] = []
    parsed_lines = 0
    parse_errors = 0

    for line_no, raw in enumerate(lines, start=1):
        s = (raw or "").strip()
        if not s:
            continue

        try:
            ev = json.loads(s)
            parsed_lines += 1
        except Exception:
            parse_errors += 1
            out_lines.append(raw)
            continue

        typ = str(ev.get("type") or "")
        payload = dict(ev.get("payload") or {})

        patch_kind: str | None = None
        new_ctx: dict[str, Any] | None = None

        if typ == "RiskDecision":
            if _context_or_none(payload) is None:
                reason = str(payload.get("reason") or "")
                allowed = payload.get("allowed")
                base_ctx = {
                    "enriched": True,
                    "enriched_at": _now_iso_utc(),
                    "enriched_by": "backfill_event_context.py",
                    "reason": "missing_risk_context",
                }
                if allowed is False and reason == "qty=0":
                    base_ctx["reason"] = "missing_qty0_context"
                new_ctx = base_ctx
                patch_kind = str(base_ctx["reason"])

        elif typ == "Signal":
            if _context_or_none(payload) is None:
                new_ctx = {
                    "enriched": True,
                    "enriched_at": _now_iso_utc(),
                    "enriched_by": "backfill_event_context.py",
                    "min_score": default_min_score,
                    "reasons": ["missing_signal_context_enriched"],
                }
                patch_kind = "missing_signal_context"

        if new_ctx is not None:
            payload["context"] = new_ctx
            ev["payload"] = payload
            patches.append(
                {
                    "line_no": line_no,
                    "type": typ,
                    "symbol": ev.get("symbol"),
                    "patch_kind": patch_kind,
                    "patched_at": _now_iso_utc(),
                }
            )

        out_lines.append(json.dumps(ev, ensure_ascii=False))

    patch_counts: dict[str, int] = {}
    for p in patches:
        k = str(p.get("patch_kind") or "unknown")
        patch_counts[k] = int(patch_counts.get(k, 0)) + 1

    src_sha = hashlib.sha256(events_path.read_bytes()).hexdigest()
    manifest = {
        "date": day.isoformat(),
        "generated_at": _now_iso_utc(),
        "source_events": str(events_path),
        "source_sha256": src_sha,
        "enriched_events": str(enriched_path),
        "patch_file": str(patch_path),
        "line_count": len(lines),
        "parsed_lines": parsed_lines,
        "parse_errors": parse_errors,
        "patch_count": len(patches),
        "patch_counts": patch_counts,
        "dry_run": bool(args.dry_run),
    }

    print(f"events_path={events_path}")
    print(f"enriched_path={enriched_path}")
    print(f"patch_count={len(patches)}")

    if args.dry_run:
        return 0

    base_dir.mkdir(parents=True, exist_ok=True)
    enriched_path.write_text("\n".join(out_lines).rstrip() + "\n", encoding="utf-8")
    if patches:
        patch_path.write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in patches).rstrip() + "\n",
            encoding="utf-8",
        )
    else:
        patch_path.write_text("", encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"WROTE: {enriched_path}")
    print(f"WROTE: {patch_path}")
    print(f"WROTE: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
