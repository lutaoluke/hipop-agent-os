# 点购 hipop / 工作流 测试入口
# 用法：
#   make test           跑 governance smoke（启动 hook 也跑这个）
#   make test-chat      跑 chat 端到端 smoke（17 个 case，要 server 起着）
#   make test-all       全部 smoke

PYTHON ?= /Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python
REPO   := $(shell pwd)

.PHONY: test test-chat test-governance test-judge test-sales-contract test-replenish-inputs test-all

test: test-governance test-judge test-sales-contract test-replenish-inputs
	@echo ""
	@echo "✓ governance + judge + sales-contract + replenish-inputs smoke passed"
	@echo "  (跑全套: make test-all；跑 chat smoke: make test-chat)"

test-governance:
	@echo "▶ governance smoke (provider 委托 + plan pipeline)"
	cd $(REPO) && PYTHONPATH=$(REPO) $(PYTHON) tests/smoke_governance.py

test-judge:
	@echo "▶ judge/confidence smoke (防 confidence=0.9 硬编码回退)"
	cd $(REPO) && PYTHONPATH=$(REPO) $(PYTHON) tests/smoke_judge.py

test-sales-contract:
	@echo "▶ sales-contract smoke (WS-15: 销量录入数据契约 fail-then-pass，SQLite 自洽)"
	cd $(REPO) && PYTHONPATH=$(REPO) $(PYTHON) tests/smoke_sales_contract.py

test-replenish-inputs:
	@echo "▶ replenish static-input smoke (三类输入契约 + 缺失检测点)"
	cd $(REPO) && PYTHONPATH=$(REPO) $(PYTHON) tests/smoke_replenish_inputs.py

test-chat:
	@echo "▶ chat smoke (17 cases, 需要 uvicorn 在 :8765 跑着)"
	cd $(REPO)/tests && bash run_smoke.sh

test-all: test-governance test-judge test-sales-contract test-replenish-inputs test-chat
	@echo ""
	@echo "✓ all smoke passed"
