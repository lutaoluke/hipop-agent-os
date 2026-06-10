#!/usr/bin/env bash
# WS-154 验收 #1/#3 — live 全量 chat e2e 阻断门的运行逻辑(Luke 方案 ①)。
#
# 由 .github/workflows/gate-chat.yml 在 self-hosted `hipop-live` runner 上调用。
# 安全形态:workflow 已用「非 fork 才跑」挡掉外部 PR,故只有自己人(可信)代码会到这台 runner;
# live LLM key / 真实业务库 / 店铺登录态全部从 runner 环境(HIPOP_ENV_FILE)取,绝不从仓库/PR 取。
#
# 职责:
#   1. preflight:缺 live 凭据(LLM key / DB / JWT)→ 直接红(exit 3),打印缺什么,拒绝空壳绿。
#   2. 起 server(若未起)→ 跑 tests/smoke_chat.py(~25 case,逐 case ✓/✗ 输出,验收 #3 可定位)。
#   3. 失败重试 1 次吸收 LLM 抖动;**两次都红才判失败(exit 1)**。
#      关键不变量:每次都红的确定性 case → 两次都红 → 门照样红(重试绝不洗白真 bug)。
#
# 本机自测(无需真 server/LLM,验 preflight 与 retry 行为):
#   缺凭据红:   HIPOP_ENV_FILE=/nonexistent bash tests/ci_chat_e2e_gate.sh ; echo $?   # → 3
#   真红不洗白: WS154_SELFTEST=1 WS154_SMOKE_CMD='false' HIPOP_ENV_FILE=<带3个key的文件> \
#                 bash tests/ci_chat_e2e_gate.sh ; echo $?                              # → 1(两次都跑 false)
#   一次过即过: WS154_SELFTEST=1 WS154_SMOKE_CMD='true'  HIPOP_ENV_FILE=<同上> \
#                 bash tests/ci_chat_e2e_gate.sh ; echo $?                              # → 0
set -uo pipefail

ENV_FILE="${HIPOP_ENV_FILE:-/Users/luke/code/hipop/.env.local}"
URL="${HIPOP_URL:-http://127.0.0.1:8765}"
PY="${PYTHON:-python3}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

# ── 1) preflight:缺 live 凭据直接红,拒绝空跑 ──────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
  echo "::error::[preflight] HIPOP_ENV_FILE ($ENV_FILE) 不存在 —— 没有 live 凭据,拒绝空壳绿。"
  exit 3
fi
set -a; . "$ENV_FILE"; set +a
missing=""
[ -n "${DEEPSEEK_API_KEY:-}${ANTHROPIC_API_KEY:-}${QWEN_API_KEY:-}${DASHSCOPE_API_KEY:-}" ] || missing="$missing LLM_API_KEY"
[ -n "${DB_URL:-}" ] || missing="$missing DB_URL"
[ -n "${JWT_SECRET:-}" ] || missing="$missing JWT_SECRET"
if [ -n "$missing" ]; then
  echo "::error::[preflight] 缺 live 凭据:$missing —— 门直接红,拒绝空壳绿(报告即事实)。"
  exit 3
fi
echo "[preflight] live 凭据就绪 (provider=${LLM_PROVIDER:-deepseek})"

# chat smoke 命令;仅本机自测时用 WS154_SMOKE_CMD 覆盖成 true/false 验 retry 行为。
SMOKE_CMD="${WS154_SMOKE_CMD:-$PY $REPO/tests/smoke_chat.py --url $URL}"

# ── 2) 起 server(自测模式跳过——假 smoke 不需要 server)────────────────────
STARTED_SERVER=""
if [ -z "${WS154_SELFTEST:-}" ]; then
  if ! curl -sS -m 3 "$URL/health" >/dev/null 2>&1; then
    echo "[server] $URL 未起,拉起 uvicorn ..."
    PYTHONPATH="$REPO" "$PY" -m uvicorn hipop.server.main:app --host 127.0.0.1 --port 8765 \
      > /tmp/ws154_gate_server.log 2>&1 &
    STARTED_SERVER=$!
    for i in $(seq 1 30); do
      curl -sS -m 3 "$URL/health" >/dev/null 2>&1 && break
      sleep 1
    done
    if ! curl -sS -m 3 "$URL/health" >/dev/null 2>&1; then
      echo "::error::[server] 30s 内没起来"; tail -40 /tmp/ws154_gate_server.log
      [ -n "$STARTED_SERVER" ] && kill "$STARTED_SERVER" 2>/dev/null
      exit 3
    fi
  fi
fi
cleanup() { [ -n "$STARTED_SERVER" ] && kill "$STARTED_SERVER" 2>/dev/null || true; }
trap cleanup EXIT

# ── 3) 跑 chat smoke,失败重试 1 次;两次都红才判失败 ────────────────────────
attempts=2
n=0
while [ "$n" -lt "$attempts" ]; do
  n=$((n+1))
  echo "──── chat e2e 第 $n/$attempts 次(逐 case ✓/✗ 见下)────"
  if $SMOKE_CMD; then
    echo "[gate] chat e2e 第 $n 次通过 ✓"
    # WS-163: chat smoke 过了 → 在同一台 live server 上再跑 graded 回归门(分数不只 pass/fail)。
    # 自测模式(WS154_SELFTEST)无真 server,跳过——self-test 只验 preflight/retry 行为。
    if [ -z "${WS154_SELFTEST:-}" ]; then
      echo "──── WS-163 graded 回归门(live, fail-closed: 缺 server 即红)────"
      if HIPOP_GRADED_REQUIRE_SERVER=1 HIPOP_URL="$URL" "$PY" "$REPO/tests/smoke_graded_threshold.py" --url "$URL"; then
        echo "[gate] WS-163 graded 回归门通过 ✓"
      else
        echo "::error::[gate] WS-163 graded 回归门红:live 分数回归到 baseline−tol 以下,阻断合并。"
        exit 1
      fi
    fi
    exit 0
  fi
  if [ "$n" -lt "$attempts" ]; then
    echo "::warning::[gate] 第 $n 次失败,重试一次吸收 LLM 抖动;确定性真红的 case 会两次都红、不会被洗白。"
  fi
done
echo "::error::[gate] chat e2e 连续 $attempts 次失败 = 真实回归,阻断合并(逐 case 详情见上方两次输出)。"
exit 1
