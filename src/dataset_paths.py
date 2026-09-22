# -*- coding: utf-8 -*-
"""Single source of truth for dataset roots and pipeline IO directories.

Layout notes (updated 2026-09-02):

- The freshly extracted official package ``初赛数据/{参考集,测试集}`` is the
  canonical read-only input. It was verified byte-identical (per-file SHA-1
  over all 89 test parquet + 3 meta files) to the ``analysis_data/test_dataset``
  working copy used by the v2.2 runs.
- Since the 2026-08-31 workspace reorganization, pipeline artifacts live under
  ``02_过程数据``: tables under ``02_分析输出/outputs``, figures under
  ``02_分析输出/figures``, and analysis/repaired/governed data under
  ``03_数据处理阶段``. Scripts no longer hardcode ROOT-level artifact paths.
- When ``02_过程数据`` is absent (pre-reorg layout), roots fall back to the
  workspace root so historical checkouts keep working.

Historical note: the v2.1 resolver used ``next(ROOT.rglob("data/chunk-000"))``
and only skipped ``analysis_data``. After ``repaired_data/`` and
``governed_data/`` appeared, that glob could resolve to the workspace root
(0 parquet files) and every reference-calibrated threshold would silently
collapse.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

OFFICIAL_ROOT = ROOT / "具身智能多模态数据质量检测算法赛题-初赛数据" / "初赛数据"
CANONICAL_REFERENCE = OFFICIAL_ROOT / "参考集"
CANONICAL_TEST = OFFICIAL_ROOT / "测试集"

_PROCESS_ROOT_CANDIDATE = ROOT / "02_过程数据"
PROCESS_ROOT = _PROCESS_ROOT_CANDIDATE if _PROCESS_ROOT_CANDIDATE.is_dir() else ROOT
ANALYSIS_OUTPUT_ROOT = PROCESS_ROOT / "02_分析输出"
_DATA_STAGE_CANDIDATE = PROCESS_ROOT / "03_数据处理阶段"
DATA_STAGE_ROOT = _DATA_STAGE_CANDIDATE if _DATA_STAGE_CANDIDATE.is_dir() else PROCESS_ROOT

OUT_DIR = ANALYSIS_OUTPUT_ROOT / "outputs"
FIG_DIR = ANALYSIS_OUTPUT_ROOT / "figures"
ANALYSIS_DATA_ROOT = DATA_STAGE_ROOT / "analysis_data"
REPAIRED_ROOT = DATA_STAGE_ROOT / "repaired_data"
GOV_ROOT = DATA_STAGE_ROOT / "governed_data"

EXPECTED_REFERENCE_EPISODES = 20
EXPECTED_TEST_EPISODES = 89
_EXCLUDE_PARTS = {
    "__MACOSX",
    "analysis_data",
    "repaired_data",
    "governed_data",
    "_archive_v1",
    "submission_ready",
}


def parquet_count(root: Path) -> int:
    if not root.exists():
        return 0
    return len(list(root.rglob("data/chunk-*/episode_*.parquet")))


def resolve_reference_root() -> Path:
    """Return the dataset root that contains exactly 20 official reference parquet files.

    Raises FileNotFoundError / RuntimeError instead of pointing at an empty directory.
    """
    candidates: list[tuple[int, Path]] = []
    for chunk in ROOT.rglob("data/chunk-000"):
        if not chunk.is_dir():
            continue
        if set(chunk.parts) & _EXCLUDE_PARTS:
            continue
        n = len(list(chunk.glob("episode_*.parquet")))
        if n == 0:
            continue
        dataset_root = chunk.parents[1]  # chunk-000 -> data -> dataset root
        candidates.append((n, dataset_root))

    if not candidates:
        raise FileNotFoundError(
            f"Cannot locate the {EXPECTED_REFERENCE_EPISODES}-episode clean reference set under {ROOT}. "
            "Expected a directory containing data/chunk-000/episode_*.parquet "
            "outside analysis_data / repaired_data / governed_data."
        )

    def _key(item: tuple[int, Path]) -> tuple[int, int, int]:
        n, path = item
        text = str(path)
        return (
            int(n == EXPECTED_REFERENCE_EPISODES),
            int("初赛数据" in text),
            n,
        )

    n, root = max(candidates, key=_key)
    found = parquet_count(root)
    if found != EXPECTED_REFERENCE_EPISODES:
        listing = "; ".join(f"{path} ({count} parquet)" for count, path in candidates)
        raise RuntimeError(
            f"Reference set at {root} has {found} parquet files, expected {EXPECTED_REFERENCE_EPISODES}. "
            f"Candidates: {listing}"
        )
    return root


def resolve_test_root() -> Path:
    """Return the canonical freshly-extracted test set (初赛数据/测试集).

    Falls back to the legacy ``analysis_data/test_dataset`` working copy only
    when the canonical extraction is absent. Raises instead of silently
    returning an empty directory.
    """
    n = parquet_count(CANONICAL_TEST)
    if n:
        if n != EXPECTED_TEST_EPISODES:
            raise RuntimeError(
                f"Canonical test set at {CANONICAL_TEST} has {n} parquet files, "
                f"expected {EXPECTED_TEST_EPISODES}."
            )
        return CANONICAL_TEST

    legacy = ANALYSIS_DATA_ROOT / "test_dataset"
    n = parquet_count(legacy)
    if n:
        return legacy
    raise FileNotFoundError(
        f"Cannot locate the {EXPECTED_TEST_EPISODES}-episode test set. Expected it at "
        f"{CANONICAL_TEST} (or a legacy copy at {legacy})."
    )


DEFAULT_TEST = resolve_test_root()
