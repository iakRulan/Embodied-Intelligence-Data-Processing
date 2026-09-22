"""Plot before/after repair comparison figures for the 7 repaired episodes.

Timestamp repairs show the dt sequence before vs after frame/fps rebuild;
the ep54 spike repair shows the position channel before vs after
neighbourhood interpolation.  Figures feed the PPT and the answer document.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
from dataset_paths import FIG_DIR, REPAIRED_ROOT, resolve_test_root

SRC = resolve_test_root() / "data" / "chunk-000"
REP = REPAIRED_ROOT / "data" / "chunk-000"
FIG = FIG_DIR

TS_EPS = [13, 25, 73, 75]
SPIKE_EP = 54


def setup_font() -> None:
    font_name = "DejaVu Sans"
    for fp in ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/NotoSansSC-VF.ttf"]:
        if Path(fp).exists():
            font_name = font_manager.FontProperties(fname=fp).get_name()
            break
    plt.rcParams["font.sans-serif"] = [font_name, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False


def plot_timestamp_repairs() -> None:
    fig, axes = plt.subplots(2, 4, figsize=(18, 7))
    for col, ep in enumerate(TS_EPS):
        src = pd.read_parquet(SRC / f"episode_{ep:06d}.parquet", columns=["timestamp", "frame_index"])
        rep = pd.read_parquet(REP / f"episode_{ep:06d}.parquet", columns=["timestamp", "frame_index"])
        dt_b = np.diff(src["timestamp"].to_numpy(float))
        dt_a = np.diff(rep["timestamp"].to_numpy(float))
        x = np.arange(len(dt_b))
        unit_err = dt_b.mean() > 1.0  # ep25: 单位错误导致 Δt≈10 s
        axes[0, col].plot(x, dt_b, lw=0.8, color="#eb5757")
        axes[0, col].axhline(0.1, color="#2f80ed", ls="--", lw=0.8)
        title = f"ep{ep} 修复前 Δt（{len(dt_b)} 间隔）"
        if unit_err:
            title += "\n（时间戳单位错误，Δt≈10 s）"
        axes[0, col].set_title(title, fontsize=11)
        axes[0, col].set_xlabel("帧间隔序号")
        axes[0, col].set_ylabel("Δt (s)")
        if not unit_err:
            axes[0, col].set_ylim(0, 0.2)
        axes[1, col].plot(np.arange(len(dt_a)), dt_a, lw=0.8, color="#27ae60")
        axes[1, col].axhline(0.1, color="#2f80ed", ls="--", lw=0.8)
        axes[1, col].set_title(f"ep{ep} 修复后 Δt（frame_index/fps 重建）", fontsize=11)
        axes[1, col].set_xlabel("帧间隔序号")
        axes[1, col].set_ylabel("Δt (s)")
        axes[1, col].set_ylim(0, 0.2)
    fig.suptitle("时间戳类修复前后对比：抖动/单位异常 → 恒定 0.1 s 采样", fontsize=14)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    fig.savefig(FIG / "repair_before_after_timestamp.png", dpi=180)
    plt.close(fig)


def plot_spike_repair() -> None:
    src = pd.read_parquet(SRC / f"episode_{SPIKE_EP:06d}.parquet", columns=["state"])
    rep = pd.read_parquet(REP / f"episode_{SPIKE_EP:06d}.parquet", columns=["state"])
    s_b = np.stack(src["state"].to_numpy()).astype(float)
    s_a = np.stack(rep["state"].to_numpy()).astype(float)
    changed = np.where(~np.isclose(s_b, s_a, atol=1e-6).all(axis=1))[0]
    cols = sorted(set(np.where(~np.isclose(s_b, s_a, atol=1e-6))[1].tolist()))
    n = len(s_b)
    x = np.arange(n)
    fig, axes = plt.subplots(len(cols), 1, figsize=(14, 3.2 * len(cols)), sharex=True)
    if len(cols) == 1:
        axes = [axes]
    for ax, c in zip(axes, cols):
        ax.plot(x, s_b[:, c], lw=1.0, color="#eb5757", label="修复前 state")
        ax.plot(x, s_a[:, c], lw=1.0, color="#27ae60", ls="--", label="修复后 state")
        for r in changed:
            ax.axvline(r, color="#f2994a", lw=0.7, alpha=0.6)
        ax.set_title(f"ep{SPIKE_EP} 状态维度 state[{c}]：突跳点 → 可信邻域线性插值", fontsize=11)
        ax.set_ylabel("幅值")
        ax.legend(loc="upper right", fontsize=9)
    axes[-1].set_xlabel("帧序号")
    fig.tight_layout()
    fig.savefig(FIG / "repair_before_after_spike_ep54.png", dpi=180)
    plt.close(fig)


def main() -> None:
    FIG.mkdir(parents=True, exist_ok=True)
    setup_font()
    plot_timestamp_repairs()
    plot_spike_repair()
    print("已输出：")
    print(" figures/repair_before_after_timestamp.png")
    print(" figures/repair_before_after_spike_ep54.png")


if __name__ == "__main__":
    main()
