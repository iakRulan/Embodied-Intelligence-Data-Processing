# -*- coding: utf-8 -*-
"""End-to-end operator: guard -> discover -> calibrate -> detect -> repair -> re-check -> dedup -> govern -> report."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import platform
import shutil
import sys
import time
import traceback
import warnings
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import numpy as np

from . import config
from .calibrate import calibrate, load_thresholds, provenance, thresholds_hash
from .dataset import MARKER, DatasetInfo, discover
from .detect import Result, analyse, screen_mask
from .features import extract
from .repair import Repairer
from .score import score, status

PKG_DIR = Path(__file__).resolve().parent
DEFAULT_CALIBRATION = PKG_DIR.parent / "calibration" / "default_thresholds.json"


class PathGuardError(RuntimeError):
    """Input/output overlap that could let the operator write onto its own input."""


def _real(p: str | Path) -> Path:
    return Path(os.path.realpath(os.path.expanduser(str(p))))


def _inside(a: Path, b: Path) -> bool:
    """a == b or a is below b (both real paths)."""
    return a == b or b in a.parents


def guard_paths(input_dir: str | Path, output_dir: str | Path, reference_dir: str | Path | None = None) -> None:
    """Refuse any configuration in which an output could be written into (or read back as) an input."""
    ro = _real(output_dir)
    for label, p in (("input", input_dir), ("reference", reference_dir)):
        if p is None:
            continue
        ri = _real(p)
        if _inside(ro, ri):
            raise PathGuardError(f"output {ro} 位于 {label} {ri} 之内（或相同），拒绝运行：修复副本可能写回/递归读入原始数据")
        if _inside(ri, ro):
            raise PathGuardError(f"{label} {ri} 位于 output {ro} 之内，拒绝运行：输出目录中的旧产物不得作为输入")


def _prepare_output(out: Path, input_root: Path, opts: dict[str, Any], log) -> None:
    """Mark the output directory and move (or, when marked and asked, delete) the previous run's artefacts."""
    out.mkdir(parents=True, exist_ok=True)
    marker = out / MARKER
    old = [out / d for d in ("report", "governed") if (out / d).exists()]
    if old:
        if not marker.is_file():
            raise PathGuardError("输出含未标记的 report/governed，拒绝移动或删除用户目录；请选择新的输出目录")
        for d in old + [out / "_previous_runs"]:
            if _real(d) != d.absolute() or not _inside(_real(d), _real(out)):
                raise PathGuardError(f"输出子目录含链接或越界路径，拒绝处理: {d}")
        if opts.get("clean_previous") and marker.exists():
            for d in old:
                shutil.rmtree(d)
            log(f"[output] 已删除上次运行产物（有输出标记）：{', '.join(d.name for d in old)}")
        else:
            dst = out / "_previous_runs" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
            dst.mkdir(parents=True, exist_ok=True)
            for d in old:
                shutil.move(str(d), str(dst / d.name))
            log(f"[output] 上次运行产物已移至 {dst}")
    marker.write_text(json.dumps({"operator": config.VERSION, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                                  "input": str(input_root), "note": "RefSync-QA 输出目录；数据发现会跳过含此标记的目录"},
                                 ensure_ascii=False, indent=2), encoding="utf-8")


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


def row_signatures(feats: dict[str, Any], thr: dict[str, Any], image_columns: list[str]) -> tuple[list[str], list[str]]:
    """Per-row content signatures for cross-episode redundancy.

    image signature: all declared cameras decoded and non-solid -> sha1 prefixes joined, else "".
    full signature : image signature + exact state/actions bytes (all finite), else "".
    """
    n = int(feats.get("n", 0))
    if not feats.get("read_ok") or n == 0:
        return [], []
    streams = feats.get("streams", {})
    if any(c not in streams for c in image_columns):
        return [""] * n, [""] * n
    ok = np.ones(n, bool)
    for c in image_columns:
        sf = streams[c]
        ok &= sf["decode_ok"] & ~screen_mask(sf, thr)
    S, A = feats["state"], feats["action"]
    fin = np.isfinite(S).all(axis=1) & np.isfinite(A).all(axis=1) & feats["state_dim_ok"] & feats["action_dim_ok"]
    img, full = [], []
    for i in range(n):
        if not ok[i]:
            img.append("")
            full.append("")
            continue
        s = "|".join(streams[c]["sha1"][i][:16] for c in image_columns)
        img.append(s)
        if fin[i]:
            h = hashlib.sha1(s.encode() + S[i].astype(np.float64).tobytes() + A[i].astype(np.float64).tobytes()).hexdigest()[:20]
            full.append(h)
        else:
            full.append("")
    return img, full


