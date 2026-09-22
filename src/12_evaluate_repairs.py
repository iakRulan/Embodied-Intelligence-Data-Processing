"""Re-profile repaired copies and produce measured before/after evidence."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
from dataset_paths import OUT_DIR, REPAIRED_ROOT, resolve_test_root

SOURCE_ROOT = resolve_test_root()


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STRUCT = load_module("profile_structural", ROOT / "src" / "03_profile_structural.py")
IMAGE = load_module("profile_images", ROOT / "src" / "04_profile_images.py")
VALUES = load_module("profile_values", ROOT / "src" / "05_profile_values.py")
DETECT = load_module("detect_score", ROOT / "src" / "06_detect_and_score.py")


def read_meta(root: Path) -> dict[int, dict[str, Any]]:
    result = {}
    for line in (root / "meta" / "episodes.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            result[int(row["episode_index"])] = row
    return result


def profile(root: Path, label: str, episode_ids: list[int], meta: dict[int, dict[str, Any]]) -> tuple[pd.DataFrame, pd.DataFrame]:
    structural_eps: list[dict[str, Any]] = []
    structural_frames: list[dict[str, Any]] = []
    image_eps: list[dict[str, Any]] = []
    image_frames: list[dict[str, Any]] = []
    value_eps: list[dict[str, Any]] = []
    value_frames: list[dict[str, Any]] = []
    for ep in episode_ids:
        path = root / "data" / "chunk-000" / f"episode_{ep:06d}.parquet"
        se, sf = STRUCT.profile_episode(path, meta.get(ep), 0.1)
        ie, imf = IMAGE.profile_episode(path)
        vf, ve = VALUES.profile_episode(path)
        for record in [se, ie, ve]:
            record["read_ok"] = True
            record["read_error"] = ""
            record["source_file"] = str(path)
            record["dataset"] = label
        structural_eps.append(se)
        image_eps.append(ie)
        value_eps.append(ve)
        structural_frames.extend(sf)
        image_frames.extend(imf)
        value_frames.extend(vf)
    s = pd.DataFrame(structural_eps)
    i = pd.DataFrame(image_eps)
    v = pd.DataFrame(value_eps)
    sf = pd.DataFrame(structural_frames)
    imf = pd.DataFrame(image_frames)
    vf = pd.DataFrame(value_frames)
    thresholds = DETECT.reference_thresholds(
        pd.read_csv(OUT_DIR / "reference_image_episodes.csv", encoding="utf-8-sig"),
        pd.read_csv(OUT_DIR / "reference_value_episodes.csv", encoding="utf-8-sig"),
        pd.read_csv(OUT_DIR / "reference_value_frames.csv", encoding="utf-8-sig"),
        pd.read_csv(OUT_DIR / "reference_image_frames.csv", encoding="utf-8-sig", low_memory=False),
    )
    frames = DETECT.build_frame_flags(sf, imf, vf, thresholds)
    report = DETECT.episode_report(s, i, v, frames, thresholds, label)
    return report, frames


def main() -> None:
    manifest = pd.read_csv(REPAIRED_ROOT / "repair_manifest.csv", encoding="utf-8-sig")
    episode_ids = manifest.episode_index.astype(int).tolist()
    source_meta = read_meta(SOURCE_ROOT)
    repaired_meta = read_meta(REPAIRED_ROOT)
    before, before_frames = profile(SOURCE_ROOT, "before", episode_ids, source_meta)
    after, after_frames = profile(REPAIRED_ROOT, "after", episode_ids, repaired_meta)
    selected = [
        "episode_index", "status", "issue_codes", "issue_categories", "overall_quality_score",
        "structural_score", "temporal_score", "sync_score", "content_score", "value_score",
    ]
    merged = before[selected].merge(after[selected], on="episode_index", suffixes=("_before", "_after"))
    merged = merged.merge(manifest[["episode_index", "repair_codes", "changed_rows", "source_sha256", "repaired_sha256"]], on="episode_index", how="left")
    merged["score_gain_measured"] = merged.overall_quality_score_after - merged.overall_quality_score_before
    merged["targeted_codes_removed"] = merged.apply(
        lambda row: all(code not in str(row.issue_codes_after) for code in str(row.repair_codes).split(";") if code), axis=1
    )
    merged["passed_full_recheck"] = merged.status_after.eq("通过")

    before_counts = before_frames.assign(flagged=before_frames.issue_codes.fillna("").ne("")).groupby("episode_index").flagged.sum()
    after_counts = after_frames.assign(flagged=after_frames.issue_codes.fillna("").ne("")).groupby("episode_index").flagged.sum()
    merged["flagged_frames_before"] = merged.episode_index.map(before_counts).fillna(0).astype(int)
    merged["flagged_frames_after"] = merged.episode_index.map(after_counts).fillna(0).astype(int)
    merged.to_csv(OUT_DIR / "repair_validation_report.csv", index=False, encoding="utf-8-sig")

    manifest = manifest.merge(
        merged[["episode_index", "overall_quality_score_before", "overall_quality_score_after", "score_gain_measured", "targeted_codes_removed", "passed_full_recheck"]],
        on="episode_index",
        how="left",
    )
    manifest["verification_status"] = manifest.passed_full_recheck.map({True: "复检通过", False: "仍需复核"})
    manifest.to_csv(REPAIRED_ROOT / "repair_manifest.csv", index=False, encoding="utf-8-sig")
    (REPAIRED_ROOT / "repair_manifest.json").write_text(json.dumps(manifest.to_dict(orient="records"), ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {
        "sample_count": int(len(merged)),
        "full_recheck_passed": int(merged.passed_full_recheck.sum()),
        "targeted_codes_removed": int(merged.targeted_codes_removed.sum()),
        "mean_score_before": round(float(merged.overall_quality_score_before.mean()), 2),
        "mean_score_after": round(float(merged.overall_quality_score_after.mean()), 2),
        "mean_score_gain_measured": round(float(merged.score_gain_measured.mean()), 2),
        "flagged_frames_before": int(merged.flagged_frames_before.sum()),
        "flagged_frames_after": int(merged.flagged_frames_after.sum()),
        "evidence_note": "Measured by re-running the same v2 detector on immutable source files and repaired copies; no estimated score uplift is used.",
    }
    (OUT_DIR / "repair_validation_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(merged.to_string(index=False))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
