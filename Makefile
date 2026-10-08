.PHONY: install lint typecheck test check up down migrate
install:
	uv sync --locked
lint:
	uv run --locked ruff check .
	uv run --locked ruff format --check .
typecheck:
	uv run --locked pyright
test:
	uv run --locked pytest
check: lint typecheck test
up:
	docker compose up --build -d --wait
down:
	docker compose down
migrate:
	uv run --locked alembic upgrade head
