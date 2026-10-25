# -*- coding: utf-8 -*-
"""RefSync-QA 决赛演示 · 本地服务（仅标准库 + 算子同款依赖 numpy / pyarrow / pillow）

    python server.py            # 默认 http://127.0.0.1:8765
    python server.py --port 9000 --no-browser

接口
  GET  /api/info                         数据集与已有运行记录
  POST /api/run?stage=detect|govern&mode=live|replay
  GET  /api/job?stage=..&from=N          运行事件（长轮询）
  GET  /api/result?stage=..              该阶段全部结果（来自算子真实输出文件）
  GET  /api/episode?stage=..&ep=N        单条轨迹：信号、逐帧问题码、区间、修复审计
  GET  /api/frame?stage=..&ep=N&row=R&cam=image&src=raw|after   相机帧 PNG
  GET  /api/motion?stage=..&ep=N         画面运动能量 vs 机械臂速度（相机重对齐证据）
  GET  /api/artifacts?stage=..  /api/preview?stage=..&path=..  POST /api/open?stage=..&path=..
  GET  /api/repairs?stage=..             修复说明：方法与条件、逐条修复、拒修与候选方案
  GET  /api/sessions                     历史结果：按数据分组，最近一次检测 / 修复
  GET  /api/export?stage=..&what=train|report|review   可用数据包 / 质检报告 / 复核清单
  导入数据（每次运行一个 stage = imp-…；页面流程：导入数据 → 检测 → 修复 → 对比）
  POST /api/import/pick?kind=file|folder 系统选择框（macOS）
  GET  /api/import/inspect?path=..       识别单个文件 / 数据集 / 普通文件夹 / 多个数据集
  PUT  /api/import/upload?token=..&path=.. 浏览器拖放上传（逐个文件）
  POST /api/import/run  {path, repair}   启动测试，返回 stage id
  GET  /api/import/list                  测试记录
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import mimetypes
import re
import uuid
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from collections import Counter, OrderedDict, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
CFG = json.loads((HERE / "demo_config.json").read_text(encoding="utf-8"))


def cfg_path(key: str) -> Path:
    p = Path(os.path.expanduser(CFG[key]))
    return p if p.is_absolute() else (HERE / p).resolve()


OPERATOR = Path(os.environ["RSQA_DEMO_OPERATOR"]).resolve() if os.environ.get("RSQA_DEMO_OPERATOR") else cfg_path("operator_dir")
INPUT = Path(os.environ["RSQA_DEMO_INPUT"]).resolve() if os.environ.get("RSQA_DEMO_INPUT") else cfg_path("input_dir")
RUNS = Path(os.environ["RSQA_DEMO_RUNS"]).resolve() if os.environ.get("RSQA_DEMO_RUNS") else cfg_path("runs_dir")
STAGES = ("detect", "govern")
CAMS = ("image", "left_wrist_image", "right_wrist_image")
CAT_OF_PREFIX = {"T": "时序", "S": "同步", "C": "内容结构", "J": "内容结构", "V": "数据价值"}
CATS = ["时序", "同步", "内容结构", "数据价值"]

ARTIFACT_NOTES = {
    "report/summary.md": "检测与治理汇总报告（人读版）",
    "report/summary.json": "汇总指标（机读版：检测、评分、治理、计时、阈值指纹）",
    "report/episode_report.csv": "逐轨迹报告：状态、五维评分、问题码、修复与治理决策",
    "report/frame_flags.csv": "逐帧问题码：每一行数据命中的问题码与所在数据流",
    "report/issue_intervals.csv": "问题区间：连续异常帧聚合成起止行号",
    "report/issue_code_dictionary.csv": "问题码字典：类别、证据等级、是否阻断训练",
    "report/duplicate_pairs.csv": "跨轨迹重复关系：覆盖率、顺序与本体一致性",
    "report/thresholds_used.json": "本次使用的阈值（20 条参考集标定）及来源",
    "report/quality_report.xlsx": "Excel 版汇总报告",
    "governed/clips.csv": "训练片段清单：下游只读这些行区间",
    "governed/train_policy.csv": "逐轨迹治理决策：整段可用 / 切段 / 隔离回采",
    "governed/repair_manifest.csv": "修复清单：源文件与副本 SHA256、修复前后问题码与得分",
    "governed/repair_audit.csv": "逐单元格修复审计：原值 → 新值、方法、问题码",
    "governed/repair_candidates.csv": "候选修复（证据不足未执行）及原因",
    "governed/mask_index.csv": "质量掩膜索引：每条轨迹的掩膜文件与有效帧数",
    "governed/meta_patches.csv": "元数据补丁（不改 Parquet）",
    "governed/quality_masks": "逐帧质量掩膜：frame_valid / train_keep / 样本权重",
    "governed/data": "修复副本（Parquet，原数据只读不改）",
    "governed/meta": "治理后元数据（overlay 叠加层声明）",
}


# ----------------------------------------------------------------- helpers
def read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    return [{k.lstrip("﻿"): v for k, v in r.items()} for r in rows]


def fnum(x, default=None):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def clean(o):
    """JSON-safe: NaN/Inf -> None, numpy -> python."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, (np.floating,)):
        v = float(o)
        return v if math.isfinite(v) else None
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, np.ndarray):
        return clean(o.tolist())
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


def split_codes(s: str) -> list[str]:
    return [c for c in (s or "").split(";") if c]


def cat_of(code: str) -> str:
    return CAT_OF_PREFIX.get(code[:1], "内容结构")


IMPORTS = RUNS / "imports"
IMP_RE = re.compile(r"^imp-\d{8}-\d{6}-[0-9a-f]{4}$")
_IMP_LOCK = threading.Lock()
_IMP: dict[str, dict] = {}


def _load_imports() -> None:
    p = IMPORTS / "index.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _IMP.update(data)
    except (OSError, ValueError):
        pass
    for rec in _IMP.values():  # a run that was interrupted by a server restart
        if rec.get("status") == "running":
            rec["status"] = "failed"
            rec["error"] = "服务重启，运行被中断"


