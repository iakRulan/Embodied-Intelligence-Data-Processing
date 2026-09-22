# -*- coding: utf-8 -*-
"""Five-dimension 0–100 scorecard with hard gates (no double counting).

Dimensions take disjoint evidence:
  结构完整性  file/schema/length/dimension/decode/shape
  时序        remaining T_ codes
  同步        S_ codes
  内容        camera picture quality (screen/duplicate/blur) + joint values (J_ except dims)
  训练价值    V_ codes and value proxies (NaN = 不可评估, weight renormalised)
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from . import config
from .detect import Result

WEIGHTS = {"structure": 0.25, "temporal": 0.20, "sync": 0.20, "content": 0.20, "value": 0.15}
STRUCT_CODES = {"C_FILE_UNREADABLE", "C_SCHEMA_MISSING_FIELD", "C_EMPTY_EPISODE", "T_METADATA_LENGTH_MISMATCH",
                "J_DIM_MISMATCH", "C_IMAGE_DECODE", "C_IMAGE_SHAPE"}


def _dim(code: str) -> str:
    if code in STRUCT_CODES:
        return "structure"
    p = code.split("_")[0]
    return {"T": "temporal", "S": "sync", "C": "content", "J": "content", "V": "value"}[p]


def score(res: Result) -> dict[str, Any]:
    codes = res.all_codes()
    out: dict[str, Any] = {}
    if "C_FILE_UNREADABLE" in res.ep_codes or "C_EMPTY_EPISODE" in res.ep_codes:
        for d in WEIGHTS:
            out[f"{d}_score"] = 0.0
        out["overall_score"] = 0.0
        out["score_gate"] = "文件不可读/空轨迹"
        return out
    row_frac = {d: 0.0 for d in WEIGHTS}
    for d in ("structure", "temporal", "sync", "content"):
        hit = np.array([any(_dim(c) == d for c in s) for s in res.row_codes]) if res.n else np.zeros(0, bool)
        row_frac[d] = float(hit.mean()) if hit.size else 0.0
    ep_pen = {d: 0.0 for d in WEIGHTS}
    for c in res.ep_codes:
        if config.CODES[c][2] != "ep":
            continue
        d = _dim(c)
        ep_pen[d] += 0.5 if config.blocks_training(c) else 0.1
    for d in ("structure", "temporal", "sync", "content"):
        pen = (1.5 if d != "sync" else 2.0) * row_frac[d] + ep_pen[d]
        out[f"{d}_score"] = round(100.0 * max(0.0, 1.0 - min(1.0, pen)), 2)
    if res.metrics.get("value_evaluable"):
        v = 100.0
        v -= 35 * ("V_HIGH_IDLE" in codes) + 40 * ("V_LOW_INTERACTION" in codes) + 25 * ("V_LOW_VISUAL_DIVERSITY" in codes)
        v -= 50 * bool({"V_STATE_FREEZE", "V_ACTION_FREEZE", "V_LOW_INFORMATION"} & set(codes))
        out["value_score"] = round(max(0.0, v), 2)
    else:
        out["value_score"] = math.nan
    num = sum(WEIGHTS[d] * out[f"{d}_score"] for d in WEIGHTS if not math.isnan(out[f"{d}_score"]))
    den = sum(WEIGHTS[d] for d in WEIGHTS if not math.isnan(out[f"{d}_score"]))
    overall = num / den if den else 0.0
    gate = ""
    if "C_SCHEMA_MISSING_FIELD" in res.ep_codes:
        overall, gate = min(overall, 25.0), "必需字段缺失，总分上限 25"
    elif "S_STREAM_MISSING" in res.ep_codes:
        overall, gate = min(overall, 40.0), "整路相机缺失，总分上限 40"
    out["overall_score"] = round(overall, 2)
    out["score_gate"] = gate
    return out


def status(res: Result) -> tuple[str, str]:
    """(status, strongest evidence tier among defect codes)."""
    codes = res.all_codes()
    defects = [c for c in codes if config.category(c) != config.CAT_V]
    if defects:
        tier = min((config.tier(c) for c in defects), key=lambda t: config.TIER_ORDER[t])
        return "需复核", config.TIER_CN[tier]
    if codes:
        return "价值提示", ""
    return "通过", ""
