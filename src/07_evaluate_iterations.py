"""Create auditable ablation/iteration metrics from the final issue report.

The hidden test set has no released labels, so this is an additive ablation
table rather than a claimed precision/recall benchmark.  Each row reuses the
same final evidence and progressively enables detector families:
structure/file gate -> temporal + value -> full multimodal fusion.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
from dataset_paths import OUT_DIR


def contains_any(text: pd.Series, prefixes: tuple[str, ...]) -> pd.Series:
    pattern = r"(?:^|;)(?:" + "|".join(prefixes) + r")[^;]*(?:;|$)"
    return text.fillna("").astype(str).str.contains(pattern, regex=True)


def row_has_stage_codes(text: pd.Series, stage: str) -> pd.Series:
    text = text.fillna("").astype(str)
    if stage == "结构文件门禁":
        return contains_any(text, ("T_METADATA_LENGTH_MISMATCH", "C_PARQUET_CORRUPT", "C_SCHEMA_MISSING_IMAGE_COLUMN", "C_FILE_READ_ERROR", "C_VALUE_READ_ERROR", "C_IMAGE_READ_ERROR"))
    if stage == "时序+数值":
        return contains_any(text, ("T_", "V_", "C_PARQUET_CORRUPT", "C_SCHEMA_MISSING_IMAGE_COLUMN", "C_FILE_READ_ERROR", "C_VALUE_READ_ERROR", "C_IMAGE_READ_ERROR"))
    return text.ne("")


def main() -> None:
    ref = pd.read_csv(OUT_DIR / "reference_quality_report.csv", encoding="utf-8-sig")
    test = pd.read_csv(OUT_DIR / "test_quality_report.csv", encoding="utf-8-sig")
    frame = pd.read_csv(OUT_DIR / "test_frame_flags.csv", encoding="utf-8-sig")
    stages = ["结构文件门禁", "时序+数值", "全模态融合"]
    rows: list[dict[str, object]] = []
    for stage in stages:
        ref_mask = row_has_stage_codes(ref["issue_codes"], stage)
        test_mask = row_has_stage_codes(test["issue_codes"], stage)
        frame_mask = row_has_stage_codes(frame["issue_codes"], stage)
        rows.append(
            {
                "iteration": stage,
                "enabled_modules": {
                    "结构文件门禁": "schema/Parquet/元数据长度",
                    "时序+数值": "结构文件 + timestamp/frame_index + state/actions 物理/残差/突跳",
                    "全模态融合": "时序+数值 + 三路图像解码/清晰度/屏幕/重复 + path/任务对齐",
                }[stage],
                "reference_false_positive_rate": round(float(ref_mask.mean()), 4),
                "test_flagged_episodes": int(test_mask.sum()),
                "test_flagged_episode_rate": round(float(test_mask.mean()), 4),
                "test_flagged_frames": int(frame_mask.sum()),
                "test_flagged_frame_rate": round(float(frame_mask.mean()), 4),
            }
        )
    result = pd.DataFrame(rows)
    result["episode_gain_vs_previous"] = result["test_flagged_episodes"].diff().fillna(0).astype(int)
    result["frame_gain_vs_previous"] = result["test_flagged_frames"].diff().fillna(0).astype(int)
    result.to_csv(OUT_DIR / "iteration_comparison.csv", index=False, encoding="utf-8-sig")

    validation = json.loads((OUT_DIR / "validation_summary.json").read_text(encoding="utf-8"))
    repair_validation = json.loads((OUT_DIR / "repair_validation_summary.json").read_text(encoding="utf-8"))

    # A compact metrics table for the answer document, workbook and PPT.
    category_map = {"时序": "时序", "同步": "同步", "内容/结构": "内容/结构", "数据价值": "数据价值"}
    metrics: list[dict[str, object]] = [
        {"metric": "参考集轨迹数", "value": int(len(ref)), "unit": "条", "note": "公开 clean reference"},
        {"metric": "测试集轨迹数", "value": int(len(test)), "unit": "条", "note": "89 条；隐藏异常标签未释放"},
        {"metric": "参考集内标记率", "value": round(float((ref.status == "需复核").mean()), 4), "unit": "比例", "note": "同一批参考数据参与校准，仅作集内检查"},
        {"metric": "参考集留一误报率", "value": round(float(validation["reference_leave_one_out_false_positive_rate"]), 4), "unit": "比例", "note": f'{validation["reference_leave_one_out_flagged"]}/{validation["reference_leave_one_out_n"]}；每次仅用其余 19 条校准'},
        {"metric": "测试集需复核轨迹率", "value": round(float((test.status == "需复核").mean()), 4), "unit": "比例", "note": f'{int((test.status == "需复核").sum())}/{len(test)}；算法标记，不等同官方异常真值'},
        {"metric": "逐帧定位覆盖率", "value": round(float(frame.issue_codes.fillna("").ne("").mean()), 4), "unit": "%", "note": "在 17,213 个可读结构帧上统计"},
        {"metric": "质量分均值（修复前）", "value": round(float(test.overall_quality_score.mean()), 2), "unit": "/100", "note": "五维加权"},
        {"metric": "真实修复样本通过率", "value": round(float(repair_validation["full_recheck_passed"] / repair_validation["sample_count"]), 4), "unit": "比例", "note": f'{repair_validation["full_recheck_passed"]}/{repair_validation["sample_count"]}；修复副本同版本复检'},
        {"metric": "真实修复样本平均增益", "value": round(float(repair_validation["mean_score_gain_measured"]), 2), "unit": "分", "note": f'{repair_validation["mean_score_before"]:.2f} → {repair_validation["mean_score_after"]:.2f}'},
        {"metric": "合成异常集轨迹级 F1", "value": round(float(validation["synthetic_episode_f1"]), 4), "unit": "比例", "note": "确定性注入压力测试，不代表隐藏测试集真值表现"},
    ]
    for label, column in category_map.items():
        metrics.append({"metric": f"{label}需复核轨迹数", "value": int(test.issue_categories.fillna("").str.contains(column).sum()), "unit": "条", "note": "算法标记；按轨迹去重，类别可重叠"})
    repair_counts = test.repair_mode.value_counts()
    for mode, count in repair_counts.items():
        metrics.append({"metric": f"修复策略：{mode}", "value": int(count), "unit": "条", "note": "由异常比例和文件可恢复性决定"})
    pd.DataFrame(metrics).to_csv(OUT_DIR / "metrics_summary.csv", index=False, encoding="utf-8-sig")

    payload = {
        "iteration_table": result.to_dict(orient="records"),
        "label_status": "hidden test labels unavailable",
        "validation_summary": validation,
        "actual_repair_validation": repair_validation,
        "interpretation": "测试集仅报告算法标记率；泛化误报用参考集留一验证，P/R/F1 仅来自确定性合成异常压力测试。",
    }
    (OUT_DIR / "iteration_evaluation.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()
