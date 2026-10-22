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
import sys
import time
import traceback


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
             flagged=sum(1 for x in o.get("row_codes", []) if x),
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
        return s

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
