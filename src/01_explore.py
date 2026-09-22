"""探索数据集结构"""
import os
import json
import pandas as pd
import numpy as np

# 数据集根路径
DATA_ROOT = r"H:\竞赛算法\芜湖\具身数据\具身智能多模态数据质量检测算法赛题-初赛数据\初赛数据"

# 找到实际数据目录
def find_dirs(root):
    candidates = []
    for d, subds, files in os.walk(root):
        if '__MACOSX' in d:
            continue
        candidates.append(d)
    return candidates

dirs = find_dirs(DATA_ROOT)
print("=" * 80)
print("目录结构:")
for d in dirs:
    rel = os.path.relpath(d, DATA_ROOT)
    print(f"  {rel}")
print()

# 找到 data 和 meta 目录
data_dir = None
meta_dir = None
for d in dirs:
    if d.endswith('data') and 'chunk' not in d:
        data_dir = d
    if d.endswith('meta'):
        meta_dir = d

print(f"Data 目录: {data_dir}")
print(f"Meta 目录: {meta_dir}")
print()

# 查看 chunk-000 目录
chunk_dir = None
for d in dirs:
    if 'chunk-000' in d and '__MACOSX' not in d:
        chunk_dir = d
        break
print(f"Chunk 目录: {chunk_dir}")
if chunk_dir:
    files = os.listdir(chunk_dir)
    print(f"Episode 数量: {len(files)}")
    print(f"前 5 个: {files[:5]}")
print()

# 读取 meta 信息
if meta_dir:
    print("=" * 80)
    print("Meta 文件:")
    for f in os.listdir(meta_dir):
        path = os.path.join(meta_dir, f)
        print(f"  {f} ({os.path.getsize(path)} bytes)")
        if f.endswith('.json'):
            with open(path, 'r', encoding='utf-8') as fp:
                info = json.load(fp)
            print(json.dumps(info, indent=2, ensure_ascii=False)[:3000])
        elif f.endswith('.jsonl'):
            with open(path, 'r', encoding='utf-8') as fp:
                lines = fp.readlines()
            print(f"  行数: {len(lines)}")
            for line in lines[:3]:
                print(f"  {line.strip()[:300]}")
    print()
