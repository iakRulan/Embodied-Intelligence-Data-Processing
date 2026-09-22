# -*- coding: utf-8 -*-
"""Reversible, evidence-unique repairs written to a copy (source stays read-only).

Principles
* change only what the evidence identifies; never touch valid values;
* never synthesise image content, never bridge long gaps;
* every changed value is written to the audit table (exact comparison);
* the repaired copy is re-checked with the same detector by the pipeline.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np

from . import config
from .dataset import expected_task_index
from .detect import Result, _is_int, _runs


def _fmt(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, float):
        return "nan" if math.isnan(v) else repr(v)
    return str(v)


class Repairer:
    def __init__(self, feats: dict[str, Any], res: Result, ep_id: int, ds, thr: dict[str, Any], opts: dict[str, Any]):
        self.feats, self.res, self.ep, self.ds, self.thr, self.opts = feats, res, ep_id, ds, thr, opts
        self.layout = opts.get("layout", config.DEFAULT_LAYOUT)
        self.audit: list[dict[str, Any]] = []
        self.applied: list[str] = []
        self.rejected: list[str] = []
        self.meta_patch: dict[str, Any] = {}
        self.table = None

    # ------------------------------------------------------------------ helpers
    def _codes(self) -> set[str]:
        return set(self.res.all_codes())

    def _log(self, row: int, field: str, old: Any, new: Any, method: str, code: str) -> None:
        self.audit.append({
            "episode_index": self.ep, "row": int(row), "field": field,
            "original_value": _fmt(old), "repaired_value": _fmt(new), "method": method, "repair_code": code,
        })

    def _load(self):
        if self.table is None:
            import pyarrow.parquet as pq
            self.table = pq.read_table(self.feats["path"])
        return self.table

    def _set_column(self, name: str, values: list[Any]) -> None:
        import pyarrow as pa
        t = self._load()
        i = t.column_names.index(name)
        field = t.schema.field(i)
        self.table = t.set_column(i, field, pa.array(values, type=field.type))

    def _frames_consecutive(self) -> bool:
        fi = self.feats["frame_index"]
        return bool(len(fi) > 0 and _is_int(fi).all() and (fi >= 0).all() and np.all(np.diff(fi) == 1))

    # ------------------------------------------------------------------ repairs
    def timestamps(self) -> None:
        codes = self._codes()
        trig = {"T_TIMESTAMP_INVALID", "T_TIMESTAMP_NON_MONOTONIC", "T_TIMESTAMP_JITTER", "T_TIMESTAMP_GAP",
                "T_TIMESTAMP_UNIT_MISMATCH", "T_TIMESTAMP_OFFSET"} & codes
        if not trig:
            return
        if not self._frames_consecutive():
            self.rejected.append("时间戳规整：frame_index 不连续/无效，缺帧不可凭空重建")
            return
        name = self.feats["field_names"]["timestamp"]
        old = self._load().column(name).to_pylist()
        fps = float(self.ds.fps)
        new = (self.feats["frame_index"] / fps).astype(np.float32)
        out = []
        for r, (o, v) in enumerate(zip(old, new.tolist())):
            same = o is not None and not (isinstance(o, float) and math.isnan(o)) and np.float32(o) == np.float32(v)
            if not same:
                self._log(r, name, o, float(np.float32(v)), "timestamp = frame_index / fps（规整到参考采样时钟）", ";".join(sorted(trig)))
            out.append(float(np.float32(v)))
        self._set_column(name, out)
        self.applied.append("时间戳规整到 frame_index/fps")

    def index(self) -> None:
        codes = self._codes()
        if not ({"T_INDEX_INVALID", "T_INDEX_DISCONTINUITY"} & codes):
            return
        c = self.res.metrics.get("index_offset")
        if c is None or not self._frames_consecutive():
            self.rejected.append("index 重建：缺少稳定的 index-frame_index 偏移或 frame_index 不连续")
            return
        name = self.feats["field_names"]["index"]
        old = self._load().column(name).to_pylist()
        new = [int(f + c) for f in self.feats["frame_index"]]
        for r, (o, v) in enumerate(zip(old, new)):
            if o != v:
                self._log(r, name, o, v, "index = frame_index + 众数偏移", "T_INDEX_*")
        self._set_column(name, new)
        self.applied.append("index 按 frame_index 恒定偏移重建")

    def episode_index(self) -> None:
        codes = self._codes()
        if not ({"S_EPISODE_INDEX_INVALID", "S_EPISODE_INDEX_MISMATCH"} & codes):
            return
        meta = self.ds.episodes_meta.get(self.ep)
        exp = int(meta["episode_index"]) if meta and "episode_index" in meta else (self.ep if self.ep < 100000 else None)
        if exp is None or (meta is not None and int(meta.get("episode_index", exp)) != self.ep):
            self.rejected.append("episode_index 重写：文件编号与元数据不能唯一确定")
            return
        name = self.feats["field_names"]["episode_index"]
        old = self._load().column(name).to_pylist()
        for r, o in enumerate(old):
            if o != exp:
                self._log(r, name, o, exp, "按文件编号与 episodes.jsonl 一致值重写", "S_EPISODE_INDEX_*")
        self._set_column(name, [exp] * len(old))
        self.applied.append(f"episode_index 统一为 {exp}")

    def task_index(self) -> None:
        codes = self._codes()
        trig = {"S_TASK_INDEX_INVALID", "S_TASK_SWITCH_WITHIN_EPISODE", "S_TASK_META_MISMATCH"} & codes
        if not trig:
            return
        exp = expected_task_index(self.ds, self.ep)
        if exp is None:
            self.rejected.append("task_index 重写：episodes.jsonl/tasks.jsonl 不能唯一确定任务")
            return
        name = self.feats["field_names"]["task_index"]
        old = self._load().column(name).to_pylist()
        for r, o in enumerate(old):
            if o != exp:
                self._log(r, name, o, exp, "按 episodes.jsonl 唯一任务重写", ";".join(sorted(trig)))
        self._set_column(name, [exp] * len(old))
        self.applied.append(f"task_index 统一为元数据任务 {exp}")

    def stream_shift(self) -> None:
        """Move payloads back to the row matching their true frame number (from path)."""
        for col, sm in self.res.metrics.get("streams", {}).items():
            k = sm.get("shift_frames")
            if not k:
                continue
            implied = sm.get("vk_implied_shift")
            if implied is not None and abs(int(implied) - int(k)) > 1:
                self.rejected.append(f"{col} 重对齐：内容时滞 {implied:+d} 与 path 偏移 {k:+d} 不一致，保留隔离")
                continue
            old = self._load().column(col).to_pylist()
            n = len(old)
            new: list[Any] = []
            for j in range(n):
                src = j - k
                if 0 <= src < n and isinstance(old[src], dict) and old[src].get("bytes"):
                    new.append(dict(old[src]))
                else:
                    new.append({"bytes": None, "path": None})
            for j in range(n):
                o_path = old[j].get("path") if isinstance(old[j], dict) else None
                n_path = new[j].get("path") if isinstance(new[j], dict) else None
                self._log(j, f"{col}", f"row{j}:{o_path}", f"row{j - k}:{n_path}" if n_path else "缺图(晚启动)",
                          "按 path 帧号把图像移回对应行，缺失端置空", "S_STREAM_SHIFTED")
            self._set_column(col, new)
            evidence = f"内容时滞 {implied:+d}" if implied is not None else "无腕部运动校验（场景相机）"
            self.applied.append(f"{col} 图像整体移回 {k:+d} 帧（{evidence}），{abs(k)} 帧记为缺图")

    def sparse_numeric(self) -> None:
        for key, code in (("state", "J_STATE_NONFINITE"), ("actions", "J_ACTION_NONFINITE")):
            M = self.feats["state" if key == "state" else "action"]
            dim_ok = self.feats["state_dim_ok" if key == "state" else "action_dim_ok"]
            bad = dim_ok & ~np.isfinite(M).all(axis=1)
            if not bad.any():
                continue
            n = len(M)
            if bad.sum() > self.opts["sparse_repair_max_ratio"] * n:
                self.rejected.append(f"{key} 稀疏补值：非有限行 {int(bad.sum())}/{n} 超过比例上限，整段不臆造")
                continue
            if any(b - a + 1 > self.opts["sparse_repair_max_gap"] for a, b in _runs(bad)):
                self.rejected.append(f"{key} 稀疏补值：存在长于 {self.opts['sparse_repair_max_gap']} 帧的连续缺口")
                continue
            new = M.copy()
            rot_groups = []
            if M.shape[1] == self.layout["dim"]:
                for spec in self.layout["arms"].values():
                    r = spec["rot6d"]
                    rot_groups.append((r[:3], r[3:]))
            ok_all = True
            for i in np.flatnonzero(bad):
                for c in np.flatnonzero(~np.isfinite(M[i])):
                    # neighbours of this component
                    lo = i - 1
                    while lo >= 0 and not np.isfinite(M[lo, c]):
                        lo -= 1
                    hi = i + 1
                    while hi < n and not np.isfinite(M[hi, c]):
                        hi += 1
                    if lo < 0 or hi >= n:
                        ok_all = False
                        break
                    interp = M[lo, c] + (M[hi, c] - M[lo, c]) * (i - lo) / (hi - lo)
                    value, method = interp, "该分量时间线性插值"
                    for va, vb in rot_groups:
                        for own, other in ((va, vb), (vb, va)):
                            if c in own:
                                others = [x for x in own if x != c]
                                if np.isfinite(M[i, others]).all() and np.isfinite(M[i, other]).all():
                                    pos = own.index(c)
                                    partner = M[i, other[pos]]
                                    if abs(partner) > 0.2:  # orthogonality: v_own . v_other = 0
                                        dot_rest = sum(M[i, own[k]] * M[i, other[k]] for k in range(3) if k != pos)
                                        value, method = -dot_rest / partner, "Rotation-6D 正交约束解出该分量"
                                    else:  # unit norm, sign from interpolation
                                        rest = sum(M[i, x] ** 2 for x in others)
                                        if rest <= 1.0:
                                            value = math.copysign(math.sqrt(1.0 - rest), interp if interp != 0 else 1.0)
                                            method = "Rotation-6D 单位范数约束解出该分量"
                    new[i, c] = value
                    self._log(i, f"{key}[{c}]", M[i, c], float(np.float32(value)), method, code)
                if not ok_all:
                    break
            if not ok_all:
                self.rejected.append(f"{key} 稀疏补值：缺口位于轨迹首尾，缺少两侧可信邻域")
                self.audit = [a for a in self.audit if not a["field"].startswith(f"{key}[")]
                continue
            name = self.feats["field_names"][key]
            old = self._load().column(name).to_pylist()
            rows = []
            for r in range(n):
                if bad[r]:
                    rows.append([float(np.float32(x)) for x in new[r]])
                else:
                    rows.append(old[r])  # untouched rows keep their exact original values
            self._set_column(name, rows)
            self.applied.append(f"{key} 仅补 {int((~np.isfinite(M[bad])).sum())} 个非有限分量，其余分量不变")

    def isolated_spikes(self) -> None:
        """Single-frame position spikes outside the envelope whose neighbours agree."""
        if "J_POSITION_ENVELOPE" not in self._codes():
            return
        env = self.thr["position_envelope"]
        for key in ("state", "actions"):
            M = self.feats["state" if key == "state" else "action"]
            if M.shape[1] != self.layout["dim"]:
                continue
            n = len(M)
            fin = np.isfinite(M).all(axis=1)
            pos_cols = [c for spec in self.layout["arms"].values() for c in spec["pos"]]
            out = np.abs(M[:, pos_cols]) > env
            rows = np.flatnonzero(out.any(axis=1) & fin)
            if len(rows) == 0:
                continue
            if len(rows) > max(1, int(0.01 * n)):
                self.rejected.append(f"{key} 位置越界 {len(rows)} 帧，不是孤立单帧突跳，不插值")
                continue
            new = M.copy()
            changed = []
            for i in rows:
                if i == 0 or i == n - 1 or not (fin[i - 1] and fin[i + 1]) or out[i - 1].any() or out[i + 1].any():
                    changed = None
                    break
                for j, c in enumerate(pos_cols):
                    if out[i, j]:
                        if abs(M[i + 1, c] - M[i - 1, c]) > self.thr["state_step_max"]:
                            changed = None
                            break
                        new[i, c] = (M[i - 1, c] + M[i + 1, c]) / 2.0
                        changed.append((i, c))
                if changed is None:
                    break
            if not changed:
                self.rejected.append(f"{key} 位置越界不满足孤立单帧条件，不插值")
                continue
            name = self.feats["field_names"][key]
            old = self._load().column(name).to_pylist()
            touched = {i for i, _ in changed}
            for i, c in changed:
                self._log(i, f"{key}[{c}]", M[i, c], float(np.float32(new[i, c])), "孤立单帧越界：取前后帧均值（仅越界分量）", "J_POSITION_ENVELOPE")
            rows_out = [([float(np.float32(x)) for x in new[r]] if r in touched else old[r]) for r in range(n)]
            self._set_column(name, rows_out)
            self.applied.append(f"{key} 孤立越界分量 {len(changed)} 个取邻帧均值")

    def metadata(self) -> None:
        if "T_METADATA_LENGTH_MISMATCH" in self.res.ep_codes:
            meta = self.ds.episodes_meta.get(self.ep, {})
            self.meta_patch = {"length": int(self.feats["n"])}
            self.audit.append({"episode_index": self.ep, "row": -1, "field": "episodes.jsonl.length",
                               "original_value": _fmt(meta.get("length")), "repaired_value": str(self.feats["n"]),
                               "method": "按实际行数更新元数据", "repair_code": "T_METADATA_LENGTH_MISMATCH"})
            self.applied.append("episodes.jsonl length 更新为实际行数")

    # ------------------------------------------------------------------ run
    def run(self) -> bool:
        if not self.feats.get("read_ok") or self.feats.get("n", 0) == 0:
            return False
        if "C_SCHEMA_MISSING_FIELD" in self.res.ep_codes:
            self.rejected.append("必需字段缺失：文件级隔离，不做数据改写")
            return False
        for step in (self.timestamps, self.index, self.episode_index, self.task_index,
                     self.stream_shift, self.sparse_numeric, self.isolated_spikes, self.metadata):
            step()
        return self.table is not None

    def write(self, out_path: Path) -> None:
        import pyarrow.parquet as pq
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(self.table, out_path, compression="snappy")
