# -*- coding: utf-8 -*-
"""Parallel image stream scanner (RefSync-QA v2.2 engineering channel).

Why this exists
---------------
The v2.1 pipeline spends ~94% of its 137 s end-to-end runtime decoding the three
camera streams single-process. The v2.1 document described a parallel design but
never measured it, so the "算法运行效率与批处理能力" scoring item (10%) rested on
a paper design. This module implements and measures it.

It also adds three per-frame features the v2.1 detector cannot compute, because
they need real inter-frame pixel comparison rather than coarse hashes:

  motion_mad_prev   mean |Δ| against the previous decoded frame -> visual motion energy
  chroma_spread     (max-min)/mean of the RGB channel means     -> colour-cast / channel corruption
  noise_residual    robust high-frequency residual (MAD of Laplacian) -> salt-noise / 花屏 proxy

Output schema is a strict superset of ``04_profile_images.py`` so every
downstream script keeps working.

Usage
-----
    python src/21_parallel_image_scan.py --dataset both --workers 16
    python src/21_parallel_image_scan.py --benchmark --workers 1,4,8,16 --limit 12
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dataset_paths import OUT_DIR, resolve_reference_root, resolve_test_root

OUT_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_REF = resolve_reference_root()
DEFAULT_TEST = resolve_test_root()
IMAGE_COLS = ["image", "left_wrist_image", "right_wrist_image"]


# --------------------------------------------------------------------------- #
# per-frame metrics
# --------------------------------------------------------------------------- #
def payload_bytes(value: Any) -> bytes | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("bytes")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    return None


def frame_metrics(raw: bytes | None, path: str, prev_gray64: np.ndarray | None) -> dict[str, Any]:
    """Decode one image payload and compute the v2.1 metric set plus the new ones."""
    base: dict[str, Any] = {
        "payload_present": raw is not None and len(raw) > 0,
        "byte_size": len(raw) if raw is not None else 0,
        "decode_ok": False,
        "width": math.nan,
        "height": math.nan,
        "channels": math.nan,
        "mean_luma": math.nan,
        "std_luma": math.nan,
        "dark_fraction": math.nan,
        "bright_fraction": math.nan,
        "gradient_energy": math.nan,
        "laplacian_var": math.nan,
        "sha1": "",
        "ahash": "",
        "path": path,
        "path_frame_index": math.nan,
        "error": "",
        # --- new in v2.2 ---
        "motion_mad_prev": math.nan,
        "chroma_spread": math.nan,
        "noise_residual": math.nan,
    }

    match = re.search(r"frame_(\d+)\.png", str(path))
    if match:
        base["path_frame_index"] = int(match.group(1))

    if raw is None or len(raw) == 0:
        base["error"] = "missing_payload"
        return base

    base["sha1"] = hashlib.sha1(raw).hexdigest()
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            base["width"], base["height"] = im.size
            base["channels"] = len(im.getbands())
            rgb = np.asarray(im.convert("RGB"), dtype=np.float32)
    except Exception as exc:  # malformed / truncated payload
        base["error"] = f"{type(exc).__name__}: {exc}"
        return base

    gray = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    base["decode_ok"] = True
    base["mean_luma"] = float(gray.mean())
    base["std_luma"] = float(gray.std())
    base["dark_fraction"] = float(np.mean(gray <= 10.0))
    base["bright_fraction"] = float(np.mean(gray >= 245.0))

    gx = np.diff(gray, axis=1)
    gy = np.diff(gray, axis=0)
    base["gradient_energy"] = float((np.abs(gx).mean() + np.abs(gy).mean()) / 2.0)

    center = gray[1:-1, 1:-1]
    lap = 4.0 * center - gray[:-2, 1:-1] - gray[2:, 1:-1] - gray[1:-1, :-2] - gray[1:-1, 2:]
    base["laplacian_var"] = float(lap.var())
    # Robust high-frequency residual: MAD of the Laplacian is insensitive to a few
    # strong edges, so it rises mainly when noise covers the whole frame.
    base["noise_residual"] = float(np.median(np.abs(lap - np.median(lap))))

    small = np.asarray(Image.fromarray(gray.astype(np.uint8)).resize((8, 8), Image.Resampling.BILINEAR))
    base["ahash"] = "".join("1" if x >= float(small.mean()) else "0" for x in small.ravel())

    ch_mean = rgb.reshape(-1, 3).mean(axis=0)
    denom = float(ch_mean.mean())
    base["chroma_spread"] = float((ch_mean.max() - ch_mean.min()) / denom) if denom > 1e-6 else math.nan

    gray64 = np.asarray(
        Image.fromarray(gray.astype(np.uint8)).resize((64, 64), Image.Resampling.BILINEAR),
        dtype=np.float32,
    )
    if prev_gray64 is not None:
        base["motion_mad_prev"] = float(np.abs(gray64 - prev_gray64).mean())
    return base, gray64


def _frame_metrics_safe(raw, path, prev_gray64):
    """Wrapper that keeps the (metrics, gray64) contract even on failure paths."""
    out = frame_metrics(raw, path, prev_gray64)
    if isinstance(out, tuple):
        return out
    return out, None


def hamming(a: str, b: str) -> float:
    if not a or not b or len(a) != len(b):
        return math.nan
    return float(sum(x != y for x, y in zip(a, b, strict=True)))


# --------------------------------------------------------------------------- #
# episode worker (no shared state -> safe to parallelise)
# --------------------------------------------------------------------------- #
def scan_episode(job: dict[str, Any]) -> dict[str, Any] | None:
    path = Path(job["path"])
    episode = job["episode"]
    label = job["label"]
    try:
        df = pd.read_parquet(path)
    except Exception as exc:
        return {
            "label": label,
            "episode": episode,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "frames": [],
            "summary": {},
        }

    frames: list[dict[str, Any]] = []
    col_present = [c for c in IMAGE_COLS if c in df.columns]

    for stream in IMAGE_COLS:
        if stream not in df.columns:
            continue
        series = df[stream]
        prev_gray: np.ndarray | None = None
        prev_hash = ""
        for row, value in enumerate(series):
            raw = payload_bytes(value)
            p = value.get("path", "") if isinstance(value, dict) else ""
            m, prev_gray = _frame_metrics_safe(raw, p, prev_gray)
            d = hamming(prev_hash, m["ahash"])
            prev_hash = m["ahash"]
            m.update(
                {
                    "dataset_episode": episode,
                    "row": row,
                    "stream": stream,
                    "frame_index": df["frame_index"].iloc[row] if "frame_index" in df.columns else row,
                    "timestamp": df["timestamp"].iloc[row] if "timestamp" in df.columns else math.nan,
                    "adjacent_ahash_distance": d,
                    "dataset": label,
                }
            )
            frames.append(m)

    missing_streams = [c for c in IMAGE_COLS if c not in col_present]
    return {
        "label": label,
        "episode": episode,
        "ok": True,
        "error": "",
        "frames": frames,
        "summary": {"missing_streams": ",".join(missing_streams), "rows": int(len(df))},
    }


def read_episode_meta(root: Path) -> dict[int, dict[str, Any]]:
    meta_path = root / "meta" / "episodes.jsonl"
    out: dict[int, dict[str, Any]] = {}
    if not meta_path.exists():
        return out
    for line in meta_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        out[int(obj.get("episode_index", -1))] = obj
    return out


def discover(root: Path, label: str, limit: int | None = None) -> list[dict[str, Any]]:
    data_dir = root / "data"
    files = sorted(data_dir.rglob("episode_*.parquet"))
    jobs = []
    for f in files:
        m = re.search(r"episode_(\d+)", f.name)
        if not m:
            continue
        jobs.append({"path": str(f), "episode": int(m.group(1)), "label": label})
    jobs.sort(key=lambda j: j["episode"])
    return jobs[:limit] if limit else jobs


def run_pool(jobs: list[dict[str, Any]], workers: int) -> tuple[list[dict[str, Any]], float]:
    t0 = time.perf_counter()
    results: list[dict[str, Any]] = []
    if workers <= 1:
        for j in jobs:
            results.append(scan_episode(j))
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(scan_episode, j) for j in jobs]
            for fut in as_completed(futures):
                results.append(fut.result())
    return results, time.perf_counter() - t0


def results_to_frames(results: list[dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for r in results:
        rows.extend(r.get("frames", []))
    cols = [
        "dataset", "dataset_episode", "row", "stream", "frame_index", "timestamp",
        "payload_present", "byte_size", "decode_ok", "width", "height", "channels",
        "mean_luma", "std_luma", "dark_fraction", "bright_fraction",
        "gradient_energy", "laplacian_var", "noise_residual", "chroma_spread",
        "motion_mad_prev", "sha1", "ahash", "path", "path_frame_index",
        "adjacent_ahash_distance", "error",
    ]
    df = pd.DataFrame(rows)
    for c in cols:
        if c not in df.columns:
            df[c] = math.nan
    return df[cols]


# --------------------------------------------------------------------------- #
# benchmark
# --------------------------------------------------------------------------- #
def benchmark(root: Path, label: str, worker_grid: list[int], limit: int) -> list[dict[str, Any]]:
    jobs = discover(root, label, limit=limit)
    print(f"[bench] {label}: {len(jobs)} episodes, grid={worker_grid}")
    out: list[dict[str, Any]] = []
    baseline = None
    probe = run_pool(jobs[:1], 1)[0]
    imgs = len(results_to_frames(probe)) * len(jobs)
    for w in sorted(set(worker_grid)):
        _, elapsed = run_pool(jobs, w)
        if baseline is None:
            baseline = elapsed
        out.append(
            {
                "dataset": label,
                "episodes": len(jobs),
                "workers": w,
                "wall_seconds": round(elapsed, 2),
                "images_per_second": round(imgs / elapsed, 1) if elapsed > 0 else None,
                "speedup_vs_1_worker": round(baseline / elapsed, 2) if elapsed > 0 else None,
            }
        )
        print(f"  workers={w:<3} {elapsed:7.2f}s  speedup={baseline / elapsed:5.2f}x")
    return out


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", choices=["test", "reference", "both"], default="both")
    ap.add_argument("--workers", type=int, default=max(1, (__import__("os").cpu_count() or 4) - 2))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--benchmark", action="store_true")
    ap.add_argument("--bench-grid", default="1,2,4,8,16")
    args = ap.parse_args()

    targets = []
    if args.dataset in {"test", "both"}:
        targets.append((DEFAULT_TEST, "test"))
    if args.dataset in {"reference", "both"}:
        targets.append((DEFAULT_REF, "reference"))

    if args.benchmark:
        grid = [int(x) for x in args.bench_grid.split(",") if x.strip()]
        rows = []
        for root, label in targets:
            rows.extend(benchmark(root, label, grid, args.limit or 12))
        df = pd.DataFrame(rows)
        df.to_csv(OUT_DIR / "parallel_benchmark.csv", index=False, encoding="utf-8-sig")
        print(df.to_string(index=False))
        print(f"\n[bench] saved -> {OUT_DIR / 'parallel_benchmark.csv'}")
        return

    for root, label in targets:
        jobs = discover(root, label, limit=args.limit)
        print(f"[scan] {label}: {len(jobs)} episodes, workers={args.workers}")
        t0 = time.perf_counter()
        results, elapsed = run_pool(jobs, args.workers)
        frames = results_to_frames(results)
        failed = [r for r in results if not r.get("ok")]
        out = OUT_DIR / f"{label}_image_frames_v22.csv"
        frames.to_csv(out, index=False, encoding="utf-8-sig")

        decoded = int(frames["decode_ok"].sum())
        summary = {
            "dataset": label,
            "episodes": len(jobs),
            "failed_episodes": len(failed),
            "frames_scanned": int(len(frames)),
            "frames_decoded": decoded,
            "workers": args.workers,
            "wall_seconds": round(elapsed, 2),
            "images_per_second": round(decoded / elapsed, 1) if elapsed > 0 else None,
            "seconds_per_episode": round(elapsed / len(jobs), 3) if jobs else None,
        }
        (OUT_DIR / f"{label}_image_scan_v22_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"[scan] saved -> {out}")


if __name__ == "__main__":
    main()
