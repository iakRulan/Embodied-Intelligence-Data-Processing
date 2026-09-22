# -*- coding: utf-8 -*-
"""Read governed training clips: original dataset + RefSync-QA governed overlay.

    from tools.load_governed import iter_clips
    for clip in iter_clips("<原始数据集目录>", "<输出目录>"):
        clip["table"]   # pyarrow.Table with exactly the clip rows (repaired copy when one exists)
        clip["episode_index"], clip["clip_id"], clip["sample_weight"]

or from the command line (prints a consistency check):

    python tools/load_governed.py --dataset <原始数据集目录> --output <输出目录>

The governed/ folder is an OVERLAY (repaired copies only) and is not a
loadable LeRobot dataset on its own.  Training must read only the rows listed
in governed/clips.csv (= train_keep in the quality masks).
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any, Iterator


def _rows(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def iter_clips(dataset_dir: str | Path, output_dir: str | Path, check: bool = True) -> Iterator[dict[str, Any]]:
    import pyarrow.parquet as pq

    ds, out = Path(dataset_dir), Path(output_dir)
    gov = out / "governed"
    masks = {int(r["episode_index"]): r for r in _rows(gov / "mask_index.csv")}
    cache: dict[str, Any] = {}
    for c in _rows(gov / "clips.csv"):
        ep = int(c["episode_index"])
        src = c["source"]
        path = out / src if src.startswith("governed/") else ds / src
        if str(path) not in cache:
            cache.clear()
            cache[str(path)] = pq.read_table(path)
        t = cache[str(path)]
        a, b = int(c["start_row"]), int(c["end_row"])
        clip = t.slice(a, b - a + 1)
        if check:
            fi = clip.column("frame_index").to_pylist()
            assert fi == list(range(int(float(c["start_frame"])), int(float(c["end_frame"])) + 1)), f"{c['clip_id']}: frame_index 不连续"
            mrows = _rows(gov / masks[ep]["mask_file"])
            assert all(mrows[i]["train_keep"] == "True" and mrows[i]["clip_id"] == c["clip_id"] for i in range(a, b + 1)), f"{c['clip_id']}: 掩膜不一致"
        yield {"episode_index": ep, "clip_id": c["clip_id"], "start_row": a, "end_row": b, "table": clip,
               "sample_weight": float(c["sample_weight"]), "origin": c["origin"], "source": str(path)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True, help="原始数据集目录（运行算子时的 --input）")
    ap.add_argument("--output", required=True, help="算子输出目录（含 governed/）")
    a = ap.parse_args()
    n_clip = n_rows = 0
    eps = set()
    for c in iter_clips(a.dataset, a.output):
        n_clip += 1
        n_rows += c["table"].num_rows
        eps.add(c["episode_index"])
    print(f"clips={n_clip} rows={n_rows} episodes={len(eps)}：帧号连续性与掩膜一致性检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
