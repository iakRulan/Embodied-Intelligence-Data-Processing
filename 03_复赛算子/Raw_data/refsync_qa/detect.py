# -*- coding: utf-8 -*-
"""Rule engine: one feature dict -> frame-level codes + episode-level codes.

All four categories are produced by the same function so the episode report,
frame flags, issue intervals, masks and post-repair re-checks can never drift
apart.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Any

import numpy as np

from . import config
from .features import arm_steps, xcorr_lags


class Result:
    def __init__(self, n: int):
        self.n = n
        self.row_codes: list[set[str]] = [set() for _ in range(n)]
        self.row_notes: list[set[str]] = [set() for _ in range(n)]
        self.ep_codes: dict[str, str] = {}
        self.metrics: dict[str, Any] = {}
        self.not_evaluable: list[str] = []

    def row(self, mask: np.ndarray, code: str, note: str | None = None) -> int:
        idx = np.flatnonzero(mask)
        for i in idx:
            self.row_codes[i].add(code)
            if note:
                self.row_notes[i].add(f"{code}@{note}")
        return int(len(idx))

    def ep(self, code: str, detail: str) -> None:
        if code in self.ep_codes and detail not in self.ep_codes[code]:
            self.ep_codes[code] += "；" + detail
        else:
            self.ep_codes.setdefault(code, detail)

    # --- views ---
    def all_codes(self) -> list[str]:
        codes = set(self.ep_codes)
        for s in self.row_codes:
            codes |= s
        return sorted(codes, key=lambda c: (config.CATEGORIES.index(config.category(c)), c))

    def blocked_rows(self) -> np.ndarray:
        """True where the frame must not be used for training."""
        blocked = np.zeros(self.n, bool)
        if any(config.CODES[c][2] == "ep" and config.blocks_training(c) for c in self.ep_codes):
            blocked[:] = True
            return blocked
        for i, s in enumerate(self.row_codes):
            if any(config.blocks_training(c) for c in s):
                blocked[i] = True
        return blocked


def _is_int(x: np.ndarray) -> np.ndarray:
    return np.isfinite(x) & (np.abs(x - np.round(x)) < 1e-9)


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    runs = []
    start = None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(mask) - 1))
    return runs


def _full_step(M: np.ndarray) -> np.ndarray:
    st = np.full(len(M), np.nan)
    if len(M) > 1:
        st[1:] = np.linalg.norm(np.diff(M, axis=0), axis=1)
    return st


def screen_mask(sf: dict[str, Any], thr: dict[str, Any]) -> np.ndarray:
    return sf["decode_ok"] & (
        (sf["dark"] >= thr["screen_dark_fraction"]) | (sf["bright"] >= thr["screen_bright_fraction"]) | (sf["std"] <= thr["screen_std_luma"])
    )


def dup_mask(sf: dict[str, Any], screen: np.ndarray) -> np.ndarray:
    """Byte-identical to the previous row (non-solid)."""
    n = len(sf["decode_ok"])
    dec, sha = sf["decode_ok"], sf["sha1"]
    dup = np.zeros(n, bool)
    for i in range(1, n):
        if dec[i] and dec[i - 1] and sha[i] and sha[i] == sha[i - 1] and not screen[i]:
            dup[i] = True
    return dup


def frozen_runs(sf: dict[str, Any], mv: np.ndarray, thr: dict[str, Any]) -> list[tuple[int, int, int]]:
    """Static-picture runs while the arm moves: (first_row, last_row, frames).

    A run of r consecutive low-motion differences spans r + 1 frames (the
    first static frame plus r repeats); detection and calibration both use
    this frame count, so thresholds and injected durations share one unit.
    """
    screen = screen_mask(sf, thr)
    frozen = sf["decode_ok"] & ~screen & ~dup_mask(sf, screen) & (sf["motion"] < thr["freeze_motion"])
    moving = np.isfinite(mv) & (mv > thr["arm_moving_step"])
    out = []
    for a, b in _runs(frozen):
        if moving[a : b + 1].mean() >= 0.5:
            out.append((a, b, b - a + 2))
    return out


def sf_motion(streams: dict[str, Any], col: str) -> np.ndarray:
    return streams[col]["motion"]


_ARROW_NAME = {"float32": "float", "float64": "double", "float16": "halffloat", "int64": "int64", "int32": "int32",
               "int16": "int16", "int8": "int8", "uint8": "uint8", "bool": "bool", "string": "string"}


def dtype_mismatches(col_types: dict[str, str], info: dict[str, Any]) -> list[str]:
    """Columns whose Arrow storage type differs from the dtype/shape declared in info.json."""
    import re

    out = []
    spec_all = info.get("features", {}) if isinstance(info.get("features"), dict) else {}
    for name, spec in spec_all.items():
        if not isinstance(spec, dict) or name not in col_types:
            continue
        dt = str(spec.get("dtype", ""))
        want = _ARROW_NAME.get(dt)
        if want is None:
            continue
        shape = spec.get("shape") or [1]
        actual = col_types[name]
        multi = int(np.prod([int(x) for x in shape])) > 1
        if multi:
            m = re.match(r"^(?:large_)?(?:fixed_size_)?list<(?:element|item): (\w+)>", actual)
            base = m.group(1) if m else actual
        else:
            base = actual
        if base != want:
            out.append(f"{name}: 存储 {actual}，声明 {dt}{list(shape) if multi else ''}")
    return out


def analyse(feats: dict[str, Any], ep_id: int, ds, thr: dict[str, Any], opts: dict[str, Any]) -> Result:
    layout = opts.get("layout", config.DEFAULT_LAYOUT)
    n = int(feats.get("n", 0))
    R = Result(n)
    R.metrics["rows"] = n
    if ds.video_columns:
        R.ep("C_MODALITY_UNSUPPORTED", "未解码的视频列: " + ",".join(ds.video_columns))
        R.not_evaluable.append("视频模态：未实现解码与逐帧验收")

    # ---------------- file / schema gate ----------------
    if not feats.get("read_ok"):
        R.ep("C_FILE_UNREADABLE", feats.get("read_error", ""))
        return R
    if n == 0:
        R.ep("C_EMPTY_EPISODE", "0 行")
        return R
    required = ["timestamp", "frame_index", "episode_index", "index", "task_index"]
    missing = [k for k in required if not feats.get(f"has_{k}")]
    if not feats.get("has_state"):
        missing.append("state")
    if not feats.get("has_action"):
        missing.append("actions")
    missing += list(feats.get("missing_stream_columns", []))
    if missing:
        R.ep("C_SCHEMA_MISSING_FIELD", "缺失字段: " + ",".join(missing))
    dmis = dtype_mismatches(feats.get("column_types", {}), ds.info or {})
    if dmis:
        R.ep("C_SCHEMA_DTYPE_MISMATCH", "；".join(dmis))
        R.metrics["dtype_mismatch"] = dmis
    for key in ("timestamp", "frame_index", "episode_index", "index", "task_index"):
        bad = feats.get(f"malformed_{key}")
        if bad is not None and bad.any():
            R.row(bad, "C_FIELD_INVALID", key)
    for key, name in (("state_unparseable", "state"), ("action_unparseable", "actions")):
        bad = feats.get(key)
        if bad is not None and bad.any():
            R.row(bad, "J_VALUE_UNPARSEABLE", name)

    fps = float(ds.fps)
    dt_nom = 1.0 / fps
    meta = ds.episodes_meta.get(ep_id)

    # ---------------- 时序 ----------------
    ts, fi, gi = feats["timestamp"], feats["frame_index"], feats["index"]
    zero = np.zeros(n, bool)
    if feats.get("has_timestamp"):
        R.row(~np.isfinite(ts) & ~feats.get("malformed_timestamp", zero), "T_TIMESTAMP_INVALID")
    fi_ok = _is_int(fi) & (fi >= 0)
    if feats.get("has_frame_index"):
        R.row(~fi_ok & ~feats.get("malformed_frame_index", zero), "T_FRAME_INDEX_INVALID")
    gi_ok = _is_int(gi)
    if feats.get("has_index"):
        R.row(~gi_ok & ~feats.get("malformed_index", zero), "T_INDEX_INVALID")

    unit_mismatch = False
    if feats.get("has_timestamp") and n > 1:
        dt = np.full(n, np.nan)
        both = np.isfinite(ts[1:]) & np.isfinite(ts[:-1])
        dt[1:][both] = ts[1:][both] - ts[:-1][both]
        valid_dt = dt[np.isfinite(dt)]
        fd_all = np.diff(fi)
        frames_consecutive = bool(fi_ok.all() and np.all(fd_all == 1))
        if valid_dt.size:
            ratio = float(np.median(valid_dt)) / dt_nom
            for scale in (10.0, 100.0, 1000.0, 0.1, 0.01, 0.001):
                if abs(ratio - scale) / scale < 0.05 and frames_consecutive and np.all(valid_dt > 0):
                    unit_mismatch = True
                    R.metrics["timestamp_unit_scale"] = scale
                    R.ep("T_TIMESTAMP_UNIT_MISMATCH", f"采样间隔中位数为标称值的 {ratio:.3g} 倍")
                    break
        R.row(np.isfinite(dt) & (dt <= 0), "T_TIMESTAMP_NON_MONOTONIC")
        if not unit_mismatch:
            gap = np.isfinite(dt) & (dt > 1.5 * dt_nom)
            R.row(gap, "T_TIMESTAMP_GAP")
            jit = np.isfinite(dt) & (dt > 0) & ~gap & (np.abs(dt - dt_nom) > thr["timestamp_jitter_frac"] * dt_nom)
            R.row(jit, "T_TIMESTAMP_JITTER")
            both_ok = np.isfinite(ts) & fi_ok
            if both_ok.sum() >= 3:
                offset = float(np.median(ts[both_ok] - fi[both_ok] / fps))
                R.metrics["timestamp_offset_s"] = round(offset, 6)
                if abs(offset) > thr["timestamp_offset_frames"] * dt_nom:
                    R.ep("T_TIMESTAMP_OFFSET", f"timestamp 相对 frame_index/fps 整体偏移 {offset:.3f} s")
    if feats.get("has_frame_index") and n > 1:
        fd = np.full(n, np.nan)
        okp = fi_ok[1:] & fi_ok[:-1]
        fd[1:][okp] = fi[1:][okp] - fi[:-1][okp]
        R.row(np.isfinite(fd) & (fd > 1), "T_FRAME_INDEX_GAP")
        R.row(np.isfinite(fd) & (fd <= 0), "T_FRAME_ORDER_ERROR")
        R.metrics["missing_frames_in_gaps"] = int(np.nansum(np.where(fd > 1, fd - 1, 0)))
    if fi_ok.any() and fi[fi_ok][0] > 0 and fi_ok[0]:
        R.ep("T_HEAD_FRAMES_MISSING", f"frame_index 从 {int(fi[0])} 开始")
    if feats.get("has_index") and feats.get("has_frame_index"):
        both = fi_ok & gi_ok
        if both.sum() >= 2:
            off = gi[both] - fi[both]
            top = Counter(off.tolist()).most_common(2)
            c, cnt = top[0]
            support = cnt / float(both.sum())
            unique = len(top) == 1 or top[1][1] < cnt
            R.metrics["index_offset_support"] = round(support, 4)
            if unique and support >= float(opts.get("index_offset_detect_support", 0.6)):
                R.metrics["index_offset"] = c
                R.metrics["index_offset_unique"] = True
                bad = both & (np.abs((gi - fi) - c) > 0)
                R.row(bad, "T_INDEX_DISCONTINUITY")
            elif len(top) > 1:
                # no dominant offset: which side is right cannot be decided -> every row is suspect
                R.metrics["index_offset"] = None
                R.metrics["index_offset_unique"] = False
                R.row(both, "T_INDEX_DISCONTINUITY", f"偏移不唯一(众数支持度{support:.0%})")
            else:
                R.metrics["index_offset"] = c
    if meta is not None and meta.get("length") is not None:
        try:
            mlen = int(meta["length"])
            if mlen != n:
                R.ep("T_METADATA_LENGTH_MISMATCH", f"元数据 {mlen} 行，实际 {n} 行")
        except (TypeError, ValueError):
            pass
    elif meta is None:
        R.not_evaluable.append("元数据一致性(meta 缺失)")

    # ---------------- 同步: episode / task ----------------
    ev = feats["episode_index"]
    if feats.get("has_episode_index"):
        ev_ok = _is_int(ev)
        R.row(~ev_ok, "S_EPISODE_INDEX_INVALID")
        expected_ep = ep_id if ep_id < 100000 else None
        if meta is not None and "episode_index" in meta:
            expected_ep = int(meta["episode_index"])
        if expected_ep is not None:
            R.row(ev_ok & (ev != expected_ep), "S_EPISODE_INDEX_MISMATCH")
    tv = feats["task_index"]
    if feats.get("has_task_index"):
        tv_ok = _is_int(tv)
        valid_tasks = set(ds.tasks) if ds.tasks else None
        if valid_tasks is not None:
            tv_ok = tv_ok & np.isin(tv, list(valid_tasks))
        R.row(~tv_ok, "S_TASK_INDEX_INVALID")
        vals = tv[tv_ok]
        uniq = sorted(set(vals.astype(int).tolist()))
        if len(uniq) > 1:
            R.ep("S_TASK_SWITCH_WITHIN_EPISODE", "task_index 取值 " + ",".join(map(str, uniq)))
        from .dataset import expected_task_index

        exp_task = expected_task_index(ds, ep_id)
        R.metrics["expected_task_index"] = exp_task
        if exp_task is not None and len(vals):
            mode = int(Counter(vals.astype(int).tolist()).most_common(1)[0][0])
            R.metrics["task_index"] = mode
            if mode != exp_task:
                R.ep("S_TASK_META_MISMATCH", f"数据为 task {mode}，episodes.jsonl 对应 task {exp_task}")
        elif len(vals):
            R.metrics["task_index"] = int(Counter(vals.astype(int).tolist()).most_common(1)[0][0])

    # ---------------- 数值 (needed by sync/value too) ----------------
    S, A = feats["state"], feats["action"]
    dim = layout.get("dim", 20)
    layout_ok = S.shape[1] == dim and A.shape[1] == dim
    s_fin = feats["state_dim_ok"] & np.isfinite(S).all(axis=1)
    a_fin = feats["action_dim_ok"] & np.isfinite(A).all(axis=1)
    steps = arm_steps(np.where(s_fin[:, None], S, np.nan), layout) if layout_ok else {}

    # ---------------- 同步 + 内容: cameras ----------------
    blur_thr = thr.get("blur", {})
    R.metrics["streams"] = {}
    cam_arm = layout.get("camera_arm", {})
    max_lag = int(opts.get("max_lag_search", 12))
    frames_consecutive = bool(n > 0 and fi_ok.all() and np.all(np.diff(fi) == 1))
    both_arms = np.fmax.reduce(np.vstack(list(steps.values())), axis=0) if steps else np.full(n, np.nan)
    shifted: dict[str, int] = {}
    for col, sf in feats.get("streams", {}).items():
        exp_shape = ds.image_shapes.get(col, ds.expected_image_shape)  # each camera has its own schema
        sm: dict[str, Any] = {}
        P = sf["present"]
        dec = sf["decode_ok"]
        pf = sf["path_frame"]
        if not P.any():
            R.ep("S_STREAM_MISSING", f"{col} 整条无图像")
            R.metrics["streams"][col] = {"coverage": "missing"}
            continue
        # path / coverage analysis (索引/覆盖证据)
        shift_k = None
        has_pf = P & np.isfinite(pf) & fi_ok
        deltas = (pf - fi)[has_pf]
        miss = ~P
        if has_pf.sum() and len(set(deltas.tolist())) == 1 and deltas[0] != 0:
            k = int(deltas[0])
            miss_idx = np.flatnonzero(miss)
            if k > 0 and miss_idx.tolist() == list(range(n - k, n)):
                shift_k = k
            elif k < 0 and miss_idx.tolist() == list(range(0, -k)):
                shift_k = k
        if shift_k is not None:
            R.row(np.ones(n, bool), "S_STREAM_SHIFTED", col)
            R.ep("S_STREAM_SHIFTED", f"{col} 图像 path 帧号整体偏移 {shift_k:+d} 帧，对端缺 {abs(shift_k)} 帧")
            sm["shift_frames"] = shift_k
            sm["shift_path_complete"] = bool(has_pf.sum() == P.sum())
            sm["shift_frames_consecutive"] = frames_consecutive
            shifted[col] = shift_k
        else:
            R.row(has_pf & ((pf - fi) != 0), "S_IMAGE_PATH_MISMATCH", col)
            for a, b in _runs(miss):
                if a == 0 and b == n - 1:
                    continue
                code = "S_SENSOR_LATE_START" if a == 0 else "S_SENSOR_EARLY_STOP" if b == n - 1 else "S_SENSOR_DROPOUT"
                m = np.zeros(n, bool)
                m[a : b + 1] = True
                R.row(m, code, col)
                sm.setdefault("gaps", []).append(f"{code}:{a}-{b}")
        # decode / shape / screen / duplicate / blur
        R.row(P & ~dec, "C_IMAGE_DECODE", col)
        if exp_shape:
            H, W, C = exp_shape
            shape_bad = dec & ((sf["h"] != H) | (sf["w"] != W) | (sf["ch"] != C))
        else:
            shape_bad = np.zeros(n, bool)
        R.row(shape_bad, "C_IMAGE_SHAPE", col)
        screen = screen_mask(sf, thr)
        R.row(screen, "C_IMAGE_SCREEN", col)
        dup = dup_mask(sf, screen)
        R.row(dup, "C_IMAGE_DUPLICATE", col)
        bt = blur_thr.get(col)
        if bt:
            lap, grad, byt = sf["lap"], sf["grad"], sf["bytes"]
            blur = dec & ~screen & ~shape_bad & (
                ((lap < bt["lap_min"]) & (grad < bt["grad_min"])) | (lap < 0.25 * bt["lap_min"]) | (byt < 0.2 * bt["byte_min"])
            )
            R.row(blur, "C_IMAGE_BLUR", col)
        else:
            R.not_evaluable.append(f"模糊检测({col} 无参考标定)")
        # frozen stream while its arm moves (solid-colour / byte-identical frames excluded)
        arm = cam_arm.get(col, None)
        if layout_ok and steps:
            mv = steps[arm] if arm in steps else both_arms
            fz = np.zeros(n, bool)
            for a, b, frames in frozen_runs(sf, mv, thr):
                if frames >= thr["freeze_min_run"]:
                    fz[a : b + 1] = True
            if fz.any():
                R.row(fz, "S_STREAM_FROZEN", col)
            # statistical visual–kinematic lag (统计时滞证据; not a hardware clock measurement)
            ref_lag = thr.get("vk_ref_lag", {}).get(col)
            if ref_lag is not None and n >= int(opts.get("vk_min_frames", 60)):
                lags = xcorr_lags(sf["motion"], mv, max_lag)
                if lags:
                    best = max(lags, key=lags.get)
                    r_b, r_0 = lags[best], lags.get(int(ref_lag), float("nan"))
                    sm.update(vk_best_lag=best, vk_r=round(r_b, 3), vk_r_at_ref=round(r_0, 3) if math.isfinite(r_0) else None,
                              vk_gain=round(r_b - r_0, 3) if math.isfinite(r_0) else None, vk_implied_shift=int(best - ref_lag),
                              vk_is_wrist=arm in steps)
            elif ref_lag is not None:
                R.not_evaluable.append(f"视觉—本体时滞({col}，帧数 {n} < {opts.get('vk_min_frames', 60)})")
        R.metrics["streams"][col] = sm

    # --- independent content evidence for path-shifted streams (gates automatic re-alignment) ---
    streams_f = feats.get("streams", {})
    for col, k in shifted.items():
        sm = R.metrics["streams"][col]
        ev = []
        if "vk_best_lag" in sm:
            wrist = bool(sm.get("vk_is_wrist"))
            gain = sm.get("vk_gain")
            min_r = thr["shift_wrist_min_r"] if wrist else thr["shift_scene_min_r"]
            min_g = thr["shift_wrist_min_gain"] if wrist else thr["shift_scene_min_gain"]
            ev.append({"method": "本臂运动时滞" if wrist else "双臂运动时滞(场景相机)", "tier": "中" if wrist else "弱",
                       "implied": sm["vk_implied_shift"], "r": sm["vk_r"], "gain": gain,
                       "qualifies": bool(abs(sm["vk_implied_shift"] - k) <= 1 and sm["vk_r"] >= min_r and gain is not None and gain >= min_g),
                       "agrees": abs(sm["vk_implied_shift"] - k) <= 1})
        for other, of in streams_f.items():
            if other == col or other in shifted or not of["present"].any() or n < int(opts.get("vk_min_frames", 60)):
                continue
            l0 = int(thr.get("xcam_ref_lag", {}).get(f"{col}|{other}", 0))
            lags = xcorr_lags(sf_motion(streams_f, col), sf_motion(streams_f, other), max_lag)
            if not lags:
                continue
            best = max(lags, key=lags.get)
            r_b, r_0 = lags[best], lags.get(l0, float("nan"))
            implied = best - l0  # corr(x[i], y[i+lag]) with x = this (shifted by k) stream
            gain = r_b - r_0 if math.isfinite(r_0) else None
            ev.append({"method": f"跨相机运动相关({other})", "tier": "弱", "implied": int(implied), "r": round(r_b, 3),
                       "gain": round(gain, 3) if gain is not None else None,
                       "qualifies": bool(abs(implied - k) <= 1 and r_b >= thr["shift_scene_min_r"] and gain is not None and gain >= thr["shift_scene_min_gain"]),
                       "agrees": abs(implied - k) <= 1})
        sm["shift_evidence"] = ev

    # --- visual–kinematic lag rule on wrist streams not explained by a path shift ---
    wrist = {c: m for c, m in R.metrics["streams"].items() if m.get("vk_is_wrist") and "vk_best_lag" in m}
    free = {}
    for c, m in wrist.items():
        if c in shifted and abs(m["vk_implied_shift"] - shifted[c]) <= 1:
            continue  # explained by the path shift (reported as S_STREAM_SHIFTED)
        free[c] = m

    def _qual(m: dict[str, Any], tol: float, min_r: float, min_g: float) -> bool:
        g = m.get("vk_gain")
        return abs(m["vk_implied_shift"]) >= tol and m["vk_r"] >= min_r and (g is None or g >= min_g)

    strong = {c: m for c, m in free.items() if _qual(m, thr["vk_lag_tol"], thr["vk_min_r"], thr["vk_min_gain"])}
    devs = [m["vk_implied_shift"] for m in strong.values()]
    all_wrists = [c for c, a in cam_arm.items() if a in steps and c in streams_f]
    if len(all_wrists) >= 2 and len(strong) == len(all_wrists) and max(devs) - min(devs) <= 1 and len(free) == len(all_wrists):
        R.ep("S_VISUAL_KINEMATIC_LAG", "两路腕部相机一致偏离参考时滞：" + "，".join(
            f"{c} 最佳 {m['vk_best_lag']:+d}（参考 {m['vk_best_lag'] - m['vk_implied_shift']:+d}，r={m['vk_r']:.2f}）" for c, m in strong.items()))
        R.metrics["vk_common_offset"] = int(round(float(np.median(devs))))
    else:
        for c, m in free.items():
            if _qual(m, thr["cam_lag_tol"], thr["cam_lag_min_r"], thr["cam_lag_min_gain"]):
                R.ep("S_CAMERA_LAG_SUSPECT", f"{c} 与本臂运动最佳时滞 {m['vk_best_lag']:+d}（参考 {m['vk_best_lag'] - m['vk_implied_shift']:+d}，"
                                             f"r={m['vk_r']:.2f}，增益 {m.get('vk_gain')}）")

    # ---------------- 内容: joint values ----------------
    for name, M, dim_ok, fin, code_nf in (
        ("state", S, feats["state_dim_ok"], s_fin, "J_STATE_NONFINITE"),
        ("actions", A, feats["action_dim_ok"], a_fin, "J_ACTION_NONFINITE"),
    ):
        if not feats.get("has_state" if name == "state" else "has_action"):
            continue
        R.row(~dim_ok, "J_DIM_MISMATCH", name)
        R.row(dim_ok & ~fin, code_nf)
        if not layout_ok:
            continue
        for arm, spec in layout["arms"].items():
            r = M[:, spec["rot6d"]]
            n1 = np.linalg.norm(r[:, :3], axis=1)
            n2 = np.linalg.norm(r[:, 3:], axis=1)
            dot = np.abs(np.sum(r[:, :3] * r[:, 3:], axis=1))
            dev = np.maximum(np.maximum(np.abs(n1 - 1), np.abs(n2 - 1)), dot)
            R.row(fin & (dev > thr["rot6d_tol"]), "J_ROT6D_INVALID", f"{name}.{arm}")
            g = M[:, spec["gripper"]]
            R.row(fin & ((g < thr["gripper_low"]) | (g > thr["gripper_high"])).any(axis=1), "J_GRIPPER_RANGE", f"{name}.{arm}")
            p = M[:, spec["pos"]]
            R.row(fin & (np.abs(p) > thr["position_envelope"]).any(axis=1), "J_POSITION_ENVELOPE", f"{name}.{arm}")
    s_step = _full_step(np.where(s_fin[:, None], S, np.nan))
    a_step = _full_step(np.where(a_fin[:, None], A, np.nan))
    R.row(np.isfinite(s_step) & (s_step > thr["state_step_max"]), "J_SPIKE_JUMP", "state")
    R.row(np.isfinite(a_step) & (a_step > thr["action_step_max"]), "J_SPIKE_JUMP", "actions")

    # state–action consistency at ONE episode-level lag (not a per-frame free choice)
    if layout_ok or S.shape[1] == A.shape[1]:
        best_lag, best_med, per = None, math.inf, {}
        for lag in range(-3, 4):
            j = np.arange(n) + lag
            ok = (j >= 0) & (j < n)
            ok[ok] &= a_fin[ok] & s_fin[j[ok]]
            if ok.sum() < max(10, n // 4):
                continue
            d = A[ok] - S[j[ok]]
            rm = np.sqrt(np.mean(d * d, axis=1))
            per[lag] = (ok, rm, np.max(np.abs(d), axis=1))
            med = float(np.mean(rm))  # mean, not median: the heavy tail decides the lag
            if med < best_med:
                best_lag, best_med = lag, med
        if best_lag is not None:
            ok, rm, mx = per[best_lag]
            bad = np.zeros(n, bool)
            bad[ok] = rm > thr["sa_residual_rmse"]
            if layout_ok:
                j = np.flatnonzero(ok) + best_lag
                d = np.abs(A[ok] - S[j])
                for grp, lim in thr.get("sa_group_max_abs", {}).items():
                    cols = [c for spec in layout["arms"].values() for c in spec.get(grp, [])]
                    if cols:
                        bad[np.flatnonzero(ok)[d[:, cols].max(axis=1) > lim]] = True
            else:
                bad[ok] |= mx > thr["sa_residual_max_abs"]
            R.row(bad, "J_STATE_ACTION_MISMATCH")
            # consistency: share of frames whose own best lag equals the episode lag
            stack = np.full((7, n), np.inf)
            for lag, (okl, rml, _) in per.items():
                stack[lag + 3, np.flatnonzero(okl)] = rml
            fin_cols = np.isfinite(stack).any(axis=0) & np.isfinite(stack[best_lag + 3])
            cons = float(np.mean(np.argmin(stack[:, fin_cols], axis=0) == best_lag + 3)) if fin_cols.any() else float("nan")
            R.metrics.update(sa_lag=best_lag, sa_lag_consistency=round(cons, 3), sa_rmse_mean=round(best_med, 4))

    # ---------------- 数据价值 (training-value proxies; never blocks frames) ----------------
    fin_s = np.isfinite(s_step)
    fin_a = np.isfinite(a_step)
    both = fin_s & fin_a
    if both.sum() >= 5:
        sp, ap = float(np.nansum(s_step)), float(np.nansum(a_step))
        R.metrics.update(state_path=round(sp, 4), action_path=round(ap, 4))
        if sp < thr["low_motion_state_path"] and ap >= thr["low_motion_action_path"]:
            R.ep("V_STATE_FREEZE", f"state 路径长 {sp:.3f}")
        elif ap < thr["low_motion_action_path"] and sp >= thr["low_motion_state_path"]:
            R.ep("V_ACTION_FREEZE", f"actions 路径长 {ap:.3f}")
        elif sp < thr["low_motion_state_path"] and ap < thr["low_motion_action_path"]:
            R.ep("V_LOW_INFORMATION", f"state/actions 路径长 {sp:.3f}/{ap:.3f}")
        idle = (s_step[both] <= thr["idle_state_cut"]) & (a_step[both] <= thr["idle_action_cut"])
        inter = (s_step[both] >= thr["interaction_state_cut"]) | (a_step[both] >= thr["interaction_action_cut"])
        R.metrics.update(idle_fraction=round(float(idle.mean()), 4), interaction_rate=round(float(inter.mean()), 4))
        if idle.mean() > thr["idle_rate_high"]:
            R.ep("V_HIGH_IDLE", f"空闲帧占比 {idle.mean():.1%}")
        if inter.mean() < thr["interaction_rate_low"]:
            R.ep("V_LOW_INTERACTION", f"交互运动帧占比 {inter.mean():.1%}")
    else:
        R.not_evaluable.append("运动价值代理(state/actions 有效帧不足)")
    # visual proxy per declared camera; a missing/undecodable camera is 不可评估, never a full score
    comps = {"运动(state/actions)": bool(both.sum() >= 5)}
    divs = []
    for col in ds.image_columns:
        sf = feats.get("streams", {}).get(col)
        h = [x for x, d in zip(sf["ahash"], sf["decode_ok"]) if d and x] if sf is not None else []
        comps[f"视觉({col})"] = len(h) >= 5
        if len(h) >= 5:
            divs.append(len(set(h)) / len(h))
        else:
            R.not_evaluable.append(f"视觉多样性({col} 无足够可解码图像)")
    div = float(np.mean(divs)) if divs else float("nan")
    R.metrics["visual_diversity"] = round(div, 4) if math.isfinite(div) else None
    if math.isfinite(div) and div < thr["visual_diversity_low"]:
        R.ep("V_LOW_VISUAL_DIVERSITY", f"画面多样性 {div:.3f}（{len(divs)}/{len(ds.image_columns)} 路相机）")
    for col in ds.video_columns:
        comps[f"video:{col}"] = False
    R.metrics["value_components"] = comps
    R.metrics["value_coverage"] = f"{sum(comps.values())}/{len(comps)}"
    R.metrics["value_evaluable"] = bool(all(comps.values()))
    R.metrics["value_partially_evaluable"] = bool(any(comps.values()))
    return R
