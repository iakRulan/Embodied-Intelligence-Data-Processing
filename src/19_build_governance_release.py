"""Build the governance release: masks, clips, train policy, extra safe repairs.

Original test Parquet stays read-only.  The first seven repaired copies are not
touched.  Extra rewrites are written under governed_data/extended_repairs/ and
evaluated separately so the 7/7 full-recheck metric stays intact.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
from dataset_paths import GOV_ROOT, OUT_DIR, REPAIRED_ROOT, resolve_test_root

SOURCE_ROOT = resolve_test_root()
EXT_ROOT = GOV_ROOT / "extended_repairs"
FPS = 10.0
DETECTOR_VERSION = "RefSync-QA-v2.1-gov"
MIN_CLIP_FRAMES = 30
FULL_REPAIRED = {6, 11, 13, 25, 54, 73, 75}
FILE_QUARANTINE = {2, 82}
PATH_OFFSET_TARGETS = {29, 48, 74}

import importlib.util
from types import ModuleType


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def clone_array_column(series: pd.Series) -> list[Any]:
    result: list[Any] = []
    for value in series:
        if isinstance(value, np.ndarray):
            result.append(value.copy())
        elif isinstance(value, list):
            result.append(np.asarray(value, dtype=np.float32).copy())
        else:
            result.append(value)
    return result


def longest_true_run(mask: np.ndarray) -> tuple[int, int, int]:
    best = (0, -1, -1)
    start = None
    for i, value in enumerate(mask.astype(bool)):
        if value and start is None:
            start = i
        if (not value or i == len(mask) - 1) and start is not None:
            end = i if value and i == len(mask) - 1 else i - 1
            length = end - start + 1
            if length > best[0]:
                best = (length, start, end)
            start = None
    return best


def contiguous_runs(mask: np.ndarray) -> list[tuple[int, int, int]]:
    runs: list[tuple[int, int, int]] = []
    start = None
    for i, value in enumerate(mask.astype(bool)):
        if value and start is None:
            start = i
        if (not value or i == len(mask) - 1) and start is not None:
            end = i if value and i == len(mask) - 1 else i - 1
            runs.append((start, end, end - start + 1))
            start = None
    return runs


def export_source_masks(report: pd.DataFrame, flags: pd.DataFrame) -> pd.DataFrame:
    mask_dir = GOV_ROOT / "quality_masks"
    mask_dir.mkdir(parents=True, exist_ok=True)
    flags = flags.copy()
    flags["codes"] = flags.issue_codes.fillna("")
    rows: list[dict[str, Any]] = []
    for rec in report.itertuples(index=False):
        ep = int(rec.episode_index)
        fg = flags[flags.episode_index.eq(ep)].sort_values("row")
        if fg.empty:
            rows.append(
                {
                    "episode_index": ep,
                    "status": rec.status,
                    "mask_rows": 0,
                    "keep_rows": 0,
                    "drop_rows": 0,
                    "longest_keep_run": 0,
                    "mask_file": "",
                }
            )
            continue
        keep = fg.codes.eq("")
        if rec.status == "通过":
            keep = pd.Series(True, index=fg.index)
        frame_index = pd.to_numeric(fg["frame_index"], errors="coerce") if "frame_index" in fg.columns else fg.row
        mask = pd.DataFrame(
            {
                "episode_index": ep,
                "row": fg.row.astype(int).to_numpy(),
                "frame_index": pd.to_numeric(frame_index, errors="coerce").to_numpy(),
                "issue_codes": fg.codes.to_numpy(),
                "repair_applied": False,
                "repair_code": "",
                "quality_mask": keep.to_numpy(),
                "train_keep": keep.to_numpy(),
            }
        )
        path = mask_dir / f"episode_{ep:06d}.quality_mask.csv"
        mask.to_csv(path, index=False, encoding="utf-8-sig")
        longest, _, _ = longest_true_run(keep.to_numpy())
        rows.append(
            {
                "episode_index": ep,
                "status": rec.status,
                "mask_rows": int(len(mask)),
                "keep_rows": int(keep.sum()),
                "drop_rows": int((~keep).sum()),
                "longest_keep_run": int(longest),
                "mask_file": str(path.relative_to(ROOT)).replace("\\", "/"),
            }
        )
    summary = pd.DataFrame(rows)
    summary.to_csv(OUT_DIR / "governance_mask_summary.csv", index=False, encoding="utf-8-sig")
    return summary


def export_clips(report: pd.DataFrame, flags: pd.DataFrame) -> pd.DataFrame:
    flags = flags.copy()
    flags["codes"] = flags.issue_codes.fillna("")
    clips: list[dict[str, Any]] = []
    for rec in report.itertuples(index=False):
        ep = int(rec.episode_index)
        if ep in FILE_QUARANTINE:
            continue
        fg = flags[flags.episode_index.eq(ep)].sort_values("row")
        if fg.empty:
            continue
        keep = fg.codes.eq("").to_numpy() if rec.status != "通过" else np.ones(len(fg), dtype=bool)
        rows = fg.row.astype(int).to_numpy()
        frames = pd.to_numeric(fg["frame_index"], errors="coerce").to_numpy() if "frame_index" in fg.columns else fg.row.astype(int).to_numpy()
        for start, end, length in contiguous_runs(keep):
            if length < MIN_CLIP_FRAMES:
                continue
            clips.append(
                {
                    "episode_index": ep,
                    "clip_id": f"ep{ep:03d}_{int(rows[start]):06d}_{int(rows[end]):06d}",
                    "start_row": int(rows[start]),
                    "end_row": int(rows[end]),
                    "frame_count": int(length),
                    "duration_s": round(length / FPS, 2),
                    "start_frame_index": int(frames[start]) if np.isfinite(frames[start]) else int(rows[start]),
                    "end_frame_index": int(frames[end]) if np.isfinite(frames[end]) else int(rows[end]),
                    "source_status": rec.status,
                    "source_repair_mode": rec.repair_mode,
                    "use": "训练切段",
                }
            )
    df = pd.DataFrame(clips)
    df.to_csv(OUT_DIR / "usable_clips.csv", index=False, encoding="utf-8-sig")
    df.to_csv(GOV_ROOT / "usable_clips.csv", index=False, encoding="utf-8-sig")
    return df


def export_work_orders(report: pd.DataFrame) -> pd.DataFrame:
    intervals = pd.read_csv(OUT_DIR / "test_issue_intervals.csv", encoding="utf-8-sig")
    flagged = report[report.status.eq("需复核")][["episode_index", "repair_mode", "issue_codes", "confidence_label"]]
    orders = intervals.merge(flagged, on="episode_index", how="inner")
    orders["work_order_type"] = np.where(
        orders.repair_mode.eq("隔离回采"),
        "隔离回采",
        np.where(orders.repair_mode.eq("自动标记+回采"), "切段回采", "掩膜复核"),
    )
    orders["priority"] = np.where(orders.frame_count >= 50, "高", np.where(orders.frame_count >= 10, "中", "低"))
    orders.to_csv(OUT_DIR / "recapture_work_orders.csv", index=False, encoding="utf-8-sig")
    return orders


def assign_policy(report: pd.DataFrame, mask_summary: pd.DataFrame, clips: pd.DataFrame, extra_full: set[int], extra_partial: set[int]) -> pd.DataFrame:
    clip_stats = (
        clips.groupby("episode_index").agg(clip_count=("clip_id", "size"), clip_frames=("frame_count", "sum"), longest_clip=("frame_count", "max"))
        if not clips.empty
        else pd.DataFrame(columns=["clip_count", "clip_frames", "longest_clip"])
    )
    merged = report.merge(mask_summary, on="episode_index", how="left", suffixes=("", "_mask"))
    merged = merged.merge(clip_stats, on="episode_index", how="left")
    merged["clip_count"] = merged.clip_count.fillna(0).astype(int)
    merged["clip_frames"] = merged.clip_frames.fillna(0).astype(int)
    merged["longest_clip"] = merged.longest_clip.fillna(0).astype(int)
    rows: list[dict[str, Any]] = []
    for rec in merged.itertuples(index=False):
        ep = int(rec.episode_index)
        codes = str(rec.issue_codes or "")
        keep = int(rec.keep_rows) if pd.notna(rec.keep_rows) else 0
        total = int(rec.mask_rows) if pd.notna(rec.mask_rows) else int(rec.rows or 0)
        longest = int(rec.longest_keep_run or 0)
        if ep in FILE_QUARANTINE:
            policy, layer, reason = "文件不可用", "隔离", "核心文件损坏或必需图像列缺失，不能从本轨迹恢复"
        elif rec.status == "通过":
            policy, layer, reason = "整段可用", "无需治理", "检测通过，按原轨迹训练"
        elif ep in FULL_REPAIRED or ep in extra_full:
            policy, layer, reason = "使用修复副本", "数据改写", "证据唯一的可逆改写已通过同版本复检"
        elif ep in extra_partial:
            if rec.longest_clip >= MIN_CLIP_FRAMES:
                policy, layer, reason = "切段使用", "部分改写+切段", "目标字段已改写，剩余不可逆缺陷按干净连续段使用"
            else:
                policy, layer, reason = "掩膜剔除后使用", "部分改写+掩膜", "目标字段已改写，训练时丢弃仍标记的行"
        elif rec.longest_clip >= 50 and keep / max(total, 1) >= 0.25:
            policy, layer, reason = "切段使用", "切段", f"最长干净连续段 {rec.longest_clip} 帧，可作训练片段"
        elif total > 0 and (total - keep) / total <= 0.10 and rec.longest_clip >= MIN_CLIP_FRAMES:
            policy, layer, reason = "掩膜剔除后使用", "掩膜", "异常行不超过 10%，训练时按 mask 丢弃"
        elif rec.longest_clip >= MIN_CLIP_FRAMES:
            policy, layer, reason = "切段使用", "切段", f"可切出 {rec.clip_count} 段、共 {rec.clip_frames} 帧"
        elif any(code in codes for code in ["V_STATE_FREEZE", "V_ACTION_FREEZE", "V_LOW_INFORMATION_TRAJECTORY"]):
            policy, layer, reason = "训练降权", "降权", "低信息/冻结无法从同轨迹恢复，保留但降低采样权重"
        else:
            policy, layer, reason = "隔离回采", "隔离", "无可安全改写路径，且没有 >=30 帧连续干净段"
        rows.append(
            {
                "episode_index": ep,
                "status": rec.status,
                "repair_mode": rec.repair_mode,
                "issue_codes": codes,
                "confidence_label": rec.confidence_label,
                "overall_quality_score": rec.overall_quality_score,
                "rows": int(rec.rows or 0),
                "keep_rows": keep,
                "drop_rows": int(rec.drop_rows or 0) if pd.notna(rec.drop_rows) else 0,
                "keep_rate": round(keep / total, 4) if total else 0.0,
                "longest_keep_run": longest,
                "clip_count": int(rec.clip_count),
                "clip_frames": int(rec.clip_frames),
                "longest_clip": int(rec.longest_clip),
                "train_policy": policy,
                "governance_layer": layer,
                "policy_reason": reason,
            }
        )
    policy = pd.DataFrame(rows).sort_values("episode_index")
    policy.to_csv(OUT_DIR / "train_policy.csv", index=False, encoding="utf-8-sig")
    policy.to_csv(GOV_ROOT / "train_policy.csv", index=False, encoding="utf-8-sig")
    return policy


def mask_template(df: pd.DataFrame, ep: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "episode_index": ep,
            "row": np.arange(len(df), dtype=int),
            "frame_index": pd.to_numeric(df["frame_index"], errors="coerce").astype("Int64"),
            "repair_applied": False,
            "repair_code": "",
            "field": "",
            "original_value": "",
            "repaired_value": "",
            "quality_mask": True,
        }
    )


def repair_timestamp(df: pd.DataFrame, mask: pd.DataFrame, code: str) -> None:
    frame = pd.to_numeric(df["frame_index"], errors="coerce").to_numpy(float)
    if not np.isfinite(frame).all() or len(np.unique(frame)) != len(frame) or not np.all(np.diff(frame) == 1):
        raise ValueError("timestamp repair rejected: frame_index is not a complete monotonic sequence")
    original = pd.to_numeric(df["timestamp"], errors="coerce").to_numpy(float)
    if not np.isfinite(original).all() or np.any(np.diff(original) <= 0):
        raise ValueError("timestamp repair rejected: original timestamp is non-finite/non-monotonic")
    repaired = original[0] + (frame - frame[0]) / FPS
    changed = ~np.isclose(original, repaired, atol=1e-6)
    df["timestamp"] = repaired.astype(np.float32)
    mask.loc[changed, ["repair_applied", "repair_code", "field", "quality_mask"]] = [True, code, "timestamp", False]
    mask.loc[changed, "original_value"] = [f"{x:.9g}" for x in original[changed]]
    mask.loc[changed, "repaired_value"] = [f"{x:.9g}" for x in repaired[changed]]


def repair_sparse_nonfinite(df: pd.DataFrame, mask: pd.DataFrame) -> None:
    states = clone_array_column(df["state"])
    matrix = np.stack(states).astype(np.float64)
    finite = np.isfinite(matrix).all(axis=1)
    bad_idx = np.where(~finite)[0]
    if len(bad_idx) == 0 or len(bad_idx) / max(len(matrix), 1) > 0.10:
        raise ValueError("numeric repair rejected: nonfinite rows are not sparse")
    for i in bad_idx:
        prevs = np.where(finite[:i])[0]
        nexts = np.where(finite[i + 1 :])[0]
        if len(prevs) == 0 or len(nexts) == 0:
            raise ValueError(f"numeric repair rejected: row {i} lacks finite neighbours")
        left = int(prevs[-1])
        right = int(nexts[0] + i + 1)
        if right - left > 3:
            raise ValueError(f"numeric repair rejected: hole around row {i} is wider than 2 frames")
        weight = (i - left) / (right - left)
        before = matrix[i].copy()
        matrix[i] = (1.0 - weight) * matrix[left] + weight * matrix[right]
        if not np.isfinite(matrix[i]).all():
            raise ValueError(f"numeric repair rejected: interpolation at row {i} still non-finite")
        finite[i] = True
        changed = np.where(~np.isclose(before, matrix[i], equal_nan=True))[0]
        mask.loc[i, ["repair_applied", "repair_code", "field", "quality_mask"]] = [
            True,
            "V_SPARSE_NONFINITE",
            ";".join(f"state[{int(c)}]" for c in changed),
            False,
        ]
        mask.loc[i, "original_value"] = ";".join("nan" if not np.isfinite(before[int(c)]) else f"{before[int(c)]:.9g}" for c in changed)
        mask.loc[i, "repaired_value"] = ";".join(f"{matrix[i, int(c)]:.9g}" for c in changed)
    df["state"] = [row.astype(np.float32) for row in matrix]


def repair_image_path_offset(df: pd.DataFrame, mask: pd.DataFrame, code: str) -> None:
    frame = pd.to_numeric(df["frame_index"], errors="coerce").to_numpy(int)
    changed_rows: set[int] = set()
    for col in ["image", "left_wrist_image", "right_wrist_image"]:
        if col not in df.columns:
            continue
        new_col: list[Any] = []
        hashes: list[str] = []
        prev_hash = ""
        dup = 0
        for i, value in enumerate(df[col]):
            item = dict(value) if isinstance(value, dict) else value
            if not isinstance(item, dict):
                new_col.append(value)
                hashes.append("")
                continue
            raw = item.get("bytes")
            if isinstance(raw, np.ndarray):
                raw_bytes = raw.tobytes()
            elif isinstance(raw, (bytes, bytearray, memoryview)):
                raw_bytes = bytes(raw)
            elif raw is None:
                raw_bytes = b""
            else:
                raw_bytes = bytes(raw)
            has_payload = len(raw_bytes) > 0
            digest = hashlib.sha1(raw_bytes).hexdigest() if has_payload else ""
            if prev_hash and digest and digest == prev_hash:
                dup += 1
            prev_hash = digest or prev_hash
            hashes.append(digest)
            expected = f"frame_{int(frame[i]):06d}.png"
            path = str(item.get("path") or "")
            if has_payload and path != expected:
                item = dict(item)
                item["path"] = expected
                changed_rows.add(i)
                current = str(mask.at[i, "field"])
                fields = [part for part in current.split(";") if part] if current not in {"", "nan"} else []
                fields.append(f"{col}.path")
                mask.at[i, "repair_applied"] = True
                mask.at[i, "repair_code"] = code
                mask.at[i, "field"] = ";".join(fields)
                orig = str(mask.at[i, "original_value"])
                new = str(mask.at[i, "repaired_value"])
                mask.at[i, "original_value"] = path if orig in {"", "nan"} else f"{orig};{path}"
                mask.at[i, "repaired_value"] = expected if new in {"", "nan"} else f"{new};{expected}"
                mask.at[i, "quality_mask"] = False
            new_col.append(item)
        if dup > 0:
            raise ValueError(f"path repair rejected on {col}: {dup} adjacent exact duplicates")
        df[col] = new_col
    if not changed_rows:
        raise ValueError("path repair rejected: no mismatched payload paths")


def apply_extended_repairs() -> tuple[pd.DataFrame, set[int], set[int]]:
    data_dir = EXT_ROOT / "data" / "chunk-000"
    mask_dir = EXT_ROOT / "quality_masks"
    meta_dir = EXT_ROOT / "meta"
    for path in (data_dir, mask_dir, meta_dir):
        path.mkdir(parents=True, exist_ok=True)
    source_meta = {int(row["episode_index"]): row for row in read_jsonl(SOURCE_ROOT / "meta" / "episodes.jsonl")}
    jobs: list[tuple[int, str]] = [
        (60, "T_METADATA_LENGTH_MISMATCH"),
        (80, "V_SPARSE_NONFINITE"),
        (17, "T_TIMESTAMP_JITTER_OR_DRIFT"),
        (29, "S_IMAGE_PATH_MISMATCH"),
        (48, "S_IMAGE_PATH_MISMATCH"),
        (74, "S_IMAGE_PATH_MISMATCH"),
    ]
    manifest: list[dict[str, Any]] = []
    selected_meta: list[dict[str, Any]] = []
    for ep, code in jobs:
        source = SOURCE_ROOT / "data" / "chunk-000" / f"episode_{ep:06d}.parquet"
        target = data_dir / source.name
        df = pd.read_parquet(source)
        if "state" in df:
            df["state"] = clone_array_column(df["state"])
        if "actions" in df:
            df["actions"] = clone_array_column(df["actions"])
        mask = mask_template(df, ep)
        if code == "T_METADATA_LENGTH_MISMATCH":
            shutil.copy2(source, target)
            mask.to_csv(mask_dir / f"episode_{ep:06d}.quality_mask.csv", index=False, encoding="utf-8-sig")
            changed = 0
        elif code == "V_SPARSE_NONFINITE":
            repair_sparse_nonfinite(df, mask)
            df.to_parquet(target, index=False)
            mask.to_csv(mask_dir / f"episode_{ep:06d}.quality_mask.csv", index=False, encoding="utf-8-sig")
            changed = int(mask.repair_applied.sum())
        elif code.startswith("T_TIMESTAMP_"):
            repair_timestamp(df, mask, code)
            df.to_parquet(target, index=False)
            mask.to_csv(mask_dir / f"episode_{ep:06d}.quality_mask.csv", index=False, encoding="utf-8-sig")
            changed = int(mask.repair_applied.sum())
        elif code == "S_IMAGE_PATH_MISMATCH":
            repair_image_path_offset(df, mask, code)
            df.to_parquet(target, index=False)
            mask.to_csv(mask_dir / f"episode_{ep:06d}.quality_mask.csv", index=False, encoding="utf-8-sig")
            changed = int(mask.repair_applied.sum())
        else:
            raise ValueError(code)
        meta_row = dict(source_meta[ep])
        if code == "T_METADATA_LENGTH_MISMATCH":
            meta_row["length"] = int(len(pd.read_parquet(target, columns=["frame_index"])))
        selected_meta.append(meta_row)
        manifest.append(
            {
                "episode_index": ep,
                "source_file": f"data/chunk-000/{source.name}",
                "repaired_file": f"governed_data/extended_repairs/data/chunk-000/{target.name}",
                "repair_codes": code,
                "changed_rows": changed,
                "source_sha256": sha256(source),
                "repaired_sha256": sha256(target),
                "detector_version": DETECTOR_VERSION,
                "repaired_at_utc": datetime.now(timezone.utc).isoformat(),
                "source_immutable": True,
                "repair_class": "meta_only" if code.startswith("T_METADATA_") else "partial_or_sparse",
            }
        )
    write_jsonl(meta_dir / "episodes.jsonl", selected_meta)
    write_jsonl(meta_dir / "tasks.jsonl", read_jsonl(SOURCE_ROOT / "meta" / "tasks.jsonl"))
    info = json.loads((SOURCE_ROOT / "meta" / "info.json").read_text(encoding="utf-8"))
    info["total_episodes"] = len(jobs)
    info["splits"] = {"extended_repair_validation": f"0:{len(jobs)}"}
    (meta_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    man = pd.DataFrame(manifest)
    man.to_csv(EXT_ROOT / "repair_manifest.csv", index=False, encoding="utf-8-sig")
    (EXT_ROOT / "repair_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(
        [{"episode_index": 60, "field": "episodes.jsonl.length", "original_value": int(source_meta[60].get("length", 0)), "repaired_value": int(selected_meta[0]["length"]), "reason": "元数据 length 与实际行数相差 47，帧级检测全绿"}]
    ).to_csv(OUT_DIR / "meta_patches.csv", index=False, encoding="utf-8-sig")
    return man, {60, 80}, {17, 29, 48, 74}


def evaluate_extended(manifest: pd.DataFrame) -> pd.DataFrame:
    evaluate = load_module("evaluate_repairs", ROOT / "src" / "12_evaluate_repairs.py")
    episode_ids = manifest.episode_index.astype(int).tolist()
    source_meta = evaluate.read_meta(SOURCE_ROOT)
    repaired_meta = evaluate.read_meta(EXT_ROOT)
    before, before_frames = evaluate.profile(SOURCE_ROOT, "before", episode_ids, source_meta)
    after, after_frames = evaluate.profile(EXT_ROOT, "after", episode_ids, repaired_meta)
    selected = [
        "episode_index", "status", "issue_codes", "issue_categories", "overall_quality_score",
        "structural_score", "temporal_score", "sync_score", "content_score", "value_score",
    ]
    merged = before[selected].merge(after[selected], on="episode_index", suffixes=("_before", "_after"))
    merged = merged.merge(manifest[["episode_index", "repair_codes", "changed_rows", "source_sha256", "repaired_sha256"]], on="episode_index", how="left")
    merged["score_gain_measured"] = merged.overall_quality_score_after - merged.overall_quality_score_before
    merged["targeted_codes_removed"] = merged.apply(
        lambda row: all(code not in str(row.issue_codes_after) for code in str(row.repair_codes).split(";") if code),
        axis=1,
    )
    merged["passed_full_recheck"] = merged.status_after.eq("通过")
    before_counts = before_frames.assign(flagged=before_frames.issue_codes.fillna("").ne("")).groupby("episode_index").flagged.sum()
    after_counts = after_frames.assign(flagged=after_frames.issue_codes.fillna("").ne("")).groupby("episode_index").flagged.sum()
    merged["flagged_frames_before"] = merged.episode_index.map(before_counts).fillna(0).astype(int)
    merged["flagged_frames_after"] = merged.episode_index.map(after_counts).fillna(0).astype(int)
    merged.to_csv(OUT_DIR / "extended_repair_validation_report.csv", index=False, encoding="utf-8-sig")
    summary = {
        "sample_count": int(len(merged)),
        "full_recheck_passed": int(merged.passed_full_recheck.sum()),
        "targeted_codes_removed": int(merged.targeted_codes_removed.sum()),
        "mean_score_before": round(float(merged.overall_quality_score_before.mean()), 2),
        "mean_score_after": round(float(merged.overall_quality_score_after.mean()), 2),
        "mean_score_gain_measured": round(float(merged.score_gain_measured.mean()), 2),
        "flagged_frames_before": int(merged.flagged_frames_before.sum()),
        "flagged_frames_after": int(merged.flagged_frames_after.sum()),
        "evidence_note": "Extended safe repairs evaluated separately from the original 7/7 full-recheck set.",
    }
    (OUT_DIR / "extended_repair_validation_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


def write_release_summary(policy: pd.DataFrame, clips: pd.DataFrame, extra: pd.DataFrame | None) -> None:
    counts = policy.train_policy.value_counts().to_dict()
    flagged = policy[policy.status.eq("需复核")]
    summary = {
        "detector_version": DETECTOR_VERSION,
        "flagged_episodes": int(len(flagged)),
        "original_full_rewrites": 7,
        "extended_full_or_partial_rewrites": 0 if extra is None else int(len(extra)),
        "train_policy_counts": {str(k): int(v) for k, v in counts.items()},
        "usable_clips": int(len(clips)),
        "usable_clip_frames": int(clips.frame_count.sum()) if not clips.empty else 0,
        "usable_clip_episodes": int(clips.episode_index.nunique()) if not clips.empty else 0,
        "source_masks": int((policy.mask_rows if "mask_rows" in policy.columns else policy.keep_rows).gt(0).sum()) if False else int((policy.keep_rows + policy.drop_rows).gt(0).sum()),
        "keep_frames_on_flagged": int(flagged.keep_rows.sum()),
        "drop_frames_on_flagged": int(flagged.drop_rows.sum()),
        "note": "改写修复率仍单独报告；本发布把 61 条需复核轨迹落到 mask/切段/降权/隔离四类可执行策略。",
    }
    if extra is not None and not extra.empty:
        summary["extended_full_recheck_passed"] = int(extra.passed_full_recheck.sum())
        summary["extended_targeted_removed"] = int(extra.targeted_codes_removed.sum())
        summary["extended_mean_gain"] = round(float(extra.score_gain_measured.mean()), 2)
    (OUT_DIR / "governance_release_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (GOV_ROOT / "governance_release_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("\n=== train_policy ===")
    print(policy.train_policy.value_counts().to_string())
    if extra is not None:
        print("\n=== extended repairs ===")
        print(extra[["episode_index", "status_before", "status_after", "issue_codes_before", "issue_codes_after", "score_gain_measured", "targeted_codes_removed", "passed_full_recheck"]].to_string(index=False))


def main() -> None:
    GOV_ROOT.mkdir(parents=True, exist_ok=True)
    report = pd.read_csv(OUT_DIR / "test_quality_report.csv", encoding="utf-8-sig")
    flags = pd.read_csv(OUT_DIR / "test_frame_flags.csv", encoding="utf-8-sig")
    mask_summary = export_source_masks(report, flags)
    clips = export_clips(report, flags)
    export_work_orders(report)
    extra_full: set[int] = set()
    extra_partial: set[int] = set()
    extra_report = None
    manifest, extra_full, extra_partial = apply_extended_repairs()
    extra_report = evaluate_extended(manifest)
    extra_full = set(extra_report.loc[extra_report.passed_full_recheck, "episode_index"].astype(int))
    extra_partial = set(extra_report.loc[~extra_report.passed_full_recheck, "episode_index"].astype(int))
    policy = assign_policy(report, mask_summary, clips, extra_full, extra_partial)
    write_release_summary(policy, clips, extra_report)


if __name__ == "__main__":
    main()
