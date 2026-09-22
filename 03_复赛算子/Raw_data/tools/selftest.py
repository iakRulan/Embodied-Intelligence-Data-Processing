# -*- coding: utf-8 -*-
"""Fault-injection self-test + reference leave-one-out.

python tools/selftest.py --reference <参考集目录> --output <dir> [--base-episode 12]

1. Leave-one-episode-out on the reference set: calibrate on the other episodes,
   run the detector on the held-out one (false-positive check).
2. Build a synthetic dataset from one clean reference episode with one injected
   fault per episode (plus a clean control and an exact duplicate), run the full
   operator (calibrated WITHOUT the base episode) and check that every fault is
   detected with the expected code and, where a safe repair exists, that the
   repaired copy restores the original values/bytes.
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
import warnings
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.simplefilter("ignore", RuntimeWarning)

from refsync_qa import calibrate, dataset, detect, features  # noqa: E402
from refsync_qa.pipeline import run  # noqa: E402


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
    rows = []
    for e in ds.episodes:
        thr = calibrate.load_thresholds(None)
        thr.update(calibrate.calibrate([f for k, f in feats.items() if k != e.episode_id]))
        R = detect.analyse(feats[e.episode_id], e.episode_id, ds, thr, {})
        codes = R.all_codes()
        rows.append({"episode": e.episode_id, "defect_codes": [c for c in codes if not c.startswith("V_")],
                     "value_hints": [c for c in codes if c.startswith("V_")]})
    return rows


def build_synthetic(ref_root: Path, base_ep: int, out_root: Path) -> dict[int, dict]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    src = ref_root / "data" / "chunk-000" / f"episode_{base_ep:06d}.parquet"
    base = pq.read_table(src)
    n = base.num_rows
    meta_src = dataset.discover(ref_root)
    task_text = meta_src.episodes_meta[base_ep]["tasks"]
    base_task = meta_src.task_by_text[task_text[0]]
    other_task = [k for k in meta_src.tasks if k != base_task][0]
    if (out_root).exists():
        shutil.rmtree(out_root)
    (out_root / "data" / "chunk-000").mkdir(parents=True)
    (out_root / "meta").mkdir(parents=True)
    rng = np.random.default_rng(20260922)
    cases: dict[int, dict] = {}

    def col(t, name):
        return t.column(name).to_pylist()

    def put(t, name, values):
        i = t.column_names.index(name)
        return t.set_column(i, t.schema.field(i), pa.array(values, type=t.schema.field(i).type))

    def save(ep, t, expect, repair=None, meta_len=None, note=""):
        t = put(t, "episode_index", [ep] * t.num_rows) if "episode_index" in t.column_names and ep not in (910,) else t
        pq.write_table(t, out_root / "data" / "chunk-000" / f"episode_{ep:06d}.parquet")
        cases[ep] = {"expect": expect, "repair": repair, "meta_len": meta_len if meta_len is not None else t.num_rows, "note": note}

    S = np.array(col(base, "state"), dtype=np.float64)
    A = np.array(col(base, "actions"), dtype=np.float64)
    ep = 900
    save(ep, base, [], note="干净对照")
    # ---- scalar fields ----
    t = put(base, "timestamp", [None if i == 50 else v for i, v in enumerate(col(base, "timestamp"))]); save(901, t, ["T_TIMESTAMP_INVALID"], "pass", note="timestamp 单点缺失")
    t = put(base, "index", [None if i == 50 else v for i, v in enumerate(col(base, "index"))]); save(902, t, ["T_INDEX_INVALID"], "pass", note="index 单点缺失")
    t = put(base, "episode_index", [None if i == 50 else 903 for i in range(n)])
    pq.write_table(t, out_root / "data" / "chunk-000" / "episode_000903.parquet")
    cases[903] = {"expect": ["S_EPISODE_INDEX_INVALID"], "repair": "pass", "meta_len": n, "note": "episode_index 单点缺失"}
    t = put(base, "frame_index", [None if i == 50 else v for i, v in enumerate(col(base, "frame_index"))]); save(904, t, ["T_FRAME_INDEX_INVALID"], "clips", note="frame_index 单点缺失")
    t = put(base, "task_index", [other_task] * n); save(905, t, ["S_TASK_META_MISMATCH"], "pass", note="整条换成另一合法任务")
    ts = np.array(col(base, "timestamp"))
    t = put(base, "timestamp", (ts + rng.uniform(-0.006, 0.006, n) * (np.arange(n) > 0)).astype(np.float32).tolist()); save(906, t, ["T_TIMESTAMP_JITTER"], "pass", note="±6ms 抖动")
    t = put(base, "timestamp", (ts * 1000).astype(np.float32).tolist()); save(907, t, ["T_TIMESTAMP_UNIT_MISMATCH"], "pass", note="单位 ms")
    t = put(base, "timestamp", (ts + 3600).astype(np.float32).tolist()); save(908, t, ["T_TIMESTAMP_OFFSET"], "pass", note="整体偏移 3600s")
    keep = [i for i in range(n) if not 60 <= i < 65]
    t = base.take(pa.array(keep)); save(909, t, ["T_FRAME_INDEX_GAP", "T_TIMESTAMP_GAP"], "clips", note="掉 5 帧")
    order = list(range(n)); order[80], order[81] = order[81], order[80]
    t = base.take(pa.array(order)); save(910, put(t, "episode_index", [910] * n), ["T_FRAME_ORDER_ERROR", "T_TIMESTAMP_NON_MONOTONIC"], "clips", note="两帧乱序")
    t = put(base, "episode_index", [999] * n)
    pq.write_table(t, out_root / "data" / "chunk-000" / "episode_000911.parquet")
    cases[911] = {"expect": ["S_EPISODE_INDEX_MISMATCH"], "repair": "pass", "meta_len": n, "note": "episode_index 整条写错"}
    save(912, base, ["T_METADATA_LENGTH_MISMATCH"], "pass", meta_len=n - 10, note="元数据长度错")
    # ---- cameras ----
    lw = col(base, "left_wrist_image")
    k = 7
    shifted = [dict(lw[i + k]) if i + k < n else {"bytes": None, "path": None} for i in range(n)]
    save(920, put(base, "left_wrist_image", shifted), ["S_STREAM_SHIFTED"], "shift", note="左腕晚启动 7 帧并整体前移")
    save(921, put(base, "left_wrist_image", [{"bytes": None, "path": None} if i < 12 else lw[i] for i in range(n)]), ["S_SENSOR_LATE_START"], "clips", note="左腕晚启动 12 帧")
    save(922, put(base, "left_wrist_image", [{"bytes": None, "path": None} if i >= n - 12 else lw[i] for i in range(n)]), ["S_SENSOR_EARLY_STOP"], "clips", note="左腕早停止 12 帧")
    save(923, put(base, "left_wrist_image", [{"bytes": None, "path": None} if 70 <= i < 80 else lw[i] for i in range(n)]), ["S_SENSOR_DROPOUT"], "clips", note="左腕中途断流 10 帧")
    save(924, put(base, "left_wrist_image", [{"bytes": None, "path": None}] * n), ["S_STREAM_MISSING"], None, note="左腕整路缺失")
    save(925, base.drop_columns(["left_wrist_image"]), ["C_SCHEMA_MISSING_FIELD"], None, note="左腕列缺失")
    im = col(base, "image")
    black = _png(np.zeros((224, 224, 3)))
    save(926, put(base, "image", [dict(im[i], bytes=black) if 40 <= i < 60 else im[i] for i in range(n)]), ["C_IMAGE_SCREEN"], "clips", note="主相机黑屏 20 帧")
    save(927, put(base, "image", [dict(im[i], bytes=im[39]["bytes"]) if 40 <= i < 50 else im[i] for i in range(n)]), ["C_IMAGE_DUPLICATE"], "clips", note="主相机重复帧 10 帧")
    from PIL import Image, ImageFilter
    def blur(b):
        return _png(np.asarray(Image.open(io.BytesIO(b)).convert("RGB").filter(ImageFilter.GaussianBlur(4))))
    save(928, put(base, "image", [dict(im[i], bytes=blur(im[i]["bytes"])) if 90 <= i < 105 else im[i] for i in range(n)]), ["C_IMAGE_BLUR"], "clips", note="主相机模糊 15 帧")
    rw = col(base, "right_wrist_image")
    frz0 = _decode(rw[100]["bytes"]).astype(int)
    frozen = []
    for i in range(n):
        if 101 <= i < 116:
            noisy = np.clip(frz0 + rng.integers(0, 2, frz0.shape), 0, 255)
            frozen.append(dict(rw[i], bytes=_png(noisy)))
        else:
            frozen.append(rw[i])
    save(929, put(base, "right_wrist_image", frozen), ["S_STREAM_FROZEN"], "clips", note="右腕画面停滞 15 帧（非字节重复）")
    save(930, put(base, "image", [dict(im[i], bytes=im[i]["bytes"][: len(im[i]["bytes"]) // 3]) if i in (30, 31) else im[i] for i in range(n)]), ["C_IMAGE_DECODE"], "clips", note="2 帧 payload 截断")
    small = [dict(im[i], bytes=_png(np.asarray(Image.open(io.BytesIO(im[i]["bytes"])).convert("RGB").resize((112, 112))))) if 10 <= i < 15 else im[i] for i in range(n)]
    save(931, put(base, "image", small), ["C_IMAGE_SHAPE"], "clips", note="5 帧分辨率 112")
    # ---- joint values ----
    def put_state(t, M, name="state"):
        return put(t, name, [[float(np.float32(x)) for x in r] for r in M])
    M = S.copy()
    nan_cells = [(20, 1), (45, 5), (70, 16), (95, 9), (120, 12)]
    for r, c in nan_cells:
        M[r, c] = np.nan
    save(940, put_state(base, M), ["J_STATE_NONFINITE"], "pass", note="5 个稀疏 NaN 分量（含 Rot6D/夹爪）")
    cases[940]["truth"] = {f"state[{c}]@{r}": float(S[r, c]) for r, c in nan_cells}
    M = S.copy(); M[:, 3:9] += rng.normal(0, 1e-3, (n, 6)); M[:, 13:19] += rng.normal(0, 1e-3, (n, 6))
    save(941, put_state(base, M), ["J_ROT6D_INVALID"], None, note="Rot6D 整条 1e-3 噪声（类 ep81）")
    M = S.copy(); M[88, 0] = 1.6
    save(942, put_state(base, M), ["J_POSITION_ENVELOPE", "J_SPIKE_JUMP"], "pass", note="单帧位置突跳")
    cases[942]["truth"] = {"state[0]@88": float(S[88, 0])}
    M = S.copy(); M[50:55, 9] = 1.3
    save(943, put_state(base, M), ["J_GRIPPER_RANGE"], "clips", note="夹爪越界 5 帧")
    Ma = A.copy(); Ma[100:120, 0:3] += 0.3
    save(944, put_state(base, Ma, "actions"), ["J_STATE_ACTION_MISMATCH"], "clips", note="actions 20 帧偏移 0.3")
    M = np.repeat(S[:1], n, axis=0)
    save(945, put_state(base, M), ["V_STATE_FREEZE"], None, note="state 冻结")
    (out_root / "data" / "chunk-000" / "episode_000950.parquet").write_bytes((src.read_bytes())[: 4096])
    cases[950] = {"expect": ["C_FILE_UNREADABLE"], "repair": None, "meta_len": n, "note": "文件截断损坏"}
    save(960, base, ["V_EPISODE_DUPLICATE"], None, note="与 900 完全重复")
    # meta
    info = dict(meta_src.info)
    info.update(total_episodes=len(cases), total_frames=int(sum(c["meta_len"] for c in cases.values())))
    (out_root / "meta" / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(out_root / "meta" / "tasks.jsonl", "w", encoding="utf-8") as f:
        for k, v in sorted(meta_src.tasks.items()):
            f.write(json.dumps({"task_index": k, "task": v}, ensure_ascii=False) + "\n")
    with open(out_root / "meta" / "episodes.jsonl", "w", encoding="utf-8") as f:
        for e, c in sorted(cases.items()):
            f.write(json.dumps({"episode_index": e, "tasks": task_text, "length": c["meta_len"]}, ensure_ascii=False) + "\n")
    return cases


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--base-episode", type=int, default=12)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    ref_root, out = Path(a.reference), Path(a.output)
    out.mkdir(parents=True, exist_ok=True)

    print("== 1. reference leave-one-out ==")
    lo = loeo(ref_root)
    fp = [r for r in lo if r["defect_codes"]]
    print(f"defect false positives: {len(fp)}/{len(lo)}  value hints: {sum(1 for r in lo if r['value_hints'])}/{len(lo)}")
    for r in lo:
        print("  ", r)

    print("== 2. synthetic faults ==")
    syn = out / "synthetic_dataset"
    cases = build_synthetic(ref_root, a.base_episode, syn)
    # calibrate without the base episode (no leakage)
    ref_ds = dataset.discover(ref_root)
    ctx = dict(image_columns=ref_ds.image_columns, state_dim=ref_ds.state_dim, action_dim=ref_ds.action_dim)
    thr = calibrate.load_thresholds(None)
    thr.update(calibrate.calibrate([features.extract(e.path, ctx) for e in ref_ds.episodes if e.episode_id != a.base_episode]))
    cal = out / "thresholds_without_base.json"
    cal.write_text(json.dumps(thr, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    res_dir = out / "synthetic_run"
    if res_dir.exists():
        shutil.rmtree(res_dir)
    run(syn, res_dir, calibration=cal, workers=a.workers, log=lambda *_: None)
    import csv
    rep = {int(r["episode_index"]): r for r in csv.DictReader(open(res_dir / "report" / "episode_report.csv", encoding="utf-8-sig"))}
    audit = list(csv.DictReader(open(res_dir / "governed" / "repair_audit.csv", encoding="utf-8-sig")))
    rows, tp, fn = [], 0, 0
    for ep, c in sorted(cases.items()):
        r = rep[ep]
        codes = set(filter(None, r["issue_codes"].split(";")))
        hit = all(x in codes for x in c["expect"]) if c["expect"] else not [x for x in codes if not x.startswith("V_EPISODE")]
        extra = sorted(x for x in codes if x not in c["expect"])
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
            rep_ok += f" max|repaired-true|={max(errs):.2e}"
        tp += hit
        fn += not hit
        rows.append({"episode": ep, "fault": c["note"], "expected": ";".join(c["expect"]), "detected": hit,
                     "other_codes": ";".join(extra), "policy": r["policy_before_dedup"], "final": r["final_status"], "repair_check": rep_ok})
    # exact byte restoration for the stream-shift case
    import pyarrow.parquet as pq
    orig = pq.read_table(ref_root / "data" / "chunk-000" / f"episode_{a.base_episode:06d}.parquet").column("left_wrist_image").to_pylist()
    fixed_path = res_dir / "governed" / "data" / "chunk-000" / "episode_000920.parquet"
    if fixed_path.exists():
        fixed = pq.read_table(fixed_path).column("left_wrist_image").to_pylist()
        same = all(fixed[i]["bytes"] == orig[i]["bytes"] and fixed[i]["path"] == orig[i]["path"] for i in range(7, len(orig)))
        head_null = all(fixed[i]["bytes"] is None for i in range(7))
        rows.append({"episode": 920, "fault": "重对齐逐字节校验", "expected": "rows 7.. 与原始完全一致", "detected": same and head_null,
                     "other_codes": "", "policy": "", "final": "", "repair_check": f"bytes+path identical={same}, head null={head_null}"})
    for r in rows:
        print(f"  ep{r['episode']:>4} {r['fault']:<28} expect={r['expected']:<45} detected={r['detected']!s:<5} policy={r['policy']:<10} repair={r['repair_check']}  other={r['other_codes']}")
    summary = {"reference_loeo": {"episodes": len(lo), "defect_false_positives": len(fp), "details": lo},
               "synthetic": {"cases": len(cases), "detected": tp, "missed": fn, "rows": rows}}
    (out / "selftest_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"synthetic detected {tp}/{len(cases)}; reference LOEO defect FP {len(fp)}/{len(lo)}")


if __name__ == "__main__":
    main()
