# -*- coding: utf-8 -*-
"""RefSync-QA 决赛演示 · 运行器

直接调用 v3.2 算子的 pipeline.run()，算法代码零改动；只在主进程里"旁听"：
  - 每条轨迹在进程池里完成时，输出一条 episode 事件（状态、问题码、耗时、解码图像数）
  - 去重 / 治理与报告写出 / 汇总 三个阶段的起止时刻
所有事件以 JSON 行写到 stdout，由 server.py 转发给页面并存成 demo_events.jsonl（回放用）。

等价命令行：
  检测：python run.py --input <测试集> --output <输出> --no-repair
  治理：python run.py --input <测试集> --output <输出>
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import time
import traceback


def brief(orig: dict, repair: dict) -> dict:
    """给页面实时字幕用的一句话证据：哪路相机偏了几帧、修了还是拒修。"""
    met = orig.get("metrics") or {}
    out = {}
    m = re.search(r"(image|left_wrist_image|right_wrist_image):path偏移([+-]?\d+)",
                  str(met.get("sync_evidence_index_coverage") or ""))
    if m:
        out["shift"] = {"cam": m.group(1), "frames": int(m.group(2))}
    else:
        for cam, st in (met.get("streams") or {}).items():
            if isinstance(st, dict) and st.get("shift_frames"):
                out["shift"] = {"cam": cam, "frames": int(st["shift_frames"])}
                break
    lag = str(met.get("sync_evidence_statistical_lag") or "")
    if lag:
        out["lag"] = lag[:300]
    if repair.get("applied"):
        out["applied"] = "；".join(repair["applied"])[:400]
    if repair.get("rejected"):
        out["rejected"] = "；".join(repair["rejected"])[:600]
    return out


CAT_BIT = {"T": 1, "S": 2, "C": 4, "J": 4, "V": 8}  # 时序 / 同步 / 内容结构 / 数据价值


def cat_mask(row_codes: list) -> str:
    """逐行问题类别位掩码（每行一个十六进制字符，0 = 该行未命中），给页面实时铺逐帧全景用。"""
    out = []
    for s in row_codes or []:
        m = 0
        for c in (s.split(";") if s else []):
            m |= CAT_BIT.get(c[:1], 4)
        out.append(format(m, "x"))
    return "".join(out)


def row_runs(rows) -> list:
    """[[起, 止], ...]：被修复改写过的数据行，压成连续区间。"""
    xs = sorted({int(r) for r in rows if int(r) >= 0})
    out = []
    for x in xs:
        if out and x == out[-1][1] + 1:
            out[-1][1] = x
        else:
            out.append([x, x])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--operator", required=True, help="算子目录（含 refsync_qa/ 与 run.py）")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--stage", choices=["detect", "govern"], required=True)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--reference", default=None)
    a = ap.parse_args()

    t0 = time.perf_counter()

    def emit(**kw):
        kw["t"] = round(time.perf_counter() - t0, 3)
        sys.stdout.write(json.dumps(kw, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()

    sys.path.insert(0, a.operator)
    from refsync_qa import config, pipeline, report  # noqa: E402

    cpu = os.cpu_count() or 2
    workers = a.workers if a.workers else max(1, min(8, cpu - 1))
    workers = max(2, workers)  # 保证走进程池路径（逐条完成事件来自 as_completed）
    repair = a.stage == "govern"
    cli = f"python run.py --input {a.input} --output {a.output}" + ("" if repair else " --no-repair") + f" --workers {workers}"
    emit(type="start", stage=a.stage, operator=config.VERSION, workers=workers, cpu=cpu,
         platform=platform.platform(), python=sys.version.split()[0], cli=cli, repair=repair)

    # ---- 旁听：逐轨迹完成 ----
    orig_as_completed = pipeline.as_completed
    counter = {"k": 0, "images": 0, "rows": 0}
    emitted: set = set()

    def emit_episode(r, total):
        counter["k"] += 1
        counter["images"] += int(r.get("images_decoded", 0))
        counter["rows"] += int(r.get("rows", 0))
        emitted.add(r["episode_index"])
        o, fin, rep = r["orig"], r["final"], r.get("repair", {})
        emit(type="episode", k=counter["k"], total=total, ep=r["episode_index"], rows=r["rows"],
             images=r.get("images_decoded", 0), seconds=r.get("seconds"),
             status=o["status"], final_status=fin["status"],
             score=o["score"].get("overall_score"), final_score=fin["score"].get("overall_score"),
             codes=o["codes"], final_codes=fin["codes"],
             repaired=bool(r.get("repaired_rel_path")), meta_patch=bool(rep.get("meta_patch")),
             cells=len(rep.get("audit", [])), rejected=len(rep.get("rejected", [])),
             brief=brief(o, rep),
             mask=cat_mask(o.get("row_codes", [])), epcodes=sorted(o.get("ep_codes", {})),
             fmask=cat_mask(fin.get("row_codes", [])) if repair else "",
             fixruns=row_runs(a.get("row", -1) for a in rep.get("audit", [])),
             flagged=sum(1 for x in o.get("row_codes", []) if x),
             fflagged=sum(1 for x in fin.get("row_codes", []) if x),
             final_tier=fin.get("tier"), tier=o.get("tier"),
             error=bool(r.get("error")), images_total=counter["images"], rows_total=counter["rows"])

    def as_completed(fs, timeout=None):
        total = len(fs)
        for f in orig_as_completed(fs, timeout):
            try:
                emit_episode(f.result(), total)
            except Exception as exc:  # noqa: BLE001
                emit(type="episode_error", msg=f"{type(exc).__name__}: {exc}")
            yield f

    pipeline.as_completed = as_completed

    # ---- 旁听：数据集级阶段 ----
    orig_relations = pipeline.relations

    def relations(results, opts):
        # 只有一条轨迹时算子在主进程里直接处理（不经过进程池），在这里补发逐条事件
        for r in results:
            if r.get("episode_index") not in emitted:
                try:
                    emit_episode(r, len(results))
                except Exception as exc:  # noqa: BLE001
                    emit(type="episode_error", msg=f"{type(exc).__name__}: {exc}")
        emit(type="stage", stage="dedup", state="start")
        out = orig_relations(results, opts)
        emit(type="stage", stage="dedup", state="end", pairs=len(out))
        return out

    pipeline.relations = relations

    orig_build = report.build_outputs

    def build_outputs(*args, **kw):
        emit(type="stage", stage="outputs", state="start")
        s = orig_build(*args, **kw)
        emit(type="stage", stage="outputs", state="end")
        emit_final()
        return s

    def emit_final():
        """跨轨迹去重会再补问题码、改写状态；报告写出后按最终报告校正页面上的计数。"""
        try:
            import csv
            with open(os.path.join(a.output, "report", "episode_report.csv"), encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            st, fst = {}, {}
            for r in rows:
                st[r["status"]] = st.get(r["status"], 0) + 1
                fst[r["final_status"]] = fst.get(r["final_status"], 0) + 1
            emit(type="final", st=st, fst=fst, episodes=len(rows))
        except Exception as exc:  # noqa: BLE001
            emit(type="log", msg=f"[warn] 最终计数读取失败：{type(exc).__name__}")

    report.build_outputs = build_outputs

    orig_write = report.write_summary

    def write_summary(*args, **kw):
        emit(type="stage", stage="summary", state="start")
        s = orig_write(*args, **kw)
        emit(type="stage", stage="summary", state="end")
        return s

    report.write_summary = write_summary

    def log(msg):
        emit(type="log", msg=str(msg))

    try:
        emit(type="stage", stage="episodes", state="start")
        summary = pipeline.run(a.input, a.output, reference_dir=a.reference, workers=workers, repair=repair,
                               options={"clean_previous": True}, log=log)
    except Exception as exc:  # noqa: BLE001
        emit(type="error", msg=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc(limit=8))
        return 2
    emit(type="done", headline=summary.get("headline"), timing=summary.get("timing"),
         errors=len(summary.get("processing_errors", [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
