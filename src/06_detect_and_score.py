"""Fuse structural, image and robot-state features into the competition report.

The detector is deliberately calibrated on the supplied clean reference set.
It uses hard schema/physics checks plus robust, interpretable thresholds rather
than a black-box classifier.  This is appropriate for the initial round: the
test labels are not released, while the reference set is explicitly described
as clean.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd
import seaborn as sns

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dataset_paths import FIG_DIR, OUT_DIR, resolve_reference_root, resolve_test_root

DEFAULT_REF = resolve_reference_root()
DEFAULT_TEST = resolve_test_root()

STREAMS = ["image", "left_wrist", "right_wrist"]
CAT_TEMPORAL = "时序"
CAT_SYNC = "同步"
CAT_CONTENT = "内容/结构"
CAT_VALUE = "数据价值"
DETECTOR_VERSION = "RefSync-QA-v2.2"


def read_csv(name: str) -> pd.DataFrame:
    return pd.read_csv(OUT_DIR / name, encoding="utf-8-sig")


def value(row: pd.Series, key: str, default: Any = 0.0) -> Any:
    if key not in row.index:
        return default
    x = row[key]
    if pd.isna(x):
        return default
    return x


def num(row: pd.Series, key: str, default: float = 0.0) -> float:
    try:
        x = float(value(row, key, default))
        return default if not np.isfinite(x) else x
    except (TypeError, ValueError):
        return default


def bool_value(row: pd.Series, key: str, default: bool = False) -> bool:
    if key not in row.index or pd.isna(row[key]):
        return default
    x = row[key]
    if isinstance(x, bool):
        return x
    return str(x).strip().lower() in {"true", "1", "yes"}


def bool_series(series: pd.Series, default: bool = False) -> pd.Series:
    """Parse CSV booleans without treating the string ``False`` as true."""

    if series is None:
        return pd.Series(dtype=bool)
    if pd.api.types.is_bool_dtype(series):
        return series.fillna(default).astype(bool)
    return series.fillna(str(default)).astype(str).str.strip().str.lower().isin({"true", "1", "yes"})


def safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0 or not np.isfinite(denominator):
        return 1.0 if numerator > 0 else 0.0
    return float(np.clip(numerator / denominator, 0.0, 1.0))


def add_issue(codes: list[str], categories: list[str], details: list[str], code: str, category: str, detail: str) -> None:
    if code not in codes:
        codes.append(code)
    if category not in categories:
        categories.append(category)
    if detail and detail not in details:
        details.append(detail)


def reference_thresholds(ref_image: pd.DataFrame, ref_values: pd.DataFrame, ref_value_frames: pd.DataFrame, ref_image_frames: pd.DataFrame | None = None) -> dict[str, Any]:
    thresholds: dict[str, Any] = {
        "nominal_dt": 0.1,
        "timestamp_jitter_abs": 0.01,
        "timestamp_median_tolerance": 0.002,
        "timestamp_mad_tolerance": 0.001,
        "state_action_rmse": 0.15,
        "state_action_max_abs": 0.40,
        "state_step_l2": 0.50,
        "action_step_l2": 0.50,
        "state_abs_max": 1.05,
        "action_abs_max": 1.05,
        "low_motion_state_path": 0.50,
        "low_motion_action_path": 1.00,
        "bad_ratio_sparse": 0.10,
        "calibration_method": "reference robust quantile + engineering safety floor; evaluated by leave-one-episode-out",
    }
    if not ref_value_frames.empty:
        for col, key, floor, multiplier in [
            ("state_action_aligned_rmse", "state_action_rmse", 0.15, 1.25),
            ("state_action_aligned_max_abs", "state_action_max_abs", 0.40, 1.25),
            ("state_step_l2", "state_step_l2", 0.50, 1.5),
            ("action_step_l2", "action_step_l2", 0.50, 1.5),
        ]:
            source_col = col if col in ref_value_frames else col.replace("aligned_", "")
            x = pd.to_numeric(ref_value_frames[source_col], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
            if not x.empty:
                robust_high = float(x.quantile(0.999))
                thresholds[key] = max(floor, robust_high * multiplier)
    for stream in STREAMS:
        lap_col = f"{stream}_laplacian_var_median"
        grad_col = f"{stream}_gradient_energy_median"
        byte_col = f"{stream}_byte_size_median"
        # A lower-tail quantile with a conservative margin is less sensitive
        # to a single calibration episode than the previous minimum anchor.
        thresholds[f"{stream}_lap_min"] = float(pd.to_numeric(ref_image[lap_col], errors="coerce").dropna().quantile(0.10) * 0.65) if lap_col in ref_image else 0.0
        thresholds[f"{stream}_grad_min"] = float(pd.to_numeric(ref_image[grad_col], errors="coerce").dropna().quantile(0.10) * 0.75) if grad_col in ref_image else 0.0
        thresholds[f"{stream}_byte_min"] = float(pd.to_numeric(ref_image[byte_col], errors="coerce").dropna().quantile(0.10) * 0.75) if byte_col in ref_image else 0.0
        stream_name = "left_wrist_image" if stream == "left_wrist" else "right_wrist_image" if stream == "right_wrist" else "image"
        frame_rows = ref_image_frames[ref_image_frames.stream.eq(stream_name)] if ref_image_frames is not None and not ref_image_frames.empty else pd.DataFrame()
        for source_col, suffix, quantile, margin in [
            ("laplacian_var", "lap_frame_min", 0.002, 0.50),
            ("gradient_energy", "grad_frame_min", 0.002, 0.65),
            ("byte_size", "byte_frame_min", 0.002, 0.50),
        ]:
            x = pd.to_numeric(frame_rows.get(source_col), errors="coerce").dropna() if not frame_rows.empty else pd.Series(dtype=float)
            thresholds[f"{stream}_{suffix}"] = float(x.quantile(quantile) * margin) if not x.empty else thresholds[f"{stream}_{suffix.replace('_frame', '')}"]
    return thresholds


def image_stream_quality(frame: pd.DataFrame, thresholds: dict[str, Any]) -> pd.DataFrame:
    """Add row-level image flags; missing columns are treated as unknown."""

    if frame.empty:
        return frame
    out = frame.copy()
    out["path_delta_num"] = pd.to_numeric(out.get("path_delta", np.nan), errors="coerce")
    out["path_missing"] = out.get("path", pd.Series("", index=out.index)).fillna("").astype(str).str.strip().eq("")
    out["path_mismatch"] = out["path_missing"] | out["path_delta_num"].fillna(0).ne(0)
    decode_ok = bool_series(out["decode_ok"])
    payload_present = bool_series(out["payload_present"])
    out["payload_missing"] = ~payload_present
    out["decode_corrupt"] = payload_present & ~decode_ok
    out["decode_bad"] = ~decode_ok
    out["shape_bad"] = (
        decode_ok
        & (pd.to_numeric(out["width"], errors="coerce").ne(224)
        | pd.to_numeric(out["height"], errors="coerce").ne(224)
        | pd.to_numeric(out["channels"], errors="coerce").ne(3))
    )
    out["screen_bad"] = (
        decode_ok
        & (pd.to_numeric(out["dark_fraction"], errors="coerce").ge(0.95)
        | pd.to_numeric(out["bright_fraction"], errors="coerce").ge(0.95)
        | pd.to_numeric(out["std_luma"], errors="coerce").le(5.0))
    )
    out["duplicate_bad"] = bool_series(out["adjacent_exact_duplicate"]) & decode_ok
    out["blur_bad"] = False
    for stream in STREAMS:
        mask = out["stream"].eq("left_wrist_image" if stream == "left_wrist" else "right_wrist_image" if stream == "right_wrist" else "image")
        lap = pd.to_numeric(out["laplacian_var"], errors="coerce")
        grad = pd.to_numeric(out["gradient_energy"], errors="coerce")
        byte_size = pd.to_numeric(out["byte_size"], errors="coerce")
        lap_min = thresholds.get(f"{stream}_lap_frame_min", thresholds.get(f"{stream}_lap_min", 0.0))
        grad_min = thresholds.get(f"{stream}_grad_frame_min", thresholds.get(f"{stream}_grad_min", 0.0))
        byte_min = thresholds.get(f"{stream}_byte_frame_min", thresholds.get(f"{stream}_byte_min", 0.0))
        bad = decode_ok & (
            (lap.lt(lap_min) & grad.lt(grad_min))
            | lap.lt(0.25 * lap_min)
            | byte_size.lt(0.2 * byte_min)
        )
        out.loc[mask, "blur_bad"] = bad.loc[mask]
    return out


def build_frame_flags(structure: pd.DataFrame, image: pd.DataFrame, values: pd.DataFrame, thresholds: dict[str, Any]) -> pd.DataFrame:
    sf = structure.copy()
    sf = sf.rename(columns={"episode_index_file": "episode_index"})
    sf["row"] = pd.to_numeric(sf["row"], errors="coerce").astype("Int64")
    for c in ["dt", "frame_diff", "global_index_diff", "timestamp", "task_index"]:
        sf[c] = pd.to_numeric(sf[c], errors="coerce")
    vf = values.copy()
    vf = vf.rename(columns={"dataset_episode": "episode_index"})
    vf["row"] = pd.to_numeric(vf["row"], errors="coerce").astype("Int64")
    if not image.empty:
        im = image_stream_quality(image, thresholds)
        im["episode_index"] = pd.to_numeric(im["dataset_episode"], errors="coerce").astype("Int64")
        im["row"] = pd.to_numeric(im["row"], errors="coerce").astype("Int64")
        im["image_problem"] = im[["payload_missing", "decode_corrupt", "shape_bad", "screen_bad", "blur_bad", "duplicate_bad", "path_mismatch"]].any(axis=1)
        ig = im.groupby(["episode_index", "row"], as_index=False).agg(
            image_decode_bad=("decode_bad", "sum"),
            image_payload_missing=("payload_missing", "sum"),
            image_decode_corrupt=("decode_corrupt", "sum"),
            image_shape_bad=("shape_bad", "sum"),
            image_screen_bad=("screen_bad", "sum"),
            image_blur_bad=("blur_bad", "sum"),
            image_duplicate_bad=("duplicate_bad", "sum"),
            image_path_mismatch=("path_mismatch", "sum"),
            image_problem_streams=("image_problem", "sum"),
        )
    else:
        ig = pd.DataFrame(columns=["episode_index", "row", "image_decode_bad", "image_payload_missing", "image_decode_corrupt", "image_shape_bad", "image_screen_bad", "image_blur_bad", "image_duplicate_bad", "image_path_mismatch", "image_problem_streams"])

    merged = sf.merge(vf, on=["episode_index", "row"], how="left", suffixes=("", "_value"))
    merged = merged.merge(ig, on=["episode_index", "row"], how="left")
    for col in ["image_decode_bad", "image_payload_missing", "image_decode_corrupt", "image_shape_bad", "image_screen_bad", "image_blur_bad", "image_duplicate_bad", "image_path_mismatch", "image_problem_streams"]:
        merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(0)

    records: list[dict[str, Any]] = []
    for _, r in merged.iterrows():
        codes: list[str] = []
        details: list[str] = []
        dt = num(r, "dt", math.nan)
        frame_diff = num(r, "frame_diff", math.nan)
        global_index_diff = num(r, "global_index_diff", math.nan)
        task_index = num(r, "task_index", math.nan)
        if np.isfinite(dt) and (dt <= 0 or dt > 0.15):
            add_issue(codes, [], details, "T_TIMESTAMP_NON_MONOTONIC" if dt <= 0 else "T_TIMESTAMP_GAP", CAT_TEMPORAL, "timestamp 间隔异常")
        elif np.isfinite(dt) and abs(dt - thresholds["nominal_dt"]) > thresholds["timestamp_jitter_abs"]:
            add_issue(codes, [], details, "T_TIMESTAMP_JITTER", CAT_TEMPORAL, "timestamp 抖动/帧率偏离")
        if np.isfinite(frame_diff) and frame_diff != 1:
            add_issue(codes, [], details, "T_FRAME_INDEX_DISCONTINUITY", CAT_TEMPORAL, "frame_index 非连续")
        if np.isfinite(global_index_diff) and global_index_diff != 1:
            add_issue(codes, [], details, "T_GLOBAL_INDEX_DISCONTINUITY", CAT_TEMPORAL, "全局 index 非连续")
        if np.isfinite(task_index) and task_index not in {0, 1}:
            add_issue(codes, [], details, "S_TASK_INDEX_INVALID", CAT_SYNC, "task_index 超出元数据定义")

        if bool(r.get("image_path_mismatch", 0)):
            add_issue(codes, [], details, "S_IMAGE_PATH_MISMATCH", CAT_SYNC, "图像 path 与 frame_index 不一致")
        if bool(r.get("image_payload_missing", 0)):
            add_issue(codes, [], details, "S_MODALITY_COVERAGE_GAP", CAT_SYNC, "至少一路相机 payload 缺失")
        if bool(r.get("image_decode_corrupt", 0)):
            add_issue(codes, [], details, "C_IMAGE_DECODE", CAT_CONTENT, "图像无法解码/缺失")
        if bool(r.get("image_shape_bad", 0)):
            add_issue(codes, [], details, "C_IMAGE_SHAPE", CAT_CONTENT, "图像分辨率或通道数不符合 schema")
        if bool(r.get("image_screen_bad", 0)):
            add_issue(codes, [], details, "C_IMAGE_SCREEN", CAT_CONTENT, "黑屏/过曝/低对比度")
        if bool(r.get("image_blur_bad", 0)):
            add_issue(codes, [], details, "C_IMAGE_BLUR", CAT_CONTENT, "清晰度低/疑似模糊或低分辨率")
        if bool(r.get("image_duplicate_bad", 0)):
            add_issue(codes, [], details, "C_IMAGE_DUPLICATE", CAT_CONTENT, "相邻图像重复")

        state_finite = bool_value(r, "state_finite_ok", True)
        action_finite = bool_value(r, "action_finite_ok", True)
        state_range = bool_value(r, "state_hard_range_bad", False) or bool_value(r, "state_position_bad", False) or bool_value(r, "state_rotation_bad", False) or bool_value(r, "state_gripper_bad", False)
        action_range = bool_value(r, "action_hard_range_bad", False) or bool_value(r, "action_position_bad", False) or bool_value(r, "action_rotation_bad", False) or bool_value(r, "action_gripper_bad", False)
        if not state_finite or not action_finite:
            add_issue(codes, [], details, "V_NONFINITE", CAT_VALUE, "state/actions 含 NaN 或 Inf")
        if state_range or action_range:
            add_issue(codes, [], details, "V_PHYSICAL_RANGE", CAT_VALUE, "state/actions 违反物理范围或 Rotation-6D 约束")
        aligned_rmse = num(r, "state_action_aligned_rmse", num(r, "state_action_rmse", 0.0))
        aligned_max = num(r, "state_action_aligned_max_abs", num(r, "state_action_max_abs", 0.0))
        if aligned_rmse > thresholds["state_action_rmse"] or aligned_max > thresholds["state_action_max_abs"]:
            add_issue(codes, [], details, "V_STATE_ACTION_MISMATCH", CAT_VALUE, "state-actions 最佳相邻帧对齐残差过大")
        if num(r, "state_step_l2", 0.0) > thresholds["state_step_l2"] or num(r, "action_step_l2", 0.0) > thresholds["action_step_l2"]:
            add_issue(codes, [], details, "V_SPIKE_JUMP", CAT_VALUE, "机器人状态/动作出现突跳")
        if codes:
            records.append(
                {
                    "episode_index": int(r["episode_index"]),
                    "row": int(r["row"]),
                    "timestamp": num(r, "timestamp", math.nan),
                    "issue_codes": ";".join(codes),
                    "issue_categories": ";".join(dict.fromkeys(
                        CAT_TEMPORAL if code.startswith("T_") else CAT_SYNC if code.startswith("S_") else CAT_CONTENT if code.startswith("C_") else CAT_VALUE
                        for code in codes
                    )),
                    "detail": ";".join(details),
                    "image_problem_streams": int(r.get("image_problem_streams", 0)),
                }
            )
        else:
            records.append(
                {
                    "episode_index": int(r["episode_index"]),
                    "row": int(r["row"]),
                    "timestamp": num(r, "timestamp", math.nan),
                    "issue_codes": "",
                    "issue_categories": "",
                    "detail": "",
                    "image_problem_streams": int(r.get("image_problem_streams", 0)),
                }
            )
    return pd.DataFrame(records)


def episode_report(structure: pd.DataFrame, image: pd.DataFrame, values: pd.DataFrame, frames: pd.DataFrame, thresholds: dict[str, Any], label: str) -> pd.DataFrame:
    s = structure.set_index("episode_index", drop=False)
    i = image.set_index("episode_index", drop=False) if not image.empty else pd.DataFrame()
    v = values.set_index("episode_index", drop=False) if not values.empty else pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for ep_id in sorted(set(s.index.astype(int)) | set(i.index.astype(int) if not i.empty else []) | set(v.index.astype(int) if not v.empty else [])):
        sr = s.loc[ep_id] if ep_id in s.index else pd.Series(dtype=object)
        ir = i.loc[ep_id] if not i.empty and ep_id in i.index else pd.Series(dtype=object)
        vr = v.loc[ep_id] if not v.empty and ep_id in v.index else pd.Series(dtype=object)
        if isinstance(sr, pd.DataFrame): sr = sr.iloc[0]
        if isinstance(ir, pd.DataFrame): ir = ir.iloc[0]
        if isinstance(vr, pd.DataFrame): vr = vr.iloc[0]
        codes: list[str] = []
        categories: list[str] = []
        details: list[str] = []
        structural_read_ok = bool_value(sr, "read_ok", True)
        image_read_ok = bool_value(ir, "read_ok", True)
        value_read_ok = bool_value(vr, "read_ok", True)
        read_ok = structural_read_ok and image_read_ok and value_read_ok
        s_error = str(value(sr, "read_error", ""))
        i_error = str(value(ir, "read_error", ""))
        v_error = str(value(vr, "read_error", ""))
        n = num(sr, "rows", 0)
        expected = num(sr, "expected_rows", 0)
        if not bool_value(sr, "read_ok", True):
            if "Parquet" in s_error or "ArrowInvalid" in s_error:
                add_issue(codes, categories, details, "C_PARQUET_CORRUPT", CAT_CONTENT, "Parquet 文件无法读取")
            else:
                add_issue(codes, categories, details, "C_FILE_READ_ERROR", CAT_CONTENT, "结构字段读取失败")
        if not bool_value(ir, "read_ok", True):
            if "FieldRef.Name" in i_error:
                add_issue(codes, categories, details, "C_SCHEMA_MISSING_IMAGE_COLUMN", CAT_CONTENT, "图像字段缺失")
            elif "Parquet" in i_error or "ArrowInvalid" in i_error:
                add_issue(codes, categories, details, "C_PARQUET_CORRUPT", CAT_CONTENT, "图像 Parquet 文件无法读取")
            else:
                add_issue(codes, categories, details, "C_IMAGE_READ_ERROR", CAT_CONTENT, "图像列读取失败")
        if not bool_value(vr, "read_ok", True) and bool_value(sr, "read_ok", True):
            add_issue(codes, categories, details, "C_VALUE_READ_ERROR", CAT_CONTENT, "状态/动作列读取失败")

        length_delta = num(sr, "length_delta", 0.0)
        if abs(length_delta) > 0:
            add_issue(codes, categories, details, "T_METADATA_LENGTH_MISMATCH", CAT_TEMPORAL, f"元数据 length 与实际行数相差 {int(abs(length_delta))}")
        dt_median = num(sr, "dt_median", thresholds["nominal_dt"])
        dt_ratio = dt_median / thresholds["nominal_dt"] if thresholds["nominal_dt"] > 0 else 1.0
        timestamp_unit_mismatch = (
            num(sr, "frame_nonunit_diff_count") == 0
            and num(sr, "dt_nonpositive_count") == 0
            and any(abs(dt_ratio - scale) / scale < 0.05 for scale in (10.0, 100.0, 1000.0))
        )
        if timestamp_unit_mismatch:
            add_issue(codes, categories, details, "T_TIMESTAMP_UNIT_MISMATCH", CAT_TEMPORAL, f"timestamp 采样间隔约为标称值的 {dt_ratio:.0f} 倍，疑似单位错误")
        if num(sr, "dt_nonpositive_count") > 0:
            add_issue(codes, categories, details, "T_TIMESTAMP_NON_MONOTONIC", CAT_TEMPORAL, f"timestamp 重复/倒退 {int(num(sr, 'dt_nonpositive_count'))} 个间隔")
        if (num(sr, "dt_gap_gt_1_5x_count") > 0 and not timestamp_unit_mismatch) or num(sr, "frame_nonunit_diff_count") > 0:
            add_issue(codes, categories, details, "T_DROPPED_OR_GAPPED_FRAMES", CAT_TEMPORAL, f"检测到 {int(max(num(sr, 'dt_gap_gt_1_5x_count'), num(sr, 'frame_nonunit_diff_count')))} 处掉帧/间隔异常")
        if not timestamp_unit_mismatch and (num(sr, "dt_abs_err_gt_2ms_count") > 0 or abs(dt_median - thresholds["nominal_dt"]) > thresholds["timestamp_median_tolerance"] or num(sr, "dt_mad") > thresholds["timestamp_mad_tolerance"]):
            add_issue(codes, categories, details, "T_TIMESTAMP_JITTER_OR_DRIFT", CAT_TEMPORAL, "帧率抖动或时钟漂移")
        if num(sr, "frame_nonpositive_diff_count") > 0 or num(sr, "frame_nonunit_diff_count") > 0:
            add_issue(codes, categories, details, "T_FRAME_ORDER_ERROR", CAT_TEMPORAL, "frame_index 乱序/不连续")
        if num(sr, "index_nonunit_diff_count") > 0:
            add_issue(codes, categories, details, "T_GLOBAL_INDEX_DISCONTINUITY", CAT_TEMPORAL, "全局 index 乱序/不连续")
        if num(sr, "episode_index_mismatch_count") > 0:
            add_issue(codes, categories, details, "S_EPISODE_INDEX_MISMATCH", CAT_SYNC, "episode_index 与文件编号不一致")

        task_unique = num(sr, "task_unique_count", 1)
        task_first = num(sr, "task_index_first", 0)
        if num(sr, "task_nonfinite_count") > 0:
            add_issue(codes, categories, details, "S_TASK_INDEX_MISSING", CAT_SYNC, "task_index 存在缺失/非有限值")
        if task_unique > 1:
            add_issue(codes, categories, details, "S_TASK_SWITCH_WITHIN_EPISODE", CAT_SYNC, "单条轨迹内 task_index 发生切换")
        if task_first not in {0, 1}:
            add_issue(codes, categories, details, "S_TASK_INDEX_INVALID", CAT_SYNC, f"task_index={task_first:g} 不在元数据定义范围")
        for stream in STREAMS:
            if num(ir, f"{stream}_path_mismatch_count") > 0:
                add_issue(codes, categories, details, "S_IMAGE_PATH_MISMATCH", CAT_SYNC, f"{stream} 图像 path 与 frame_index 不一致")
        if num(ir, "any_path_mismatch_count") > 0:
            add_issue(codes, categories, details, "S_MODALITY_INDEX_ALIGNMENT", CAT_SYNC, "图像流与主索引存在对齐偏差")
        missing_images = num(ir, "any_missing_count", 0.0)
        corrupt_images = num(ir, "any_decode_corrupt_count", max(0.0, num(ir, "any_decode_bad_count") - missing_images))
        if missing_images > 0:
            add_issue(codes, categories, details, "S_MODALITY_COVERAGE_GAP", CAT_SYNC, f"至少一路相机 payload 缺失 {int(missing_images)} 张次")

        if corrupt_images > 0:
            add_issue(codes, categories, details, "C_IMAGE_DECODE", CAT_CONTENT, f"图像 payload 损坏/无法解码 {int(corrupt_images)} 张")
        if num(ir, "any_shape_bad_count") > 0:
            add_issue(codes, categories, details, "C_IMAGE_SHAPE", CAT_CONTENT, f"图像 shape 异常 {int(num(ir, 'any_shape_bad_count'))} 张")
        if num(ir, "any_dark_frame_count") > 0 or num(ir, "any_bright_frame_count") > 0 or num(ir, "any_low_contrast_count") > 0:
            add_issue(codes, categories, details, "C_IMAGE_SCREEN", CAT_CONTENT, "检测到黑屏/过曝/低对比度片段")
        if num(ir, "any_path_mismatch_count") > 0:
            pass
        blur_streams: list[str] = []
        for stream in STREAMS:
            lap = num(ir, f"{stream}_laplacian_var_median", math.nan)
            grad = num(ir, f"{stream}_gradient_energy_median", math.nan)
            bytes_med = num(ir, f"{stream}_byte_size_median", math.nan)
            lap_min = thresholds.get(f"{stream}_lap_min", 0.0)
            grad_min = thresholds.get(f"{stream}_grad_min", 0.0)
            byte_min = thresholds.get(f"{stream}_byte_min", 0.0)
            if (
                (np.isfinite(lap) and np.isfinite(grad) and lap < 0.8 * lap_min and grad < 0.95 * grad_min)
                or (np.isfinite(lap) and lap < 0.3 * lap_min)
                or (np.isfinite(bytes_med) and bytes_med < 0.2 * byte_min)
            ):
                blur_streams.append(stream)
        if blur_streams:
            add_issue(codes, categories, details, "C_IMAGE_BLUR", CAT_CONTENT, "低清晰度流: " + ",".join(blur_streams))
        if num(ir, "any_exact_duplicate_adjacent_count") > 0:
            add_issue(codes, categories, details, "C_IMAGE_DUPLICATE", CAT_CONTENT, f"相邻重复图像 {int(num(ir, 'any_exact_duplicate_adjacent_count'))} 张次")

        # Keep the frame-level union available for sparse faults.  Episode
        # p99 statistics are intentionally used for broad degradation, but a
        # single dangerous spike or state/action mismatch must still surface
        # in the episode decision.
        fg = frames[frames["episode_index"].eq(ep_id)] if not frames.empty else pd.DataFrame()
        frame_issue_text = fg["issue_codes"].fillna("").astype(str) if not fg.empty else pd.Series(dtype=str)
        if frame_issue_text.str.contains(r"(?:^|;)C_IMAGE_BLUR(?:;|$)", regex=True).any():
            add_issue(codes, categories, details, "C_IMAGE_BLUR", CAT_CONTENT, "逐帧发现局部低清晰度图像")
        if frame_issue_text.str.contains(r"(?:^|;)C_IMAGE_SHAPE(?:;|$)", regex=True).any():
            add_issue(codes, categories, details, "C_IMAGE_SHAPE", CAT_CONTENT, "逐帧发现图像 shape 异常")

        numeric_bad = 0.0
        physical_bad = 0.0
        if bool_value(vr, "read_ok", True):
            numeric_bad = num(vr, "state_finite_bad_count") + num(vr, "action_finite_bad_count")
            physical_bad = num(vr, "state_hard_range_bad_count") + num(vr, "action_hard_range_bad_count") + num(vr, "state_position_bad_count") + num(vr, "action_position_bad_count") + num(vr, "state_rotation_bad_count") + num(vr, "action_rotation_bad_count") + num(vr, "state_gripper_bad_count") + num(vr, "action_gripper_bad_count")
            if numeric_bad > 0:
                add_issue(codes, categories, details, "V_NONFINITE", CAT_VALUE, f"state/actions 非有限值 {int(numeric_bad)} 帧次")
            if physical_bad > 0:
                add_issue(codes, categories, details, "V_PHYSICAL_RANGE", CAT_VALUE, "数值超范围或 Rotation-6D 不合法")
            aligned_p99 = num(vr, "state_action_aligned_rmse_p99", num(vr, "state_action_rmse_p99", 0.0))
            aligned_max = num(vr, "state_action_aligned_rmse_max", num(vr, "state_action_rmse_max", 0.0))
            aligned_component_max = num(vr, "state_action_aligned_max_abs_max", num(vr, "state_action_max_abs_max", 0.0))
            if aligned_p99 > thresholds["state_action_rmse"] or aligned_max > thresholds["state_action_rmse"] or aligned_component_max > thresholds["state_action_max_abs"]:
                add_issue(codes, categories, details, "V_STATE_ACTION_MISMATCH", CAT_VALUE, "state-actions 在 {-1,0,+1} 帧最佳对齐后残差仍偏大")
            if num(vr, "state_step_p99", 0.0) > thresholds["state_step_l2"] or num(vr, "state_step_max", 0.0) > thresholds["state_step_l2"] or num(vr, "action_step_p99", 0.0) > thresholds["action_step_l2"] or num(vr, "action_step_max", 0.0) > thresholds["action_step_l2"]:
                add_issue(codes, categories, details, "V_SPIKE_JUMP", CAT_VALUE, "state/action 存在突跳")
            if num(sr, "state_path_length", 0.0) < thresholds["low_motion_state_path"] and num(sr, "action_path_length", 0.0) >= thresholds["low_motion_action_path"]:
                add_issue(codes, categories, details, "V_STATE_FREEZE", CAT_VALUE, "状态轨迹几乎无运动/疑似冻结")
            if num(sr, "action_path_length", 0.0) < thresholds["low_motion_action_path"] and num(sr, "state_path_length", 0.0) >= thresholds["low_motion_state_path"]:
                add_issue(codes, categories, details, "V_ACTION_FREEZE", CAT_VALUE, "动作轨迹几乎无运动/疑似冻结")
            if num(sr, "state_path_length", 0.0) < thresholds["low_motion_state_path"] and num(sr, "action_path_length", 0.0) < thresholds["low_motion_action_path"]:
                add_issue(codes, categories, details, "V_LOW_INFORMATION_TRAJECTORY", CAT_VALUE, "状态与动作均缺少有效变化，训练信息量代理偏低")

            # Propagate sparse frame-level value findings to the episode
            # report; otherwise a p99 can hide a one-frame safety violation.
            if frame_issue_text.str.contains(r"(?:^|;)V_STATE_ACTION_MISMATCH(?:;|$)", regex=True).any():
                add_issue(codes, categories, details, "V_STATE_ACTION_MISMATCH", CAT_VALUE, "逐帧发现 state-actions 残差异常")
            if frame_issue_text.str.contains(r"(?:^|;)V_SPIKE_JUMP(?:;|$)", regex=True).any():
                add_issue(codes, categories, details, "V_SPIKE_JUMP", CAT_VALUE, "逐帧发现 state/action 突跳")

        # Frame-level summary: use union of affected rows, not raw per-stream
        # image counts, so a three-camera defect is not triple-counted.
        total_rows = int(n) if n > 0 else 0
        temp_rate = safe_ratio(len(fg[fg.issue_codes.str.contains("T_", na=False)]), total_rows)
        sync_rate = safe_ratio(len(fg[fg.issue_codes.str.contains("S_", na=False)]), total_rows)
        content_rate = safe_ratio(len(fg[fg.issue_codes.str.contains("C_", na=False)]), total_rows)
        value_rate = safe_ratio(len(fg[fg.issue_codes.str.contains("V_", na=False)]), total_rows)
        # Five dimensions are scored from disjoint primary evidence.  A hard
        # file/schema gate is applied only after the dimensional scores are
        # computed, avoiding the previous all-dimension zeroing and duplicate
        # penalties.
        structural_penalty = safe_ratio(num(sr, "shape_bad_count"), max(total_rows, 1)) + safe_ratio(abs(length_delta), max(expected, total_rows, 1))
        structural_score = 100.0 * max(0.0, 1.0 - min(1.0, 1.5 * structural_penalty)) if structural_read_ok and value_read_ok and image_read_ok else 0.0
        temporal_penalty = 1.5 * temp_rate + safe_ratio(num(sr, "dt_nonpositive_count") + num(sr, "dt_gap_gt_1_5x_count") + num(sr, "dt_abs_err_gt_2ms_count"), max(total_rows, 1))
        temporal_score = 100.0 * max(0.0, 1.0 - min(1.0, temporal_penalty)) if structural_read_ok else 0.0
        sync_penalty = safe_ratio(num(ir, "any_path_mismatch_count") + missing_images, max(3 * total_rows, 1)) + (0.25 if task_unique > 1 or task_first not in {0, 1} else 0.0)
        sync_score = 100.0 * max(0.0, 1.0 - min(1.0, 2.0 * sync_penalty)) if structural_read_ok and image_read_ok else 0.0
        content_score = 100.0 * max(0.0, 1.0 - min(1.0, 1.5 * content_rate)) if image_read_ok else 0.0
        state_action_signal = max(num(vr, "state_action_aligned_rmse_p99", num(vr, "state_action_rmse_p99", 0.0)), num(vr, "state_action_aligned_rmse_max", num(vr, "state_action_rmse_max", 0.0)))
        state_step_signal = max(num(vr, "state_step_p99", 0.0), num(vr, "state_step_max", 0.0))
        action_step_signal = max(num(vr, "action_step_p99", 0.0), num(vr, "action_step_max", 0.0))
        value_penalty = 1.25 * value_rate + min(1.0, state_action_signal / max(2.0 * thresholds["state_action_rmse"], 1e-6)) * 0.15 + min(1.0, max(state_step_signal, action_step_signal) / 2.0) * 0.10
        if "V_STATE_FREEZE" in codes or "V_ACTION_FREEZE" in codes or "V_LOW_INFORMATION_TRAJECTORY" in codes:
            value_penalty += 0.35
        value_score = 100.0 * max(0.0, 1.0 - min(1.0, value_penalty)) if value_read_ok else 0.0
        overall = 0.25 * structural_score + 0.20 * temporal_score + 0.20 * sync_score + 0.20 * content_score + 0.15 * value_score
        score_gate_reason = ""
        if not structural_read_ok:
            overall = 0.0
            score_gate_reason = "核心 Parquet 不可读"
        elif not image_read_ok or not value_read_ok:
            overall = min(overall, 25.0)
            score_gate_reason = "必需模态/字段不可读，质量总分执行 25 分硬门禁"

        # Conservative post-repair estimate: only reversible defects are
        # removed; long corrupt segments and unreadable files stay penalized.
        repairable: list[str] = []
        unrecoverable: list[str] = []
        actions: list[str] = []
        bad_value_rate = safe_ratio(numeric_bad + physical_bad, max(total_rows, 1))
        bad_image_rate = safe_ratio(num(ir, "any_image_bad_frame_count"), max(total_rows, 1))
        if any(code in codes for code in ["T_TIMESTAMP_JITTER_OR_DRIFT", "T_TIMESTAMP_UNIT_MISMATCH"]) and "T_DROPPED_OR_GAPPED_FRAMES" not in codes and "T_TIMESTAMP_NON_MONOTONIC" not in codes:
            repairable.extend(code for code in ["T_TIMESTAMP_JITTER_OR_DRIFT", "T_TIMESTAMP_UNIT_MISMATCH"] if code in codes)
            actions.append("按 frame_index/fps 重建 timestamp，并保留原始时间戳备查")
        for code in ["S_TASK_INDEX_INVALID", "S_TASK_SWITCH_WITHIN_EPISODE"]:
            if code in codes:
                repairable.append(code)
                actions.append("仅在 episodes.jsonl 与 tasks.jsonl 能唯一确定任务时重写 task_index，并保留逐帧审计记录")
        for code in ["T_METADATA_LENGTH_MISMATCH", "T_TIMESTAMP_NON_MONOTONIC", "T_DROPPED_OR_GAPPED_FRAMES", "T_FRAME_ORDER_ERROR"]:
            if code in codes:
                if code == "T_METADATA_LENGTH_MISMATCH" and abs(length_delta) <= 2:
                    repairable.append(code)
                    actions.append("按实际有效行数更新 episodes.jsonl length")
                else:
                    unrecoverable.append(code)
                    actions.append("重新采集/排序并核对掉帧区间，禁止静默覆盖原始轨迹")
        for code in ["S_IMAGE_PATH_MISMATCH", "S_MODALITY_INDEX_ALIGNMENT"]:
            if code in codes:
                repairable.append(code)
                actions.append("按 frame_index 重写图像 path，并用内容 hash 复核跨模态对齐")
        for code in ["V_NONFINITE", "V_PHYSICAL_RANGE", "V_STATE_ACTION_MISMATCH", "V_SPIKE_JUMP"]:
            if code in codes:
                if bad_value_rate <= thresholds["bad_ratio_sparse"]:
                    repairable.append(code)
                    actions.append("对稀疏状态/动作异常做 Hampel 检测 + 邻域线性插值，写入 quality_mask")
                else:
                    unrecoverable.append(code)
                    actions.append("异常比例过高，隔离该通道并回采；不使用模型臆造整段状态")
        for code in ["V_STATE_FREEZE", "V_ACTION_FREEZE", "V_LOW_INFORMATION_TRAJECTORY"]:
            if code in codes:
                unrecoverable.append(code)
                actions.append("传感器冻结无法从同一轨迹恢复，整段回采/降权")
        for code in ["C_IMAGE_DECODE", "C_IMAGE_SCREEN", "C_IMAGE_BLUR", "C_IMAGE_DUPLICATE"]:
            if code in codes:
                if bad_image_rate <= thresholds["bad_ratio_sparse"]:
                    repairable.append(code)
                    actions.append("生成逐帧 quality_mask，跨模态同步剔除问题帧；短段可回采替换")
                else:
                    unrecoverable.append(code)
                    actions.append("图像异常段超过 10%，隔离并回采，避免伪造视觉内容")
        if "C_IMAGE_SHAPE" in codes:
            unrecoverable.append("C_IMAGE_SHAPE")
            actions.append("保留原图并核对采集/编码配置；禁止仅为过 schema 而强制拉伸图像")
        for code in ["C_PARQUET_CORRUPT", "C_SCHEMA_MISSING_IMAGE_COLUMN", "C_FILE_READ_ERROR", "C_IMAGE_READ_ERROR", "C_VALUE_READ_ERROR"]:
            if code in codes:
                unrecoverable.append(code)
                actions.append("文件级隔离，使用备份/重新导出恢复 schema；不在原文件上覆盖")
        if not codes:
            actions.append("通过质量门禁，无需修复")
        repair_mode = "无需处理" if not codes else "自动修复候选" if repairable and not unrecoverable else "自动标记+回采" if repairable else "隔离回采"
        rows.append(
            {
                "dataset": label,
                "episode_index": ep_id,
                "rows": int(n) if n > 0 else 0,
                "expected_rows": int(expected) if expected > 0 else 0,
                "status": "通过" if not codes else "需复核",
                "severity": "通过" if not codes else "严重" if (not read_ok or overall < 40) else "高" if overall < 65 else "中" if overall < 85 else "低",
                "issue_codes": ";".join(codes),
                "issue_categories": ";".join(categories),
                "issue_details": "；".join(details),
                "structural_score": round(structural_score, 2),
                "temporal_score": round(temporal_score, 2),
                "sync_score": round(sync_score, 2),
                "content_score": round(content_score, 2),
                "value_score": round(value_score, 2),
                "overall_quality_score": round(overall, 2),
                "estimated_quality_after_repair": math.nan,
                "score_gate_reason": score_gate_reason,
                "detector_version": DETECTOR_VERSION,
                "repair_mode": repair_mode,
                "repairable_codes": ";".join(dict.fromkeys(repairable)),
                "unrecoverable_codes": ";".join(dict.fromkeys(unrecoverable)),
                "repair_actions": "；".join(dict.fromkeys(actions)),
                "raw_file": "/".join(Path(str(value(sr, "source_file", value(ir, "source_file", "")))).parts[-3:]),
            }
        )
    return pd.DataFrame(rows).sort_values("episode_index").reset_index(drop=True)


def plot_results(test_report: pd.DataFrame, test_frames: pd.DataFrame) -> None:
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    # Use a CJK-capable font when running on the Windows judging workstation;
    # fall back cleanly on Linux/CI so the figures never contain tofu boxes.
    font_name = "DejaVu Sans"
    for font_path in ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/NotoSansSC-VF.ttf"]:
        if Path(font_path).exists():
            font_name = font_manager.FontProperties(fname=font_path).get_name()
            break
    plt.rcParams["font.sans-serif"] = [font_name, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    sns.set_theme(style="whitegrid", font=font_name)
    # Score distribution and ranking.
    fig, axes = plt.subplots(1, 2, figsize=(14, 5), gridspec_kw={"width_ratios": [1, 2]})
    score_cols = ["structural_score", "temporal_score", "sync_score", "content_score", "value_score"]
    labels = ["结构", "时序", "同步", "内容", "价值"]
    axes[0].hist(test_report["overall_quality_score"], bins=12, color="#2f80ed", edgecolor="white")
    axes[0].set_title("测试集质量总分分布")
    axes[0].set_xlabel("0-100")
    axes[0].set_ylabel("轨迹数")
    top = test_report.sort_values("overall_quality_score").tail(20)
    axes[1].barh(top["episode_index"].astype(str), top["overall_quality_score"], color="#27ae60")
    axes[1].set_title("质量分最高的 20 条轨迹")
    axes[1].set_xlabel("质量分")
    fig.tight_layout()
    fig.savefig(FIG_DIR / "quality_score_distribution.png", dpi=180)
    plt.close(fig)

    matrix = test_report.set_index("episode_index")[score_cols]
    matrix.columns = labels
    fig, ax = plt.subplots(figsize=(10, 18))
    sns.heatmap(matrix, cmap="RdYlGn", vmin=0, vmax=100, linewidths=0.15, ax=ax, cbar_kws={"label": "分数"})
    ax.set_title("89 条测试轨迹五维质量评分卡")
    ax.set_xlabel("质量维度")
    ax.set_ylabel("episode_index")
    fig.tight_layout()
    fig.savefig(FIG_DIR / "quality_score_heatmap.png", dpi=180)
    plt.close(fig)

    categories = {
        "时序": test_report.issue_categories.fillna("").str.contains("时序").sum(),
        "同步": test_report.issue_categories.fillna("").str.contains("同步").sum(),
        "内容/结构": test_report.issue_categories.fillna("").str.contains("内容/结构").sum(),
        "数据价值": test_report.issue_categories.fillna("").str.contains("数据价值").sum(),
    }
    fig, ax = plt.subplots(figsize=(8, 4.5))
    bars = ax.bar(categories.keys(), categories.values(), color=["#f2994a", "#9b51e0", "#eb5757", "#2d9cdb"])
    ax.set_title("按类别检出的异常轨迹数（可多选）")
    ax.set_ylabel("轨迹数")
    for bar in bars:
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5, str(int(bar.get_height())), ha="center")
    fig.tight_layout()
    fig.savefig(FIG_DIR / "category_issue_counts.png", dpi=180)
    plt.close(fig)

    if not test_frames.empty:
        count = test_frames.assign(has_issue=test_frames.issue_codes.ne("")).groupby("episode_index").has_issue.sum()
        fig, ax = plt.subplots(figsize=(14, 4))
        ax.bar(count.index.astype(str), count.values, color="#bb6bd9")
        ax.set_title("逐帧异常定位数量")
        ax.set_xlabel("episode_index")
        ax.set_ylabel("异常帧数")
        ax.tick_params(axis="x", labelrotation=90, labelsize=7)
        fig.tight_layout()
        fig.savefig(FIG_DIR / "frame_issue_counts.png", dpi=180)
        plt.close(fig)


def build_summary(ref_report: pd.DataFrame, test_report: pd.DataFrame, test_frames: pd.DataFrame, thresholds: dict[str, Any]) -> dict[str, Any]:
    def issue_episode_count(report: pd.DataFrame, category: str) -> int:
        return int(report.issue_categories.fillna("").str.contains(category).sum())

    test_flagged = int((test_report.status != "通过").sum())
    ref_flagged = int((ref_report.status != "通过").sum())
    return {
        "data_scope": {
            "reference_episodes": int(len(ref_report)),
            "test_episodes": int(len(test_report)),
            "test_readable_episodes": int(test_frames.episode_index.nunique()),
            "test_frames_readable": int(len(test_frames)),
        },
        "reference_clean_false_positive_rate": round(ref_flagged / max(len(ref_report), 1), 4),
        "test_flagged_episode_count": test_flagged,
        "test_flagged_episode_rate": round(test_flagged / max(len(test_report), 1), 4),
        "test_issue_episodes_by_category": {c: issue_episode_count(test_report, c) for c in [CAT_TEMPORAL, CAT_SYNC, CAT_CONTENT, CAT_VALUE]},
        "test_issue_frame_count": int(test_frames.issue_codes.ne("").sum()),
        "score": {
            "mean": round(float(test_report.overall_quality_score.mean()), 2),
            "median": round(float(test_report.overall_quality_score.median()), 2),
            "min": round(float(test_report.overall_quality_score.min()), 2),
            "max": round(float(test_report.overall_quality_score.max()), 2),
        },
        "repair_modes": test_report.repair_mode.value_counts().to_dict(),
        "thresholds": thresholds,
        "detector_version": DETECTOR_VERSION,
        "label_note": "测试集未提供官方逐帧标签；测试集计数表示算法标记的需复核轨迹，不等同于真实异常数或召回率。泛化与 P/R/F1 由留一参考验证和合成注入基准单独报告。",
    }


def process_dataset(label: str, thresholds: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    structure = read_csv(f"{label}_structural_episodes.csv")
    image = read_csv(f"{label}_image_episodes.csv")
    values = read_csv(f"{label}_value_episodes.csv")
    sf = read_csv(f"{label}_structural_frames.csv")
    im = read_csv(f"{label}_image_frames.csv")
    vf = read_csv(f"{label}_value_frames.csv")
    frames = build_frame_flags(sf, im, vf, thresholds)
    report = episode_report(structure, image, values, frames, thresholds, label)
    return report, frames


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-plots", action="store_true")
    args = parser.parse_args()
    ref_image = read_csv("reference_image_episodes.csv")
    ref_image_frames = read_csv("reference_image_frames.csv")
    ref_values = read_csv("reference_value_episodes.csv")
    ref_value_frames = read_csv("reference_value_frames.csv")
    thresholds = reference_thresholds(ref_image, ref_values, ref_value_frames, ref_image_frames)
    ref_report, ref_frames = process_dataset("reference", thresholds)
    test_report, test_frames = process_dataset("test", thresholds)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ref_report.to_csv(OUT_DIR / "reference_quality_report.csv", index=False, encoding="utf-8-sig")
    ref_frames.to_csv(OUT_DIR / "reference_frame_flags.csv", index=False, encoding="utf-8-sig")
    test_report.to_csv(OUT_DIR / "test_quality_report.csv", index=False, encoding="utf-8-sig")
    test_frames.to_csv(OUT_DIR / "test_frame_flags.csv", index=False, encoding="utf-8-sig")
    summary = build_summary(ref_report, test_report, test_frames, thresholds)
    (OUT_DIR / "quality_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.no_plots:
        plot_results(test_report, test_frames)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("\nTOP LOW QUALITY")
    print(test_report.sort_values("overall_quality_score").head(20)[["episode_index", "overall_quality_score", "issue_categories", "issue_details", "repair_mode"]].to_string(index=False))


if __name__ == "__main__":
    main()
