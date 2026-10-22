#!/bin/bash
# RefSync-QA 决赛算法演示：双击启动（macOS）。关闭这个终端窗口即停止服务。
cd "$(dirname "$0")" || exit 1

# 优先用仓库自带虚拟环境，其次仓库根目录 .venv，最后系统 python3
ROOT="$(cd .. && pwd)"
for CAND in "$ROOT/.venv/bin/python" ".venv/bin/python" "$(command -v python3)"; do
  if [ -n "$CAND" ] && [ -x "$CAND" ]; then
    if "$CAND" -c "import numpy, pyarrow, PIL" >/dev/null 2>&1; then
      PY="$CAND"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "未找到可用的 Python（需要 numpy / pyarrow / pillow）。"
  echo "请在仓库根目录执行：python3 -m venv .venv && .venv/bin/pip install -r 03_复赛算子/Raw_data/requirements.txt"
  read -r -p "按回车退出" _
  exit 1
fi

PORT=8765
# 上一次没关掉的演示服务占着端口时，先结束它
OLD=$(lsof -ti tcp:$PORT 2>/dev/null)
if [ -n "$OLD" ]; then
  echo "结束占用端口 $PORT 的旧进程：$OLD"
  kill $OLD 2>/dev/null
  sleep 1
fi

echo "使用 Python：$PY"
"$PY" server.py --port $PORT
