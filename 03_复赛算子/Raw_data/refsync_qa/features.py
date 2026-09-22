# -*- coding: utf-8 -*-
"""Single-pass per-episode feature extraction.

The parquet file is read once; scalar fields never crash on NaN/None, every
image payload is decoded once, and all later stages (detection, repair
validation, calibration) consume the same feature dictionary.
"""
from __future__ import annotations

import hashlib
import io
import math
import re
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from . import config

_PATH_RE = re.compile(r"frame_(\d+)\.")


def _scalar(table, name: str | None, n: int) -> tuple[np.ndarray, bool]:
    """Column as float64 with NaN for null/unparseable. Returns (values, present)."""
    if name is None or name not in table.column_names:
        return np.full(n, np.nan), False
    out = np.full(n, np.nan)
    for i, v in enumerate(table.column(name).to_pylist()):
        try:
            if v is None:
                continue
            if isinstance(v, (list, tuple, np.ndarray)):
                v = v[0] if len(v) else None
                if v is None:
                    continue
            out[i] = float(v)
        except (TypeError, ValueError):
            pass
    return out, True


def _matrix(table, name: str | None, n: int, dim: int | None) -> tuple[np.ndarray, np.ndarray, bool]:
    """List column as n x dim float64 (NaN padded). Returns (M, dim_ok, present)."""
    if name is None or name not in table.column_names:
        d = dim or 1
        return np.full((n, d), np.nan), np.zeros(n, bool), False
    rows = table.column(name).to_pylist()
    lens = [len(r) if isinstance(r, (list, tuple)) else -1 for r in rows]
    d = dim or (max(set(lens), key=lens.count) if lens else 1)
    d = max(int(d), 1)
    M = np.full((n, d), np.nan)
    ok = np.zeros(n, bool)
    for i, r in enumerate(rows):
        if isinstance(r, (list, tuple)) and len(r) == d:
            arr = np.array([np.nan if x is None else x for x in r], dtype=np.float64)
            M[i] = arr
            ok[i] = True
    return M, ok, True


def _image_metrics(raw: bytes | None) -> dict[str, Any] | None:
    if raw is None or len(raw) == 0:
        return None
    out: dict[str, Any] = {"bytes": len(raw), "sha1": hashlib.sha1(raw).hexdigest(), "decode_ok": False}
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            w, h = im.size
            ch = len(im.getbands())
            rgb = np.asarray(im.convert("RGB"), dtype=np.float32)
    except Exception:  # noqa: BLE001 - corrupt payload is itself the finding
        return out
    gray = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    gx = np.diff(gray, axis=1)
    gy = np.diff(gray, axis=0)
    c = gray[1:-1, 1:-1]
    lap = 4.0 * c - gray[:-2, 1:-1] - gray[2:, 1:-1] - gray[1:-1, :-2] - gray[1:-1, 2:]
    g8 = gray.astype(np.uint8)
    small = np.asarray(Image.fromarray(g8).resize((8, 8), Image.Resampling.BILINEAR), dtype=np.float32)
    g64 = np.asarray(Image.fromarray(g8).resize((64, 64), Image.Resampling.BILINEAR), dtype=np.float32)
    out.update(
        decode_ok=True,
        w=w, h=h, ch=ch,
        mean=float(gray.mean()),
        std=float(gray.std()),
        dark=float(np.mean(gray <= 10.0)),
        bright=float(np.mean(gray >= 245.0)),
        grad=float((np.abs(gx).mean() + np.abs(gy).mean()) / 2.0),
        lap=float(lap.var()),
        ahash=np.packbits(small.ravel() >= float(small.mean())).tobytes().hex(),
        g64=g64,
    )
    return out


