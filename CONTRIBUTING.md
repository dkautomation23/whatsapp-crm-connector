# Contributing

## Setup

Use a dedicated virtual environment for this repository. Do not reuse a virtual environment from another project — a shared venv hides missing dependencies until they break somewhere else.

```
python -m venv .venv
.venv\Scripts\activate      # Windows
source .venv/bin/activate   # macOS / Linux
pip install -r requirements.txt
```

## Running the tests

CI runs these two commands on every push, against Python 3.10 and 3.12:

```
python -m compileall -q .
python -m pytest -q
```

Run both before opening a pull request.

## Making a change

Tests live in `tests/`. For a new check or a bug fix, add a failing test first, confirm it fails for the reason you expect, then write the fix and confirm the test passes. Do not submit a fix without a test that would have caught it.

## Commit style

Short, lowercase, usually `type: description` (`feat`, `fix`, `test`, `docs`, `chore`), sometimes a plain descriptive sentence. There is no strict convention enforced beyond that. Examples from this repository's history:

```
feat: 24-hour window enforcement and token lifecycle
test: 42 tests over signature, parsing, CRM, window, tokens and HTTP
docs: README with flow diagram, real demo output and go-live steps
```

## Pull requests

- Keep changes scoped to one thing.
- CI (byte-compile + pytest, on Python 3.10 and 3.12) must pass before review.
- Do not commit secrets, tokens, or credentials. Real values belong in a local `.env`, never in a commit — see `.env.example` for the variable names.
