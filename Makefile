.PHONY: test lint scan

test:
	python -m pytest -q

lint:
	python -m ruff check hermes_agent_mcp_bridge tests

scan:
	python -m pytest -q tests/test_sanitization.py
