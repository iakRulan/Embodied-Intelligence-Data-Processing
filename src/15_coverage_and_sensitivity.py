"""Generate the competition-coverage matrix and score-weight sensitivity
analysis.  Read-only with respect to detector outputs.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
from dataset_paths import OUT_DIR

DIMS = ["structural_score", "temporal_score", "sync_score", "content_score", "value_score"]
BASE_W = np.array([0.25, 0.20, 0.20, 0.20, 0.15])
DIM_LABEL = ["结构", "时序", "同步", "内容", "价值"]

COVERAGE = [
    ("时序", "掉帧/缺帧", "已覆盖", "T_DROPPED_OR_GAPPED_FRAMES（帧级 T_TIMESTAMP_GAP）", "1.5×标称间隔阈值 + frame_index 连续性"),
    ("时序", "帧率不稳/抖动", "已覆盖", "T_TIMESTAMP_JITTER_OR_DRIFT（帧级 T_TIMESTAMP_JITTER）", "Δt 中位数/MAD/绝对误差，参考集校准"),
    ("时序", "时间戳倒退", "已覆盖", "T_TIMESTAMP_NON_MONOTONIC", "Δt≤0 计数，确定性判定"),
    ("时序", "乱序", "已覆盖", "T_FRAME_ORDER_ERROR / T_GLOBAL_INDEX_DISCONTINUITY", "frame_index / 全局 index 差分"),
    ("时序", "消息生成与记录时间偏差", "数据条件不满足", "—", "数据仅含单一 timestamp，无独立生成时间字段；接口已预留"),
    ("同步", "多模态时间戳偏差", "部分覆盖 + 负向实测", "S_IMAGE_PATH_MISMATCH / S_MODALITY_INDEX_ALIGNMENT；像素运动互相关", "索引层对齐仍成立；真实像素运动 vs state 步长 ±6 帧互相关：参考集/测试集 lag0 命中率均为 0.00，分布重合，不能支持真实时滞测量"),
    ("同步", "时钟漂移", "实测不可测（负向结论）", "像素运动互相关（含去趋势）", "无独立传感器时钟；去趋势后 |lag| 均值参考 5.20 / 测试 4.91，与干净参考集重合，故不做伪漂移检测器"),
    ("同步", "部分传感器晚启动/早停止", "部分覆盖", "S_MODALITY_COVERAGE_GAP", "payload 缺失可捕获晚启动/早停止的覆盖缺口"),
    ("同步", "多模态覆盖不全", "已覆盖", "S_MODALITY_COVERAGE_GAP", "三路相机逐帧 payload 计数"),
    ("内容/结构", "相机黑屏/花屏/遮挡", "已覆盖（分项披露）", "C_IMAGE_SCREEN / C_IMAGE_BLUR / S_VISUAL_FREEZE_UNDER_MOTION", "黑屏：dark/bright≥0.95 直接判定。遮挡：三路像素变化≈0 且本体运动处于轨迹上四分位，连续≥20 帧（参考集 LOEO 0/20，测试集 4/88 均已在 61 内）。花屏：噪声残差+色度散布通道已建，51,027 张图中仅 1 帧超上界，判定本数据集不含花屏样本"),
    ("内容/结构", "IMU/关节非法值/超标", "已覆盖", "V_NONFINITE / V_PHYSICAL_RANGE", "本数据集无 IMU；关节量以 state/actions 20 维表示，做有限性/范围/Rotation-6D 约束检查"),
    ("内容/结构", "协议/文件字段缺失/损坏", "已覆盖", "C_PARQUET_CORRUPT / C_SCHEMA_MISSING_IMAGE_COLUMN / C_FILE_READ_ERROR 等", "Parquet 可读性 + 必需字段 schema 门禁"),
    ("数据价值", "高价值片段缺失", "已覆盖（代理）", "低交互运动比例 / 高价值代理不足", "参考集校准的运动量分位数代理"),
    ("数据价值", "低价值片段占比过高", "已覆盖（代理）", "高 idle 帧占比 / 最长低信息段", "state/action idle 分位数阈值"),
    ("数据价值", "数据分布不均衡", "已覆盖（代理）", "任务均衡度 min/max=0.9333、归一化熵 0.9991", "task_index 分布统计"),
    ("数据价值", "场景覆盖不全", "部分覆盖（代理）", "三路 aHash 视觉多样性", "无场景真值标签，以视觉哈希多样性代理"),
    ("数据价值", "0-100 多维评分卡", "已覆盖", "五维评分 + 总分 + 硬门禁", "Q=0.25·结构+0.20·时序+0.20·同步+0.20·内容+0.15·价值"),
]


def weight_sensitivity(report: pd.DataFrame) -> dict:
    X = report[DIMS].to_numpy(float)
    base = X @ BASE_W
    base_rank = pd.Series(base).rank(ascending=False).to_numpy()
    flagged_base = set(report.loc[base < 85, "episode_index"].astype(int))
    low10_base = set(report.assign(q=base).nsmallest(10, "q")["episode_index"].astype(int))

    results = []
    rng_scenarios = []
    for i in range(5):
        for delta in (-0.05, 0.05):
            w = BASE_W.copy()
            w[i] += delta
            w = np.clip(w, 0.01, None)
            w = w / w.sum()
            rng_scenarios.append((DIM_LABEL[i], delta, w))
    # plus an equal-weight extreme scenario
    rng_scenarios.append(("等权", 0.0, np.full(5, 0.2)))

    spearmans, jaccards_flag, jaccards_low10 = [], [], []
    for name, delta, w in rng_scenarios:
        q = X @ w
        rank = pd.Series(q).rank(ascending=False).to_numpy()
        sp = float(pd.Series(base_rank).corr(pd.Series(rank), method="spearman"))
        flagged = set(report.loc[q < 85, "episode_index"].astype(int))
        low10 = set(report.assign(q=q).nsmallest(10, "q")["episode_index"].astype(int))
        jf = len(flagged & flagged_base) / max(len(flagged | flagged_base), 1)
        jl = len(low10 & low10_base) / max(len(low10 | low10_base), 1)
        spearmans.append(sp)
        jaccards_flag.append(jf)
        jaccards_low10.append(jl)
        results.append({
            "scenario": f"{name}{'+' if delta > 0 else ''}{delta:.2f}" if delta else name,
            "weights": "/".join(f"{x:.3f}" for x in w),
            "spearman_rank_corr": round(sp, 4),
            "flagged_set_jaccard": round(jf, 4),
            "bottom10_jaccard": round(jl, 4),
        })
    return {
        "scenarios": results,
        "summary": {
            "spearman_min": round(min(spearmans), 4),
            "spearman_mean": round(float(np.mean(spearmans)), 4),
            "flagged_jaccard_min": round(min(jaccards_flag), 4),
            "bottom10_jaccard_min": round(min(jaccards_low10), 4),
        },
        "interpretation": "对每维权重 ±0.05 及等权共 11 种情景重算总分；排名 Spearman 与标记集合 Jaccard 越接近 1，结论对权重选择越不敏感。",
    }


def main() -> None:
    cov = pd.DataFrame(COVERAGE, columns=["赛题类别", "赛题子条目", "覆盖状态", "对应 issue code / 指标", "方法或限制说明"])
    cov.to_csv(OUT_DIR / "competition_coverage_matrix.csv", index=False, encoding="utf-8-sig")
    md_lines = ["| 赛题类别 | 赛题子条目 | 覆盖状态 | 对应 issue code / 指标 | 方法或限制说明 |",
                "|---|---|---|---|---|"]
    for row in COVERAGE:
        md_lines.append("| " + " | ".join(row) + " |")
    (OUT_DIR / "competition_coverage_matrix.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    report = pd.read_csv(OUT_DIR / "test_quality_report.csv", encoding="utf-8-sig")
    sens = weight_sensitivity(report)
    pd.DataFrame(sens["scenarios"]).to_csv(OUT_DIR / "weight_sensitivity.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "weight_sensitivity.json").write_text(json.dumps(sens, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=== 覆盖状态统计 ===")
    print(cov["覆盖状态"].value_counts().to_string())
    print("\n=== 权重敏感性 ===")
    print(pd.DataFrame(sens["scenarios"]).to_string(index=False))
    print("\nsummary:", json.dumps(sens["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
