# 点购 hipop / 工作流 测试入口
# 用法：
#   make test           跑全部 smoke（自动聚合 tests/smoke_*.py，排除需 server 的 chat）
#   make test-chat      跑 chat 端到端 smoke（17 个 case，要 server 起在 :8765）
#   make test-all       test + test-chat
#   make test-one F=tests/smoke_judge.py   跑单个 smoke
#
# 关键：`make test` 自动扫 tests/smoke_*.py，新增 smoke 文件无需改本文件——
# 这避免了多分支都改 `test:` 同一行导致的连环 merge 冲突（WS-* 流水线踩过）。

PYTHON ?= /Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python
REPO   := $(shell pwd)

# 自动发现所有 smoke，排除需 uvicorn 的 gate（单独跑 make test-chat）
SMOKE_FILES := $(filter-out tests/smoke_chat.py tests/smoke_graded_threshold.py,$(sort $(wildcard tests/smoke_*.py)))

.PHONY: test test-chat test-one test-all

test:
	@set -e; for f in $(SMOKE_FILES); do \
	  echo ""; echo "▶ $$f"; \
	  PYTHONPATH=$(REPO) $(PYTHON) $$f; \
	done; \
	echo ""; echo "✓ 自动聚合跑过 $(words $(SMOKE_FILES)) 个 smoke 全绿（chat 需 server，另跑 make test-chat）"

test-one:
	@test -n "$(F)" || (echo "用法: make test-one F=tests/smoke_judge.py"; exit 2)
	PYTHONPATH=$(REPO) $(PYTHON) $(F)

test-chat:
	@echo "▶ chat smoke (17 cases, 需要 uvicorn 在 :8765 跑着)"
	cd $(REPO)/tests && bash run_smoke.sh

test-all: test test-chat
	@echo ""
	@echo "✓ all smoke passed"
