#!/bin/bash
# 一键端到端：起单机服务器（内置引擎）+ 模拟本地模型，跑验收，再全部收尾。
# MCP 服务端由验收脚本通过 /api/settings 配置成 fake_mcp.py 并重启引擎加载。
set -u
cd "$(dirname "$0")/.." || exit 1

VENV="/c/Users/pc/.workbuddy/binaries/python/envs/default"
PY="$VENV/Scripts/python.exe"
PORT=18765
E2E="D:/神秘小软件/miaozi/_e2e"

PIDS=()
cleanup() {
  echo ""
  echo "--- 收尾 ---"
  for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done
  sleep 1
  for p in "${PIDS[@]}"; do kill -9 "$p" 2>/dev/null; done
}
trap cleanup EXIT

rm -rf "$E2E/data"; mkdir -p "$E2E/data" "$E2E/wf" "server/outputs"

echo "=== 启动服务器 ==="
MIAOZI_DATA_DIR="$E2E/data" PORT=$PORT HOST=127.0.0.1 PYTHONUNBUFFERED=1 \
  "$PY" server/app.py > "$E2E/server.log" 2>&1 &
PIDS+=($!)

echo "=== 启动模拟本地模型 ==="
PYTHONUNBUFFERED=1 "$PY" _e2e/fake_llm.py > "$E2E/llm.log" 2>&1 &
PIDS+=($!)

# 轮询等端口，而不是靠日志（输出可能被缓冲）
ready=0
for i in $(seq 1 30); do
  if "$PY" -c "
import socket,sys
s=socket.socket(); s.settimeout(1)
sys.exit(0 if s.connect_ex(('127.0.0.1',$PORT))==0 else 1)
" 2>/dev/null; then ready=1; break; fi
  sleep 0.5
done
if [ "$ready" != "1" ]; then
  echo "服务器启动失败（端口 $PORT 未监听）"; cat "$E2E/server.log"; exit 1
fi
echo "服务器就绪"
cat "$E2E/server.log"
echo ""

echo "=== 运行验收（脚本会自行配置 fake_mcp 并重启引擎） ==="
"$PY" _e2e/run_e2e.py
RC=$?
echo ""
echo "验收退出码: $RC"
exit $RC
