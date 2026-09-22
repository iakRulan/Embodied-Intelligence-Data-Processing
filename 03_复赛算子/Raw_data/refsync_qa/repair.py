# -*- coding: utf-8 -*-
"""Reversible, evidence-gated repairs written to a copy (source stays read-only).

Principles
* change only what the evidence identifies uniquely; never touch valid values:
  writes are cell-level and keep each column's storage dtype (a float64 column
  stays float64, untouched components are never re-rounded);
* never synthesise image content, never bridge missing frames, never
  interpolate from neighbours that failed another quality gate;
* when evidence is not unique the repair is NOT applied: it is written to
  ``repair_candidates.csv`` with the evidence for a human decision;
* every changed cell is in the audit table, and after writing the copy is
  re-read and compared cell by cell with the source: any change that is not in
  the audit (or any audited change that did not happen) discards the copy;
* the copy is written to a temporary file and moved into place atomically.
"""
from __future__ import annotations

import math
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import numpy as np

from . import config
from .dataset import expected_task_index
from .detect import _is_int, _runs

# neighbours carrying one of these codes are not trusted as interpolation anchors
_UNTRUSTED = {"J_SPIKE_JUMP", "J_POSITION_ENVELOPE", "J_GRIPPER_RANGE", "J_ROT6D_INVALID", "J_DIM_MISMATCH",
              "J_VALUE_UNPARSEABLE", "J_STATE_ACTION_MISMATCH", "T_FRAME_INDEX_INVALID", "T_FRAME_INDEX_GAP",
              "T_FRAME_ORDER_ERROR", "C_FIELD_INVALID"}


