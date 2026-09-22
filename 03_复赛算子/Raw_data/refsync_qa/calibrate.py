# -*- coding: utf-8 -*-
"""Reference-set calibration.

All statistical thresholds are derived from episodes declared clean (the
competition "参考集").  Hard constraints (Rotation-6D, NaN, index rules) are
not calibrated.  Each key records its source so the report can state which
thresholds are reference statistics and which are engineering floors.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from . import config
from .features import arm_steps, xcorr_lags


def _q(x: list[float] | np.ndarray, q: float) -> float:
    a = np.asarray(x, dtype=float)
    a = a[np.isfinite(a)]
    return float(np.quantile(a, q)) if a.size else math.nan


def _full_step(M: np.ndarray) -> np.ndarray:
    st = np.full(len(M), np.nan)
    if len(M) > 1:
        st[1:] = np.linalg.norm(np.diff(M, axis=0), axis=1)
    return st


def _screen(sf: dict[str, Any], thr: dict[str, Any]) -> np.ndarray:
    return sf["decode_ok"] & (
        (sf["dark"] >= thr["screen_dark_fraction"]) | (sf["bright"] >= thr["screen_bright_fraction"]) | (sf["std"] <= thr["screen_std_luma"])
    )


def calibrate(ref_feats: list[dict[str, Any]], opts: dict[str, Any] | None = None) -> dict[str, Any]:
    opts = {**config.DEFAULT_OPTIONS, **(opts or {})}
    layout = opts.get("layout", config.DEFAULT_LAYOUT)
    thr: dict[str, Any] = json.loads(json.dumps(config.FALLBACK_THRESHOLDS))
    src: dict[str, str] = {}
    feats = [f for f in ref_feats if f.get("read_ok") and f.get("n", 0) > 1]
    if not feats:
        thr["_source"] = {"all": "fallback (no readable reference episode)"}
        return thr

    # --- image sharpness lower bounds per stream (frame-level 0.2% quantile x margin) ---
    per_stream: dict[str, dict[str, list[float]]] = {}
    for f in feats:
        for col, sf in f.get("streams", {}).items():
            ok = sf["decode_ok"] & ~_screen(sf, thr)
            d = per_stream.setdefault(col, {"lap": [], "grad": [], "bytes": []})
            d["lap"] += sf["lap"][ok].tolist()
            d["grad"] += sf["grad"][ok].tolist()
            d["bytes"] += sf["bytes"][ok].tolist()
    thr["blur"] = {
        col: {
            "lap_min": _q(d["lap"], 0.002) * 0.50,
            "grad_min": _q(d["grad"], 0.002) * 0.65,
            "byte_min": _q(d["bytes"], 0.002) * 0.50,
        }
        for col, d in per_stream.items() if len(d["lap"]) >= 50
    }
    src["blur"] = "reference frame-level 0.2% quantile x (0.50/0.65/0.50)"

    # --- numeric steps ---
    s_steps, a_steps, sa_rmse, sa_max, sa_lags = [], [], [], [], []
    grp_max: dict[str, list[float]] = {}
    for f in feats:
        S, A = f["state"], f["action"]
        sf_ok = f["state_dim_ok"] & np.isfinite(S).all(axis=1)
        af_ok = f["action_dim_ok"] & np.isfinite(A).all(axis=1)
        s_steps += _full_step(np.where(sf_ok[:, None], S, np.nan)).tolist()
        a_steps += _full_step(np.where(af_ok[:, None], A, np.nan)).tolist()
        n = len(S)
        best = None
        for lag in range(-3, 4):
            j = np.arange(n) + lag
            ok = (j >= 0) & (j < n)
            ok[ok] &= af_ok[ok] & sf_ok[j[ok]]
            if ok.sum() < 10:
                continue
            d = A[ok] - S[j[ok]]
            rm = np.sqrt(np.mean(d * d, axis=1))
            if best is None or np.mean(rm) < best[0]:
                best = (float(np.mean(rm)), lag, rm, np.max(np.abs(d), axis=1))
        if best:
            sa_lags.append(best[1])
            sa_rmse += best[2].tolist()
            sa_max += best[3].tolist()
            if S.shape[1] == layout["dim"]:
                j = np.arange(n) + best[1]
                ok = (j >= 0) & (j < n)
                ok[ok] &= af_ok[ok] & sf_ok[j[ok]]
                d = np.abs(A[ok] - S[j[ok]])
                for grp in ("pos", "rot6d", "gripper"):
                    cols = [c for spec in layout["arms"].values() for c in spec[grp]]
                    grp_max.setdefault(grp, []).extend(d[:, cols].max(axis=1).tolist())
    thr["state_step_max"] = max(0.5, _q(s_steps, 0.999) * 1.5)
    thr["action_step_max"] = max(0.5, _q(a_steps, 0.999) * 1.5)
    thr["sa_residual_rmse"] = max(0.15, _q(sa_rmse, 0.999) * 1.25)
    thr["sa_residual_max_abs"] = max(0.40, _q(sa_max, 0.999) * 1.25)
    floors = {"pos": 0.08, "rot6d": 0.25, "gripper": 0.5}
    thr["sa_group_max_abs"] = {g: max(floors[g], 2.0 * float(np.nanmax(v))) for g, v in grp_max.items() if v}
    thr["_reference_sa_group_max"] = {g: float(np.nanmax(v)) for g, v in grp_max.items() if v}
    thr["_reference_sa_lags"] = sa_lags
    src["steps"] = "max(0.5, reference 99.9% x 1.5)"
    src["sa_residual"] = "episode-level fixed lag; rmse max(0.15, ref 99.9% x1.25); per-group |a-s| > max(floor, 2 x reference max) for pos/rot6d/gripper"

    # --- training-value proxies (same definitions as the detector) ---
    idle_s = _q(s_steps, 0.10)
    idle_a = _q(a_steps, 0.10)
    thr["idle_state_cut"] = max(1e-4, idle_s)
    thr["idle_action_cut"] = max(1e-4, idle_a)
    thr["interaction_state_cut"] = _q(s_steps, 0.50)
    thr["interaction_action_cut"] = _q(a_steps, 0.50)
    idle_rates, inter_rates, divs = [], [], []
    for f in feats:
        S, A = f["state"], f["action"]
        ss = _full_step(np.where(np.isfinite(S).all(axis=1)[:, None], S, np.nan))
        aa = _full_step(np.where(np.isfinite(A).all(axis=1)[:, None], A, np.nan))
        b = np.isfinite(ss) & np.isfinite(aa)
        if b.sum() >= 5:
            idle_rates.append(float(((ss[b] <= thr["idle_state_cut"]) & (aa[b] <= thr["idle_action_cut"])).mean()))
            inter_rates.append(float(((ss[b] >= thr["interaction_state_cut"]) | (aa[b] >= thr["interaction_action_cut"])).mean()))
        dv = []
        for sf in f.get("streams", {}).values():
            h = [x for x, d in zip(sf["ahash"], sf["decode_ok"]) if d and x]
            if h:
                dv.append(len(set(h)) / len(h))
        if dv:
            divs.append(float(np.mean(dv)))
    # value hints fire only beyond every clean reference episode (with a 10% margin)
    thr["idle_rate_high"] = max(idle_rates) * 1.1 if idle_rates else thr["idle_rate_high"]
    thr["interaction_rate_low"] = min(inter_rates) * 0.9 if inter_rates else thr["interaction_rate_low"]
    thr["visual_diversity_low"] = min(divs) * 0.9 if divs else thr["visual_diversity_low"]
    src["value"] = "step cuts: reference 10%/50% quantiles; episode rates: beyond reference max/min by 10%"

    # --- arm motion, frozen stream, visual–kinematic lag ---
    arm_all, vk = [], {}
    frozen_runs: list[int] = []
    for f in feats:
        S = f["state"]
        if S.shape[1] != layout["dim"]:
            continue
        st = arm_steps(np.where(np.isfinite(S).all(axis=1)[:, None], S, np.nan), layout)
        for v in st.values():
            arm_all += v.tolist()
        for col, sf in f.get("streams", {}).items():
            arm = layout.get("camera_arm", {}).get(col)
            if arm in st:
                lags = xcorr_lags(sf["motion"], st[arm], int(opts.get("max_lag_search", 12)))
                if lags:
                    b = max(lags, key=lags.get)
                    vk.setdefault(col, []).append((b, lags[b]))
    thr["arm_moving_step"] = _q(arm_all, 0.50)
    for f in feats:
        S = f["state"]
        if S.shape[1] != layout["dim"]:
            continue
        st = arm_steps(np.where(np.isfinite(S).all(axis=1)[:, None], S, np.nan), layout)
        for col, sf in f.get("streams", {}).items():
            arm = layout.get("camera_arm", {}).get(col)
            mv = st[arm] if arm in st else np.fmax.reduce(np.vstack(list(st.values())), axis=0)
            frozen = sf["decode_ok"] & ~_screen(sf, thr) & (sf["motion"] < thr["freeze_motion"])
            moving = np.isfinite(mv) & (mv > thr["arm_moving_step"])
            run = best = 0
            for fz, m in zip(frozen, moving):
                run = run + 1 if (fz and m) else 0
                best = max(best, run)
            frozen_runs.append(best)
    thr["freeze_min_run"] = int(max(10, 3 * max(frozen_runs or [0])))
    thr["_reference_max_frozen_moving_run"] = int(max(frozen_runs or [0]))
    thr["vk_ref_lag"] = {}
    thr["_reference_vk"] = {}
    for col, pairs in vk.items():
        lags = [p[0] for p in pairs]
        rs = [p[1] for p in pairs]
        mode = max(set(lags), key=lags.count)
        thr["vk_ref_lag"][col] = int(mode)
        thr["_reference_vk"][col] = {"lags": lags, "r": [round(r, 3) for r in rs]}
    src["vk"] = "mode of reference best lag between wrist-camera motion and same-arm step"
    src["freeze"] = "motion < 0.3 grey levels (reference 0.1%-1% quantile band); run >= max(10, 3 x longest reference frozen-while-moving run)"
    thr["_source"] = src
    thr["_reference_episodes"] = len(feats)
    return thr


def load_thresholds(path: str | Path | None) -> dict[str, Any]:
    thr = json.loads(json.dumps(config.FALLBACK_THRESHOLDS))
    if path and Path(path).exists():
        thr.update(json.loads(Path(path).read_text(encoding="utf-8")))
    return thr