def _images_decoded(feats: dict[str, Any]) -> int:
    return int(sum(int(sf["decode_ok"].sum()) for sf in feats.get("streams", {}).values()))


def process_episode(job: dict[str, Any]) -> dict[str, Any]:
    warnings.simplefilter("ignore", RuntimeWarning)
    t0 = time.perf_counter()
    ds: DatasetInfo = job["ds"]
    thr, opts = job["thr"], job["opts"]
    ep_id, path, rel = job["ep_id"], Path(job["path"]), job["rel"]
    feats = extract(path, _ctx(ds, opts))
    res = analyse(feats, ep_id, ds, thr, opts)
    orig = _pack(res)
    img_sig, full_sig = row_signatures(feats, thr, ds.image_columns)
    out: dict[str, Any] = {
        "episode_index": ep_id, "rel_path": job.get("source_rel", rel), "rows": int(feats.get("n", 0)),
        "timestamp_final": feats.get("timestamp", np.zeros(0)).tolist(),
        "frame_index": feats.get("frame_index", np.zeros(0)).tolist(),
        "orig": orig, "final": orig, "repair": {"applied": [], "rejected": [], "audit": [], "meta_patch": {}, "candidates": []},
        "row_img_sig": img_sig, "row_full_sig": full_sig,
        "source_sha256": _sha256(path) if path.exists() else "",
        "repaired_rel_path": "", "repaired_sha256": "", "images_decoded": _images_decoded(feats), "error": "",
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
            if rep.write(target, forbidden=set(job.get("forbidden", ()))):
                feats2 = extract(target, _ctx(ds, opts))
                out["repaired_rel_path"] = "data/" + rel
                out["repaired_sha256"] = _sha256(target)
                out["frame_index_final"] = feats2.get("frame_index", np.zeros(0)).tolist()
                out["timestamp_final"] = feats2.get("timestamp", np.zeros(0)).tolist()
                out["images_decoded"] += _images_decoded(feats2)
                out["row_img_sig"], out["row_full_sig"] = row_signatures(feats2, thr, ds.image_columns)
            else:
                rep.rejected.append("写后逐单元格校验失败，修复副本已丢弃：" + "；".join(rep.verify_problems[:5]))
                rep.applied = [a for a in rep.applied if "元数据补丁" in a]
                rep.audit = [a for a in rep.audit if a["field"] == "episodes.jsonl.length"]
                changed = False
        if changed or rep.meta_patch:
            out["final"] = _pack(analyse(feats2, ep_id, ds_final, thr, opts))
        out["repair"] = {"applied": rep.applied, "rejected": rep.rejected, "audit": rep.audit, "meta_patch": rep.meta_patch,
                         "candidates": rep.candidates}
    if out["final"] is out["orig"]:
        out["final"] = copy.deepcopy(orig)
    out["seconds"] = round(time.perf_counter() - t0, 3)
    return out


def error_result(job: dict[str, Any], exc: BaseException | str) -> dict[str, Any]:
    """A failed episode becomes a C_PROCESSING_ERROR record; the batch continues."""
    msg = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    R = Result(0)
    R.ep("C_PROCESSING_ERROR", str(msg)[:300])
    pk = _pack(R)
    path = Path(job["path"])
    try:
        sha = _sha256(path) if path.exists() else ""
    except OSError:
        sha = ""
    return {"episode_index": job["ep_id"], "rel_path": job.get("source_rel", job["rel"]), "rows": 0, "frame_index": [], "orig": pk, "final": copy.deepcopy(pk),
            "repair": {"applied": [], "rejected": [], "audit": [], "meta_patch": {}, "candidates": []},
            "row_img_sig": [], "row_full_sig": [], "source_sha256": sha, "repaired_rel_path": "", "repaired_sha256": "",
            "images_decoded": 0, "error": str(msg)[:2000], "seconds": 0.0}


def process_episode_safe(job: dict[str, Any]) -> dict[str, Any]:
    try:
        return process_episode(job)
    except Exception as exc:  # noqa: BLE001 - one bad episode must not stop the batch
        r = error_result(job, exc)
        r["error"] += "\n" + traceback.format_exc(limit=6)
        return r


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
        elif not (i < len(fi) and np.isfinite(fi[i]) and np.isfinite(fi[i - 1]) and fi[i] - fi[i - 1] == 1):
            clips.append((start, i - 1))
            start = i
    if start is not None:
        clips.append((start, len(keep) - 1))
    return [(a, b) for a, b in clips if b - a + 1 >= min_len]


def relations(results: list[dict[str, Any]], opts: dict[str, Any]) -> list[dict[str, Any]]:
    """Pairwise redundancy relations from row signatures (no transitive grouping).

    整轨等价   : both directions' row coverage, row order and state/actions all >= equivalent_coverage, same task
    同源副本   : images cover each other but order/kinematics/task differ
    包含       : one episode's rows are (almost) all inside the other
    部分重合   : max coverage >= partial_overlap
    """
    eq = float(opts.get("equivalent_coverage", 0.95))
    part = float(opts.get("partial_overlap", 0.3))
    min_shared = int(opts.get("min_shared_images", 30))
    pos: dict[int, dict[str, int]] = {}
    fulls: dict[int, set[str]] = {}
    index: dict[str, list[int]] = {}
    for r in results:
        ep = r["episode_index"]
        d: dict[str, int] = {}
        for i, s in enumerate(r.get("row_img_sig", [])):
            if s and s not in d:
                d[s] = i
        pos[ep] = d
        fulls[ep] = {s for s in r.get("row_full_sig", []) if s}
        for s in d:
            index.setdefault(s, []).append(ep)
    shared: dict[tuple[int, int], int] = {}
    for eps in index.values():
        if 1 < len(eps) <= 200:
            for i, a in enumerate(eps):
                for b in eps[i + 1:]:
                    key = (a, b) if a < b else (b, a)
                    shared[key] = shared.get(key, 0) + 1
    task = {r["episode_index"]: r["final"]["metrics"].get("task_index") for r in results}
    full_rows = {r["episode_index"]: r.get("row_full_sig", []) for r in results}
    out = []
    for (a, b), k in shared.items():
        if k < min_shared:
            continue
        na, nb = len(pos[a]), len(pos[b])
        cov_a, cov_b = k / max(na, 1), k / max(nb, 1)
        if max(cov_a, cov_b) < part:
            continue
        common = sorted((i, pos[b][s]) for s, i in pos[a].items() if s in pos[b])
        jb = [j for _, j in common]
        order = float(np.mean(np.diff(jb) > 0)) if len(jb) > 1 else 1.0
        a_full = [full_rows[a][i] for i, _ in common if i < len(full_rows[a])]
        kin = float(np.mean([bool(x) and x in fulls[b] for x in a_full])) if a_full else 0.0
        same_task = task.get(a) is not None and task.get(a) == task.get(b)
        if cov_a >= eq and cov_b >= eq:
            exact = bool(full_rows[a]) and all(full_rows[a]) and full_rows[a] == full_rows[b]
            rel = "整轨等价" if exact and same_task else "同源副本"
        elif max(cov_a, cov_b) >= eq:
            rel = "包含"
        else:
            rel = "部分重合"
        out.append({"episode_a": a, "episode_b": b, "shared_rows": k, "signed_rows_a": na, "signed_rows_b": nb,
                    "coverage_a": round(cov_a, 4), "coverage_b": round(cov_b, 4), "order_consistency": round(order, 4),
                    "kinematic_consistency": round(kin, 4), "same_task": same_task, "relation": rel})
    return sorted(out, key=lambda p: (-max(p["coverage_a"], p["coverage_b"]), p["episode_a"], p["episode_b"]))


def _hardware() -> dict[str, Any]:
    info = {"platform": platform.platform(), "processor": platform.processor() or platform.machine(),
            "cpu_count": os.cpu_count(), "python": sys.version.split()[0]}
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            kb = int(f.readline().split()[1])
            info["memory_gib"] = round(kb / 1024 / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    return info


def validate_options(opts: dict[str, Any], workers=None, limit=None) -> None:
    unknown = set(opts) - set(config.DEFAULT_OPTIONS) - {"layout", "field_aliases", "dedup"}
    if unknown:
        raise ValueError(f"未知 options 配置: {sorted(unknown)}")
    for key, val in [("workers", workers), ("limit", limit)] + [(k, opts[k]) for k in
                    ("min_clip_frames", "sparse_repair_max_gap", "max_lag_search", "min_shared_images", "vk_min_frames")]:
        if val is not None and (isinstance(val, bool) or not isinstance(val, int) or val <= 0):
            raise ValueError(f"{key} 必须是正整数")
    for key in ("sparse_repair_max_ratio", "low_value_sample_weight", "estimated_repair_sample_weight", "equivalent_coverage", "partial_overlap",
                "index_offset_detect_support", "index_offset_repair_support", "shift_cross_camera_min_r"):
        val = opts[key]
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or not 0 <= val <= 1:
            raise ValueError(f"{key} 必须在 [0,1] 内")
    if opts["timestamp_policy"] not in ("off", "conservative", "nominal"):
        raise ValueError("未知 timestamp_policy")
    shape = opts.get("expected_image_shape")
    if shape is not None and (not isinstance(shape, (tuple, list)) or len(shape) != 3 or
                             any(not isinstance(x, int) or x <= 0 for x in shape)):
        raise ValueError("expected_image_shape 必须为三个正整数")
    aliases = opts.get("field_aliases", config.FIELD_ALIASES)
    if set(aliases) != set(config.FIELD_ALIASES) or any(not isinstance(v, list) or not v or
            any(not isinstance(x, str) or not x for x in v) for v in aliases.values()):
        raise ValueError("field_aliases 必须完整列出七类字段及非空别名列表")
    layout = opts["layout"]
    dim = layout.get("dim", 0)
    if not isinstance(dim, int) or dim <= 0 or not layout.get("arms"):
        raise ValueError("layout 必须含正整数 dim 及 arms")
    used = []
    for arm in layout["arms"].values():
        for key, size in (("pos", 3), ("rot6d", 6), ("gripper", 1)):
            vals = arm.get(key, [])
            if len(vals) != size or any(not isinstance(i, int) or not 0 <= i < dim for i in vals):
                raise ValueError(f"layout.{key} 索引长度/范围非法")
            used.extend(vals)
    if len(set(used)) != len(used):
        raise ValueError("layout 各语义分量索引不能重叠")


def run(input_dir: str | Path, output_dir: str | Path, reference_dir: str | Path | None = None,
        calibration: str | Path | None = None, workers: int | None = None, repair: bool = True,
        options: dict[str, Any] | None = None, limit: int | None = None, log=print) -> dict[str, Any]:
    t_start = time.perf_counter()
    opts = {**config.DEFAULT_OPTIONS, **(options or {})}
    opts.setdefault("layout", config.DEFAULT_LAYOUT)
    validate_options(opts, workers, limit)
    guard_paths(input_dir, output_dir, reference_dir)  # P0: never write into (or read back) an input
    out = _real(output_dir)
    workers = workers if workers is not None else max(1, min(8, (os.cpu_count() or 2) - 1))

    ds = discover(input_dir, opts)
    if not ds.episodes:
        raise ValueError("输入没有可处理的 Parquet；未生成空成功报告")
    if limit is not None:
        ds.episodes = ds.episodes[:limit]
    gov = out / "governed"
    log(f"[RefSync-QA {config.VERSION}] input={ds.root} episodes={len(ds.episodes)} fps={ds.fps} cameras={ds.image_columns} workers={workers}")
    for w in ds.warnings:
        log(f"[warn] {w}")
    forbidden = {os.path.realpath(e.path) for e in ds.episodes}

    # ---- thresholds ----
    calib_source = ""
    prov: dict[str, Any] = {}
    if reference_dir:
        ref = discover(reference_dir, opts)
        if not ref.episodes:
            raise ValueError("参考集为空，不能标定阈值")
        forbidden |= {os.path.realpath(e.path) for e in ref.episodes}
        ctx = _ctx(ref, opts)
        log(f"[calibrate] reference episodes={len(ref.episodes)}")
        with ProcessPoolExecutor(max_workers=workers) as ex:
            ref_feats = list(ex.map(extract, [e.path for e in ref.episodes], [ctx] * len(ref.episodes)))
        if any(not f.get("read_ok") or not f.get("n") for f in ref_feats):
            raise ValueError("参考集含不可读/空轨迹，未使用部分参考集静默标定")
        thr = load_thresholds(None)
        thr.update(calibrate(ref_feats, opts))
        prov = provenance([e.path for e in ref.episodes], [e.episode_id for e in ref.episodes], thr)
        thr["_provenance"] = prov
        calib_source = f"现场标定：参考集 {ref.root}（{len(ref.episodes)} 条：{prov['reference_episode_ids']}）"
    else:
        path = Path(calibration) if calibration else DEFAULT_CALIBRATION
        if calibration and not path.is_file():
            raise FileNotFoundError(f"指定阈值文件不存在: {path}")
        thr = load_thresholds(path)
        prov = thr.get("_provenance", {})
        ids = prov.get("reference_episode_ids")
        calib_source = (f"阈值文件 {path.name}" + (f"（参考集 {len(ids)} 条：{ids}）" if ids else "")) if path.exists() else "内置 fallback 阈值"
    calib_hash = thresholds_hash(thr)
    _prepare_output(out, ds.root, opts, log)
    (out / "report").mkdir(parents=True, exist_ok=True)
    gov.mkdir(parents=True, exist_ok=True)
    (out / "report" / "thresholds_used.json").write_text(json.dumps(thr, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # ---- per-episode (each failure becomes a record; the batch continues) ----
    jobs = [{"ds": ds, "thr": thr, "opts": opts, "ep_id": e.episode_id, "path": str(e.path),
             "rel": e.rel_path.removeprefix("data/"), "source_rel": e.rel_path,
             "governed_dir": str(gov), "repair": repair, "forbidden": sorted(forbidden)} for e in ds.episodes]
    results: list[dict[str, Any]] = []
    t_detect = time.perf_counter()
    retry: list[dict[str, Any]] = []
    if workers > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(process_episode_safe, j): j for j in jobs}
            for k, f in enumerate(as_completed(futs), 1):
                try:
                    results.append(f.result())
                except Exception as exc:  # noqa: BLE001 - e.g. a crashed worker process
                    log(f"[warn] episode {futs[f]['ep_id']} worker failure ({type(exc).__name__}); retrying in-process")
                    retry.append(futs[f])
                if k % 10 == 0 or k == len(jobs):
                    log(f"[detect] {k}/{len(jobs)}")
    else:
        retry = jobs
    for k, j in enumerate(retry, 1):
        results.append(process_episode_safe(j))
        if workers <= 1 or len(jobs) <= 1:
            log(f"[detect] {k}/{len(jobs)} episode {j['ep_id']}")
    results.sort(key=lambda r: r["episode_index"])
    detect_seconds = time.perf_counter() - t_detect
    errors = [r for r in results if r.get("error")]
    for r in errors:
        log(f"[error] episode {r['episode_index']}: {r['error'].splitlines()[0]}")

    # ---- dataset-level: redundancy relations (codes only; frame-level dropping happens in governance) ----
    pairs = relations(results, opts) if opts.get("dedup", True) else []
    by_ep = {r["episode_index"]: r for r in results}
    for p in pairs:
        code = "V_EPISODE_DUPLICATE" if p["relation"] == "整轨等价" else "V_EPISODE_OVERLAP"
        for a, b, cov in ((p["episode_a"], p["episode_b"], p["coverage_a"]), (p["episode_b"], p["episode_a"], p["coverage_b"])):
            for key in ("orig", "final"):
                pk = by_ep[a][key]
                note = f"与 ep{b} {p['relation']}（本轨 {cov:.0%} 行被覆盖）"
                pk["ep_codes"][code] = (pk["ep_codes"][code] + "；" + note) if pk["ep_codes"].get(code) else note
                if code not in pk["codes"]:
                    pk["codes"].append(code)

    from .report import build_outputs, write_summary
    summary = build_outputs(results, pairs, ds, thr, opts, out, gov, calib_source)
    total = time.perf_counter() - t_start
    n_img = int(sum(r.get("images_decoded", 0) for r in results))
    summary["calibration_provenance"] = {"thresholds_sha256": calib_hash, **{k: v for k, v in prov.items() if k != "thresholds_sha256"}}
    summary["processing_errors"] = [{"episode_index": r["episode_index"], "error": r["error"].splitlines()[0]} for r in errors]
    summary["options"] = opts
    summary["timing"] = {
        "detect_repair_recheck_seconds": round(detect_seconds, 2),
        "total_seconds_incl_all_csv_parquet_outputs": round(total, 2),
        "workers": workers, "episodes": len(results), "rows": int(sum(r["rows"] for r in results)),
        "images_decoded": n_img,
        "episodes_per_second": round(len(results) / total, 3) if total else None,
        "decoded_images_per_second": round(n_img / total, 1) if total else None,
        "hardware": _hardware(),
        "note": "计时包含读取、检测、修复、复检、去重与全部 CSV/Parquet/掩膜写出；summary/xlsx 序列化在其后；冷/热缓存未区分。",
    }
    write_summary(summary, out / "report", gov)
    log(f"[done] {summary['headline']}")
    return summary
