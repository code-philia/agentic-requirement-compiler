# Contributing to Agentic Requirement Compiler (ARC)

Thanks for contributing to ARC.

ARC is currently best understood as a set of ideas and a framework for requirement-driven software generation. We welcome contributions that extend or sharpen that framework, including:

- new tools or tool integrations
- new skills or prompting workflows
- alternative execution pipelines
- support for different foundation agents or model backends
- documentation, examples, and research-oriented improvements

## Before You Start

Please first check whether a similar issue or pull request already exists.

Useful repository areas:

- `src/arc-agent/`: current CLI-oriented ARC implementation
- `src/arc-extension/`: VS Code extension and interactive UI work
- `README.md`: project overview and research context

## Submit an Issue

Open an issue when you want to:

- report a bug
- propose a feature
- discuss a design direction
- ask a focused implementation question

When opening an issue, include:

- what you expected
- what happened instead
- steps to reproduce, if this is a bug
- relevant logs, screenshots, or requirement examples
- your suggested direction, if you already have one

If your change is large, architectural, or research-facing, opening an issue before writing code is strongly preferred.

## Submit a Pull Request

1. Fork the repository.
2. Create a feature branch from your fork.
3. Make the smallest coherent change that solves one problem clearly.
4. Run the relevant checks for the part you changed.
5. Update documentation when behavior, workflow, or contribution surface changes.
6. Open a pull request with a clear description of the change and why it is needed.

Please include in the PR:

- the problem being solved
- the scope of the change
- how you validated it
- screenshots or logs when the change affects the UI, workflow, or traceability behavior
- links to related issues, if any

## Local Notes

For the Python CLI:

```bash
cd src/arc-agent
uv venv
uv pip install -r requirements.txt
uv pip install -e .
```

For the VS Code extension:

```bash
cd src/arc-extension
npm install
npm run compile
npm run lint
```

Run the checks that match your change. If you did not run something important, state that clearly in the PR.

## Contribution Style

- Prefer focused PRs over broad mixed changes.
- Preserve traceability-oriented behavior where possible.
- Keep docs aligned with code changes.
- Explain non-obvious design decisions directly in the PR description.

## Questions

If you are unsure whether an idea fits ARC, open an issue first. That is the fastest way to align on direction before implementation.
