# Anki ImageSearchAddon

Made with [Python 3.13.15](https://www.python.org/downloads/release/python-31315/)

## Getting Started

### 1. Set up the environment

```sh
py -3.13.15 -m venv .venv
```

Then activate it:

- **Windows:** `.venv\Scripts\activate`
- **Unix/macOS:** `source .venv/bin/activate`

### 2. Install vendor dependencies (required for Anki)

```sh
pip install -r vendor-requirements.txt --target=vendor
```

### 3. Run

| Mode | Command |
|------|---------|
| In Anki (copy files) | `.\copy-to-anki.bat` |
| In Anki (launch) | `.\run-anki.bat` |