# -*- coding: utf-8 -*-
"""Fault-injection self-test + reference leave-one-episode-out (LOEO).

python tools/selftest.py --reference <参考集目录> --output <新的专用目录> [--base-episodes 12,16,0] [--workers 4]

1. LOEO on the reference set: each fold re-calibrates on the other episodes
   and runs the detector on the held-out one (false-positive check).
2. For every base episode, a synthetic dataset gets one injected fault per
   episode (plus a clean control and an exact duplicate); the full operator
   runs with thresholds calibrated WITHOUT any base episode.  Row-level faults
   carry the injected rows as truth, so frame-level TP/FP/FN are reported
   per case; unexpected defect codes count as false alarms.
3. Exit code 0 only when: every expected code is found, no unexpected defect
   code appears (known side effects are declared per case), every repair
   check passes, the clean controls are unflagged and LOEO has 0 defect FP.

Only directories carrying the .refsync_selftest marker are ever deleted.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import shutil
import sys
import warnings
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.simplefilter("ignore", RuntimeWarning)

from refsync_qa import calibrate, config, dataset, detect, features  # noqa: E402
from refsync_qa.pipeline import run  # noqa: E402

MARK = ".refsync_selftest"


def _safe_rmtree(p: Path) -> None:
    if p.exists():
        if not (p / MARK).exists():
            raise RuntimeError(f"refusing to delete {p}: no {MARK} marker")
        shutil.rmtree(p)


def _png(arr: np.ndarray) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(arr.astype(np.uint8)).save(buf, format="PNG")
    return buf.getvalue()


def _decode(b: bytes) -> np.ndarray:
    from PIL import Image
    return np.asarray(Image.open(io.BytesIO(b)).convert("RGB"))


def loeo(ref_root: Path) -> list[dict]:
    ds = dataset.discover(ref_root)
    ctx = dict(image_columns=ds.image_columns, state_dim=ds.state_dim, action_dim=ds.action_dim)
    feats = {e.episode_id: features.extract(e.path, ctx) for e in ds.episodes}
    opts = {**config.DEFAULT_OPTIONS, "layout": config.DEFAULT_LAYOUT}
    rows = []
    for e in ds.episodes:
        thr = calibrate.load_thresholds(None)
        thr.update(calibrate.calibrate([f for k, f in feats.items() if k != e.episode_id]))
        R = detect.analyse(feats[e.episode_id], e.episode_id, ds, thr, opts)
        codes = R.all_codes()
        rows.append({"episode": e.episode_id, "defect_codes": [c for c in codes if not c.startswith("V_")],
                     "value_hints": [c for c in codes if c.startswith("V_")], "freeze_min_run": thr["freeze_min_run"]})
    return rows


def build_synthetic(ref_root: Path, base_ep: int, out_root: Path, off: int, thr: dict, cases: dict[int, dict]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    src = next(e.path for e in dataset.discover(ref_root).episodes if e.episode_id == base_ep)
    base = pq.read_table(src)
    n = base.num_rows
    meta_src = dataset.discover(ref_root)
    task_text = meta_src.episodes_meta[base_ep]["tasks"]
    base_task = meta_src.task_by_text[task_text[0]]
    other_task = [k for k in meta_src.tasks if k != base_task][0]
    data = out_root / "data" / "chunk-000"
    rng = np.random.default_rng(20260922 + base_ep)

    def col(t, name):
        return t.column(name).to_pylist()

    def put(t, name, values, typ=None):
        i = t.column_names.index(name)
        f = t.schema.field(i)
        typ = typ or f.type
        return t.set_column(i, pa.field(name, typ, f.nullable), pa.array(values, type=typ))

    def save(k, t, expect, repair=None, meta_len=None, note="", rows=None, allow=(), keep_ep=False):
        ep = off + k
        if "episode_index" in t.column_names and not keep_ep:
            t = put(t, "episode_index", [ep] * t.num_rows)
        pq.write_table(t, data / f"episode_{ep:06d}.parquet")
        cases[ep] = {"base": base_ep, "case": k, "expect": expect, "repair": repair, "meta_len": meta_len if meta_len is not None else t.num_rows,
                     "note": note, "truth_rows": sorted(rows) if rows is not None else None, "allow": set(allow), "tasks": task_text}

    S = np.array(col(base, "state"), dtype=np.float64)
    A = np.array(col(base, "actions"), dtype=np.float64)
    save(0, base, [], note="干净对照")
    # ---- scalar fields ----
    save(1, put(base, "timestamp", [None if i == 50 else v for i, v in enumerate(col(base, "timestamp"))]), ["T_TIMESTAMP_INVALID"], "pass", note="timestamp 单点缺失", rows=[50])
    save(2, put(base, "index", [None if i == 50 else v for i, v in enumerate(col(base, "index"))]), ["T_INDEX_INVALID"], "pass", note="index 单点缺失", rows=[50])
    save(3, put(base, "episode_index", [None if i == 50 else off + 3 for i in range(n)]), ["S_EPISODE_INDEX_INVALID"], "pass", note="episode_index 单点缺失", rows=[50], keep_ep=True)
    save(4, put(base, "frame_index", [None if i == 50 else v for i, v in enumerate(col(base, "frame_index"))]), ["T_FRAME_INDEX_INVALID"], "clips",
         note="frame_index 单点缺失", rows=[50], allow={"T_INDEX_DISCONTINUITY"})
    save(5, put(base, "task_index", [other_task] * n), ["S_TASK_META_MISMATCH"], "pass", note="整条换成另一合法任务")
    ts = np.array(col(base, "timestamp"))
    save(6, put(base, "timestamp", (ts + rng.uniform(-0.006, 0.006, n) * (np.arange(n) > 0)).astype(np.float32).tolist()), ["T_TIMESTAMP_JITTER"], "pass", note="±6ms 抖动")
    save(7, put(base, "timestamp", (ts * 1000).astype(np.float32).tolist()), ["T_TIMESTAMP_UNIT_MISMATCH"], "pass", note="单位 ms")
    save(8, put(base, "timestamp", (ts + 3600).astype(np.float32).tolist()), ["T_TIMESTAMP_OFFSET"], "pass", note="整体偏移 3600s")
    keep = [i for i in range(n) if not 60 <= i < 65]
    save(9, base.take(pa.array(keep)), ["T_FRAME_INDEX_GAP", "T_TIMESTAMP_GAP"], "clips", note="掉 5 帧", rows=[60],
         allow={"T_METADATA_LENGTH_MISMATCH", "J_SPIKE_JUMP", "J_STATE_ACTION_MISMATCH", "S_STREAM_FROZEN"}, meta_len=n)
    order = list(range(n))
    order[80], order[81] = order[81], order[80]
    save(10, base.take(pa.array(order)), ["T_FRAME_ORDER_ERROR", "T_TIMESTAMP_NON_MONOTONIC"], "clips", note="两帧乱序", rows=[81],
         allow={"T_FRAME_INDEX_GAP", "J_SPIKE_JUMP", "J_STATE_ACTION_MISMATCH", "T_TIMESTAMP_GAP"})
    save(11, put(base, "episode_index", [999] * n), ["S_EPISODE_INDEX_MISMATCH"], "pass", note="episode_index 整条写错", keep_ep=True)
    save(12, base, ["T_METADATA_LENGTH_MISMATCH"], "pass", meta_len=n - 10, note="元数据长度错")
    tsi = np.array(col(base, "timestamp")) * 100
    save(13, put(base, "timestamp", np.trunc(tsi + 1e-6).astype(np.int64).tolist(), pa.int64()), ["T_TIMESTAMP_UNIT_MISMATCH", "C_SCHEMA_DTYPE_MISMATCH"], "pass",
         note="timestamp int64 且单位 10ms（类 ep25）")
    ii = np.array(col(base, "index"))
    ii[[30, 31, 32]] += 5
    save(14, put(base, "index", ii.tolist()), ["T_INDEX_DISCONTINUITY"], "pass", note="index 3 行偏移", rows=[30, 31, 32])
    # ---- cameras ----
    lw = col(base, "left_wrist_image")
    k = 7
    shifted = [dict(lw[i + k]) if i + k < n else {"bytes": None, "path": None} for i in range(n)]
    save(20, put(base, "left_wrist_image", shifted), ["S_STREAM_SHIFTED"], "shift", note="左腕晚启动 7 帧并整体前移", allow={"S_CAMERA_LAG_SUSPECT"})
    save(21, put(base, "left_wrist_image", [{"bytes": None, "path": None} if i < 12 else lw[i] for i in range(n)]), ["S_SENSOR_LATE_START"], "clips", note="左腕晚启动 12 帧", rows=range(12))
    save(22, put(base, "left_wrist_image", [{"bytes": None, "path": None} if i >= n - 12 else lw[i] for i in range(n)]), ["S_SENSOR_EARLY_STOP"], "clips", note="左腕早停止 12 帧", rows=range(n - 12, n))
    save(23, put(base, "left_wrist_image", [{"bytes": None, "path": None} if 70 <= i < 80 else lw[i] for i in range(n)]), ["S_SENSOR_DROPOUT"], "clips", note="左腕中途断流 10 帧", rows=range(70, 80))
    save(24, put(base, "left_wrist_image", [{"bytes": None, "path": None}] * n), ["S_STREAM_MISSING"], None, note="左腕整路缺失")
    save(25, base.drop_columns(["left_wrist_image"]), ["C_SCHEMA_MISSING_FIELD"], None, note="左腕列缺失")
    im = col(base, "image")
    black = _png(np.zeros((224, 224, 3)))
    save(26, put(base, "image", [dict(im[i], bytes=black) if 40 <= i < 60 else im[i] for i in range(n)]), ["C_IMAGE_SCREEN"], "clips", note="主相机黑屏 20 帧", rows=range(40, 60))
    save(27, put(base, "image", [dict(im[i], bytes=im[39]["bytes"]) if 40 <= i < 50 else im[i] for i in range(n)]), ["C_IMAGE_DUPLICATE"], "clips", note="主相机重复帧 10 帧", rows=range(40, 50))
    from PIL import Image, ImageFilter

    def blur(b):
        return _png(np.asarray(Image.open(io.BytesIO(b)).convert("RGB").filter(ImageFilter.GaussianBlur(4))))
    save(28, put(base, "image", [dict(im[i], bytes=blur(im[i]["bytes"])) if 90 <= i < 105 else im[i] for i in range(n)]), ["C_IMAGE_BLUR"], "clips", note="主相机模糊 15 帧", rows=range(90, 105))
    rw = col(base, "right_wrist_image")
    frz0 = _decode(rw[100]["bytes"]).astype(int)
    for kk, D in ((29, 15), (32, 8), (33, 30)):
        frozen = []
        for i in range(n):
            if 101 <= i < 101 + D:
                noisy = np.clip(frz0 + rng.integers(0, 2, frz0.shape), 0, 255)
                frozen.append(dict(rw[i], bytes=_png(noisy)))
            else:
                frozen.append(rw[i])
        detectable = D >= thr["freeze_min_run"]
        save(kk, put(base, "right_wrist_image", frozen), ["S_STREAM_FROZEN"] if detectable else [], "clips" if detectable else None,
             note=f"右腕画面停滞 {D} 帧（非字节重复；检测下限 {thr['freeze_min_run']} 帧，{'应检出' if detectable else '低于下限，不应报'}）",
             rows=range(101, 101 + D) if detectable else None, allow={"S_CAMERA_LAG_SUSPECT"})
    save(30, put(base, "image", [dict(im[i], bytes=im[i]["bytes"][: len(im[i]["bytes"]) // 3]) if i in (30, 31) else im[i] for i in range(n)]), ["C_IMAGE_DECODE"], "clips", note="2 帧 payload 截断", rows=[30, 31])
    small = [dict(im[i], bytes=_png(np.asarray(Image.open(io.BytesIO(im[i]["bytes"])).convert("RGB").resize((112, 112))))) if 10 <= i < 15 else im[i] for i in range(n)]
    save(31, put(base, "image", small), ["C_IMAGE_SHAPE"], "clips", note="5 帧分辨率 112", rows=range(10, 15))
    # single wrist content delayed 6 frames, paths rewritten to the row (no path evidence) -> single-camera lag suspect
    d6 = 6
    lag_lw = [dict(lw[max(0, i - d6)], path=lw[i]["path"]) for i in range(n)]
    save(34, put(base, "left_wrist_image", lag_lw), ["S_CAMERA_LAG_SUSPECT"], None, note="左腕内容整体滞后 6 帧（path 被改写，无索引证据）",
         allow={"C_IMAGE_DUPLICATE", "S_STREAM_FROZEN"})
    # state/actions shifted 4 frames against ALL cameras -> both wrists deviate consistently
    t = base.slice(0, n - 4)
    t = put(t, "state", [[float(np.float32(x)) for x in r] for r in S[4:]])
    t = put(t, "actions", [[float(np.float32(x)) for x in r] for r in A[4:]])
    save(35, t, ["S_VISUAL_KINEMATIC_LAG"], None, note="state/actions 相对三路图像整体提前 4 帧", meta_len=n - 4)
    # ---- joint values ----
    def put_state(t, M, name="state"):
        return put(t, name, [[float(np.float32(x)) for x in r] for r in M])
    M = S.copy()
    nan_cells = [(20, 1), (45, 5), (70, 16), (95, 9), (120, 12)]
    for r, c in nan_cells:
        M[r, c] = np.nan
    save(40, put_state(base, M), ["J_STATE_NONFINITE"], "pass", note="5 个稀疏 NaN 分量（含 Rot6D/夹爪）", rows=[r for r, _ in nan_cells])
    cases[off + 40]["truth"] = {f"state[{c}]@{r}": float(np.float32(S[r, c])) for r, c in nan_cells}
    M = S.copy()
    M[:, 3:9] += rng.normal(0, 1e-3, (n, 6))
    M[:, 13:19] += rng.normal(0, 1e-3, (n, 6))
    save(41, put_state(base, M), ["J_ROT6D_INVALID"], None, note="Rot6D 整条 1e-3 噪声（类 ep81）", allow={"J_STATE_ACTION_MISMATCH"})
    M = S.copy()
    M[88, 0] = 1.6
    save(42, put_state(base, M), ["J_POSITION_ENVELOPE", "J_SPIKE_JUMP"], "pass", note="单帧位置突跳", rows=[88, 89], allow={"J_STATE_ACTION_MISMATCH"})
    cases[off + 42]["truth"] = {"state[0]@88": float(np.float32(S[88, 0]))}
    M = S.copy()
    M[50:55, 9] = 1.3
    save(43, put_state(base, M), ["J_GRIPPER_RANGE"], "clips", note="夹爪越界 5 帧", rows=range(50, 55), allow={"J_STATE_ACTION_MISMATCH", "J_SPIKE_JUMP"})
    Ma = A.copy()
    Ma[100:120, 0:3] += 0.3
    save(44, put_state(base, Ma, "actions"), ["J_STATE_ACTION_MISMATCH"], "clips", note="actions 20 帧偏移 0.3", rows=range(100, 120), allow={"J_SPIKE_JUMP"})
    save(45, put_state(base, np.repeat(S[:1], n, axis=0)), ["V_STATE_FREEZE"], None, note="state 冻结",
         allow={"J_STATE_ACTION_MISMATCH", "S_STREAM_FROZEN", "S_CAMERA_LAG_SUSPECT", "S_VISUAL_KINEMATIC_LAG"})
    (data / f"episode_{off + 50:06d}.parquet").write_bytes(src.read_bytes()[:4096])
    cases[off + 50] = {"base": base_ep, "case": 50, "expect": ["C_FILE_UNREADABLE"], "repair": None, "meta_len": n, "note": "文件截断损坏",
                       "truth_rows": None, "allow": set(), "tasks": task_text}
    save(60, base, ["V_EPISODE_DUPLICATE"], None, note=f"与 ep{off} 完全重复")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--base-episodes", default="12,16,0")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    ref_root, out = Path(a.reference), Path(a.output)
    out.mkdir(parents=True, exist_ok=True)
    bases = [int(x) for x in a.base_episodes.split(",") if x.strip()]

    print("== 1. reference leave-one-episode-out ==")
    lo = loeo(ref_root)
    fp = [r for r in lo if r["defect_codes"]]
    print(f"defect false positives: {len(fp)}/{len(lo)}  value hints: {sum(1 for r in lo if r['value_hints'])}/{len(lo)}")
    for r in lo:
        print("  ", r)

    print("== 2. synthetic faults ==")
    ref_ds = dataset.discover(ref_root)
    ctx = dict(image_columns=ref_ds.image_columns, state_dim=ref_ds.state_dim, action_dim=ref_ds.action_dim)
    thr = calibrate.load_thresholds(None)
    cal_eps = [e for e in ref_ds.episodes if e.episode_id not in bases]
    thr.update(calibrate.calibrate([features.extract(e.path, ctx) for e in cal_eps]))
    thr["_provenance"] = calibrate.provenance([e.path for e in cal_eps], [e.episode_id for e in cal_eps], thr)
    cal = out / "thresholds_without_bases.json"
    cal.write_text(json.dumps(thr, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    print(f"calibrated on {len(cal_eps)} reference episodes (bases {bases} excluded); freeze_min_run={thr['freeze_min_run']} frames")

    syn = out / "synthetic_dataset"
    _safe_rmtree(syn)
    (syn / "data" / "chunk-000").mkdir(parents=True)
    (syn / "meta").mkdir(parents=True)
    (syn / MARK).write_text("selftest synthetic data", encoding="utf-8")
    cases: dict[int, dict] = {}
    for bi, b in enumerate(bases):
        build_synthetic(ref_root, b, syn, 1000 * (bi + 1), thr, cases)
    info = dict(ref_ds.info)
    info.update(total_episodes=len(cases), total_frames=int(sum(c["meta_len"] for c in cases.values())))
    (syn / "meta" / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(syn / "meta" / "tasks.jsonl", "w", encoding="utf-8") as f:
        for k, v in sorted(ref_ds.tasks.items()):
            f.write(json.dumps({"task_index": k, "task": v}, ensure_ascii=False) + "\n")
    with open(syn / "meta" / "episodes.jsonl", "w", encoding="utf-8") as f:
        for e, c in sorted(cases.items()):
            f.write(json.dumps({"episode_index": e, "tasks": c["tasks"], "length": c["meta_len"]}, ensure_ascii=False) + "\n")

    res_dir = out / "synthetic_run"
    _safe_rmtree(res_dir)
    res_dir.mkdir(parents=True)
    (res_dir / MARK).write_text("selftest run output", encoding="utf-8")  # written first: an interrupted run stays deletable
    run(syn, res_dir, calibration=cal, workers=a.workers,
        options={"clean_previous": True, "timestamp_policy": "nominal"}, log=lambda *_: None)
    rep = {int(r["episode_index"]): r for r in csv.DictReader(open(res_dir / "report" / "episode_report.csv", encoding="utf-8-sig"))}
    flags: dict[int, list[set[str]]] = {}
    for r in csv.DictReader(open(res_dir / "report" / "frame_flags.csv", encoding="utf-8-sig")):
        flags.setdefault(int(r["episode_index"]), []).append(set(filter(None, r["issue_codes"].split(";"))))
    audit = list(csv.DictReader(open(res_dir / "governed" / "repair_audit.csv", encoding="utf-8-sig")))
    rows_out, fails = [], []
    TP = FP = FN = 0
    # interpolation is an estimate, not exact recovery: its error must stay well below one typical frame-to-frame motion
    tol_est = 0.25 * float(thr["arm_moving_step"])
    for ep, c in sorted(cases.items()):
        r = rep[ep]
        codes = set(filter(None, r["issue_codes"].split(";")))
        defects = {x for x in codes if not x.startswith("V_")}
        hints = sorted(x for x in codes if x.startswith("V_") and not x.startswith("V_EPISODE") and x not in c["expect"])
        hit = all(x in codes for x in c["expect"])
        extra = sorted(x for x in defects if x not in c["expect"] and x not in c["allow"])
        if c["case"] == 0:
            extra += hints  # the clean control must not even get a value hint
        rep_ok = ""
        if c["repair"] == "pass":
            rep_ok = "OK" if r["final_status"] in ("通过", "价值提示") else "FAIL"
        elif c["repair"] == "clips":
            rep_ok = "OK" if r["policy_before_dedup"] in ("切段使用", "部分修复+切段使用", "修复后整段可用") else "FAIL"
        elif c["repair"] == "shift":
            rep_ok = "OK" if "S_SENSOR_LATE_START" in r["final_issue_codes"] and "S_STREAM_SHIFTED" not in r["final_issue_codes"] else "FAIL"
        if c.get("truth"):
            errs = []
            for key, truth in c["truth"].items():
                field, row = key.split("@")
                vals = [float(x["repaired_value"]) for x in audit if int(x["episode_index"]) == ep and x["field"] == field and int(x["row"]) == int(row)]
                errs.append(abs(vals[0] - truth) if vals else float("inf"))
            rep_ok += f" max|repaired-true|={max(errs):.2e}(容差 {tol_est:.1e})"
            if max(errs) > tol_est:
                rep_ok = "FAIL" + rep_ok[2:] if rep_ok.startswith("OK") else rep_ok
        frame = ""
        if c["truth_rows"] is not None and c["expect"]:
            truth = set(c["truth_rows"])
            pred = {i for i, s in enumerate(flags.get(ep, [])) if s & set(c["expect"])}
            tp, fp_, fn = len(pred & truth), len(pred - truth), len(truth - pred)
            TP, FP, FN = TP + tp, FP + fp_, FN + fn
            frame = f"TP={tp} FP={fp_} FN={fn}"
        if not c["expect"] and c["case"] != 0:
            hit = not (defects - c["allow"])  # below-limit negative case: nothing may be reported
        dims = {d: r[f"{d}_score"] for d in ("structure", "temporal", "sync", "content", "value")}
        lowered = ",".join(f"{d}={v}" for d, v in dims.items() if v not in ("", "100.0"))
        row = {"episode": ep, "base": c["base"], "fault": c["note"], "expected": ";".join(c["expect"]) or "（无缺陷码）", "detected": hit,
               "score_dims_below_100": lowered, "overall_score": r["overall_score"],
               "unexpected_codes": ";".join(extra), "value_hints": ";".join(hints), "frame_level": frame, "policy": r["policy_before_dedup"], "final": r["final_status"],
               "repair_check": rep_ok}
        rows_out.append(row)
        if not hit or extra or rep_ok.startswith("FAIL"):
            fails.append(row)
    # byte-exact restoration of the stream-shift cases
    import pyarrow.parquet as pq
    for bi, b in enumerate(bases):
        ep = 1000 * (bi + 1) + 20
        orig = pq.read_table(next(e.path for e in ref_ds.episodes if e.episode_id == b)).column("left_wrist_image").to_pylist()
        fixed_path = res_dir / "governed" / "data" / "chunk-000" / f"episode_{ep:06d}.parquet"
        ok = False
        if fixed_path.exists():
            fixed = pq.read_table(fixed_path).column("left_wrist_image").to_pylist()
            ok = all(fixed[i]["bytes"] == orig[i]["bytes"] and fixed[i]["path"] == orig[i]["path"] for i in range(7, len(orig))) and \
                all(fixed[i]["bytes"] is None for i in range(7))
        row = {"episode": ep, "base": b, "fault": "重对齐逐字节校验", "expected": "rows 7.. 与原始完全一致、0..6 为空", "detected": ok,
               "unexpected_codes": "", "frame_level": "", "policy": "", "final": "", "repair_check": "OK" if ok else "FAIL"}
        rows_out.append(row)
        if not ok:
            fails.append(row)
    clean_fp = [r for r in rows_out if r["fault"] == "干净对照" and not r["detected"]]
    for r in rows_out:
        mark = "ok " if r not in fails else "XX "
        print(f"{mark}ep{r['episode']:>5} {r['fault']:<40} expect={r['expected']:<40} detected={r['detected']!s:<5} {r['frame_level']:<18} "
              f"policy={r['policy']:<10} repair={r['repair_check']} unexpected={r['unexpected_codes']}")
    prec = TP / (TP + FP) if TP + FP else float("nan")
    rec = TP / (TP + FN) if TP + FN else float("nan")
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else float("nan")
    n_cases = len(cases)
    detected = sum(1 for r in rows_out[:n_cases] if r["detected"])
    summary = {
        "protocol": {"reference": str(ref_root), "loeo_episodes": len(lo), "bases": bases,
                     "synthetic_calibration_episodes": [e.episode_id for e in cal_eps], "freeze_min_run": thr["freeze_min_run"],
                     "thresholds_sha256": thr["_provenance"]["thresholds_sha256"]},
        "reference_loeo": {"episodes": len(lo), "defect_false_positives": len(fp), "value_hint_episodes": sum(1 for r in lo if r["value_hints"]), "details": lo},
        "synthetic": {"cases": n_cases, "cases_meeting_expectation": detected, "cases_failed": len([f for f in fails if f in rows_out[:n_cases]]),
                      "frame_level_row_faults": {"TP": TP, "FP": FP, "FN": FN, "precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4),
                                                 "note": "仅统计注入行已知的帧级故障，按期望问题码逐行比较"},
                      "repair_estimate_tolerance": tol_est,
                      "rows": rows_out},
        "timestamp_policy": "nominal (explicit opt-in for contract-normalization test cases; production default is conservative)",
        "passed": not fails and not fp and not clean_fp and not any(r["value_hints"] for r in lo),
    }
    (out / "selftest_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"synthetic: {detected}/{n_cases} cases meet expectation; frame-level (row faults) P={prec:.3f} R={rec:.3f} F1={f1:.3f} "
          f"(TP={TP} FP={FP} FN={FN}); reference LOEO defect FP {len(fp)}/{len(lo)}; failures={len(fails)}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
