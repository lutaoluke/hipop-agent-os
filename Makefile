# 点购 hipop / 工作流 测试入口
# 用法：
#   make test           跑 governance smoke（启动 hook 也跑这个）
#   make test-chat      跑 chat 端到端 smoke（17 个 case，要 server 起着）
#   make test-all       全部 smoke

PYTHON ?= /Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python
REPO   := $(shell pwd)

.PHONY: test test-chat test-governance test-judge test-sales-contract test-erp-orders test-wf1-ingest test-wf1-history test-all

test: test-governance test-judge test-sales-contract test-erp-orders test-wf1-ingest test-wf1-history
	@echo ""
	@echo "✓ governance + judge + sales-contract + erp-orders + wf1-ingest + wf1-history smoke passed"
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

test-erp-orders:
	@echo "▶ erp-orders smoke (WS-17: ERP 商品总表 + 订单成本利润接入 fail-then-pass，SQLite 自洽)"
	cd $(REPO) && PYTHONPATH=$(REPO) $(PYTHON) tests/smoke_erp_orders_contract.py

test-wf1-ingest:
	@echo "▶ wf1 ingest smoke (Noon inventory + ASN/送仓 → v2 wf1_stock/staging, WS-10)"
	cd $(REPO) && PYTHONPATH=$(REPO) $(PYTHON) tests/smoke_wf1_ingest_v2.py

test-wf1-history:
	@echo "▶ wf1 history smoke (WS-22: as_of_date dated 快照层 + 历史抽检 fail-then-pass)"
	cd $(REPO) && PYTHONPATH=$(REPO) $(PYTHON) tests/smoke_wf1_stock_history_v2.py

test-chat:
	@echo "▶ chat smoke (17 cases, 需要 uvicorn 在 :8765 跑着)"
	cd $(REPO)/tests && bash run_smoke.sh

test-all: test-governance test-judge test-sales-contract test-erp-orders test-wf1-ingest test-wf1-history test-chat
	@echo ""
	@echo "✓ all smoke passed"