def _stream_features(values: list[Any]) -> dict[str, Any]:
    n = len(values)
    f = {
        "present": np.zeros(n, bool),
        "decode_ok": np.zeros(n, bool),
        "w": np.full(n, np.nan), "h": np.full(n, np.nan), "ch": np.full(n, np.nan),
        "mean": np.full(n, np.nan), "std": np.full(n, np.nan),
        "dark": np.full(n, np.nan), "bright": np.full(n, np.nan),
        "grad": np.full(n, np.nan), "lap": np.full(n, np.nan),
        "bytes": np.zeros(n),
        "motion": np.full(n, np.nan),  # mean |gray64_t - gray64_{t-1}| (previous row only)
        "path_frame": np.full(n, np.nan),
        "sha1": [""] * n,
        "ahash": [""] * n,
        "path": [""] * n,
    }
    prev = None
    for i, v in enumerate(values):
        raw = None
        path = ""
        if isinstance(v, dict):
            raw = v.get("bytes")
            path = v.get("path") or ""
        elif isinstance(v, (bytes, bytearray, memoryview)):
            raw = v
        if raw is not None and not isinstance(raw, bytes):
            raw = bytes(raw)
        f["path"][i] = str(path)
        m = _PATH_RE.search(str(path))
        if m:
            f["path_frame"][i] = int(m.group(1))
        mt = _image_metrics(raw)
        if mt is None:
            prev = None
            continue
        f["present"][i] = True
        f["bytes"][i] = mt["bytes"]
        f["sha1"][i] = mt["sha1"]
        if not mt["decode_ok"]:
            prev = None
            continue
        f["decode_ok"][i] = True
        for k in ("w", "h", "ch", "mean", "std", "dark", "bright", "grad", "lap"):
            f[k][i] = mt[k]
        f["ahash"][i] = mt["ahash"]
        if prev is not None:
            f["motion"][i] = float(np.abs(mt["g64"] - prev).mean())
        prev = mt["g64"]
    return f


def _resolve(names: list[str], columns: list[str]) -> str | None:
    for n in names:
        if n in columns:
            return n
    return None


def extract(path: str | Path, ctx: dict[str, Any]) -> dict[str, Any]:
    """Return a feature dict for one episode file. Never raises for data defects."""
    import pyarrow.parquet as pq

    path = Path(path)
    feats: dict[str, Any] = {"path": str(path), "read_ok": False, "read_error": "", "n": 0}
    try:
        table = pq.read_table(path)
    except Exception as exc:  # noqa: BLE001
        feats["read_error"] = f"{type(exc).__name__}: {exc}"[:300]
        return feats
    feats["read_ok"] = True
    n = table.num_rows
    feats["n"] = n
    cols = list(table.column_names)
    feats["columns"] = cols
    aliases = ctx.get("field_aliases", config.FIELD_ALIASES)
    names = {k: _resolve(v, cols) for k, v in aliases.items()}
    feats["field_names"] = names
    for key in ("timestamp", "frame_index", "episode_index", "index", "task_index"):
        vals, present = _scalar(table, names[key], n)
        feats[key] = vals
        feats[f"has_{key}"] = present
    S, s_ok, s_present = _matrix(table, names["state"], n, ctx.get("state_dim"))
    A, a_ok, a_present = _matrix(table, names["actions"], n, ctx.get("action_dim"))
    feats.update(state=S, state_dim_ok=s_ok, has_state=s_present, action=A, action_dim_ok=a_ok, has_action=a_present)

    streams = {}
    missing_streams = []
    for col in ctx.get("image_columns", config.DEFAULT_IMAGE_COLUMNS):
        if col not in cols:
            missing_streams.append(col)
            continue
        streams[col] = _stream_features(table.column(col).to_pylist())
    feats["streams"] = streams
    feats["missing_stream_columns"] = missing_streams
    return feats


def arm_steps(S: np.ndarray, layout: dict[str, Any]) -> dict[str, np.ndarray]:
    """Per-arm L2 step (pos + rot6d) between consecutive rows; NaN where undefined."""
    out = {}
    n = len(S)
    for arm, spec in layout["arms"].items():
        idx = [i for i in spec["pos"] + spec["rot6d"] if i < S.shape[1]]
        st = np.full(n, np.nan)
        if n > 1 and idx:
            d = np.diff(S[:, idx], axis=0)
            st[1:] = np.linalg.norm(d, axis=1)
        out[arm] = st
    return out


def xcorr_lags(a: np.ndarray, b: np.ndarray, max_lag: int) -> dict[int, float]:
    """Pearson corr(a[i], b[i+lag]) for lag in [-max_lag, max_lag]."""
    res: dict[int, float] = {}
    n = len(a)
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            x, y = a[: n - lag], b[lag:]
        else:
            x, y = a[-lag:], b[: n + lag]
        m = np.isfinite(x) & np.isfinite(y)
        if m.sum() < 30:
            continue
        xs, ys = x[m], y[m]
        if xs.std() < 1e-12 or ys.std() < 1e-12:
            continue
        res[lag] = float(np.corrcoef(xs, ys)[0, 1])
    return res


def safe_float(x: Any) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else math.nan
    except (TypeError, ValueError):
        return math.nan
