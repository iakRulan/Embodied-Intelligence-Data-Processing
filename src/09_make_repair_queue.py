"""Export the prioritized, human-readable repair queue."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
from dataset_paths import OUT_DIR


def main() -> None:
    report = pd.read_csv(OUT_DIR / "test_quality_report.csv", encoding="utf-8-sig")
    report["priority"] = report["severity"].map({"严重": 0, "高": 1, "中": 2, "低": 3, "通过": 4}).fillna(5)
    report = report.sort_values(["priority", "overall_quality_score", "episode_index"])
    columns = [
        "priority", "episode_index", "status", "severity", "rows", "expected_rows",
        "overall_quality_score", "score_gate_reason", "issue_categories",
        "issue_codes", "issue_details", "repair_mode", "repairable_codes",
        "unrecoverable_codes", "repair_actions", "detector_version", "raw_file",
    ]
    queue = report[columns].copy()
    validation_path = OUT_DIR / "repair_validation_report.csv"
    if validation_path.exists():
        validation = pd.read_csv(validation_path, encoding="utf-8-sig")
        keep = [c for c in [
            "episode_index", "source_sha256", "repaired_sha256", "changed_rows",
            "overall_quality_score_before", "overall_quality_score_after", "score_gain_measured",
            "targeted_codes_removed", "passed_full_recheck", "flagged_frames_before", "flagged_frames_after",
        ] if c in validation.columns]
        queue = queue.merge(validation[keep], on="episode_index", how="left")
    queue.to_csv(OUT_DIR / "repair_queue.csv", index=False, encoding="utf-8-sig")

    frame = pd.read_csv(OUT_DIR / "test_frame_flags.csv", encoding="utf-8-sig")
    issue = frame[frame.issue_codes.fillna("").ne("")].sort_values(["episode_index", "row"]).copy()
    intervals: list[dict[str, object]] = []
    for episode_index, group in issue.groupby("episode_index", sort=True):
        start = previous = None
        codes: set[str] = set()
        categories: set[str] = set()
        details: list[str] = []
        for item in group.itertuples(index=False):
            row = int(item.row)
            if previous is not None and row != previous + 1:
                intervals.append({
                    "episode_index": int(episode_index), "start_row": start, "end_row": previous,
                    "frame_count": previous - start + 1, "issue_codes": ";".join(sorted(codes)),
                    "issue_categories": ";".join(sorted(categories)), "detail_examples": " | ".join(details[:3]),
                })
                start, codes, categories, details = row, set(), set(), []
            if start is None:
                start = row
            codes.update(filter(None, str(item.issue_codes).split(";")))
            categories.update(filter(None, str(item.issue_categories).split(";")))
            if getattr(item, "detail", "") and str(item.detail) not in details:
                details.append(str(item.detail))
            previous = row
        if start is not None:
            intervals.append({
                "episode_index": int(episode_index), "start_row": start, "end_row": previous,
                "frame_count": previous - start + 1, "issue_codes": ";".join(sorted(codes)),
                "issue_categories": ";".join(sorted(categories)), "detail_examples": " | ".join(details[:3]),
            })
    pd.DataFrame(intervals).to_csv(OUT_DIR / "test_issue_intervals.csv", index=False, encoding="utf-8-sig")
    print(queue.head(15).to_string(index=False))


if __name__ == "__main__":
    main()
