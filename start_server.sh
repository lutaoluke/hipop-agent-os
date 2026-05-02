#!/bin/bash
# HIPOP Skill Server 启动脚本
# 同时启动 FastAPI 服务 + Cloudflare Tunnel

cd "$(dirname "$0")"

# 检查 cloudflared
if ! command -v cloudflared &>/dev/null; then
    echo "正在安装 cloudflared..."
    brew install cloudflared
fi

# 启动 FastAPI（后台）
echo "启动 Skill Server..."
python3 -m uvicorn hipop.server.main:app --host 0.0.0.0 --port 8765 &
SERVER_PID=$!
echo "Skill Server PID: $SERVER_PID"
sleep 2

# 启动 Cloudflare Tunnel，把本地 8765 暴露到公网
echo ""
echo "启动 Cloudflare Tunnel..."
echo "公网地址将显示在下方，复制 https://xxx.trycloudflare.com 填入飞书 Event Subscription"
echo "────────────────────────────────────────"
cloudflared tunnel --url http://localhost:8765

# Tunnel 关闭后，同时关闭服务器
kill $SERVER_PID 2>/dev/null
