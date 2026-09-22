# -*- coding: utf-8 -*-
"""Governance decisions and all output files (one consistent source: the packed results).

Governance order
1. frame_valid  = final (post-repair) rows without a training-blocking code;
2. clips        = runs of frame_valid rows with consecutive frame_index, >= min_clip_frames;
3. frame-level redundancy removal (non-transitive): in priority order, a clip
   row is dropped only when an identical all-modality row (all cameras +
   state + actions) is already KEPT in a higher-priority episode; clips are
   then recomputed;
4. train_keep   = member of a final clip (the only rows a trainer should read).
"""
from __future__ import annotations

import csv
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from . import config
from .detect import _runs
from .score import score_parts, status_codes

_ORIGIN = {"整段可用": "原本可用", "修复后整段可用": "修复恢复", "切段使用": "切段新增", "部分修复+切段使用": "修复+切段"}


def _w(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = fields or (list(rows[0].keys()) if rows else ["empty"])
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if (isinstance(v, float) and math.isnan(v)) else v) for k, v in r.items()})


def _cats(codes: list[str]) -> list[str]:
    return [c for c in config.CATEGORIES if any(config.category(x) == c for x in codes)]


def _defects(codes: list[str]) -> list[str]:
    return [c for c in codes if config.category(c) != config.CAT_V]


def _rescore(pk: dict[str, Any], redundancy: float) -> None:
    rows = [set(filter(None, s.split(";"))) for s in pk["row_codes"]]
    pk["score"] = score_parts(rows, pk["ep_codes"], pk["metrics"], redundancy)
    pk["codes"] = sorted(set(pk["codes"]) | set(pk["ep_codes"]), key=lambda c: (config.CATEGORIES.index(config.category(c)), c))
    pk["status"], pk["tier"] = status_codes(pk["codes"])


def _nan(x: Any) -> Any:
    return None if isinstance(x, float) and math.isnan(x) else x


def governance(results: list[dict[str, Any]], opts: dict[str, Any]) -> dict[int, dict[str, Any]]:
    from .pipeline import _clips

    min_clip = int(opts["min_clip_frames"])
    gov: dict[int, dict[str, Any]] = {}
    for r in results:
        ep, n, fin, org = r["episode_index"], r["rows"], r["final"], r["orig"]
        valid = ~np.asarray(fin["blocked"], dtype=bool) if n else np.zeros(0, bool)
        fi = r.get("frame_index_final", r["frame_index"])
        repaired = bool(r["repaired_rel_path"] or r["repair"]["meta_patch"])
        fatal = {"C_FILE_UNREADABLE", "C_EMPTY_EPISODE", "C_SCHEMA_MISSING_FIELD", "C_PROCESSING_ERROR"} & set(fin["ep_codes"])
        clips = [] if fatal else _clips(valid, fi, min_clip)
        whole = bool(n) and len(clips) == 1 and clips[0] == (0, n - 1)
        if "C_PROCESSING_ERROR" in fin["ep_codes"]:
            policy, valid = "处理失败", np.zeros(n, bool)
        elif fatal:
            policy, valid = "文件不可用", np.zeros(n, bool)
        elif whole and not _defects(org["codes"]):
            policy = "整段可用"
        elif whole and repaired:
            policy = "修复后整段可用"
        elif whole:
            policy = "整段可用"
        elif clips:
            policy = "部分修复+切段使用" if repaired else "切段使用"
        else:
            policy = "隔离回采"
        low_value = any(config.category(c) == config.CAT_V and c not in ("V_EPISODE_OVERLAP", "V_EPISODE_DUPLICATE") for c in fin["codes"])
        gov[ep] = {"policy": policy, "policy_before_dedup": policy, "valid": valid, "clips": clips, "clips_before_dedup": list(clips),
                   "repaired": repaired, "fi": fi, "weight": opts["low_value_sample_weight"] if low_value else 1.0,
                   "final_score": fin["score"].get("overall_score") or 0.0, "dedup_drop": np.zeros(n, bool), "covered_by": set()}

    # ---- frame-level, non-transitive redundancy removal ----
    if opts.get("dedup", True):
        by_ep = {r["episode_index"]: r for r in results}

        def prio(ep: int) -> tuple:
            g = gov[ep]
            return (sum(b - a + 1 for a, b in g["clips"]), g["final_score"], -ep)

        kept: dict[str, int] = {}
        for ep in sorted(gov, key=prio, reverse=True):
            g = gov[ep]
            sig = by_ep[ep].get("row_full_sig", [])
            member = np.zeros(len(g["valid"]), bool)
            for a, b in g["clips"]:
                member[a : b + 1] = True
            if len(sig) == len(member):
                drop = np.array([member[i] and bool(sig[i]) and sig[i] in kept for i in range(len(member))], dtype=bool)
            else:
                drop = np.zeros(len(member), bool)
            if drop.any():
                g["dedup_drop"] = drop
                g["covered_by"] = {kept[sig[i]] for i in np.flatnonzero(drop)}
                g["clips"] = _clips(member & ~drop, g["fi"], min_clip)
                if not g["clips"]:
                    g["policy"] = "重复剔除"
            for a, b in g["clips"]:
                for i in range(a, b + 1):
                    if i < len(sig) and sig[i] and sig[i] not in kept:
                        kept[sig[i]] = ep
    for ep, g in gov.items():
        n = len(g["valid"])
        g["train"] = np.zeros(n, bool)
        g["clip_id"] = [""] * n
        for a, b in g["clips"]:
            g["train"][a : b + 1] = True
            for i in range(a, b + 1):
                g["clip_id"][i] = f"ep{ep:06d}_{a:05d}_{b:05d}"
        before = sum(b - a + 1 for a, b in g["clips_before_dedup"])
        g["dedup_lost_frames"] = before - int(g["train"].sum())
        g["redundancy"] = (g["dedup_lost_frames"] / n) if n else 0.0
    return gov


