PYTHON ?= python3
VENV ?= venv
PIP := $(VENV)/bin/pip
PLAYWRIGHT := $(VENV)/bin/playwright
APP := $(VENV)/bin/python

.PHONY: setup install-browser gui help lint test package

setup:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install -r requirements.txt
	@echo "轻量版配置完成。请确保系统已安装 Chrome 或 Edge。"

install-browser:
	@echo "当前轻量版 mac app 不再内置 Chromium。"
	@echo "请直接使用系统已安装的 Chrome 或 Edge。"

gui:
	$(APP) gui.py

help:
	$(APP) main.py --help

lint:
	$(APP) -m py_compile main.py gui.py config.py renderers.py input_normalizer.py scraper/*.py rag/*.py models.py storage.py

test:
	$(APP) -m pytest -q

package:
	bash scripts/build_app.sh
