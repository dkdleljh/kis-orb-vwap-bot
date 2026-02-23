#!/usr/bin/env python3
"""Verify next-day prep artifacts + event explainability quality.

Flow:
1) Auto-generate missing artifacts (trade report / next-day reco) unless disabled.
2) Evaluate event sanity + quality.
3) If context quality fails and auto-enrich is enabled, run one enrichment pass.
4) Re-check and print standardized summary.

Exit code:
- 0: PASS
- 2: FAIL
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PARSE_ERROR_RATE_MAX = 0.05


def _is_real_context(ctx: Any) -> bool:
    if not (isinstance(ctx, dict) and len(ctx) > 0):
        return False
    if ctx.get("context_missing") is True:
        return False
    if ctx.get("autofilled_at_emit") is True:
        return False
    return True


def _iter_events_with_stats(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    stats = {"line_count": 0, "parsed_count": 0, "parse_errors": 0}
    out: list[dict[str, Any]] = []

    if not path.exists():
        return out, stats

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            s = raw.strip()
            if not s:
                continue
            stats["line_count"] += 1
            try:
                ev = json.loads(s)
                if isinstance(ev, dict):
                    out.append(ev)
                    stats["parsed_count"] += 1
                else:
                    stats["parse_errors"] += 1
            except Exception:
                stats["parse_errors"] += 1

    return out, stats


def _run(cmd: list[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), check=False, capture_output=True, text=True)
        out = (proc.stdout or "").strip().splitlines() + (proc.stderr or "").strip().splitlines()
        return proc.returncode == 0, (out[-1] if out else "")
    except Exception as e:
        return False, str(e)


def _compute_event_quality(events: list[dict[str, Any]]) -> dict[str, int]:
    qty0_total = 0
    qty0_with_context = 0
    qty0_with_autofilled_context = 0

    signal_total = 0
    signal_with_any_context = 0
    signal_with_context = 0
    signal_with_autofilled_context = 0
    signal_with_min_score = 0
    signal_with_reasons = 0

    risk_total = 0
    risk_with_any_context = 0
    risk_with_context = 0
    risk_with_autofilled_context = 0

    for ev in events:
        typ = ev.get("type")
        payload = ev.get("payload") or {}

        if typ == "RiskDecision":
            risk_total += 1
            ctx = payload.get("context")
            if isinstance(ctx, dict) and len(ctx) > 0:
                risk_with_any_context += 1
                if _is_real_context(ctx):
                    risk_with_context += 1
                else:
                    risk_with_autofilled_context += 1

            if payload.get("allowed") is False and str(payload.get("reason") or "") == "qty=0":
                qty0_total += 1
                if _is_real_context(ctx):
                    qty0_with_context += 1
                elif isinstance(ctx, dict) and len(ctx) > 0:
                    qty0_with_autofilled_context += 1

        elif typ == "Signal":
            signal_total += 1
            ctx = payload.get("context")
            if isinstance(ctx, dict) and len(ctx) > 0:
                signal_with_any_context += 1
                if _is_real_context(ctx):
                    signal_with_context += 1
                    if ctx.get("min_score") is not None:
                        signal_with_min_score += 1
                    reasons = ctx.get("reasons")
                    if isinstance(reasons, list) and len(reasons) > 0:
                        signal_with_reasons += 1
                else:
                    signal_with_autofilled_context += 1

    return {
        "qty0_total": qty0_total,
        "qty0_with_context": qty0_with_context,
        "qty0_with_autofilled_context": qty0_with_autofilled_context,
        "qty0_missing_context": qty0_total - qty0_with_context,
        "risk_total": risk_total,
        "risk_with_any_context": risk_with_any_context,
        "risk_with_context": risk_with_context,
        "risk_with_autofilled_context": risk_with_autofilled_context,
        "risk_missing_context": risk_total - risk_with_context,
        "signal_total": signal_total,
        "signal_with_any_context": signal_with_any_context,
        "signal_with_context": signal_with_context,
        "signal_with_autofilled_context": signal_with_autofilled_context,
        "signal_missing_context": signal_total - signal_with_context,
        "signal_with_min_score": signal_with_min_score,
        "signal_with_reasons": signal_with_reasons,
    }


def _classify_failures(
    *,
    artifacts_ok: bool,
    sanity_ok: bool,
    qty0_ctx_ok: bool,
    signal_ctx_ok: bool,
    strict_enriched_ok: bool,
) -> list[str]:
    reasons: list[str] = []
    if not artifacts_ok:
        reasons.append("artifact_missing")
    if not sanity_ok:
        reasons.append("events_sanity_failed")
    if not qty0_ctx_ok:
        reasons.append("qty0_context_missing")
    if not signal_ctx_ok:
        reasons.append("signal_context_incomplete")
    if not strict_enriched_ok:
        reasons.append("strict_requires_enriched_no")
    return reasons


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (KST). default=today", default=None)
    ap.add_argument("--events-path", default=None, help="override events path")
    ap.add_argument("--no-autogen", action="store_true", help="do not auto-generate missing report/reco")
    ap.add_argument("--auto-enrich", action="store_true", help="run one enrichment pass when context checks fail")
    ap.add_argument("--auto-backfill", action="store_true", help="deprecated alias of --auto-enrich")
    ap.add_argument(
        "--strict-signal-context",
        action="store_true",
        help="require signal min_score/reasons coverage ratios instead of >=1 absolute checks",
    )
    ap.add_argument(
        "--strict-require-enriched-no",
        action="store_true",
        help="with --strict-signal-context, also require enriched_used==NO",
    )
    ap.add_argument("--signal-min-score-ratio-threshold", type=float, default=0.30)
    ap.add_argument("--signal-reasons-ratio-threshold", type=float, default=0.10)
    args = ap.parse_args()

    kst = dt.timezone(dt.timedelta(hours=9))
    day = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(tz=kst).date()

    day_s = day.isoformat()
    ymd = day.strftime("%Y%m%d")

    # If it's a weekend (KST), markets are closed and the event stream may be empty.
    # In that case, treat verification as PASS to avoid noisy failures.
    if day.weekday() >= 5:
        print(f"date={day_s}")
        print("weekend=YES (market closed)")
        print("KIS NextDayPrep Verification: PASS (weekend)")
        return 0

    base_events_path = Path(args.events_path) if args.events_path else (ROOT / "logs" / "events" / ymd / "events.jsonl")
    enriched_events_path = base_events_path.with_name("events.enriched.jsonl")

    report_path = ROOT / "reports" / f"trade_report_{day_s}.md"
    reco_json = ROOT / "reports" / f"next_day_reco_{day_s}.json"
    reco_md = ROOT / "reports" / f"next_day_reco_{day_s}.md"

    autogen_done: list[str] = []
    if not args.no_autogen and not report_path.exists():
        ok, _ = _run([sys.executable, "scripts/daily_trade_report.py", "--date", day_s])
        if ok:
            autogen_done.append("trade_report")

    if not args.no_autogen and (not reco_json.exists() or not reco_md.exists()):
        ok, _ = _run([sys.executable, "scripts/recommend_next_day.py", "--date", day_s])
        if ok:
            autogen_done.append("next_day_reco")

    artifacts = {
        "trade_report_exists": report_path.exists(),
        "next_day_reco_json_exists": reco_json.exists(),
        "next_day_reco_md_exists": reco_md.exists(),
    }
    artifacts_ok = all(artifacts.values())

    enrich_enabled = bool(args.auto_enrich or args.auto_backfill)
    enrich_attempted = False
    enrich_done = False
    enrich_last = ""
    used_events_path = base_events_path

    events, parse_stats = _iter_events_with_stats(used_events_path)
    q = _compute_event_quality(events)

    line_count = int(parse_stats["line_count"])
    parse_errors = int(parse_stats["parse_errors"])
    parse_error_rate = (parse_errors / line_count) if line_count else 1.0

    sanity = {
        "events_exists": used_events_path.exists(),
        "events_line_count_gt0": line_count > 0,
        "events_parse_error_rate_ok": parse_error_rate <= PARSE_ERROR_RATE_MAX,
    }
    sanity_ok = all(sanity.values())

    qty0_ctx_ok = (q["qty0_total"] == 0) or (q["qty0_with_context"] == q["qty0_total"])
    signal_total = max(0, int(q.get("signal_total", 0)))
    min_score_ratio = (
        (float(q.get("signal_with_min_score", 0)) / float(signal_total))
        if signal_total > 0
        else 0.0
    )
    reasons_ratio = (
        (float(q.get("signal_with_reasons", 0)) / float(signal_total))
        if signal_total > 0
        else 0.0
    )
    if args.strict_signal_context:
        signal_ctx_ok = (
            signal_total > 0
            and min_score_ratio >= float(args.signal_min_score_ratio_threshold)
            and reasons_ratio >= float(args.signal_reasons_ratio_threshold)
        )
    else:
        signal_ctx_ok = q["signal_with_min_score"] >= 1 and q["signal_with_reasons"] >= 1

    context_fail = (not qty0_ctx_ok) or (not signal_ctx_ok)
    if enrich_enabled and context_fail and base_events_path.exists():
        enrich_attempted = True
        enrich_ok, enrich_last = _run([sys.executable, "scripts/backfill_event_context.py", "--date", day_s])
        enrich_done = enrich_ok and enriched_events_path.exists()
        if enrich_done:
            used_events_path = enriched_events_path
            events, parse_stats = _iter_events_with_stats(used_events_path)
            q = _compute_event_quality(events)

            line_count = int(parse_stats["line_count"])
            parse_errors = int(parse_stats["parse_errors"])
            parse_error_rate = (parse_errors / line_count) if line_count else 1.0
            sanity = {
                "events_exists": used_events_path.exists(),
                "events_line_count_gt0": line_count > 0,
                "events_parse_error_rate_ok": parse_error_rate <= PARSE_ERROR_RATE_MAX,
            }
            sanity_ok = all(sanity.values())
            qty0_ctx_ok = (q["qty0_total"] == 0) or (q["qty0_with_context"] == q["qty0_total"])
            signal_total = max(0, int(q.get("signal_total", 0)))
            min_score_ratio = (
                (float(q.get("signal_with_min_score", 0)) / float(signal_total))
                if signal_total > 0
                else 0.0
            )
            reasons_ratio = (
                (float(q.get("signal_with_reasons", 0)) / float(signal_total))
                if signal_total > 0
                else 0.0
            )
            if args.strict_signal_context:
                signal_ctx_ok = (
                    signal_total > 0
                    and min_score_ratio >= float(args.signal_min_score_ratio_threshold)
                    and reasons_ratio >= float(args.signal_reasons_ratio_threshold)
                )
            else:
                signal_ctx_ok = q["signal_with_min_score"] >= 1 and q["signal_with_reasons"] >= 1

    strict_enriched_ok = True
    if args.strict_signal_context and args.strict_require_enriched_no:
        strict_enriched_ok = used_events_path != enriched_events_path

    ok = artifacts_ok and sanity_ok and qty0_ctx_ok and signal_ctx_ok and strict_enriched_ok
    pass_via_enrichment = ok and (used_events_path == enriched_events_path)

    reasons = _classify_failures(
        artifacts_ok=artifacts_ok,
        sanity_ok=sanity_ok,
        qty0_ctx_ok=qty0_ctx_ok,
        signal_ctx_ok=signal_ctx_ok,
        strict_enriched_ok=strict_enriched_ok,
    )
    if enrich_attempted and not enrich_done:
        reasons.append("enrichment_failed")

    metrics = {
        "date": day_s,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "artifacts": artifacts,
        "events": {
            "source_path": str(base_events_path),
            "used_path": str(used_events_path),
            "enriched_used": used_events_path == enriched_events_path,
            "line_count": line_count,
            "parsed_count": int(parse_stats["parsed_count"]),
            "parse_errors": parse_errors,
            "parse_error_rate": round(parse_error_rate, 6),
            "parse_error_rate_max": PARSE_ERROR_RATE_MAX,
        },
        "quality": q,
        "signal_context_policy": {
            "strict_enabled": bool(args.strict_signal_context),
            "strict_require_enriched_no": bool(args.strict_require_enriched_no),
            "min_score_ratio": round(min_score_ratio, 6),
            "reasons_ratio": round(reasons_ratio, 6),
            "min_score_ratio_threshold": float(args.signal_min_score_ratio_threshold),
            "reasons_ratio_threshold": float(args.signal_reasons_ratio_threshold),
        },
        "autogen": autogen_done,
        "enrichment": {
            "enabled": enrich_enabled,
            "attempted": enrich_attempted,
            "done": enrich_done,
            "last": enrich_last,
            "pass_via_enrichment": pass_via_enrichment,
        },
        "failure_reasons": reasons,
        "strict_enriched_ok": strict_enriched_ok,
        "pass": ok,
    }

    metrics_path = ROOT / "reports" / f"next_day_prep_metrics_{day_s}.json"
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"date={day_s}")
    print(f"autogen={','.join(autogen_done) if autogen_done else 'none'}")
    print(f"auto_enrich={'YES' if enrich_enabled else 'NO'}")
    if enrich_enabled:
        print(f"enrich_attempted={'YES' if enrich_attempted else 'NO'}")
        print(f"enrich_done={'YES' if enrich_done else 'NO'}")
    print(f"events_used={used_events_path}")
    print(f"failure_reasons={','.join(reasons) if reasons else 'none'}")

    print(f"trade_report_exists={'PASS' if artifacts['trade_report_exists'] else 'FAIL'} :: {report_path}")
    print(f"next_day_reco_json_exists={'PASS' if artifacts['next_day_reco_json_exists'] else 'FAIL'} :: {reco_json}")
    print(f"next_day_reco_md_exists={'PASS' if artifacts['next_day_reco_md_exists'] else 'FAIL'} :: {reco_md}")
    print(f"events_exists={'PASS' if sanity['events_exists'] else 'FAIL'} :: {used_events_path}")
    print(f"events_line_count_gt0={'PASS' if sanity['events_line_count_gt0'] else 'FAIL'} :: line_count={line_count}")
    print(
        f"events_parse_error_rate_ok={'PASS' if sanity['events_parse_error_rate_ok'] else 'FAIL'}"
        f" :: parse_errors={parse_errors}, line_count={line_count}, parse_error_rate={parse_error_rate:.6f}"
    )
    print(f"qty0_has_context={'PASS' if qty0_ctx_ok else 'FAIL'} :: qty0_context={q['qty0_with_context']}/{q['qty0_total']}")
    print(
        "signal_context_min_score_and_reasons="
        + ("PASS" if signal_ctx_ok else "FAIL")
        + f" :: min_score={q['signal_with_min_score']}, reasons={q['signal_with_reasons']}, "
        + f"min_score_ratio={min_score_ratio:.3f}, reasons_ratio={reasons_ratio:.3f}, "
        + f"strict={'YES' if args.strict_signal_context else 'NO'}"
    )
    if args.strict_signal_context and args.strict_require_enriched_no:
        print(
            f"strict_require_enriched_no={'PASS' if strict_enriched_ok else 'FAIL'}"
            + f" :: enriched_used={'YES' if used_events_path == enriched_events_path else 'NO'}"
        )

    print(
        "KIS NextDayPrep Verification: "
        + ("PASS" if ok else "FAIL")
        + f" | date={day_s}"
        + f" | qty0_context {q['qty0_with_context']}/{q['qty0_total']}"
        + f" | signal_ctx min_score {q['signal_with_min_score']} & reasons {q['signal_with_reasons']}"
        + f" | enriched_used {'YES' if used_events_path == enriched_events_path else 'NO'}"
        + f" | pass_via_enrichment {'YES' if pass_via_enrichment else 'NO'}"
        + f" | failure_reason {','.join(reasons) if reasons else 'none'}"
    )

    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
