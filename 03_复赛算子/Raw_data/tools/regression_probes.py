# -*- coding: utf-8 -*-
"""Regression probes for the boundary cases raised in the v3.0 review (all must PASS).

python tools/regression_probes.py --reference /data/reference --output /data/regression [--base-episode 12]

Every probe works on in-memory copies or on synthetic files inside --output
(a fresh sandbox marked with .refsync_probe); official data is only read.
Exit code 0 = all probes passed, 1 = at least one failed.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import warnings
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.simplefilter("ignore", RuntimeWarning)

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from refsync_qa import calibrate, config, dataset, detect, features, pipeline  # noqa: E402
from refsync_qa.pipeline import PathGuardError, _pack, process_episode_safe, relations, run  # noqa: E402
from refsync_qa.report import build_outputs, governance  # noqa: E402
from refsync_qa.repair import Repairer, verify_copy  # noqa: E402

SANDBOX_MARK = ".refsync_probe"
results: dict[str, dict] = {}


def probe(name: str, fn) -> None:
    try:
        ok, detail = fn()
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, {"exception": f"{type(exc).__name__}: {exc}"}
    results[name] = {"pass": bool(ok), "detail": detail}
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {json.dumps(detail, ensure_ascii=False, default=str)[:400]}", flush=True)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def fake_result(ep: int, n: int, img: list[str], full: list[str], blocking_ep_code: str | None = None, task: int = 0) -> dict:
    R = detect.Result(n)
    R.metrics.update(value_evaluable=True, value_partially_evaluable=True, task_index=task)
    if blocking_ep_code:
        R.ep(blocking_ep_code, "synthetic")
    pk = _pack(R)
    return {"episode_index": ep, "rows": n, "rel_path": f"data/chunk-000/episode_{ep:06d}.parquet", "frame_index": list(range(n)),
            "orig": pk, "final": copy.deepcopy(pk), "repair": {"applied": [], "rejected": [], "audit": [], "meta_patch": {}, "candidates": []},
            "row_img_sig": img, "row_full_sig": full, "source_sha256": "synthetic", "repaired_rel_path": "", "repaired_sha256": "", "error": ""}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--base-episode", type=int, default=12)
    a = ap.parse_args()
    out = Path(a.output).resolve()
    if out.exists() and any(out.iterdir()) and not (out / SANDBOX_MARK).exists():
        print(f"[error] {out} 非空且不是探针沙箱，拒绝写入", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    (out / SANDBOX_MARK).write_text("regression probe sandbox", encoding="utf-8")

    ds = dataset.discover(a.reference)
    base_path = next(e.path for e in ds.episodes if e.episode_id == a.base_episode)
    base = pq.read_table(base_path)
    ref_sha_before = sha(base_path)
    ctx = dict(image_columns=ds.image_columns, state_dim=ds.state_dim, action_dim=ds.action_dim)
    thr = calibrate.load_thresholds(Path(__file__).resolve().parents[1] / "calibration" / "default_thresholds.json")
    opts = {**config.DEFAULT_OPTIONS, "layout": config.DEFAULT_LAYOUT}
    base_feats = features.extract(base_path, ctx)
    n = base_feats["n"]

    # ---------------- P1-3 robustness ----------------
    probe("short_xcorr_5_rows", lambda: (features.xcorr_lags(np.arange(5.), np.arange(5.), 12) == {}, "5 行输入返回空结果，不抛异常"))
    probe("xcorr_unequal_lengths", lambda: (isinstance(features.xcorr_lags(np.arange(80.), np.arange(60.), 12), dict), "长度不等按短者截断"))

    def numeric_string():
        M, ok, present, unp = features._matrix(pa.table({"state": [["bad"] + ["0"] * 19]}), "state", 1, 20)
        return bool(unp[0] and np.isnan(M[0, 0])), {"unparseable_row": bool(unp[0]), "dim_ok": bool(ok[0])}
    probe("numeric_string_corruption", numeric_string)

    def scalar_shape():
        v, present, bad = features._scalar(pa.table({"index": [[1., 99.], [2.]]}), "index", 2)
        return bool(np.isnan(v[0]) and v[1] == 2.0 and bad.tolist() == [True, False]), {"values": v.tolist(), "malformed": bad.tolist()}
    probe("scalar_wrong_shape_reported", scalar_shape)

    def short_episode():
        f = features.extract(base_path, ctx)
        for k in ("timestamp", "frame_index", "episode_index", "index", "task_index"):
            f[k] = f[k][:5]
            f[f"malformed_{k}"] = f[f"malformed_{k}"][:5]
        for k in ("state", "action", "state_dim_ok", "action_dim_ok", "state_unparseable", "action_unparseable"):
            f[k] = f[k][:5]
        for sf in f["streams"].values():
            for k in list(sf):
                sf[k] = sf[k][:5]
        f["n"] = 5
        R = detect.analyse(f, a.base_episode, ds, thr, opts)
        return True, {"codes": R.all_codes(), "not_evaluable": R.not_evaluable[:3]}
    probe("short_episode_analyse", short_episode)

    def worker_exception():
        job = {"ds": None, "thr": thr, "opts": opts, "ep_id": 7, "path": str(base_path), "rel": "chunk-000/x.parquet", "governed_dir": str(out)}
        r = process_episode_safe(job)
        return "C_PROCESSING_ERROR" in r["orig"]["ep_codes"], {"error": r["error"].splitlines()[0]}
    probe("worker_exception_becomes_record", worker_exception)

    # ---------------- P1-4 conservative numeric repair ----------------
    def sparse_gap():
        f = copy.deepcopy(base_feats)
        f["state"][50, 0] = np.nan
        f["frame_index"][50:] += 100
        f["timestamp"][50:] += 10
        f["state"][49, 0] = 0.0
        f["state"][51, 0] = 0.8
        rr = detect.analyse(f, a.base_episode, ds, thr, opts)
        rep = Repairer(f, rr, a.base_episode, ds, thr, opts)
        rep.table = rep.source = base
        rep.sparse_numeric()
        changed = [x for x in rep.audit if x["field"] == "state[0]"]
        return (not changed and any("frame_index" in x for x in rep.rejected)), {"audit": changed, "rejected": rep.rejected}
    probe("sparse_repair_rejects_frame_gap", sparse_gap)

    def untrusted_neighbour():
        f = copy.deepcopy(base_feats)
        f["state"][60, 0] = np.nan
        f["state"][61, 1] = f["state"][61, 1] + 0.9  # neighbour turned into a spike
        rr = detect.analyse(f, a.base_episode, ds, thr, opts)
        rep = Repairer(f, rr, a.base_episode, ds, thr, opts)
        rep.table = rep.source = base
        rep.sparse_numeric()
        return (not rep.audit and bool(rep.rejected)), {"neighbour_codes": sorted(rr.row_codes[61]), "rejected": rep.rejected}
    probe("sparse_repair_rejects_untrusted_neighbour", untrusted_neighbour)

    def float64_preservation():
        vals = np.array(base.column("state").to_pylist(), dtype=np.float64)
        vals[50, 0] = np.nan
        vals[50, 1] += 1.234567891e-9
        f = copy.deepcopy(base_feats)
        f["state"] = vals.copy()
        t64 = base.set_column(base.column_names.index("state"), "state", pa.array(vals.tolist(), type=pa.list_(pa.float64())))
        rr = detect.analyse(f, a.base_episode, ds, thr, opts)
        rep = Repairer(f, rr, a.base_episode, ds, thr, opts)
        rep.table = rep.source = t64
        rep.sparse_numeric()
        after = np.array(rep.table.column("state").to_pylist())
        fin = np.isfinite(vals)
        changed = int(np.sum(fin & (after != vals)))
        problems = verify_copy(t64, rep.table, rep.audit)
        return (changed == 0 and len(rep.audit) == 1 and not problems and str(rep.table.schema.field("state").type.value_type) == "double"), \
            {"finite_cells_changed": changed, "audit_entries": len(rep.audit), "verify_problems": problems,
             "dtype_after": str(rep.table.schema.field("state").type)}
    probe("float64_sparse_repair_preservation", float64_preservation)

    def index_tie():
        f = copy.deepcopy(base_feats)
        f["index"] = f["frame_index"].copy()
        f["index"][n // 2:] += 10000
        rr = detect.analyse(f, a.base_episode, ds, thr, opts)
        rep = Repairer(f, rr, a.base_episode, ds, thr, opts)
        rep.table = rep.source = base.set_column(base.column_names.index("index"), "index", pa.array(f["index"].astype(np.int64)))
        rep.index()
        flagged = sum("T_INDEX_DISCONTINUITY" in s for s in rr.row_codes)
        return (not rep.audit and bool(rep.rejected) and flagged == n), {"support": rr.metrics.get("index_offset_support"), "rows_flagged": flagged,
                                                                         "rejected": rep.rejected}
    probe("index_offset_tie_not_repaired", index_tie)

    def index_dominant():
        f = copy.deepcopy(base_feats)
        f["index"] = f["frame_index"].copy()
        f["index"][[10, 20, 30]] += 7
        rr = detect.analyse(f, a.base_episode, ds, thr, opts)
        rep = Repairer(f, rr, a.base_episode, ds, thr, opts)
        rep.table = rep.source = base.set_column(base.column_names.index("index"), "index", pa.array(f["index"].astype(np.int64)))
        rep.index()
        return (sorted(x["row"] for x in rep.audit) == [10, 20, 30] and not verify_copy(rep.source, rep.table, rep.audit)), \
            {"support": rr.metrics.get("index_offset_support"), "changed_rows": [x["row"] for x in rep.audit]}
    probe("index_offset_dominant_repaired_minimally", index_dominant)

    def verify_catches_unaudited():
        tv = base.column("task_index").to_pylist()
        t = base.set_column(base.column_names.index("task_index"), "task_index", pa.array([tv[0] + 1] + tv[1:], type=pa.int64()))
        return bool(verify_copy(base, t, [])), {"problems": verify_copy(base, t, [])}
    probe("post_write_verify_catches_unaudited_change", verify_catches_unaudited)

    # ---------------- P1-2 non-transitive dedup ----------------
    A = [f"a{i}" for i in range(100)]
    C = [f"c{i}" for i in range(100)]

    def dedup_non_transitive():
        rs = [fake_result(1, 100, A, A), fake_result(2, 200, A + C, A + C, "S_STREAM_MISSING"), fake_result(3, 100, C, C)]
        pairs = relations(rs, opts)
        gov = governance(rs, opts)
        kept = {ep: int(g["train"].sum()) for ep, g in gov.items()}
        rel = {(p["episode_a"], p["episode_b"]): p["relation"] for p in pairs}
        return (kept == {1: 100, 2: 0, 3: 100} and (1, 3) not in rel), {"train_frames": kept, "relations": {f"{k}": v for k, v in rel.items()}}
    probe("dedup_non_transitive_keeps_disjoint_episode", dedup_non_transitive)

    def dedup_equivalent():
        rs = [fake_result(1, 100, A, A), fake_result(2, 100, A, A)]
        pairs = relations(rs, opts)
        gov = governance(rs, opts)
        return (pairs and pairs[0]["relation"] == "整轨等价" and gov[1]["policy"] == "整段可用" and gov[2]["policy"] == "重复剔除"), \
            {"relation": pairs[0]["relation"] if pairs else None, "policies": {e: g["policy"] for e, g in gov.items()}}
    probe("dedup_equivalent_pair_drops_one_copy", dedup_equivalent)

    def dedup_same_images_other_state():
        B_full = [f"x{i}" for i in range(100)]  # same images, different state/actions
        rs = [fake_result(1, 100, A, A), fake_result(2, 100, A, B_full)]
        pairs = relations(rs, opts)
        gov = governance(rs, opts)
        return (pairs[0]["relation"] == "同源副本" and int(gov[2]["train"].sum()) == 100), \
            {"relation": pairs[0]["relation"], "kinematic_consistency": pairs[0]["kinematic_consistency"], "ep2_train": int(gov[2]["train"].sum())}
    probe("dedup_same_images_different_state_not_dropped", dedup_same_images_other_state)

    def dedup_contained_partial():
        # ep2 contains ep1 plus 60 new rows; ep2 is kept, ep1 is fully covered, the new rows of ep2 are never dropped
        rs = [fake_result(1, 100, A, A), fake_result(2, 160, A + C[:60], A + C[:60])]
        gov = governance(rs, opts)
        return (int(gov[2]["train"].sum()) == 160 and gov[1]["policy"] == "重复剔除"), {e: (g["policy"], int(g["train"].sum())) for e, g in gov.items()}
    probe("dedup_containment_keeps_superset", dedup_contained_partial)

    def dedup_report_end_to_end():
        rs = [fake_result(1, 100, A, A), fake_result(2, 200, A + C, A + C, "S_STREAM_MISSING"), fake_result(3, 100, C, C)]
        pairs = relations(rs, opts)
        d = out / "dedup_report"
        (d / "report").mkdir(parents=True, exist_ok=True)
        info = dataset.DatasetInfo(root=d, episodes=[], image_columns=["image"], tasks={0: "A", 1: "B"})
        s = build_outputs(rs, pairs, info, thr, opts, d, d / "governed", "synthetic")
        s.pop("_tables", None)
        import csv
        pol = {int(r["episode_index"]): (r["train_policy"], int(r["train_frames"])) for r in csv.DictReader(open(d / "governed" / "train_policy.csv", encoding="utf-8-sig"))}
        tv = s["dataset_value"]
        return (pol[3] == ("整段可用", 100) and tv["task_episode_counts"] == {"0": 3, "1": 0} and tv["task_balance_ratio_episodes"] == 0.0), \
            {"policies": pol, "task_counts": tv["task_episode_counts"], "balance": tv["task_balance_ratio_episodes"]}
    probe("dedup_and_task_coverage_in_reports", dedup_report_end_to_end)

    # ---------------- P0 input/output guard + marker ----------------
    def overlap_guard():
        sand = out / "overlap"
        inp = sand / "governed"
        target = inp / "data" / "chunk-000" / f"episode_{a.base_episode:06d}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        (inp / "meta").mkdir(parents=True, exist_ok=True)
        t = base.slice(0, 60)
        ts = t.column("timestamp").to_pylist()
        ts[20] = None
        i = t.column_names.index("timestamp")
        t = t.set_column(i, t.schema.field(i), pa.array(ts, type=t.schema.field(i).type))
        pq.write_table(t, target)
        (inp / "meta" / "info.json").write_text(json.dumps(dict(ds.info, total_episodes=1, total_frames=60)), encoding="utf-8")
        (inp / "meta" / "tasks.jsonl").write_text("\n".join(json.dumps({"task_index": k, "task": v}) for k, v in ds.tasks.items()), encoding="utf-8")
        (inp / "meta" / "episodes.jsonl").write_text(json.dumps({**ds.episodes_meta[a.base_episode], "length": 60}), encoding="utf-8")
        before = sha(target)
        refused = []
        for i_, o_ in ((inp, sand), (inp, inp), (sand, inp / "out")):
            try:
                run(i_, o_, workers=1, log=lambda *x: None)
                refused.append(False)
            except PathGuardError:
                refused.append(True)
        after = sha(target)
        # a legitimate run next to it still works, and the output is marked
        ok_out = out / "overlap_ok_output"
        run(inp, ok_out, workers=1, log=lambda *x: None)
        return (all(refused) and before == after and (ok_out / dataset.MARKER).exists() and sha(target) == before), \
            {"refused": refused, "source_modified": before != after}
    probe("output_input_overlap_guard", overlap_guard)

    def marker_skip():
        root = out / "overlap"  # contains governed/ (an input-like tree) and is not marked
        d0 = dataset.discover(root)
        (root / dataset.MARKER).write_text("{}", encoding="utf-8")
        d_marked = dataset.discover(root)
        (root / dataset.MARKER).unlink()
        d1 = dataset.discover(out / "overlap_ok_output") if (out / "overlap_ok_output").exists() else None
        d2 = dataset.discover(out)  # sandbox root: overlap_ok_output is marked -> its governed copies are skipped
        found = [str(e.rel_path) for e in d2.episodes]
        return (len(d0.episodes) == 1 and len(d_marked.episodes) == 0 and d1 is not None and len(d1.episodes) == 0
                and any(f.startswith("overlap/") for f in found) and all("overlap_ok_output" not in f for f in found)), \
            {"unmarked": len(d0.episodes), "marked_output": len(d1.episodes) if d1 else None, "sandbox_scan": found}
    probe("marked_output_never_rediscovered_as_input", marker_skip)

    def previous_run_archived():
        ok_out = out / "overlap_ok_output"
        run(out / "overlap" / "governed", ok_out, workers=1, log=lambda *x: None)
        arch = list((ok_out / "_previous_runs").glob("*/report"))
        return bool(arch), {"archived": [str(p.parent.name) for p in arch]}
    probe("previous_run_archived_not_overwritten", previous_run_archived)

    # ---------------- ep25-like int64 timestamp ----------------
    def int_timestamp():
        sand = out / "int_ts"
        (sand / "data" / "chunk-000").mkdir(parents=True, exist_ok=True)
        (sand / "meta").mkdir(parents=True, exist_ok=True)
        ts = np.array(base.column("timestamp").to_pylist()) * 100
        tt = base.set_column(base.column_names.index("timestamp"), "timestamp", pa.array(ts.astype(np.int64)))
        pq.write_table(tt, sand / "data" / "chunk-000" / f"episode_{a.base_episode:06d}.parquet")
        (sand / "meta" / "info.json").write_text(json.dumps(dict(ds.info, total_episodes=1, total_frames=n)), encoding="utf-8")
        (sand / "meta" / "tasks.jsonl").write_text("\n".join(json.dumps({"task_index": k, "task": v}) for k, v in ds.tasks.items()), encoding="utf-8")
        (sand / "meta" / "episodes.jsonl").write_text(json.dumps(ds.episodes_meta[a.base_episode]), encoding="utf-8")
        o = out / "int_ts_run"
        run(sand, o, workers=1, options={"timestamp_policy": "nominal"}, log=lambda *x: None)
        import csv
        r = next(csv.DictReader(open(o / "report" / "episode_report.csv", encoding="utf-8-sig")))
        fixed = pq.read_table(o / "governed" / "data" / "chunk-000" / f"episode_{a.base_episode:06d}.parquet")
        same = np.allclose(np.array(fixed.column("timestamp").to_pylist()), np.array(base.column("timestamp").to_pylist()), atol=0)
        return (r["final_status"] == "通过" and str(fixed.schema.field("timestamp").type) == "float" and same), \
            {"codes": r["issue_codes"], "final": r["final_status"], "dtype_after": str(fixed.schema.field("timestamp").type), "exact_restore": bool(same)}
    probe("int64_timestamp_schema_and_unit_repair", int_timestamp)

    def batch_continues():
        orig = pipeline.process_episode

        def boom(job):
            if job["ep_id"] == a.base_episode:
                raise RuntimeError("injected failure")
            return orig(job)
        pipeline.process_episode = boom
        try:
            sand = out / "int_ts"
            s = run(sand, out / "error_run", workers=1, log=lambda *x: None)
        finally:
            pipeline.process_episode = orig
        return (len(s["processing_errors"]) == 1 and s["governance"]["policy_counts"].get("处理失败") == 1), \
            {"errors": s["processing_errors"], "policies": s["governance"]["policy_counts"]}
    probe("processing_error_recorded_batch_continues", batch_continues)

    probe("reference_file_untouched", lambda: (sha(base_path) == ref_sha_before, {"sha256": ref_sha_before[:16]}))

    passed = sum(r["pass"] for r in results.values())
    (out / "regression_probe_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"{passed}/{len(results)} probes passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
