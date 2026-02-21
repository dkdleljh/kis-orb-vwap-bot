#!/usr/bin/env python3
"""Train and infer next-day recommendations from historical events/reports."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

SAFE_RANGES: dict[str, tuple[float, float]] = {
    "trading.cash_reserve_pct": (0.10, 0.35),
    "trading.entry_budget_pct": (0.05, 0.35),
    "trading.scoring.kr_scalp_entry_threshold": (55.0, 85.0),
    "trading.symbol_cooldown_sec": (60.0, 3600.0),
    "trading.atr_min_percent": (0.10, 2.00),
}

FEATURE_NAMES = [
    "risk_blocked",
    "risk_allowed",
    "qty0_blocks",
    "qty0_missing_context",
    "signal_total",
    "signal_with_min_score",
    "signal_with_reasons",
    "fill_count",
    "fee_total",
    "fee_zero_live",
    "cooldown_blocks",
    "entry_rate_limit_blocks",
    "report_realized_pnl_est",
    "report_slippage_count",
    "report_slippage_avg",
    "report_slippage_worst",
    "report_avg_hold_sec",
    "report_win_count",
    "report_loss_count",
    "prep_parse_error_rate",
    "prep_qty0_missing_ratio",
    "prep_signal_min_score_coverage",
    "prep_signal_reasons_coverage",
    "prep_enriched_used",
]


def _iter_events(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                d = json.loads(s)
                if isinstance(d, dict):
                    yield d
            except Exception:
                continue


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _dataset_dir() -> Path:
    out = ROOT / "reports" / "datasets"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _dataset_path(day: dt.date) -> Path:
    return _dataset_dir() / f"next_day_features_{day.isoformat()}.json"


def _store_dataset_row(
    *,
    day: dt.date,
    features: dict[str, float],
    label: float | None,
    events_path: Path,
    report_path: Path | None,
    prep_metrics_path: Path | None,
) -> Path:
    payload = {
        "date": day.isoformat(),
        "features": {k: float(features.get(k, 0.0) or 0.0) for k in FEATURE_NAMES},
        "label_daily_pnl": (None if label is None else float(label)),
        "artifacts": {
            "events_path": str(events_path),
            "report_path": str(report_path) if report_path else "",
            "prep_metrics_path": str(prep_metrics_path) if prep_metrics_path else "",
        },
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    out = _dataset_path(day)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _conf(level: str, score: float) -> dict[str, Any]:
    return {"level": level, "score": round(float(score), 2)}


def _load_current_cfg() -> dict[str, Any]:
    for name in ("config.kr.json", "config.json"):
        p = ROOT / name
        if not p.exists():
            continue
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
            cfg["_config_path"] = str(p)
            cfg["_config_hash"] = hashlib.sha256(p.read_bytes()).hexdigest()
            return cfg
        except Exception:
            continue
    return {"_config_path": "", "_config_hash": ""}


def _extract_trade_report_features(report_path: Path) -> dict[str, float]:
    out = {
        "report_realized_pnl_est": 0.0,
        "report_slippage_count": 0.0,
        "report_slippage_avg": 0.0,
        "report_slippage_worst": 0.0,
        "report_avg_hold_sec": 0.0,
        "report_win_count": 0.0,
        "report_loss_count": 0.0,
    }
    if not report_path.exists():
        return out
    try:
        txt = report_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return out

    # 당일 손익(추정, KRW): **-2,070원**
    m_pnl = re.search(r"당일 손익\(추정, KRW\): \*\*([\-0-9,]+)원\*\*", txt)
    if m_pnl:
        try:
            out["report_realized_pnl_est"] = float(m_pnl.group(1).replace(",", ""))
        except Exception:
            pass

    # KR slippage: count=11, avg=-1.6364, best=-10.0000, worst=0.0000
    m_slip = re.search(
        r"KR slippage:\s*count=([0-9]+),\s*avg=([\-0-9.]+),\s*best=([\-0-9.]+),\s*worst=([\-0-9.]+)",
        txt,
    )
    if m_slip:
        try:
            out["report_slippage_count"] = float(m_slip.group(1))
            out["report_slippage_avg"] = float(m_slip.group(2))
            out["report_slippage_worst"] = float(m_slip.group(4))
        except Exception:
            pass

    # Optional patterns when report template includes explicit holding-time summary.
    m_hold = re.search(r"(?:avg[_ ]hold[_ ]sec|평균\s*보유\s*초)\s*[:=]\s*([0-9.]+)", txt, flags=re.IGNORECASE)
    if m_hold:
        try:
            out["report_avg_hold_sec"] = float(m_hold.group(1))
        except Exception:
            pass

    # Best-effort win/loss from per-symbol realized lines.
    # Example: - 실현손익(추정, FIFO): 804원
    wins = 0
    losses = 0
    for ms in re.finditer(r"실현손익\(추정,\s*FIFO\):\s*([\-0-9,]+)원", txt):
        try:
            v = float(ms.group(1).replace(",", ""))
            if v > 0:
                wins += 1
            elif v < 0:
                losses += 1
        except Exception:
            continue
    out["report_win_count"] = float(wins)
    out["report_loss_count"] = float(losses)
    return out


def _extract_report_features_from_metrics(metrics_path: Path | None) -> dict[str, float]:
    out = {
        "report_realized_pnl_est": 0.0,
        "report_slippage_count": 0.0,
        "report_slippage_avg": 0.0,
        "report_slippage_worst": 0.0,
        "report_avg_hold_sec": 0.0,
        "report_win_count": 0.0,
        "report_loss_count": 0.0,
    }
    if metrics_path is None:
        return out
    data = _read_json(metrics_path)
    if not data:
        return out

    report = data.get("report") if isinstance(data.get("report"), dict) else {}
    quality = data.get("quality") if isinstance(data.get("quality"), dict) else {}

    pnl = report.get("daily_pnl_est") if report else None
    if isinstance(pnl, (int, float)):
        out["report_realized_pnl_est"] = float(pnl)

    sl = report.get("slippage") if isinstance(report.get("slippage"), dict) else {}
    if isinstance(sl.get("count"), (int, float)):
        out["report_slippage_count"] = float(sl.get("count") or 0.0)
    if isinstance(sl.get("avg"), (int, float)):
        out["report_slippage_avg"] = float(sl.get("avg") or 0.0)
    if isinstance(sl.get("worst"), (int, float)):
        out["report_slippage_worst"] = float(sl.get("worst") or 0.0)

    if isinstance(report.get("avg_hold_sec"), (int, float)):
        out["report_avg_hold_sec"] = float(report.get("avg_hold_sec") or 0.0)

    if isinstance(quality.get("win_count"), (int, float)):
        out["report_win_count"] = float(quality.get("win_count") or 0.0)
    if isinstance(quality.get("loss_count"), (int, float)):
        out["report_loss_count"] = float(quality.get("loss_count") or 0.0)

    return out


def _extract_prep_metrics_features(metrics_path: Path | None) -> dict[str, float]:
    out = {
        "prep_parse_error_rate": 0.0,
        "prep_qty0_missing_ratio": 0.0,
        "prep_signal_min_score_coverage": 0.0,
        "prep_signal_reasons_coverage": 0.0,
        "prep_enriched_used": 0.0,
    }
    if metrics_path is None:
        return out
    data = _read_json(metrics_path)
    if not data:
        return out

    ev = data.get("events") if isinstance(data.get("events"), dict) else {}
    q = data.get("quality") if isinstance(data.get("quality"), dict) else {}
    out["prep_parse_error_rate"] = float(ev.get("parse_error_rate", 0.0) or 0.0)
    out["prep_enriched_used"] = 1.0 if bool(ev.get("enriched_used")) else 0.0

    qty0_total = float(q.get("qty0_total", 0.0) or 0.0)
    qty0_missing = float(q.get("qty0_missing_context", 0.0) or 0.0)
    signal_total = float(q.get("signal_total", 0.0) or 0.0)
    signal_with_min_score = float(q.get("signal_with_min_score", 0.0) or 0.0)
    signal_with_reasons = float(q.get("signal_with_reasons", 0.0) or 0.0)

    if qty0_total > 0:
        out["prep_qty0_missing_ratio"] = float(qty0_missing / qty0_total)
    if signal_total > 0:
        out["prep_signal_min_score_coverage"] = float(signal_with_min_score / signal_total)
        out["prep_signal_reasons_coverage"] = float(signal_with_reasons / signal_total)

    return out


def _extract_day_features(
    events_path: Path,
    report_path: Path | None = None,
    prep_metrics_path: Path | None = None,
) -> dict[str, float]:
    f = {k: 0.0 for k in FEATURE_NAMES}

    for ev in _iter_events(events_path):
        typ = ev.get("type")
        payload = ev.get("payload") or {}

        if typ == "RiskDecision":
            if payload.get("allowed"):
                f["risk_allowed"] += 1
            else:
                f["risk_blocked"] += 1
                reason = str(payload.get("reason") or "")
                if reason == "qty=0":
                    f["qty0_blocks"] += 1
                    ctx = payload.get("context")
                    is_real_ctx = (
                        isinstance(ctx, dict)
                        and bool(ctx)
                        and ctx.get("context_missing") is not True
                        and ctx.get("autofilled_at_emit") is not True
                    )
                    if not is_real_ctx:
                        f["qty0_missing_context"] += 1
                elif reason == "cooldown":
                    f["cooldown_blocks"] += 1
                elif reason == "entry_rate_limit":
                    f["entry_rate_limit_blocks"] += 1

        elif typ == "Signal":
            f["signal_total"] += 1
            ctx = payload.get("context")
            if isinstance(ctx, dict) and ctx:
                if ctx.get("min_score") is not None:
                    f["signal_with_min_score"] += 1
                reasons = ctx.get("reasons")
                if isinstance(reasons, list) and reasons:
                    f["signal_with_reasons"] += 1

        elif typ == "Fill":
            f["fill_count"] += 1
            fee = float(payload.get("fee") or 0.0)
            f["fee_total"] += fee
            if fee == 0.0 and str(payload.get("broker_order_id") or "").upper() != "PAPER":
                f["fee_zero_live"] += 1

    rep_json = _extract_report_features_from_metrics(prep_metrics_path)
    rep_json_keys = {
        "report_realized_pnl_est",
        "report_slippage_count",
        "report_slippage_avg",
        "report_slippage_worst",
        "report_avg_hold_sec",
        "report_win_count",
        "report_loss_count",
    }
    for k in FEATURE_NAMES:
        if k in rep_json:
            f[k] = float(rep_json[k])

    if report_path is not None:
        rep = _extract_trade_report_features(report_path)
        for k in FEATURE_NAMES:
            if k in rep and (k not in rep_json_keys or f.get(k, 0.0) == 0.0):
                f[k] = float(rep[k])
    prep = _extract_prep_metrics_features(prep_metrics_path)
    for k in FEATURE_NAMES:
        if k in prep:
            f[k] = float(prep[k])
    return f


def _parse_daily_pnl(
    report_path: Path,
    prep_metrics_path: Path | None = None,
) -> float | None:
    if prep_metrics_path is not None:
        data = _read_json(prep_metrics_path)
        if data:
            report = data.get("report") if isinstance(data.get("report"), dict) else {}
            pnl = report.get("daily_pnl_est") if report else None
            if isinstance(pnl, (int, float)):
                return float(pnl)
    if not report_path.exists():
        return None
    try:
        txt = report_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    # 당일 손익(추정, KRW): **-2,070원**
    m = re.search(r"당일 손익\(추정, KRW\): \*\*([\-0-9,]+)원\*\*", txt)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except Exception:
        return None


def _discover_training_rows(target_day: dt.date, lookback_days: int) -> tuple[list[list[float]], list[float], list[str]]:
    events_root = ROOT / "logs" / "events"
    rows_x: list[list[float]] = []
    rows_y: list[float] = []
    row_days: list[str] = []

    if not events_root.exists():
        return rows_x, rows_y, row_days

    start_day = target_day - dt.timedelta(days=lookback_days)

    for d in sorted(events_root.iterdir()):
        if not d.is_dir() or not d.name.isdigit() or len(d.name) != 8:
            continue
        try:
            day = dt.datetime.strptime(d.name, "%Y%m%d").date()
        except Exception:
            continue
        if day >= target_day or day < start_day:
            continue

        events_path = d / "events.jsonl"
        report_path = ROOT / "reports" / f"trade_report_{day.isoformat()}.md"
        prep_metrics_path = ROOT / "reports" / f"next_day_prep_metrics_{day.isoformat()}.json"
        y = _parse_daily_pnl(report_path, prep_metrics_path=prep_metrics_path)
        if y is None or not events_path.exists():
            continue

        feats = _extract_day_features(
            events_path,
            report_path=report_path,
            prep_metrics_path=prep_metrics_path,
        )
        _store_dataset_row(
            day=day,
            features=feats,
            label=float(y),
            events_path=events_path,
            report_path=report_path,
            prep_metrics_path=prep_metrics_path,
        )
        rows_x.append([float(feats[k]) for k in FEATURE_NAMES])
        rows_y.append(float(y))
        row_days.append(day.isoformat())

    return rows_x, rows_y, row_days


def _fit_and_predict(
    x_train: list[list[float]],
    y_train: list[float],
    x_pred: list[float],
    *,
    model_name: str = "ridge",
) -> dict[str, Any]:
    # sklearn path first; fallback to numpy ridge
    try:
        import joblib  # type: ignore
        import numpy as np
        from sklearn.ensemble import RandomForestRegressor
        from sklearn.linear_model import Ridge
        from sklearn.metrics import r2_score
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler

        x = np.asarray(x_train, dtype=float)
        y = np.asarray(y_train, dtype=float)
        xp = np.asarray([x_pred], dtype=float)

        use_tree = str(model_name or "ridge").strip().lower() == "tree"
        if use_tree:
            model = Pipeline(
                [
                    ("scaler", StandardScaler()),
                    ("tree", RandomForestRegressor(n_estimators=120, random_state=42)),
                ]
            )
        else:
            model = Pipeline(
                [
                    ("scaler", StandardScaler()),
                    ("ridge", Ridge(alpha=2.0, random_state=42)),
                ]
            )
        model.fit(x, y)

        y_hat_train = model.predict(x)
        pred = float(model.predict(xp)[0])
        r2 = float(r2_score(y, y_hat_train)) if len(y_train) >= 2 else 0.0

        scaler = model.named_steps["scaler"]
        if use_tree:
            tree = model.named_steps["tree"]
            coef = [float(z) for z in list(tree.feature_importances_)]
            intercept = float(sum(y_train) / max(1, len(y_train)))
            backend = "sklearn_tree"
        else:
            ridge = model.named_steps["ridge"]
            coef = (ridge.coef_ / scaler.scale_).tolist()
            intercept = float(ridge.intercept_ - float(sum((ridge.coef_ * scaler.mean_) / scaler.scale_)))
            backend = "sklearn_ridge"

        return {
            "ok": True,
            "backend": backend,
            "prediction": pred,
            "r2_train": r2,
            "coef": [float(c) for c in coef],
            "intercept": intercept,
            "pickle_obj": model,
            "pickle_lib": joblib,
        }
    except Exception:
        pass

    try:
        import numpy as np

        x = np.asarray(x_train, dtype=float)
        y = np.asarray(y_train, dtype=float)
        xp = np.asarray(x_pred, dtype=float)

        alpha = 2.0
        ones = np.ones((x.shape[0], 1), dtype=float)
        xa = np.hstack([ones, x])
        eye = np.eye(xa.shape[1], dtype=float)
        eye[0, 0] = 0.0  # do not regularize intercept
        w = np.linalg.pinv(xa.T @ xa + alpha * eye) @ xa.T @ y

        pred = float(np.hstack([[1.0], xp]) @ w)
        y_hat = xa @ w
        y_bar = float(np.mean(y)) if len(y) else 0.0
        ss_res = float(np.sum((y - y_hat) ** 2))
        ss_tot = float(np.sum((y - y_bar) ** 2))
        r2 = 0.0 if ss_tot <= 0 else 1.0 - (ss_res / ss_tot)

        return {
            "ok": True,
            "backend": "numpy_ridge",
            "prediction": pred,
            "r2_train": r2,
            "coef": [float(z) for z in w[1:].tolist()],
            "intercept": float(w[0]),
            "pickle_obj": None,
            "pickle_lib": None,
        }
    except Exception:
        return {
            "ok": False,
            "backend": "none",
            "prediction": 0.0,
            "r2_train": 0.0,
            "coef": [0.0 for _ in FEATURE_NAMES],
            "intercept": 0.0,
            "pickle_obj": None,
            "pickle_lib": None,
        }


def _walk_forward_validate(
    x_all: list[list[float]],
    y_all: list[float],
    day_all: list[str],
    *,
    min_train_rows: int,
    eval_last_k: int,
    model_name: str = "ridge",
    hit_threshold: float = 500.0,
) -> dict[str, Any]:
    n = len(x_all)
    if n <= min_train_rows:
        return {
            "enabled": True,
            "evaluated_days": [],
            "n_eval": 0,
            "mae": None,
            "directional_accuracy": None,
            "hit_rate_above_threshold": None,
            "hit_threshold": float(hit_threshold),
            "calibration_error": None,
            "y_true": [],
            "y_pred": [],
        }

    start = max(min_train_rows, n - max(1, int(eval_last_k)))
    eval_days: list[str] = []
    y_true: list[float] = []
    y_pred: list[float] = []

    for i in range(start, n):
        fit = _fit_and_predict(x_all[:i], y_all[:i], x_all[i], model_name=model_name)
        if not fit.get("ok"):
            continue
        eval_days.append(day_all[i])
        y_true.append(float(y_all[i]))
        y_pred.append(float(fit.get("prediction") or 0.0))

    n_eval = len(y_true)
    if n_eval <= 0:
        return {
            "enabled": True,
            "evaluated_days": eval_days,
            "n_eval": 0,
            "mae": None,
            "directional_accuracy": None,
            "hit_rate_above_threshold": None,
            "hit_threshold": float(hit_threshold),
            "calibration_error": None,
            "y_true": [],
            "y_pred": [],
        }

    abs_err = [abs(y_true[i] - y_pred[i]) for i in range(n_eval)]
    dir_hits = 0
    for i in range(n_eval):
        yt = y_true[i]
        yp = y_pred[i]
        yt_sign = 1 if yt > 0 else (-1 if yt < 0 else 0)
        yp_sign = 1 if yp > 0 else (-1 if yp < 0 else 0)
        if yt_sign == yp_sign:
            dir_hits += 1

    idx = [i for i in range(n_eval) if abs(float(y_pred[i])) >= float(hit_threshold)]
    hit_rate = None
    if idx:
        hit_ok = 0
        for i in idx:
            yt = float(y_true[i])
            yp = float(y_pred[i])
            if (yt > 0 and yp > 0) or (yt < 0 and yp < 0):
                hit_ok += 1
        hit_rate = float(hit_ok / len(idx))

    denom = sum(abs(float(v)) for v in y_true) / max(1, n_eval)
    calibration_error = None
    if denom > 0:
        avg_abs_pred = sum(abs(float(v)) for v in y_pred) / max(1, n_eval)
        calibration_error = float(abs(avg_abs_pred - denom) / denom)

    return {
        "enabled": True,
        "evaluated_days": eval_days,
        "n_eval": n_eval,
        "mae": float(sum(abs_err) / n_eval),
        "directional_accuracy": float(dir_hits / n_eval),
        "hit_rate_above_threshold": hit_rate,
        "hit_threshold": float(hit_threshold),
        "calibration_error": calibration_error,
        "y_true": [round(v, 4) for v in y_true],
        "y_pred": [round(v, 4) for v in y_pred],
    }


def _confidence_from_quality(
    r2_train: float,
    n_rows: int,
    wf_n_eval: int,
    wf_directional_acc: float | None,
) -> tuple[str, float]:
    base = 0.45
    if n_rows >= 8:
        base += 0.1
    if n_rows >= 15:
        base += 0.1
    if wf_n_eval >= 3 and wf_directional_acc is not None:
        if wf_directional_acc >= 0.65:
            base += 0.08
        elif wf_directional_acc >= 0.55:
            base += 0.04
        else:
            base -= 0.10
    score = _clamp(base + _clamp(r2_train, 0.0, 1.0) * 0.22, 0.25, 0.92)
    if score >= 0.75:
        return "high", score
    if score >= 0.58:
        return "medium", score
    return "low", score


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (KST). default=today", default=None)
    ap.add_argument("--events-path", default=None, help="override events path")
    ap.add_argument("--lookback-days", type=int, default=30)
    ap.add_argument("--min-train-rows", type=int, default=10)
    ap.add_argument(
        "--model",
        choices=["ridge", "tree"],
        default="ridge",
        help="model backend to use for fit and walk-forward (default: ridge)",
    )
    ap.add_argument("--walk-forward-k", type=int, default=7, help="evaluate last K train days")
    ap.add_argument("--wf-min-evals", type=int, default=3, help="minimum walk-forward eval points")
    ap.add_argument(
        "--wf-directional-acc-min",
        type=float,
        default=0.55,
        help="minimum walk-forward directional accuracy gate",
    )
    ap.add_argument(
        "--wf-mae-max",
        type=float,
        default=4000.0,
        help="maximum walk-forward MAE gate (KRW estimate)",
    )
    ap.add_argument(
        "--wf-hit-threshold",
        type=float,
        default=500.0,
        help="absolute predicted pnl threshold for hit-rate metric",
    )
    ap.add_argument(
        "--wf-hit-rate-min",
        type=float,
        default=0.50,
        help="minimum walk-forward hit rate above threshold gate",
    )
    ap.add_argument(
        "--wf-calibration-max",
        type=float,
        default=1.00,
        help="maximum walk-forward calibration error gate",
    )
    args = ap.parse_args()

    kst = dt.timezone(dt.timedelta(hours=9))
    day = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(tz=kst).date()

    ymd = day.strftime("%Y%m%d")
    events_path = Path(args.events_path) if args.events_path else (ROOT / "logs" / "events" / ymd / "events.jsonl")
    trade_report_path = ROOT / "reports" / f"trade_report_{day.isoformat()}.md"
    prep_metrics_path = ROOT / "reports" / f"next_day_prep_metrics_{day.isoformat()}.json"

    cfg = _load_current_cfg()
    trading = dict(cfg.get("trading") or {})
    scoring = dict(trading.get("scoring") or {})

    cur_entry_budget_pct = float(trading.get("entry_budget_pct", 0.20) or 0.20)
    cur_cash_reserve_pct = float(trading.get("cash_reserve_pct", 0.20) or 0.20)
    cur_entries_per_min = int(trading.get("max_new_entries_per_minute", 2) or 2)
    cur_min_score = float(scoring.get("kr_scalp_entry_threshold", 50) or 50)
    cur_cooldown_sec = float(trading.get("symbol_cooldown_sec", 900) or 900)
    cur_atr_min = float(trading.get("atr_min_percent", 0.35) or 0.35)

    features_today = _extract_day_features(
        events_path,
        report_path=trade_report_path,
        prep_metrics_path=prep_metrics_path,
    )
    _store_dataset_row(
        day=day,
        features=features_today,
        label=None,
        events_path=events_path,
        report_path=trade_report_path,
        prep_metrics_path=prep_metrics_path,
    )
    x_train, y_train, train_days = _discover_training_rows(day, max(7, int(args.lookback_days)))
    x_pred = [float(features_today[k]) for k in FEATURE_NAMES]
    min_train_rows = max(1, int(args.min_train_rows))
    train_rows = len(x_train)
    training_rows_sufficient = train_rows >= min_train_rows
    training_gate_reason = (
        f"insufficient_training_rows:{train_rows}<{min_train_rows}"
        if not training_rows_sufficient
        else ""
    )

    wf = _walk_forward_validate(
        x_train,
        y_train,
        train_days,
        min_train_rows=min_train_rows,
        eval_last_k=max(1, int(args.walk_forward_k)),
        model_name=args.model,
        hit_threshold=float(args.wf_hit_threshold),
    )
    wf_n_eval = int(wf.get("n_eval") or 0)
    wf_dir_acc = wf.get("directional_accuracy")
    wf_mae = wf.get("mae")
    wf_hit_rate = wf.get("hit_rate_above_threshold")
    wf_cal_error = wf.get("calibration_error")

    fit = (
        _fit_and_predict(x_train, y_train, x_pred, model_name=args.model)
        if training_rows_sufficient
        else {
            "ok": False,
            "backend": "insufficient_rows_guard",
            "prediction": 0.0,
            "r2_train": 0.0,
            "coef": [0.0 for _ in FEATURE_NAMES],
            "intercept": 0.0,
            "pickle_obj": None,
            "pickle_lib": None,
        }
    )
    pred_pnl = float(fit["prediction"]) if fit.get("ok") else 0.0
    r2_train = float(fit.get("r2_train") or 0.0)

    if training_rows_sufficient:
        conf_level, conf_score = _confidence_from_quality(
            r2_train,
            train_rows,
            wf_n_eval,
            float(wf_dir_acc) if wf_dir_acc is not None else None,
        )
    else:
        conf_level, conf_score = "low", 0.32

    wf_quality_ok = (
        training_rows_sufficient
        and wf_n_eval >= max(1, int(args.wf_min_evals))
        and (wf_dir_acc is not None and float(wf_dir_acc) >= float(args.wf_directional_acc_min))
        and (wf_mae is not None and float(wf_mae) <= float(args.wf_mae_max))
        and (wf_hit_rate is not None and float(wf_hit_rate) >= float(args.wf_hit_rate_min))
        and (wf_cal_error is not None and float(wf_cal_error) <= float(args.wf_calibration_max))
    )
    ml_reco_gate_ok = bool(training_rows_sufficient and fit.get("ok") and wf_quality_ok)
    ml_reco_gate_reason = ""
    if not training_rows_sufficient:
        ml_reco_gate_reason = training_gate_reason
    elif not fit.get("ok"):
        ml_reco_gate_reason = "model_fit_failed"
    elif wf_n_eval < max(1, int(args.wf_min_evals)):
        ml_reco_gate_reason = f"wf_insufficient:{wf_n_eval}<{max(1, int(args.wf_min_evals))}"
    elif wf_dir_acc is None or float(wf_dir_acc) < float(args.wf_directional_acc_min):
        ml_reco_gate_reason = (
            f"wf_directional_acc_low:{0.0 if wf_dir_acc is None else float(wf_dir_acc):.3f}"
            f"<{float(args.wf_directional_acc_min):.3f}"
        )
    elif wf_mae is None or float(wf_mae) > float(args.wf_mae_max):
        ml_reco_gate_reason = (
            f"wf_mae_high:{0.0 if wf_mae is None else float(wf_mae):.2f}"
            f">{float(args.wf_mae_max):.2f}"
        )
    elif wf_hit_rate is None or float(wf_hit_rate) < float(args.wf_hit_rate_min):
        ml_reco_gate_reason = (
            f"wf_hit_rate_low:{0.0 if wf_hit_rate is None else float(wf_hit_rate):.3f}"
            f"<{float(args.wf_hit_rate_min):.3f}"
        )
    elif wf_cal_error is None or float(wf_cal_error) > float(args.wf_calibration_max):
        ml_reco_gate_reason = (
            f"wf_calibration_high:{0.0 if wf_cal_error is None else float(wf_cal_error):.4f}"
            f">{float(args.wf_calibration_max):.4f}"
        )

    coef = [float(c) for c in (fit.get("coef") or [0.0 for _ in FEATURE_NAMES])]
    importances = []
    for i, name in enumerate(FEATURE_NAMES):
        c = coef[i] if i < len(coef) else 0.0
        importances.append({"feature": name, "coefficient": round(float(c), 6), "abs": abs(float(c))})
    importances.sort(key=lambda x: x["abs"], reverse=True)
    top_importances = importances[:5]

    recos: dict[str, Any] = {
        "date": day.isoformat(),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "generated_by": "ml_training_inference",
        "current": {
            "trading.entry_budget_pct": cur_entry_budget_pct,
            "trading.cash_reserve_pct": cur_cash_reserve_pct,
            "trading.max_new_entries_per_minute": cur_entries_per_min,
            "trading.scoring.kr_scalp_entry_threshold": cur_min_score,
            "trading.symbol_cooldown_sec": cur_cooldown_sec,
            "trading.atr_min_percent": cur_atr_min,
        },
        "inputs": {
            "config_path": str(cfg.get("_config_path") or ""),
            "config_hash": str(cfg.get("_config_hash") or ""),
            "events_path": str(events_path),
            "trade_report_path": str(trade_report_path),
            "trade_report_exists": trade_report_path.exists(),
            "prep_metrics_path": str(prep_metrics_path),
            "prep_metrics_exists": prep_metrics_path.exists(),
            "features": features_today,
            "model": {
                "backend": fit.get("backend"),
                "selected_model": str(args.model),
                "train_rows": train_rows,
                "min_train_rows": min_train_rows,
                "train_rows_sufficient": training_rows_sufficient,
                "training_gate_reason": training_gate_reason,
                "train_days": train_days,
                "r2_train": round(r2_train, 6),
                "prediction_daily_pnl": round(pred_pnl, 4),
                "feature_importances": top_importances,
            },
        },
        "reasons": [x for x in [training_gate_reason, ml_reco_gate_reason] if x],
        "recommendations": [],
    }

    def add(path: str, current: float | int, recommended: float | int, rationale: str, evidence: dict[str, Any]) -> None:
        lo, hi = SAFE_RANGES[path]
        v = _clamp(float(recommended), float(lo), float(hi))
        level, score = conf_level, conf_score
        recos["recommendations"].append(
            {
                "key": path.split(".")[-1],
                "path": path,
                "current": current,
                "recommended": round(v, 6),
                "delta": round(v - float(current), 6),
                "confidence": _conf(level, score),
                "rationale": rationale,
                "evidence": evidence,
            }
        )

    qty0 = int(features_today["qty0_blocks"])
    qty0_miss = int(features_today["qty0_missing_context"])
    fill_count = int(features_today["fill_count"])
    fee_total = float(features_today["fee_total"])

    if ml_reco_gate_ok and train_rows >= 5:
        # Guardrail policy from predicted outcome.
        if pred_pnl < -500:
            bump = _clamp(abs(pred_pnl) / 3000.0, 1.0, 4.0)
            add(
                "trading.scoring.kr_scalp_entry_threshold",
                cur_min_score,
                cur_min_score + bump,
                "ML 예측 손익이 음수로 나타나 진입 점수 하한을 상향해 과도한 진입을 억제합니다.",
                {
                    "prediction_daily_pnl": round(pred_pnl, 2),
                    "r2_train": round(r2_train, 4),
                    "top_importances": top_importances,
                },
            )
            add(
                "trading.symbol_cooldown_sec",
                cur_cooldown_sec,
                cur_cooldown_sec + 120,
                "ML 리스크 신호에 따라 재진입 빈도를 완만히 낮춥니다.",
                {"prediction_daily_pnl": round(pred_pnl, 2), "cooldown_blocks": int(features_today["cooldown_blocks"])} ,
            )

        elif pred_pnl > 700:
            ease = _clamp(pred_pnl / 7000.0, 0.01, 0.03)
            add(
                "trading.entry_budget_pct",
                cur_entry_budget_pct,
                cur_entry_budget_pct + ease,
                "ML 예측 손익이 양호해 진입 예산을 소폭 완화합니다.",
                {
                    "prediction_daily_pnl": round(pred_pnl, 2),
                    "r2_train": round(r2_train, 4),
                    "top_importances": top_importances,
                },
            )

    # Safety overlays from observed day quality.
    if ml_reco_gate_ok and qty0 >= 5:
        add(
            "trading.cash_reserve_pct",
            cur_cash_reserve_pct,
            cur_cash_reserve_pct - 0.02,
            "qty=0 차단이 반복되어 현금보유 하한을 보수 범위 내에서 소폭 완화합니다.",
            {"qty0_blocks": qty0, "qty0_missing_context": qty0_miss},
        )

    if ml_reco_gate_ok and fill_count >= 12 and fee_total >= 800:
        add(
            "trading.atr_min_percent",
            cur_atr_min,
            cur_atr_min + 0.05,
            "체결/수수료 부담이 커서 저변동 구간 진입을 줄이도록 ATR 필터를 상향합니다.",
            {"fill_count": fill_count, "fee_total": round(fee_total, 2)},
        )

    if not recos["recommendations"] or not ml_reco_gate_ok:
        recos["recommendations"].append(
            {
                "key": "keep_defaults",
                "path": "trading.*",
                "current": "as-is",
                "recommended": "as-is",
                "delta": 0,
                "confidence": _conf(conf_level, conf_score),
                "rationale": (
                    "학습/검증 게이트를 통과하지 못해(as-is 유지) 추천 변경을 생성하지 않습니다."
                    if not ml_reco_gate_ok
                    else "학습 신뢰도/데이터 기준에서 적극적인 파라미터 변경 근거가 충분하지 않습니다."
                ),
                "evidence": {
                    "prediction_daily_pnl": round(pred_pnl, 2),
                    "train_rows": train_rows,
                    "min_train_rows": min_train_rows,
                    "train_rows_sufficient": training_rows_sufficient,
                    "training_gate_reason": training_gate_reason,
                    "ml_reco_gate_ok": ml_reco_gate_ok,
                    "ml_reco_gate_reason": ml_reco_gate_reason,
                    "r2_train": round(r2_train, 4),
                },
            }
        )

    out_dir = ROOT / "reports"
    models_dir = out_dir / "models"
    out_dir.mkdir(parents=True, exist_ok=True)
    models_dir.mkdir(parents=True, exist_ok=True)

    out_json = out_dir / f"next_day_reco_{day.isoformat()}.json"
    out_md = out_dir / f"next_day_reco_{day.isoformat()}.md"
    model_json = models_dir / f"reco_model_{day.isoformat()}.json"
    model_pkl = models_dir / f"reco_model_{day.isoformat()}.pkl"

    model_artifact = {
        "date": day.isoformat(),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "backend": fit.get("backend"),
        "feature_names": FEATURE_NAMES,
        "coef": coef,
        "intercept": float(fit.get("intercept") or 0.0),
        "train_rows": train_rows,
        "min_train_rows": min_train_rows,
        "train_rows_sufficient": training_rows_sufficient,
        "training_gate_reason": training_gate_reason,
        "train_days": train_days,
        "r2_train": round(r2_train, 6),
        "prediction_daily_pnl": round(pred_pnl, 6),
        "walk_forward": {
            "k": int(args.walk_forward_k),
            "n_eval": wf_n_eval,
            "mae": None if wf_mae is None else round(float(wf_mae), 6),
            "directional_accuracy": None if wf_dir_acc is None else round(float(wf_dir_acc), 6),
            "hit_rate_above_threshold": None if wf_hit_rate is None else round(float(wf_hit_rate), 6),
            "hit_threshold": float(args.wf_hit_threshold),
            "calibration_error": None if wf_cal_error is None else round(float(wf_cal_error), 6),
            "evaluated_days": list(wf.get("evaluated_days") or []),
        },
        "ml_reco_gate": {
            "ok": ml_reco_gate_ok,
            "reason": ml_reco_gate_reason,
            "wf_min_evals": int(args.wf_min_evals),
            "wf_directional_acc_min": float(args.wf_directional_acc_min),
            "wf_mae_max": float(args.wf_mae_max),
            "wf_hit_rate_min": float(args.wf_hit_rate_min),
            "wf_hit_threshold": float(args.wf_hit_threshold),
            "wf_calibration_max": float(args.wf_calibration_max),
        },
    }
    model_json.write_text(json.dumps(model_artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    pkl_obj = fit.get("pickle_obj")
    pkl_lib = fit.get("pickle_lib")
    if pkl_obj is not None and pkl_lib is not None:
        try:
            pkl_lib.dump(pkl_obj, model_pkl)
        except Exception:
            pass

    recos["inputs"]["model"]["artifact_json"] = str(model_json)
    recos["inputs"]["model"]["artifact_pickle"] = str(model_pkl) if model_pkl.exists() else None
    recos["inputs"]["model"]["walk_forward"] = {
        "k": int(args.walk_forward_k),
        "n_eval": wf_n_eval,
        "mae": None if wf_mae is None else round(float(wf_mae), 6),
        "directional_accuracy": None if wf_dir_acc is None else round(float(wf_dir_acc), 6),
        "hit_rate_above_threshold": None if wf_hit_rate is None else round(float(wf_hit_rate), 6),
        "hit_threshold": float(args.wf_hit_threshold),
        "calibration_error": None if wf_cal_error is None else round(float(wf_cal_error), 6),
        "evaluated_days": list(wf.get("evaluated_days") or []),
    }
    recos["inputs"]["model"]["ml_reco_gate"] = {
        "ok": ml_reco_gate_ok,
        "reason": ml_reco_gate_reason,
        "wf_min_evals": int(args.wf_min_evals),
        "wf_directional_acc_min": float(args.wf_directional_acc_min),
        "wf_mae_max": float(args.wf_mae_max),
        "wf_hit_rate_min": float(args.wf_hit_rate_min),
        "wf_hit_threshold": float(args.wf_hit_threshold),
        "wf_calibration_max": float(args.wf_calibration_max),
    }

    out_json.write_text(json.dumps(recos, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [f"# 내일 장 대비 추천 - {day.isoformat()}", "", "## Metadata", ""]
    lines.append(f"- generated_at: `{recos['generated_at']}`")
    lines.append(f"- input_events_path: `{events_path}`")
    lines.append(f"- config_hash_sha256: `{cfg.get('_config_hash') or ''}`")
    lines.append(f"- model_artifact: `{model_json}`")
    lines.append("")
    lines.append("## ML Summary")
    lines.append("")
    lines.append(f"- backend: `{fit.get('backend')}`")
    lines.append(f"- train_rows: `{train_rows}`")
    lines.append(f"- min_train_rows: `{min_train_rows}`")
    lines.append(f"- train_rows_sufficient: `{training_rows_sufficient}`")
    if training_gate_reason:
        lines.append(f"- training_gate_reason: `{training_gate_reason}`")
    lines.append(f"- r2_train: `{round(r2_train, 6)}`")
    lines.append(f"- prediction_daily_pnl: `{round(pred_pnl, 2)}`")
    lines.append(f"- walk_forward_k: `{int(args.walk_forward_k)}`")
    lines.append(f"- walk_forward_n_eval: `{wf_n_eval}`")
    lines.append(f"- walk_forward_mae: `{None if wf_mae is None else round(float(wf_mae), 2)}`")
    lines.append(
        f"- walk_forward_directional_accuracy: `{None if wf_dir_acc is None else round(float(wf_dir_acc), 4)}`"
    )
    lines.append(
        f"- walk_forward_hit_rate_above_threshold: `{None if wf_hit_rate is None else round(float(wf_hit_rate), 4)}`"
    )
    lines.append(f"- walk_forward_hit_threshold: `{float(args.wf_hit_threshold)}`")
    lines.append(
        f"- walk_forward_calibration_error: `{None if wf_cal_error is None else round(float(wf_cal_error), 4)}`"
    )
    lines.append(f"- ml_reco_gate_ok: `{ml_reco_gate_ok}`")
    if ml_reco_gate_reason:
        lines.append(f"- ml_reco_gate_reason: `{ml_reco_gate_reason}`")
    lines.append(f"- top_importances: `{json.dumps(top_importances, ensure_ascii=False)}`")
    lines.append("")
    lines.append("## 추천 항목")
    lines.append("")

    for r in recos["recommendations"]:
        lines.append(f"### {r['key']} ({r['path']})")
        lines.append("")
        lines.append(f"- 현재값: `{r['current']}`")
        lines.append(f"- 추천값: `{r['recommended']}`")
        if r.get("delta") is not None:
            lines.append(f"- 변경량: `{r['delta']}`")
        conf = r.get("confidence") or {}
        lines.append(f"- 신뢰도: `{conf.get('level', 'n/a')}` ({conf.get('score', 'n/a')})")
        lines.append(f"- 근거: {r['rationale']}")
        lines.append(f"- 증거: `{json.dumps(r.get('evidence') or {}, ensure_ascii=False)}`")
        lines.append("")

    out_md.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    print(str(out_json))
    print(str(out_md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
