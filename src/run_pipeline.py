# -*- coding: utf-8 -*-
"""Parameterized CLI for RefSync-QA (复赛算子形态预留).

Examples
--------
    uv run python src/run_pipeline.py check
    uv run python src/run_pipeline.py score
    uv run python src/run_pipeline.py enrich
    uv run python src/run_pipeline.py detect --workers 16

``detect`` re-decodes every image and is the slow path. ``score`` / ``enrich``
reuse existing CSVs under --out.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dataset_paths import OUT_DIR, parquet_count, resolve_reference_root, resolve_test_root  # noqa: E402


def run_script(script: str, extra: list[str] | None = None) -> None:
    cmd = [sys.executable, str(SRC / script), *(extra or [])]
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(ROOT), check=True)


def cmd_check(args: argparse.Namespace) -> None:
    ref = Path(args.reference) if args.reference else resolve_reference_root()
    test = Path(args.test) if args.test else resolve_test_root()
    payload = {
        "reference_root": str(ref),
        "reference_parquet": parquet_count(ref),
        "test_root": str(test),
        "test_parquet": parquet_count(test),
        "out": str(Path(args.out).resolve()),
    }
    if payload["reference_parquet"] != 20:
        raise SystemExit(f"reference parquet count is {payload['reference_parquet']}, expected 20")
    if payload["test_parquet"] == 0:
        raise SystemExit("test parquet count is 0")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_score(_: argparse.Namespace) -> None:
    run_script("06_detect_and_score.py", ["--no-plots"])
    run_script("24_v22_channels.py")
    run_script("14_confidence_and_coverage.py")
    run_script("15_coverage_and_sensitivity.py")


def cmd_enrich(_: argparse.Namespace) -> None:
    run_script("24_v22_channels.py")
    run_script("14_confidence_and_coverage.py")
    run_script("15_coverage_and_sensitivity.py")


def cmd_detect(args: argparse.Namespace) -> None:
    ref = str(Path(args.reference) if args.reference else resolve_reference_root())
    test = str(Path(args.test) if args.test else resolve_test_root())
    run_script("03_profile_structural.py", ["--reference", ref, "--test", test])
    run_script(
        "21_parallel_image_scan.py",
        ["--dataset", "both", "--workers", str(args.workers)],
    )
    run_script("05_profile_values.py", ["--reference", ref, "--test", test])
    cmd_score(args)


def main() -> None:
    parser = argparse.ArgumentParser(description="RefSync-QA parameterized pipeline")
    parser.add_argument("--reference", type=Path, default=None, help="clean reference dataset root")
    parser.add_argument("--test", type=Path, default=None, help="test dataset root")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--workers", type=int, default=16)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="resolve dataset roots and assert parquet counts")
    sub.add_parser("score", help="rerun detector + v2.2 channels from existing CSVs")
    sub.add_parser("enrich", help="attach freeze overlay and IsolationForest only")
    sub.add_parser("detect", help="full profile + score (slow: image decode)")
    args = parser.parse_args()
    Path(args.out).mkdir(parents=True, exist_ok=True)
    {"check": cmd_check, "score": cmd_score, "enrich": cmd_enrich, "detect": cmd_detect}[args.command](args)


if __name__ == "__main__":
    main()
