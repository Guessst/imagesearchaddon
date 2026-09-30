# Anki ImageSearchAddon

Made with [Python 3.13.15](https://www.python.org/downloads/release/python-31315/)

1. Make your `.venv`
2. Activate it
3. 1. Run standalone
3. 1. 1. Use `py __init__.py`
3. 2. Run in Anki
3. 2. 1.  Use `.\copy-to-anki.bat`
3. 2. 2. Use `.\run-anki.bat`

## make .venv
Use `py -3.13.15 -m venv .venv`

## make vendor (required for running in Anki)
Use `pip install -r vendor-requirements.txt --target=vendor`