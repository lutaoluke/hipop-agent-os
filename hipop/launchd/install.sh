#!/bin/bash
# 安装 / 卸载 / 状态检查 hipop launchd 任务
#
# 用法:
#   ./install.sh install    # 安装并启用两个任务
#   ./install.sh uninstall  # 停用并卸载
#   ./install.sh status     # 查看是否在运行
#   ./install.sh test       # 立刻手动跑一次 weekly（验证用）

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
LA="$HOME/Library/LaunchAgents"
mkdir -p "$LA"

PLISTS=("com.hipop.weekly" "com.hipop.daily" "com.hipop.pull")

cmd="${1:-status}"

case "$cmd" in
    install)
        for p in "${PLISTS[@]}"; do
            cp "$DIR/$p.plist" "$LA/$p.plist"
            launchctl unload "$LA/$p.plist" 2>/dev/null || true
            launchctl load "$LA/$p.plist"
            echo "✓ $p 已加载"
        done
        echo ""
        echo "📅 周一 10:00 自动跑全量（wf1+wf2+wf3+wf6+wf5+周报）"
        echo "📦 每天 09:00 自动跑物流（wf3+wf6+日报）"
        echo "🔄 每 30 分钟拉飞书反馈"
        echo "📂 日志: /Users/luke/code/hipop/logs/"
        ;;
    uninstall)
        for p in "${PLISTS[@]}"; do
            launchctl unload "$LA/$p.plist" 2>/dev/null || true
            rm -f "$LA/$p.plist"
            echo "✓ $p 已卸载"
        done
        ;;
    status)
        for p in "${PLISTS[@]}"; do
            if launchctl list | grep -q "$p"; then
                echo "✓ $p 运行中"
                launchctl list | grep "$p"
            else
                echo "✗ $p 未加载"
            fi
        done
        ;;
    test)
        echo "🚀 立刻手动跑一次 weekly..."
        cd "$DIR/.."
        python3 -m scripts.weekly_run
        ;;
    *)
        echo "Usage: $0 {install|uninstall|status|test}"
        exit 1
        ;;
esac
