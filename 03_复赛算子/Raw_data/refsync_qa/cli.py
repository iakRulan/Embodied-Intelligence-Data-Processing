# -*- coding: utf-8 -*-
"""Command-line entry.

    python run.py --input /data/test --output /data/result --reference /data/reference

Input/output can also come from environment variables (for platforms that
start operators without arguments): RSQA_INPUT / INPUT_DIR / DATA_DIR and
RSQA_OUTPUT / OUTPUT_DIR / RESULT_DIR; RSQA_REFERENCE for the reference set.

If --input contains several LeRobot datasets, a uniquely named reference,
clean or ref dataset is used for calibration. Legacy source-folder aliases
are also recognised. Use explicit --reference and English paths for deployment.
Every other dataset is processed into its own sub-folder of --output.

Exit codes: 0 ok; 2 bad input or refused input/output overlap; 3 (--strict)
at least one episode could not be processed (it is still reported as
C_PROCESSING_ERROR and every other episode is processed).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import VERSION

_REF_HINTS = ("参考集", "参考", "reference", "clean", "ref")
_SKIP = {"__MACOSX", ".git"}


def _env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def _lerobot_roots(p: Path) -> list[Path]:
    from .dataset import is_under, marked_dirs

    if (p / "meta" / "info.json").exists():
        return [p]
    marks = marked_dirs(p)
    roots = []
    for info in sorted(p.rglob("meta/info.json")):
        root = info.parent.parent
        if not (set(root.parts) & _SKIP) and not is_under(root, marks) and any((root / "data").rglob("*.parquet")):
            roots.append(root)
    return roots


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="refsync_qa", description=f"{VERSION} 具身多模态数据质量检测与安全治理算子")
    ap.add_argument("--input", "-i", default=_env("RSQA_INPUT", "INPUT_DIR", "DATA_DIR"),
                    help="待检测数据集目录（LeRobot v2.1：data/ + meta/）、任意含 parquet 的目录或单个 parquet")
    ap.add_argument("--output", "-o", default=_env("RSQA_OUTPUT", "OUTPUT_DIR", "RESULT_DIR"), help="输出目录")
    ap.add_argument("--reference", "-r", default=_env("RSQA_REFERENCE"), help="可选：正常参考集目录；提供时现场重新标定阈值")
    ap.add_argument("--calibration", default=None, help="可选：阈值 JSON（默认 calibration/default_thresholds.json）")
    ap.add_argument("--workers", "-w", type=int, default=int(_env("RSQA_WORKERS") or 0) or None, help="并行进程数（默认 min(8, CPU-1)）")
    ap.add_argument("--no-repair", action="store_true", help="只检测，不生成修复副本")
    ap.add_argument("--no-dedup", action="store_true", help="不做跨轨迹重复检测")
    ap.add_argument("--min-clip", type=int, default=None, help="训练片段最短帧数（默认 30）")
    ap.add_argument("--options", default=None, help="可选：JSON 文件，覆盖字段别名/20 维布局/fps 等配置")
    ap.add_argument("--limit", type=int, default=None, help="调试用：只处理前 N 条轨迹")
    ap.add_argument("--timestamp-policy", choices=["nominal", "conservative", "off"], default=None,
                    help="时间戳策略：conservative（默认）=单位换算/基准归零并保留残余抖动；nominal=显式按 frame_index/fps 规整（非实测时刻恢复）；off=不改")
    ap.add_argument("--clean-previous", action="store_true", help="删除（而非归档）同一输出目录中上次运行的 report/governed（仅当目录有输出标记）")
    ap.add_argument("--strict", action="store_true", help="任一轨迹处理异常时返回退出码 3（异常轨迹仍记录为 C_PROCESSING_ERROR）")
    a = ap.parse_args(argv)
    if not a.input or not a.output:
        ap.error("--input 与 --output 必填（或设置环境变量 RSQA_INPUT / RSQA_OUTPUT）")
    opts = {}
    if a.options:
        try:
            loaded = json.loads(Path(a.options).read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("options 必须是 JSON 对象")
            opts.update(loaded)
        except (OSError, ValueError) as exc:
            print(f"[error] {exc}", file=sys.stderr)
            return 2
    if a.min_clip is not None:
        opts["min_clip_frames"] = a.min_clip
    if a.no_dedup:
        opts["dedup"] = False
    if a.timestamp_policy:
        opts["timestamp_policy"] = a.timestamp_policy
    if a.clean_previous:
        opts["clean_previous"] = True
    from .pipeline import PathGuardError, run

    inp, out = Path(a.input), Path(a.output)
    if not inp.exists():
        print(f"[error] input not found: {inp}", file=sys.stderr)
        return 2
    targets = [(inp, out)]
    reference = a.reference
    if inp.is_dir():
        roots = _lerobot_roots(inp)
        if len(roots) > 1:
            refs = [r for r in roots if r.name.lower() in _REF_HINTS]
            if reference is None and len(refs) == 1:
                reference = str(refs[0])
                print(f"[auto] 使用 {refs[0]} 作为参考集标定阈值")
            targets = [(r, out / r.relative_to(inp)) for r in roots if reference is None or r.resolve() != Path(reference).resolve()]
    if not targets:
        print("[error] 没有待检测数据集", file=sys.stderr)
        return 2
    n_err = 0
    for src, dst in targets:
        try:
            summary = run(src, dst, reference_dir=reference, calibration=a.calibration, workers=a.workers,
                          repair=not a.no_repair, options=opts, limit=a.limit)
        except (PathGuardError, ValueError, OSError) as exc:
            print(f"[error] {exc}", file=sys.stderr)
            return 2
        n_err += len(summary.get("processing_errors", []))
    if n_err:
        print(f"[warn] {n_err} 条轨迹处理异常（已记录为 C_PROCESSING_ERROR，其余轨迹正常输出）", file=sys.stderr)
        if a.strict:
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
