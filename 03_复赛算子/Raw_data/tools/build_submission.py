"""Build a reviewed, ASCII-named platform submission. Never include source datasets.

Run only after review/testing; generated data are copied by an explicit allowlist.
The output directory must not exist. The PDF is authored separately and inspected.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--output", required=True)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--report", required=True)
    a = ap.parse_args()
    source, out, pdf, report = [Path(x).resolve() for x in (a.source, a.output, a.pdf, a.report)]
    if not str(out).isascii() or out.exists():
        raise ValueError("Use a new ASCII-only output directory")
    selected = ["run.py", "README.md", "requirements.txt", "Dockerfile", "CHANGELOG_v3.2.md",
                "calibration/default_thresholds.json", "validation/v3.2_acceptance.json",
                "validation/platform_release_review.json"]
    selected += [p.relative_to(source).as_posix() for p in sorted((source / "refsync_qa").glob("*.py"))]
    selected += ["tools/" + x for x in ["acceptance.py", "calibrate_reference.py", "load_governed.py",
                                      "regression_probes.py", "selftest.py", "test_safety.py"]]
    selected += ["docs/Algorithm_Description_v3_2.md", "docs/Deployment_Guide_v3_2.md"]
    for rel in selected:
        path = source / rel
        if not rel.isascii() or not path.is_file() or path.stat().st_size > 2_000_000:
            raise ValueError(f"Invalid release file: {rel}")
        content = path.read_text(encoding="utf-8-sig")
        if path.suffix == ".py":
            ast.parse(content, filename=rel)
        if re.search(r"-----BEGIN .*PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}", content):
            raise ValueError(f"Possible credential: {rel}")
    if not pdf.is_file() or not report.is_file():
        raise ValueError("PDF or full episode report missing")
    review = json.loads((source / "validation/platform_release_review.json").read_text(encoding="utf-8"))
    if not review.get("passed"):
        raise ValueError("Release review has not passed")
    for item in review["source_manifest"]:
        if digest(source / item["file"]) != item["sha256"]:
            raise ValueError(f"Source changed after review: {item['file']}")
    out.mkdir(parents=True)
    package = out / "refsync_qa_v3_2"
    package.mkdir()
    for rel in selected:
        target = package / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / rel, target)
    shutil.copyfile(pdf, package / "docs/Algorithm_Description_v3_2.pdf")
    # Runtime exception text can embed the original machine's dataset path.
    # Rebase that prefix without altering episode values, labels or metrics.
    report_text = report.read_text(encoding="utf-8-sig")
    report_text, path_redactions = re.subn(
        r"(?i)[A-Z]:[/\\][^'\r\n]*?([/\\]data[/\\]chunk-[^'\r\n]*)",
        lambda m: "/data/test" + m.group(1).replace("\\", "/"), report_text)
    (package / "validation/full89_episode_report.csv").write_text(report_text, encoding="utf-8-sig", newline="")
    # Preserve the original acceptance content, only rebase historical path labels.
    acc = package / "validation/v3.2_acceptance.json"
    data = json.loads(acc.read_text(encoding="utf-8-sig"))
    for entry in data["source_manifest"]:
        entry["file"] = entry["file"].split("Raw_data/", 1)[-1]
    data["manifest_note"] = "Historical algorithm acceptance hashes before docstring-only English-path edits; current hashes are in platform_release_review.json and submission_manifest.json."
    acc.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    files = sorted(p for p in package.rglob("*") if p.is_file())
    for p in files:
        if not p.relative_to(package).as_posix().isascii():
            raise ValueError("Non-ASCII archive path")
        if p.suffix == ".md":
            for link in re.findall(r"\]\(([^)]+)\)", p.read_text(encoding="utf-8-sig")):
                if "://" not in link and not (p.parent / link).resolve().is_file():
                    raise ValueError(f"Broken documentation link: {p.name}: {link}")
    manifest = {"operator": "RefSync-QA-v3.2", "release": "v3_2_20260922",
                "scope": "Code, documentation and local validation only; no platform-effect claim",
                "default_timestamp_policy": "conservative",
                "report_path_prefixes_redacted": path_redactions,
                "report_redaction_note": "Only local absolute input paths in exception text rebased to /data/test; no algorithm result changed",
                "files": [
                    {"file": p.relative_to(package).as_posix(), "bytes": p.stat().st_size, "sha256": digest(p)}
                    for p in files]}
    (package / "submission_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    archive = out / "RefSync_QA_v3_2_Submission.zip"
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(package.rglob("*")):
            if p.is_file():
                z.write(p, arcname=p.relative_to(out).as_posix())
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None or not all(x.isascii() for x in z.namelist()):
            raise ValueError("Archive verification failed")
    shutil.copyfile(pdf, out / "Algorithm_Description_v3_2.pdf")
    shutil.copyfile(source / "docs/Algorithm_Description_v3_2.md", out / "Algorithm_Description_v3_2.md")
    (out / "Submission_SHA256.txt").write_text(f"{digest(archive)}  {archive.name}\n", encoding="ascii")
    print(json.dumps({"archive": str(archive), "sha256": digest(archive),
                      "files": len(manifest["files"]) + 1, "bytes": archive.stat().st_size}, indent=2))


if __name__ == "__main__":
    main()
