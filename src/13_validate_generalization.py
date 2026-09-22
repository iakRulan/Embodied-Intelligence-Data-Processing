"""Leave-one-episode-out and deterministic synthetic-fault validation."""

from __future__ import annotations

import importlib.util
import io
import json
import math
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pandas as pd
from PIL import Image
from sklearn.metrics import precision_recall_fscore_support


ROOT = Path(__file__).resolve().parents[1]
from dataset_paths import OUT_DIR

SEED = 20260827


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STRUCT = load_module("profile_structural_val", ROOT / "src" / "03_profile_structural.py")
IMAGE_PROFILE = load_module("profile_images_val", ROOT / "src" / "04_profile_images.py")
VALUES = load_module("profile_values_val", ROOT / "src" / "05_profile_values.py")
DETECT = load_module("detect_score_val", ROOT / "src" / "06_detect_and_score.py")

IMAGE_COLS = ["image", "left_wrist_image", "right_wrist_image"]
CATEGORIES = ["时序", "同步", "内容/结构", "数据价值"]


def png_bytes(level: int, size: tuple[int, int] = (224, 224)) -> bytes:
    array = np.full((size[1], size[0], 3), level, dtype=np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format="PNG")
    return buffer.getvalue()


def resize_payload(value: Any, size: tuple[int, int]) -> dict[str, Any]:
    result = dict(value)
    with Image.open(io.BytesIO(result["bytes"])) as image:
        buffer = io.BytesIO()
        image.convert("RGB").resize(size, Image.Resampling.BILINEAR).save(buffer, format="PNG")
    result["bytes"] = buffer.getvalue()
    return result


def clone_images(df: pd.DataFrame) -> None:
    for col in IMAGE_COLS:
        df[col] = [dict(value) if isinstance(value, dict) else value for value in df[col]]


def clone_arrays(df: pd.DataFrame) -> None:
    for col in ["state", "actions"]:
        df[col] = [np.asarray(value, dtype=np.float32).copy() for value in df[col]]


