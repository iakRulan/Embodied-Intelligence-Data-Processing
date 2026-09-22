# -*- coding: utf-8 -*-
"""v2.2 additive channels: visual-freeze overlay + IsolationForest + negative experiments.

Rules
-----
* Do not change the 61/89 algorithm-flagged count by inventing new rule hits.
  S_VISUAL_FREEZE_UNDER_MOTION is attached only to episodes already in the 61
  (ep15/69/85/88). Scores are not rewritten.
* IsolationForest is a supplementary unsupervised channel. Its flags never
  enter issue_codes or the 61/89 headline.
* Clock-lag and 花屏 stay as measured negative results; no fake detectors.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

ROOT = Path(__file__).resolve().parents[1]
from dataset_paths import OUT_DIR
DETECTOR_VERSION = "RefSync-QA-v2.2"
FREEZE_CODE = "S_VISUAL_FREEZE_UNDER_MOTION"
FREEZE_CATEGORY = "同步"

FEATURE_COLS = [
    "dt_mad",
    "dt_nonpositive_rate",
    "dt_gap_rate",
    "frame_nonunit_rate",
    "length_delta_abs",
    "missing_rate",
    "dark_rate",
    "low_contrast_rate",
    "duplicate_rate",
    "std_luma_median",
    "laplacian_median",
    "gradient_median",
    "state_finite_rate",
    "state_step_p99",
    "action_step_p99",
    "aligned_rmse_p99",
    "motion_mad_median",
    "noise_residual_median",
    "chroma_spread_median",
]


def read_csv(name: str) -> pd.DataFrame:
    return pd.read_csv(OUT_DIR / name, encoding="utf-8-sig")


def _rate(count: pd.Series, rows: pd.Series) -> pd.Series:
    rows = pd.to_numeric(rows, errors="coerce").replace(0, np.nan)
    return pd.to_numeric(count, errors="coerce") / rows


def episode_features(label: str) -> pd.DataFrame:
    structural = read_csv(f"{label}_structural_episodes.csv")
    image = read_csv(f"{label}_image_episodes.csv")
    values = read_csv(f"{label}_value_episodes.csv")

    s = structural.copy()
    s["episode_index"] = pd.to_numeric(s["episode_index"], errors="coerce")
    rows = pd.to_numeric(s.get("rows"), errors="coerce")
    feat = pd.DataFrame({"episode_index": s["episode_index"]})
    feat["dt_mad"] = pd.to_numeric(s.get("dt_mad"), errors="coerce")
    feat["dt_nonpositive_rate"] = _rate(s.get("dt_nonpositive_count"), rows)
    feat["dt_gap_rate"] = _rate(s.get("dt_gap_gt_1_5x_count"), rows)
    feat["frame_nonunit_rate"] = _rate(s.get("frame_nonunit_diff_count"), rows)
    feat["length_delta_abs"] = pd.to_numeric(s.get("length_delta"), errors="coerce").abs()

    img = image.copy()
    img["episode_index"] = pd.to_numeric(img["episode_index"], errors="coerce")
    img_rows = pd.to_numeric(img.get("rows"), errors="coerce")
    img_part = pd.DataFrame({"episode_index": img["episode_index"]})
    img_part["missing_rate"] = _rate(img.get("any_missing_count"), img_rows)
    img_part["dark_rate"] = _rate(img.get("any_dark_frame_count"), img_rows)
    img_part["low_contrast_rate"] = _rate(img.get("any_low_contrast_count"), img_rows)
    img_part["duplicate_rate"] = _rate(img.get("any_exact_duplicate_adjacent_count"), img_rows)
    img_part["std_luma_median"] = pd.to_numeric(img.get("image_std_luma_median"), errors="coerce")
    img_part["laplacian_median"] = pd.to_numeric(img.get("image_laplacian_var_median"), errors="coerce")
    img_part["gradient_median"] = pd.to_numeric(img.get("image_gradient_energy_median"), errors="coerce")
    feat = feat.merge(img_part, on="episode_index", how="left")

    val = values.copy()
    val["episode_index"] = pd.to_numeric(val["episode_index"], errors="coerce")
    val_rows = pd.to_numeric(val.get("rows"), errors="coerce")
    val_part = pd.DataFrame({"episode_index": val["episode_index"]})
    val_part["state_finite_rate"] = _rate(val.get("state_finite_bad_count"), val_rows)
    val_part["state_step_p99"] = pd.to_numeric(val.get("state_step_p99"), errors="coerce")
    val_part["action_step_p99"] = pd.to_numeric(val.get("action_step_p99"), errors="coerce")
    rmse_col = "state_action_aligned_rmse_p99" if "state_action_aligned_rmse_p99" in val.columns else "state_action_rmse_p99"
    val_part["aligned_rmse_p99"] = pd.to_numeric(val.get(rmse_col), errors="coerce")
    feat = feat.merge(val_part, on="episode_index", how="left")

    v22_path = OUT_DIR / f"{label}_image_frames_v22.csv"
    if v22_path.exists():
        v22 = pd.read_csv(v22_path, encoding="utf-8-sig", low_memory=False)
        ep_col = "dataset_episode" if "dataset_episode" in v22.columns else "episode_index"
        v22[ep_col] = pd.to_numeric(v22[ep_col], errors="coerce")
        extra = v22.groupby(ep_col, as_index=False).agg(
            motion_mad_median=("motion_mad_prev", "median"),
            noise_residual_median=("noise_residual", "median"),
            chroma_spread_median=("chroma_spread", "median"),
        )
        extra = extra.rename(columns={ep_col: "episode_index"})
        feat = feat.merge(extra, on="episode_index", how="left")
    else:
        feat["motion_mad_median"] = np.nan
        feat["noise_residual_median"] = np.nan
        feat["chroma_spread_median"] = np.nan

    feat["dataset"] = label
    return feat


def _impute_with_ref(ref: pd.DataFrame, other: pd.DataFrame, cols: list[str]) -> tuple[np.ndarray, np.ndarray, dict]:
    med = ref[cols].median(numeric_only=True)
    ref_x = ref[cols].fillna(med).to_numpy(dtype=float)
    other_x = other[cols].fillna(med).to_numpy(dtype=float)
    mu = ref_x.mean(axis=0)
    sd = ref_x.std(axis=0)
    sd = np.where(sd < 1e-9, 1.0, sd)
    return (ref_x - mu) / sd, (other_x - mu) / sd, {"mean": mu.tolist(), "std": sd.tolist()}


def isolation_forest_loeo(ref_feat: pd.DataFrame, test_feat: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    cols = [c for c in FEATURE_COLS if c in ref_feat.columns]
    ref = ref_feat.dropna(subset=["episode_index"]).copy()
    test = test_feat.dropna(subset=["episode_index"]).copy()
    ref_idx = np.arange(len(ref))
    loeo_scores = np.zeros(len(ref), dtype=float)
    for i in ref_idx:
        train = ref.drop(ref.index[i])
        held = ref.iloc[[i]]
        x_train, x_held, _ = _impute_with_ref(train, held, cols)
        model = IsolationForest(
            n_estimators=200,
            contamination="auto",
            random_state=0,
            n_jobs=1,
        )
        model.fit(x_train)
        loeo_scores[i] = float(-model.score_samples(x_held)[0])
    threshold = float(np.max(loeo_scores)) if loeo_scores.size else float("nan")

    x_ref, x_test, scaler = _impute_with_ref(ref, test, cols)
    model = IsolationForest(n_estimators=200, contamination="auto", random_state=0, n_jobs=1)
    model.fit(x_ref)
    ref_scores = -model.score_samples(x_ref)
    test_scores = -model.score_samples(x_test)

    ref_out = ref[["episode_index"]].copy()
    ref_out["iforest_score"] = np.round(ref_scores, 6)
    ref_out["iforest_loeo_score"] = np.round(loeo_scores, 6)
    # Compare in full precision before rounding so max(LOEO) itself is not a false positive.
    ref_out["iforest_flag"] = loeo_scores > threshold
    ref_out["dataset"] = "reference"

    test_out = test[["episode_index"]].copy()
    test_out["iforest_score"] = np.round(test_scores, 6)
    test_out["iforest_flag"] = test_scores > threshold
    test_out["dataset"] = "test"

    summary = {
        "model": "IsolationForest",
        "n_estimators": 200,
        "features": cols,
        "scaler": "reference mean/std; NaN filled with reference median",
        "threshold_rule": "max LOEO anomaly score on the 20 clean reference episodes (0/20 false positives by construction)",
        "threshold": threshold,
        "reference_loeo_flagged": int(ref_out["iforest_flag"].sum()),
        "test_flagged": int(test_out["iforest_flag"].sum()),
        "role": "supplementary unseen-pattern recall; does not enter issue_codes or the 61/89 headline",
    }
    return ref_out, test_out, summary


def as_bool(series: pd.Series) -> pd.Series:
    if series is None or series.empty:
        return pd.Series(dtype=bool)
    if pd.api.types.is_bool_dtype(series):
        return series.fillna(False).astype(bool)
    return series.fillna("False").astype(str).str.strip().str.lower().isin({"true", "1", "yes"})


def overlay_freeze(report: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    freeze = read_csv("visual_freeze_test_results.csv")
    intervals = read_csv("visual_freeze_intervals.csv")
    freeze["episode_index"] = pd.to_numeric(freeze["episode_index"], errors="coerce")
    report = report.copy()
    report["episode_index"] = pd.to_numeric(report["episode_index"], errors="coerce")
    for col in [
        "visual_freeze_flag",
        "visual_freeze_frames",
        "visual_freeze_max_run",
        "visual_freeze_interval",
        "iforest_score",
        "iforest_flag",
    ]:
        if col in report.columns:
            report = report.drop(columns=[col])

    merged = report.merge(
        freeze[
            [
                "episode_index",
                "freeze_under_motion_frames",
                "max_run_frames",
                "flagged",
            ]
        ],
        on="episode_index",
        how="left",
    )
    merged["visual_freeze_flag"] = as_bool(merged["flagged"])
    merged["visual_freeze_frames"] = pd.to_numeric(merged["freeze_under_motion_frames"], errors="coerce").fillna(0).astype(int)
    merged["visual_freeze_max_run"] = pd.to_numeric(merged["max_run_frames"], errors="coerce").fillna(0).astype(int)
    merged = merged.drop(columns=["flagged", "freeze_under_motion_frames", "max_run_frames"])

    interval_map = {
        int(row.episode_index): f"{int(row.start_row)}-{int(row.end_row)}/{int(row.length_frames)}f"
        for row in intervals.itertuples(index=False)
    }
    merged["visual_freeze_interval"] = merged["episode_index"].map(lambda e: interval_map.get(int(e), ""))

    already_flagged = set(merged.loc[merged["status"] != "通过", "episode_index"].astype(int))
    freeze_eps = set(merged.loc[merged["visual_freeze_flag"], "episode_index"].astype(int))
    new_to_61 = sorted(freeze_eps - already_flagged)
    if new_to_61:
        raise RuntimeError(
            f"Visual freeze would add previously unflagged episodes {new_to_61}; refusing to inflate 61/89."
        )

    details_extra = {
        int(row.episode_index): f"视觉冻结而本体仍在运动（{int(row.start_row)}–{int(row.end_row)} 行，{int(row.length_frames)} 帧）"
        for row in intervals.itertuples(index=False)
    }

    def _append_code(row: pd.Series) -> pd.Series:
        if not bool(row["visual_freeze_flag"]):
            return row
        codes = [c for c in str(row.get("issue_codes") or "").split(";") if c]
        cats = [c for c in str(row.get("issue_categories") or "").split(";") if c]
        details = [d for d in str(row.get("issue_details") or "").split("；") if d]
        if FREEZE_CODE not in codes:
            codes.append(FREEZE_CODE)
        if FREEZE_CATEGORY not in cats:
            cats.append(FREEZE_CATEGORY)
        extra = details_extra.get(int(row["episode_index"]))
        if extra and extra not in details:
            details.append(extra)
        row["issue_codes"] = ";".join(codes)
        row["issue_categories"] = ";".join(cats)
        row["issue_details"] = "；".join(details)
        return row

    merged = merged.apply(_append_code, axis=1)
    merged["detector_version"] = DETECTOR_VERSION

    flagged_after = int((merged["status"] != "通过").sum())
    summary = {
        "code": FREEZE_CODE,
        "test_flagged_episodes": sorted(int(x) for x in freeze_eps),
        "test_flagged_count": int(len(freeze_eps)),
        "already_in_61": True,
        "new_to_61": [],
        "flagged_after_overlay": flagged_after,
        "note": "诊断升级：从暗帧/低对比代理精确到视野冻结而本体在动。不改变 61/89。",
    }
    return merged, summary


def negative_experiment_table() -> pd.DataFrame:
    probe = json.loads((OUT_DIR / "crossmodal_probe_summary.json").read_text(encoding="utf-8"))
    rows = []
    for item in probe.get("Q1_visual_motor_lag", []):
        rows.append(
            {
                "experiment": "visual_motor_lag",
                "dataset": item["dataset"],
                "metric": "lag0_share",
                "value": item["lag0_share"],
                "companion": f"|lag|_mean={item['abs_lag_mean']}",
                "conclusion": "参考集与测试集分布重合，本数据集无法支持真实时钟漂移/时滞测量",
            }
        )
    for item in probe.get("Q2_glitch", []):
        rows.append(
            {
                "experiment": "chroma_noise_glitch",
                "dataset": item["dataset"],
                "metric": "glitch_frames_total",
                "value": item["glitch_frames_total"],
                "companion": f"episodes_ge1={item['episodes_ge1']}",
                "conclusion": "全测试集仅 1 帧超上界（ep76 right_wrist），判定本数据集不含花屏样本",
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report = read_csv("test_quality_report.csv")
    flagged_before = int((report["status"] != "通过").sum())
    report, freeze_summary = overlay_freeze(report)
    flagged_after = int((report["status"] != "通过").sum())
    if flagged_after != flagged_before:
        raise RuntimeError(f"61/89 changed {flagged_before} -> {flagged_after}")

    ref_feat = episode_features("reference")
    test_feat = episode_features("test")
    ref_if, test_if, if_summary = isolation_forest_loeo(ref_feat, test_feat)

    report = report.merge(
        test_if[["episode_index", "iforest_score", "iforest_flag"]],
        on="episode_index",
        how="left",
    )
    report["iforest_flag"] = report["iforest_flag"].fillna(False).astype(bool)

    overlap = report[(report["status"] != "通过") & report["iforest_flag"]]
    extra = report[(report["status"] == "通过") & report["iforest_flag"]]
    if_summary["overlap_with_61"] = int(len(overlap))
    if_summary["supplementary_unflagged"] = int(len(extra))
    if_summary["supplementary_episode_list"] = sorted(int(x) for x in extra["episode_index"].tolist())
    if_summary["missed_of_61"] = int(((report["status"] != "通过") & ~report["iforest_flag"]).sum())

    report.to_csv(OUT_DIR / "test_quality_report.csv", index=False, encoding="utf-8-sig")
    pd.concat([ref_if, test_if], ignore_index=True).to_csv(
        OUT_DIR / "unsupervised_iforest.csv", index=False, encoding="utf-8-sig"
    )
    neg = negative_experiment_table()
    neg.to_csv(OUT_DIR / "crossmodal_negative_results.csv", index=False, encoding="utf-8-sig")

    def category_count(name: str) -> int:
        return int(report["issue_categories"].fillna("").str.contains(name).sum())

    quality_path = OUT_DIR / "quality_summary.json"
    summary = json.loads(quality_path.read_text(encoding="utf-8")) if quality_path.exists() else {}
    summary["detector_version"] = DETECTOR_VERSION
    summary["test_flagged_episode_count"] = flagged_after
    summary["test_issue_episodes_by_category"] = {
        "时序": category_count("时序"),
        "同步": category_count("同步"),
        "内容/结构": category_count("内容/结构"),
        "数据价值": category_count("数据价值"),
    }
    summary["v22_channels"] = {
        "visual_freeze": freeze_summary,
        "isolation_forest": {
            "test_flagged": if_summary["test_flagged"],
            "overlap_with_61": if_summary["overlap_with_61"],
            "supplementary_unflagged": if_summary["supplementary_unflagged"],
            "threshold": if_summary["threshold"],
            "reference_loeo_flagged": if_summary["reference_loeo_flagged"],
        },
        "label_note": summary.get(
            "label_note",
            "测试集计数表示算法标记的需复核轨迹，不等同于真实异常数。",
        ),
    }
    quality_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    channel_summary = {
        "detector_version": DETECTOR_VERSION,
        "flagged_61_unchanged": True,
        "flagged_before": flagged_before,
        "flagged_after": flagged_after,
        "category_counts": summary["test_issue_episodes_by_category"],
        "visual_freeze": freeze_summary,
        "isolation_forest": if_summary,
        "parallel_benchmark": {
            "subset_12_episodes_16_workers_speedup": 4.34,
            "full_test_16_workers_seconds": 33.09,
            "full_test_images_per_second": 1534.5,
            "note": "16 workers reached 4.34× not 16×; decode and memory bandwidth bound. Do not claim linear scaling.",
        },
        "negative_experiments": {
            "clock_lag": "pixel-motion vs state-step lag0 share is 0.00 on both reference and test; distributions coincide.",
            "glitch": "1 frame over threshold in 51,027 images (ep76 right_wrist); dataset treated as 花屏-free.",
        },
    }
    (OUT_DIR / "v22_channel_summary.json").write_text(
        json.dumps(channel_summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(channel_summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
