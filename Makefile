.PHONY: install-dev lint typecheck test migration-check check

install-dev:
	python -m pip install -r requirements-dev.txt

lint:
	ruff check .

typecheck:
	mypy

test:
	pytest app/tests -m "not postgres" -q

migration-check:
	alembic upgrade head
	alembic check

check: lint typecheck test
	python -m compileall -q app migrations scripts
