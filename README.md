# Agentic Requirement Compiler (ARC)

<p align="center">
  ARC treats requirements as compilable artifacts rather than loose prompt context.
  It turns structured requirement trees into interfaces, tests, code, and an auditable execution trail.
</p>

<p align="center">
  <a href="#news">News</a> &middot;
  <a href="#what-arc-does">Pipeline</a> &middot;
  <a href="#getting-started">Getting Started</a> &middot;
  <a href="#visualization">ARC-Bench</a>
</p>

[![License: MIT](https://img.shields.io/badge/License-MIT-0f172a.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-2563eb.svg)](#getting-started)
[![CLI](https://img.shields.io/badge/Interface-CLI-16a34a.svg)](#cli-usage)
[![Status](https://img.shields.io/badge/Status-Active%20Build-9333ea.svg)](#news)

> Instead of asking an LLM to "write an app" from a long prompt, ARC compiles structured requirements through staged agents, test-first generation, and explicit traceability.

## News &#x2728;

- <font color="#93b071"><strong>2026-06-25 &middot; Accepted</strong></font> The paper <em>Compiling Large Multi-Modal Requirement Documents into Runnable Software Systems: From an Agentic Test-Driven Perspective</em> was accepted to ISSTA 2026.
- <font color="#93b071"><strong>2026-07-06 &middot; Released</strong></font> Open-sourced ARC CLI v1 and published the WeChat article: [Agentic Requirement Compilation: Turning Requirements into Source Code](https://mp.weixin.qq.com/s/AQSjEMdhEZZRetgQyVclGw)
- <font color="#598f91"><strong>In progress</strong></font> Integrating ARC into the visual web experience for a more interactive development workflow.
- <font color="#939ca3"><strong>Planned</strong></font> Extend ARC into a VS Code plugin so requirement compilation fits directly into day-to-day coding.

## Why ARC

Most AI coding workflows are still prompt-centric. A model reads a large requirement document, tries to infer structure implicitly, and produces code in one or a few broad passes.

ARC takes a compiler-oriented view instead:

- Requirements are not just context. They are the source program.
- Tests are not just verification. They are executable constraints.
- Traceability is not optional metadata. It is part of the system contract.

In practice, ARC models requirements as a structured graph, compiles them through multiple agent stages, and records how each requirement node maps to interfaces, tests, code, and commits.

## What ARC Does

ARC is designed as a requirement-to-system compiler with a staged pipeline:

| Stage | What ARC does |
| --- | --- |
| **Structured requirement modeling** | Consumes a hierarchical requirement tree with dependencies, scenarios, and optional multimodal references such as screenshots or design assets. |
| **Interface design** | Derives explicit interfaces and implementation boundaries before broad code generation begins. |
| **Test-first generation** | Produces unit, integration, and end-to-end tests from requirement scenarios before implementation. |
| **Traceability by default** | Records the requirement-to-interface-to-test-to-code chain instead of treating generation as a black box. |

## Getting Started

Use the following setup as a practical baseline. The installation example below uses `uv`.

### Requirements

- [Python 3.11+](https://www.python.org/downloads/)
- A virtual environment and package manager such as [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
- An OpenAI-compatible API endpoint and model

Additional requirements for web generation:
- [Node.js 20+](https://nodejs.org/en/download) with [`pnpm`](https://pnpm.io/installation)

Additional requirements for Android generation:
- [JDK 21](https://adoptium.net/temurin/releases/)
- [Android SDK / Android Studio](https://developer.android.com/studio) with `platforms;android-34` and `build-tools;34.0.0`

### Installation

The example below uses `uv`.

```bash
git clone https://github.com/your-org/agentic-requirement-compiler.git
cd agentic-requirement-compiler

uv venv

# Activate on Windows PowerShell:
.venv\Scripts\Activate.ps1

# Activate on Linux / macOS:
source .venv/bin/activate

# Install dependencies:
uv pip install -r src/requirements.txt
uv pip install -e .
```

Installing with `-e .` from the repository root exposes the `arc-agent` CLI entrypoint defined in the root `pyproject.toml`.

### Configuration

ARC reads configuration in this order:

1. Existing shell environment variables from the current terminal session
2. `ARC_ENV_FILE` if you explicitly point to an env file
3. `<repo_root>/.env`

Minimal `.env` example:

```dotenv
OPENAI_API_KEY=your-api-key
OPENAI_BASE_URL=https://api.openai.com/v1
MODEL=your-model
ARC_OPENAI_API_MODE=responses
```

Optional visual-model and debug configuration:

```dotenv
VISUAL_API_KEY=your-visual-api-key
VISUAL_BASE_URL=https://api.openai.com/v1
VISUAL_MODEL=your-visual-model
```

### Input: Requirement Model

ARC consumes a hierarchical requirement tree with `FOLDER` and `ATOMIC` nodes, dependency links, and executable scenarios. See [example/](example/) for complete examples.

At minimum, ARC expects:

- `requirements.yaml`
- optional assets such as `reference/...`

Conceptually, ARC produces three layers of output:

- **Runnable system**: the generated web or Android project
- **Execution memory**: queue state, debug logs, and intermediate compiler artifacts
- **Audit trail**: traceability records and git history that explain how requirements became code

This is one of the main differences between ARC and prompt-only code generation: the result is not just an output directory, but a recoverable compilation process.

### CLI Usage

ARC expects a requirement directory containing `requirements.yaml`.

Minimal input layout:

```text
my-requirement-dir
|-- requirements.yaml
`-- reference/
    `-- homepage.png
```

Run ARC from the repository root with Python:

```bash
python src/main.py /path/to/my-requirement-dir --app-type web
```

Or use the installed CLI:

```bash
arc-agent /path/to/my-requirement-dir --app-type web
```

#### CLI arguments

| Argument | Description |
| --- | --- |
| `requirement_path` | Requirement directory containing `requirements.yaml` |
| `--output-dir` | Output workspace directory. Defaults to `<repo_root>/workspace/run-<timestamp>` |
| `--clear-all` | Clears the output workspace and recopies the requirement input before recompiling |
| `--retry-failed` | Retry all failed nodes in the existing queue without clearing the workspace |
| `--retry-node` | Retry only the specified node IDs in the existing queue |
| `--app-type` | `web`, `android`, or `cli` |
| `--web-port` | Backend port for generated web applications |
| `--model-api-mode` | OpenAI-compatible API mode: `responses` or `chat_completions` |

#### Runtime behavior

- ARC copies the requirement directory into `<output-dir>/requirements/`
- Compilation executes inside `output-dir`
- If `--clear-all` is not used and `.arc/processing_queue.json` already exists, ARC resumes from that workspace

#### Partial failure recovery

ARC now supports retrying failed nodes in an existing workspace without wiping generated code.

- `--retry-failed` retries every node whose queue state is `FAILED`
- `--retry-node REQ-1 REQ-2` retries only the named nodes, including nodes that already passed
- `--clear-all` cannot be combined with retry flags

Retry semantics are phase-aware:

- If a node's `DESIGN` task failed, ARC treats it as a design failure, resets both queue tasks for that node to `PENDING`, clears that node's design/test traceability artifacts, and restarts the node from `UNSEEN`
- If a node's `IMPLEMENT` task failed while `DESIGN` completed, ARC treats it as an implement-only failure, keeps the design artifacts, resets only `IMPLEMENT` to `PENDING`, and restarts the node from `DESIGNED`
- If a selected node is already completed, ARC restarts that node from `DESIGN` but preserves existing interfaces, tests, node-session artifacts, and implementation files so the agents can revise incrementally in the same workspace

This distinction comes from the queue itself, not from a separate manual flag. The workflow checks the task statuses for the node and chooses the narrowest safe reset for that node.

#### Model API mode

ARC uses a model adapter for OpenAI-compatible providers. Select the request API explicitly when needed:

```bash
python src/main.py /path/to/my-requirement-dir --model-api-mode chat_completions
```

- `responses` uses the Responses API and is the default.
- `chat_completions` uses Chat Completions for providers that do not support Responses.
- `ARC_OPENAI_API_MODE` provides the same control from `.env`.
- The legacy `ARC_USE_RESPONSES_API=false` flag is still accepted and maps to `chat_completions`.

## Visualization

If you want a visual execution workflow, ARC can also be packaged and uploaded to **ARC-Bench**: [arc-bench.com](http://arc-bench.com). Follow the "Quick Start" instructions to upload a custom agent bundle.

For the current repository layout, the simplest upload path is:

1. Copy the contents of `src/` into your submission bundle root
2. Keep `main.py` at the bundle root
3. Zip the bundle
4. Upload it to ARC-Bench as a custom agent

A minimal bundle layout looks like this:

```text
submission
|-- main.py
|-- requirements.txt
`-- ...
```

ARC-Bench provides the container runtime, workspace lifecycle, event streaming, and visualization layer. ARC performs the actual requirement-to-project compilation inside that environment.

## Positioning

ARC is not trying to be a generic chat wrapper around an LLM.  
It is an attempt to make AI software generation more **structured**, **test-constrained**, and **inspectable**.

If you care about:

- turning requirement documents into working systems,
- making agent execution auditable,
- connecting tests directly to requirement intent,
- and keeping generated code understandable after the run,

then ARC is the right abstraction to explore.

## Research Context

ARC is also a research-driven system. It reflects a broader idea:

> software generation becomes more reliable when requirements are structured, tests are generated before implementation, and every transformation step remains inspectable.

That is the technical direction behind ARC's requirement graph modeling, test-first workflow, and traceability design. See  https://arxiv.org/abs/2602.13723

#### Citation

```bibtex
@article{kong2026arc,
  author    = {Weiyu Kong and Yun Lin and Xiwen Teoh and Duc-Minh Nguyen and Ruofei Ren and Jiaxin Chang and Haoxu Hu and Haoyu Chen},
  title     = {Compiling Large Multi-Modal Requirement Documents into Runnable Software Systems: From an Agentic Test-Driven Perspective},
  booktitle = {Proceedings of the ACM SIGSOFT International Symposium on Software Testing and Analysis},
  year      = {2026},
  series    = {ISSTA}
}
```

## Contributing

ARC is currently best understood as a set of ideas and a framework for requirement-driven software generation.
You are welcome to build on top of it by integrating your own tools, skills, workflows, or even foundation agents.

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for how to open an issue or submit a pull request.

If you want to join the community, you are also welcome to join the ARC WeChat group for updates, discussion, and collaboration. 🎉

<p align="center">
  <img src="assets/qr.jpg" alt="ARC WeChat Group QR Code" width="300" />
</p>

<p align="center">
  Welcome to contribute and build ARC together ✨🤝
</p>

## License

Distributed under the MIT License. See [LICENSE](LICENSE).
