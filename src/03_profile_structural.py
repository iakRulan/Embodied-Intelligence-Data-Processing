"""Profile LeRobot parquet episodes without loading image payloads.

This is the first, cheap pass of the quality pipeline.  It produces
episode-level and frame-level structural features that are later consumed by
the anomaly detector.  The script deliberately keeps the original datasets
read-only and writes only derived CSV/JSON files under ``outputs``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dataset_paths import OUT_DIR, resolve_reference_root, resolve_test_root

DEFAULT_REF = resolve_reference_root()
DEFAULT_TEST = resolve_test_root()


def finite_array(value: Any, expected_size: int = 20) -> tuple[np.ndarray | None, bool]:
    """Return a flattened float array and whether it has the expected shape."""

    try:
        arr = np.asarray(value, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError):
        return None, False
    if arr.size != expected_size:
        return arr, False
    return arr, bool(np.isfinite(arr).all())


def robust_stats(values: np.ndarray) -> dict[str, float]:
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return {"median": math.nan, "mad": math.nan, "p01": math.nan, "p99": math.nan}
    med = float(np.median(values))
    return {
        "median": med,
        "mad": float(np.median(np.abs(values - med))),
        "p01": float(np.quantile(values, 0.01)),
        "p99": float(np.quantile(values, 0.99)),
    }


def finite_quantile(values: np.ndarray, q: float) -> float:
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    return float(np.quantile(values, q)) if values.size else math.nan


def as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def profile_episode(path: Path, meta: dict[str, Any] | None, nominal_dt: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    columns = [
        "timestamp",
        "frame_index",
        "episode_index",
        "index",
        "task_index",
        "state",
        "actions",
    ]
    df = pd.read_parquet(path, columns=columns)
    ep_id = int(path.stem.split("_")[-1])
    n = len(df)
    ts = pd.to_numeric(df["timestamp"], errors="coerce").to_numpy(dtype=np.float64)
    frame = pd.to_numeric(df["frame_index"], errors="coerce").to_numpy(dtype=np.float64)
    global_index = pd.to_numeric(df["index"], errors="coerce").to_numpy(dtype=np.float64)
    ep_col = pd.to_numeric(df["episode_index"], errors="coerce").to_numpy(dtype=np.float64)
    task = pd.to_numeric(df["task_index"], errors="coerce").to_numpy(dtype=np.float64)

    dt = np.diff(ts) if n > 1 else np.empty(0)
    frame_diff = np.diff(frame) if n > 1 else np.empty(0)
    index_diff = np.diff(global_index) if n > 1 else np.empty(0)
    target = float(nominal_dt)
    valid_dt = dt[np.isfinite(dt)]
    dt_abs_err = np.abs(valid_dt - target)

    state_rows: list[np.ndarray | None] = []
    action_rows: list[np.ndarray | None] = []
    shape_ok = []
    finite_ok = []
    sa_rmse = np.full(n, np.nan)
    sa_max = np.full(n, np.nan)
    state_step = np.full(n, np.nan)
    action_step = np.full(n, np.nan)
    state_norm = np.full(n, np.nan)
    action_norm = np.full(n, np.nan)
    for i, (s_raw, a_raw) in enumerate(zip(df["state"], df["actions"], strict=False)):
        s, s_ok = finite_array(s_raw)
        a, a_ok = finite_array(a_raw)
        state_rows.append(s)
        action_rows.append(a)
        shape_ok.append(bool(s is not None and a is not None and s.size == 20 and a.size == 20))
        finite_ok.append(bool(s_ok and a_ok))
        if s is not None:
            state_norm[i] = float(np.linalg.norm(s))
        if a is not None:
            action_norm[i] = float(np.linalg.norm(a))
        if s is not None and a is not None and s.size == a.size:
            delta = a - s
            sa_rmse[i] = float(np.sqrt(np.mean(delta * delta)))
            sa_max[i] = float(np.max(np.abs(delta)))
        if i > 0 and state_rows[i - 1] is not None and s is not None and state_rows[i - 1].size == s.size:
            state_step[i] = float(np.linalg.norm(s - state_rows[i - 1]))
        if i > 0 and action_rows[i - 1] is not None and a is not None and action_rows[i - 1].size == a.size:
            action_step[i] = float(np.linalg.norm(a - action_rows[i - 1]))

    frame_rows: list[dict[str, Any]] = []
    for i in range(n):
        frame_rows.append(
            {
                "episode_index_file": ep_id,
                "row": i,
                "timestamp": float(ts[i]) if np.isfinite(ts[i]) else math.nan,
                "dt": float(dt[i - 1]) if i > 0 and np.isfinite(dt[i - 1]) else math.nan,
                "frame_index": float(frame[i]) if np.isfinite(frame[i]) else math.nan,
                "frame_diff": float(frame_diff[i - 1]) if i > 0 and np.isfinite(frame_diff[i - 1]) else math.nan,
                "global_index": float(global_index[i]) if np.isfinite(global_index[i]) else math.nan,
                "global_index_diff": float(index_diff[i - 1]) if i > 0 and np.isfinite(index_diff[i - 1]) else math.nan,
                "episode_index_value": float(ep_col[i]) if np.isfinite(ep_col[i]) else math.nan,
                "task_index": float(task[i]) if np.isfinite(task[i]) else math.nan,
                "shape_ok": bool(shape_ok[i]),
                "finite_ok": bool(finite_ok[i]),
                "state_norm": float(state_norm[i]),
                "action_norm": float(action_norm[i]),
                "state_action_rmse": float(sa_rmse[i]),
                "state_action_max_abs": float(sa_max[i]),
                "state_step_l2": float(state_step[i]),
                "action_step_l2": float(action_step[i]),
            }
        )

    expected_len = None if meta is None else as_int(meta.get("length"))
    dt_stats = robust_stats(valid_dt)
    episode = {
        "episode_index": ep_id,
        "rows": n,
        "expected_rows": expected_len,
        "length_delta": None if expected_len is None else n - expected_len,
        "timestamp_first": float(ts[0]) if n and np.isfinite(ts[0]) else math.nan,
        "timestamp_last": float(ts[-1]) if n and np.isfinite(ts[-1]) else math.nan,
        "timestamp_span": float(ts[-1] - ts[0]) if n and np.isfinite(ts[[0, -1]]).all() else math.nan,
        "dt_median": dt_stats["median"],
        "dt_mad": dt_stats["mad"],
        "dt_p01": dt_stats["p01"],
        "dt_p99": dt_stats["p99"],
        "dt_min": float(np.min(valid_dt)) if valid_dt.size else math.nan,
        "dt_max": float(np.max(valid_dt)) if valid_dt.size else math.nan,
        "dt_nonfinite_count": int((~np.isfinite(dt)).sum()),
        "dt_nonpositive_count": int((np.isfinite(dt) & (dt <= 0)).sum()),
        "dt_gap_gt_1_5x_count": int((np.isfinite(dt) & (dt > 1.5 * target)).sum()),
        "dt_abs_err_gt_2ms_count": int((np.isfinite(dt) & (np.abs(dt - target) > 0.002)).sum()),
        "dt_abs_err_gt_10ms_count": int((np.isfinite(dt) & (np.abs(dt - target) > 0.01)).sum()),
        "frame_start": float(frame[0]) if n and np.isfinite(frame[0]) else math.nan,
        "frame_end": float(frame[-1]) if n and np.isfinite(frame[-1]) else math.nan,
        "frame_nonfinite_count": int((~np.isfinite(frame)).sum()),
        "frame_nonunit_diff_count": int((np.isfinite(frame_diff) & (frame_diff != 1)).sum()),
        "frame_nonpositive_diff_count": int((np.isfinite(frame_diff) & (frame_diff <= 0)).sum()),
        "index_nonfinite_count": int((~np.isfinite(global_index)).sum()),
        "index_nonunit_diff_count": int((np.isfinite(index_diff) & (index_diff != 1)).sum()),
        "episode_index_mismatch_count": int((np.isfinite(ep_col) & (ep_col != ep_id)).sum()),
        "task_unique_count": int(pd.Series(task).dropna().nunique()),
        "task_nonfinite_count": int((~np.isfinite(task)).sum()),
        "shape_bad_count": int((~np.asarray(shape_ok, dtype=bool)).sum()),
        "finite_bad_count": int((~np.asarray(finite_ok, dtype=bool)).sum()),
        "state_abs_max": float(np.nanmax(np.abs(np.concatenate([x for x in state_rows if x is not None])))) if any(x is not None for x in state_rows) else math.nan,
        "action_abs_max": float(np.nanmax(np.abs(np.concatenate([x for x in action_rows if x is not None])))) if any(x is not None for x in action_rows) else math.nan,
        "state_action_rmse_median": float(np.nanmedian(sa_rmse)) if np.isfinite(sa_rmse).any() else math.nan,
        "state_action_rmse_p99": finite_quantile(sa_rmse, 0.99),
        "state_action_max_p99": finite_quantile(sa_max, 0.99),
        "state_step_p99": finite_quantile(state_step, 0.99),
        "action_step_p99": finite_quantile(action_step, 0.99),
        "state_path_length": float(np.nansum(np.where(np.isfinite(state_step), state_step, 0.0))),
        "action_path_length": float(np.nansum(np.where(np.isfinite(action_step), action_step, 0.0))),
        "state_motion_fraction": float(np.mean(np.isfinite(state_step[1:]) & (state_step[1:] > 1e-4))) if n > 1 else 0.0,
        "action_motion_fraction": float(np.mean(np.isfinite(action_step[1:]) & (action_step[1:] > 1e-4))) if n > 1 else 0.0,
        "task_index_first": float(task[0]) if n and np.isfinite(task[0]) else math.nan,
        "task_index_last": float(task[-1]) if n and np.isfinite(task[-1]) else math.nan,
    }
    return episode, frame_rows


def read_episode_meta(root: Path) -> dict[int, dict[str, Any]]:
    meta_files = list(root.rglob("meta/episodes.jsonl"))
    if not meta_files:
        return {}
    result: dict[int, dict[str, Any]] = {}
    for line in meta_files[0].read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            result[int(row["episode_index"])] = row
    return result


def profile_dataset(root: Path, label: str, nominal_dt: float) -> None:
    data_files = sorted(root.rglob("data/chunk-*/episode_*.parquet"))
    if not data_files:
        raise FileNotFoundError(f"No parquet episodes found under {root}")
    meta = read_episode_meta(root)
    episodes: list[dict[str, Any]] = []
    frames: list[dict[str, Any]] = []
    started = time.time()
    for i, path in enumerate(data_files, start=1):
        ep_id = int(path.stem.split("_")[-1])
        try:
            ep, fr = profile_episode(path, meta.get(ep_id), nominal_dt)
            ep["read_ok"] = True
            ep["read_error"] = ""
        except Exception as exc:  # corrupted/truncated parquet is itself a reportable defect
            ep = {
                "episode_index": ep_id,
                "rows": math.nan,
                "expected_rows": None if meta.get(ep_id) is None else as_int(meta[ep_id].get("length")),
                "length_delta": math.nan,
                "read_ok": False,
                "read_error": f"{type(exc).__name__}: {exc}",
            }
            fr = []
            print(f"[{label}] episode {ep_id} READ_ERROR {type(exc).__name__}", flush=True)
        ep["source_file"] = str(path)
        ep["dataset"] = label
        for row in fr:
            row["dataset"] = label
        episodes.append(ep)
        frames.extend(fr)
        print(f"[{label}] {i}/{len(data_files)} episode {ep['episode_index']} rows={ep['rows']}", flush=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(episodes).sort_values("episode_index").to_csv(
        OUT_DIR / f"{label}_structural_episodes.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(frames).to_csv(OUT_DIR / f"{label}_structural_frames.csv", index=False, encoding="utf-8-sig")
    summary = {
        "dataset": label,
        "root": str(root),
        "episodes": len(episodes),
        "frames": int(sum(row["rows"] for row in episodes if isinstance(row.get("rows"), (int, float)) and np.isfinite(row["rows"]))),
        "read_error_episodes": int(sum(not bool(row.get("read_ok", False)) for row in episodes)),
        "nominal_dt": nominal_dt,
        "elapsed_seconds": time.time() - started,
    }
    (OUT_DIR / f"{label}_structural_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", type=Path, default=DEFAULT_REF)
    parser.add_argument("--test", type=Path, default=DEFAULT_TEST)
    parser.add_argument("--skip-reference", action="store_true")
    parser.add_argument("--fps", type=float, default=10.0)
    args = parser.parse_args()
    nominal_dt = 1.0 / args.fps
    if not args.skip_reference:
        profile_dataset(args.reference, "reference", nominal_dt)
    profile_dataset(args.test, "test", nominal_dt)


if __name__ == "__main__":
    main()
