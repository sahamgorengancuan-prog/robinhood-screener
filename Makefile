.PHONY: help install init probe ui run once demo test lint kill unkill clean package

help:
	@echo "install  - install dependencies"
	@echo "init     - create .env from template and build the database"
	@echo "ui       - start the Gradio control panel (http://127.0.0.1:7860)  <-- start here"
	@echo "probe    - same connection tests, in the terminal"
	@echo "once     - run a single screening cycle and print the result"
	@echo "run      - start the API + scheduler (http://localhost:8000)"
	@echo "demo     - render sample alerts offline (no network, no keys)"
	@echo "test     - run the test suite"
	@echo "package  - rebuild dist/robinhood-screener-linux-<version>.zip"
	@echo "kill     - engage the kill switch immediately"
	@echo "unkill   - clear the kill-switch file"

install:
	pip install -r requirements.txt

init:
	@test -f .env || (cp .env.example .env && echo "created .env — edit it before running")
	@mkdir -p data
	python scripts/init_db.py

ui:
	python scripts/run_ui.py

probe:
	python scripts/probe_endpoints.py $(TOKEN)

once:
	python scripts/run_once.py

run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000

demo:
	python scripts/demo_alert.py

test:
	python -m pytest -q

package:
	python scripts/make_linux_package.py

kill:
	@touch KILL_SWITCH && echo "KILL SWITCH ENGAGED — all execution stopped"

unkill:
	@rm -f KILL_SWITCH && echo "kill-switch file removed (env var and DB flag are unaffected)"

clean:
	rm -rf __pycache__ .pytest_cache
	find . -name "*.pyc" -delete
