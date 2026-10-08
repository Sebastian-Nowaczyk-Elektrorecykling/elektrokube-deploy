.PHONY: test unit lint
unit:
	python -m unittest discover -s tests -p 'test_runner.py' -v
lint:
	helm lint charts/branch-preview --strict --set repository.url=https://github.com/example/my-app
test: lint
	python -m unittest discover -s tests -v
