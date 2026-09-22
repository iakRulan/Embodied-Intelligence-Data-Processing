"""Add confidence tiers, competition-coverage mapping and candidate-repair
disposition on top of the frozen v2 detector outputs.

This script never re-runs detection; it only post-processes
``outputs/test_quality_report.csv`` so the validated scores stay untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
from dataset_paths import OUT_DIR

# Confidence tier per issue code.  Tier meaning:
#   deterministic – schema/physics/byte-level facts; no statistical threshold involved
#   high          – strong evidence with a small boundary risk
#   threshold     – reference-calibrated statistical threshold; boundary samples need review
TIER_MAP = {
    # deterministic: file/schema/index facts
    "C_PARQUET_CORRUPT": "deterministic",
    "C_SCHEMA_MISSING_IMAGE_COLUMN": "deterministic",
    "C_FILE_READ_ERROR": "deterministic",
    "C_IMAGE_READ_ERROR": "deterministic",
    "C_VALUE_READ_ERROR": "deterministic",
    "C_IMAGE_DECODE": "deterministic",
    "C_IMAGE_SHAPE": "deterministic",
    "C_IMAGE_DUPLICATE": "deterministic",
    "T_TIMESTAMP_NON_MONOTONIC": "deterministic",
    "T_FRAME_ORDER_ERROR": "deterministic",
    "T_FRAME_INDEX_DISCONTINUITY": "deterministic",
    "T_GLOBAL_INDEX_DISCONTINUITY": "deterministic",
    "T_METADATA_LENGTH_MISMATCH": "deterministic",
    "T_TIMESTAMP_UNIT_MISMATCH": "deterministic",
    "S_TASK_INDEX_INVALID": "deterministic",
    "S_TASK_INDEX_MISSING": "deterministic",
    "S_TASK_SWITCH_WITHIN_EPISODE": "deterministic",
    "S_EPISODE_INDEX_MISMATCH": "deterministic",
    "V_NONFINITE": "deterministic",
    # high: strong pixel/physics evidence
    "C_IMAGE_SCREEN": "high",
    "V_PHYSICAL_RANGE": "high",
    "S_IMAGE_PATH_MISMATCH": "high",
    "S_MODALITY_INDEX_ALIGNMENT": "high",
    "S_MODALITY_COVERAGE_GAP": "high",
    "S_VISUAL_FREEZE_UNDER_MOTION": "high",
    # threshold: statistical, boundary-sensitive
    "T_TIMESTAMP_JITTER": "threshold",
    "T_TIMESTAMP_JITTER_OR_DRIFT": "threshold",
    "T_TIMESTAMP_GAP": "threshold",
    "T_DROPPED_OR_GAPPED_FRAMES": "threshold",
    "C_IMAGE_BLUR": "threshold",
    "V_STATE_ACTION_MISMATCH": "threshold",
    "V_SPIKE_JUMP": "threshold",
    "V_STATE_FREEZE": "threshold",
    "V_ACTION_FREEZE": "threshold",
    "V_LOW_INFORMATION_TRAJECTORY": "threshold",
}
TIER_RANK = {"deterministic": 3, "high": 2, "threshold": 1}
TIER_LABEL = {"deterministic": "确定性", "high": "高置信", "threshold": "阈值型需复核"}

# Disposition of the 15 auto-repair candidates after case-by-case review.
REPAIRED = {6, 11, 13, 25, 54, 73, 75}
DISPOSITION = {
    6: ("已修复", "task_index 非法→meta 唯一任务"),
    11: ("已修复", "轨迹内 task 切换→meta 唯一任务"),
    13: ("已修复", "timestamp 抖动→frame/fps 重建"),
    25: ("已修复", "timestamp 单位不一致→frame/fps 重建"),
    54: ("已修复", "单点位置突跳→可信邻域线性插值"),
    73: ("已修复", "timestamp 抖动→frame/fps 重建"),
    75: ("已修复", "timestamp 抖动→frame/fps 重建"),
    8: ("保留候选", "图像模糊不可凭空恢复；22/256 已出 mask，切段使用，不生成假图"),
    9: ("保留候选", "图像模糊不可凭空恢复；按不伪造视觉内容原则仅出 quality_mask 与回采建议"),
    35: ("保留候选", "图像模糊+重复；不生成假图，mask 后人工决定剔除/回采"),
    47: ("保留候选", "图像模糊不可凭空恢复；按不伪造视觉内容原则仅出 quality_mask 与回采建议"),
    84: ("保留候选", "图像模糊不可凭空恢复；按不伪造视觉内容原则仅出 quality_mask 与回采建议"),
    58: ("保留候选", "突跳帧经复核为真实快速运动成分（该帧接近后邻帧），机械插值会破坏真实数据，需人工确认"),
    41: ("保留候选", "path 错位根因是图像内容真实重复（14/14 重复行 bytes 全同）+中段 15 帧段跳变；重写 path 会掩盖真实缺陷"),
    87: ("保留候选", "path 错位根因是图像内容真实重复（29/29 重复行 bytes 全同）+中段 30 帧段跳变；重写 path 会掩盖真实缺陷"),
}


def episode_tier(codes: str) -> str:
    if not isinstance(codes, str) or not codes.strip():
        return ""
    tiers = [TIER_MAP.get(c.strip(), "threshold") for c in codes.split(";") if c.strip()]
    if not tiers:
        return ""
    return max(tiers, key=lambda t: TIER_RANK[t])


def main() -> None:
    report = pd.read_csv(OUT_DIR / "test_quality_report.csv", encoding="utf-8-sig")
    report["confidence_tier"] = report["issue_codes"].apply(episode_tier)
    report["confidence_label"] = report["confidence_tier"].map(TIER_LABEL).fillna("通过")
    report.to_csv(OUT_DIR / "test_quality_report.csv", index=False, encoding="utf-8-sig")

    flagged = report[report["status"] != "通过"]
    tier_counts = flagged["confidence_label"].value_counts().to_dict()
    tier_summary = {
        "flagged_episodes": int(len(flagged)),
        "tier_counts": {TIER_LABEL.get(k, k): int(v) for k, v in
                        flagged["confidence_tier"].value_counts().items()},
        "tier_definition": {
            "确定性": "schema/物理/字节级事实判定，不依赖统计阈值（如文件损坏、NaN、时间戳倒退、相邻重复、task 非法）",
            "高置信": "强证据、边界风险小（如黑屏/过曝像素统计、视觉冻结而本体在动、物理范围违规、path-frame 不一致、payload 缺失）",
            "阈值型需复核": "参考集校准的统计阈值判定，边界样本建议人工复核（如模糊、抖动、突跳、state-action 残差、低信息代理）",
        },
        "label_note": "置信度描述算法标记的证据强度，不改变质量分；测试集仍无官方真值标签。",
    }
    (OUT_DIR / "confidence_summary.json").write_text(
        json.dumps(tier_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    tier_rows = [
        {"confidence_tier": t, "confidence_label": TIER_LABEL[t],
         "episode_count": int((flagged["confidence_tier"] == t).sum()),
         "share_of_flagged": round(float((flagged["confidence_tier"] == t).mean()), 4),
         "definition": tier_summary["tier_definition"][TIER_LABEL[t]]}
        for t in ["deterministic", "high", "threshold"]
    ]
    pd.DataFrame(tier_rows).to_csv(OUT_DIR / "confidence_tier_summary.csv", index=False, encoding="utf-8-sig")

    # Candidate-repair disposition matrix for the 15 auto-repair candidates.
    queue = pd.read_csv(OUT_DIR / "repair_queue.csv", encoding="utf-8-sig")
    cand = queue[queue["repair_mode"] == "自动修复候选"].copy()
    cand["disposition"] = cand["episode_index"].map(lambda e: DISPOSITION.get(int(e), ("保留候选", ""))[0])
    cand["disposition_reason"] = cand["episode_index"].map(lambda e: DISPOSITION.get(int(e), ("", ""))[1])
    cols = ["episode_index", "issue_codes", "overall_quality_score", "disposition", "disposition_reason"]
    cand[cols].sort_values("episode_index").to_csv(
        OUT_DIR / "repair_candidate_disposition.csv", index=False, encoding="utf-8-sig")

    print(json.dumps(tier_summary, ensure_ascii=False, indent=2))
    print("\n=== 15 条自动修复候选处置矩阵 ===")
    print(cand[cols].sort_values("episode_index").to_string(index=False))
    print(f"\n已修复 {len(REPAIRED)}/15；保留候选 {15 - len(REPAIRED)}/15（均附不修复理由）")


if __name__ == "__main__":
    main()
