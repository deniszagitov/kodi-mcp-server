.PHONY: test check setup
setup:
	@uv sync --locked
test:
	@PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_reliable*.py'
check:
	@python3 -B -c 'import ast,pathlib; [ast.parse(p.read_text()) for p in pathlib.Path("src").glob("*.py")]; print("✅ Синтаксис Python проверен")'
