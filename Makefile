.PHONY: install-dev lint typecheck test sdk-test migration-check check

install-dev:
	python -m pip install -r requirements-dev.txt

lint:
	ruff check .

typecheck:
	mypy

test:
	pytest app/tests -m "not postgres" -q

sdk-test:
	PYTHONPATH=sdk/python/src pytest sdk/python/tests -q

migration-check:
	alembic upgrade head
	alembic check

check: lint typecheck test sdk-test
	python -m compileall -q app migrations scripts sdk/python/src examples
