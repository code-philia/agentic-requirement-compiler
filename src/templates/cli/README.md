# CLI Template

This template is a small Python command-line application. It uses only the Python standard library and can be executed as a module with `python -m app`.

## Project Layout

- `app/main.py`: Argument parser, application logic, and CLI entrypoint.
- `app/__main__.py`: Module execution entrypoint for `python -m app`.
- `tests/unit`: Unit tests for pure functions.
- `tests/integration`: CLI subprocess integration tests.
- `tests/e2e`: User-visible roundtrip CLI tests.

## Prerequisites

- Python 3.11 or newer is recommended.
- No third-party Python dependencies are required by the template.

## Run

From the template root:

```bash
python -m app
```

Pass a name:

```bash
python -m app --name ARC
```

Render uppercase output:

```bash
python -m app --name arc --uppercase
```

## Tests

Run all tests:

```bash
python -m unittest discover -s tests
```

Run a single layer:

```bash
python -m unittest discover -s tests/unit
python -m unittest discover -s tests/integration
python -m unittest discover -s tests/e2e
```

## Extension Notes

- Keep command parsing in `build_parser()`.
- Keep pure reusable logic in functions such as `format_greeting()`.
- Keep `run(argv)` as the main program boundary so tests can call logic directly or execute the CLI as a subprocess.
- Preserve `python -m app` as the primary execution path.
