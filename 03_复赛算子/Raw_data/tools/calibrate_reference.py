# -*- coding: utf-8 -*-
"""Regenerate a threshold file from a clean reference set (with provenance).

python tools/calibrate_reference.py --reference <参考集目录> --output calibration/default_thresholds.json [--episodes 0,1,2]

The file records the reference episode IDs, the SHA-256 of every reference
Parquet and a hash of the threshold values, so any report can state exactly
which calibration it used.
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.simplefilter("ignore", RuntimeWarning)

from refsync_qa import calibrate, dataset, features  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--episodes", default=None, help="逗号分隔的参考轨迹编号（默认全部）")
    a = ap.parse_args()
    ds = dataset.discover(a.reference)
    eps = ds.episodes
    if a.episodes:
        keep = {int(x) for x in a.episodes.split(",")}
        eps = [e for e in eps if e.episode_id in keep]
    ctx = dict(image_columns=ds.image_columns, state_dim=ds.state_dim, action_dim=ds.action_dim)
    thr = calibrate.load_thresholds(None)
    thr.update(calibrate.calibrate([features.extract(e.path, ctx) for e in eps]))
    thr["_provenance"] = calibrate.provenance([e.path for e in eps], [e.episode_id for e in eps], thr)
    out = Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(thr, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    print(f"{len(eps)} reference episodes {thr['_provenance']['reference_episode_ids']} -> {out}")
    print("thresholds_sha256", thr["_provenance"]["thresholds_sha256"])
    print("freeze_min_run", thr["freeze_min_run"], "vk_ref_lag", thr["vk_ref_lag"], "xcam_ref_lag", thr["xcam_ref_lag"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
