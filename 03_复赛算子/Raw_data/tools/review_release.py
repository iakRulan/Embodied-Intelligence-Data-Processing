"""Recheck release code and record local, non-sensitive evidence.

This is not a platform execution result. It compares executable ASTs, so
English-only docstring updates do not pretend to be new algorithm experiments.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path


class RemoveDocs(ast.NodeTransformer):
    def generic_visit(self, node):
        node = super().generic_visit(node)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
                if isinstance(node.body[0].value.value, str):
                    node.body = node.body[1:]
        return node


def semantic(text):
    return ast.dump(RemoveDocs().visit(ast.parse(text.lstrip("\ufeff"))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--output", required=True)
    a = ap.parse_args()
    root = Path(__file__).resolve().parents[1]
    repo = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=root).decode().strip())
    prefix = root.relative_to(repo).as_posix()
    historical = json.loads((root / "validation/v3.2_acceptance.json").read_text(encoding="utf-8-sig"))
    entries, failures, changed_docs = [], [], []
    for item in historical["source_manifest"]:
        rel = item["file"].split("Raw_data/", 1)[-1]
        path = root / rel
        raw = path.read_bytes()
        before = subprocess.check_output(["git", "show", f"{a.baseline}:{prefix}/{rel}"], cwd=repo)
        if semantic(raw.decode("utf-8-sig")) != semantic(before.decode("utf-8-sig")):
            failures.append(f"Executable AST changed: {rel}")
        if raw != before:
            changed_docs.append(rel)
        entries.append({"file": rel, "sha256": hashlib.sha256(raw).hexdigest()})
    syntax = 0
    for p in root.rglob("*.py"):
        if "__pycache__" not in p.parts:
            ast.parse(p.read_text(encoding="utf-8-sig"), filename=p.relative_to(root).as_posix())
            syntax += 1
    tests = []
    for flags in ([], ["-O"]):
        r = subprocess.run([sys.executable, *flags, "tools/test_safety.py"], cwd=root,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        passed = r.returncode == 0 and "Ran 24 tests" in r.stderr and "\nOK" in r.stderr
        tests.append({"mode": "optimized" if flags else "normal", "unique_tests": 24, "passed": passed})
        if not passed:
            failures.append("Safety tests failed: " + tests[-1]["mode"])
            print(r.stderr[-3000:])
    cli = subprocess.run([sys.executable, str(root / "run.py"), "--help"], cwd=root.parent,
                         capture_output=True)
    if cli.returncode or b"--timestamp-policy" not in cli.stdout:
        failures.append("CLI from unrelated cwd failed")
    result = {"operator": "RefSync-QA-v3.2", "date": "2026-09-22", "baseline_commit": a.baseline,
              "scope": "Local packaging re-review; no platform login, upload, deployment or effect claim",
              "unchanged_executable_ast_files": len(entries) if not any("AST" in x for x in failures) else None,
              "docstring_only_changed_files": changed_docs, "ast_files_checked": syntax,
              "safety_tests": tests, "cli_help_from_other_cwd_passed": cli.returncode == 0,
              "source_manifest": entries, "failures": failures, "passed": not failures}
    target = Path(a.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "source_manifest"}, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
