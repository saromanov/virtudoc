.PHONY: dist publish

dist:
	python -m build

publish: dist
	twine check dist/*
	TWINE_USERNAME=__token__ TWINE_PASSWORD="$${PYPI_TOKEN:?PYPI_TOKEN is not set}" twine upload dist/*