def inject_fault(df: pd.DataFrame, fault_index: int, synthetic_ep: int) -> dict[str, Any]:
    clone_images(df)
    clone_arrays(df)
    df["episode_index"] = synthetic_ep
    n = len(df)
    row = min(max(8, n // 3), n - 8)
    label: dict[str, Any] = {"synthetic_episode": synthetic_ep, "source_episode": fault_index, "frame_evaluable": True}

    if fault_index == 0:
        df.loc[row, "timestamp"] = float(df.loc[row, "timestamp"]) + 0.03
        label.update(fault="timestamp_jitter", categories=["时序"], rows=[row, row + 1])
    elif fault_index == 1:
        df.loc[row, "timestamp"] = float(df.loc[row - 1, "timestamp"]) - 0.05
        label.update(fault="timestamp_rollback", categories=["时序"], rows=[row, row + 1])
    elif fault_index == 2:
        start = float(df.timestamp.iloc[0])
        df["timestamp"] = (start + np.arange(n) * 0.12).astype(np.float32)
        label.update(fault="timestamp_rate_drift", categories=["时序"], rows=list(range(1, n)))
    elif fault_index == 3:
        df.loc[row, "timestamp"] = float(df.loc[row - 1, "timestamp"])
        label.update(fault="timestamp_duplicate", categories=["时序"], rows=[row, row + 1])
    elif fault_index == 4:
        df.loc[row:, "timestamp"] = pd.to_numeric(df.loc[row:, "timestamp"], errors="coerce") + 0.2
        label.update(fault="timestamp_gap", categories=["时序"], rows=[row])
    elif fault_index == 5:
        df.loc[row, "task_index"] = 2
        label.update(fault="task_index_invalid", categories=["同步"], rows=[row])
    elif fault_index == 6:
        current = int(df.task_index.mode().iloc[0])
        df.loc[row:, "task_index"] = 1 - current
        label.update(fault="task_switch", categories=["同步"], rows=list(range(row, n)), frame_evaluable=False)
    elif fault_index == 7:
        value = dict(df.at[row, "image"])
        value["path"] = "frame_999999.png"
        df.at[row, "image"] = value
        label.update(fault="single_path_mismatch", categories=["同步"], rows=[row])
    elif fault_index == 8:
        for idx in range(n):
            value = dict(df.at[idx, "left_wrist_image"])
            value["path"] = f"frame_{idx + 1:06d}.png"
            df.at[idx, "left_wrist_image"] = value
        label.update(fault="stream_path_offset", categories=["同步"], rows=list(range(n)))
    elif fault_index == 9:
        df.loc[row, "episode_index"] = synthetic_ep + 1
        label.update(fault="episode_index_mismatch", categories=["同步"], rows=[row], frame_evaluable=False)
    elif fault_index == 10:
        for idx in range(row, row + 3):
            for col in IMAGE_COLS:
                value = dict(df.at[idx, col]); value["bytes"] = png_bytes(0); df.at[idx, col] = value
        label.update(fault="black_frames", categories=["内容/结构"], rows=list(range(row, row + 3)))
    elif fault_index == 11:
        for idx in range(row, row + 3):
            for col in IMAGE_COLS:
                value = dict(df.at[idx, col]); value["bytes"] = png_bytes(255); df.at[idx, col] = value
        label.update(fault="bright_frames", categories=["内容/结构"], rows=list(range(row, row + 3)))
    elif fault_index == 12:
        df.at[row, "image"] = resize_payload(df.at[row, "image"], (112, 112))
        label.update(fault="image_shape", categories=["内容/结构"], rows=[row])
    elif fault_index == 13:
        for col in IMAGE_COLS:
            df.at[row, col] = dict(df.at[row - 1, col])
        label.update(fault="exact_duplicate", categories=["内容/结构"], rows=[row])
    elif fault_index == 14:
        value = dict(df.at[row, "image"]); value["bytes"] = b"not-a-valid-png"; df.at[row, "image"] = value
        label.update(fault="corrupt_payload", categories=["内容/结构"], rows=[row])
    elif fault_index == 15:
        value = np.asarray(df.at[row, "state"], dtype=np.float32).copy(); value[0] = np.nan; df.at[row, "state"] = value
        label.update(fault="state_nan", categories=["数据价值"], rows=[row])
    elif fault_index == 16:
        value = np.asarray(df.at[row, "state"], dtype=np.float32).copy(); value[0] = 1.8; df.at[row, "state"] = value
        label.update(fault="position_spike", categories=["数据价值"], rows=[row, row + 1])
    elif fault_index == 17:
        value = np.asarray(df.at[row, "state"], dtype=np.float32).copy(); value[3:9] = 0; df.at[row, "state"] = value
        label.update(fault="rotation_invalid", categories=["数据价值"], rows=[row, row + 1])
    elif fault_index == 18:
        value = np.asarray(df.at[row, "actions"], dtype=np.float32).copy(); value[9] = 1.5; df.at[row, "actions"] = value
        label.update(fault="gripper_out_of_range", categories=["数据价值"], rows=[row, row + 1])
    elif fault_index == 19:
        value = np.asarray(df.at[row, "actions"], dtype=np.float32).copy(); value[0] = np.clip(value[0] + 0.75, -0.95, 0.95); df.at[row, "actions"] = value
        label.update(fault="action_jump", categories=["数据价值"], rows=[row, row + 1])
    return label


def classification_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    precision, recall, f1, _ = precision_recall_fscore_support(y_true, y_pred, average="binary", zero_division=0)
    return {"precision": float(precision), "recall": float(recall), "f1": float(f1)}


def bootstrap_ci(y_true: np.ndarray, y_pred: np.ndarray, metric: str, iterations: int = 2000) -> tuple[float, float]:
    rng = np.random.default_rng(SEED)
    values = []
    for _ in range(iterations):
        idx = rng.integers(0, len(y_true), len(y_true))
        if len(np.unique(y_true[idx])) < 2:
            continue
        values.append(classification_metrics(y_true[idx], y_pred[idx])[metric])
    if not values:
        return math.nan, math.nan
    return float(np.quantile(values, 0.025)), float(np.quantile(values, 0.975))


def main() -> None:
    ref_struct = pd.read_csv(OUT_DIR / "reference_structural_episodes.csv", encoding="utf-8-sig")
    ref_sf = pd.read_csv(OUT_DIR / "reference_structural_frames.csv", encoding="utf-8-sig")
    ref_image = pd.read_csv(OUT_DIR / "reference_image_episodes.csv", encoding="utf-8-sig")
    ref_imf = pd.read_csv(OUT_DIR / "reference_image_frames.csv", encoding="utf-8-sig", low_memory=False)
    ref_values = pd.read_csv(OUT_DIR / "reference_value_episodes.csv", encoding="utf-8-sig")
    ref_vf = pd.read_csv(OUT_DIR / "reference_value_frames.csv", encoding="utf-8-sig")
    source_root = Path(str(ref_struct.source_file.dropna().iloc[0])).parents[2]

    # Leave-one-episode-out normal-data validation.
    loo_rows = []
    for ep in sorted(ref_struct.episode_index.astype(int)):
        thresholds = DETECT.reference_thresholds(
            ref_image[ref_image.episode_index.ne(ep)],
            ref_values[ref_values.episode_index.ne(ep)],
            ref_vf[ref_vf.dataset_episode.ne(ep)],
            ref_imf[ref_imf.dataset_episode.ne(ep)],
        )
        sf = ref_sf[ref_sf.episode_index_file.eq(ep)]
        imf = ref_imf[ref_imf.dataset_episode.eq(ep)]
        vf = ref_vf[ref_vf.dataset_episode.eq(ep)]
        flags = DETECT.build_frame_flags(sf, imf, vf, thresholds)
        report = DETECT.episode_report(
            ref_struct[ref_struct.episode_index.eq(ep)],
            ref_image[ref_image.episode_index.eq(ep)],
            ref_values[ref_values.episode_index.eq(ep)],
            flags,
            thresholds,
            "reference_loo",
        ).iloc[0]
        loo_rows.append({"episode_index": ep, "status": report.status, "issue_codes": report.issue_codes, "score": report.overall_quality_score})
    loo = pd.DataFrame(loo_rows)
    loo.to_csv(OUT_DIR / "reference_leave_one_out.csv", index=False, encoding="utf-8-sig")

    thresholds = DETECT.reference_thresholds(ref_image, ref_values, ref_vf, ref_imf)
    clean_s = ref_struct.copy(); clean_s["episode_index"] += 1000; clean_s["episode_index_mismatch_count"] = 0
    clean_sf = ref_sf.copy(); clean_sf["episode_index_file"] += 1000; clean_sf["episode_index_value"] += 1000
    clean_i = ref_image.copy(); clean_i["episode_index"] += 1000
    clean_imf = ref_imf.copy(); clean_imf["dataset_episode"] += 1000
    clean_v = ref_values.copy(); clean_v["episode_index"] += 1000
    clean_vf = ref_vf.copy(); clean_vf["dataset_episode"] += 1000

    corrupt_s: list[dict[str, Any]] = []
    corrupt_sf: list[dict[str, Any]] = []
    corrupt_i: list[dict[str, Any]] = []
    corrupt_imf: list[dict[str, Any]] = []
    corrupt_v: list[dict[str, Any]] = []
    corrupt_vf: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="refsync_synthetic_") as temp_dir:
        temp = Path(temp_dir)
        for ep in range(20):
            source = source_root / "data" / "chunk-000" / f"episode_{ep:06d}.parquet"
            df = pd.read_parquet(source)
            synthetic_ep = 2000 + ep
            label = inject_fault(df, ep, synthetic_ep)
            path = temp / f"episode_{synthetic_ep:06d}.parquet"
            df.to_parquet(path, index=False)
            se, sf = STRUCT.profile_episode(path, {"length": len(df)}, 0.1)
            ie, imf = IMAGE_PROFILE.profile_episode(path)
            vf, ve = VALUES.profile_episode(path)
            for record in [se, ie, ve]:
                record["read_ok"] = True; record["read_error"] = ""; record["source_file"] = f"synthetic/{path.name}"; record["dataset"] = "synthetic"
            corrupt_s.append(se); corrupt_i.append(ie); corrupt_v.append(ve)
            corrupt_sf.extend(sf); corrupt_imf.extend(imf); corrupt_vf.extend(vf); labels.append(label)

    s = pd.concat([clean_s, pd.DataFrame(corrupt_s)], ignore_index=True, sort=False)
    sf = pd.concat([clean_sf, pd.DataFrame(corrupt_sf)], ignore_index=True, sort=False)
    i = pd.concat([clean_i, pd.DataFrame(corrupt_i)], ignore_index=True, sort=False)
    imf = pd.concat([clean_imf, pd.DataFrame(corrupt_imf)], ignore_index=True, sort=False)
    v = pd.concat([clean_v, pd.DataFrame(corrupt_v)], ignore_index=True, sort=False)
    vf = pd.concat([clean_vf, pd.DataFrame(corrupt_vf)], ignore_index=True, sort=False)
    flags = DETECT.build_frame_flags(sf, imf, vf, thresholds)
    report = DETECT.episode_report(s, i, v, flags, thresholds, "synthetic_benchmark")
    label_by_ep = {int(row["synthetic_episode"]): row for row in labels}
    report["ground_truth_anomaly"] = report.episode_index.ge(2000)
    report["predicted_flag"] = report.status.ne("通过")
    report["fault"] = report.episode_index.map(lambda ep: label_by_ep.get(int(ep), {}).get("fault", "clean"))
    report["ground_truth_categories"] = report.episode_index.map(lambda ep: ";".join(label_by_ep.get(int(ep), {}).get("categories", [])))
    report.to_csv(OUT_DIR / "synthetic_episode_results.csv", index=False, encoding="utf-8-sig")

    metric_rows: list[dict[str, Any]] = []
    y_true = report.ground_truth_anomaly.astype(int).to_numpy()
    y_pred = report.predicted_flag.astype(int).to_numpy()
    overall_metrics = classification_metrics(y_true, y_pred)
    for metric, value in overall_metrics.items():
        low, high = bootstrap_ci(y_true, y_pred, metric)
        metric_rows.append({"scope": "synthetic_episode", "category": "overall", "metric": metric, "value": value, "ci95_low": low, "ci95_high": high, "n": len(y_true)})
    for category in CATEGORIES:
        true_cat = report.ground_truth_categories.fillna("").str.contains(category).astype(int).to_numpy()
        pred_cat = report.issue_categories.fillna("").str.contains(category).astype(int).to_numpy()
        metrics = classification_metrics(true_cat, pred_cat)
        for metric, value in metrics.items():
            low, high = bootstrap_ci(true_cat, pred_cat, metric)
            metric_rows.append({"scope": "synthetic_episode", "category": category, "metric": metric, "value": value, "ci95_low": low, "ci95_high": high, "n": len(true_cat)})

    frame_truth = flags[["episode_index", "row", "issue_codes"]].copy()
    frame_truth["ground_truth"] = False
    frame_truth["frame_evaluable"] = True
    for label in labels:
        ep = int(label["synthetic_episode"])
        if not label.get("frame_evaluable", True):
            frame_truth.loc[frame_truth.episode_index.eq(ep), "frame_evaluable"] = False
            continue
        rows = set(int(x) for x in label["rows"])
        frame_truth.loc[frame_truth.episode_index.eq(ep) & frame_truth.row.isin(rows), "ground_truth"] = True
    eval_frames = frame_truth[frame_truth.frame_evaluable].copy()
    eval_frames["predicted"] = eval_frames.issue_codes.fillna("").ne("")
    frame_metrics = classification_metrics(eval_frames.ground_truth.astype(int).to_numpy(), eval_frames.predicted.astype(int).to_numpy())
    for metric, value in frame_metrics.items():
        metric_rows.append({"scope": "synthetic_frame", "category": "overall", "metric": metric, "value": value, "ci95_low": math.nan, "ci95_high": math.nan, "n": len(eval_frames)})
    frame_truth.to_csv(OUT_DIR / "synthetic_frame_results.csv", index=False, encoding="utf-8-sig")
    metrics_df = pd.DataFrame(metric_rows)
    metrics_df.to_csv(OUT_DIR / "validation_metrics.csv", index=False, encoding="utf-8-sig")

    summary = {
        "reference_leave_one_out_false_positive_rate": round(float(loo.status.ne("通过").mean()), 4),
        "reference_leave_one_out_flagged": int(loo.status.ne("通过").sum()),
        "reference_leave_one_out_n": int(len(loo)),
        "synthetic_episode_n": int(len(report)),
        "synthetic_corrupted_episodes": int(report.ground_truth_anomaly.sum()),
        "synthetic_clean_episodes": int((~report.ground_truth_anomaly).sum()),
        "synthetic_episode_precision": round(overall_metrics["precision"], 4),
        "synthetic_episode_recall": round(overall_metrics["recall"], 4),
        "synthetic_episode_f1": round(overall_metrics["f1"], 4),
        "synthetic_frame_precision": round(frame_metrics["precision"], 4),
        "synthetic_frame_recall": round(frame_metrics["recall"], 4),
        "synthetic_frame_f1": round(frame_metrics["f1"], 4),
        "scope_note": "Synthetic metrics measure recovery of deterministic injected faults on clean reference trajectories; they are not official hidden-test metrics.",
    }
    (OUT_DIR / "validation_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
