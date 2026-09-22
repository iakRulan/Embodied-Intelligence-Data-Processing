"""Export the governance decision matrix: defect type -> action -> auto-repair
condition -> verification -> test-set instance.  Static content derived from
the v2 detector's repair policy (src/06) and the validated repair outcomes.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
from dataset_paths import OUT_DIR

ROWS = [
    # 缺陷类别, issue code, 治理动作, 自动修复条件, 验证方式, 测试集实例/结果
    ("时序", "T_TIMESTAMP_JITTER_OR_DRIFT / T_TIMESTAMP_UNIT_MISMATCH",
     "按 frame_index/fps 重建 timestamp，原始时间戳保留备查",
     "frame_index 连续且无倒退（修复函数内置硬校验，不满足即拒绝执行）",
     "同版本检测器复检 + Δt 曲线对比 + SHA-256 + quality_mask",
     "ep13/25/73/75 已修复，4/4 复检通过"),
    ("时序", "T_METADATA_LENGTH_MISMATCH",
     "按实际有效行数更新 episodes.jsonl length；原 Parquet 只复制不改行",
     "帧级检测全绿，仅 metadata 与行数不一致", "同版本复检",
     "ep60 已修复（185→232），1/1 复检通过"),
    ("时序", "T_TIMESTAMP_NON_MONOTONIC / T_DROPPED_OR_GAPPED_FRAMES / T_FRAME_ORDER_ERROR",
     "不可自动恢复：重新采集/重排序，禁止静默覆盖原始轨迹",
     "—（掉帧段信息不可凭空重建）", "隔离回采工单",
     "ep1/10/26/44/55/63/79 等，隔离回采"),
    ("同步", "S_TASK_INDEX_INVALID / S_TASK_SWITCH_WITHIN_EPISODE",
     "按 meta 唯一任务重写 task_index，逐帧审计",
     "episodes.jsonl 与 tasks.jsonl 能唯一确定任务",
     "同版本复检 + quality_mask",
     "ep6/11 已修复，2/2 复检通过"),
    ("同步", "S_IMAGE_PATH_MISMATCH / S_MODALITY_INDEX_ALIGNMENT",
     "按 frame_index 重写图像 path，并用内容 hash 复核跨模态对齐",
     "图像内容无重复/无段跳变（hash 复核通过）",
     "内容 hash 复核 + 同版本复检",
     "ep41/87 复核后发现内容为真实重复+段跳变，重写 path 会洗白缺陷 → 保留候选"),
    ("同步", "S_MODALITY_COVERAGE_GAP",
     "缺失 payload 不可生成；与 C_IMAGE_SHAPE 同源的缺口只删训练使用权、切出两侧干净段",
     "缺口连续且其余帧 path 无真实内容重复", "mask + 切段目录 + 同版本复检",
     "ep3/18/40/68/70/86 切段可用；ep29/48/74 先修正单流 path 偏移，缺口 3–10 行出 mask"),
    ("同步", "S_VISUAL_FREEZE_UNDER_MOTION",
     "视野冻结而本体仍在运动：按冻结区间出 mask / 切两侧干净段，不生成假图、不插值视觉内容",
     "连续冻结段已由参考集 LOEO 标定；该 4 条此前已被 C_IMAGE_SCREEN 等标记",
     "visual_freeze_intervals + quality_mask；不改变 61/89",
     "ep15/69/85/88 共 126 帧，诊断从暗帧代理升级为跨模态遮挡/相机 stall"),
    ("内容/结构", "C_IMAGE_BLUR / C_IMAGE_SCREEN / C_IMAGE_DUPLICATE / C_IMAGE_DECODE",
     "生成逐帧 quality_mask，跨模态同步剔除问题帧；短段可回采替换",
     "异常帧比例 ≤ 10% 出 mask；>10% 直接隔离回采；不生成假图",
     "quality_mask + 人工复核",
     "ep8 仅 22/256 模糊→切段使用；ep9/35/47/84 仍不造图，出 mask；长段黑屏隔离或切两侧干净段"),
    ("内容/结构", "C_IMAGE_SHAPE",
     "保留原图并核对采集/编码配置，禁止仅为过 schema 强制拉伸",
     "—", "隔离 + 配置核对工单",
     "ep77 等，隔离回采"),
    ("内容/结构", "C_PARQUET_CORRUPT / C_SCHEMA_MISSING_IMAGE_COLUMN / C_FILE_READ_ERROR 等",
     "文件级隔离，使用备份/重新导出恢复 schema；不在原文件上覆盖",
     "—（文件级损坏不可自动恢复）", "隔离 + 0 分/25 分硬门禁",
     "ep82 损坏记 0 分；ep2 缺图像列 ≤25 分"),
    ("数据价值", "V_SPIKE_JUMP / V_PHYSICAL_RANGE（稀疏数值异常）",
     "Hampel 检测 + 可信邻域线性插值，写入 quality_mask",
     "异常比例 ≤ 10% 且复核确认为非真实运动",
     "同版本复检 + 修复前后曲线 + quality_mask",
     "ep54 已修复（单点突跳插值）；ep58 复核为真实运动 → 保留候选"),
    ("数据价值", "V_NONFINITE / V_PHYSICAL_RANGE（高比例）",
     "异常比例 >10%：隔离该通道并回采，不使用模型臆造整段状态",
     "—", "隔离回采工单",
     "ep80 的 12/172 NaN 已稀疏插值并复检通过；ep17 先重建 timestamp，NaN 段出 mask 不编造状态"),
    ("数据价值", "V_STATE_FREEZE / V_ACTION_FREEZE / V_LOW_INFORMATION_TRAJECTORY",
     "传感器冻结/低信息不可从同一轨迹恢复：整段回采或训练降权",
     "—", "价值维度评分体现 + 回采建议",
     "ep0/51 训练降权；其余低信息按策略表降权或隔离"),
]


def main() -> None:
    df = pd.DataFrame(ROWS, columns=["缺陷类别", "issue code", "治理动作", "自动修复条件", "验证方式", "测试集实例/结果"])
    df.to_csv(OUT_DIR / "governance_decision_matrix.csv", index=False, encoding="utf-8-sig")
    md = ["| 缺陷类别 | issue code | 治理动作 | 自动修复条件 | 验证方式 | 测试集实例/结果 |",
          "|---|---|---|---|---|---|"]
    for r in ROWS:
        md.append("| " + " | ".join(r) + " |")
    (OUT_DIR / "governance_decision_matrix.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()
