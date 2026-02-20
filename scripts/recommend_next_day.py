#!/usr/bin/env python3
"""Train and infer next-day recommendations from historical events/reports."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
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


def _extract_day_features(events_path: Path) -> dict[str, float]:
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
                    if not (isinstance(ctx, dict) and ctx):
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

    return f


def _parse_daily_pnl(report_path: Path) -> float | None:
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
        y = _parse_daily_pnl(report_path)
        if y is None or not events_path.exists():
            continue

        feats = _extract_day_features(events_path)
        rows_x.append([float(feats[k]) for k in FEATURE_NAMES])
        rows_y.append(float(y))
        row_days.append(day.isoformat())

    return rows_x, rows_y, row_days


def _fit_and_predict(x_train: list[list[float]], y_train: list[float], x_pred: list[float]) -> dict[str, Any]:
    # sklearn path first; fallback to numpy ridge
    try:
        import joblib  # type: ignore
        import numpy as np
        from sklearn.linear_model import Ridge
        from sklearn.metrics import r2_score
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler

        x = np.asarray(x_train, dtype=float)
        y = np.asarray(y_train, dtype=float)
        xp = np.asarray([x_pred], dtype=float)

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

        ridge = model.named_steps["ridge"]
        scaler = model.named_steps["scaler"]
        # Convert scaled coef back to original feature scale.
        coef = (ridge.coef_ / scaler.scale_).tolist()
        intercept = float(ridge.intercept_ - float(sum((ridge.coef_ * scaler.mean_) / scaler.scale_)))

        return {
            "ok": True,
            "backend": "sklearn_ridge",
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


def _confidence_from_quality(r2_train: float, n_rows: int) -> tuple[str, float]:
    base = 0.45
    if n_rows >= 8:
        base += 0.1
    if n_rows >= 15:
        base += 0.1
    score = _clamp(base + _clamp(r2_train, 0.0, 1.0) * 0.25, 0.3, 0.92)
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
    args = ap.parse_args()

    kst = dt.timezone(dt.timedelta(hours=9))
    day = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(tz=kst).date()

    ymd = day.strftime("%Y%m%d")
    events_path = Path(args.events_path) if args.events_path else (ROOT / "logs" / "events" / ymd / "events.jsonl")
    trade_report_path = ROOT / "reports" / f"trade_report_{day.isoformat()}.md"

    cfg = _load_current_cfg()
    trading = dict(cfg.get("trading") or {})
    scoring = dict(trading.get("scoring") or {})

    cur_entry_budget_pct = float(trading.get("entry_budget_pct", 0.20) or 0.20)
    cur_cash_reserve_pct = float(trading.get("cash_reserve_pct", 0.20) or 0.20)
    cur_entries_per_min = int(trading.get("max_new_entries_per_minute", 2) or 2)
    cur_min_score = float(scoring.get("kr_scalp_entry_threshold", 50) or 50)
    cur_cooldown_sec = float(trading.get("symbol_cooldown_sec", 900) or 900)
    cur_atr_min = float(trading.get("atr_min_percent", 0.35) or 0.35)

    features_today = _extract_day_features(events_path)
    x_train, y_train, train_days = _discover_training_rows(day, max(7, int(args.lookback_days)))
    x_pred = [float(features_today[k]) for k in FEATURE_NAMES]

    fit = _fit_and_predict(x_train, y_train, x_pred)
    pred_pnl = float(fit["prediction"]) if fit.get("ok") else 0.0
    r2_train = float(fit.get("r2_train") or 0.0)

    conf_level, conf_score = _confidence_from_quality(r2_train, len(x_train))

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
            "features": features_today,
            "model": {
                "backend": fit.get("backend"),
                "train_rows": len(x_train),
                "train_days": train_days,
                "r2_train": round(r2_train, 6),
                "prediction_daily_pnl": round(pred_pnl, 4),
                "feature_importances": top_importances,
            },
        },
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

    if fit.get("ok") and len(x_train) >= 5:
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
    if qty0 >= 5:
        add(
            "trading.cash_reserve_pct",
            cur_cash_reserve_pct,
            cur_cash_reserve_pct - 0.02,
            "qty=0 차단이 반복되어 현금보유 하한을 보수 범위 내에서 소폭 완화합니다.",
            {"qty0_blocks": qty0, "qty0_missing_context": qty0_miss},
        )

    if fill_count >= 12 and fee_total >= 800:
        add(
            "trading.atr_min_percent",
            cur_atr_min,
            cur_atr_min + 0.05,
            "체결/수수료 부담이 커서 저변동 구간 진입을 줄이도록 ATR 필터를 상향합니다.",
            {"fill_count": fill_count, "fee_total": round(fee_total, 2)},
        )

    if not recos["recommendations"]:
        recos["recommendations"].append(
            {
                "key": "keep_defaults",
                "path": "trading.*",
                "current": "as-is",
                "recommended": "as-is",
                "delta": 0,
                "confidence": _conf(conf_level, conf_score),
                "rationale": "학습 신뢰도/데이터 기준에서 적극적인 파라미터 변경 근거가 충분하지 않습니다.",
                "evidence": {
                    "prediction_daily_pnl": round(pred_pnl, 2),
                    "train_rows": len(x_train),
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
        "train_rows": len(x_train),
        "train_days": train_days,
        "r2_train": round(r2_train, 6),
        "prediction_daily_pnl": round(pred_pnl, 6),
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
    lines.append(f"- train_rows: `{len(x_train)}`")
    lines.append(f"- r2_train: `{round(r2_train, 6)}`")
    lines.append(f"- prediction_daily_pnl: `{round(pred_pnl, 2)}`")
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