def _save_imports() -> None:
    IMPORTS.mkdir(parents=True, exist_ok=True)
    tmp = IMPORTS / "index.json.tmp"
    tmp.write_text(json.dumps(_IMP, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, IMPORTS / "index.json")


def is_import(stage: str) -> bool:
    return bool(IMP_RE.match(stage or "")) and stage in _IMP


def stage_dir(stage: str) -> Path:
    if is_import(stage):
        return Path(_IMP[stage]["out"])
    return RUNS / stage


def stage_input(stage: str) -> Path:
    """Dataset root that the per-episode relative paths in the reports refer to."""
    if is_import(stage):
        try:
            s = json.loads((stage_dir(stage) / "report" / "summary.json").read_text(encoding="utf-8"))
            if s.get("input"):
                return Path(s["input"])
        except (OSError, ValueError):
            pass
        return Path(_IMP[stage].get("root") or _IMP[stage]["input"])
    return INPUT


def is_govern(stage: str) -> bool:
    return stage == "govern" or (is_import(stage) and bool(_IMP[stage].get("repair")))


def has_run(stage: str) -> bool:
    return (stage_dir(stage) / "report" / "summary.json").is_file()


# ----------------------------------------------------------------- runs grouped by input（同一份数据的检测 / 修复）
def _norm(p) -> str:
    try:
        return str(Path(os.path.expanduser(str(p))).resolve())
    except (OSError, RuntimeError):
        return str(p)


def stage_key(stage: str) -> str:
    """这次运行处理的是哪份数据（文件或文件夹）的规范化路径。"""
    if is_import(stage):
        return _norm(_IMP[stage]["input"])
    return _norm(INPUT)


def is_example(key: str) -> bool:
    ex = _norm(INPUT)
    return key == ex or key.startswith(ex + os.sep)


def run_meta(stage: str) -> dict | None:
    """一次成功运行的身份信息：时间、算子版本、数据、条数。"""
    if not has_run(stage):
        return None
    sp = stage_dir(stage) / "report" / "summary.json"

    def build(d: Path):
        try:
            s = json.loads(sp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            s = {}
        return {"version": s.get("operator"), "episodes": (s.get("scope") or {}).get("episodes"),
                "rows": (s.get("scope") or {}).get("rows")}
    m = cached(stage, "meta", build)
    ts = sp.stat().st_mtime
    rec = _IMP.get(stage, {}) if is_import(stage) else {}
    return {"id": stage, "repair": is_govern(stage), "ts": ts, "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)),
            "version": m.get("version"), "episodes": m.get("episodes"), "rows": m.get("rows"), "key": stage_key(stage),
            "kind": rec.get("kind") or "dataset", "name": rec.get("name") or INPUT.name,
            "replayable": (stage_dir(stage) / "demo_events.jsonl").is_file()}


def all_runs() -> list[dict]:
    with _IMP_LOCK:
        ids = [k for k, r in _IMP.items() if r.get("status") == "ok"]
    out = [m for m in (run_meta(s) for s in list(STAGES) + ids) if m]
    return sorted(out, key=lambda m: -m["ts"])


def latest_run(key: str, repair: bool, version: str | None = None, exclude: str | None = None) -> dict | None:
    for m in all_runs():
        if m["key"] == key and m["repair"] == repair and m["id"] != exclude and (version is None or m["version"] == version):
            return m
    return None


def sessions() -> dict:
    """历史结果：按数据分组，每份数据取最近一次检测、最近一次修复。"""
    groups: dict[str, dict] = {}
    for m in all_runs():
        g = groups.setdefault(m["key"], {"key": m["key"], "kind": m["kind"], "name": m["name"], "episodes": m["episodes"],
                                         "example": is_example(m["key"]), "ts": m["ts"], "detect": None, "govern": None})
        slot = "govern" if m["repair"] else "detect"
        if g[slot] is None:
            g[slot] = {k: m[k] for k in ("id", "created", "version", "replayable")}
    items = sorted(groups.values(), key=lambda g: -g["ts"])
    for g in items:
        g["created"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(g["ts"]))
    return clean({"ok": True, "items": items})


# ----------------------------------------------------------------- jobs
class Job:
    def __init__(self, stage: str, mode: str):
        self.stage, self.mode = stage, mode
        self.events: list[dict] = []
        self.done = False
        self.ok: bool | None = None
        self.cond = threading.Condition()
        self.started = time.time()

    def push(self, ev: dict) -> None:
        with self.cond:
            self.events.append(ev)
            self.cond.notify_all()

    def finish(self, ok: bool) -> None:
        with self.cond:
            self.done, self.ok = True, ok
            self.cond.notify_all()


JOBS: dict[str, Job] = {}
JOB_LOCK = threading.Lock()
CACHE: dict[tuple, tuple] = {}
CACHE_LOCK = threading.Lock()


def invalidate(stage: str) -> None:
    with CACHE_LOCK:
        for k in [k for k in CACHE if k[1] == stage]:
            CACHE.pop(k, None)
    FRAMES.clear()


def run_live(job: Job) -> None:
    stage = job.stage
    live = RUNS / f"{stage}_live"
    try:
        if live.exists():
            shutil.rmtree(live)
        live.mkdir(parents=True)
        cmd = [sys.executable, "-u", str(HERE / "runner.py"), "--operator", str(OPERATOR), "--input", str(INPUT),
               "--output", str(live), "--stage", stage]
        if CFG.get("workers"):
            cmd += ["--workers", str(CFG["workers"])]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", bufsize=1, env=env, cwd=str(HERE))
        with open(live / "demo_events.jsonl", "w", encoding="utf-8") as ef:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    if not isinstance(ev, dict):
                        raise ValueError
                except ValueError:
                    ev = {"type": "log", "msg": line}
                ef.write(json.dumps(ev, ensure_ascii=False) + "\n")
                ef.flush()
                job.push(ev)
        rc = proc.wait()
        ok = rc == 0 and (live / "report" / "summary.json").is_file()
        if ok:
            final, prev = stage_dir(stage), RUNS / f"{stage}_prev"
            if prev.exists():
                shutil.rmtree(prev)
            if final.exists():
                final.rename(prev)
            live.rename(final)
            if prev.exists():
                shutil.rmtree(prev, ignore_errors=True)
            invalidate(stage)
        else:
            job.push({"type": "error", "msg": f"算子退出码 {rc}；上一次成功的结果保留不变，可切换回放模式继续演示"})
        job.finish(ok)
    except Exception as exc:  # noqa: BLE001
        job.push({"type": "error", "msg": f"{type(exc).__name__}: {exc}"})
        job.finish(False)


def replay_brief(stage: str, ep: int) -> dict:
    """回放记录里没有实时 brief 时，从已写出的报告里补同一句话。"""
    r = episode_rows(stage).get(ep) or {}
    out = {}
    m = re.search(r"(image|left_wrist_image|right_wrist_image):path偏移([+-]?\d+)",
                  str(r.get("sync_evidence_index_coverage") or ""))
    if m:
        out["shift"] = {"cam": m.group(1), "frames": int(m.group(2))}
    lag = str(r.get("sync_evidence_statistical_lag") or "")
    if lag:
        out["lag"] = lag[:300]
    if r.get("repair_applied"):
        out["applied"] = str(r["repair_applied"])[:400]
    if r.get("repair_rejected"):
        out["rejected"] = str(r["repair_rejected"])[:600]
    return out


CAT_BIT = {"T": 1, "S": 2, "C": 4, "J": 4, "V": 8}


def _hex_mask(codes_per_row: list[str]) -> str:
    out = []
    for s in codes_per_row:
        m = 0
        for c in split_codes(s):
            m |= CAT_BIT.get(c[:1], 4)
        out.append(format(m, "x"))
    return "".join(out)


def replay_rows(stage: str) -> dict[int, dict]:
    """旧回放记录没有逐行掩膜时，从已写出的 frame_flags / repair_audit 补齐（与实时事件同一格式）。"""
    def build(d: Path):
        ff = frame_rows(stage)
        eps = episode_rows(stage)
        scope = {c: m.get("scope") for c, m in code_dictionary(stage).items()}
        changed: dict[int, set] = defaultdict(set)
        ap = d / "governed" / "repair_audit.csv"
        if is_govern(stage) and ap.exists():
            for a in read_csv(ap):
                try:
                    if int(a["row"]) >= 0:
                        changed[int(a["episode_index"])].add(int(a["row"]))
                except (KeyError, ValueError):
                    pass
        out = {}
        for ep, er in eps.items():
            rows = sorted(ff.get(ep, []), key=lambda x: int(x["row"]))
            runs: list = []
            for x in sorted(changed.get(ep, ())):
                if runs and x == runs[-1][1] + 1:
                    runs[-1][1] = x
                else:
                    runs.append([x, x])
            out[ep] = {"mask": _hex_mask([r.get("issue_codes", "") for r in rows]),
                       "fmask": _hex_mask([r.get("final_issue_codes", "") for r in rows]) if is_govern(stage) else "",
                       "fixruns": runs, "fflagged": sum(1 for r in rows if r.get("final_issue_codes")),
                       "epcodes": [c for c in split_codes(er.get("issue_codes")) if scope.get(c) == "轨迹级"],
                       "tier": er.get("evidence_tier")}
        return out
    return cached(stage, "replay_rows", build)


def run_replay(job: Job, speed: float = 1.0) -> None:
    src = stage_dir(job.stage) / "demo_events.jsonl"
    try:
        events = [json.loads(line) for line in src.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        job.push({"type": "error", "msg": "没有可回放的运行记录：请先实时运行一次"})
        job.finish(False)
        return
    for ev in events:
        if ev.get("type") != "episode":
            continue
        try:
            if not ev.get("brief"):
                ev["brief"] = replay_brief(job.stage, int(ev["ep"]))
            if "mask" not in ev:
                for k, v in (replay_rows(job.stage).get(int(ev["ep"])) or {}).items():
                    ev.setdefault(k, v)
        except (KeyError, TypeError, ValueError, OSError):
            pass
    if not any(ev.get("type") == "final" for ev in events):
        try:
            st, fst = Counter(), Counter()
            for r in episode_rows(job.stage).values():
                st[r.get("status")] += 1
                fst[r.get("final_status")] += 1
            k = next((i for i, ev in enumerate(events) if ev.get("type") == "stage" and ev.get("stage") == "outputs"
                      and ev.get("state") == "end"), None)
            if k is not None:
                events.insert(k + 1, {"type": "final", "t": events[k].get("t", 0), "st": dict(st), "fst": dict(fst),
                                      "episodes": sum(st.values())})
        except (OSError, ValueError, KeyError):
            pass
    job.push({"type": "replay", "msg": "回放上一次实测记录（按原始时间节奏）", "recorded": time.strftime(
        "%Y-%m-%d %H:%M", time.localtime(src.stat().st_mtime))})
    t0 = time.perf_counter()
    for ev in events:
        target = float(ev.get("t", 0)) / speed
        while True:
            dt = target - (time.perf_counter() - t0)
            if dt <= 0:
                break
            time.sleep(min(dt, 0.05))
        job.push(ev)
    job.finish(True)


def start_job(stage: str, mode: str) -> dict:
    with JOB_LOCK:
        for j in JOBS.values():
            if not j.done:
                return {"ok": False, "error": f"{j.stage} 正在运行"}
        if mode == "replay" and not (stage_dir(stage) / "demo_events.jsonl").is_file():
            return {"ok": False, "error": "没有可回放的运行记录：请先实时运行一次"}
        job = Job(stage, mode)
        JOBS[stage] = job
        target = run_replay if mode == "replay" else run_live
        threading.Thread(target=target, args=(job,), daemon=True).start()
        return {"ok": True, "stage": stage, "mode": mode}


def job_events(stage: str, start: int, wait: float = 1.0) -> dict:
    job = JOBS.get(stage)
    if job is None:
        return {"exists": False}
    with job.cond:
        if start >= len(job.events) and not job.done:
            job.cond.wait(timeout=wait)
        evs = job.events[start:]
        return {"exists": True, "mode": job.mode, "events": evs, "next": start + len(evs), "done": job.done, "ok": job.ok}


# ----------------------------------------------------------------- dataset info
def dataset_info() -> dict:
    meta = INPUT / "meta"
    info = {}
    try:
        info = json.loads((meta / "info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    tasks = []
    try:
        tasks = [json.loads(x) for x in (meta / "tasks.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        pass
    files = sorted((INPUT / "data").rglob("*.parquet")) if (INPUT / "data").exists() else []
    size = sum(f.stat().st_size for f in files)
    feats = info.get("features", {})
    cams = [k for k, v in feats.items() if isinstance(v, dict) and v.get("dtype") in ("image", "video")]
    return {
        "label": CFG.get("dataset_label", "测试集"),
        "path": str(INPUT),
        "episodes_files": len(files),
        "bytes": size,
        "meta_episodes": info.get("total_episodes"),
        "meta_frames": info.get("total_frames"),
        "fps": info.get("fps"),
        "robot": info.get("robot_type"),
        "format": "LeRobot " + str(info.get("codebase_version", "")),
        "cameras": [{"name": c, "shape": feats[c].get("shape")} for c in cams],
        "state_dim": (feats.get("state", {}).get("shape") or [None])[0],
        "action_dim": (feats.get("actions", {}).get("shape") or [None])[0],
        "tasks": tasks,
    }


def run_brief(stage: str) -> dict | None:
    d = stage_dir(stage)
    if not has_run(stage):
        return None
    try:
        s = json.loads((d / "report" / "summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {"created": time.strftime("%Y-%m-%d %H:%M", time.localtime((d / "report" / "summary.json").stat().st_mtime)),
            "timing": s.get("timing"), "headline": s.get("headline"),
            "replayable": (d / "demo_events.jsonl").is_file()}


_EXAMPLE: dict = {}


def example_info() -> dict:
    """示例数据（demo_config 里的测试集）：每条轨迹的文件、帧数；有检测结果时附上状态，方便现场挑一条。"""
    files = sorted((INPUT / "data").rglob("*.parquet")) if (INPUT / "data").exists() else []
    sig = (str(INPUT), len(files))
    if _EXAMPLE.get("sig") != sig:
        eps = []
        for f, ep in zip(files, _episode_ids(files)):
            try:
                rows = pq.ParquetFile(f).metadata.num_rows
            except Exception:  # noqa: BLE001
                rows = None
            eps.append({"id": ep, "path": str(f), "rows": rows, "bytes": f.stat().st_size})
        _EXAMPLE.clear()
        _EXAMPLE.update({"sig": sig, "episodes": eps})
    eps = [dict(e) for e in _EXAMPLE["episodes"]]
    runs = [m for m in all_runs() if m["key"] == _norm(INPUT)]
    last = runs[0] if runs else None
    gov = next((m for m in runs if m["repair"]), None)
    st = episode_rows(last["id"]) if last else {}
    gv = episode_rows(gov["id"]) if gov else {}
    for e in eps:
        if e["id"] in st:
            e["status"] = st[e["id"]].get("status")
        if e["id"] in gv:
            e["fixed"] = bool(gv[e["id"]].get("repaired_file"))
    return {"path": str(INPUT), "label": CFG.get("dataset_label", "测试集"), "episodes": eps,
            "rows": sum(e["rows"] or 0 for e in eps), "bytes": sum(e["bytes"] for e in eps)}


def api_info() -> dict:
    return clean({
        "dataset": dataset_info(),
        "example": example_info(),
        "runs": {s: run_brief(s) for s in STAGES},
        "host": {"platform": platform.platform(), "cpu": os.cpu_count(), "python": sys.version.split()[0]},
        "operator_dir": str(OPERATOR),
        "team": CFG.get("team"),
        "compare_pair": CFG.get("compare_pair", {}),
    })


# ----------------------------------------------------------------- results
def cached(stage: str, name: str, builder):
    d = stage_dir(stage)
    sp = d / "report" / "summary.json"
    key = (name, stage)
    mtime = sp.stat().st_mtime if sp.exists() else 0
    with CACHE_LOCK:
        hit = CACHE.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
    val = builder(d)
    with CACHE_LOCK:
        CACHE[key] = (mtime, val)
    return val


def frame_rows(stage: str) -> dict[int, list[dict]]:
    def build(d: Path):
        by = defaultdict(list)
        for r in read_csv(d / "report" / "frame_flags.csv"):
            by[int(r["episode_index"])].append(r)
        return by
    return cached(stage, "frame_flags", build)


def episode_rows(stage: str) -> dict[int, dict]:
    return cached(stage, "episode_report", lambda d: {int(r["episode_index"]): r for r in read_csv(d / "report" / "episode_report.csv")})


def code_dictionary(stage: str) -> dict[str, dict]:
    def build(d: Path):
        p = d / "report" / "issue_code_dictionary.csv"
        return {r["issue_code"]: r for r in read_csv(p)} if p.exists() else {}
    return cached(stage, "codes", build)


def mask_strings(stage: str, d: Path, eps: dict[int, dict]) -> tuple[dict, dict, dict]:
    """before: per-row category bitmask (hex char; T=1 S=2 C=4 V=8; '.' no data)
    after : per-row governance state  k=train_keep  r=train_keep & repaired  v=valid not in clip  x=blocked  d=dedup drop
    final : per-row re-detection after repair  r=row changed by a repair, otherwise the category bitmask of final codes"""
    ff = frame_rows(stage)
    bit = {"时序": 1, "同步": 2, "内容结构": 4, "数据价值": 8}
    audit_rows: dict[int, set] = defaultdict(set)
    ap = d / "governed" / "repair_audit.csv"
    if ap.exists():
        for a in read_csv(ap):
            try:
                if int(a["row"]) >= 0:
                    audit_rows[int(a["episode_index"])].add(int(a["row"]))
            except (ValueError, KeyError):
                pass
    before, after, final = {}, {}, {}
    for ep, er in eps.items():
        rows = ff.get(ep, [])
        n = int(er.get("rows") or 0)
        if not rows or n == 0:
            before[ep], after[ep], final[ep] = "", "", ""
            continue
        b = ["0"] * n
        f = ["0"] * n
        aft = ["x"] * n
        for r in rows:
            i = int(r["row"])
            if not 0 <= i < n:
                continue
            m = 0
            for c in split_codes(r.get("issue_codes", "")):
                m |= bit.get(cat_of(c), 0)
            b[i] = format(m, "x")
            m = 0
            for c in split_codes(r.get("final_issue_codes", "")):
                m |= bit.get(cat_of(c), 0)
            f[i] = "r" if i in audit_rows.get(ep, ()) else format(m, "x")
        mp = d / "governed" / "quality_masks" / f"episode_{ep:06d}.csv"
        if mp.exists():
            for r in read_csv(mp):
                i = int(r["row"])
                if not 0 <= i < n:
                    continue
                if r.get("train_keep") == "True":
                    aft[i] = "r" if i in audit_rows.get(ep, ()) else "k"
                elif r.get("dedup_drop") == "True":
                    aft[i] = "d"
                elif r.get("frame_valid") == "True":
                    aft[i] = "v"
        before[ep], after[ep], final[ep] = "".join(b), "".join(aft), "".join(f)
    return before, after, final


def artifacts(stage: str) -> list[dict]:
    d = stage_dir(stage)
    out = []
    if not d.exists():
        return out
    for sub in ("report", "governed"):
        base = d / sub
        if not base.exists():
            continue
        for p in sorted(base.iterdir()):
            rel = f"{sub}/{p.name}"
            if p.is_dir():
                files = [f for f in p.rglob("*") if f.is_file()]
                out.append({"path": rel, "dir": True, "files": len(files), "bytes": sum(f.stat().st_size for f in files),
                            "note": ARTIFACT_NOTES.get(rel, ""),
                            "children": [{"path": str(f.relative_to(d)), "bytes": f.stat().st_size} for f in sorted(files)[:200]]})
            else:
                rows = None
                if p.suffix == ".csv":
                    with open(p, "rb") as f:
                        rows = max(0, sum(1 for _ in f) - 1)
                out.append({"path": rel, "dir": False, "bytes": p.stat().st_size, "rows": rows,
                            "mtime": p.stat().st_mtime, "note": ARTIFACT_NOTES.get(rel, "")})
    return out


def build_result(stage: str) -> dict:
    d = stage_dir(stage)
    s = json.loads((d / "report" / "summary.json").read_text(encoding="utf-8"))
    eps = episode_rows(stage)
    codes = code_dictionary(stage)
    before, after, final = mask_strings(stage, d, eps)
    # frames per category (rows with >=1 code of that category, original detection)
    ff = frame_rows(stage)
    cat_frames = Counter()
    for rows in ff.values():
        for r in rows:
            cs = {cat_of(c) for c in split_codes(r.get("issue_codes", ""))}
            for c in cs:
                cat_frames[c] += 1
    ep_list = []
    for ep, r in sorted(eps.items()):
        ep_list.append({
            "id": ep, "rows": int(r.get("rows") or 0), "status": r.get("status"), "tier": r.get("evidence_tier"),
            "codes": split_codes(r.get("issue_codes")), "final_codes": split_codes(r.get("final_issue_codes")),
            "final_status": r.get("final_status"), "score": fnum(r.get("overall_score")),
            "final_score": fnum(r.get("final_overall_score")),
            "dims": {k: fnum(r.get(f"{k}_score")) for k in ("structure", "temporal", "sync", "content", "value")},
            "flagged_frames": int(fnum(r.get("flagged_frames"), 0) or 0),
            "policy": r.get("train_policy"), "train_frames": int(fnum(r.get("train_frames"), 0) or 0),
            "frame_valid": int(fnum(r.get("frame_valid"), 0) or 0), "clips": int(fnum(r.get("clip_count"), 0) or 0),
            "repaired": bool(r.get("repaired_file")), "applied": r.get("repair_applied"), "rejected": r.get("repair_rejected"),
            "seconds": fnum(r.get("seconds")), "task": r.get("final_task_index") or r.get("task_index"),
            "weight": fnum(r.get("sample_weight")),
        })
    by_code = s.get("detection", {}).get("by_code", {})
    code_list = []
    for c, v in by_code.items():
        meta = codes.get(c, {})
        code_list.append({"code": c, "category": v.get("category") or cat_of(c), "tier": v.get("tier"),
                          "episodes": v.get("episodes", 0), "frames": v.get("frames", 0), "description": v.get("description"),
                          "scope": meta.get("scope"), "blocks": meta.get("blocks_training") == "True"})
    res = {
        "stage": stage,
        "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime((d / "report" / "summary.json").stat().st_mtime)),
        "headline": s.get("headline"), "timing": s.get("timing"), "scope": s.get("scope"),
        "detection": {k: v for k, v in s.get("detection", {}).items() if k != "by_code"},
        "cat_frames": dict(cat_frames), "codes": code_list, "scores": s.get("scores"),
        "governance": s.get("governance"), "dataset_value": s.get("dataset_value"),
        "thresholds_sha256": s.get("calibration_provenance", {}).get("thresholds_sha256"),
        "processing_errors": s.get("processing_errors"),
        "episodes": ep_list, "mask_before": before, "mask_after": after, "mask_final": final,
        "artifacts": artifacts(stage),
        "code_dict": {c: {"category": m.get("category"), "tier": m.get("evidence_tier"), "scope": m.get("scope"),
                          "blocks": m.get("blocks_training") == "True", "description": m.get("description")}
                      for c, m in codes.items()},
    }
    res["kind"] = "govern" if is_govern(stage) else "detect"
    if is_import(stage):
        res["import"] = {k: _IMP[stage].get(k) for k in ("id", "source", "input", "kind", "name", "repair", "created")}
    if is_govern(stage):
        g = d / "governed"
        res["manifest"] = read_csv(g / "repair_manifest.csv") if (g / "repair_manifest.csv").exists() else []
        res["clips"] = read_csv(g / "clips.csv") if (g / "clips.csv").exists() else []
        res["candidates"] = read_csv(g / "repair_candidates.csv") if (g / "repair_candidates.csv").exists() else []
        res["audit_counts"] = dict(Counter(a["episode_index"] for a in read_csv(g / "repair_audit.csv"))) if (g / "repair_audit.csv").exists() else {}
        res["duplicates"] = read_csv(d / "report" / "duplicate_pairs.csv") if (d / "report" / "duplicate_pairs.csv").exists() else []
        # 同一份数据、同一版算子的"只检测"运行：给出 不修不切 / 只切段 / 切段 + 修复 三档的真实对照
        pair = latest_run(stage_key(stage), False, s.get("operator"))
        if pair:
            try:
                ds = json.loads((stage_dir(pair["id"]) / "report" / "summary.json").read_text(encoding="utf-8"))
                tf0 = (ds.get("governance") or {}).get("train_frames") or {}
                res["detect_pair"] = pair["id"]
                res["detect_only_train_frames"] = tf0.get("total")
                res["detect_only_base"] = tf0.get("原本可用")
                res["detect_only_policy"] = (ds.get("governance") or {}).get("policy_counts")
                res["detect_ep_train"] = {str(k): int(fnum(v.get("train_frames"), 0) or 0) for k, v in episode_rows(pair["id"]).items()}
            except (OSError, ValueError):
                pass
        res["compare"] = before_after(stage, s, eps, res.get("detect_only_train_frames"), res.get("detect_only_base"))
    res["run"] = run_meta(stage)
    if res["run"]:
        res["run"]["example"] = is_example(res["run"]["key"])
    return clean(res)


def before_after(stage: str, s: dict, eps: dict[int, dict], detect_only_total=None, detect_only_base=None) -> dict:
    """修复前（原始检测）vs 修复后（对修复副本重新检测）。全部来自算子自己的输出，不需要任何真值标签。"""
    ff = frame_rows(stage)
    blocks = {c: m.get("blocks_training") == "True" for c, m in code_dictionary(stage).items()}
    desc = {c: m.get("description") for c, m in code_dictionary(stage).items()}
    cat_b, cat_a = Counter(), Counter()
    code_fr_b, code_fr_a = Counter(), Counter()
    rows_b = rows_a = blk_b = blk_a = 0
    for rows in ff.values():
        for r in rows:
            cb, ca = split_codes(r.get("issue_codes")), split_codes(r.get("final_issue_codes"))
            rows_b += bool(cb)
            rows_a += bool(ca)
            blk_b += any(blocks.get(c, False) for c in cb)
            blk_a += any(blocks.get(c, False) for c in ca)
            for c in {cat_of(x) for x in cb}:
                cat_b[c] += 1
            for c in {cat_of(x) for x in ca}:
                cat_a[c] += 1
            for c in cb:
                code_fr_b[c] += 1
            for c in ca:
                code_fr_a[c] += 1
    code_ep_b, code_ep_a = Counter(), Counter()
    cat_ep_b, cat_ep_a = Counter(), Counter()
    st_b, st_a = Counter(), Counter()
    sc_b, sc_a = [], []
    changed = []
    for ep, r in sorted(eps.items()):
        cb, ca = split_codes(r.get("issue_codes")), split_codes(r.get("final_issue_codes"))
        for c in cb:
            code_ep_b[c] += 1
        for c in ca:
            code_ep_a[c] += 1
        for c in {cat_of(x) for x in cb}:
            cat_ep_b[c] += 1
        for c in {cat_of(x) for x in ca}:
            cat_ep_a[c] += 1
        st_b[r.get("status")] += 1
        st_a[r.get("final_status")] += 1
        b, a = fnum(r.get("overall_score")), fnum(r.get("final_overall_score"))
        if b is not None:
            sc_b.append(b)
        if a is not None:
            sc_a.append(a)
        if r.get("repaired_file") or set(cb) != set(ca) or r.get("status") != r.get("final_status"):
            changed.append({"ep": ep, "status_before": r.get("status"), "status_after": r.get("final_status"),
                            "score_before": b, "score_after": a, "resolved": [c for c in cb if c not in ca],
                            "remaining": [c for c in ca if c in cb], "new": [c for c in ca if c not in cb],
                            "train_frames": int(fnum(r.get("train_frames"), 0) or 0), "policy": r.get("train_policy")})
    codes = sorted(set(code_ep_b) | set(code_ep_a))
    code_rows = [{"code": c, "category": cat_of(c), "description": desc.get(c) or "", "blocks": blocks.get(c, False),
                  "episodes_before": code_ep_b[c], "episodes_after": code_ep_a[c],
                  "frames_before": code_fr_b[c], "frames_after": code_fr_a[c]} for c in codes]
    code_rows.sort(key=lambda x: (-(x["episodes_before"] - x["episodes_after"]), -(x["frames_before"] - x["frames_after"]), x["code"]))
    tf = (s.get("governance") or {}).get("train_frames") or {}
    mean = lambda xs: round(sum(xs) / len(xs), 2) if xs else None  # noqa: E731
    return {
        "episodes": len(eps), "rows": (s.get("scope") or {}).get("rows"),
        "status_before": dict(st_b), "status_after": dict(st_a),
        "flag_rows_before": rows_b, "flag_rows_after": rows_a,
        "block_rows_before": blk_b, "block_rows_after": blk_a,
        "cat_rows_before": dict(cat_b), "cat_rows_after": dict(cat_a),
        "cat_eps_before": dict(cat_ep_b), "cat_eps_after": dict(cat_ep_a),
        "score_mean_before": mean(sc_b), "score_mean_after": mean(sc_a),
        # 不修不切的基线取"只检测"那次运行（修复运行里，被补丁过的整段可用轨迹会记到"修复恢复"）
        "train_base": detect_only_base if detect_only_base is not None else (tf.get("原本可用", 0) if tf else None),
        "train_detect_only": detect_only_total, "train_total": tf.get("total"),
        "train_parts": {k: v for k, v in tf.items() if k != "total"},
        "codes": code_rows, "changed": changed,
    }


def api_result(stage: str) -> dict:
    if not has_run(stage):
        return {"ok": False, "error": "该阶段还没有运行结果"}
    return {"ok": True, **cached(stage, "result", lambda d: build_result(stage))}


# ----------------------------------------------------------------- per-episode
def to_matrix(col, n: int, dim: int = 20) -> list:
    out = []
    for v in col.to_pylist():
        if isinstance(v, (list, tuple)):
            row = []
            for x in v:
                try:
                    fx = float(x)
                    row.append(fx if math.isfinite(fx) else None)
                except (TypeError, ValueError):
                    row.append(None)
            out.append(row)
        else:
            out.append(None)
    return out


def scalars(col) -> list:
    out = []
    for v in col.to_pylist():
        if isinstance(v, list) and len(v) == 1:
            v = v[0]
        try:
            fv = float(v)
            out.append(fv if math.isfinite(fv) else None)
        except (TypeError, ValueError):
            out.append(None)
    return out


def read_signals(path: Path) -> dict:
    if not path.is_file():
        return {"ok": False, "error": "文件不存在"}
    try:
        pf = pq.ParquetFile(path)
        names = pf.schema_arrow.names
        want = [c for c in ("state", "actions", "timestamp", "frame_index", "index", "task_index") if c in names]
        t = pq.read_table(path, columns=want)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    n = t.num_rows
    out = {"ok": True, "n": n, "columns": names,
           "dtypes": {f.name: str(f.type) for f in pf.schema_arrow if f.name in ("timestamp", "frame_index", "index", "task_index")}}
    for c in ("state", "actions"):
        out[c] = to_matrix(t.column(c), n) if c in want else None
    for c in ("timestamp", "frame_index", "index", "task_index"):
        out[c] = scalars(t.column(c)) if c in want else None
    paths = {}
    for c in CAMS:
        if c in names:
            try:
                paths[c] = pq.read_table(path, columns=[f"{c}.path"]).column(0).to_pylist()
            except Exception:  # noqa: BLE001
                paths[c] = None
    out["paths"] = paths
    return out


def episode_detail(stage: str, ep: int) -> dict:
    eps = episode_rows(stage)
    if ep not in eps:
        return {"ok": False, "error": f"没有 ep{ep}"}
    r = eps[ep]
    d = stage_dir(stage)
    flags = frame_rows(stage).get(ep, [])
    intervals = [x for x in read_csv(d / "report" / "issue_intervals.csv") if int(x["episode_index"]) == ep]
    raw = read_signals(stage_input(stage) / r["file"])
    res = {"ok": True, "stage": stage, "ep": ep, "report": r, "intervals": intervals,
           "row_codes": [x.get("issue_codes", "") for x in sorted(flags, key=lambda x: int(x["row"]))],
           "row_final_codes": [x.get("final_issue_codes", "") for x in sorted(flags, key=lambda x: int(x["row"]))],
           "row_streams": [x.get("streams", "") for x in sorted(flags, key=lambda x: int(x["row"]))],
           "raw": raw, "after": None, "audit": [], "manifest": None, "mask": None, "declared": {}}
    try:  # info.json 声明的字段类型（类型不符的修复要对照它）
        feats = json.loads((stage_input(stage) / "meta" / "info.json").read_text(encoding="utf-8")).get("features", {})
        res["declared"] = {k: (feats.get(k) or {}).get("dtype") for k in ("timestamp", "frame_index", "index", "task_index") if k in feats}
    except (OSError, ValueError, AttributeError):
        pass
    try:
        res["image_evidence"] = image_evidence(stage, ep, intervals, flags) if raw.get("ok") else {}
    except Exception as exc:  # noqa: BLE001 - 证据只是说明，失败不影响页面
        res["image_evidence"] = {"error": str(exc)}
    g = d / "governed"
    mp = g / "quality_masks" / f"episode_{ep:06d}.csv"
    if mp.exists():
        res["mask"] = [{"keep": m["train_keep"] == "True", "valid": m["frame_valid"] == "True", "clip": m.get("clip_id", ""),
                        "est": m.get("estimated_repair") == "True"} for m in read_csv(mp)]
    if is_govern(stage):
        if (g / "repair_audit.csv").exists():
            res["audit"] = [a for a in read_csv(g / "repair_audit.csv") if int(a["episode_index"]) == ep]
        if (g / "repair_manifest.csv").exists():
            res["manifest"] = next((m for m in read_csv(g / "repair_manifest.csv") if int(m["episode_index"]) == ep), None)
        if r.get("repaired_file"):
            res["after"] = read_signals(g / r["repaired_file"])
    return clean(res)


# ----------------------------------------------------------------- frames & motion
class LRU(OrderedDict):
    def __init__(self, cap: int):
        super().__init__()
        self.cap = cap
        self.lock = threading.Lock()

    def get_or(self, key, fn):
        with self.lock:
            if key in self:
                self.move_to_end(key)
                return self[key]
        val = fn()
        with self.lock:
            self[key] = val
            while len(self) > self.cap:
                self.popitem(last=False)
        return val


FRAMES = LRU(10)


def source_path(stage: str, ep: int, src: str) -> Path | None:
    r = episode_rows(stage).get(ep) if has_run(stage) else None
    if not r:
        if src == "raw":
            p = INPUT / "data" / "chunk-000" / f"episode_{ep:06d}.parquet"
            return p if p.is_file() else None
        return None
    if src == "after":
        if not r.get("repaired_file"):
            return None
        return stage_dir(stage) / "governed" / r["repaired_file"]
    return stage_input(stage) / r["file"]


def camera_bytes(path: Path, cam: str) -> list:
    def load():
        return pq.read_table(path, columns=[f"{cam}.bytes"]).column(0).to_pylist()
    return FRAMES.get_or((str(path), cam), load)


def frame_png(stage: str, ep: int, row: int, cam: str, src: str) -> bytes | None:
    if cam not in CAMS:
        return None
    p = source_path(stage, ep, src)
    if p is None or not p.is_file():
        return None
    b = camera_bytes(p, cam)
    if not 0 <= row < len(b):
        return None
    return b[row]


IMAGE_CODES = ("C_IMAGE_BLUR", "C_IMAGE_MOSAIC", "C_IMAGE_SCREEN", "C_IMAGE_DUPLICATE", "C_IMAGE_SHAPE",
               "C_IMAGE_NOISE", "C_IMAGE_EXPOSURE", "C_IMAGE_COLOR")


def _sharpness(b: bytes) -> tuple[float, float] | None:
    """与算子 features._image_metrics 同一口径：灰度拉普拉斯方差、平均梯度。"""
    from PIL import Image
    try:
        rgb = np.asarray(Image.open(io.BytesIO(b)).convert("RGB"), dtype=np.float32)
    except Exception:  # noqa: BLE001
        return None
    g = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    c = g[1:-1, 1:-1]
    lap = 4.0 * c - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]
    grad = (np.abs(np.diff(g, axis=1)).mean() + np.abs(np.diff(g, axis=0)).mean()) / 2.0
    return float(lap.var()), float(grad)


def image_evidence(stage: str, ep: int, intervals: list, flags: list) -> dict:
    """画面类问题的证据：哪路相机、多少帧；模糊另给实测清晰度与判定下界，让页面能说清"为什么说模糊、有多模糊"。"""
    cams: dict[str, set] = defaultdict(set)
    for it in intervals:
        if it.get("issue_code") in IMAGE_CODES:
            for st in re.split(r"[,;]", str(it.get("streams") or "")):
                if st in CAMS:
                    cams[it["issue_code"]].add(st)
    if not cams:
        return {}
    out: dict = {}
    thr = {}
    try:
        thr = json.loads((stage_dir(stage) / "report" / "thresholds_used.json").read_text(encoding="utf-8")).get("blur", {})
    except (OSError, ValueError):
        pass
    p = source_path(stage, ep, "raw")
    for code, cs in cams.items():
        info = {"cams": sorted(cs, key=CAMS.index), "frames": {}}
        for cam in info["cams"]:
            tag = f"{code}@{cam}"
            rows = [int(f["row"]) for f in flags if tag in str(f.get("streams") or "")]
            info["frames"][cam] = len(rows)
            if code != "C_IMAGE_BLUR" or not rows or p is None or not p.is_file() or cam not in thr:
                continue
            try:
                bs = camera_bytes(p, cam)
            except Exception:  # noqa: BLE001
                continue
            pick = rows[:: max(1, len(rows) // 24)][:24]
            m = [(*x, len(bs[r])) for r in pick if 0 <= r < len(bs) and bs[r] for x in [_sharpness(bs[r])] if x]
            if m:
                t = thr[cam]
                lm, gm, bm = t.get("lap_min") or 0, t.get("grad_min") or 0, t.get("byte_min") or 0
                info.setdefault("sharp", {})[cam] = {
                    "lap": float(np.median([x[0] for x in m])), "grad": float(np.median([x[1] for x in m])),
                    "bytes": float(np.median([x[2] for x in m])),
                    "lap_min": lm, "grad_min": gm, "byte_min": bm, "sampled": len(m),
                    # 算子三条判据各命中多少（抽样帧）：两项都低 / 清晰度不到下界 1/4 / 文件体积不到下界 1/5
                    "hit_both": sum(1 for x in m if x[0] < lm and x[1] < gm),
                    "hit_lap4": sum(1 for x in m if x[0] < 0.25 * lm),
                    "hit_bytes": sum(1 for x in m if x[2] < 0.2 * bm)}
        out[code] = info
    return out


def motion(stage: str, ep: int) -> dict:
    from PIL import Image

    def energy(path: Path):
        out = {}
        for cam in CAMS:
            try:
                bs = camera_bytes(path, cam)
            except Exception:  # noqa: BLE001
                continue
            prev, e = None, []
            for b in bs:
                if not b:
                    e.append(None)
                    prev = None
                    continue
                try:
                    g = np.asarray(Image.open(io.BytesIO(b)).convert("L").resize((48, 48)), dtype=np.float32)
                except Exception:  # noqa: BLE001
                    e.append(None)
                    prev = None
                    continue
                e.append(None if prev is None else float(np.mean(np.abs(g - prev))))
                prev = g
            out[cam] = e
        return out

    def key():
        res = {"ok": True, "ep": ep}
        for src in ("raw", "after"):
            p = source_path(stage, ep, src)
            if p is None or not p.is_file():
                continue
            sig = read_signals(p)
            st = sig.get("state") or []
            speed = {"left": [None], "right": [None]}
            for arm, sl in (("left", slice(0, 3)), ("right", slice(10, 13))):
                for i in range(1, len(st)):
                    a, b = st[i - 1], st[i]
                    if a is None or b is None or len(a) < 13 or len(b) < 13 or any(v is None for v in a[sl] + b[sl]):
                        speed[arm].append(None)
                    else:
                        speed[arm].append(float(np.linalg.norm(np.array(b[sl]) - np.array(a[sl]))))
            res[src] = {"energy": energy(p), "speed": speed, "paths": sig.get("paths")}
        return res

    path = stage_dir(stage) / f"demo_motion_{ep}.json"
    if path.is_file() and path.stat().st_mtime >= (stage_dir(stage) / "report" / "summary.json").stat().st_mtime:
        return json.loads(path.read_text(encoding="utf-8"))
    res = clean(key())
    try:
        path.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return res


# ----------------------------------------------------------------- repair explanations (one place for every fix)
CAM_CN = {"image": "场景相机", "left_wrist_image": "左腕相机", "right_wrist_image": "右腕相机"}

REPAIR_METHODS = [
    {"key": "shift", "codes": ["S_STREAM_SHIFTED"], "title": "相机画面整体错位",
     "problem": "某一路相机晚启动或掉帧后，录制时把后面的画面整体往前挤，每一行的画面都比机械臂状态早 k 帧，尾部空出 k 行。",
     "action": "按图像文件名里的帧号把每张图放回它真正对应的行；相机没拍到的那几行显式置空，不合成任何画面。",
     "conditions": "path 帧号恒定偏移 k 且完整、frame_index 连续；对端恰好缺 |k| 帧；独立内容证据过门槛：本臂运动相关 r ≥ 0.6 或跨相机运动相关 r ≥ 0.8，且相关增益 ≥ 0.1；没有方向相反的强证据。",
     "otherwise": "不移动画面，整条保持原样隔离，并写入候选修复清单交人工确认。"},
    {"key": "nonfinite", "codes": ["J_STATE_NONFINITE", "J_ACTION_NONFINITE"], "title": "state / actions 稀疏 NaN",
     "problem": "个别行的个别分量是 NaN/Inf，直接训练会让损失变成 NaN。",
     "action": "Rotation-6D 分量按单位范数与两列正交的数学约束解出（残差 ≤ 1e-4）；位置、夹爪分量按前后帧线性插值；只写缺失的那几格，其余分量逐位不变。",
     "conditions": "受影响行 ≤ 10%；每个缺口连续 ≤ 2 帧；缺口两侧都有可信邻居且帧号连续；不在轨迹首尾。",
     "otherwise": "整段不补（补出来就是猜的），问题行不进入训练。",
     "note": "补出的行在训练时样本权重降为 0.5。"},
    {"key": "spike", "codes": ["J_POSITION_ENVELOPE"], "title": "末端位置孤立单帧越界",
     "problem": "末端位置某一帧突然越过工程上界（|值| > 1.0），前后帧都正常。",
     "action": "只把越界的那个分量改成前后两帧的均值。",
     "conditions": "越界帧数 ≤ max(1, 1% 行数)；每一处都是孤立单帧，前后两帧有效、不越界、帧号连续、没有其他数值问题；前后两帧相差 ≤ 0.5。",
     "otherwise": "不插值：连续多帧越界可能是真实动作或标定问题，插值会抹掉真实信号。"},
    {"key": "task", "codes": ["S_TASK_INDEX_INVALID", "S_TASK_SWITCH_WITHIN_EPISODE", "S_TASK_META_MISMATCH"], "title": "任务标签与元数据不一致",
     "problem": "task_index 为未定义值、轨迹中途切换，或与 episodes.jsonl 登记的任务不一致。",
     "action": "整条统一改为 episodes.jsonl 登记的任务编号；画面、动作、时间戳都不动。",
     "conditions": "episodes.jsonl 中该轨迹只登记了一个任务，并能在 tasks.jsonl 中唯一对应。",
     "otherwise": "不改标签。"},
    {"key": "timestamp", "codes": ["T_TIMESTAMP_UNIT_MISMATCH", "T_TIMESTAMP_OFFSET", "T_TIMESTAMP_JITTER", "T_TIMESTAMP_GAP", "T_TIMESTAMP_NON_MONOTONIC", "T_TIMESTAMP_INVALID"], "title": "时间戳单位或基准问题",
     "problem": "时间戳整体单位错标（×10/100/1000）或整体基准不为 0。",
     "action": "只做整条可逆的单位换算或基准归零，保留真实的采样抖动。",
     "conditions": "frame_index 连续；换算后与 frame_index / fps 的契约在抖动容差（2% 采样周期）内吻合。",
     "otherwise": "默认的 conservative 策略不按标称周期重建时间戳：抖动、倒退、掉帧都保持原样，问题段不进入训练。"},
    {"key": "index", "codes": ["T_INDEX_DISCONTINUITY"], "title": "全局 index 与帧号不同步",
     "problem": "index 列与 frame_index 没有同步递增。",
     "action": "index 按 frame_index + 唯一众数偏移重建。",
     "conditions": "frame_index 本身连续，且同一个偏移覆盖 ≥ 90% 的行。",
     "otherwise": "无法判断哪一列是对的，不重建。"},
    {"key": "dtype", "codes": ["C_SCHEMA_DTYPE_MISMATCH"], "title": "字段存储类型与声明不符",
     "problem": "字段的存储类型与 info.json 声明不一致，值本身可读。",
     "action": "按声明做无损类型转换，每个数值逐位不变。",
     "conditions": "转换前后每个数值完全相等。",
     "otherwise": "可能有损时不转换。"},
    {"key": "meta", "codes": ["T_METADATA_LENGTH_MISMATCH"], "title": "元数据记录长度不符",
     "problem": "episodes.jsonl 记录的长度与实际行数不一致。",
     "action": "只更新元数据记录（元数据补丁），Parquet 文件不改。",
     "conditions": "实际行数可读。", "otherwise": "—"},
    {"key": "isolate", "codes": ["C_SCHEMA_MISSING_FIELD", "C_FILE_UNREADABLE"], "title": "必需字段缺失 / 文件损坏",
     "problem": "缺少必需字段，或 Parquet 文件无法读取。",
     "action": "不修复，整条隔离回采。", "conditions": "—", "otherwise": "—"},
]
SAFETY_RULES = [
    "所有改动只写在副本里（governed/ 叠加层），原始数据只读；副本先写临时文件再原子替换。",
    "写完后重新读回副本，逐个单元格与原文件比对：出现审计表里没有登记的改动，或登记了却没发生的改动，整个副本直接丢弃。",
    "每一格改动都记入 repair_audit.csv（行、字段、原值、新值、方法、问题码），源文件与副本都记录 SHA256。",
    "修复后对副本重新检测（复检），用复检结果决定能否进入训练；修复估计出的样本降权。",
]

REASON_RULES = [
    (r"^时间戳规整", "缺帧不能凭空重建时间戳", "帧号本身不连续（中间缺帧或乱序），无法确定缺掉的是哪几帧；按标称周期补出时间戳等于伪造时钟。"),
    (r"^index 重建", "帧号不连续，索引无法唯一确定", "index 与 frame_index 不同步，而 frame_index 自己也不连续，无法判断哪一列是对的。"),
    (r"^时间戳", "时间戳偏差不能无损还原，不伪造时钟", "只有整条可逆的单位换算或基准归零能把时间戳还原到 frame_index/fps（误差在抖动容差内）时才修；抖动、倒退，或换算 / 归零后仍对不上的，保留真实测量时间，不按 1/fps 规整。"),
    (r"稀疏补值", "NaN 太多或缺口太长，整段不臆造", "缺失超过 10% 的行，或连续缺口超过 2 帧，补出来的值就是猜测。"),
    (r"越界", "连续越界不是孤立突跳，不插值", "越界持续多帧，可能是真实动作或标定问题，插值会抹掉真实信号。"),
    (r"必需字段缺失", "必需字段缺失，文件级隔离", "缺少检测和训练必需的字段，无从修复。"),
    (r"重对齐", "相机错位缺少内容证据，不移动画面", "path 帧号显示有偏移，但画面内容与参照信号的相关性没有过门槛，或者有相反证据，不能确认是真的错位。"),
    (r"写后逐单元格校验失败", "写后回读核验失败，副本丢弃", "副本读回后与审计记录不一致，按规则整份丢弃。"),
    (r"存储|转换", "类型转换可能有损，不转换", "按声明类型转换会改变数值。"),
    (r"task_index|episode_index", "元数据不能唯一确定，不改标签", "episodes.jsonl / tasks.jsonl 无法唯一确定正确的值。"),
]


def split_clauses(text: str) -> list[str]:
    """Split on '；' only outside brackets (evidence lists inside （…） also use '；')."""
    out, depth, cur = [], 0, []
    for ch in text or "":
        if ch in "（(":
            depth += 1
        elif ch in "）)":
            depth = max(0, depth - 1)
        if ch == "；" and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return [x for x in out if x]


def cam_cn(text: str) -> str:
    for k in ("left_wrist_image", "right_wrist_image"):
        text = text.replace(k, CAM_CN[k])
    return re.sub(r"\bimage\b", CAM_CN["image"], text)


def reason_of(clause: str) -> tuple[str, str]:
    c = clause.strip()
    for pat, label, why in REASON_RULES:
        if re.search(pat, c):
            return label, why
    return c.split("：")[0], ""


def _runs_text(rows: list[int]) -> str:
    rows = sorted(set(rows))
    if not rows:
        return "无"
    out, a, b = [], rows[0], rows[0]
    for x in rows[1:]:
        if x == b + 1:
            b = x
        else:
            out.append((a, b))
            a = b = x
    out.append((a, b))
    return "、".join(f"第 {x} 行" if x == y else f"第 {x}–{y} 行" for x, y in out[:6]) + ("等" if len(out) > 6 else "")


def _path_nums(path: Path, cam: str) -> list:
    try:
        vals = pq.read_table(path, columns=[f"{cam}.path"]).column(0).to_pylist()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for v in vals:
        m = re.search(r"(\d+)\.\w+$", v or "")
        out.append(int(m.group(1)) if m else None)
    return out


def _num(v: str) -> str:
    try:
        f = float(v)
        return f"{f:.4f}".rstrip("0").rstrip(".") if math.isfinite(f) else v
    except (TypeError, ValueError):
        return v


def _task_names(stage: str) -> dict[int, str]:
    out = {}
    p = stage_input(stage) / "meta" / "tasks.jsonl"
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                t = json.loads(line)
                out[int(t["task_index"])] = str(t["task"]).split(":")[0]
    except (OSError, ValueError, KeyError):
        pass
    return out


def _explain_section(stage: str, code: str, rows: list[dict], r: dict, m: dict, clips: list[dict]) -> dict:
    n = int(r.get("rows") or 0)
    if code == "S_STREAM_SHIFTED":
        cam = rows[0]["field"]
        mm = re.search(rf"{cam} 图像整体移回 ([+-]?\d+) 帧", m.get("applied", ""))
        k = int(mm.group(1)) if mm else None
        raw_p = _path_nums(stage_input(stage) / r["file"], cam)
        aft_p = _path_nums(stage_dir(stage) / "governed" / r["repaired_file"], cam) if r.get("repaired_file") else []
        if k is None:
            offs = [p - i for i, p in enumerate(raw_p) if p is not None]
            k = sorted(offs)[len(offs) // 2] if offs else 0
        if not raw_p or not aft_p:  # files not readable here: derive positions from the offset itself
            raw_p = [i + k if 0 <= i + k < n else None for i in range(n)]
            aft_p = [i if 0 <= i - k < n else None for i in range(n)]
        empty_b = [i for i, p in enumerate(raw_p) if p is None]
        empty_a = [i for i, p in enumerate(aft_p) if p is None]
        first = next((p for p in raw_p if p is not None), None)
        ev = re.search(r"独立内容证据\[([^\]]+)\]：(.+?)）[，,]", m.get("applied", ""))
        evidence = cam_cn(f"{ev.group(1)}：{ev.group(2)}") if ev else "内容相关性过门槛"
        new_codes = [c for c in split_codes(m.get("codes_after")) if c not in split_codes(m.get("codes_before"))]
        clip_txt = "、".join(f"第 {c['start_row']}–{c['end_row']} 行（{c['frames']} 帧）" for c in clips) or "无"
        return {
            "title": f"{CAM_CN.get(cam, cam)}整体错位 {k:+d} 帧",
            "problem": f"修复前第 0 行放的是第 {first} 帧的图像，{CAM_CN.get(cam, cam)}每一行的画面都比机械臂状态早 {abs(k)} 帧；"
                       f"{_runs_text(empty_b)}没有图像（共 {len(empty_b)} 行）。",
            "action": f"按图像文件名里的帧号把每张图放回对应行（第 {first} 帧 → 第 {first} 行）；{_runs_text(empty_a)}是这路相机没拍到的帧，"
                      f"显式置空（共 {len(empty_a)} 行），不合成画面。改写 {len(rows)} 格。",
            "why": f"path 帧号整体恒定偏移 {k:+d}，对端恰好缺 {abs(k)} 帧；独立内容证据 {evidence}，超过门槛（本臂 ≥ 0.6 或跨相机 ≥ 0.8）。",
            "note": f"空图数量没有增加（修复前 {len(empty_b)} 张、修复后 {len(empty_a)} 张），只是从错误的位置挪到了真正缺数据的位置；"
                    f"这些行{('标记为 ' + '、'.join(new_codes) + '，') if new_codes else ''}不进入训练。训练片段：{clip_txt}。",
        }
    if code.startswith("S_TASK"):
        orig = Counter(a["original_value"] for a in rows)
        new = rows[0]["repaired_value"]
        names = _task_names(stage)
        name = names.get(int(new)) if str(new).isdigit() else None
        return {
            "title": "任务标签与元数据不一致",
            "problem": f"task_index 原值 {', '.join(f'{v}（{c} 行）' for v, c in orig.items())}，与 episodes.jsonl 登记的任务不一致。",
            "action": f"这 {len(rows)} 行的 task_index 统一改为 {new}" + (f"（{name}）" if name else "") + "；画面、动作、时间戳都不动。",
            "why": "episodes.jsonl 中该轨迹只登记了一个任务，并能在 tasks.jsonl 中唯一对应，答案唯一。",
            "note": "只改标签，不影响任何传感器数据。",
        }
    if code == "C_SCHEMA_DTYPE_MISMATCH":
        a = rows[0]
        return {
            "title": "时间戳存储类型与声明不符",
            "problem": f"{a['field'].replace('.dtype', '')} 存储为 {a['original_value']}，与 info.json 的声明不一致。",
            "action": f"无损转换为 {a['repaired_value']}，{n} 行数值逐位不变。",
            "why": "转换前后每个数值完全相等。",
            "note": "同一条轨迹里与时间单位有关的问题没有改写（见下方拒修原因），保留给下游判断。" if m.get("rejected") else "",
        }
    if code == "J_POSITION_ENVELOPE":
        cells = "；".join(f"第 {a['row']} 行 {a['field']}：{_num(a['original_value'])} → {_num(a['repaired_value'])}" for a in rows[:6])
        return {
            "title": "末端位置孤立单帧越界",
            "problem": f"{len(rows)} 个位置分量在单独一帧越过工程上界（|值| > 1.0），前后帧都正常。",
            "action": f"只把越界分量改为前后两帧均值：{cells}。",
            "why": "每一处都是孤立单帧，前后两帧有效、不越界、帧号连续、没有其他数值问题，且前后两帧相差 ≤ 0.5。",
            "note": "其余分量和其他行都不动。",
        }
    if code in ("J_STATE_NONFINITE", "J_ACTION_NONFINITE"):
        by_method = Counter("Rotation-6D 约束解出" if "Rotation-6D" in a["method"] else "前后帧线性插值" for a in rows)
        rws = sorted({int(a["row"]) for a in rows})
        fields = sorted({a["field"] for a in rows}, key=lambda f: int(re.sub(r"\D", "", f) or 0))
        return {
            "title": f"{'state' if code == 'J_STATE_NONFINITE' else 'actions'} 稀疏 NaN",
            "problem": f"{len(rows)} 个分量为 NaN，分布在 {len(rws)} 行（{_runs_text(rws)}），涉及 {', '.join(fields)}。",
            "action": "、".join(f"{k} {v} 个" for k, v in by_method.items()) + "；只写这几格，其余分量逐位不变，存储类型保持 float。",
            "why": "缺失稀疏：受影响行 ≤ 10%，每个缺口连续 ≤ 2 帧，两侧邻居可信且帧号连续；Rotation-6D 分量由单位范数和正交约束唯一确定（残差 ≤ 1e-4）。",
            "note": f"补出的 {len(rws)} 行标记为估计修复，训练样本权重降为 {r.get('sample_weight') or '0.5'}。",
        }
    if code == "T_METADATA_LENGTH_MISMATCH":
        a = rows[0]
        return {
            "title": "元数据记录长度不符",
            "problem": f"episodes.jsonl 记录长度 {a['original_value']}，实际有 {a['repaired_value']} 行。",
            "action": "只更新元数据记录（写入 governed/meta_patches.csv 与 governed/meta/），Parquet 文件不改。",
            "why": "实际行数可读，答案唯一。", "note": "",
        }
    methods = Counter(a["method"] for a in rows)
    return {"title": code, "problem": f"{code}：{len(rows)} 格", "action": "；".join(f"{k}（{v} 格）" for k, v in methods.items()),
            "why": "", "note": ""}


def explain_repairs(stage: str) -> dict:
    if not has_run(stage) or not is_govern(stage):
        return {"ok": False, "error": "该运行没有执行修复"}

    def build(d: Path):
        g = d / "governed"
        man = read_csv(g / "repair_manifest.csv") if (g / "repair_manifest.csv").exists() else []
        audit = read_csv(g / "repair_audit.csv") if (g / "repair_audit.csv").exists() else []
        cands = read_csv(g / "repair_candidates.csv") if (g / "repair_candidates.csv").exists() else []
        clips = read_csv(g / "clips.csv") if (g / "clips.csv").exists() else []
        eps = episode_rows(stage)
        s = json.loads((d / "report" / "summary.json").read_text(encoding="utf-8"))
        aud_by: dict[int, list] = defaultdict(list)
        for a in audit:
            aud_by[int(a["episode_index"])].append(a)
        clips_by: dict[int, list] = defaultdict(list)
        for c in clips:
            clips_by[int(c["episode_index"])].append(c)
        fixed, refused = [], defaultdict(lambda: {"items": []})
        method_count = Counter()
        for m in man:
            ep = int(m["episode_index"])
            r = eps.get(ep, {})
            before, after = split_codes(m.get("codes_before")), split_codes(m.get("codes_after"))
            base = {
                "ep": ep, "kind": m.get("kind"), "score_before": fnum(m.get("score_before")), "score_after": fnum(m.get("score_after")),
                "status_before": m.get("status_before"), "status_after": m.get("status_after"),
                "resolved": [c for c in before if c not in after], "remaining": [c for c in after if c in before],
                "new": [c for c in after if c not in before], "policy": r.get("train_policy"),
                "train_frames": int(fnum(r.get("train_frames"), 0) or 0), "rows": int(fnum(r.get("rows"), 0) or 0),
                "weight": fnum(r.get("sample_weight")),
                "clips": [f"{c['start_row']}–{c['end_row']}" for c in clips_by.get(ep, [])],
                "source_sha256": m.get("source_sha256"), "repaired_sha256": m.get("repaired_sha256"),
            }
            for clause in split_clauses(m.get("rejected") or ""):
                label, why = reason_of(clause)
                grp = refused[label]
                grp["label"], grp["why"] = label, why
                grp["items"].append({"ep": ep, "detail": cam_cn(clause), "repaired_elsewhere": m.get("kind") != "未修改"})
            if m.get("kind") == "未修改":
                continue
            groups: dict[str, list] = defaultdict(list)
            for a in aud_by.get(ep, []):
                groups[a["repair_code"]].append(a)
            sections = []
            for code, rows in groups.items():
                try:
                    sections.append(_explain_section(stage, code, rows, r, m, clips_by.get(ep, [])))
                except Exception as exc:  # noqa: BLE001
                    sections.append({"title": code, "problem": "", "action": f"{len(rows)} 格", "why": "", "note": f"说明生成失败：{exc}"})
                for meth in REPAIR_METHODS:
                    if code in meth["codes"]:
                        method_count[meth["key"]] += 1
            fixed.append({**base, "cells": sum(1 for a in aud_by.get(ep, []) if int(fnum(a.get("row"), -1) or 0) >= 0),
                          "sections": sections, "applied": m.get("applied"),
                          "rejected_here": m.get("rejected")})
        cand_groups: dict[str, dict] = {}
        for c in cands:
            key = c["issue_code"]
            g2 = cand_groups.setdefault(key, {"code": key, "action": c["proposed_action"], "because": Counter(), "eps": []})
            g2["because"][c["not_applied_because"]] += 1
            g2["eps"].append(int(c["episode_index"]))
        cand_list = [{"code": k, "action": v["action"], "because": v["because"].most_common(1)[0][0], "eps": sorted(set(v["eps"]))}
                     for k, v in cand_groups.items()]
        methods = [{**meth, "fixed": method_count.get(meth["key"], 0)} for meth in REPAIR_METHODS]
        gov = s.get("governance", {})
        return clean({
            "ok": True, "stage": stage, "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime((d / "report" / "summary.json").stat().st_mtime)),
            "totals": {"fixed": len(fixed), "parquet": gov.get("parquet_repaired_copies"), "meta": gov.get("meta_patch_only"),
                       "cells": gov.get("values_changed"), "rejected": gov.get("repairs_rejected"), "candidates": gov.get("repair_candidates"),
                       "resolved": (gov.get("flagged_resolved") or {}).get("total"), "train_frames": (gov.get("train_frames") or {}).get("total")},
            "safety": SAFETY_RULES, "methods": methods, "fixed": fixed,
            "refused": sorted(({"label": k, "why": v["why"], "items": v["items"]} for k, v in refused.items()), key=lambda x: -len(x["items"])),
            "candidates": sorted(cand_list, key=lambda x: -len(x["eps"])),
        })
    return cached(stage, "repairs", build)


# ----------------------------------------------------------------- import testing
_EP_RE = re.compile(r"episode_(\d+)")
_SKIP = {"__MACOSX", ".git", "__pycache__", "_previous_runs"}
MARKER = ".refsync_qa_output.json"


def _marked(root: Path) -> list[Path]:
    try:
        return [m.parent for m in root.rglob(MARKER)]
    except OSError:
        return []


def _under_any(p: Path, dirs: list[Path]) -> bool:
    return any(d == p or d in p.parents for d in dirs)


def _parquets(root: Path) -> list[Path]:
    marks = _marked(root)
    out = []
    for f in root.rglob("*.parquet"):
        rel = f.relative_to(root).parts
        if set(rel) & _SKIP or f.name.startswith("._") or _under_any(f, marks):
            continue
        out.append(f)
    return sorted(out)


def _episode_ids(files: list[Path]) -> list[int]:
    used, ids = set(), []
    for i, f in enumerate(files):
        m = _EP_RE.search(f.stem)
        ep = int(m.group(1)) if m else i
        while ep in used:
            ep += 100000
        used.add(ep)
        ids.append(ep)
    return sorted(ids)


def _lerobot_roots(p: Path) -> list[Path]:
    if (p / "meta" / "info.json").exists():
        return [p]
    marks = _marked(p)
    roots = []
    for info in sorted(p.rglob("meta/info.json")):
        root = info.parent.parent
        if set(root.relative_to(p).parts) & _SKIP or _under_any(root, marks):
            continue
        if (root / "data").exists() and any((root / "data").rglob("*.parquet")):
            roots.append(root)
    return roots


def _meta_brief(root: Path) -> dict:
    try:
        info = json.loads((root / "meta" / "info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    feats = info.get("features", {}) if isinstance(info.get("features"), dict) else {}
    return {"episodes": info.get("total_episodes"), "frames": info.get("total_frames"), "fps": info.get("fps"),
            "format": "LeRobot " + str(info.get("codebase_version", "")),
            "cameras": [k for k, v in feats.items() if isinstance(v, dict) and v.get("dtype") in ("image", "video")]}


def inspect_path(raw: str) -> dict:
    if not raw:
        return {"ok": False, "error": "请先选择文件或文件夹"}
    p = Path(os.path.expanduser(raw.strip().strip('"').strip("'"))).resolve()
    if not p.exists():
        return {"ok": False, "error": f"路径不存在：{p}"}
    if p.is_file():
        if p.suffix.lower() != ".parquet":
            return {"ok": False, "error": "单个文件测试只支持 .parquet 文件"}
        try:
            pf = pq.ParquetFile(p)
            rows, cols, readable = pf.metadata.num_rows, pf.schema_arrow.names, True
        except Exception as exc:  # noqa: BLE001
            rows, cols, readable = None, [], False
            err = f"{type(exc).__name__}: {str(exc)[:120]}"
        root = p.parent.parent.parent if p.parent.name.startswith("chunk-") else p.parent
        has_meta = (root / "meta" / "info.json").exists()
        return clean({"ok": True, "kind": "file", "path": str(p), "name": p.name, "bytes": p.stat().st_size, "rows": rows,
                      "columns": cols, "readable": readable, "read_error": None if readable else err,
                      "ids": _episode_ids([p]), "episodes": 1, "root": str(root), "has_meta": has_meta,
                      "meta": _meta_brief(root) if has_meta else {}})
    roots = _lerobot_roots(p)
    if len(roots) > 1:
        choices = []
        for r in roots:
            fs = _parquets(r)
            choices.append({"path": str(r), "name": str(r.relative_to(p)), "episodes": len(fs), "bytes": sum(f.stat().st_size for f in fs),
                            "meta": _meta_brief(r)})
        return {"ok": True, "kind": "multi", "path": str(p), "name": p.name, "choices": choices}
    target = roots[0] if roots else p
    files = _parquets(target)
    if not files:
        return {"ok": False, "error": "这个文件夹里没有 .parquet 文件"}
    rows = 0
    for f in files:
        try:
            rows += pq.ParquetFile(f).metadata.num_rows
        except Exception:  # noqa: BLE001
            pass
    return clean({"ok": True, "kind": "dataset" if roots else "folder", "path": str(target), "name": target.name,
                  "picked": str(p), "episodes": len(files), "ids": _episode_ids(files), "bytes": sum(f.stat().st_size for f in files),
                  "rows": rows,
                  "has_meta": bool(roots), "meta": _meta_brief(target) if roots else {},
                  "sample": [str(f.relative_to(target)) for f in files[:5]]})


def native_pick(kind: str) -> dict:
    if sys.platform != "darwin":
        return {"ok": False, "error": "系统选择框只在 macOS 上可用；请把文件拖到页面里，或直接粘贴路径"}
    base = INPUT if kind == "folder" else (INPUT / "data" / "chunk-000")
    loc = f' default location (POSIX file "{base}")' if base.exists() else ""
    if kind == "file":
        script = f'POSIX path of (choose file with prompt "选择要单独测试的 Parquet 文件"{loc})'
    else:
        script = f'POSIX path of (choose folder with prompt "选择要批量测试的数据文件夹"{loc})'
    try:
        out = subprocess.run(["osascript", "-e", "tell me to activate", "-e", script], capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"无法打开选择框：{exc}"}
    if out.returncode != 0:
        if "-128" in out.stderr:
            return {"ok": False, "canceled": True}
        return {"ok": False, "error": out.stderr.strip()[:200] or "选择框返回错误"}
    return {"ok": True, "path": out.stdout.strip()}


def _safe_rel(rel: str) -> Path | None:
    rel = rel.replace("\\", "/").lstrip("/")
    parts = [x for x in rel.split("/") if x not in ("", ".")]
    if not parts or any(x == ".." for x in parts):
        return None
    return Path(*parts)


def upload_dir(token: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{8,32}", token or ""):
        raise ValueError("非法上传标识")
    return IMPORTS / "uploads" / token


def _import_out(input_path: Path, iid: str) -> Path:
    """Results go to runs/imports/<id>/out, unless that would be inside the selected input (the operator refuses that)."""
    out = (IMPORTS / iid / "out").resolve()
    inp = input_path.resolve()
    if inp == out or inp in out.parents:
        out = Path.home() / "RefSyncQA_导入测试" / iid / "out"
    return out


def run_import(job: Job, rec: dict) -> None:
    out = Path(rec["out"])
    try:
        out.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-u", str(HERE / "runner.py"), "--operator", str(OPERATOR), "--input", rec["input"],
               "--output", str(out), "--stage", "govern" if rec["repair"] else "detect"]
        if CFG.get("workers"):
            cmd += ["--workers", str(CFG["workers"])]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", bufsize=1, env=env, cwd=str(HERE))
        last_err = ""
        with open(out / "demo_events.jsonl", "w", encoding="utf-8") as ef:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    if not isinstance(ev, dict):
                        raise ValueError
                except ValueError:
                    ev = {"type": "log", "msg": line}
                if ev.get("type") == "error" or line.startswith("[error]"):
                    last_err = ev.get("msg") or line
                ef.write(json.dumps(ev, ensure_ascii=False) + "\n")
                ef.flush()
                job.push(ev)
        rc = proc.wait()
        ok = rc == 0 and (out / "report" / "summary.json").is_file()
        with _IMP_LOCK:
            rec["status"] = "ok" if ok else "failed"
            if ok:
                s = json.loads((out / "report" / "summary.json").read_text(encoding="utf-8"))
                t, det, gov = s.get("timing", {}), s.get("detection", {}), s.get("governance", {})
                rec.update({"version": s.get("operator"),
                            "episodes": t.get("episodes"), "rows": t.get("rows"), "seconds": t.get("total_seconds_incl_all_csv_parquet_outputs"),
                            "flagged": det.get("flagged_episodes"), "flagged_frames": det.get("flagged_frames"),
                            "repaired": gov.get("parquet_repaired_copies"), "meta_patch": gov.get("meta_patch_only"),
                            "train_frames": (gov.get("train_frames") or {}).get("total"), "headline": s.get("headline")})
            else:
                rec["error"] = last_err or f"算子退出码 {rc}"
            _save_imports()
        if not ok:
            job.push({"type": "error", "msg": rec.get("error")})
        invalidate(rec["id"])
        if ok:
            prune_runs(rec)
        job.finish(ok)
    except Exception as exc:  # noqa: BLE001
        with _IMP_LOCK:
            rec["status"], rec["error"] = "failed", f"{type(exc).__name__}: {exc}"
            _save_imports()
        job.push({"type": "error", "msg": rec["error"]})
        job.finish(False)


KEEP_RUNS = 3


def prune_runs(rec: dict) -> None:
    """同一份数据、同一种运行（检测 / 修复）只保留最近 3 次结果，避免反复排练把磁盘写满。
    只清理本页面新流程产生的运行（flow=2），且只删 runs/imports 下由本服务创建的目录。"""
    key, repair = _norm(rec["input"]), bool(rec.get("repair"))
    with _IMP_LOCK:
        same = sorted((r for r in _IMP.values() if r.get("flow") == 2 and r.get("status") == "ok"
                       and bool(r.get("repair")) == repair and _norm(r["input"]) == key),
                      key=lambda r: r["created"], reverse=True)
        old = same[KEEP_RUNS:]
        for r in old:
            _IMP.pop(r["id"], None)
        if old:
            _save_imports()
    for r in old:
        d = IMPORTS / r["id"]
        if d.is_dir() and IMPORTS.resolve() in d.resolve().parents:
            shutil.rmtree(d, ignore_errors=True)
        invalidate(r["id"])


def start_import(body: dict) -> dict:
    info = inspect_path(str(body.get("path") or ""))
    if not info.get("ok"):
        return info
    if info["kind"] == "multi":
        return {"ok": False, "error": "这个文件夹里有多个数据集，请先选择其中一个", "inspect": info}
    if info["kind"] == "file" and not info.get("readable"):
        pass  # still run: the operator reports C_FILE_UNREADABLE, which is itself a detection result
    with JOB_LOCK:
        for j in JOBS.values():
            if not j.done:
                return {"ok": False, "error": "已有运行在进行中，请等它结束"}
        iid = time.strftime("imp-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        input_path = Path(info["path"])
        rec = {"id": iid, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "source": body.get("source") or info.get("picked") or info["path"],
               "input": info["path"], "root": info.get("root") or info["path"], "kind": info["kind"], "name": info["name"],
               "repair": bool(body.get("repair", True)), "status": "running", "ids": info.get("ids"), "flow": 2,
               "uploaded": bool(body.get("uploaded")), "out": str(_import_out(input_path, iid))}
        with _IMP_LOCK:
            _IMP[iid] = rec
            _save_imports()
        job = Job(iid, "live")
        JOBS[iid] = job
        threading.Thread(target=run_import, args=(job, rec), daemon=True).start()
    return {"ok": True, "id": iid, "ids": info.get("ids"), "repair": rec["repair"], "kind": info["kind"], "name": info["name"]}


def list_imports() -> dict:
    with _IMP_LOCK:
        recs = sorted(_IMP.values(), key=lambda r: r["created"], reverse=True)
        return clean({"ok": True, "items": [{k: v for k, v in r.items() if k not in ("ids",)} for r in recs]})


# ----------------------------------------------------------------- previews
def safe_under(stage: str, rel: str) -> Path | None:
    base = stage_dir(stage).resolve()
    p = (base / rel).resolve()
    if base != p and base not in p.parents:
        return None
    return p


def preview(stage: str, rel: str) -> dict:
    p = safe_under(stage, rel)
    if p is None or not p.exists():
        return {"ok": False, "error": "文件不存在"}
    if p.is_dir():
        files = sorted(f for f in p.rglob("*") if f.is_file())
        return {"ok": True, "kind": "dir", "files": [{"path": str(f.relative_to(stage_dir(stage))), "bytes": f.stat().st_size} for f in files[:300]], "count": len(files)}
    suf = p.suffix.lower()
    if suf == ".csv":
        rows = read_csv(p)
        return clean({"ok": True, "kind": "table", "columns": list(rows[0].keys()) if rows else [], "rows": rows[:60], "total": len(rows)})
    if suf == ".parquet":
        pf = pq.ParquetFile(p)
        names = pf.schema_arrow.names
        scal = [c for c in names if c not in CAMS]
        t = pq.read_table(p, columns=scal).slice(0, 30)
        rows = []
        for i in range(t.num_rows):
            row = {}
            for c in scal:
                v = t.column(c)[i].as_py()
                if isinstance(v, list):
                    v = "[" + ", ".join("NaN" if (isinstance(x, float) and math.isnan(x)) else f"{x:.4g}" if isinstance(x, float) else str(x) for x in v[:6]) + (", …]" if len(v) > 6 else "]")
                row[c] = v
            rows.append(row)
        return clean({"ok": True, "kind": "table", "columns": scal, "rows": rows, "total": pf.metadata.num_rows,
                      "schema": [f"{f.name}: {f.type}" for f in pf.schema_arrow]})
    if suf in (".json", ".md", ".jsonl", ".txt"):
        txt = p.read_text(encoding="utf-8", errors="replace")
        return {"ok": True, "kind": "text", "text": txt[:40000], "truncated": len(txt) > 40000}
    return {"ok": True, "kind": "binary", "bytes": p.stat().st_size}


def open_path(stage: str, rel: str) -> dict:
    p = safe_under(stage, rel or ".")
    if p is None or not p.exists():
        return {"ok": False, "error": "路径不存在"}
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(p)] if p.is_file() else ["open", str(p)])
        elif os.name == "nt":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(p if p.is_dir() else p.parent)])
        else:
            return {"ok": False, "error": "当前系统无法打开文件夹", "path": str(p)}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "path": str(p)}


# ----------------------------------------------------------------- exports（第 4 步的三个出口）
TRAIN_README = """RefSync-QA 可用数据包
======================

用法：训练时只读 clips.csv 里列出的行区间。
  - 没有修复的轨迹：从原始数据集读取（原始文件一个字节都没有改）；
  - 修复过的轨迹：读 data/ 下的修复副本（同名、同结构的 Parquet）；
  - meta/ 与 meta_patches.csv：修复后的元数据叠加层；
  - quality_masks/：逐帧 frame_valid / train_keep / 样本权重；
  - train_policy.csv：每条轨迹的去向（整段可用 / 切段 / 隔离回采）。

数据：{name}
运行：{created} · {version}
可训练帧：{frames}
"""


def _zip_dir(zf, base: Path, arc_prefix: str) -> None:
    import zipfile
    for f in sorted(base.rglob("*")):
        if not f.is_file():
            continue
        comp = zipfile.ZIP_STORED if f.suffix.lower() in (".parquet", ".png", ".jpg", ".xlsx") else zipfile.ZIP_DEFLATED
        zf.write(f, arc_prefix + str(f.relative_to(base)), compress_type=comp)


REVIEW_COLS = ["轨迹", "文件", "复检状态", "去向", "可训练帧", "总帧数", "问题", "问题码", "修复", "未修原因", "建议"]


def review_rows(stage: str) -> list[dict]:
    """复核 / 回采清单：复检后仍需复核、或被隔离 / 不可用的轨迹，每条写明问题与建议。"""
    dict_ = code_dictionary(stage)
    out = []
    for ep, r in sorted(episode_rows(stage).items()):
        final_codes = split_codes(r.get("final_issue_codes") or r.get("issue_codes"))
        status = r.get("final_status") or r.get("status")
        policy = r.get("train_policy") or ""
        if status != "需复核" and policy not in ("隔离回采", "文件不可用", "处理失败"):
            continue
        blocking = [c for c in final_codes if (dict_.get(c) or {}).get("blocks_training") == "True"]
        train, rows = int(fnum(r.get("train_frames"), 0) or 0), int(fnum(r.get("rows"), 0) or 0)
        advice = ("文件损坏：重新导出或回采" if policy == "文件不可用" else
                  "整条回采" if train == 0 else "保留训练片段；问题段人工复核或回采")
        out.append({"轨迹": f"ep{ep:02d}", "文件": r.get("file", ""), "复检状态": status, "去向": policy,
                    "可训练帧": train, "总帧数": rows,
                    "问题": "；".join((dict_.get(c) or {}).get("description", c) for c in (blocking or final_codes)),
                    "问题码": ";".join(blocking or final_codes), "修复": r.get("repair_applied") or "",
                    "未修原因": r.get("repair_rejected") or "", "建议": advice})
    return out


def export_file(stage: str, what: str) -> tuple[str, bytes | Path, str]:
    """返回 (下载文件名, 内容或文件路径, MIME)。"""
    import zipfile
    d = stage_dir(stage)
    meta = run_meta(stage) or {}
    name = re.sub(r"[^\w一-鿿.-]+", "_", (meta.get("name") or "data").replace(".parquet", ""))[:40]
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(meta.get("ts") or time.time()))
    if what == "review":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=REVIEW_COLS)
        w.writeheader()
        w.writerows(review_rows(stage))
        return f"RefSyncQA_复核清单_{name}_{stamp}.csv", ("﻿" + buf.getvalue()).encode("utf-8"), "text/csv; charset=utf-8"
    if what not in ("train", "report"):
        raise ValueError("未知的导出类型")
    sub = d / ("governed" if what == "train" else "report")
    if not sub.is_dir():
        raise ValueError("这次运行没有可导出的内容")
    label = "可用数据包" if what == "train" else "质检报告"
    target = d / f"_export_{what}.zip"
    if not target.exists() or target.stat().st_mtime < (d / "report" / "summary.json").stat().st_mtime:
        tmp = target.with_suffix(".tmp")
        with zipfile.ZipFile(tmp, "w") as zf:
            _zip_dir(zf, sub, "")
            if what == "train":
                s = json.loads((d / "report" / "summary.json").read_text(encoding="utf-8"))
                frames = ((s.get("governance") or {}).get("train_frames") or {}).get("total")
                zf.writestr("README.txt", TRAIN_README.format(name=meta.get("name"), created=meta.get("created"),
                                                              version=meta.get("version"), frames=frames))
        os.replace(tmp, target)
    return f"RefSyncQA_{label}_{name}_{stamp}.zip", target, "application/zip"


# ----------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "RefSyncQADemo/1.0"

    def log_message(self, fmt, *args):  # quiet
        if "/api/job" not in (args[0] if args else ""):
            sys.stderr.write("[http] " + (fmt % args) + "\n")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, body: bytes, ctype: str, cache=True):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "max-age=600" if cache else "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _download(self, fname: str, body, ctype: str):
        size = body.stat().st_size if isinstance(body, Path) else len(body)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", fname)
        self.send_header("Content-Disposition", f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(fname)}")
        self.end_headers()
        if isinstance(body, Path):
            with open(body, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)
        else:
            self.wfile.write(body)

    def _q(self):
        u = urllib.parse.urlparse(self.path)
        return u.path, {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}

    def _stage(self, q):
        s = q.get("stage", "detect")
        if s not in STAGES and not is_import(s):
            raise ValueError("未知的运行：" + s)
        return s

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        path, q = self._q()
        try:
            if path == "/api/info":
                return self._json(api_info())
            if path == "/api/job":
                return self._json(job_events(self._stage(q), int(q.get("from", 0))))
            if path == "/api/result":
                return self._json(api_result(self._stage(q)))
            if path == "/api/episode":
                return self._json(episode_detail(self._stage(q), int(q["ep"])))
            if path == "/api/frame":
                b = frame_png(self._stage(q), int(q["ep"]), int(q["row"]), q.get("cam", "image"), q.get("src", "raw"))
                if not b:
                    self.send_response(204)
                    self.end_headers()
                    return
                ctype = "image/png" if b[:4] == b"\x89PNG" else "image/jpeg"
                return self._bytes(b, ctype)
            if path == "/api/motion":
                return self._json(motion(self._stage(q), int(q["ep"])))
            if path == "/api/artifacts":
                return self._json(clean({"ok": True, "items": artifacts(self._stage(q))}))
            if path == "/api/preview":
                return self._json(preview(self._stage(q), q.get("path", "")))
            if path == "/api/repairs":
                return self._json(explain_repairs(self._stage(q)))
            if path == "/api/import/inspect":
                return self._json(inspect_path(q.get("path", "")))
            if path == "/api/import/list":
                return self._json(list_imports())
            if path == "/api/sessions":
                return self._json(sessions())
            if path == "/api/export":
                fname, body, ctype = export_file(self._stage(q), q.get("what", ""))
                return self._download(fname, body, ctype)
            return self._static(path)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self):
        path, q = self._q()
        try:
            if path == "/api/run":
                return self._json(start_job(self._stage(q), "replay" if q.get("mode") == "replay" else "live"))
            if path == "/api/open":
                return self._json(open_path(self._stage(q), q.get("path", "")))
            if path == "/api/import/pick":
                return self._json(native_pick("folder" if q.get("kind") == "folder" else "file"))
            if path == "/api/import/run":
                body = json.loads(self._body() or b"{}")
                return self._json(start_import(body if isinstance(body, dict) else {}))
            return self._json({"ok": False, "error": "unknown"}, 404)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_PUT(self):
        path, q = self._q()
        try:
            if path != "/api/import/upload":
                return self._json({"ok": False, "error": "unknown"}, 404)
            base = upload_dir(q.get("token", ""))
            rel = _safe_rel(q.get("path", ""))
            if rel is None or rel.suffix.lower() not in (".parquet", ".json", ".jsonl"):
                return self._json({"ok": False, "error": "只接收 .parquet / .json / .jsonl 文件"}, 400)
            target = base / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            remaining = int(self.headers.get("Content-Length") or 0)
            with open(target, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            return self._json({"ok": True, "dir": str(base), "file": str(target)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    def _static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        p = (WEB / rel).resolve()
        if WEB.resolve() not in p.parents or not p.is_file():
            self.send_response(404)
            self.end_headers()
            return
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        return self._bytes(p.read_bytes(), ctype, cache=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(CFG.get("port", 8765)))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    RUNS.mkdir(parents=True, exist_ok=True)
    _load_imports()
    for problem, ok in (("算子目录", (OPERATOR / "refsync_qa").is_dir()), ("测试集", (INPUT / "meta" / "info.json").is_file())):
        if not ok:
            print(f"[warn] 找不到{problem}，请检查 demo_config.json", file=sys.stderr)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    url = f"http://{a.host}:{a.port}/"
    print(f"RefSync-QA 演示服务已启动：{url}")
    print(f"  算子：{OPERATOR}\n  数据：{INPUT}\n  输出：{RUNS}")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
