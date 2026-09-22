# -*- coding: utf-8 -*-
"""Gap probe: verify whether three currently-undetected defect classes have
measurable signal in the provided data.

1. Visual-motor cross-modal lag  (fills the "no independent timestamp" gap)
2. Screen corruption / glitch     (花屏, currently merged into black/bright)
3. Occlusion via static-vision-while-moving (遮挡, currently only low-contrast proxy)

Read-only. Writes a probe report to outputs/gap_probe_*.csv
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
from dataset_paths import OUT_DIR

OUT_DIR.mkdir(parents=True, exist_ok=True)


def read_csv(name: str) -> pd.DataFrame:
    return pd.read_csv(OUT_DIR / name, encoding="utf-8-sig")


def per_frame_motion(image_frames: pd.DataFrame, value_frames: pd.DataFrame) -> pd.DataFrame:
    """Aggregate the three camera streams into one image-motion series per frame,
    then join the state-motion series."""
    img = image_frames[image_frames["decode_ok"] == True].copy()  # noqa: E712
    img["row"] = pd.to_numeric(img["row"], errors="coerce")
    img["motion"] = pd.to_numeric(img["adjacent_ahash_distance"], errors="coerce")
    vis = img.groupby(["dataset_episode", "row"], as_index=False)["motion"].mean()
    vis = vis.rename(columns={"motion": "vis_motion"})

    val = value_frames.copy()
    val["row"] = pd.to_numeric(val["row"], errors="coerce")
    val["state_step_l2"] = pd.to_numeric(val["state_step_l2"], errors="coerce")
    val["action_step_l2"] = pd.to_numeric(val["action_step_l2"], errors="coerce")

    merged = vis.merge(
        val[["dataset_episode", "row", "state_step_l2", "action_step_l2"]],
        on=["dataset_episode", "row"],
        how="inner",
    )
    return merged.sort_values(["dataset_episode", "row"])


def zscore_fill(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    mask = np.isfinite(x)
    if mask.sum() < 5:
        return np.zeros_like(x)
    mu = np.nanmean(x[mask])
    sd = np.nanstd(x[mask])
    if sd < 1e-9:
        return np.zeros_like(x)
    out = np.zeros_like(x)
    out[mask] = (x[mask] - mu) / sd
    return out


def cross_corr_profile(a: np.ndarray, b: np.ndarray, max_lag: int = 8) -> tuple[int, float, float, np.ndarray]:
    """Peak lag of cross-correlation between visual motion and state motion.

    lag>0 means the visual stream leads the state stream by `lag` frames.
    """
    a = zscore_fill(a)
    b = zscore_fill(b)
    n = len(a)
    if n < 20:
        return 0, 0.0, 0.0, np.array([])
    lags = np.arange(-max_lag, max_lag + 1)
    scores = []
    for lag in lags:
        if lag >= 0:
            x, y = a[: n - lag], b[lag:]
        else:
            x, y = a[-lag:], b[: n + lag]
        if len(x) < 10:
            scores.append(np.nan)
            continue
        scores.append(float(np.mean(x * y)))
    scores = np.asarray(scores, dtype=float)
    if not np.any(np.isfinite(scores)):
        return 0, 0.0, 0.0, scores
    best = int(np.nanargmax(np.abs(scores)))
    peak_lag = int(lags[best])
    peak_corr = float(scores[best])
    zero_corr = float(scores[int(np.argmax(lags == 0))])
    return peak_lag, peak_corr, zero_corr, scores


def analyse_dataset(image_frames: pd.DataFrame, value_frames: pd.DataFrame, label: str) -> pd.DataFrame:
    merged = per_frame_motion(image_frames, value_frames)
    rows = []
    for ep, g in merged.groupby("dataset_episode"):
        g = g.sort_values("row")
        vis = g["vis_motion"].to_numpy(dtype=float)
        st = g["state_step_l2"].to_numpy(dtype=float)
        ac = g["action_step_l2"].to_numpy(dtype=float)

        # --- (1) cross-modal lag -------------------------------------------
        # state_step_l2 is a forward difference, so align it with the visual
        # difference series by shifting one frame back before correlating.
        st_al = np.r_[st[1:], np.nan]
        ac_al = np.r_[ac[1:], np.nan]
        lag_s, peak_s, zero_s, _ = cross_corr_profile(vis, st_al)
        lag_a, peak_a, zero_a, _ = cross_corr_profile(vis, ac_al)

        # --- (3) static vision while the robot keeps moving -----------------
        moving = np.isfinite(st) & (st > np.nanpercentile(st[np.isfinite(st)], 60) if np.isfinite(st).any() else False)
        still = np.isfinite(vis) & (vis <= 1.0)  # aHash distance ~0 -> image frozen
        frozen_while_moving = int(np.sum(still & moving))
        frozen_run = int(longest_true_run(still))

        rows.append(
            {
                "dataset": label,
                "episode": int(ep),
                "n_frames": int(len(g)),
                "vis_motion_mean": float(np.nanmean(vis)),
                "vis_motion_p90": float(np.nanpercentile(vis[np.isfinite(vis)], 90)) if np.isfinite(vis).any() else np.nan,
                "lag_vs_state": lag_s,
                "peak_corr_state": peak_s,
                "zero_corr_state": zero_s,
                "lag_vs_action": lag_a,
                "peak_corr_action": peak_a,
                "zero_corr_action": zero_a,
                "frozen_while_moving_frames": frozen_while_moving,
                "frozen_run_max": frozen_run,
                "vis_motion_zero_frac": float(np.mean(still)) if len(still) else np.nan,
            }
        )
    return pd.DataFrame(rows)


def longest_true_run(mask: np.ndarray) -> int:
    best = cur = 0
    for v in mask:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def glitch_probe(image_frames: pd.DataFrame, label: str) -> pd.DataFrame:
    """花屏 / screen-corruption candidates.

    A glitched frame keeps ordinary brightness but carries abnormal high-frequency
    energy and/or an abnormal colour-channel relationship, while its neighbours
    are normal. That separates it from blur (low high-frequency energy) and from
    black/over-exposed frames (abnormal brightness).
    """
    img = image_frames[image_frames["decode_ok"] == True].copy()  # noqa: E712
    if img.empty:
        return pd.DataFrame()
    rows = []
    for (ep, stream), g in img.groupby(["dataset_episode", "stream"]):
        g = g.sort_values("row")
        lap = pd.to_numeric(g["laplacian_var"], errors="coerce")
        grad = pd.to_numeric(g["gradient_energy"], errors="coerce")
        luma = pd.to_numeric(g["mean_luma"], errors="coerce")
        std = pd.to_numeric(g["std_luma"], errors="coerce")
        med_lap = float(lap.median()) if lap.notna().any() else np.nan
        med_grad = float(grad.median()) if grad.notna().any() else np.nan

        # Robust z against the episode's own median (glitch is a local outlier)
        mad = float((lap - med_lap).abs().median()) if lap.notna().any() else 0.0
        z = (lap - med_lap) / (1.4826 * mad) if mad > 1e-6 else pd.Series(np.zeros(len(lap)), index=lap.index)

        normal_brightness = (luma.between(25, 235)) & (std > 8)
        hi_freq = (z > 6) | (grad > med_grad * 4 + 5)
        cand = int((normal_brightness & hi_freq).sum())
        rows.append(
            {
                "dataset": label,
                "episode": int(ep),
                "stream": stream,
                "n": int(len(g)),
                "laplacian_median": med_lap,
                "laplacian_p99": float(lap.quantile(0.99)) if lap.notna().any() else np.nan,
                "laplacian_max": float(lap.max()) if lap.notna().any() else np.nan,
                "gradient_median": med_grad,
                "gradient_p99": float(grad.quantile(0.99)) if grad.notna().any() else np.nan,
                "glitch_candidates": cand,
                "glitch_rate": cand / len(g) if len(g) else np.nan,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    print("[probe] loading cached per-frame features ...")
    ref_img = read_csv("reference_image_frames.csv")
    ref_val = read_csv("reference_value_frames.csv")
    tst_img = read_csv("test_image_frames.csv")
    tst_val = read_csv("test_value_frames.csv")

    print("[probe] visual-motor lag + occlusion proxy ...")
    lag_ref = analyse_dataset(ref_img, ref_val, "reference")
    lag_tst = analyse_dataset(tst_img, tst_val, "test")
    lag_all = pd.concat([lag_ref, lag_tst], ignore_index=True)
    lag_all.to_csv(OUT_DIR / "gap_probe_visual_motor_lag.csv", index=False, encoding="utf-8-sig")

    print("[probe] glitch / screen-corruption proxy ...")
    gl_ref = glitch_probe(ref_img, "reference")
    gl_tst = glitch_probe(tst_img, "test")
    gl_all = pd.concat([gl_ref, gl_tst], ignore_index=True)
    gl_all.to_csv(OUT_DIR / "gap_probe_glitch.csv", index=False, encoding="utf-8-sig")

    # ---- summary -------------------------------------------------------
    def lag_summary(df: pd.DataFrame, name: str) -> dict:
        nonzero = df[df["lag_vs_state"] != 0]
        weak = df[df["peak_corr_state"].abs() < 0.15]
        return {
            "dataset": name,
            "episodes": int(len(df)),
            "abs_lag_state_mean": float(df["lag_vs_state"].abs().mean()),
            "abs_lag_state_p90": float(df["lag_vs_state"].abs().quantile(0.9)),
            "episodes_peak_lag_nonzero": int(len(nonzero)),
            "episodes_peak_lag_ge3": int((df["lag_vs_state"].abs() >= 3).sum()),
            "peak_corr_state_mean": float(df["peak_corr_state"].mean()),
            "zero_corr_state_mean": float(df["zero_corr_state"].mean()),
            "episodes_weak_corr_lt0.15": int(len(weak)),
            "frozen_run_max_p90": float(df["frozen_run_max"].quantile(0.9)),
            "episodes_frozen_run_ge30": int((df["frozen_run_max"] >= 30).sum()),
            "frozen_while_moving_total": int(df["frozen_while_moving_frames"].sum()),
        }

    def glitch_summary(df: pd.DataFrame, name: str) -> dict:
        per_ep = df.groupby(["dataset", "episode"], as_index=False)["glitch_candidates"].sum()
        return {
            "dataset": name,
            "streams": int(len(df)),
            "glitch_frames_total": int(df["glitch_candidates"].sum()),
            "episodes_with_ge5_glitch": int((per_ep["glitch_candidates"] >= 5).sum()),
            "episodes_with_ge1_glitch": int((per_ep["glitch_candidates"] >= 1).sum()),
            "max_glitch_in_one_stream": int(df["glitch_candidates"].max()),
            "laplacian_p99_median": float(df["laplacian_p99"].median()),
        }

    summary = {
        "visual_motor_lag": [lag_summary(lag_ref, "reference"), lag_summary(lag_tst, "test")],
        "glitch": [glitch_summary(gl_ref, "reference"), glitch_summary(gl_tst, "test")],
    }
    (OUT_DIR / "gap_probe_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    print("\n[probe] top-10 test episodes by |peak lag| (visual vs state):")
    top = lag_tst.reindex(lag_tst["lag_vs_state"].abs().sort_values(ascending=False).index).head(10)
    print(
        top[
            ["episode", "n_frames", "lag_vs_state", "peak_corr_state", "zero_corr_state",
             "frozen_run_max", "frozen_while_moving_frames"]
        ].to_string(index=False)
    )

    print("\n[probe] top-10 test stream-level glitch candidates:")
    gtop = gl_tst.sort_values("glitch_candidates", ascending=False).head(10)
    print(gtop[["episode", "stream", "n", "laplacian_median", "laplacian_p99", "glitch_candidates"]].to_string(index=False))


if __name__ == "__main__":
    main()
