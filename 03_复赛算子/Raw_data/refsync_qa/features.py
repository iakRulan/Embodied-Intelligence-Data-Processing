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


def _num(v: Any) -> tuple[float, bool]:
    """(value, well_formed). Accepts real numbers and length-1 lists (declared shape [1])."""
    if v is None:
        return math.nan, True
    if isinstance(v, (list, tuple, np.ndarray)):
        if len(v) != 1:
            return math.nan, False
        v = v[0]
        if v is None:
            return math.nan, True
    if isinstance(v, (bool, np.bool_)) or isinstance(v, (str, bytes, dict)):
        return math.nan, False
    try:
        return float(v), True
    except (TypeError, ValueError):
        return math.nan, False


def _scalar(table, name: str | None, n: int) -> tuple[np.ndarray, bool, np.ndarray]:
    """Column as float64 (NaN for null/invalid). Returns (values, present, malformed_rows).

    A malformed value (list of length != 1, string, dict, bool) is never
    silently reduced to its first element: it becomes NaN and is reported.
    """
    bad = np.zeros(n, bool)
    if name is None or name not in table.column_names:
        return np.full(n, np.nan), False, bad
    import pyarrow as pa

    col = table.column(name)
    if pa.types.is_integer(col.type) or pa.types.is_floating(col.type):
        import pyarrow.compute as pc

        out = pc.cast(col, pa.float64()).to_numpy(zero_copy_only=False).astype(np.float64)
        return out, True, bad
    out = np.full(n, np.nan)
    for i, v in enumerate(col.to_pylist()):
        out[i], ok = _num(v)
        bad[i] = not ok
    return out, True, bad


def _matrix(table, name: str | None, n: int, dim: int | None) -> tuple[np.ndarray, np.ndarray, bool, np.ndarray]:
    """List column as n x dim float64 (NaN padded). Returns (M, dim_ok, present, unparseable_rows).

    Fast path for numeric list columns; otherwise each element is parsed on its
    own so one bad string marks its row instead of aborting the batch.
    """
    unp = np.zeros(n, bool)
    if name is None or name not in table.column_names:
        d = dim or 1
        return np.full((n, d), np.nan), np.zeros(n, bool), False, unp
    import pyarrow as pa
    import pyarrow.compute as pc

    col = table.column(name).combine_chunks() if n else table.column(name)
    typ = col.type
    listy = pa.types.is_list(typ) or pa.types.is_large_list(typ) or pa.types.is_fixed_size_list(typ)
    if listy and n and (pa.types.is_floating(typ.value_type) or pa.types.is_integer(typ.value_type)):
        valid = col.is_valid().to_numpy(zero_copy_only=False)
        lens = pc.list_value_length(col).to_numpy(zero_copy_only=False)
        lens = np.where(valid, np.nan_to_num(lens.astype(float), nan=-1), -1).astype(int)
        d = int(dim) if dim else (int(np.bincount(lens[lens >= 0]).argmax()) if (lens >= 0).any() else 1)
        d = max(d, 1)
        vals = pc.cast(col.flatten(), pa.float64()).to_numpy(zero_copy_only=False).astype(np.float64)
        M = np.full((n, d), np.nan)
        ok = lens == d
        if ok.any():
            starts = np.concatenate([[0], np.cumsum(np.maximum(lens, 0))[:-1]])
            idx = starts[ok][:, None] + np.arange(d)[None, :]
            M[ok] = vals[idx]
        return M, ok, True, unp
    rows = col.to_pylist()
    lens = [len(r) if isinstance(r, (list, tuple)) else -1 for r in rows]
    good = [x for x in lens if x >= 0]
    d = dim or (max(set(good), key=good.count) if good else 1)
    d = max(int(d), 1)
    M = np.full((n, d), np.nan)
    ok = np.zeros(n, bool)
    for i, r in enumerate(rows):
        if isinstance(r, (list, tuple)) and len(r) == d:
            ok[i] = True
            for j, x in enumerate(r):
                v, fine = _num(x)
                if isinstance(x, (list, tuple)):
                    fine = False
                if not fine:
                    unp[i] = True
                    v = math.nan
                M[i, j] = v
    return M, ok, True, unp


def column_types(table) -> dict[str, str]:
    """Arrow storage type per column (for schema-contract checks)."""
    out = {}
    for f in table.schema:
        out[f.name] = str(f.type)
    return out


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
    feats["column_types"] = column_types(table)
    for key in ("timestamp", "frame_index", "episode_index", "index", "task_index"):
        vals, present, bad = _scalar(table, names[key], n)
        feats[key] = vals
        feats[f"has_{key}"] = present
        feats[f"malformed_{key}"] = bad
    S, s_ok, s_present, s_unp = _matrix(table, names["state"], n, ctx.get("state_dim"))
    A, a_ok, a_present, a_unp = _matrix(table, names["actions"], n, ctx.get("action_dim"))
    feats.update(state=S, state_dim_ok=s_ok, has_state=s_present, state_unparseable=s_unp,
                 action=A, action_dim_ok=a_ok, has_action=a_present, action_unparseable=a_unp)

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


def xcorr_lags(a: np.ndarray, b: np.ndarray, max_lag: int, min_points: int = 30) -> dict[int, float]:
    """Pearson corr(a[i], b[i+lag]) for lag in [-L, L], L clipped to the usable length.

    Short or unequal inputs return fewer (possibly zero) lags instead of raising.
    """
    res: dict[int, float] = {}
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    L = min(int(max_lag), n - min_points)
    if L < 0:
        return res
    for lag in range(-L, L + 1):
        if lag >= 0:
            x, y = a[: n - lag], b[lag:]
        else:
            x, y = a[-lag:], b[: n + lag]
        m = np.isfinite(x) & np.isfinite(y)
        if m.sum() < min_points:
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
