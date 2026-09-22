"""Profile state/action physical and cross-modal consistency features."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dataset_paths import OUT_DIR, resolve_reference_root, resolve_test_root

DEFAULT_REF = resolve_reference_root()
DEFAULT_TEST = resolve_test_root()


def row_array(value: Any) -> np.ndarray | None:
    try:
        return np.asarray(value, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError):
        return None


def physical_flags(arr: np.ndarray | None) -> dict[str, Any]:
    if arr is None or arr.size != 20:
        return {
            "shape_ok": False,
            "finite_ok": False,
            "hard_range_bad": True,
            "rotation_bad": True,
            "gripper_bad": True,
            "position_bad": True,
            "abs_max": math.nan,
            "position_abs_max": math.nan,
            "rotation_norm_dev": math.nan,
            "rotation_orth_dev": math.nan,
            "gripper_min": math.nan,
            "gripper_max": math.nan,
        }
    finite = bool(np.isfinite(arr).all())
    abs_max = float(np.max(np.abs(arr))) if finite else math.inf
    # Two 3-vectors per arm are the first and second columns of a rotation.
    rot = np.concatenate([arr[3:9], arr[13:19]])
    rot_norms = np.array([np.linalg.norm(arr[3:6]), np.linalg.norm(arr[6:9]), np.linalg.norm(arr[13:16]), np.linalg.norm(arr[16:19])])
    rot_dots = np.array([np.dot(arr[3:6], arr[6:9]), np.dot(arr[13:16], arr[16:19])])
    rotation_norm_dev = float(np.max(np.abs(rot_norms - 1.0))) if finite else math.inf
    rotation_orth_dev = float(np.max(np.abs(rot_dots))) if finite else math.inf
    rotation_bad = bool((not finite) or np.any(np.abs(rot) > 1.05) or rotation_norm_dev > 0.08 or rotation_orth_dev > 0.08)
    # Gripper values are normalized in the supplied schema.
    grippers = np.array([arr[9], arr[19]])
    gripper_bad = bool((not finite) or np.any(grippers < -0.05) or np.any(grippers > 1.05))
    # The clean reference uses normalized Cartesian coordinates inside the
    # unit cube.  Keep the legacy hard limit broad, but expose a tighter
    # per-frame envelope so a single local excursion is not hidden by an
    # episode-level percentile.
    positions = np.concatenate([arr[0:3], arr[10:13]])
    position_abs_max = float(np.max(np.abs(positions))) if finite else math.inf
    position_bad = bool((not finite) or np.any(np.abs(positions) > 1.0))
    hard_range_bad = bool((not finite) or np.any(np.abs(positions) > 2.0) or np.any(np.abs(rot) > 1.2) or np.any(np.abs(grippers) > 1.2))
    return {
        "shape_ok": True,
        "finite_ok": finite,
        "hard_range_bad": hard_range_bad,
        "rotation_bad": rotation_bad,
        "gripper_bad": gripper_bad,
        "position_bad": position_bad,
        "abs_max": abs_max,
        "position_abs_max": position_abs_max,
        "rotation_norm_dev": rotation_norm_dev,
        "rotation_orth_dev": rotation_orth_dev,
        "gripper_min": float(np.min(grippers)) if finite else math.nan,
        "gripper_max": float(np.max(grippers)) if finite else math.nan,
    }


def profile_episode(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ep_id = int(path.stem.split("_")[-1])
    df = pd.read_parquet(path, columns=["state", "actions", "frame_index", "timestamp"])
    rows = []
    state_arrays = [row_array(value) for value in df["state"]]
    action_arrays = [row_array(value) for value in df["actions"]]
    previous_state: np.ndarray | None = None
    previous_action: np.ndarray | None = None
    for i, (s, a) in enumerate(zip(state_arrays, action_arrays, strict=False)):
        sf = physical_flags(s)
        af = physical_flags(a)
        state_action_rmse = math.nan
        state_action_max = math.nan
        if s is not None and a is not None and s.size == a.size:
            delta = a - s
            state_action_rmse = float(np.sqrt(np.mean(delta * delta))) if np.isfinite(delta).all() else math.inf
            state_action_max = float(np.max(np.abs(delta))) if np.isfinite(delta).all() else math.inf
        aligned_rmse = math.nan
        aligned_max = math.nan
        aligned_lag = math.nan
        # ``actions`` may describe a contemporaneous command or a target for a
        # neighbouring state.  Compare {-1, 0, +1} frame alignments and retain
        # the best supported lag instead of assuming state_t == action_t.
        if a is not None and a.size == 20 and np.isfinite(a).all():
            candidates: list[tuple[float, float, int]] = []
            for lag in (-1, 0, 1):
                j = i + lag
                if 0 <= j < len(state_arrays):
                    candidate = state_arrays[j]
                    if candidate is not None and candidate.size == a.size and np.isfinite(candidate).all():
                        delta = a - candidate
                        candidates.append((float(np.sqrt(np.mean(delta * delta))), float(np.max(np.abs(delta))), lag))
            if candidates:
                aligned_rmse, aligned_max, aligned_lag = min(candidates, key=lambda item: item[0])
        state_step = math.nan
        action_step = math.nan
        if previous_state is not None and s is not None and previous_state.size == s.size:
            d = s - previous_state
            state_step = float(np.linalg.norm(d)) if np.isfinite(d).all() else math.inf
        if previous_action is not None and a is not None and previous_action.size == a.size:
            d = a - previous_action
            action_step = float(np.linalg.norm(d)) if np.isfinite(d).all() else math.inf
        row = {
            "dataset_episode": ep_id,
            "row": i,
            "frame_index": int(df["frame_index"].iloc[i]),
            "timestamp": float(df["timestamp"].iloc[i]),
            "state_action_rmse": state_action_rmse,
            "state_action_max_abs": state_action_max,
            "state_action_aligned_rmse": aligned_rmse,
            "state_action_aligned_max_abs": aligned_max,
            "state_action_best_lag": aligned_lag,
            "state_step_l2": state_step,
            "action_step_l2": action_step,
        }
        row.update({f"state_{k}": v for k, v in sf.items()})
        row.update({f"action_{k}": v for k, v in af.items()})
        rows.append(row)
        previous_state = s
        previous_action = a
    f = pd.DataFrame(rows)
    summary = {
        "episode_index": ep_id,
        "rows": len(f),
        "state_finite_bad_count": int((~f["state_finite_ok"].astype(bool)).sum()),
        "action_finite_bad_count": int((~f["action_finite_ok"].astype(bool)).sum()),
        "state_hard_range_bad_count": int(f["state_hard_range_bad"].astype(bool).sum()),
        "action_hard_range_bad_count": int(f["action_hard_range_bad"].astype(bool).sum()),
        "state_position_bad_count": int(f["state_position_bad"].astype(bool).sum()),
        "action_position_bad_count": int(f["action_position_bad"].astype(bool).sum()),
        "state_rotation_bad_count": int(f["state_rotation_bad"].astype(bool).sum()),
        "action_rotation_bad_count": int(f["action_rotation_bad"].astype(bool).sum()),
        "state_gripper_bad_count": int(f["state_gripper_bad"].astype(bool).sum()),
        "action_gripper_bad_count": int(f["action_gripper_bad"].astype(bool).sum()),
        "state_abs_max": float(pd.to_numeric(f["state_abs_max"], errors="coerce").replace([np.inf, -np.inf], np.nan).max()),
        "action_abs_max": float(pd.to_numeric(f["action_abs_max"], errors="coerce").replace([np.inf, -np.inf], np.nan).max()),
        "state_position_abs_max": float(pd.to_numeric(f["state_position_abs_max"], errors="coerce").replace([np.inf, -np.inf], np.nan).max()),
        "action_position_abs_max": float(pd.to_numeric(f["action_position_abs_max"], errors="coerce").replace([np.inf, -np.inf], np.nan).max()),
        "state_action_rmse_p99": float(pd.to_numeric(f["state_action_rmse"], errors="coerce").replace([np.inf, -np.inf], np.nan).quantile(0.99)),
        "state_action_rmse_max": float(pd.to_numeric(f["state_action_rmse"], errors="coerce").replace([np.inf, -np.inf], np.nan).max()),
        "state_action_max_p99": float(pd.to_numeric(f["state_action_max_abs"], errors="coerce").replace([np.inf, -np.inf], np.nan).quantile(0.99)),
        "state_action_max_abs_max": float(pd.to_numeric(f["state_action_max_abs"], errors="coerce").replace([np.inf, -np.inf], np.nan).max()),
        "state_action_aligned_rmse_p99": float(pd.to_numeric(f["state_action_aligned_rmse"], errors="coerce").replace([np.inf, -np.inf], np.nan).quantile(0.99)),
        "state_action_aligned_rmse_max": float(pd.to_numeric(f["state_action_aligned_rmse"], errors="coerce").replace([np.inf, -np.inf], np.nan).max()),
        "state_action_aligned_max_abs_max": float(pd.to_numeric(f["state_action_aligned_max_abs"], errors="coerce").replace([np.inf, -np.inf], np.nan).max()),
        "state_action_best_lag_mode": float(pd.to_numeric(f["state_action_best_lag"], errors="coerce").dropna().mode().iloc[0]) if pd.to_numeric(f["state_action_best_lag"], errors="coerce").notna().any() else math.nan,
        "state_step_p99": float(pd.to_numeric(f["state_step_l2"], errors="coerce").replace([np.inf, -np.inf], np.nan).quantile(0.99)),
        "state_step_max": float(pd.to_numeric(f["state_step_l2"], errors="coerce").replace([np.inf, -np.inf], np.nan).max()),
        "action_step_p99": float(pd.to_numeric(f["action_step_l2"], errors="coerce").replace([np.inf, -np.inf], np.nan).quantile(0.99)),
        "action_step_max": float(pd.to_numeric(f["action_step_l2"], errors="coerce").replace([np.inf, -np.inf], np.nan).max()),
    }
    return rows, summary


def profile_dataset(root: Path, label: str) -> None:
    files = sorted(root.rglob("data/chunk-*/episode_*.parquet"))
    episodes = []
    frames = []
    for i, path in enumerate(files, start=1):
        ep_id = int(path.stem.split("_")[-1])
        try:
            fr, ep = profile_episode(path)
        except Exception as exc:
            ep = {"episode_index": ep_id, "rows": math.nan, "read_ok": False, "read_error": f"{type(exc).__name__}: {exc}"}
            fr = []
            print(f"[{label}] episode {ep_id} READ_ERROR {type(exc).__name__}", flush=True)
        ep["dataset"] = label
        for row in fr:
            row["dataset"] = label
        episodes.append(ep)
        frames.extend(fr)
        print(f"[{label}] {i}/{len(files)} episode {ep_id} rows={ep.get('rows')}", flush=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(episodes).sort_values("episode_index").to_csv(OUT_DIR / f"{label}_value_episodes.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(frames).to_csv(OUT_DIR / f"{label}_value_frames.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / f"{label}_value_summary.json").write_text(json.dumps({"dataset": label, "episodes": len(episodes), "frames": len(frames)}, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", type=Path, default=DEFAULT_REF)
    parser.add_argument("--test", type=Path, default=DEFAULT_TEST)
    parser.add_argument("--skip-reference", action="store_true")
    args = parser.parse_args()
    if not args.skip_reference:
        profile_dataset(args.reference, "reference")
    profile_dataset(args.test, "test")


if __name__ == "__main__":
    main()
