"""Write auditable repaired copies for defects with unambiguous evidence.

Original Parquet files are never modified.  Every changed row is described in
a sidecar quality mask and every source/output file is SHA-256 hashed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
from dataset_paths import REPAIRED_ROOT, resolve_test_root

SOURCE_ROOT = resolve_test_root()
OUTPUT_ROOT = REPAIRED_ROOT
FPS = 10.0
DETECTOR_VERSION = "RefSync-QA-v2.0"
TARGETS = {
    6: ["S_TASK_INDEX_INVALID"],
    11: ["S_TASK_SWITCH_WITHIN_EPISODE"],
    13: ["T_TIMESTAMP_JITTER_OR_DRIFT"],
    25: ["T_TIMESTAMP_UNIT_MISMATCH"],
    54: ["V_SPARSE_POSITION_SPIKE"],
    73: ["T_TIMESTAMP_JITTER_OR_DRIFT"],
    75: ["T_TIMESTAMP_JITTER_OR_DRIFT"],
}


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


def task_mapping() -> tuple[dict[int, dict[str, Any]], dict[str, int]]:
    episodes = {int(row["episode_index"]): row for row in read_jsonl(SOURCE_ROOT / "meta" / "episodes.jsonl")}
    tasks = {str(row["task"]): int(row["task_index"]) for row in read_jsonl(SOURCE_ROOT / "meta" / "tasks.jsonl")}
    return episodes, tasks


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


def repair_task_index(df: pd.DataFrame, mask: pd.DataFrame, expected_task: int, code: str) -> None:
    original = pd.to_numeric(df["task_index"], errors="coerce").to_numpy()
    changed = ~np.isclose(original, expected_task, equal_nan=False)
    df.loc[changed, "task_index"] = expected_task
    mask.loc[changed, ["repair_applied", "repair_code", "field", "quality_mask"]] = [True, code, "task_index", False]
    mask.loc[changed, "original_value"] = [str(x) for x in original[changed]]
    mask.loc[changed, "repaired_value"] = str(expected_task)


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


def repair_sparse_position_spike(df: pd.DataFrame, mask: pd.DataFrame) -> None:
    states = clone_array_column(df["state"])
    matrix = np.stack(states).astype(np.float64)
    position_columns = np.array([0, 1, 2, 10, 11, 12])
    bad = np.argwhere(np.abs(matrix[:, position_columns]) > 1.0)
    if len(bad) == 0 or len(np.unique(bad[:, 0])) > max(1, int(0.01 * len(df))):
        raise ValueError("numeric repair rejected: anomaly is not sparse")
    changes_by_row: dict[int, list[tuple[str, float, float]]] = {}
    for row_idx, local_col in bad:
        col = int(position_columns[int(local_col)])
        if row_idx <= 0 or row_idx >= len(matrix) - 1:
            raise ValueError("numeric repair rejected: spike lacks two trusted neighbours")
        before = float(matrix[row_idx, col])
        repaired = float((matrix[row_idx - 1, col] + matrix[row_idx + 1, col]) / 2.0)
        if abs(repaired) > 1.0:
            raise ValueError("numeric repair rejected: neighbour interpolation violates envelope")
        matrix[row_idx, col] = repaired
        changes_by_row.setdefault(int(row_idx), []).append((f"state[{col}]", before, repaired))
    for row_idx, changes in changes_by_row.items():
        mask.loc[row_idx, ["repair_applied", "repair_code", "field", "quality_mask"]] = [
            True,
            "V_SPARSE_POSITION_SPIKE",
            ";".join(field for field, _, _ in changes),
            False,
        ]
        mask.loc[row_idx, "original_value"] = ";".join(f"{before:.9g}" for _, before, _ in changes)
        mask.loc[row_idx, "repaired_value"] = ";".join(f"{repaired:.9g}" for _, _, repaired in changes)
    df["state"] = [row.astype(np.float32) for row in matrix]


def main() -> None:
    data_dir = OUTPUT_ROOT / "data" / "chunk-000"
    mask_dir = OUTPUT_ROOT / "quality_masks"
    meta_dir = OUTPUT_ROOT / "meta"
    data_dir.mkdir(parents=True, exist_ok=True)
    mask_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)
    episode_meta, tasks = task_mapping()
    manifest: list[dict[str, Any]] = []
    selected_meta: list[dict[str, Any]] = []

    for ep, codes in TARGETS.items():
        source = SOURCE_ROOT / "data" / "chunk-000" / f"episode_{ep:06d}.parquet"
        target = data_dir / source.name
        df = pd.read_parquet(source)
        if "state" in df:
            df["state"] = clone_array_column(df["state"])
        if "actions" in df:
            df["actions"] = clone_array_column(df["actions"])
        mask = mask_template(df, ep)
        for code in codes:
            if code.startswith("S_TASK_"):
                task_names = episode_meta[ep].get("tasks", [])
                if len(task_names) != 1 or task_names[0] not in tasks:
                    raise ValueError(f"episode {ep}: metadata does not uniquely determine task_index")
                repair_task_index(df, mask, tasks[task_names[0]], code)
            elif code.startswith("T_TIMESTAMP_"):
                repair_timestamp(df, mask, code)
            elif code == "V_SPARSE_POSITION_SPIKE":
                repair_sparse_position_spike(df, mask)
            else:
                raise ValueError(f"unsupported repair code: {code}")
        df.to_parquet(target, index=False)
        mask.to_csv(mask_dir / f"episode_{ep:06d}.quality_mask.csv", index=False, encoding="utf-8-sig")
        manifest.append(
            {
                "episode_index": ep,
                "source_file": f"data/chunk-000/{source.name}",
                "repaired_file": f"data/chunk-000/{target.name}",
                "repair_codes": ";".join(codes),
                "changed_rows": int(mask.repair_applied.sum()),
                "changed_values": int(
                    mask.loc[mask.repair_applied, "field"].astype(str).map(lambda value: value.count(";") + 1).sum()
                ),
                "source_sha256": sha256(source),
                "repaired_sha256": sha256(target),
                "detector_version": DETECTOR_VERSION,
                "repaired_at_utc": datetime.now(timezone.utc).isoformat(),
                "source_immutable": True,
                "verification_status": "待复检",
            }
        )
        selected_meta.append(episode_meta[ep])

    info = json.loads((SOURCE_ROOT / "meta" / "info.json").read_text(encoding="utf-8"))
    info["total_episodes"] = len(TARGETS)
    info["total_frames"] = int(sum(int(row["length"]) for row in selected_meta))
    info["splits"] = {"repair_validation": f"0:{len(TARGETS)}"}
    (meta_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    write_jsonl(meta_dir / "episodes.jsonl", selected_meta)
    write_jsonl(meta_dir / "tasks.jsonl", read_jsonl(SOURCE_ROOT / "meta" / "tasks.jsonl"))
    pd.DataFrame(manifest).to_csv(OUTPUT_ROOT / "repair_manifest.csv", index=False, encoding="utf-8-sig")
    (OUTPUT_ROOT / "repair_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(pd.DataFrame(manifest).to_string(index=False))


if __name__ == "__main__":
    main()
