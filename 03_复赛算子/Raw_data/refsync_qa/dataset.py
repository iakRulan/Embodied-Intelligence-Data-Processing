# -*- coding: utf-8 -*-
"""Dataset discovery and metadata (LeRobot v2.1 layout, tolerant of partial layouts).

Accepted inputs:
* a LeRobot root containing ``data/chunk-*/episode_*.parquet`` and ``meta/``;
* any directory tree containing ``*.parquet`` files (searched recursively);
* a single ``.parquet`` file.
Nothing here assumes a fixed number of episodes.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config

_SKIP_PARTS = {"__MACOSX", ".git", "__pycache__", "_previous_runs"}
_EP_RE = re.compile(r"episode_(\d+)")
MARKER = ".refsync_qa_output.json"  # written into every output directory by the pipeline


def marked_dirs(root: Path) -> list[Path]:
    """Operator output directories at or below ``root`` (their contents are never treated as input)."""
    return [m.parent for m in root.rglob(MARKER)] if root.is_dir() else []


def is_under(path: Path, dirs: list[Path]) -> bool:
    return any(d == path or d in path.parents for d in dirs)


@dataclass
class EpisodeRef:
    episode_id: int
    path: Path
    rel_path: str
    meta: dict[str, Any] | None = None


@dataclass
class DatasetInfo:
    root: Path
    episodes: list[EpisodeRef]
    info: dict[str, Any] = field(default_factory=dict)
    tasks: dict[int, str] = field(default_factory=dict)  # task_index -> task text
    task_by_text: dict[str, int] = field(default_factory=dict)
    episodes_meta: dict[int, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    fps: float = 10.0
    image_columns: list[str] = field(default_factory=list)
    video_columns: list[str] = field(default_factory=list)
    expected_image_shape: tuple[int, int, int] | None = (224, 224, 3)
    state_dim: int | None = 20
    action_dim: int | None = 20


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _find_meta_dir(root: Path) -> Path | None:
    if (root / "meta").is_dir():
        return root / "meta"
    marks = marked_dirs(root)
    for cand in sorted(root.rglob("meta")):
        if cand.is_dir() and not (set(cand.parts) & _SKIP_PARTS) and (cand / "info.json").exists() and not is_under(cand, marks):
            return cand
    return None


def discover(input_path: str | Path, options: dict[str, Any] | None = None) -> DatasetInfo:
    options = {**config.DEFAULT_OPTIONS, **(options or {})}
    p = Path(input_path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"input not found: {p}")
    if p.is_file():
        files = [p]
        root = p.parent.parent.parent if p.parent.name.startswith("chunk-") else p.parent
    else:
        root = p
        marks = marked_dirs(p)
        files = sorted(
            f for f in p.rglob("*.parquet")
            if not (set(f.relative_to(p).parts) & _SKIP_PARTS) and not f.name.startswith("._") and not is_under(f, marks)
        )
    ds = DatasetInfo(root=root, episodes=[])
    if p.is_dir() and marks:
        ds.warnings.append("跳过 RefSync-QA 输出目录（含输出标记）: " + ", ".join(str(m) for m in marks))
    if not files:
        ds.warnings.append(f"no parquet files under {p}")

    meta_dir = _find_meta_dir(root)
    if meta_dir is not None:
        try:
            ds.info = json.loads((meta_dir / "info.json").read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            ds.warnings.append(f"meta/info.json unreadable: {exc}")
        if (meta_dir / "tasks.jsonl").exists():
            try:
                for row in _read_jsonl(meta_dir / "tasks.jsonl"):
                    ds.tasks[int(row["task_index"])] = str(row["task"])
                ds.task_by_text = {v: k for k, v in ds.tasks.items()}
            except Exception as exc:  # noqa: BLE001
                ds.warnings.append(f"meta/tasks.jsonl unreadable: {exc}")
        if (meta_dir / "episodes.jsonl").exists():
            try:
                for row in _read_jsonl(meta_dir / "episodes.jsonl"):
                    ds.episodes_meta[int(row["episode_index"])] = row
            except Exception as exc:  # noqa: BLE001
                ds.warnings.append(f"meta/episodes.jsonl unreadable: {exc}")
    else:
        ds.warnings.append("meta/ not found: metadata consistency checks are marked 不可评估")

    # fps
    fps = options.get("fps") or ds.info.get("fps") or 10.0
    ds.fps = float(fps)

    # image / video columns from info.json features
    feats = ds.info.get("features", {}) if isinstance(ds.info.get("features"), dict) else {}
    for name, spec in feats.items():
        dtype = str(spec.get("dtype", "")) if isinstance(spec, dict) else ""
        if dtype == "image":
            ds.image_columns.append(name)
            shape = spec.get("shape")
            if shape and len(shape) == 3 and options.get("expected_image_shape") is None:
                ds.expected_image_shape = tuple(int(x) for x in shape)
        elif dtype == "video":
            ds.video_columns.append(name)
    if options.get("expected_image_shape"):
        ds.expected_image_shape = tuple(int(x) for x in options["expected_image_shape"])
    if not ds.image_columns and not ds.video_columns:
        ds.image_columns = list(config.DEFAULT_IMAGE_COLUMNS)
    for key, alias in (("state_dim", "state"), ("action_dim", "actions")):
        for name in config.FIELD_ALIASES[alias]:
            spec = feats.get(name)
            if isinstance(spec, dict) and spec.get("shape"):
                setattr(ds, key, int(spec["shape"][0]))
                break
    if ds.video_columns:
        ds.warnings.append(
            "video-encoded camera columns detected (" + ",".join(ds.video_columns) + "); "
            "this operator scores embedded images only, video streams are reported as 不可评估"
        )

    used: set[int] = set()
    for i, f in enumerate(files):
        m = _EP_RE.search(f.stem)
        ep = int(m.group(1)) if m else i
        while ep in used:  # duplicated ids in a flat folder
            ep += 100000
        used.add(ep)
        try:
            rel = str(f.relative_to(root)).replace("\\", "/")
        except ValueError:
            rel = f.name
        ds.episodes.append(EpisodeRef(ep, f, rel, ds.episodes_meta.get(ep)))
    ds.episodes.sort(key=lambda e: e.episode_id)
    return ds


def expected_task_index(ds: DatasetInfo, ep: int) -> int | None:
    """Task index uniquely implied by episodes.jsonl + tasks.jsonl, else None."""
    meta = ds.episodes_meta.get(ep)
    if not meta:
        return None
    tasks = meta.get("tasks") or []
    if isinstance(tasks, str):
        tasks = [tasks]
    if len(tasks) != 1:
        return None
    return ds.task_by_text.get(str(tasks[0]))
