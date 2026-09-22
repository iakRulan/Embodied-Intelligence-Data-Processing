"""Make compact visual evidence for the initial-round answer.

The repaired curves are demonstrations of the proposed safe repair operator
on copied arrays only; the source Parquet files are never overwritten.
"""

from __future__ import annotations

import io
import math
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
from dataset_paths import FIG_DIR, OUT_DIR, resolve_test_root

TEST_ROOT = resolve_test_root()
IMAGE_COLS = ["image", "left_wrist_image", "right_wrist_image"]


def setup_plot() -> None:
    font_name = "DejaVu Sans"
    for font_path in ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/NotoSansSC-VF.ttf"]:
        if Path(font_path).exists():
            font_name = font_manager.FontProperties(fname=font_path).get_name()
            break
    plt.rcParams["font.sans-serif"] = [font_name, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False


def payload_bytes(value: object) -> bytes | None:
    if isinstance(value, dict):
        value = value.get("bytes")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    return None


def decode(value: object) -> np.ndarray | None:
    raw = payload_bytes(value)
    if not raw:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            return np.asarray(im.convert("RGB"))
    except Exception:
        return None


def episode_path(ep: int) -> Path:
    return TEST_ROOT / "data" / "chunk-000" / f"episode_{ep:06d}.parquet"


def pick_case_rows() -> list[tuple[int, int, str]]:
    frames = pd.read_csv(OUT_DIR / "test_image_frames.csv", encoding="utf-8-sig")
    rows: list[tuple[int, int, str]] = []
    choices = [
        (15, "dark_fraction", "黑屏/全黑"),
        (4, "bright_fraction", "过曝/亮屏"),
        (35, "laplacian_var", "低分辨率/块状"),
        (76, "shape_bad", "shape 不合规"),
        (84, "laplacian_var", "严重模糊"),
        (62, "median", "参考正常样例"),
    ]
    for ep, criterion, label in choices:
        f = frames[(frames.dataset_episode == ep) & (frames.stream == "image")].copy()
        if f.empty:
            rows.append((ep, 0, label))
            continue
        if criterion == "dark_fraction":
            idx = pd.to_numeric(f[criterion], errors="coerce").idxmax()
        elif criterion == "bright_fraction":
            idx = pd.to_numeric(f[criterion], errors="coerce").idxmax()
        elif criterion == "shape_bad":
            bad = f[(f.width != 224) | (f.height != 224) | (f.channels != 3)]
            idx = bad.index[0] if not bad.empty else f.index[0]
        elif criterion == "median":
            values = pd.to_numeric(f["laplacian_var"], errors="coerce")
            target = float(values.median())
            idx = (values - target).abs().idxmin()
        else:
            idx = pd.to_numeric(f[criterion], errors="coerce").idxmin()
        rows.append((ep, int(f.loc[idx, "row"]), label))
    return rows


def make_gallery() -> None:
    setup_plot()
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    cases = pick_case_rows()
    fig, axes = plt.subplots(3, len(cases), figsize=(19, 9), squeeze=False)
    stream_names = ["主视角", "左腕", "右腕"]
    for col, (ep, row, label) in enumerate(cases):
        try:
            df = pd.read_parquet(episode_path(ep), columns=IMAGE_COLS + ["frame_index"])
            values = [df.iloc[row][name] for name in IMAGE_COLS] if row < len(df) else [None] * 3
            frame_index = int(df.iloc[row]["frame_index"]) if row < len(df) else -1
        except Exception:
            values = [None] * 3
            frame_index = -1
        for r, (name, value) in enumerate(zip(stream_names, values, strict=True)):
            ax = axes[r, col]
            image = decode(value)
            if image is None:
                ax.text(0.5, 0.5, "无法解码/缺失", ha="center", va="center", fontsize=12, color="#b00020")
                ax.set_facecolor("#f8eeee")
            else:
                ax.imshow(image)
            ax.axis("off")
            if col == 0:
                ax.set_ylabel(name, fontsize=11)
        axes[0, col].set_title(f"ep{ep}  {label}\nrow={row}, frame={frame_index}", fontsize=11)
    fig.suptitle("典型内容异常与正常样例（原始帧抽样）", fontsize=16, y=0.98)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    fig.savefig(FIG_DIR / "effect_gallery.png", dpi=180)
    plt.close(fig)


def safe_array(value: object) -> np.ndarray:
    try:
        return np.asarray(value, dtype=float).reshape(-1)
    except Exception:
        return np.full(20, np.nan)


def interpolate_spikes(x: np.ndarray, threshold: float) -> tuple[np.ndarray, np.ndarray]:
    """Hampel-like local replacement, retaining a boolean quality mask."""
    x = np.asarray(x, dtype=float).copy()
    bad = ~np.isfinite(x)
    for i in range(len(x)):
        lo, hi = max(0, i - 2), min(len(x), i + 3)
        window = x[lo:hi]
        med = np.nanmedian(window)
        mad = np.nanmedian(np.abs(window - med))
        scale = max(1.4826 * mad, threshold * 0.25, 1e-9)
        if not np.isfinite(x[i]) or abs(x[i] - med) > max(6.0 * scale, threshold):
            bad[i] = True
    repaired = x.copy()
    good = np.flatnonzero(~bad & np.isfinite(x))
    if good.size:
        repaired[bad] = np.interp(np.flatnonzero(bad), good, x[good])
    return repaired, bad


def make_trajectory_figure() -> None:
    setup_plot()
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(2, 2, figsize=(16, 9))

    # Timestamp jitter/drop case.
    ep = 25
    ts = pd.read_parquet(episode_path(ep), columns=["timestamp"])["timestamp"].to_numpy(dtype=float)
    dt = np.diff(ts)
    bad_dt = (~np.isfinite(dt)) | (dt <= 0) | (np.abs(dt - 0.1) > 0.01)
    ax = axes[0, 0]
    ax.plot(np.arange(1, len(ts)), dt, lw=1.3, label="原始 Δt")
    ax.plot(np.arange(1, len(ts)), np.full(len(dt), 0.1), "--", lw=1.2, label="重建 Δt=0.1s")
    ax.scatter(np.flatnonzero(bad_dt) + 1, dt[bad_dt], c="#d62728", s=18, label="检测点")
    ax.axhspan(0.09, 0.11, color="#2ca02c", alpha=0.08, label="允许带")
    ax.set_title("ep25：时间戳异常定位与重建示例")
    ax.set_xlabel("row")
    ax.set_ylabel("timestamp delta (data units)")
    ax.legend(fontsize=8, loc="best")

    # Sparse state spike case.
    ep = 54
    df = pd.read_parquet(episode_path(ep), columns=["state", "actions"])
    state = np.vstack([safe_array(x) for x in df["state"]])
    action = np.vstack([safe_array(x) for x in df["actions"]])
    state_x, state_bad = interpolate_spikes(state[:, 0], threshold=0.8)
    ax = axes[0, 1]
    ax.plot(state[:, 0], lw=1.2, label="原始 state[0]")
    ax.plot(state_x, "--", lw=1.5, label="Hampel+插值示例")
    ax.scatter(np.flatnonzero(state_bad), state[:, 0][state_bad], c="#d62728", s=24, label="检测点")
    ax.axhline(1.0, color="#ff7f0e", ls=":", label="位置包络 |x|≤1")
    ax.set_title("ep54：单帧位置越界/突跳")
    ax.set_xlabel("row")
    ax.set_ylabel("normalized position")
    ax.legend(fontsize=8, loc="best")

    # Sparse action spike case.
    ep = 67
    df = pd.read_parquet(episode_path(ep), columns=["actions"])
    action0 = np.array([safe_array(x)[0] for x in df["actions"]], dtype=float)
    action_r, action_bad = interpolate_spikes(action0, threshold=1.05)
    ax = axes[1, 0]
    ax.plot(action0, lw=1.1, label="原始 actions[0]")
    ax.plot(action_r, "--", lw=1.5, label="Hampel+插值示例")
    ax.scatter(np.flatnonzero(action_bad), action0[action_bad], c="#d62728", s=22, label="检测点")
    ax.axhline(1.05, color="#ff7f0e", ls=":", label="动作范围上界")
    ax.axhline(-1.05, color="#ff7f0e", ls=":")
    ax.set_title("ep67：动作通道异常与安全替换")
    ax.set_xlabel("row")
    ax.set_ylabel("normalized action")
    ax.legend(fontsize=8, loc="best")

    # Score before/after estimate.
    report = pd.read_csv(OUT_DIR / "test_quality_report.csv", encoding="utf-8-sig")
    selected = [0, 17, 43, 54, 64, 82]
    sub = report.set_index("episode_index").reindex(selected)
    ax = axes[1, 1]
    x = np.arange(len(selected))
    width = 0.36
    ax.bar(x - width / 2, sub["overall_quality_score"], width, label="修复前", color="#7aa6d8")
    ax.bar(x + width / 2, sub["estimated_quality_after_repair"], width, label="可修复项估计后", color="#4daf7c")
    ax.set_xticks(x, [f"ep{i}" for i in selected])
    ax.set_ylim(0, 105)
    ax.set_ylabel("quality score")
    ax.set_title("代表案例：质量分与保守修复估计")
    ax.legend(fontsize=8)
    ax.text(0.02, 0.02, "估计值不覆盖原始数据，文件级损坏保持低分", transform=ax.transAxes, fontsize=8, color="#555")

    fig.suptitle("检测—定位—安全修复效果展示（基于测试集典型案例）", fontsize=16, y=0.98)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    fig.savefig(FIG_DIR / "trajectory_repair_effect.png", dpi=180)
    plt.close(fig)


if __name__ == "__main__":
    make_gallery()
    make_trajectory_figure()
    print("wrote effect_gallery.png and trajectory_repair_effect.png")
