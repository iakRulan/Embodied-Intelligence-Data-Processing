"""Decode and profile the three image streams in every episode.

The output is intentionally model-free: it contains measurements that can be
calibrated on the clean reference set (blur, dark/bright frames, repeated
frames, malformed payloads and stream coverage).  It is therefore useful both
for the detector and for the result figures in the initial-round submission.
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
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = False

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dataset_paths import OUT_DIR, resolve_reference_root, resolve_test_root

DEFAULT_REF = resolve_reference_root()
DEFAULT_TEST = resolve_test_root()
IMAGE_COLS = ["image", "left_wrist_image", "right_wrist_image"]


def payload_bytes(value: Any) -> bytes | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("bytes")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    return None


def image_metrics(value: Any) -> dict[str, Any]:
    raw = payload_bytes(value)
    base = {
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
        "path": value.get("path", "") if isinstance(value, dict) else "",
        "path_frame_index": math.nan,
        "error": "",
    }
    if raw is None or len(raw) == 0:
        base["error"] = "missing_payload"
        match = re.search(r"frame_(\d+)\.png", str(base["path"]))
        if match:
            base["path_frame_index"] = int(match.group(1))
        return base
    base["sha1"] = hashlib.sha1(raw).hexdigest()
    match = re.search(r"frame_(\d+)\.png", str(base["path"]))
    if match:
        base["path_frame_index"] = int(match.group(1))
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            base["width"], base["height"] = im.size
            base["channels"] = len(im.getbands())
            rgb = np.asarray(im.convert("RGB"), dtype=np.float32)
    except Exception as exc:  # malformed/truncated image payload
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
    small = np.asarray(Image.fromarray(gray.astype(np.uint8)).resize((8, 8), Image.Resampling.BILINEAR))
    base["ahash"] = "".join("1" if x >= float(small.mean()) else "0" for x in small.ravel())
    return base


def hamming(a: str, b: str) -> int | float:
    if not a or not b or len(a) != len(b):
        return math.nan
    return sum(x != y for x, y in zip(a, b, strict=True))


def profile_episode(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    ep_id = int(path.stem.split("_")[-1])
    df = pd.read_parquet(path, columns=IMAGE_COLS + ["frame_index", "timestamp"])
    n = len(df)
    frame_rows: list[dict[str, Any]] = []
    stream_stats: dict[str, dict[str, Any]] = {}
    for col in IMAGE_COLS:
        previous_ahash = ""
        previous_sha1 = ""
        per_stream: list[dict[str, Any]] = []
        for i, value in enumerate(df[col]):
            m = image_metrics(value)
            m.update(
                {
                    "dataset_episode": ep_id,
                    "row": i,
                    "stream": col,
                    "frame_index": int(df["frame_index"].iloc[i]),
                    "timestamp": float(df["timestamp"].iloc[i]),
                    "adjacent_exact_duplicate": bool(previous_sha1 and previous_sha1 == m["sha1"]),
                    "adjacent_ahash_distance": hamming(previous_ahash, m["ahash"]),
                }
            )
            m["path_delta"] = m["path_frame_index"] - m["frame_index"] if np.isfinite(m["path_frame_index"]) else math.nan
            frame_rows.append(m)
            per_stream.append(m)
            previous_ahash = m["ahash"]
            previous_sha1 = m["sha1"]
        f = pd.DataFrame(per_stream)
        ok = f["decode_ok"].astype(bool)
        stream_stats[col] = {
            "decode_bad_count": int((~ok).sum()),
            "shape_bad_count": int(((f["width"] != 224) | (f["height"] != 224) | (f["channels"] != 3)).fillna(True).sum()),
            "missing_count": int((~f["payload_present"].astype(bool)).sum()),
            "dark_frame_count": int((f["dark_fraction"] >= 0.95).sum()),
            "bright_frame_count": int((f["bright_fraction"] >= 0.95).sum()),
            "low_contrast_count": int((f["std_luma"] <= 5.0).sum()),
            "exact_duplicate_adjacent_count": int(f["adjacent_exact_duplicate"].astype(bool).sum()),
            "path_mismatch_count": int((pd.to_numeric(f["path_delta"], errors="coerce").fillna(0) != 0).sum()),
            "ahash_distance_p01": float(pd.to_numeric(f["adjacent_ahash_distance"], errors="coerce").quantile(0.01)),
            "ahash_distance_median": float(pd.to_numeric(f["adjacent_ahash_distance"], errors="coerce").median()),
            "ahash_distance_p99": float(pd.to_numeric(f["adjacent_ahash_distance"], errors="coerce").quantile(0.99)),
            "byte_size_median": float(pd.to_numeric(f["byte_size"], errors="coerce").median()),
            "std_luma_median": float(pd.to_numeric(f["std_luma"], errors="coerce").median()),
            "gradient_energy_median": float(pd.to_numeric(f["gradient_energy"], errors="coerce").median()),
            "laplacian_var_median": float(pd.to_numeric(f["laplacian_var"], errors="coerce").median()),
        }

    episode: dict[str, Any] = {
        "episode_index": ep_id,
        "rows": n,
        "read_ok": True,
        "read_error": "",
    }
    for col, stats in stream_stats.items():
        prefix = col.replace("_image", "")
        for key, value in stats.items():
            episode[f"{prefix}_{key}"] = value
    episode["any_decode_bad_count"] = int(sum(x["decode_bad_count"] for x in stream_stats.values()))
    episode["any_missing_count"] = int(sum(x["missing_count"] for x in stream_stats.values()))
    episode["any_decode_corrupt_count"] = int(
        sum(max(0, x["decode_bad_count"] - x["missing_count"]) for x in stream_stats.values())
    )
    episode["any_shape_bad_count"] = int(sum(x["shape_bad_count"] for x in stream_stats.values()))
    episode["any_dark_frame_count"] = int(sum(x["dark_frame_count"] for x in stream_stats.values()))
    episode["any_bright_frame_count"] = int(sum(x["bright_frame_count"] for x in stream_stats.values()))
    episode["any_low_contrast_count"] = int(sum(x["low_contrast_count"] for x in stream_stats.values()))
    episode["any_exact_duplicate_adjacent_count"] = int(sum(x["exact_duplicate_adjacent_count"] for x in stream_stats.values()))
    episode["any_path_mismatch_count"] = int(sum(x["path_mismatch_count"] for x in stream_stats.values()))
    episode["any_image_bad_frame_count"] = int(
        sum(
            1
            for i in range(n)
            if any(
                (not bool(frame_rows[i + j * n]["decode_ok"]))
                or frame_rows[i + j * n]["width"] != 224
                or frame_rows[i + j * n]["height"] != 224
                or frame_rows[i + j * n]["channels"] != 3
                or frame_rows[i + j * n]["dark_fraction"] >= 0.95
                or frame_rows[i + j * n]["bright_fraction"] >= 0.95
                or frame_rows[i + j * n]["std_luma"] <= 5.0
                for j in range(len(IMAGE_COLS))
            )
        )
    )
    return episode, frame_rows


def read_episode_meta(root: Path) -> dict[int, dict[str, Any]]:
    files = list(root.rglob("meta/episodes.jsonl"))
    if not files:
        return {}
    result = {}
    for line in files[0].read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            result[int(row["episode_index"])] = row
    return result


def profile_dataset(root: Path, label: str) -> None:
    files = sorted(root.rglob("data/chunk-*/episode_*.parquet"))
    if not files:
        raise FileNotFoundError(root)
    meta = read_episode_meta(root)
    episodes = []
    frames = []
    started = time.time()
    for i, path in enumerate(files, start=1):
        ep_id = int(path.stem.split("_")[-1])
        try:
            ep, fr = profile_episode(path)
        except Exception as exc:
            ep = {
                "episode_index": ep_id,
                "rows": math.nan,
                "expected_rows": meta.get(ep_id, {}).get("length", math.nan),
                "read_ok": False,
                "read_error": f"{type(exc).__name__}: {exc}",
                "any_decode_bad_count": math.nan,
            }
            fr = []
            print(f"[{label}] episode {ep_id} READ_ERROR {type(exc).__name__}", flush=True)
        ep["expected_rows"] = meta.get(ep_id, {}).get("length", math.nan)
        ep["source_file"] = str(path)
        ep["dataset"] = label
        for row in fr:
            row["dataset"] = label
        episodes.append(ep)
        frames.extend(fr)
        print(f"[{label}] {i}/{len(files)} episode {ep_id} rows={ep['rows']}", flush=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(episodes).sort_values("episode_index").to_csv(
        OUT_DIR / f"{label}_image_episodes.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(frames).to_csv(OUT_DIR / f"{label}_image_frames.csv", index=False, encoding="utf-8-sig")
    summary = {
        "dataset": label,
        "episodes": len(episodes),
        "frames": int(sum(x.get("rows", 0) for x in episodes if isinstance(x.get("rows"), (int, float)) and np.isfinite(x.get("rows")))),
        "read_error_episodes": int(sum(not bool(x.get("read_ok", False)) for x in episodes)),
        "elapsed_seconds": time.time() - started,
    }
    (OUT_DIR / f"{label}_image_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


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
