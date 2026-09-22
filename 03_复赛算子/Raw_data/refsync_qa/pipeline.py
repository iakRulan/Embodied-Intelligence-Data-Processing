# -*- coding: utf-8 -*-
"""End-to-end operator: discover -> calibrate -> detect -> repair -> re-check -> govern -> report."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import numpy as np

from . import config
from .calibrate import calibrate, load_thresholds
from .dataset import DatasetInfo, discover
from .detect import Result, analyse
from .features import extract
from .repair import Repairer
from .score import score, status

PKG_DIR = Path(__file__).resolve().parent
DEFAULT_CALIBRATION = PKG_DIR.parent / "calibration" / "default_thresholds.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ctx(ds: DatasetInfo, opts: dict[str, Any]) -> dict[str, Any]:
    return {
        "image_columns": ds.image_columns,
        "state_dim": ds.state_dim,
        "action_dim": ds.action_dim,
        "field_aliases": opts.get("field_aliases", config.FIELD_ALIASES),
    }


def _pack(res: Result) -> dict[str, Any]:
    sc = score(res)
    st, tier = status(res)
    return {
        "codes": res.all_codes(),
        "ep_codes": dict(res.ep_codes),
        "row_codes": [";".join(sorted(s)) for s in res.row_codes],
        "row_notes": [";".join(sorted(s)) for s in res.row_notes],
        "blocked": res.blocked_rows(),
        "metrics": res.metrics,
        "not_evaluable": list(res.not_evaluable),
        "score": sc,
        "status": st,
        "tier": tier,
    }


def _image_hashes(feats: dict[str, Any], thr: dict[str, Any]) -> list[str]:
    out = set()
    for sf in feats.get("streams", {}).values():
        solid = sf["decode_ok"] & (
            (sf["dark"] >= thr["screen_dark_fraction"]) | (sf["bright"] >= thr["screen_bright_fraction"]) | (sf["std"] <= thr["screen_std_luma"])
        )
        for h, ok, so in zip(sf["sha1"], sf["decode_ok"], solid):
            if ok and not so and h:
                out.add(h[:16])
    return sorted(out)


def process_episode(job: dict[str, Any]) -> dict[str, Any]:
    warnings.simplefilter("ignore", RuntimeWarning)
    t0 = time.perf_counter()
    ds: DatasetInfo = job["ds"]
    thr, opts = job["thr"], job["opts"]
    ep_id, path, rel = job["ep_id"], Path(job["path"]), job["rel"]
    feats = extract(path, _ctx(ds, opts))
    res = analyse(feats, ep_id, ds, thr, opts)
    orig = _pack(res)
    out: dict[str, Any] = {
        "episode_index": ep_id, "rel_path": rel, "rows": int(feats.get("n", 0)),
        "frame_index": feats.get("frame_index", np.zeros(0)).tolist(),
        "orig": orig, "final": orig, "repair": {"applied": [], "rejected": [], "audit": [], "meta_patch": {}},
        "image_hashes": _image_hashes(feats, thr) if feats.get("read_ok") else [],
        "source_sha256": _sha256(path) if path.exists() else "",
        "repaired_rel_path": "", "repaired_sha256": "",
    }
    if job.get("repair", True) and orig["codes"]:
        rep = Repairer(feats, res, ep_id, ds, thr, opts)
        changed = rep.run()
        ds_final = ds
        if rep.meta_patch:
            ds_final = copy.copy(ds)
            ds_final.episodes_meta = dict(ds.episodes_meta)
            ds_final.episodes_meta[ep_id] = {**ds.episodes_meta.get(ep_id, {"episode_index": ep_id}), **rep.meta_patch}
        feats2 = feats
        if changed:
            target = Path(job["governed_dir"]) / "data" / rel
            rep.write(target)
            feats2 = extract(target, _ctx(ds, opts))
            out["repaired_rel_path"] = "data/" + rel
            out["repaired_sha256"] = _sha256(target)
            out["frame_index_final"] = feats2.get("frame_index", np.zeros(0)).tolist()
        if changed or rep.meta_patch:
            out["final"] = _pack(analyse(feats2, ep_id, ds_final, thr, opts))
        out["repair"] = {"applied": rep.applied, "rejected": rep.rejected, "audit": rep.audit, "meta_patch": rep.meta_patch}
    out["seconds"] = round(time.perf_counter() - t0, 3)
    return out


def _clips(keep: np.ndarray, frame_index: list[float], min_len: int) -> list[tuple[int, int]]:
    """Runs of kept rows whose frame_index increases by exactly 1 (no hidden gaps)."""
    fi = np.asarray(frame_index, dtype=float)
    clips, start = [], None
    for i in range(len(keep)):
        if not keep[i]:
            if start is not None:
                clips.append((start, i - 1))
            start = None
            continue
        if start is None:
            start = i
        elif not (np.isfinite(fi[i]) and np.isfinite(fi[i - 1]) and fi[i] - fi[i - 1] == 1):
            clips.append((start, i - 1))
            start = i
    if start is not None:
        clips.append((start, len(keep) - 1))
    return [(a, b) for a, b in clips if b - a + 1 >= min_len]


def _dedup(results: list[dict[str, Any]], opts: dict[str, Any]) -> list[dict[str, Any]]:
    index: dict[str, list[int]] = {}
    sets = {r["episode_index"]: set(r["image_hashes"]) for r in results}
    for ep, hs in sets.items():
        for h in hs:
            index.setdefault(h, []).append(ep)
    shared: dict[tuple[int, int], int] = {}
    for eps in index.values():
        if 1 < len(eps) <= 50:
            for i, a in enumerate(eps):
                for b in eps[i + 1:]:
                    shared[(a, b)] = shared.get((a, b), 0) + 1
    pairs = []
    for (a, b), k in shared.items():
        if k < opts["min_shared_images"]:
            continue
        ov = k / max(1, min(len(sets[a]), len(sets[b])))
        if ov >= opts["partial_overlap"]:
            pairs.append({"episode_a": a, "episode_b": b, "shared_images": k, "images_a": len(sets[a]), "images_b": len(sets[b]),
                          "overlap": round(ov, 4), "relation": "重复" if ov >= opts["duplicate_overlap"] else "部分重合"})
    return sorted(pairs, key=lambda p: -p["overlap"])


def run(input_dir: str | Path, output_dir: str | Path, reference_dir: str | Path | None = None,
        calibration: str | Path | None = None, workers: int | None = None, repair: bool = True,
        options: dict[str, Any] | None = None, limit: int | None = None, log=print) -> dict[str, Any]:
    t_start = time.perf_counter()
    opts = {**config.DEFAULT_OPTIONS, **(options or {})}
    opts.setdefault("layout", config.DEFAULT_LAYOUT)
    out = Path(output_dir).expanduser().resolve()
    (out / "report").mkdir(parents=True, exist_ok=True)
    gov = out / "governed"
    gov.mkdir(parents=True, exist_ok=True)
    workers = workers or max(1, min(8, (os.cpu_count() or 2) - 1))

    ds = discover(input_dir, opts)
    if limit:
        ds.episodes = ds.episodes[:limit]
    log(f"[RefSync-QA] input={ds.root} episodes={len(ds.episodes)} fps={ds.fps} cameras={ds.image_columns} workers={workers}")
    for w in ds.warnings:
        log(f"[warn] {w}")

    # ---- thresholds ----
    calib_source = ""
    if reference_dir:
        ref = discover(reference_dir, opts)
        ctx = _ctx(ref, opts)
        log(f"[calibrate] reference episodes={len(ref.episodes)}")
        with ProcessPoolExecutor(max_workers=workers) as ex:
            ref_feats = list(ex.map(extract, [e.path for e in ref.episodes], [ctx] * len(ref.episodes)))
        thr = load_thresholds(None)
        thr.update(calibrate(ref_feats, opts))
        calib_source = f"reference set {ref.root} ({len(ref.episodes)} episodes)"
    else:
        path = Path(calibration) if calibration else DEFAULT_CALIBRATION
        thr = load_thresholds(path)
        calib_source = f"calibration file {path.name}" if path.exists() else "built-in fallback thresholds"
    (out / "report" / "thresholds_used.json").write_text(json.dumps(thr, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # ---- per-episode ----
    jobs = [{"ds": ds, "thr": thr, "opts": opts, "ep_id": e.episode_id, "path": str(e.path), "rel": e.rel_path.split("data/", 1)[-1] if "data/" in e.rel_path else e.rel_path,
             "governed_dir": str(gov), "repair": repair} for e in ds.episodes]
    results: list[dict[str, Any]] = []
    t_detect = time.perf_counter()
    if workers > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(process_episode, j) for j in jobs]
            for k, f in enumerate(as_completed(futs), 1):
                results.append(f.result())
                if k % 10 == 0 or k == len(jobs):
                    log(f"[detect] {k}/{len(jobs)}")
    else:
        for k, j in enumerate(jobs, 1):
            results.append(process_episode(j))
            log(f"[detect] {k}/{len(jobs)} episode {j['ep_id']}")
    results.sort(key=lambda r: r["episode_index"])
    detect_seconds = time.perf_counter() - t_detect

    # ---- dataset-level: duplicates ----
    dup_pairs = _dedup(results, opts) if opts.get("dedup", True) else []
    by_ep = {r["episode_index"]: r for r in results}
    for p in dup_pairs:
        code = "V_EPISODE_DUPLICATE" if p["relation"] == "重复" else "V_EPISODE_OVERLAP"
        for a, b in ((p["episode_a"], p["episode_b"]), (p["episode_b"], p["episode_a"])):
            for key in ("orig", "final"):
                pk = by_ep[a][key]
                pk["ep_codes"].setdefault(code, "")
                pk["ep_codes"][code] = (pk["ep_codes"][code] + "；" if pk["ep_codes"][code] else "") + f"与 ep{b} 重合 {p['overlap']:.0%}"
                if code not in pk["codes"]:
                    pk["codes"].append(code)
                if pk["status"] == "通过":
                    pk["status"] = "价值提示"

    from .report import build_outputs
    summary = build_outputs(results, dup_pairs, ds, thr, opts, out, gov, calib_source,
                            timings={"detect_and_repair_seconds": round(detect_seconds, 2),
                                     "total_seconds": round(time.perf_counter() - t_start, 2), "workers": workers})
    log(f"[done] {summary['headline']}")
    return summary
