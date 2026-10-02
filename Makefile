.PHONY: bootstrap install-dev lint typecheck test sdk-test harness-test migration-check lock lock-check check

bootstrap:
	python scripts/bootstrap_env.py

install-dev:
	python -m pip install -r requirements-dev.txt

lint:
	ruff check .
	ruff format --check .

typecheck:
	mypy

test:
	pytest app/tests -m "not postgres" -q

sdk-test:
	PYTHONPATH=sdk/python/src pytest sdk/python/tests -q

harness-test:
	pytest tests/egress/test_harness.py -q

migration-check:
	alembic upgrade head
	alembic check

lock:
	uv pip compile requirements.txt --python-version 3.11 --python-platform x86_64-unknown-linux-gnu --generate-hashes --custom-compile-command "make lock" $(LOCK_ARGS) --output-file requirements-deploy.txt

lock-check:
	python scripts/check_deployment_lock.py

check: lint typecheck test sdk-test harness-test lock-check
	python -m compileall -q app migrations scripts sdk/python/src examples tests/egress
