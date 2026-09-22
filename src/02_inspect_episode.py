"""读取一个 episode 查看结构"""
import os
import json
import pandas as pd
import numpy as np

DATA_ROOT = r"H:\竞赛算法\芜湖\具身数据\具身智能多模态数据质量检测算法赛题-初赛数据\初赛数据"
chunk_dir = None
for d, subds, files in os.walk(DATA_ROOT):
    if 'chunk-000' in d and '__MACOSX' not in d:
        chunk_dir = d
        break

ep_path = os.path.join(chunk_dir, 'episode_000000.parquet')
df = pd.read_parquet(ep_path)
print(f"Shape: {df.shape}")
print(f"Columns: {df.columns.tolist()}")
print(f"Dtypes:\n{df.dtypes}")
print()
print("前 3 行:")
for c in df.columns:
    val = df.iloc[0][c]
    if isinstance(val, (bytes, np.ndarray)):
        if isinstance(val, np.ndarray):
            print(f"  {c}: ndarray shape={val.shape} dtype={val.dtype}")
        else:
            print(f"  {c}: bytes len={len(val)}")
    else:
        print(f"  {c}: {val!r}")
print()

# 检查 state 和 actions 维度
print("State 第 0 行:", df['state'].iloc[0])
print("Actions 第 0 行:", df['actions'].iloc[0])
print()
print("Timestamp 前 5:", df['timestamp'].head().tolist())
print("Frame_index 前 5:", df['frame_index'].head().tolist())
print("Episode_index 前 5:", df['episode_index'].head().tolist())
print("Index 前 5:", df['index'].head().tolist())
print("Task_index 前 5:", df['task_index'].head().tolist())
print()
print("Timestamp 差分:", df['timestamp'].diff().describe())
print()
print("Episode length:", len(df))
