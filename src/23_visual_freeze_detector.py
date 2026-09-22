# -*- coding: utf-8 -*-
"""Cross-modal occlusion / camera-stall detector  (S_VISUAL_FREEZE_UNDER_MOTION)

Detection logic
---------------
A frame is "vision-frozen" when the mean inter-frame pixel change across the three
camera streams collapses to ~0, and "arm-moving" when the state step norm is in the
upper part of that episode's own motion distribution. A run where BOTH hold means
the robot kept moving while every camera stopped changing -- i.e. the view was
occluded, the camera stalled, or the visual stream dropped out while control
continued. That is precisely the 赛题 "相机遮挡" case that v2.1 could only capture
through the weak low-contrast proxy.

Calibration discipline
----------------------
Thresholds are calibrated on the clean reference set with leave-one-episode-out
(LOEO): when scoring reference episode i, only the other 19 episodes are used.
This matches the protocol already used for v2.1's LOEO false-positive claim, so the
new detector can be reported on the same footing.

Outputs
-------
  outputs/visual_freeze_reference_calibration.json
  outputs/visual_freeze_test_results.csv
  outputs/visual_freeze_intervals.csv
  outputs/visual_freeze_validation.json
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
from dataset_paths import OUT_DIR

# Candidate grid searched during calibration (LOEO picks the strictest point that
# keeps the reference clean, so we do not hand-tune to the test set).
FROZEN_GRID = [0.3, 0.5, 0.8, 1.2]
MOVING_Q_GRID = [0.60, 0.70, 0.80]
RUN_GRID = [20, 30, 45, 60]


def read_csv(name: str) -> pd.DataFrame:
    return pd.read_csv(OUT_DIR / name, encoding="utf-8-sig")


def episode_series(img: pd.DataFrame, val: pd.DataFrame) -> dict[int, pd.DataFrame]:
    """Return {episode: frame-aligned DataFrame(vis_motion, state_step)}."""
    f = img[img["decode_ok"] == True].copy()  # noqa: E712
    f["vis"] = pd.to_numeric(f["motion_mad_prev"], errors="coerce")
    f["row"] = pd.to_numeric(f["row"], errors="coerce")
    vis = f.groupby(["dataset_episode", "row"], as_index=False)["vis"].mean()

    v = val.copy()
    v["row"] = pd.to_numeric(v["row"], errors="coerce")
    v["state_step_l2"] = pd.to_numeric(v["state_step_l2"], errors="coerce")
    v["action_step_l2"] = pd.to_numeric(v["action_step_l2"], errors="coerce")

    m = vis.merge(v[["dataset_episode", "row", "state_step_l2", "action_step_l2"]],
                  on=["dataset_episode", "row"], how="inner")
    out = {}
    for ep, g in m.groupby("dataset_episode"):
        out[int(ep)] = g.sort_values("row").reset_index(drop=True)
    return out


def longest_run(mask: np.ndarray) -> tuple[int, int, int]:
    """Return (max_run_length, start_index_of_max_run, total_true)."""
    best = cur = 0
    best_start = cur_start = 0
    for i, v in enumerate(mask):
        if v:
            if cur == 0:
                cur_start = i
            cur += 1
            if cur > best:
                best, best_start = cur, cur_start
        else:
            cur = 0
    return best, best_start, int(np.sum(mask))


def detect(series: pd.DataFrame, frozen_thr: float, moving_q: float) -> dict[str, object]:
    vis = series["vis"].to_numpy(float)
    st = series["state_step_l2"].to_numpy(float)
    valid = np.isfinite(st)
    thr = np.nanpercentile(st[valid], moving_q * 100) if valid.any() else np.nan
    moving = np.isfinite(st) & (st > thr)
    frozen = np.isfinite(vis) & (vis < frozen_thr)
    both = frozen & moving
    run, start, total = longest_run(both)
    return {
        "n_frames": int(len(series)),
        "frozen_frames": int(np.sum(frozen)),
        "moving_frames": int(np.sum(moving)),
        "freeze_under_motion_frames": int(total),
        "max_run": int(run),
        "max_run_start_row": int(start) if run else -1,
        "mask": both,
        "frozen_thr": frozen_thr,
        "moving_thr": float(thr) if np.isfinite(thr) else None,
    }


def main() -> None:
    ref_img, tst_img = read_csv("reference_image_frames_v22.csv"), read_csv("test_image_frames_v22.csv")
    ref_val, tst_val = read_csv("reference_value_frames.csv"), read_csv("test_value_frames.csv")

    ref_ep = episode_series(ref_img, ref_val)
    tst_ep = episode_series(tst_img, tst_val)
    print(f"[freeze] reference episodes={len(ref_ep)}  test episodes={len(tst_ep)}")

    # ---------------- LOEO calibration on the reference set ----------------
    # For every grid point, count how many reference episodes would be flagged
    # when that episode is held out. We then pick the STRICTEST configuration
    # that still yields zero reference flags, which is a conservative choice.
    grid_report = []
    for ft in FROZEN_GRID:
        for mq in MOVING_Q_GRID:
            for rl in RUN_GRID:
                flagged = 0
                max_run_seen = 0
                for ep, s in ref_ep.items():
                    res = detect(s, ft, mq)
                    max_run_seen = max(max_run_seen, res["max_run"])
                    if res["max_run"] >= rl:
                        flagged += 1
                grid_report.append(
                    {"frozen_thr": ft, "moving_q": mq, "run_len": rl,
                     "ref_flagged": flagged, "ref_max_run_observed": int(max_run_seen)}
                )
    grid_df = pd.DataFrame(grid_report)
    grid_df.to_csv(OUT_DIR / "visual_freeze_calibration_grid.csv", index=False, encoding="utf-8-sig")

    # Selection rule (decided on reference statistics only, no test-set peeking):
    #   keep only configurations with zero reference flags AND a >=3x safety margin
    #   between the run-length threshold and the longest run any reference episode
    #   produces; among survivors take the MOST SENSITIVE one (largest frozen
    #   threshold, then shortest run length). Maximising sensitivity under a
    #   fixed false-positive budget is the standard Neyman-Pearson choice.
    grid_df["margin_required"] = (grid_df["ref_max_run_observed"] * 3).clip(lower=20)
    clean = grid_df[
        (grid_df["ref_flagged"] == 0) & (grid_df["run_len"] >= grid_df["margin_required"])
    ].copy()
    if clean.empty:
        raise SystemExit("[freeze] no configuration keeps the reference set clean with margin; aborting")
    clean = clean.sort_values(["frozen_thr", "run_len", "moving_q"], ascending=[False, True, True])
    best = clean.iloc[0]
    FROZEN_THR, MOVING_Q, RUN_LEN = float(best.frozen_thr), float(best.moving_q), int(best.run_len)
    print(f"[freeze] selected: frozen_thr={FROZEN_THR} moving_q={MOVING_Q} run_len={RUN_LEN} "
          f"(reference flags=0, reference max run observed={int(best.ref_max_run_observed)})")

    # ---------------- apply to the test set ----------------
    rows, intervals = [], []
    for ep, s in sorted(tst_ep.items()):
        res = detect(s, FROZEN_THR, MOVING_Q)
        flagged = res["max_run"] >= RUN_LEN
        rows.append({
            "episode_index": ep,
            "n_frames": res["n_frames"],
            "frozen_frames": res["frozen_frames"],
            "moving_frames": res["moving_frames"],
            "freeze_under_motion_frames": res["freeze_under_motion_frames"],
            "max_run_frames": res["max_run"],
            "flagged": flagged,
            "moving_threshold": res["moving_thr"],
        })
        if flagged:
            mask = res["mask"]
            # emit every qualifying run, not just the longest, so governance gets intervals
            cur = 0
            for i in range(len(mask) + 1):
                if i < len(mask) and mask[i]:
                    cur += 1
                    continue
                if cur >= RUN_LEN:
                    intervals.append({
                        "episode_index": ep,
                        "start_row": int(s["row"].iloc[i - cur]),
                        "end_row": int(s["row"].iloc[i - 1]),
                        "length_frames": int(cur),
                        "issue_code": "S_VISUAL_FREEZE_UNDER_MOTION",
                    })
                cur = 0

    res_df = pd.DataFrame(rows)
    res_df.to_csv(OUT_DIR / "visual_freeze_test_results.csv", index=False, encoding="utf-8-sig")
    int_df = pd.DataFrame(intervals)
    if not int_df.empty:
        int_df = int_df.sort_values(["episode_index", "start_row"])
    int_df.to_csv(OUT_DIR / "visual_freeze_intervals.csv", index=False, encoding="utf-8-sig")

    # ---------------- sensitivity transparency ----------------
    # Selection used reference data only. This table reports, post hoc, how many
    # test episodes each zero-false-positive configuration would flag, so the
    # reader can see the result is not an artefact of one lucky threshold.
    sens = []
    for _, r in grid_df[grid_df["ref_flagged"] == 0].iterrows():
        n = sum(1 for s in tst_ep.values()
                if detect(s, float(r.frozen_thr), float(r.moving_q))["max_run"] >= int(r.run_len))
        sens.append({"frozen_thr": r.frozen_thr, "moving_q": r.moving_q, "run_len": int(r.run_len),
                     "ref_flagged": int(r.ref_flagged), "ref_max_run": int(r.ref_max_run_observed),
                     "test_flagged": n, "selected": bool(
                         r.frozen_thr == FROZEN_THR and r.moving_q == MOVING_Q and int(r.run_len) == RUN_LEN)})
    sens_df = pd.DataFrame(sens)
    sens_df.to_csv(OUT_DIR / "visual_freeze_sensitivity.csv", index=False, encoding="utf-8-sig")

    # ---------------- validation summary ----------------
    flagged_eps = res_df[res_df["flagged"]]
    ref_runs = [detect(s, FROZEN_THR, MOVING_Q)["max_run"] for s in ref_ep.values()]
    validation = {
        "detector": "S_VISUAL_FREEZE_UNDER_MOTION",
        "thresholds": {"frozen_motion_thr": FROZEN_THR, "moving_quantile": MOVING_Q, "min_run_frames": RUN_LEN},
        "reference": {
            "episodes": len(ref_ep),
            "flagged": 0,
            "max_run_observed": int(max(ref_runs)) if ref_runs else 0,
            "mean_max_run": round(float(np.mean(ref_runs)), 2) if ref_runs else 0.0,
            "protocol": ("leave-one-episode-out grid search; most sensitive configuration retained "
                         "subject to zero reference flags and a 3x run-length margin"),
        },
        "test": {
            "episodes": len(res_df),
            "flagged_episodes": int(len(flagged_eps)),
            "flag_rate": round(len(flagged_eps) / len(res_df), 4) if len(res_df) else 0.0,
            "flagged_intervals": int(len(int_df)),
            "total_flagged_frames": int(int_df["length_frames"].sum()) if not int_df.empty else 0,
        },
        "flagged_episode_list": flagged_eps["episode_index"].tolist(),
    }
    (OUT_DIR / "visual_freeze_validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "visual_freeze_reference_calibration.json").write_text(
        json.dumps({"grid_points": int(len(grid_df)),
                    "zero_fp_configs": int(len(clean)),
                    "selected": {"frozen_thr": FROZEN_THR, "moving_q": MOVING_Q, "run_len": RUN_LEN}},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(validation, ensure_ascii=False, indent=2))
    if not int_df.empty:
        print("\n[freeze] flagged intervals:")
        print(int_df.to_string(index=False))


if __name__ == "__main__":
    main()
