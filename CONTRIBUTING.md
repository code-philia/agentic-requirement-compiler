# Contributing to Agentic Requirement Compiler (ARC)

First off, thank you for considering contributing to ARC! It's people like you that make the open-source community such an amazing place to learn, inspire, and create.

## 1. Prerequisites

Before you start, please ensure you have the following set up:

* **Python**: Version `[TODO: Insert version, e.g., 3.10+]`.
* **Package Manager**: We use `[TODO: pip / poetry / uv]` for dependency management.
* **API Keys**: You will need access to LLM providers (e.g., OpenAI, Anthropic) if you intend to run integration tests.
    * *Note: For basic logic changes, we provide mocked tests that do not require API keys.*

## 2. Development Setup

### Installation

1.  **Fork and Clone** the repository.
2.  **Create a Virtual Environment**:
    ```bash
    # [TODO: Insert command to create venv]
    python -m venv venv
    source venv/bin/activate
    ```
3.  **Install Dependencies**:
    ```bash
    # [TODO: Insert command to install dev dependencies]
    pip install -r requirements-dev.txt
    ```

### Environment Configuration

1.  Copy the example environment file: `cp .env.example .env`
2.  **Important**: Do not commit your `.env` file to version control.
3.  `[TODO: Explain which specific environment variables are mandatory for local dev]`

## 3. Project Structure Guide

To help you navigate the codebase:

* `src/agents/`: **[TODO: Add description]** Contains individual agent definitions (UI Agent, DB Agent).
* `src/compiler/`: **[TODO: Add description]** Core logic for parsing requirements and managing the traceability graph.
* `tests/unit/`: Tests that mock LLM calls (Safe to run often).
* `tests/integration/`: Tests that make real API calls (Costs money, run sparingly).

## 4. Testing Policy

We use `[TODO: pytest / unittest]` for testing.

### Unit Tests (Mocked)
All PRs must pass unit tests. These simulate LLM responses and check the logic of the compiler and traceability mapping.
```bash
# [TODO: Command to run unit tests]
pytest tests/unit

```

### Integration Tests (Live)

**Warning**: These tests consume API tokens.
Run these only if you are modifying the Prompt Engineering or the interaction logic between agents.

```bash
# [TODO: Command to run integration tests]
pytest tests/integration

```

## 5. Submission Guidelines

### Commit Convention

We follow the **Conventional Commits** specification. This allows us to automatically generate changelogs.

* `feat`: A new feature (e.g., adding a new Agent).
* `fix`: A bug fix (e.g., fixing a broken traceability link).
* `docs`: Documentation only changes.
* `style`: Formatting, missing semi-colons, etc; no code change.

### Pull Requests

1. **Search**: Check if a similar PR already exists.
2. **Branch**: Create a new branch: `git checkout -b feat/my-new-feature`.
3. **Traceability Check**: If you modify the compiler, please include a screenshot or log showing that the *Traceability Chain* (Req -> Code) is preserved. `[TODO: explain how to verify this]`.
4. **Linting**: Ensure your code is formatted correctly using `[TODO: Black / Ruff / Flake8]`.

## 6. Code of Conduct

Please note that this project is released with a [Code of Conduct](https://www.google.com/search?q=CODE_OF_CONDUCT.md). By participating in this project you agree to abide by its terms.

## 7. Questions?

Feel free to open a specific issue tagged `question` or contact `[TODO: Your Email or Discord Link]`.
