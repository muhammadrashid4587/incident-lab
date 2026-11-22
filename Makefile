.PHONY: test demo clean

test:
	python3 -m unittest discover -s tests -v

demo:
	python3 scripts/demo.py

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete
