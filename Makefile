PYTHON ?= python3
VENV ?= venv
PIP := $(VENV)/bin/pip
PLAYWRIGHT := $(VENV)/bin/playwright
APP := $(VENV)/bin/python

.PHONY: setup install-browser gui help lint test

setup:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install -r requirements.txt
	$(PLAYWRIGHT) install chromium

install-browser:
	$(PLAYWRIGHT) install chromium

gui:
	$(APP) gui.py

help:
	$(APP) main.py --help

lint:
	$(APP) -m py_compile main.py gui.py config.py renderers.py input_normalizer.py scraper/*.py models.py storage.py

test:
	$(APP) -m pytest -q
