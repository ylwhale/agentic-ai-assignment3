# RepoMind. Run `make setup` once, then `make check-03` whenever you like.

PY ?= .venv/bin/python

.PHONY: setup test ping audit reset check-03 bounds-smoke

setup:
	python3 -m venv .venv
	.venv/bin/pip install -q -r requirements.txt
	@echo "ready. Next: copy your Assignment 2 model into settings.yaml and its price line into pricing.yaml, set your key, then make ping."

test:            ## the harness's own tests; they spend no tokens
	$(PY) -m unittest discover -s tests -t .

ping:            ## your key, your model, tool calling, its price line, and your five definitions in one request
	$(PY) -m harness.ping

audit:           ## Part A's schema audit of tools/*.json; no model
	$(PY) tools/audit_schemas.py

reset:           ## before Part D: archive the tracker's database, and set earlier evaluation runs aside
	$(PY) -m harness.tracker reset

bounds-smoke:    ## the agent way against the model that never stops; free
	$(PY) run.py --way agent --backend never-stops --set dev --runs 1

check-03:        ## the Assignment 3 checker
	$(PY) checks/check_03.py
