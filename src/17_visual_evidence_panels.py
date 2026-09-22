"""Build before/after visual evidence panels from the REAL decoded images.

The repair pipeline never modifies image payloads (no fabricated visuals),
so the honest before/after story for image-class defects is:
  (a) same-episode clean segment vs black-screen segment (detection), and
  (b) adjacent duplicate frames shown side by side (byte-identical proof).
For the ep54 numeric spike we show that the image at the spike row is fine,
which is exactly why the numeric layer must exist alongside the image layer.
"""

from __future__ import annotations

import io
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
from dataset_paths import FIG_DIR, REPAIRED_ROOT, resolve_test_root

SRC = resolve_test_root() / "data" / "chunk-000"
FIG = FIG_DIR

CAMS = [("image", "主相机"), ("left_wrist_image", "左腕相机"), ("right_wrist_image", "右腕相机")]


def setup_font() -> None:
    font_name = "DejaVu Sans"
    for fp in ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/NotoSansSC-VF.ttf"]:
        if Path(fp).exists():
            font_name = font_manager.FontProperties(fname=fp).get_name()
            break
    plt.rcParams["font.sans-serif"] = [font_name, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False


def decode(cell) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(cell["bytes"])).convert("RGB"))


def load(ep: int) -> pd.DataFrame:
    return pd.read_parquet(SRC / f"episode_{ep:06d}.parquet")


def show(ax, img, title, ok: bool | None = None) -> None:
    ax.imshow(img)
    ax.set_title(title, fontsize=10, color=("#27ae60" if ok else "#eb5757") if ok is not None else "black")
    ax.axis("off")


def panel_black_segment() -> None:
    """ep69: rows 0-53 clean, 54-127 black, 128+ recovered (same episode)."""
    df = load(69)
    rows = [53, 54, 100, 128]
    labels = ["row53 黑屏前（正常）", "row54 黑屏开始", "row100 黑屏中段", "row128 黑屏后（恢复）"]
    oks = [True, False, False, True]
    fig, axes = plt.subplots(3, 4, figsize=(16, 10))
    for r, (cam, cname) in enumerate(CAMS):
        for c, (row, lab, ok) in enumerate(zip(rows, labels, oks)):
            show(axes[r, c], decode(df[cam].iloc[row]), f"{cname}｜{lab}", ok)
    fig.suptitle("ep69 黑屏段检测证据：同一条轨迹 正常 → 黑屏 → 恢复（算法定位 row54–127，共 74 帧）", fontsize=14)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    fig.savefig(FIG / "evidence_black_segment_ep69.png", dpi=170)
    plt.close(fig)


def panel_duplicate() -> None:
    """ep41: rows 97-110 adjacent duplicate (byte-identical), path repeats."""
    df = load(41)
    rows = [96, 97, 98]
    titles = ["row96（参照帧）", "row97（重复对 A）", "row98（重复对 B，与 A 字节完全相同）"]
    fig, axes = plt.subplots(3, 3, figsize=(12, 10))
    for r, (cam, cname) in enumerate(CAMS):
        for c, (row, t) in enumerate(zip(rows, titles)):
            ok = True if c == 0 else None
            show(axes[r, c], decode(df[cam].iloc[row]), f"{cname}｜{t}", ok)
    fig.suptitle("ep41 相邻重复帧证据：row97=row98 字节级相同（path 编号也相同）→ 真实内容重复，非命名冲突", fontsize=13)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    fig.savefig(FIG / "evidence_duplicate_ep41.png", dpi=170)
    plt.close(fig)


def panel_blur_vs_clean() -> None:
    """ep84 whole-episode blur vs ep62 clean reference (same workspace)."""
    df84, df62 = load(84), load(62)
    pairs = [(df84, 32, "ep84 row32（被标记：严重模糊）", False),
             (df62, 112, "ep62 row112（正常清晰样例）", True)]
    fig, axes = plt.subplots(3, 2, figsize=(9, 10))
    for r, (cam, cname) in enumerate(CAMS):
        for c, (df, row, t, ok) in enumerate(pairs):
            show(axes[r, c], decode(df[cam].iloc[row]), f"{cname}｜{t}", ok)
    fig.suptitle("模糊检测证据：问题帧 vs 正常帧（Laplacian 方差 + 梯度能量 + 字节数联合判据）", fontsize=13)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    fig.savefig(FIG / "evidence_blur_vs_clean.png", dpi=170)
    plt.close(fig)


def panel_spike_image() -> None:
    """ep54 spike row: image is clean while state[0..2] spikes — numeric layer matters."""
    df = load(54)
    src = pd.read_parquet(SRC / "episode_000054.parquet", columns=["state"])
    rep = pd.read_parquet(REPAIRED_ROOT / "data" / "chunk-000" / "episode_000054.parquet", columns=["state"])
    s_b = np.stack(src["state"].to_numpy()).astype(float)
    s_a = np.stack(rep["state"].to_numpy()).astype(float)
    spike_row = int(np.where(~np.isclose(s_b, s_a, atol=1e-6).all(axis=1))[0][0])
    lo, hi = max(0, spike_row - 30), min(len(s_b), spike_row + 31)
    x = np.arange(lo, hi)

    fig = plt.figure(figsize=(16, 7))
    gs = fig.add_gridspec(2, 4, width_ratios=[1, 1, 1, 1.6])
    for c, (cam, cname) in enumerate(CAMS):
        ax = fig.add_subplot(gs[0, c])
        show(ax, decode(df[cam].iloc[spike_row]), f"{cname}｜row{spike_row}\n图像本身正常", True)
    ax_curve = fig.add_subplot(gs[:, 3])
    ax_curve.plot(x, s_b[lo:hi, 0], lw=1.2, color="#eb5757", label="修复前 state[0]")
    ax_curve.plot(x, s_a[lo:hi, 0], lw=1.2, color="#27ae60", ls="--", label="修复后 state[0]")
    ax_curve.axvline(spike_row, color="#f2994a", lw=1)
    ax_curve.set_title(f"同一帧数值层：state[0] 突跳 → 邻域插值（图像层检不出这类缺陷）", fontsize=11)
    ax_curve.set_xlabel("帧序号")
    ax_curve.legend(fontsize=9)
    ax_img2 = fig.add_subplot(gs[1, 0])
    show(ax_img2, decode(df["image"].iloc[spike_row - 1]), f"主相机｜row{spike_row - 1}（前一帧）", True)
    ax_img3 = fig.add_subplot(gs[1, 1])
    show(ax_img3, decode(df["image"].iloc[spike_row]), f"主相机｜row{spike_row}（突跳帧）", True)
    ax_img4 = fig.add_subplot(gs[1, 2])
    show(ax_img4, decode(df["image"].iloc[spike_row + 1]), f"主相机｜row{spike_row + 1}（后一帧）", True)
    fig.suptitle("ep54 数值突跳案例：图像完全正常，缺陷在 state 通道——多模态检测缺一不可", fontsize=14)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    fig.savefig(FIG / "evidence_spike_ep54.png", dpi=170)
    plt.close(fig)


def main() -> None:
    FIG.mkdir(parents=True, exist_ok=True)
    setup_font()
    panel_black_segment()
    panel_duplicate()
    panel_blur_vs_clean()
    panel_spike_image()
    print("已输出：")
    for name in ["evidence_black_segment_ep69.png", "evidence_duplicate_ep41.png",
                 "evidence_blur_vs_clean.png", "evidence_spike_ep54.png"]:
        print(f" figures/{name}")


if __name__ == "__main__":
    main()
