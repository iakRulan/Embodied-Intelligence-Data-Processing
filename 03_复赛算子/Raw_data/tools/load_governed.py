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
import math
import json
import sys
from pathlib import Path
from typing import Any, Iterator


def _rows(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _under(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or root.resolve() not in path.parents:
        raise ValueError(f"清单路径越界: {relative}")
    return path


def _scalars(column) -> list:
    vals = column.to_pylist()
    return [v[0] if isinstance(v, list) and len(v) == 1 else v for v in vals]


def iter_clips(dataset_dir: str | Path, output_dir: str | Path, check: bool = True) -> Iterator[dict[str, Any]]:
    import pyarrow.parquet as pq

    ds, out = Path(dataset_dir).resolve(), Path(output_dir).resolve()
    if ds.is_file():
        ds = ds.parent.parent.parent if ds.parent.name.startswith("chunk-") else ds.parent
    gov = out / "governed"
    summary_path = out / "report" / "summary.json"
    options = json.loads(summary_path.read_text(encoding="utf-8")).get("options", {}) if summary_path.exists() else {}
    aliases = options.get("field_aliases", {})
    def column(table, key):
        name = next((k for k in aliases.get(key, [key]) if k in table.column_names), None)
        if name is None:
            raise ValueError(f"训练片段缺少字段 {key}")
        return table.column(name)
    masks = {int(r["episode_index"]): r for r in _rows(gov / "mask_index.csv")}
    cache: dict[str, Any] = {}
    for c in _rows(gov / "clips.csv"):
        ep = int(c["episode_index"])
        src = c["source"]
        path = _under(gov, src.removeprefix("governed/")) if src.startswith("governed/") else _under(ds, src)
        if str(path) not in cache:
            cache.clear()
            cache[str(path)] = pq.read_table(path)
        t = cache[str(path)]
        a, b = int(c["start_row"]), int(c["end_row"])
        if not 0 <= a <= b < t.num_rows or b - a + 1 != int(c["frames"]):
            raise ValueError(f"{c['clip_id']}: 片段越界/行数不一致")
        weight = float(c["sample_weight"])
        if not math.isfinite(weight) or not 0 <= weight <= 1:
            raise ValueError(f"{c['clip_id']}: 权重非法")
        clip = t.slice(a, b - a + 1)
        if check:
            fi = _scalars(column(clip, "frame_index"))
            if fi != list(range(int(float(c["start_frame"])), int(float(c["end_frame"])) + 1)):
                raise ValueError(f"{c['clip_id']}: frame_index 不连续")
            ts = _scalars(column(clip, "timestamp"))
            if any(not isinstance(v, (float, int)) or not math.isfinite(v) for v in ts) or any(y <= x for x, y in zip(ts, ts[1:])):
                raise ValueError(f"{c['clip_id']}: timestamp 无效/非递增")
            mi = masks[ep]
            mrows = _rows(_under(gov, mi["mask_file"]))
            if mi["source_file"] != src or len(mrows) != t.num_rows or int(mi["rows"]) != t.num_rows:
                raise ValueError(f"{c['clip_id']}: 掩膜来源/行数不一致")
            if not all(int(mrows[i]["row"]) == i and mrows[i]["train_keep"] == "True" and
                       mrows[i]["frame_valid"] == "True" and mrows[i]["clip_id"] == c["clip_id"] and
                       float(mrows[i]["sample_weight"]) == weight and float(mrows[i]["frame_index"]) == fi[i-a]
                       for i in range(a, b + 1)):
                raise ValueError(f"{c['clip_id']}: 掩膜不一致")
        yield {"episode_index": ep, "clip_id": c["clip_id"], "start_row": a, "end_row": b, "table": clip,
               "sample_weight": weight, "origin": c["origin"], "source": str(path)}


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
