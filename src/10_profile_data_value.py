"""Quantify training-value proxies without pretending to measure task success.

The supplied initial-round data has no success/collision labels or scene
annotations.  This module therefore reports auditable proxies relative to the
clean reference set: interaction coverage, long idle segments, visual
diversity, task balance and usable high-value coverage.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
from dataset_paths import OUT_DIR, resolve_test_root

TEST_ROOT = resolve_test_root()


def longest_true_run(mask: np.ndarray) -> int:
    best = current = 0
    for value in mask.astype(bool):
        current = current + 1 if value else 0
        best = max(best, current)
    return best


def episode_features(label: str) -> pd.DataFrame:
    structural = pd.read_csv(OUT_DIR / f"{label}_structural_episodes.csv", encoding="utf-8-sig")
    values = pd.read_csv(OUT_DIR / f"{label}_value_frames.csv", encoding="utf-8-sig")
    images = pd.read_csv(OUT_DIR / f"{label}_image_frames.csv", encoding="utf-8-sig", low_memory=False)
    flags_path = OUT_DIR / f"{label}_frame_flags.csv"
    if flags_path.exists():
        flags = pd.read_csv(flags_path, encoding="utf-8-sig")
    elif label == "test" and (OUT_DIR / "test_frame_flags.csv").exists():
        flags = pd.read_csv(OUT_DIR / "test_frame_flags.csv", encoding="utf-8-sig")
    else:
        flags = pd.DataFrame(columns=["episode_index", "row", "issue_codes"])

    values["episode_index"] = pd.to_numeric(values["dataset_episode"], errors="coerce").astype("Int64")
    values["row"] = pd.to_numeric(values["row"], errors="coerce").astype("Int64")
    images["episode_index"] = pd.to_numeric(images["dataset_episode"], errors="coerce").astype("Int64")
    images["row"] = pd.to_numeric(images["row"], errors="coerce").astype("Int64")
    if not flags.empty:
        flags["episode_index"] = pd.to_numeric(flags["episode_index"], errors="coerce").astype("Int64")
        flags["row"] = pd.to_numeric(flags["row"], errors="coerce").astype("Int64")

    state_step = pd.to_numeric(values["state_step_l2"], errors="coerce")
    action_step = pd.to_numeric(values["action_step_l2"], errors="coerce")
    if label == "reference":
        state_cut = float(state_step.quantile(0.50))
        action_cut = float(action_step.quantile(0.50))
        idle_state_cut = max(1e-4, float(state_step.quantile(0.10)))
        idle_action_cut = max(1e-4, float(action_step.quantile(0.10)))
    else:
        ref = json.loads((OUT_DIR / "data_value_reference_thresholds.json").read_text(encoding="utf-8"))
        state_cut = ref["state_interaction_cut"]
        action_cut = ref["action_interaction_cut"]
        idle_state_cut = ref["state_idle_cut"]
        idle_action_cut = ref["action_idle_cut"]

    rows: list[dict[str, object]] = []
    for _, sr in structural.sort_values("episode_index").iterrows():
        ep = int(sr["episode_index"])
        n = int(sr["rows"]) if pd.notna(sr.get("rows")) else 0
        vf = values[values.episode_index.eq(ep)].sort_values("row")
        im = images[images.episode_index.eq(ep)]
        fg = flags[flags.episode_index.eq(ep)] if not flags.empty else pd.DataFrame()

        ss = pd.to_numeric(vf.get("state_step_l2"), errors="coerce").fillna(0).to_numpy(float)
        aa = pd.to_numeric(vf.get("action_step_l2"), errors="coerce").fillna(0).to_numpy(float)
        idle = (ss <= idle_state_cut) & (aa <= idle_action_cut)
        interaction = (ss >= state_cut) | (aa >= action_cut)
        bad_rows: set[int] = set()
        if not fg.empty:
            bad_rows = set(pd.to_numeric(fg.loc[fg.issue_codes.fillna("").ne(""), "row"], errors="coerce").dropna().astype(int))
        usable_interaction = np.array([bool(x) and i not in bad_rows for i, x in enumerate(interaction)], dtype=bool)

        diversity_values: list[float] = []
        for _, stream_rows in im.groupby("stream"):
            hashes = stream_rows["ahash"].fillna("").astype(str)
            hashes = hashes[hashes.ne("")]
            if len(hashes):
                diversity_values.append(float(hashes.nunique() / len(hashes)))
        visual_diversity = float(np.mean(diversity_values)) if diversity_values else math.nan
        idle_fraction = float(idle.mean()) if len(idle) else math.nan
        interaction_fraction = float(interaction.mean()) if len(interaction) else math.nan
        usable_interaction_fraction = float(usable_interaction.mean()) if len(usable_interaction) else math.nan
        rows.append(
            {
                "dataset": label,
                "episode_index": ep,
                "task_index": int(sr.get("task_index_first", -1)) if pd.notna(sr.get("task_index_first")) else -1,
                "rows": n,
                "idle_fraction": idle_fraction,
                "longest_idle_run": longest_true_run(idle),
                "longest_idle_run_rate": longest_true_run(idle) / max(len(idle), 1),
                "interaction_proxy_rate": interaction_fraction,
                "usable_high_value_proxy_rate": usable_interaction_fraction,
                "visual_diversity_proxy": visual_diversity,
            }
        )
    result = pd.DataFrame(rows)
    if label == "reference":
        thresholds = {
            "state_interaction_cut": state_cut,
            "action_interaction_cut": action_cut,
            "state_idle_cut": idle_state_cut,
            "action_idle_cut": idle_action_cut,
            "idle_rate_high": float(result.idle_fraction.quantile(0.95)),
            "interaction_rate_low": float(result.interaction_proxy_rate.quantile(0.05)),
            "visual_diversity_low": float(result.visual_diversity_proxy.quantile(0.05)),
        }
        (OUT_DIR / "data_value_reference_thresholds.json").write_text(json.dumps(thresholds, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    reference = episode_features("reference")
    test = episode_features("test")
    thresholds = json.loads((OUT_DIR / "data_value_reference_thresholds.json").read_text(encoding="utf-8"))
    test["high_idle_flag"] = test.idle_fraction.gt(thresholds["idle_rate_high"])
    test["high_value_missing_flag"] = test.interaction_proxy_rate.lt(thresholds["interaction_rate_low"])
    test["scene_coverage_low_flag"] = test.visual_diversity_proxy.lt(thresholds["visual_diversity_low"])
    test["value_coverage_score"] = (
        100
        - 35 * test.high_idle_flag.astype(int)
        - 40 * test.high_value_missing_flag.astype(int)
        - 25 * test.scene_coverage_low_flag.astype(int)
    ).clip(0, 100)

    task_counts_all = test.task_index.value_counts().sort_index()
    task_counts = task_counts_all[task_counts_all.index.isin([0, 1])]
    task_balance = float(task_counts.min() / task_counts.max()) if len(task_counts) > 1 and task_counts.max() else 1.0
    probabilities = task_counts / max(task_counts.sum(), 1)
    entropy = float(-(probabilities * np.log2(probabilities.where(probabilities > 0))).sum())
    normalized_entropy = entropy / math.log2(len(task_counts)) if len(task_counts) > 1 else 1.0
    summary = {
        "scope_note": "无成功率/碰撞率和场景真值；以下均为参考集校准的训练价值代理，不替代下游任务评测。",
        "task_counts_valid": {str(int(k)): int(v) for k, v in task_counts.items()},
        "invalid_or_unreadable_task_episodes": int((~test.task_index.isin([0, 1])).sum()),
        "task_balance_ratio": round(task_balance, 4),
        "task_distribution_entropy": round(normalized_entropy, 4),
        "high_idle_episodes": int(test.high_idle_flag.sum()),
        "high_value_proxy_missing_episodes": int(test.high_value_missing_flag.sum()),
        "scene_coverage_proxy_low_episodes": int(test.scene_coverage_low_flag.sum()),
        "mean_value_coverage_score": round(float(test.value_coverage_score.mean()), 2),
        "thresholds": thresholds,
    }
    reference.to_csv(OUT_DIR / "reference_value_coverage_report.csv", index=False, encoding="utf-8-sig")
    test.to_csv(OUT_DIR / "test_value_coverage_report.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "data_value_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
