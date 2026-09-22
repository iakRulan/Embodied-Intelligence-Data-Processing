# -*- coding: utf-8 -*-
"""Five-dimension 0–100 scorecard with hard gates.

Each issue code is assigned to exactly one dimension (no code is counted in
two dimensions).  This is not a causal de-duplication: one physical defect
can still move two *different* codes (e.g. a black screen is C_IMAGE_SCREEN
and lowers visual diversity), which is reported as a multi-effect.

  结构完整性  file/schema/dtype/field shape/length/dimension/decode/shape
  时序        remaining T_ codes
  同步        S_ codes
  内容        camera picture quality (screen/duplicate/blur) + joint values
  训练价值    V_ codes, value proxies and cross-episode redundancy; NaN when a
              component (motion / any camera) is not evaluable – a missing
              camera is never reported as a full value score
"""
from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np

from . import config

WEIGHTS = {"structure": 0.25, "temporal": 0.20, "sync": 0.20, "content": 0.20, "value": 0.15}
STRUCT_CODES = {"C_FILE_UNREADABLE", "C_SCHEMA_MISSING_FIELD", "C_EMPTY_EPISODE", "C_PROCESSING_ERROR", "C_SCHEMA_DTYPE_MISMATCH", "C_MODALITY_UNSUPPORTED",
                "C_FIELD_INVALID", "J_VALUE_UNPARSEABLE", "T_METADATA_LENGTH_MISMATCH",
                "J_DIM_MISMATCH", "C_IMAGE_DECODE", "C_IMAGE_SHAPE"}


def _dim(code: str) -> str:
    if code in STRUCT_CODES:
        return "structure"
    p = code.split("_")[0]
    return {"T": "temporal", "S": "sync", "C": "content", "J": "content", "V": "value"}[p]


def score_parts(row_codes: list[Iterable[str]], ep_codes: dict[str, str], metrics: dict[str, Any],
                redundancy: float = 0.0) -> dict[str, Any]:
    """Score from row-code sets, episode codes and metrics (usable before and after dataset-level dedup).

    redundancy: share of the episode's rows that governance dropped because an
    identical all-modality row is kept in another episode (marginal value lost).
    """
    rows = [set(s) for s in row_codes]
    n = len(rows)
    codes = set(ep_codes)
    for s in rows:
        codes |= s
    out: dict[str, Any] = {}
    fatal = {"C_FILE_UNREADABLE", "C_EMPTY_EPISODE", "C_PROCESSING_ERROR"} & set(ep_codes)
    if fatal:
        for d in WEIGHTS:
            out[f"{d}_score"] = 0.0
        out["value_score_partial"] = 0.0
        out["overall_score"] = 0.0
        out["score_gate"] = "文件不可读/空轨迹/处理异常"
        return out
    for d in ("structure", "temporal", "sync", "content"):
        hit = np.array([any(_dim(c) == d for c in s) for s in rows]) if n else np.zeros(0, bool)
        frac = float(hit.mean()) if hit.size else 0.0
        pen = sum((0.5 if config.blocks_training(c) else 0.1) for c in ep_codes if config.CODES[c][2] == "ep" and _dim(c) == d)
        pen += (1.5 if d != "sync" else 2.0) * frac
        out[f"{d}_score"] = round(100.0 * max(0.0, 1.0 - min(1.0, pen)), 2)
    v = 100.0
    v -= 35 * ("V_HIGH_IDLE" in codes) + 40 * ("V_LOW_INTERACTION" in codes) + 25 * ("V_LOW_VISUAL_DIVERSITY" in codes)
    v -= 50 * bool({"V_STATE_FREEZE", "V_ACTION_FREEZE", "V_LOW_INFORMATION"} & codes)
    v -= 50 * max(0.0, min(1.0, float(redundancy)))
    v = round(max(0.0, v), 2)
    out["value_score_partial"] = v if metrics.get("value_partially_evaluable", metrics.get("value_evaluable")) else math.nan
    out["value_score"] = v if metrics.get("value_evaluable") else math.nan
    num = sum(WEIGHTS[d] * out[f"{d}_score"] for d in WEIGHTS if not math.isnan(out[f"{d}_score"]))
    den = sum(WEIGHTS[d] for d in WEIGHTS if not math.isnan(out[f"{d}_score"]))
    overall = num / den if den else 0.0
    gate = ""
    if "C_MODALITY_UNSUPPORTED" in ep_codes:
        overall, gate = min(overall, 25.0), "视频模态未验收，总分上限 25；不能作为训练准入证明"
    elif "C_SCHEMA_MISSING_FIELD" in ep_codes:
        overall, gate = min(overall, 25.0), "必需字段缺失，总分上限 25"
    elif "S_STREAM_MISSING" in ep_codes:
        overall, gate = min(overall, 40.0), "整路相机缺失，总分上限 40"
    out["overall_score"] = round(overall, 2)
    out["score_gate"] = gate
    return out


def score(res) -> dict[str, Any]:
    return score_parts(res.row_codes, res.ep_codes, res.metrics)


def status_codes(codes: Iterable[str]) -> tuple[str, str]:
    """(status, strongest evidence tier among defect codes)."""
    codes = list(codes)
    defects = [c for c in codes if config.category(c) != config.CAT_V]
    if defects:
        tier = min((config.tier(c) for c in defects), key=lambda t: config.TIER_ORDER[t])
        return "需复核", config.TIER_CN[tier]
    if codes:
        return "价值提示", ""
    return "通过", ""


def status(res) -> tuple[str, str]:
    return status_codes(res.all_codes())