def build_outputs(results, pairs, ds, thr, opts, out: Path, gov_dir: Path, calib_source: str) -> dict[str, Any]:
    min_clip = int(opts["min_clip_frames"])
    rep_dir = out / "report"
    gov = governance(results, opts)
    # value score after dataset-level redundancy (both packs, same governance-derived redundancy)
    for r in results:
        for key in ("orig", "final"):
            _rescore(r[key], gov[r["episode_index"]]["redundancy"])

    # ---------------- tables ----------------
    ep_rows, frame_rows, interval_rows, mask_index, clips_all, cand_rows = [], [], [], [], [], []
    for r in results:
        ep, n, org, fin = r["episode_index"], r["rows"], r["orig"], r["final"]
        g = gov[ep]
        m = org["metrics"]
        code_frames = Counter()
        for s in org["row_codes"]:
            for c in filter(None, s.split(";")):
                code_frames[c] += 1
        details = []
        for c in org["codes"]:
            d = org["ep_codes"].get(c, "")
            k = code_frames.get(c, 0)
            details.append(f"{c}({config.describe(c)}{'；' + d if d else ''}{'；' + str(k) + ' 帧' if k else ''})")
        streams = m.get("streams", {})
        train_frames = int(g["train"].sum())
        sync_idx = [f"{k}:path偏移{v['shift_frames']:+d}" for k, v in streams.items() if v.get("shift_frames")]
        sync_idx += [f"{k}:{'/'.join(v['gaps'])}" for k, v in streams.items() if v.get("gaps")]
        sync_lag = [f"{k}:{v['vk_best_lag']:+d}(r={v['vk_r']},增益={v.get('vk_gain')})" for k, v in streams.items() if "vk_best_lag" in v]
        ep_rows.append({
            "episode_index": ep, "file": r["rel_path"], "rows": n,
            "status": org["status"], "evidence_tier": org["tier"],
            "defect_count": len(_defects(org["codes"])),
            "categories": ";".join(_cats(org["codes"])),
            "issue_codes": ";".join(org["codes"]),
            "issue_details": "；".join(details),
            "flagged_frames": int(sum(1 for s in org["row_codes"] if s)),
            **{k: _nan(org["score"].get(k)) for k in ("structure_score", "temporal_score", "sync_score", "content_score", "value_score",
                                                       "value_score_partial", "overall_score", "score_gate")},
            "value_coverage": m.get("value_coverage", ""),
            "not_evaluable": "；".join(org["not_evaluable"]),
            "task_index": m.get("task_index"), "expected_task_index": m.get("expected_task_index"),
            "sa_lag": m.get("sa_lag"), "sa_lag_consistency": m.get("sa_lag_consistency"),
            "idle_fraction": m.get("idle_fraction"), "interaction_rate": m.get("interaction_rate"),
            "visual_diversity": m.get("visual_diversity"),
            "sync_evidence_index_coverage": ";".join(sync_idx),
            "sync_evidence_statistical_lag": ";".join(sync_lag),
            "repair_applied": "；".join(r["repair"]["applied"]),
            "repair_rejected": "；".join(r["repair"]["rejected"]),
            "repair_candidates": len(r["repair"].get("candidates", [])),
            "repaired_file": r["repaired_rel_path"],
            "final_status": fin["status"], "final_issue_codes": ";".join(fin["codes"]),
            "final_overall_score": _nan(fin["score"].get("overall_score")),
            "train_policy": g["policy"], "policy_before_dedup": g["policy_before_dedup"],
            "frame_valid": int(g["valid"].sum()), "train_frames": train_frames, "clip_count": len(g["clips"]),
            "dedup_lost_frames": g["dedup_lost_frames"],
            "dedup_covered_by": ",".join(f"ep{x}" for x in sorted(g["covered_by"])),
            "sample_weight": g["weight"], "processing_error": (r.get("error") or "").splitlines()[0] if r.get("error") else "",
            "seconds": r.get("seconds"),
        })
        fi = r["frame_index"]
        for i in range(n):
            codes = org["row_codes"][i]
            frame_rows.append({"episode_index": ep, "row": i, "frame_index": fi[i] if i < len(fi) else "",
                               "issue_codes": codes, "categories": ";".join(_cats(codes.split(";"))) if codes else "",
                               "streams": org["row_notes"][i], "train_keep": bool(g["train"][i])})
        for code in sorted({c for s in org["row_codes"] for c in s.split(";") if c}):
            mask = np.array([code in s.split(";") for s in org["row_codes"]])
            for a, b in _runs(mask):
                streams_hit = sorted({x.split("@", 1)[1] for i in range(a, b + 1) for x in org["row_notes"][i].split(";") if x.startswith(code + "@")})
                interval_rows.append({"episode_index": ep, "issue_code": code, "category": config.category(code),
                                      "evidence_tier": config.TIER_CN[config.tier(code)], "start_row": a, "end_row": b,
                                      "start_frame": fi[a] if a < len(fi) else "", "end_frame": fi[b] if b < len(fi) else "",
                                      "frames": b - a + 1, "streams": ",".join(streams_hit), "description": config.describe(code)})
        if n:
            src = "repaired" if r["repaired_rel_path"] else "original"
            mrows = [{"row": i, "frame_index": g["fi"][i] if i < len(g["fi"]) else "", "final_issue_codes": fin["row_codes"][i],
                      "frame_valid": bool(g["valid"][i]), "dedup_drop": bool(g["dedup_drop"][i]), "clip_id": g["clip_id"][i],
                      "train_keep": bool(g["train"][i]), "sample_weight": g["weight"]} for i in range(n)]
            _w(gov_dir / "quality_masks" / f"episode_{ep:06d}.csv", mrows)
            mask_index.append({"episode_index": ep, "mask_file": f"quality_masks/episode_{ep:06d}.csv", "read_from": src,
                               "source_file": ("governed/" + r["repaired_rel_path"]) if r["repaired_rel_path"] else r["rel_path"],
                               "frame_valid_rows": int(g["valid"].sum()), "train_keep_rows": train_frames, "rows": n})
        for a, b in g["clips"]:
            clips_all.append({"episode_index": ep, "clip_id": f"ep{ep:06d}_{a:05d}_{b:05d}", "start_row": a, "end_row": b,
                              "start_frame": g["fi"][a], "end_frame": g["fi"][b], "frames": b - a + 1,
                              "source": ("governed/" + r["repaired_rel_path"]) if r["repaired_rel_path"] else r["rel_path"],
                              "origin": _ORIGIN.get(g["policy_before_dedup"], "切段新增"), "sample_weight": g["weight"]})
        cand_rows += r["repair"].get("candidates", [])

    _w(rep_dir / "episode_report.csv", ep_rows)
    _w(rep_dir / "frame_flags.csv", frame_rows)
    _w(rep_dir / "issue_intervals.csv", interval_rows,
       ["episode_index", "issue_code", "category", "evidence_tier", "start_row", "end_row", "start_frame", "end_frame", "frames", "streams", "description"])
    _w(rep_dir / "duplicate_pairs.csv", pairs,
       ["episode_a", "episode_b", "shared_rows", "signed_rows_a", "signed_rows_b", "coverage_a", "coverage_b",
        "order_consistency", "kinematic_consistency", "same_task", "relation"])
    _w(rep_dir / "issue_code_dictionary.csv",
       [{"issue_code": c, "category": v[0], "evidence_tier": config.TIER_CN[v[1]], "scope": "轨迹级" if v[2] == "ep" else "帧级",
         "blocks_training": v[3], "description": v[4]} for c, v in config.CODES.items()])
    _w(gov_dir / "train_policy.csv", [{k: e[k] for k in ("episode_index", "file", "status", "train_policy", "policy_before_dedup", "frame_valid",
                                                             "train_frames", "clip_count", "dedup_lost_frames", "dedup_covered_by",
                                                             "sample_weight", "repaired_file", "final_status")} for e in ep_rows])
    _w(gov_dir / "clips.csv", clips_all, ["episode_index", "clip_id", "start_row", "end_row", "start_frame", "end_frame", "frames", "source", "origin", "sample_weight"])
    _w(gov_dir / "mask_index.csv", mask_index,
       ["episode_index", "mask_file", "read_from", "source_file", "frame_valid_rows", "train_keep_rows", "rows"])
    audit = [a for r in results for a in r["repair"]["audit"]]
    _w(gov_dir / "repair_audit.csv", audit, ["episode_index", "row", "field", "original_value", "repaired_value", "method", "repair_code"])
    _w(gov_dir / "repair_candidates.csv", cand_rows, ["episode_index", "issue_code", "proposed_action", "evidence", "not_applied_because"])
    manifest = [{"episode_index": r["episode_index"], "source_file": r["rel_path"], "source_sha256": r["source_sha256"],
                 "repaired_file": r["repaired_rel_path"], "repaired_sha256": r["repaired_sha256"],
                 "kind": "Parquet 副本" if r["repaired_rel_path"] else ("仅元数据补丁" if r["repair"]["meta_patch"] else "未修改"),
                 "applied": "；".join(r["repair"]["applied"]), "rejected": "；".join(r["repair"]["rejected"]),
                 "status_before": r["orig"]["status"], "status_after": r["final"]["status"],
                 "codes_before": ";".join(r["orig"]["codes"]), "codes_after": ";".join(r["final"]["codes"]),
                 "score_before": _nan(r["orig"]["score"].get("overall_score")), "score_after": _nan(r["final"]["score"].get("overall_score")),
                 "values_changed": sum(1 for a in r["repair"]["audit"] if a["row"] >= 0)}
                for r in results if r["repair"]["applied"] or r["repair"]["rejected"]]
    _w(gov_dir / "repair_manifest.csv", manifest)

    # governed meta: an OVERLAY describing the repaired copies only (not a loadable full dataset)
    repaired = [r for r in results if r["repaired_rel_path"]]
    meta_dir = gov_dir / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    with open(meta_dir / "episodes.jsonl", "w", encoding="utf-8") as f:
        for r in repaired:
            base = dict(ds.episodes_meta.get(r["episode_index"], {"episode_index": r["episode_index"]}))
            base["length"] = r["rows"]
            f.write(json.dumps(base, ensure_ascii=False) + "\n")
    if ds.tasks:
        with open(meta_dir / "tasks.jsonl", "w", encoding="utf-8") as f:
            for k in sorted(ds.tasks):
                f.write(json.dumps({"task_index": k, "task": ds.tasks[k]}, ensure_ascii=False) + "\n")
    info = dict(ds.info) if ds.info else {}
    info.update(total_episodes=len(repaired), total_frames=int(sum(r["rows"] for r in repaired)), splits={})
    info["refsync_overlay"] = {
        "is_overlay": True, "operator": config.VERSION,
        "episode_ids": [r["episode_index"] for r in repaired],
        "base_dataset": str(ds.root),
        "note": "仅包含修复副本，不是可直接加载的完整 LeRobot 数据集；训练请用 tools/load_governed.py 按 mask_index.csv/clips.csv 叠加读取",
    }
    (meta_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    _w(gov_dir / "meta_patches.csv", [{"episode_index": r["episode_index"], "field": "episodes.jsonl.length",
                                        "original_value": ds.episodes_meta.get(r["episode_index"], {}).get("length"),
                                        "repaired_value": r["repair"]["meta_patch"]["length"]}
                                       for r in results if r["repair"]["meta_patch"]],
       ["episode_index", "field", "original_value", "repaired_value"])

    # ---------------- summary ----------------
    N = len(results)
    readable = [r for r in results if r["rows"] > 0 and not ({"C_FILE_UNREADABLE", "C_PROCESSING_ERROR"} & set(r["orig"]["ep_codes"]))]
    frames = int(sum(r["rows"] for r in results))
    flagged = [e for e in ep_rows if e["status"] == "需复核"]
    value_only = [e for e in ep_rows if e["status"] == "价值提示"]
    by_cat = {c: sum(1 for r in results if any(config.category(x) == c for x in r["orig"]["codes"])) for c in config.CATEGORIES}
    by_code = {}
    for c in config.CODES:
        eps = sum(1 for r in results if c in r["orig"]["codes"])
        if eps:
            by_code[c] = {"episodes": eps, "frames": int(sum(1 for r in results for s in r["orig"]["row_codes"] if c in s.split(";"))),
                          "category": config.category(c), "tier": config.TIER_CN[config.tier(c)], "description": config.describe(c)}
    tiers = Counter(e["evidence_tier"] for e in flagged)
    flagged_frames = int(sum(e["flagged_frames"] for e in ep_rows))
    pol = Counter(g["policy"] for g in gov.values())
    frames_by_origin = Counter()
    for c in clips_all:
        frames_by_origin[c["origin"]] += c["frames"]
    scores = [e["overall_score"] for e in ep_rows if e["overall_score"] is not None]
    resolved = [m for m in manifest if m["status_before"] == "需复核" and m["status_after"] != "需复核"]
    # task coverage over the FULL declared task list (declared-but-absent tasks count as 0)
    declared = sorted(ds.tasks) if ds.tasks else sorted({e["task_index"] for e in ep_rows if e["task_index"] is not None})
    t_all = {k: 0 for k in declared}
    t_train_eps = {k: 0 for k in declared}
    t_train_frames = {k: 0 for k in declared}
    for e in ep_rows:
        k = e["task_index"]
        if k in t_all:
            t_all[k] += 1
            if e["train_frames"]:
                t_train_eps[k] += 1
                t_train_frames[k] += e["train_frames"]

    def _bal(d: dict[int, int]) -> tuple[Any, Any]:
        if len(d) < 2:
            return None, None
        v = np.array(list(d.values()), dtype=float)
        if v.sum() == 0:
            return None, None
        p = v / v.sum()
        ent = float(-(p[p > 0] * np.log2(p[p > 0])).sum() / math.log2(len(v)))
        return round(float(v.min() / v.max()), 4), round(ent, 4)

    bal_all, ent_all = _bal(t_all)
    bal_tr, ent_tr = _bal(t_train_frames)
    rel_counts = Counter(p["relation"] for p in pairs)
    sync_codes = {"索引/覆盖（确定性：path 帧号、缺图分布、索引字段）": [
                      "S_STREAM_SHIFTED", "S_IMAGE_PATH_MISMATCH", "S_SENSOR_LATE_START", "S_SENSOR_EARLY_STOP", "S_SENSOR_DROPOUT",
                      "S_STREAM_MISSING", "S_EPISODE_INDEX_INVALID", "S_EPISODE_INDEX_MISMATCH", "S_TASK_INDEX_INVALID",
                      "S_TASK_SWITCH_WITHIN_EPISODE", "S_TASK_META_MISMATCH"],
                  "统计时滞/停滞（阈值型：运动互相关、画面静止）": ["S_VISUAL_KINEMATIC_LAG", "S_CAMERA_LAG_SUSPECT", "S_STREAM_FROZEN"]}
    n_resolved_parquet = sum(1 for m in resolved if m["kind"] == "Parquet 副本")
    summary = {
        "operator": config.VERSION,
        "input": str(ds.root),
        "calibration": calib_source,
        "label_note": "无官方逐帧标签时，所有计数均为算法标记、需人工复核的结果，不等同于真实异常数或召回率；“未标记”不等于“确认正常”。",
        "scope": {"episodes": N, "readable_episodes": len(readable), "rows": frames,
                  "meta_total_frames": ds.info.get("total_frames"), "meta_total_episodes": ds.info.get("total_episodes"),
                  "cameras": ds.image_columns, "fps": ds.fps},
        "detection": {
            "flagged_episodes": len(flagged), "flagged_rate": round(len(flagged) / max(N, 1), 4),
            "value_hint_only_episodes": len(value_only),
            "unflagged_episodes": N - len(flagged) - len(value_only),
            "episodes_by_category": by_cat,
            "flagged_by_strongest_evidence": dict(tiers),
            "flagged_frames": flagged_frames, "flagged_frame_rate": round(flagged_frames / max(frames, 1), 4),
            "issue_intervals": len(interval_rows),
            "by_code": by_code,
            "sync_observability": {
                **{layer: {c: by_code[c]["episodes"] for c in codes if c in by_code} for layer, codes in sync_codes.items()},
                "硬件时钟同步": "不可评估：数据只有单一 timestamp，没有逐传感器独立硬件时间戳，无法实测时钟漂移",
            },
            "redundancy_relations": dict(rel_counts),
        },
        "scores": {"mean": round(float(np.mean(scores)), 2) if scores else None, "median": round(float(np.median(scores)), 2) if scores else None,
                   "min": round(float(np.min(scores)), 2) if scores else None,
                   "dimension_means": {d: (round(float(np.mean(v)), 2) if v else None) for d in ("structure", "temporal", "sync", "content", "value")
                                       for v in [[e[f"{d}_score"] for e in ep_rows if e[f"{d}_score"] is not None]]},
                   "value_not_evaluable_episodes": sum(1 for e in ep_rows if e["value_score"] is None),
                   "note": "每个问题码只计入一个维度；同一物理缺陷可能触发不同维度的多个问题码（多效应），不宣称因果去重。"},
        "governance": {
            "policy_counts": dict(pol),
            "parquet_repaired_copies": len(repaired),
            "meta_patch_only": sum(1 for m in manifest if m["kind"] == "仅元数据补丁"),
            "flagged_resolved": {"total": len(resolved), "by_parquet_copy": n_resolved_parquet, "by_meta_patch_only": len(resolved) - n_resolved_parquet},
            "values_changed": len([a for a in audit if a["row"] >= 0]),
            "repairs_rejected": sum(len(r["repair"]["rejected"]) for r in results),
            "repair_candidates": len(cand_rows),
            "frame_valid_rows": int(sum(int(g["valid"].sum()) for g in gov.values())),
            "train_frames": {"total": int(sum(frames_by_origin.values())), **dict(frames_by_origin)},
            "dedup_lost_frames": int(sum(g["dedup_lost_frames"] for g in gov.values())),
            "not_trainable_frames": int(frames - sum(frames_by_origin.values())),
            "min_clip_frames": min_clip,
            "denominators": "episodes=全部轨迹；train_frames=最终片段内帧；frame_valid=复检后不含阻断码的帧（含不足最短片段的散点，不可直接训练）",
        },
        "dataset_value": {
            "declared_tasks": len(declared),
            "task_episode_counts": {str(k): v for k, v in t_all.items()},
            "task_trainable_episodes": {str(k): v for k, v in t_train_eps.items()},
            "task_trainable_frames": {str(k): v for k, v in t_train_frames.items()},
            "task_balance_ratio_episodes": bal_all, "task_normalized_entropy_episodes": ent_all,
            "task_balance_ratio_trainable_frames": bal_tr, "task_normalized_entropy_trainable_frames": ent_tr,
            "equivalent_duplicate_pairs": [(p["episode_a"], p["episode_b"]) for p in pairs if p["relation"] == "整轨等价"],
            "note": "均衡度/熵基于 tasks.jsonl 全部声明任务（缺失任务计 0；少于 2 个任务为不可评估）；视觉多样性为 aHash 代理，不等于语义场景覆盖。",
        },
        "coverage_limits": [
            "硬件时间同步/时钟漂移：无独立逐传感器时间戳，不可评估（只报告索引覆盖与统计时滞）",
            "花屏/遮挡：无专门正样本评测，仅由黑白屏、解码、模糊、停滞规则间接覆盖",
            "任务成功率/碰撞等下游指标：未验证，数据价值为运动/视觉代理指标",
            "位置/夹爪边界为固定工程值（参考集观测范围见 thresholds_used.json 的 _reference_observed_ranges），不是学习包络或硬件实测",
        ],
    }
    summary["headline"] = (
        f"{N} 条轨迹：需复核 {len(flagged)} 条（{len(flagged) / max(N, 1):.1%}），仅价值提示 {len(value_only)} 条，"
        f"未被规则标记 {N - len(flagged) - len(value_only)} 条；帧级问题码命中 {flagged_frames}/{frames} 行；"
        f"Parquet 修复副本 {len(repaired)} 条 + 仅元数据补丁 {summary['governance']['meta_patch_only']} 条，"
        f"原需复核且治理后不再需复核 {len(resolved)} 条（Parquet {n_resolved_parquet} + 元数据 {len(resolved) - n_resolved_parquet}）；"
        f"最终训练片段帧 {int(sum(frames_by_origin.values()))}")
    summary["_tables"] = (ep_rows, interval_rows, by_code)
    return summary


def write_summary(summary: dict[str, Any], rep_dir: Path, gov_dir: Path) -> None:
    ep_rows, interval_rows, by_code = summary.pop("_tables", ([], [], {}))
    (rep_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _write_markdown(rep_dir / "summary.md", summary)
    _write_xlsx(rep_dir / "quality_report.xlsx", ep_rows, interval_rows, by_code, summary, gov_dir)


def _write_markdown(path: Path, s: dict[str, Any]) -> None:
    d, g, v = s["detection"], s["governance"], s["dataset_value"]
    lines = ["# RefSync-QA 检测与治理报告", "", f"- 算子版本：{s['operator']}", f"- 输入：{s['input']}", f"- 阈值来源：{s['calibration']}",
             f"- 阈值哈希：{s.get('calibration_provenance', {}).get('thresholds_sha256', '')}",
             f"- 说明：{s['label_note']}", "", "## 结论", "", s["headline"], "",
             "## 检测", "", "| 类别 | 轨迹数 |", "|---|---:|"]
    lines += [f"| {k} | {x} |" for k, x in d["episodes_by_category"].items()]
    lines += ["", "| 证据强度（需复核轨迹取最强） | 轨迹数 |", "|---|---:|"] + [f"| {k} | {x} |" for k, x in d["flagged_by_strongest_evidence"].items()]
    lines += ["", "| 问题码 | 类别 | 证据 | 轨迹 | 帧 | 说明 |", "|---|---|---|---:|---:|---|"]
    lines += [f"| {c} | {x['category']} | {x['tier']} | {x['episodes']} | {x['frames']} | {x['description']} |" for c, x in d["by_code"].items()]
    lines += ["", "### 同步证据分层", ""]
    for layer, val in d["sync_observability"].items():
        lines.append(f"- {layer}：{val}")
    if d["redundancy_relations"]:
        lines += ["", f"- 跨轨迹冗余关系：{d['redundancy_relations']}（详见 duplicate_pairs.csv；只剔除被直接覆盖的逐帧全模态重复）"]
    lines += ["", "## 治理", "", "| 训练策略 | 轨迹数 |", "|---|---:|"] + [f"| {k} | {x} |" for k, x in g["policy_counts"].items()]
    lines += ["", f"- Parquet 修复副本 {g['parquet_repaired_copies']} 条，仅元数据补丁 {g['meta_patch_only']} 条；治理后不再需复核 {g['flagged_resolved']}",
              f"- 改写单元格 {g['values_changed']} 处（全部见 repair_audit.csv，写后逐格校验）；拒绝修复 {g['repairs_rejected']} 项；"
              f"待人工决定的候选方案 {g['repair_candidates']} 项（repair_candidates.csv）",
              f"- 最终训练片段帧：{g['train_frames']}（片段最短 {g['min_clip_frames']} 帧，帧号连续）；去重剔除 {g['dedup_lost_frames']} 帧",
              f"- 口径：{g['denominators']}",
              "", "## 数据价值", "",
              f"- 声明任务 {v['declared_tasks']} 个；轨迹数 {v['task_episode_counts']}，可训练帧 {v['task_trainable_frames']}",
              f"- 均衡度（轨迹/可训练帧）{v['task_balance_ratio_episodes']}/{v['task_balance_ratio_trainable_frames']}，"
              f"归一化熵 {v['task_normalized_entropy_episodes']}/{v['task_normalized_entropy_trainable_frames']}",
              f"- 整轨等价重复对：{v['equivalent_duplicate_pairs']}", f"- 说明：{v['note']}",
              "", "## 未覆盖/不可评估", ""] + [f"- {x}" for x in s["coverage_limits"]]
    if s.get("processing_errors"):
        lines += ["", "## 处理异常", ""] + [f"- ep{e['episode_index']}: {e['error']}" for e in s["processing_errors"]]
    lines += ["", "## 效率（诊断日志，非独立基准）", "", f"- {s.get('timing')}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_xlsx(path: Path, ep_rows, interval_rows, by_code, summary, gov: Path) -> None:
    try:
        import pandas as pd
        import openpyxl  # noqa: F401
    except Exception:  # noqa: BLE001 - xlsx is optional
        return
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame([{"指标": k, "值": (json.dumps(v, ensure_ascii=False, default=str)[:32000] if isinstance(v, (dict, list)) else v)}
                      for k, v in summary.items()]).to_excel(xw, sheet_name="总览", index=False)
        pd.DataFrame(ep_rows).to_excel(xw, sheet_name="轨迹报告", index=False)
        pd.DataFrame([{"issue_code": k, **x} for k, x in by_code.items()]).to_excel(xw, sheet_name="问题码统计", index=False)
        pd.DataFrame(interval_rows).to_excel(xw, sheet_name="问题区间", index=False)
        for name, sheet in (("clips.csv", "训练片段"), ("repair_manifest.csv", "修复清单"), ("repair_audit.csv", "修复审计"),
                            ("repair_candidates.csv", "候选修复")):
            p = gov / name
            if p.exists() and p.stat().st_size > 10:
                try:
                    pd.read_csv(p, encoding="utf-8-sig").to_excel(xw, sheet_name=sheet, index=False)
                except Exception:  # noqa: BLE001
                    pass
