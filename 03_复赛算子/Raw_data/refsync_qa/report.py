# -*- coding: utf-8 -*-
"""Governance decisions and all output files (one consistent source: the packed results)."""
from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from . import config
from .detect import _runs


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


def _dup_groups(pairs: list[dict[str, Any]]) -> list[set[int]]:
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for p in pairs:
        if p["relation"] == "重复":
            parent[find(p["episode_a"])] = find(p["episode_b"])
    groups = defaultdict(set)
    for x in list(parent):
        groups[find(x)].add(x)
    return [g for g in groups.values() if len(g) > 1]


def build_outputs(results, dup_pairs, ds, thr, opts, out: Path, gov: Path, calib_source: str, timings: dict[str, Any]) -> dict[str, Any]:
    min_clip = int(opts["min_clip_frames"])
    rep_dir = out / "report"

    # ---------------- per-episode governance ----------------
    gov_rows: dict[int, dict[str, Any]] = {}
    clips_all: list[dict[str, Any]] = []
    for r in results:
        ep, n, fin, org = r["episode_index"], r["rows"], r["final"], r["orig"]
        keep = ~np.asarray(fin["blocked"], dtype=bool) if n else np.zeros(0, bool)
        fi = r.get("frame_index_final", r["frame_index"])
        repaired = bool(r["repaired_rel_path"] or r["repair"]["meta_patch"])
        fatal = {"C_FILE_UNREADABLE", "C_EMPTY_EPISODE", "C_SCHEMA_MISSING_FIELD"} & set(fin["ep_codes"])
        clips = [] if fatal else _clips_local(keep, fi, min_clip)
        if fatal:
            policy, keep = "文件不可用", np.zeros(n, bool)
        elif n and keep.all() and not _defects(org["codes"]):
            policy = "整段可用"
        elif n and keep.all() and repaired:
            policy = "修复后整段可用"
        elif n and keep.all():
            policy = "整段可用"
        elif clips:
            policy = "部分修复+切段使用" if repaired else "切段使用"
        else:
            policy = "隔离回采"
        low_value = any(config.category(c) == config.CAT_V and c not in ("V_EPISODE_OVERLAP", "V_EPISODE_DUPLICATE") for c in fin["codes"])
        gov_rows[ep] = {"policy": policy, "policy_before_dedup": policy, "keep": keep, "clips": clips, "repaired": repaired,
                        "weight": opts["low_value_sample_weight"] if low_value else 1.0, "fi": fi,
                        "final_score": fin["score"].get("overall_score") or 0.0}

    # duplicates: keep the copy with most usable frames, drop the rest
    dup_note: dict[int, str] = {}
    for g in _dup_groups(dup_pairs):
        def usable(e: int) -> tuple:
            gr = gov_rows[e]
            frames = int(sum(b - a + 1 for a, b in gr["clips"])) if gr["policy"] not in ("文件不可用",) else -1
            return (frames, gr["final_score"], -e)
        keep_ep = max(sorted(g), key=usable)
        for e in g:
            if e != keep_ep and gov_rows[e]["policy"] != "文件不可用":
                gov_rows[e].update(policy="重复剔除", keep=np.zeros(len(gov_rows[e]["keep"]), bool), clips=[])
                dup_note[e] = f"与 ep{keep_ep} 重复，保留 ep{keep_ep}"
            elif e == keep_ep:
                dup_note[e] = "重复组保留副本: " + ",".join(f"ep{x}" for x in sorted(g) if x != e)

    # ---------------- tables ----------------
    ep_rows, frame_rows, interval_rows, mask_index = [], [], [], []
    for r in results:
        ep, n, org, fin = r["episode_index"], r["rows"], r["orig"], r["final"]
        gr = gov_rows[ep]
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
        clip_frames = int(sum(b - a + 1 for a, b in gr["clips"]))
        ep_rows.append({
            "episode_index": ep, "file": r["rel_path"], "rows": n,
            "status": org["status"], "evidence_tier": org["tier"],
            "defect_count": len(_defects(org["codes"])),
            "categories": ";".join(_cats(org["codes"])),
            "issue_codes": ";".join(org["codes"]),
            "issue_details": "；".join(details),
            "flagged_frames": int(sum(1 for s in org["row_codes"] if s)),
            **{k: org["score"].get(k) for k in ("structure_score", "temporal_score", "sync_score", "content_score", "value_score", "overall_score", "score_gate")},
            "not_evaluable": "；".join(org["not_evaluable"]),
            "task_index": m.get("task_index"), "expected_task_index": m.get("expected_task_index"),
            "sa_lag": m.get("sa_lag"), "sa_lag_consistency": m.get("sa_lag_consistency"),
            "idle_fraction": m.get("idle_fraction"), "interaction_rate": m.get("interaction_rate"),
            "visual_diversity": m.get("visual_diversity"),
            "stream_shift": ";".join(f"{k}:{v['shift_frames']:+d}" for k, v in streams.items() if v.get("shift_frames")),
            "visual_kinematic_lag": ";".join(f"{k}:{v['vk_best_lag']:+d}(r={v['vk_r']})" for k, v in streams.items() if "vk_best_lag" in v),
            "coverage_gaps": ";".join(f"{k}:{'/'.join(v['gaps'])}" for k, v in streams.items() if v.get("gaps")),
            "repair_applied": "；".join(r["repair"]["applied"]),
            "repair_rejected": "；".join(r["repair"]["rejected"]),
            "repaired_file": r["repaired_rel_path"],
            "final_status": fin["status"], "final_issue_codes": ";".join(fin["codes"]),
            "final_overall_score": fin["score"].get("overall_score"),
            "train_policy": gr["policy"], "policy_before_dedup": gr["policy_before_dedup"], "train_frames": clip_frames, "clip_count": len(gr["clips"]),
            "sample_weight": gr["weight"], "duplicate_note": dup_note.get(ep, ""),
            "seconds": r.get("seconds"),
        })
        fi = r["frame_index"]
        for i in range(n):
            codes = org["row_codes"][i]
            frame_rows.append({"episode_index": ep, "row": i, "frame_index": fi[i] if i < len(fi) else "",
                               "issue_codes": codes, "categories": ";".join(_cats(codes.split(";"))) if codes else "",
                               "streams": org["row_notes"][i], "train_keep": bool(gr["keep"][i]) if i < len(gr["keep"]) else False})
        for code in sorted({c for s in org["row_codes"] for c in s.split(";") if c}):
            mask = np.array([code in s.split(";") for s in org["row_codes"]])
            for a, b in _runs(mask):
                streams_hit = sorted({x.split("@", 1)[1] for i in range(a, b + 1) for x in org["row_notes"][i].split(";") if x.startswith(code + "@")})
                interval_rows.append({"episode_index": ep, "issue_code": code, "category": config.category(code),
                                      "evidence_tier": config.TIER_CN[config.tier(code)], "start_row": a, "end_row": b,
                                      "start_frame": fi[a] if a < len(fi) else "", "end_frame": fi[b] if b < len(fi) else "",
                                      "frames": b - a + 1, "streams": ",".join(streams_hit), "description": config.describe(code)})
        # governed mask (rows of the copy that training should read)
        if n:
            src = "repaired" if r["repaired_rel_path"] else "original"
            mrows = [{"row": i, "frame_index": gr["fi"][i] if i < len(gr["fi"]) else "", "final_issue_codes": fin["row_codes"][i],
                      "train_keep": bool(gr["keep"][i]), "sample_weight": gr["weight"]} for i in range(n)]
            _w(gov / "quality_masks" / f"episode_{ep:06d}.csv", mrows)
            mask_index.append({"episode_index": ep, "mask_file": f"quality_masks/episode_{ep:06d}.csv", "read_from": src,
                               "keep_rows": int(gr["keep"].sum()), "drop_rows": int(n - gr["keep"].sum())})
        for a, b in gr["clips"]:
            origin = "原本可用" if gr["policy"] == "整段可用" else "修复恢复" if gr["policy"] == "修复后整段可用" else "切段新增"
            clips_all.append({"episode_index": ep, "clip_id": f"ep{ep:06d}_{a:05d}_{b:05d}", "start_row": a, "end_row": b,
                              "start_frame": gr["fi"][a], "end_frame": gr["fi"][b], "frames": b - a + 1,
                              "source": ("governed/" + r["repaired_rel_path"]) if r["repaired_rel_path"] else r["rel_path"],
                              "origin": origin, "sample_weight": gr["weight"]})

    _w(rep_dir / "episode_report.csv", ep_rows)
    _w(rep_dir / "frame_flags.csv", frame_rows)
    _w(rep_dir / "issue_intervals.csv", interval_rows,
       ["episode_index", "issue_code", "category", "evidence_tier", "start_row", "end_row", "start_frame", "end_frame", "frames", "streams", "description"])
    _w(rep_dir / "duplicate_pairs.csv", dup_pairs,
       ["episode_a", "episode_b", "shared_images", "images_a", "images_b", "overlap", "relation"])
    _w(rep_dir / "issue_code_dictionary.csv",
       [{"issue_code": c, "category": v[0], "evidence_tier": config.TIER_CN[v[1]], "scope": "轨迹级" if v[2] == "ep" else "帧级",
         "blocks_training": v[3], "description": v[4]} for c, v in config.CODES.items()])
    _w(gov / "train_policy.csv", [{k: e[k] for k in ("episode_index", "file", "status", "train_policy", "train_frames", "clip_count",
                                                         "sample_weight", "repaired_file", "final_status", "duplicate_note")} for e in ep_rows])
    _w(gov / "clips.csv", clips_all, ["episode_index", "clip_id", "start_row", "end_row", "start_frame", "end_frame", "frames", "source", "origin", "sample_weight"])
    _w(gov / "mask_index.csv", mask_index)
    audit = [a for r in results for a in r["repair"]["audit"]]
    _w(gov / "repair_audit.csv", audit, ["episode_index", "row", "field", "original_value", "repaired_value", "method", "repair_code"])
    manifest = [{"episode_index": r["episode_index"], "source_file": r["rel_path"], "source_sha256": r["source_sha256"],
                 "repaired_file": r["repaired_rel_path"], "repaired_sha256": r["repaired_sha256"],
                 "applied": "；".join(r["repair"]["applied"]), "rejected": "；".join(r["repair"]["rejected"]),
                 "status_before": r["orig"]["status"], "status_after": r["final"]["status"],
                 "codes_before": ";".join(r["orig"]["codes"]), "codes_after": ";".join(r["final"]["codes"]),
                 "score_before": r["orig"]["score"].get("overall_score"), "score_after": r["final"]["score"].get("overall_score"),
                 "values_changed": sum(1 for a in r["repair"]["audit"] if a["row"] >= 0)}
                for r in results if r["repair"]["applied"] or r["repair"]["rejected"]]
    _w(gov / "repair_manifest.csv", manifest)

    # governed meta: describes the repaired copies only (+ patches for the whole set)
    repaired = [r for r in results if r["repaired_rel_path"]]
    meta_dir = gov / "meta"
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
    info.update(total_episodes=len(repaired), total_frames=int(sum(r["rows"] for r in repaired)),
                splits={"governed_repaired_copies": f"0:{len(repaired)}"})
    (meta_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    _w(gov / "meta_patches.csv", [{"episode_index": r["episode_index"], "field": "episodes.jsonl.length",
                                    "original_value": ds.episodes_meta.get(r["episode_index"], {}).get("length"),
                                    "repaired_value": r["repair"]["meta_patch"]["length"]}
                                   for r in results if r["repair"]["meta_patch"]],
       ["episode_index", "field", "original_value", "repaired_value"])

    # ---------------- summary ----------------
    N = len(results)
    readable = [r for r in results if r["rows"] > 0 and "C_FILE_UNREADABLE" not in r["orig"]["ep_codes"]]
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
    pol = Counter(g["policy"] for g in gov_rows.values())
    frames_by_origin = Counter()
    for c in clips_all:
        frames_by_origin[c["origin"]] += c["frames"]
    scores = [e["overall_score"] for e in ep_rows if e["overall_score"] is not None]
    fixed_full = [m for m in manifest if m["status_before"] == "需复核" and m["status_after"] != "需复核"]
    task_counts = Counter(e["task_index"] for e in ep_rows if e["task_index"] is not None and (not ds.tasks or e["task_index"] in ds.tasks))
    tc = np.array(list(task_counts.values()), dtype=float)
    balance = float(tc.min() / tc.max()) if len(tc) > 1 else 1.0
    entropy = float(-(tc / tc.sum() * np.log2(tc / tc.sum())).sum() / math.log2(len(tc))) if len(tc) > 1 else 1.0
    n_images = int(sum(len(r["orig"]["row_codes"]) for r in readable)) * max(1, len(ds.image_columns))
    total_s = timings["total_seconds"]
    summary = {
        "operator": config.VERSION,
        "input": str(ds.root),
        "calibration": calib_source,
        "label_note": "无官方逐帧标签时，所有计数均为算法标记、需人工复核的结果，不等同于真实异常数或召回率。",
        "scope": {"episodes": N, "readable_episodes": len(readable), "rows": frames,
                  "meta_total_frames": ds.info.get("total_frames"), "meta_total_episodes": ds.info.get("total_episodes"),
                  "cameras": ds.image_columns, "fps": ds.fps},
        "detection": {
            "flagged_episodes": len(flagged), "flagged_rate": round(len(flagged) / max(N, 1), 4),
            "value_hint_only_episodes": len(value_only),
            "pass_episodes": N - len(flagged) - len(value_only),
            "episodes_by_category": by_cat,
            "flagged_by_strongest_evidence": dict(tiers),
            "flagged_frames": flagged_frames, "flagged_frame_rate": round(flagged_frames / max(frames, 1), 4),
            "issue_intervals": len(interval_rows),
            "by_code": by_code,
            "duplicate_pairs": [p for p in dup_pairs if p["relation"] == "重复"],
            "overlap_pairs": [p for p in dup_pairs if p["relation"] != "重复"],
        },
        "scores": {"mean": round(float(np.mean(scores)), 2) if scores else None, "median": round(float(np.median(scores)), 2) if scores else None,
                   "min": round(float(np.min(scores)), 2) if scores else None,
                   "dimension_means": {d: round(float(np.nanmean([e[f"{d}_score"] if e[f"{d}_score"] is not None else np.nan for e in ep_rows])), 2)
                                       for d in ("structure", "temporal", "sync", "content", "value")}},
        "governance": {
            "policy_counts": dict(pol),
            "repaired_copies": len(repaired),
            "flagged_resolved_by_repair": len(fixed_full),
            "values_changed": len([a for a in audit if a["row"] >= 0]),
            "repairs_rejected": sum(len(r["repair"]["rejected"]) for r in results),
            "usable_frames": {"total": int(sum(frames_by_origin.values())), **dict(frames_by_origin)},
            "blocked_or_dropped_frames": int(frames - sum(frames_by_origin.values())),
            "min_clip_frames": min_clip,
        },
        "dataset_value": {"task_counts": {str(k): v for k, v in sorted(task_counts.items())}, "task_balance_ratio": round(balance, 4),
                          "task_normalized_entropy": round(entropy, 4),
                          "duplicate_groups": [sorted(g) for g in _dup_groups(dup_pairs)]},
        "timing": {**timings, "episodes_per_second": round(N / total_s, 3) if total_s else None,
                   "images_per_second": round(n_images / total_s, 1) if total_s else None},
    }
    summary["headline"] = (f"{N} 条轨迹：需复核 {len(flagged)} 条（{len(flagged) / max(N, 1):.1%}），"
                           f"仅价值提示 {len(value_only)} 条；逐帧标记 {flagged_frames}/{frames}；"
                           f"修复副本 {len(repaired)} 条，其中 {len(fixed_full)} 条复检通过；"
                           f"可训练帧 {int(sum(frames_by_origin.values()))}")
    (rep_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _write_markdown(rep_dir / "summary.md", summary)
    _write_xlsx(rep_dir / "quality_report.xlsx", ep_rows, interval_rows, by_code, summary, gov)
    return summary


def _clips_local(keep, fi, min_len):
    from .pipeline import _clips
    return _clips(keep, fi, min_len)


def _write_markdown(path: Path, s: dict[str, Any]) -> None:
    d, g = s["detection"], s["governance"]
    lines = ["# RefSync-QA 检测与治理报告", "", f"- 算子版本：{s['operator']}", f"- 输入：{s['input']}", f"- 阈值来源：{s['calibration']}",
             f"- 说明：{s['label_note']}", "", "## 结论", "", s["headline"], "",
             "## 检测", "", "| 类别 | 轨迹数 |", "|---|---:|"]
    lines += [f"| {k} | {v} |" for k, v in d["episodes_by_category"].items()]
    lines += ["", "| 证据强度（需复核轨迹取最强） | 轨迹数 |", "|---|---:|"] + [f"| {k} | {v} |" for k, v in d["flagged_by_strongest_evidence"].items()]
    lines += ["", "| 问题码 | 类别 | 证据 | 轨迹 | 帧 | 说明 |", "|---|---|---|---:|---:|---|"]
    lines += [f"| {c} | {v['category']} | {v['tier']} | {v['episodes']} | {v['frames']} | {v['description']} |" for c, v in d["by_code"].items()]
    if d["duplicate_pairs"]:
        lines += ["", "重复轨迹对：" + "；".join(f"ep{p['episode_a']}–ep{p['episode_b']}（{p['overlap']:.0%}）" for p in d["duplicate_pairs"])]
    lines += ["", "## 治理", "", "| 训练策略 | 轨迹数 |", "|---|---:|"] + [f"| {k} | {v} |" for k, v in g["policy_counts"].items()]
    lines += ["", f"- 修复副本 {g['repaired_copies']} 条，复检后不再需复核 {g['flagged_resolved_by_repair']} 条；改写数值/字段 {g['values_changed']} 处；拒绝修复 {g['repairs_rejected']} 项",
              f"- 可训练帧：{g['usable_frames']}（片段最短 {g['min_clip_frames']} 帧，帧号连续）",
              "", "## 数据价值", "", f"- 任务分布：{s['dataset_value']['task_counts']}，均衡度 {s['dataset_value']['task_balance_ratio']}，归一化熵 {s['dataset_value']['task_normalized_entropy']}",
              f"- 重复组：{s['dataset_value']['duplicate_groups']}", "", "## 效率", "", f"- {s['timing']}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_xlsx(path: Path, ep_rows, interval_rows, by_code, summary, gov: Path) -> None:
    try:
        import pandas as pd
        import openpyxl  # noqa: F401
    except Exception:  # noqa: BLE001 - xlsx is optional
        return
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame([{"指标": k, "值": (json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v) if not isinstance(v, (dict, list)) or len(json.dumps(v, ensure_ascii=False, default=str)) < 32000 else "见 summary.json"}
                      for k, v in summary.items()]).to_excel(xw, sheet_name="总览", index=False)
        pd.DataFrame(ep_rows).to_excel(xw, sheet_name="轨迹报告", index=False)
        pd.DataFrame([{"issue_code": k, **v} for k, v in by_code.items()]).to_excel(xw, sheet_name="问题码统计", index=False)
        pd.DataFrame(interval_rows).to_excel(xw, sheet_name="问题区间", index=False)
        for name, sheet in (("clips.csv", "训练片段"), ("repair_manifest.csv", "修复清单"), ("repair_audit.csv", "修复审计")):
            p = gov / name
            if p.exists() and p.stat().st_size > 10:
                pd.read_csv(p, encoding="utf-8-sig").to_excel(xw, sheet_name=sheet, index=False)