def _fmt(v: Any) -> str:
    if isinstance(v, dict):
        def safe(x):
            if isinstance(x, bytes):
                return {"bytes_sha256": hashlib.sha256(x).hexdigest(), "length": len(x)}
            if isinstance(x, dict):
                return {k: safe(val) for k, val in x.items()}
            return x
        return json.dumps(safe(v), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if v is None:
        return "null"
    if isinstance(v, float):
        return "nan" if math.isnan(v) else repr(v)
    return str(v)


def _same(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, (int, float, np.number)) and isinstance(b, (int, float, np.number)) and not isinstance(a, bool) and not isinstance(b, bool):
        # Never round int64 identifiers through float64 (2**53 + 1 is distinct).
        if isinstance(a, (int, np.integer)) and isinstance(b, (int, np.integer)):
            return int(a) == int(b)
        if isinstance(a, (int, np.integer)):
            return math.isfinite(float(b)) and float(b).is_integer() and int(a) == int(b)
        if isinstance(b, (int, np.integer)):
            return _same(b, a)
        return (math.isnan(float(a)) and math.isnan(float(b))) or a == b
    return a == b


def _arrow_type(dtype: str):
    import pyarrow as pa

    return {"float32": pa.float32(), "float64": pa.float64(), "int64": pa.int64(), "int32": pa.int32()}.get(dtype)


def verify_copy(orig, new, audit: list[dict[str, Any]]) -> list[str]:
    """Cell-by-cell comparison of source and repaired tables against the audit.

    Returns a list of problems (empty = every change is audited and every
    audited change happened with the audited value).
    """
    expected: dict[tuple[str, int], list[dict]] = {}
    for a in audit:
        if int(a["row"]) >= 0 or a["field"].endswith(".dtype"):
            expected.setdefault((a["field"], int(a["row"])), []).append(a)
    seen: set[tuple[str, int]] = set()
    problems: list[str] = []
    if orig.num_rows != new.num_rows:
        return [f"行数 {orig.num_rows} -> {new.num_rows}"]
    if list(orig.column_names) != list(new.column_names):
        return ["列集合/顺序发生变化"]

    def equal_text(a: str, b: str) -> bool:
        if a == b:
            return True
        try:
            x, y = json.loads(a), json.loads(b)
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                return _same(x, y)
        except (ValueError, TypeError):
            pass
        return False

    def check(key: tuple[str, int], old: Any, value: Any) -> None:
        seen.add(key)
        if key not in expected:
            problems.append(f"未审计的改动 {key[0]}@{key[1]}")
            return
        chain = expected[key]
        if not equal_text(_fmt(old), chain[0]["original_value"]):
            problems.append(f"原值与审计不符 {key[0]}@{key[1]}")
        if not equal_text(_fmt(value), chain[-1]["repaired_value"]):
            problems.append(f"写入值与审计不符 {key[0]}@{key[1]}")
        for left, right in zip(chain, chain[1:]):
            if not equal_text(left["repaired_value"], right["original_value"]):
                problems.append(f"审计链断裂 {key[0]}@{key[1]}")

    for name in orig.column_names:
        of, nf = orig.schema.field(name), new.schema.field(name)
        if of.type != nf.type:
            check((name + ".dtype", -1), str(of.type), str(nf.type))
        if of.nullable != nf.nullable or of.metadata != nf.metadata:
            problems.append(f"未授权字段属性变更 {name}")
        o, w = orig.column(name).to_pylist(), new.column(name).to_pylist()
        for r, (x, y) in enumerate(zip(o, w)):
            if isinstance(x, list) and isinstance(y, list) and len(x) == len(y):
                for c, (u, v) in enumerate(zip(x, y)):
                    if not _same(u, v):
                        check((f"{name}[{c}]", r), u, v)
            elif isinstance(x, dict) or isinstance(y, dict):
                if x != y:
                    check((name, r), x, y)
            elif not _same(x, y):
                check((name, r), x, y)
    if orig.schema.metadata != new.schema.metadata:
        problems.append("未授权表级元数据变更")
    for key in expected:
        if key not in seen:
            problems.append(f"审计记录的改动未发生 {key[0]}@{key[1]}")
    return problems[:20]


class Repairer:
    def __init__(self, feats: dict[str, Any], res, ep_id: int, ds, thr: dict[str, Any], opts: dict[str, Any]):
        self.feats, self.res, self.ep, self.ds, self.thr, self.opts = feats, res, ep_id, ds, thr, opts
        self.layout = opts.get("layout", config.DEFAULT_LAYOUT)
        self.audit: list[dict[str, Any]] = []
        self.applied: list[str] = []
        self.rejected: list[str] = []
        self.candidates: list[dict[str, Any]] = []
        self.meta_patch: dict[str, Any] = {}
        self.table = None
        self.source = None
        self.verify_problems: list[str] = []

    # ------------------------------------------------------------------ helpers
    def _codes(self) -> set[str]:
        return set(self.res.all_codes())

    def _log(self, row: int, field: str, old: Any, new: Any, method: str, code: str) -> None:
        self.audit.append({
            "episode_index": self.ep, "row": int(row), "field": field,
            "original_value": _fmt(old), "repaired_value": _fmt(new), "method": method, "repair_code": code,
        })

    def _candidate(self, code: str, action: str, evidence: str, reason: str) -> None:
        self.candidates.append({"episode_index": self.ep, "issue_code": code, "proposed_action": action,
                                "evidence": evidence, "not_applied_because": reason})

    def _load(self):
        if self.table is None:
            import pyarrow.parquet as pq
            self.table = pq.read_table(self.feats["path"])
            self.source = self.table
        return self.table

    def _declared(self, name: str) -> str | None:
        spec = (self.ds.info or {}).get("features", {}).get(name)
        return str(spec.get("dtype")) if isinstance(spec, dict) and spec.get("dtype") else None

    def _put_scalar(self, name: str, values: list[Any], code: str) -> None:
        """Write a scalar column; the storage type becomes the declared one when the current one is not a numeric scalar."""
        import pyarrow as pa

        t = self._load()
        i = t.column_names.index(name)
        field = t.schema.field(i)
        typ = field.type
        if not (pa.types.is_integer(typ) or pa.types.is_floating(typ)):
            declared = _arrow_type(self._declared(name) or "")
            typ = declared or pa.float64()
            self._schema_log(name, str(field.type), str(typ), "非数值标量列按 info.json 声明类型重建", code)
        self.table = t.set_column(i, pa.field(name, typ, field.nullable, metadata=field.metadata), pa.array(values, type=typ))

    def _schema_log(self, name: str, old: str, new: str, method: str, code: str) -> None:
        self.audit.append({"episode_index": self.ep, "row": -1, "field": f"{name}.dtype", "original_value": old,
                           "repaired_value": new, "method": method, "repair_code": code})

    def _cast_for(self, name: str):
        """Python cast matching the storage of list column ``name`` (float32 values are rounded to float32)."""
        import pyarrow as pa

        vt = self._load().schema.field(name).type.value_type
        if vt == pa.float32():
            return lambda v: float(np.float32(v))
        if pa.types.is_floating(vt):
            return float
        return lambda v: int(round(v))

    def _write_cells(self, name: str, cells: dict[tuple[int, int], float]) -> None:
        """Replace only the given (row, component) cells of a list column; everything else is copied verbatim."""
        import pyarrow as pa

        t = self._load()
        i = t.column_names.index(name)
        field = t.schema.field(i)
        rows = t.column(name).to_pylist()
        cast = self._cast_for(name)
        for (r, c), v in cells.items():
            row = list(rows[r])
            row[c] = cast(v)
            rows[r] = row
        self.table = t.set_column(i, field, pa.array(rows, type=field.type))

    def _frames_consecutive(self) -> bool:
        fi = self.feats["frame_index"]
        return bool(len(fi) > 0 and _is_int(fi).all() and (fi >= 0).all() and np.all(np.diff(fi) == 1))

    # ------------------------------------------------------------------ repairs
    def schema_dtype(self) -> None:
        """Lossless cast of scalar columns whose storage dtype differs from info.json (e.g. int64 timestamp)."""
        import pyarrow as pa
        import pyarrow.compute as pc

        if "C_SCHEMA_DTYPE_MISMATCH" not in self.res.ep_codes:
            return
        t = self._load()
        for name in list(t.column_names):
            declared = self._declared(name)
            want = _arrow_type(declared or "")
            if want is None:
                continue
            field = t.schema.field(name)
            spec = self.ds.info.get("features", {}).get(name, {})
            multi = int(np.prod(spec.get("shape") or [1])) > 1
            if multi:
                vt = getattr(field.type, "value_type", None)
                if vt is not None and vt != want:
                    lossless = pa.types.is_floating(want) and pa.types.is_integer(vt)
                    self.rejected.append(f"{name} 存储 {vt}，声明 {declared}：" + ("整数转浮点需复核范围，" if lossless else "") + "不自动转换（可能有损）")
                    self._candidate("C_SCHEMA_DTYPE_MISMATCH", f"{name} 转为 {declared}", f"存储 {field.type}", "列表数值列转换可能改变有效值")
                continue
            if field.type == want or not (pa.types.is_integer(field.type) or pa.types.is_floating(field.type)):
                continue
            col = t.column(name)
            vals = pc.cast(col, pa.float64()).to_numpy(zero_copy_only=False)
            fin = vals[np.isfinite(vals)]
            if pa.types.is_floating(want):
                limit = 2 ** 24 if want == pa.float32() else 2 ** 53
                ok = bool(np.all(np.abs(fin) < limit) and np.all(fin == np.round(fin))) if pa.types.is_integer(field.type) else \
                    bool(np.all(fin.astype(np.float32).astype(np.float64) == fin)) if want == pa.float32() else True
            else:
                ok = bool(np.all(fin == np.round(fin)) and np.all(np.abs(fin) < 2 ** 53))
            if not ok:
                self.rejected.append(f"{name} 按声明 {declared} 转换会改变数值，不自动转换")
                self._candidate("C_SCHEMA_DTYPE_MISMATCH", f"{name} 转为 {declared}", f"存储 {field.type}", "有损转换")
                continue
            new = pc.cast(col, want)
            self.table = t = t.set_column(t.column_names.index(name), pa.field(name, want, field.nullable, metadata=field.metadata), new)
            self._schema_log(name, str(field.type), str(want), "按 info.json 声明类型无损转换（数值不变）", "C_SCHEMA_DTYPE_MISMATCH")
            self.applied.append(f"{name} 存储类型 {field.type} → {want}（无损）")

    def timestamps(self) -> None:
        codes = self._codes()
        trig = {"T_TIMESTAMP_INVALID", "T_TIMESTAMP_NON_MONOTONIC", "T_TIMESTAMP_JITTER", "T_TIMESTAMP_GAP",
                "T_TIMESTAMP_UNIT_MISMATCH", "T_TIMESTAMP_OFFSET"} & codes
        if "C_FIELD_INVALID" in codes and any("C_FIELD_INVALID@timestamp" in s for s in self.res.row_notes):
            trig.add("C_FIELD_INVALID")
        if not trig:
            return
        policy = self.opts.get("timestamp_policy", "conservative")
        if policy == "off":
            self.rejected.append("时间戳修复已关闭（timestamp_policy=off）")
            self._candidate(";".join(sorted(trig)), "timestamp = frame_index / fps", "LeRobot 契约", "timestamp_policy=off")
            return
        if not self._frames_consecutive():
            self.rejected.append("时间戳规整：frame_index 不连续/无效，缺帧不可凭空重建")
            return
        name = self.feats["field_names"]["timestamp"]
        fps = float(self.ds.fps)
        dt = 1.0 / fps
        fi = self.feats["frame_index"]
        ts = self.feats["timestamp"]
        nominal = fi / fps
        tol = self.thr["timestamp_jitter_frac"] * dt
        m = self.res.metrics
        # lossless whole-episode transforms first: they must reproduce the contract exactly (within jitter tolerance)
        lossless, label = None, ""
        if trig <= {"T_TIMESTAMP_UNIT_MISMATCH", "T_TIMESTAMP_OFFSET"} and np.isfinite(ts).all():
            cand = ts.copy()
            parts = []
            if "T_TIMESTAMP_UNIT_MISMATCH" in trig and m.get("timestamp_unit_scale"):
                cand = cand / float(m["timestamp_unit_scale"])
                parts.append(f"单位换算（÷{m['timestamp_unit_scale']:g}）")
            off = float(np.median(cand - nominal))
            if abs(off) > 0.5 * dt or "T_TIMESTAMP_OFFSET" in trig:
                cand = cand - off
                parts.append(f"时间基准归零（减去 {off:.6g} s）")
            dev = float(np.max(np.abs(cand - nominal)))
            if dev <= tol:
                lossless, label = cand, "+".join(parts)
            else:
                label = f"{'+'.join(parts) or '无损变换'}后与 frame_index/fps 最大偏差 {dev:.4g} s（>{tol:.2g} s，原值含截断/抖动）"
        if lossless is None and policy == "conservative":
            self.rejected.append("时间戳：非单位/基准类问题，conservative 策略不按标称周期重建")
            self._candidate(";".join(sorted(trig)), "timestamp = frame_index / fps（按标称周期规整）",
                            f"frame_index 连续；jitter 容差 {tol:.4g}s", "timestamp_policy=conservative")
            return
        import pyarrow as pa

        t = self._load()
        field = t.schema.field(name)
        declared = _arrow_type(self._declared(name) or "") or pa.float32()
        typ = field.type if pa.types.is_floating(field.type) else declared
        cast = (lambda v: float(np.float32(v))) if typ == pa.float32() else float
        old = t.column(name).to_pylist()
        new_vals = [cast(v) for v in (lossless if lossless is not None else nominal).tolist()]
        rc = self.res.row_codes
        n_changed = 0
        for r, (o, v) in enumerate(zip(old, new_vals)):
            if _same(o if not isinstance(o, list) else None, v) and not isinstance(o, list):
                continue
            if lossless is not None:
                method = f"{label}，保留变换后的残余抖动（不强制替换为 frame_index/fps）"
            elif {"T_TIMESTAMP_INVALID", "C_FIELD_INVALID"} & rc[r]:
                method = "按标称周期补值 frame_index/fps（原值无效）"
            else:
                method = "按标称周期规整 frame_index/fps（LeRobot 契约；非真实采样时刻恢复，原值见本列）" + (f"；{label}" if label else "")
            self._log(r, name, o, v, method, ";".join(sorted(trig)))
            n_changed += 1
        if typ != field.type:
            self._schema_log(name, str(field.type), str(typ), "按 info.json 声明类型写回", ";".join(sorted(trig)))
        self.table = t.set_column(t.column_names.index(name), pa.field(name, typ, field.nullable, metadata=field.metadata), pa.array(new_vals, type=typ))
        self.applied.append(f"timestamp {'：' + label if lossless is not None else '按标称周期规整' + ('（' + label + '）' if label else '')}，"
                            f"改写 {n_changed} 行（原值保留在审计）")

    def index(self) -> None:
        codes = self._codes()
        if not ({"T_INDEX_INVALID", "T_INDEX_DISCONTINUITY"} & codes) and not any("C_FIELD_INVALID@index" in s for s in self.res.row_notes):
            return
        m = self.res.metrics
        c, sup, uniq = m.get("index_offset"), m.get("index_offset_support", 0.0), m.get("index_offset_unique", False)
        need = float(self.opts.get("index_offset_repair_support", 0.9))
        if c is None or not uniq or sup < need or not self._frames_consecutive():
            why = "frame_index 不连续" if not self._frames_consecutive() else f"index−frame_index 偏移众数支持度 {sup:.0%}（需 ≥{need:.0%} 且唯一）"
            self.rejected.append(f"index 重建：{why}，无法唯一确定正确一侧")
            self._candidate("T_INDEX_DISCONTINUITY", "index = frame_index + 偏移", f"众数偏移 {c}，支持度 {sup:.0%}", why)
            return
        name = self.feats["field_names"]["index"]
        old = self._load().column(name).to_pylist()
        new = [int(f + c) for f in self.feats["frame_index"]]
        for r, (o, v) in enumerate(zip(old, new)):
            if not _same(o, v):
                self._log(r, name, o, v, f"index = frame_index + 偏移 {int(c)}（支持度 {sup:.0%}）", "T_INDEX_*")
        self._put_scalar(name, new, "T_INDEX_*")
        self.applied.append(f"index 按 frame_index + {int(c)} 重建（支持度 {sup:.0%}）")

    def episode_index(self) -> None:
        codes = self._codes()
        if not ({"S_EPISODE_INDEX_INVALID", "S_EPISODE_INDEX_MISMATCH"} & codes) and not any("C_FIELD_INVALID@episode_index" in s for s in self.res.row_notes):
            return
        meta = self.ds.episodes_meta.get(self.ep)
        exp = int(meta["episode_index"]) if meta and "episode_index" in meta else (self.ep if self.ep < 100000 else None)
        if exp is None or (meta is not None and int(meta.get("episode_index", exp)) != self.ep):
            self.rejected.append("episode_index 重写：文件编号与元数据不能唯一确定")
            return
        name = self.feats["field_names"]["episode_index"]
        old = self._load().column(name).to_pylist()
        for r, o in enumerate(old):
            if not _same(o, exp):
                self._log(r, name, o, exp, "按文件编号与 episodes.jsonl 一致值重写", "S_EPISODE_INDEX_*")
        self._put_scalar(name, [exp] * len(old), "S_EPISODE_INDEX_*")
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
            if not _same(o, exp):
                self._log(r, name, o, exp, "按 episodes.jsonl 唯一任务重写", ";".join(sorted(trig)))
        self._put_scalar(name, [exp] * len(old), ";".join(sorted(trig)))
        self.applied.append(f"task_index 统一为元数据任务 {exp}")

    def stream_shift(self) -> None:
        """Move payloads back to the row matching their frame number, only with independent content evidence."""
        for col, sm in self.res.metrics.get("streams", {}).items():
            k = sm.get("shift_frames")
            if not k:
                continue
            ev = sm.get("shift_evidence", [])
            ev_txt = "；".join(f"{e['method']} 推断 {e['implied']:+d} r={e['r']} 增益={e['gain']}" for e in ev) or "无"
            if not sm.get("shift_path_complete") or not sm.get("shift_frames_consecutive"):
                why = "path 帧号不完整" if not sm.get("shift_path_complete") else "frame_index 不连续"
                self.rejected.append(f"{col} 重对齐：{why}，不自动移动")
                self._candidate("S_STREAM_SHIFTED", f"{col} 图像整体移回 {k:+d} 帧", ev_txt, why)
                continue
            # Weak scene correlation remains review evidence, never write authority.
            def strong(e):
                return ((e["tier"] == "中" and e["r"] >= self.thr["shift_wrist_min_r"])
                        or (e["method"].startswith("跨相机") and e["r"] >= self.opts.get("shift_cross_camera_min_r", 0.8))) \
                    and (e["gain"] or 0) >= self.thr["shift_wrist_min_gain"]
            qual = [e for e in ev if e["qualifies"] and strong(e)]
            contra = [e for e in ev if not e["agrees"] and strong(e)]
            if not qual or contra:
                why = "独立内容证据与 path 偏移矛盾" if contra else "缺少满足门槛的独立内容证据"
                self.rejected.append(f"{col} 重对齐：{why}（path 偏移 {k:+d}；{ev_txt}），保留隔离")
                self._candidate("S_STREAM_SHIFTED", f"{col} 图像整体移回 {k:+d} 帧", ev_txt, why)
                continue
            strength = "本臂运动统计" if any(e["tier"] == "中" for e in qual) else "跨相机强相关（统计）"
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
                if old[j] != new[j]:
                    self._log(j, col, old[j], new[j],
                              f"按 path 帧号把图像移回对应行，缺失端置空（内容证据：{strength}）", "S_STREAM_SHIFTED")
            import pyarrow as pa

            t = self._load()
            i = t.column_names.index(col)
            self.table = t.set_column(i, t.schema.field(i), pa.array(new, type=t.schema.field(i).type))
            used = "；".join(f"{e['method']} {e['implied']:+d}(r={e['r']})" for e in qual)
            self.applied.append(f"{col} 图像整体移回 {k:+d} 帧（path 帧号 + 独立内容证据[{strength}]：{used}），{abs(k)} 帧记为缺图")

    def _trusted(self, r: int) -> bool:
        return not (_UNTRUSTED & self.res.row_codes[r])

    def sparse_numeric(self) -> None:
        fi = self.feats["frame_index"]
        fi_ok = _is_int(fi) & (fi >= 0)
        for key, code in (("state", "J_STATE_NONFINITE"), ("actions", "J_ACTION_NONFINITE")):
            M = self.feats["state" if key == "state" else "action"]
            dim_ok = self.feats["state_dim_ok" if key == "state" else "action_dim_ok"]
            unp = self.feats.get("state_unparseable" if key == "state" else "action_unparseable", np.zeros(len(M), bool))
            bad = dim_ok & ~np.isfinite(M).all(axis=1)
            if not bad.any():
                continue
            n = len(M)
            if unp[bad].any():
                self.rejected.append(f"{key} 稀疏补值：存在无法解析的分量，不在同一列上补数")
                continue
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
                    r6 = spec["rot6d"]
                    rot_groups.append((r6[:3], r6[3:], r6))
            cells: dict[tuple[int, int], float] = {}
            logs = []
            fail = ""
            for i in np.flatnonzero(bad):
                for c in np.flatnonzero(~np.isfinite(M[i])):
                    lo = i - 1
                    while lo >= 0 and not np.isfinite(M[lo, c]):
                        lo -= 1
                    hi = i + 1
                    while hi < n and not np.isfinite(M[hi, c]):
                        hi += 1
                    if lo < 0 or hi >= n:
                        fail = "缺口位于轨迹首尾，缺少两侧可信邻域"
                        break
                    seg = np.arange(lo, hi + 1)
                    if not fi_ok[seg].all() or not np.all(np.diff(fi[seg]) == 1):
                        fail = f"行 {lo}–{hi} 间 frame_index 不连续/无效（跨缺帧或乱序不补值）"
                        break
                    if hi - lo - 1 > self.opts["sparse_repair_max_gap"]:
                        fail = f"分量 {c} 连续缺口 {hi - lo - 1} 帧超过上限"
                        break
                    if not (self._trusted(lo) and self._trusted(hi)):
                        fail = f"行 {i} 的邻居 {lo}/{hi} 带有数值/帧序问题码，不作为插值锚点"
                        break
                    w = (fi[i] - fi[lo]) / (fi[hi] - fi[lo])
                    interp = M[lo, c] + (M[hi, c] - M[lo, c]) * w
                    value, method = interp, "该分量按 frame_index 线性插值（两侧可信邻居、帧号连续）"
                    for va, vb, r6 in rot_groups:
                        if c not in r6:
                            continue
                        own, other = (va, vb) if c in va else (vb, va)
                        others = [x for x in own if x != c]
                        if not (np.isfinite(M[i, others]).all() and np.isfinite(M[i, other]).all()):
                            continue
                        pos = own.index(c)
                        cands = []
                        partner = M[i, other[pos]]
                        if abs(partner) > 0.2:
                            dot_rest = sum(M[i, own[q]] * M[i, other[q]] for q in range(3) if q != pos)
                            cands.append((-dot_rest / partner, "Rotation-6D 正交约束解出该分量"))
                        rest = sum(M[i, x] ** 2 for x in others)
                        if rest <= 1.0:
                            cands.append((math.copysign(math.sqrt(1.0 - rest), interp if interp != 0 else 1.0), "Rotation-6D 单位范数约束解出该分量"))
                        best = None
                        for v, meth in cands:
                            row = M[i, r6].copy()
                            row[r6.index(c)] = v
                            n1, n2 = np.linalg.norm(row[:3]), np.linalg.norm(row[3:])
                            dev = max(abs(n1 - 1), abs(n2 - 1), abs(float(np.dot(row[:3], row[3:]))))
                            if best is None or dev < best[0]:
                                best = (dev, v, meth)
                        if best is not None and best[0] <= self.thr["rot6d_tol"]:
                            value, method = best[1], best[2]
                        else:
                            fail = f"行 {i} Rotation-6D 分量无法在容差内由约束唯一确定"
                    if fail:
                        break
                    new[i, c] = value
                    cells[(int(i), int(c))] = float(value)
                    logs.append((int(i), c, M[i, c], method))
                if fail:
                    break
            if not fail:
                for i in np.flatnonzero(bad):
                    for _, _, r6 in rot_groups:
                        row = new[i, r6]
                        dev = max(abs(np.linalg.norm(row[:3]) - 1), abs(np.linalg.norm(row[3:]) - 1), abs(float(np.dot(row[:3], row[3:]))))
                        if not np.isfinite(dev) or dev > self.thr["rot6d_tol"]:
                            fail = f"行 {i} 补值后 Rotation-6D 偏差 {dev:.2e} 超出容差"
                            break
                    if fail:
                        break
            if fail:
                self.rejected.append(f"{key} 稀疏补值：{fail}")
                self._candidate(code, f"{key} 补 {int((~np.isfinite(M[bad])).sum())} 个非有限分量", "稀疏 NaN", fail)
                continue
            name = self.feats["field_names"][key]
            old_rows = self._load().column(name).to_pylist()
            self._write_cells(name, cells)
            cast = self._cast_for(name)
            for i, c, o, method in logs:
                self._log(i, f"{name}[{c}]", old_rows[i][c], cast(cells[(i, int(c))]), method, code)
            self.applied.append(f"{key} 仅补 {len(cells)} 个非有限分量（单元格级写回，保持 {self._load().schema.field(name).type.value_type}），其余分量不变")

    def isolated_spikes(self) -> None:
        """Single-frame position spikes outside the envelope whose trusted, frame-consecutive neighbours agree."""
        if "J_POSITION_ENVELOPE" not in self._codes():
            return
        env = self.thr["position_envelope"]
        fi = self.feats["frame_index"]
        fi_ok = _is_int(fi) & (fi >= 0)
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
            cells: dict[tuple[int, int], float] = {}
            fail = ""
            for i in rows:
                if i == 0 or i == n - 1 or not (fin[i - 1] and fin[i + 1]) or out[i - 1].any() or out[i + 1].any():
                    fail = "越界帧不是两侧有效的孤立单帧"
                    break
                if not (fi_ok[i - 1 : i + 2].all() and np.all(np.diff(fi[i - 1 : i + 2]) == 1)):
                    fail = "越界帧两侧帧号不连续"
                    break
                other_codes = (self.res.row_codes[i - 1] | self.res.row_codes[i + 1]) - {"J_SPIKE_JUMP", "J_STATE_ACTION_MISMATCH"}
                if _UNTRUSTED & other_codes:
                    fail = "越界帧邻居带有其他数值问题码"
                    break
                for j, c in enumerate(pos_cols):
                    if out[i, j]:
                        if abs(M[i + 1, c] - M[i - 1, c]) > self.thr["state_step_max"]:
                            fail = "两侧邻居本身差异过大"
                            break
                        cells[(int(i), int(c))] = (M[i - 1, c] + M[i + 1, c]) / 2.0
                if fail:
                    break
            if fail or not cells:
                self.rejected.append(f"{key} 位置越界不满足孤立单帧条件（{fail or '无'}），不插值")
                continue
            name = self.feats["field_names"][key]
            self._write_cells(name, cells)
            cast = self._cast_for(name)
            for (i, c), v in cells.items():
                self._log(i, f"{name}[{c}]", float(M[i, c]), cast(v), "孤立单帧越界：取前后帧均值（仅越界分量）", "J_POSITION_ENVELOPE")
            self.applied.append(f"{key} 孤立越界分量 {len(cells)} 个取邻帧均值")

    def metadata(self) -> None:
        if "T_METADATA_LENGTH_MISMATCH" in self.res.ep_codes:
            meta = self.ds.episodes_meta.get(self.ep, {})
            self.meta_patch = {"length": int(self.feats["n"])}
            self.audit.append({"episode_index": self.ep, "row": -1, "field": "episodes.jsonl.length",
                               "original_value": _fmt(meta.get("length")), "repaired_value": str(self.feats["n"]),
                               "method": "按实际行数更新元数据（元数据补丁，不改 Parquet）", "repair_code": "T_METADATA_LENGTH_MISMATCH"})
            self.applied.append("episodes.jsonl length 更新为实际行数（元数据补丁）")

    def candidates_only(self) -> None:
        """Issues with a plausible but not uniquely evidenced repair: proposal only."""
        m = self.res.metrics
        if "S_VISUAL_KINEMATIC_LAG" in self.res.ep_codes and m.get("vk_common_offset") is not None:
            d = int(m["vk_common_offset"])
            self._candidate("S_VISUAL_KINEMATIC_LAG", f"state/actions 相对图像平移 {d:+d} 帧并裁去 {abs(d)} 帧",
                            self.res.ep_codes["S_VISUAL_KINEMATIC_LAG"], "统计时滞证据，非硬件时钟；平移会改变全部本体样本，需人工确认")
        if "S_CAMERA_LAG_SUSPECT" in self.res.ep_codes:
            self._candidate("S_CAMERA_LAG_SUSPECT", "复核该路相机的采集时间戳", self.res.ep_codes["S_CAMERA_LAG_SUSPECT"], "单路统计证据，不自动改动")
        if "J_ROT6D_INVALID" in self.res.all_codes():
            self._candidate("J_ROT6D_INVALID", "Gram–Schmidt 重新正交化 Rotation-6D", "数学约束违反", "会改动全部相关分量，非唯一证据")

    # ------------------------------------------------------------------ run
    def run(self) -> bool:
        if not self.feats.get("read_ok") or self.feats.get("n", 0) == 0:
            return False
        if "C_SCHEMA_MISSING_FIELD" in self.res.ep_codes:
            self.rejected.append("必需字段缺失：文件级隔离，不做数据改写")
            return False
        for step in (self.schema_dtype, self.timestamps, self.index, self.episode_index, self.task_index,
                     self.stream_shift, self.sparse_numeric, self.isolated_spikes, self.metadata, self.candidates_only):
            step()
        return self.table is not None and self.table is not self.source

    def write(self, out_path: Path, forbidden: set[str] | None = None) -> bool:
        """Atomic write + read-back verification. Returns False (and writes nothing) when verification fails."""
        import pyarrow.parquet as pq

        out_path = Path(out_path)
        real = os.path.realpath(out_path)
        if forbidden and real in forbidden:
            raise PermissionError(f"refusing to write onto an input file: {out_path}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_name(out_path.name + f".tmp{os.getpid()}")
        pq.write_table(self.table, tmp, compression="snappy")
        try:
            back = pq.read_table(tmp)
            self.verify_problems = verify_copy(self.source, back, self.audit)
        except Exception as exc:  # noqa: BLE001
            self.verify_problems = [f"回读失败 {type(exc).__name__}: {exc}"]
        if self.verify_problems:
            tmp.unlink(missing_ok=True)
            return False
        os.replace(tmp, out_path)
        return True
