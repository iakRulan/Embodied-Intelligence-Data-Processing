"""Full read-only-input acceptance: run, hash invariance, repair audit, training overlay.

Generated data stay under --output. A nonzero exit means not accepted.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from refsync_qa.pipeline import run
from refsync_qa.repair import verify_copy
from tools.load_governed import iter_clips


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--reference", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--timestamp-policy", choices=["conservative", "nominal", "off"], default="conservative")
    a = ap.parse_args()
    ds, ref, out = Path(a.input).resolve(), Path(a.reference).resolve(), Path(a.output).resolve()
    sources = sorted(set(p for root in (ds, ref) for p in root.rglob("*") if p.is_file()))
    before = {p: sha(p) for p in sources}
    print(f"[acceptance] hashed {len(sources)} input files", flush=True)
    s = run(ds, out, reference_dir=ref, workers=a.workers, options={"timestamp_policy": a.timestamp_policy})
    failures = []
    audits = defaultdict(list)
    for r in rows(out / "governed" / "repair_audit.csv"):
        audits[int(r["episode_index"])].append(r)
    copies = 0
    for r in rows(out / "governed" / "repair_manifest.csv"):
        if not r["repaired_file"]:
            continue
        copies += 1
        orig, fixed = ds / r["source_file"], out / "governed" / r["repaired_file"]
        if sha(orig) != r["source_sha256"] or sha(fixed) != r["repaired_sha256"]:
            failures.append(f"hash mismatch ep{r['episode_index']}")
        for problem in verify_copy(pq.read_table(orig), pq.read_table(fixed), audits[int(r["episode_index"])]):
            failures.append(f"ep{r['episode_index']}: {problem}")
    clips = frame_count = 0
    episodes = set()
    try:
        for c in iter_clips(ds, out):
            clips += 1
            frame_count += c["table"].num_rows
            episodes.add(c["episode_index"])
    except Exception as exc:
        failures.append(f"loader: {type(exc).__name__}: {exc}")
    if frame_count != s["governance"]["train_frames"]["total"]:
        failures.append("training frame denominator mismatch")
    changed = [p.relative_to(ds).as_posix() if ds in p.parents else "reference/" + p.relative_to(ref).as_posix()
               for p, value in before.items() if not p.is_file() or sha(p) != value]
    if changed:
        failures.append("input changed")
    if s["processing_errors"]:
        failures.append("processing errors")
    result = {"operator": s["operator"], "timestamp_policy": a.timestamp_policy,
              "input_files_hashed": len(before), "input_files_changed": changed,
              "episodes": s["scope"]["episodes"], "reference_episodes": len(s["calibration_provenance"]["reference_episode_ids"]),
              "verified_repaired_copies": copies, "verified_clips": clips, "verified_training_frames": frame_count,
              "training_episodes": len(episodes), "thresholds_sha256": s["calibration_provenance"]["thresholds_sha256"],
              "failures": failures, "passed": not failures}
    (out / "report" / "acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
