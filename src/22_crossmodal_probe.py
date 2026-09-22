# -*- coding: utf-8 -*-
"""Cross-modal probe v2 — uses real inter-frame pixel motion (motion_mad_prev)
instead of the coarse aHash distance used in 20_explore_gaps.py.

Questions this answers, with the reference set as control:

Q1  Is there a usable visual-motor lag signal? If reference episodes lock to
    lag 0 with high correlation, then a test episode that peaks far from 0 (or
    decorrelates) is real evidence of cross-modal desync -- which would upgrade
    "同步" from the admitted L1 (index/alignment only) to a measured L2.
Q2  Does 花屏/screen-corruption separate from normal texture once we use a
    reference-calibrated noise+chroma test?
Q3  Does occlusion (frozen vision while the arm keeps moving) separate from the
    reference set, or is "image static" simply how this robot pauses?
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
from dataset_paths import OUT_DIR


def read_csv(name: str) -> pd.DataFrame:
    return pd.read_csv(OUT_DIR / name, encoding="utf-8-sig")


def z(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    m = np.isfinite(x)
    if m.sum() < 10:
        return np.zeros_like(x)
    sd = np.nanstd(x[m])
    if sd < 1e-12:
        return np.zeros_like(x)
    out = np.zeros_like(x)
    out[m] = (x[m] - np.nanmean(x[m])) / sd
    return out


def best_lag(vis: np.ndarray, mot: np.ndarray, max_lag: int = 6) -> tuple[int, float, float]:
    a, b = z(vis), z(mot)
    n = len(a)
    lags, scores = [], []
    for lag in range(-max_lag, max_lag + 1):
        x, y = (a[: n - lag], b[lag:]) if lag >= 0 else (a[-lag:], b[: n + lag])
        if len(x) < 15:
            continue
        lags.append(lag)
        scores.append(float(np.nanmean(x * y)))
    if not scores:
        return 0, 0.0, 0.0
    s = np.asarray(scores)
    i = int(np.nanargmax(np.abs(s)))
    zero = float(s[int(np.argmin(np.abs(np.asarray(lags))))])
    return int(lags[i]), float(s[i]), zero


def build_motion(frames: pd.DataFrame) -> pd.DataFrame:
    f = frames[frames["decode_ok"] == True].copy()  # noqa: E712
    f["motion"] = pd.to_numeric(f["motion_mad_prev"], errors="coerce")
    f["row"] = pd.to_numeric(f["row"], errors="coerce")
    return f.groupby(["dataset_episode", "row"], as_index=False)["motion"].mean()


def main() -> None:
    ref_img = read_csv("reference_image_frames_v22.csv")
    tst_img = read_csv("test_image_frames_v22.csv")
    ref_val = read_csv("reference_value_frames.csv")
    tst_val = read_csv("test_value_frames.csv")

    # ---------------- Q1 visual-motor lag -------------------------------
    def lag_table(img, val, label):
        vis = build_motion(img)
        v = val.copy()
        v["row"] = pd.to_numeric(v["row"], errors="coerce")
        v["state_step_l2"] = pd.to_numeric(v["state_step_l2"], errors="coerce")
        m = vis.merge(v[["dataset_episode", "row", "state_step_l2"]], on=["dataset_episode", "row"])
        rows = []
        for ep, g in m.groupby("dataset_episode"):
            g = g.sort_values("row")
            vm = g["motion"].to_numpy(float)
            st = g["state_step_l2"].to_numpy(float)
            st = np.r_[st[1:], np.nan]  # align forward-difference with visual difference
            lag, peak, zero = best_lag(vm, st)
            rows.append(
                {
                    "dataset": label,
                    "episode": int(ep),
                    "n": int(len(g)),
                    "best_lag": lag,
                    "peak_corr": round(peak, 4),
                    "zero_corr": round(zero, 4),
                    "corr_gain": round(abs(peak) - abs(zero), 4),
                }
            )
        return pd.DataFrame(rows)

    lag_ref, lag_tst = lag_table(ref_img, ref_val, "reference"), lag_table(tst_img, tst_val, "test")
    lag = pd.concat([lag_ref, lag_tst], ignore_index=True)
    lag.to_csv(OUT_DIR / "crossmodal_lag.csv", index=False, encoding="utf-8-sig")

    # ---------------- Q2 screen corruption (花屏) ------------------------
    feats = ["noise_residual", "chroma_spread", "laplacian_var"]
    for d in (ref_img, tst_img):
        for c in feats:
            d[c] = pd.to_numeric(d[c], errors="coerce")

    calib = {}
    for stream, g in ref_img.groupby("stream"):
        calib[stream] = {c: {q: float(g[c].quantile(q)) for q in (0.99, 0.995, 0.999)} | {"max": float(g[c].max())}
                         for c in feats}

    def glitch_flags(img, label):
        out = []
        for (ep, stream), g in img.groupby(["dataset_episode", "stream"]):
            g = g.sort_values("row")
            c = calib.get(stream)
            if c is None:
                continue
            over = (
                (g["noise_residual"] > c["noise_residual"]["max"])
                & (g["chroma_spread"] > c["chroma_spread"][0.999])
                & (g["mean_luma"].between(20, 240))
            )
            n = int(over.sum())
            out.append({"dataset": label, "episode": int(ep), "stream": stream, "n": len(g), "glitch_frames": n})
        return pd.DataFrame(out)

    gl = pd.concat([glitch_flags(ref_img, "reference"), glitch_flags(tst_img, "test")], ignore_index=True)
    gl.to_csv(OUT_DIR / "crossmodal_glitch.csv", index=False, encoding="utf-8-sig")

    # ---------------- Q3 occlusion: frozen vision + moving arm -----------
    def occl_table(img, val, label):
        vis = build_motion(img)
        v = val.copy()
        v["row"] = pd.to_numeric(v["row"], errors="coerce")
        v["state_step_l2"] = pd.to_numeric(v["state_step_l2"], errors="coerce")
        m = vis.merge(v[["dataset_episode", "row", "state_step_l2"]], on=["dataset_episode", "row"])
        rows = []
        for ep, g in m.groupby("dataset_episode"):
            g = g.sort_values("row")
            vm = g["motion"].to_numpy(float)
            st = g["state_step_l2"].to_numpy(float)
            thr = np.nanpercentile(st[np.isfinite(st)], 70) if np.isfinite(st).any() else np.nan
            moving = np.isfinite(st) & (st > thr)
            frozen = np.isfinite(vm) & (vm < 0.5)  # 64x64 mean |delta| < 0.5 grey levels
            rows.append(
                {
                    "dataset": label,
                    "episode": int(ep),
                    "frozen_frames": int(np.sum(frozen)),
                    "frozen_frac": round(float(np.mean(frozen)), 4),
                    "frozen_while_moving": int(np.sum(frozen & moving)),
                    "max_frozen_run": int(longest_run(frozen)),
                }
            )
        return pd.DataFrame(rows)

    def longest_run(mask):
        best = cur = 0
        for v in mask:
            cur = cur + 1 if v else 0
            best = max(best, cur)
        return best

    oc = pd.concat([occl_table(ref_img, ref_val, "reference"), occl_table(tst_img, tst_val, "test")], ignore_index=True)
    oc.to_csv(OUT_DIR / "crossmodal_occlusion.csv", index=False, encoding="utf-8-sig")

    # ---------------- report --------------------------------------------
    def lag_sum(d, name):
        return {
            "dataset": name,
            "episodes": len(d),
            "lag0_share": round(float((d.best_lag == 0).mean()), 3),
            "abs_lag_mean": round(float(d.best_lag.abs().mean()), 3),
            "at_boundary_6": int((d.best_lag.abs() == 6).sum()),
            "zero_corr_mean": round(float(d.zero_corr.mean()), 3),
            "zero_corr_p10": round(float(d.zero_corr.quantile(0.10)), 3),
            "peak_corr_mean": round(float(d.peak_corr.mean()), 3),
        }

    def gl_sum(d, name):
        per_ep = d.groupby(["dataset", "episode"], as_index=False)["glitch_frames"].sum()
        return {
            "dataset": name,
            "episodes": len(per_ep),
            "glitch_frames_total": int(per_ep.glitch_frames.sum()),
            "episodes_ge1": int((per_ep.glitch_frames >= 1).sum()),
            "episodes_ge10": int((per_ep.glitch_frames >= 10).sum()),
            "max_in_episode": int(per_ep.glitch_frames.max()),
        }

    def oc_sum(d, name):
        return {
            "dataset": name,
            "episodes": len(d),
            "frozen_frac_mean": round(float(d.frozen_frac.mean()), 4),
            "frozen_while_moving_mean": round(float(d.frozen_while_moving.mean()), 2),
            "max_frozen_run_median": float(d.max_frozen_run.median()),
            "episodes_run_ge60": int((d.max_frozen_run >= 60).sum()),
        }

    report = {
        "Q1_visual_motor_lag": [lag_sum(lag_ref, "reference"), lag_sum(lag_tst, "test")],
        "Q2_glitch": [gl_sum(gl[gl.dataset == "reference"], "reference"), gl_sum(gl[gl.dataset == "test"], "test")],
        "Q3_occlusion": [oc_sum(oc[oc.dataset == "reference"], "reference"), oc_sum(oc[oc.dataset == "test"], "test")],
    }
    (OUT_DIR / "crossmodal_probe_summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))

    print("\n[Q1] test episodes with weakest lag-0 visual-motor coupling (desync candidates):")
    t = lag_tst.sort_values("zero_corr").head(12)
    print(t[["episode", "n", "best_lag", "peak_corr", "zero_corr", "corr_gain"]].to_string(index=False))

    print("\n[Q2] test episodes with most 花屏 candidates:")
    g2 = gl[gl.dataset == "test"].groupby("episode", as_index=False)["glitch_frames"].sum()
    print(g2.sort_values("glitch_frames", ascending=False).head(10).to_string(index=False))

    print("\n[Q3] test episodes with longest frozen-vision runs (occlusion candidates):")
    print(oc[oc.dataset == "test"].sort_values("max_frozen_run", ascending=False).head(10).to_string(index=False))


if __name__ == "__main__":
    main()
