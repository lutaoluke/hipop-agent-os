#!/bin/bash
# 安装 / 卸载 / 状态检查 hipop launchd 任务
#
# 用法:
#   ./install.sh install    # 安装并启用全部任务（自动发现本目录所有 *.plist）
#   ./install.sh uninstall  # 停用并卸载
#   ./install.sh status     # 查看是否在运行
#   ./install.sh test       # 立刻手动跑一次 weekly（验证用）
#
# 关键：plist 列表自动发现本目录 `*.plist`，新增守护任务无需改本脚本——
# 避免多分支都改同一 PLISTS 行连环 merge 冲突（与 Makefile auto-discover 同理）。
# plist 里的 __REPO__ / __PYTHON__ / __LOG_DIR__ 占位符在安装时按本机仓库路径渲染，
# 不再硬编死路径。

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"           # 仓库根（launchd 在 hipop/launchd/ 下）
LOG_DIR="$REPO/logs"
PYTHON_BIN="$(command -v python3 || echo /usr/bin/python3)"
LA="$HOME/Library/LaunchAgents"
mkdir -p "$LA"

# 自动发现：本目录所有 plist 的 Label（去掉路径与 .plist）。
PLISTS=()
for f in "$DIR"/*.plist; do
    [ -e "$f" ] || continue
    PLISTS+=("$(basename "$f" .plist)")
done

render() {  # 渲染占位符到 LaunchAgents（无占位符的老 plist 等价于直接 cp）。
    sed -e "s#__REPO__#$REPO#g" \
        -e "s#__PYTHON__#$PYTHON_BIN#g" \
        -e "s#__LOG_DIR__#$LOG_DIR#g" \
        "$DIR/$1.plist" > "$LA/$1.plist"
}

cmd="${1:-status}"

case "$cmd" in
    install)
        mkdir -p "$LOG_DIR"
        for p in "${PLISTS[@]}"; do
            render "$p"
            launchctl unload "$LA/$p.plist" 2>/dev/null || true
            launchctl load "$LA/$p.plist"
            echo "✓ $p 已加载"
        done
        echo ""
        echo "📅 周一 10:00 自动跑全量（wf1+wf2+wf3+wf6+wf5+周报）"
        echo "📦 每天 09:00 自动跑物流（wf3+wf6+日报）"
        echo "🔄 每 30 分钟拉飞书反馈"
        echo "🦅 紫鸟 web_driver 常驻守护 127.0.0.1:18080（开机自启 + keepalive）"
        echo "📂 日志: $LOG_DIR/"
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
