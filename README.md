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

## Replication Benchmark (ARC-Bench)

The performance of ARC can be validated on [ARC-Bench](https://github.com/code-philia/arc-bench), which consists of a number of applications, their requirements, corresponding validation tests, and reference behavior (via web domain address).


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
| **Global database preparation** | Analyzes the complete requirement tree into validated JSON table and bootstrap records; deterministically generates database code and initializes SQLite before any node design or TDD. |
| **Node design** | Improves existing frontend pages/components and wires real client requests, then materializes node-owned backend call skeletons with API/FUNC/DB operation contracts. UI is not modeled as interface nodes. |
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

```bash
# Clone the repository and its template submodule
git clone --recurse-submodules https://github.com/code-philia/agentic-requirement-compiler.git
cd agentic-requirement-compiler

# Initialize and update submodules if you didn't use --recurse-submodules
git submodule update --init --recursive

# Create and activate virtual environment
uv venv
source .venv/bin/activate  # On Linux/macOS
# .venv\Scripts\Activate.ps1  # On Windows PowerShell

# Install ARC
uv pip install -e .
```

After installation, the `arc` command will be available in your virtual environment.

**Verify installation:**
```bash
arc --version
arc --help
```

### Configuration

**Quick setup (recommended):**

```bash
arc config
```

This will interactively prompt you for required configuration values and create/update `.env`.

**Manual setup:**

Copy `.env_example` to `.env` and fill in your API credentials:

```bash
cp .env_example .env
```

Edit `.env` with your configuration:

```bash
# Required
OPENAI_API_KEY=sk-your-api-key-here
OPENAI_BASE_URL=https://api.openai.com/v1
MODEL=gpt-5.5
ARC_OPENAI_API_MODE=chat_completions

# Optional: Visual analysis
VISUAL_API_KEY=
VISUAL_BASE_URL=
VISUAL_MODEL=

# Optional: Debug mode
ARC_DEBUG=0
```

**Validate configuration:**
```bash
arc doctor
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

Database preparation is automatic and requires no additional CLI flags. All compilation model calls return only a JSON array of tool calls, with a tool name and necessary parameters on each item; no wrapper, summary, explanation or status fields. DESIGN exposes only read_file/add_file/edit_file/delete_file; new files carry their layer and are tracked automatically. Test generation uses register_test. Database analysis uses define_entity, define_table and seed_rows to identify shared entities and analyze requirement subtrees incrementally. The compiler translates these calls into validated internal records and merges only changed definitions and new bootstrap rows. Conflicting drafts require explicit replacement calls, while applied database records remain additive-only. Forward foreign keys are checked during final reconciliation. The analyzer has no shell or filesystem tools and does not generate SQL, application code or schema documentation.

The compiler saves the entity catalog, accepted batch deltas, cumulative draft, and final reconciliation in `.arc/database/analysis.json`, keyed by the requirement revision and database baseline. `--resume` skips accepted batches; a changed revision or baseline restarts analysis. Only after all batches and final validation pass does the existing deterministic generator write runtime files and initialize the actual database. The final plan and materialization status remain in `.arc/database/state.json`. A failed database stage prevents node execution.

For web applications, generated `backend/src/database/arc_database.js` is connected to the existing `init_db.js` scaffold. Runtime startup and isolated E2E databases use the same schema and default rows. CLI applications use generated `app/arc_database.py`, bootstrapped by `app/__main__.py`. Android applications receive a generated `database/ArcDatabase.java` helper and `assets/arc_database.json`, bootstrapped by the manifest-declared Java Application; the compiler prepares a host SQLite database before node compilation, while the device database is initialized on open. Existing web/CLI/Android bootstrap files are extended at recognized anchors rather than replaced; an unsupported existing bootstrap fails with a specific message.

Node agents receive the relevant database records and reuse stable `GLOBAL:DB:<table>` interfaces. Schema and bootstrap code are compiler-owned. `--sync-requirements` reanalyzes the complete tree and supports additive tables, safe columns, indexes, and seed rows while preserving existing data. Destructive or incompatible schema changes are rejected. User inputs and records created by registration, login, or ordering are not default seed data.

Node DESIGN directly extends existing frontend pages/components, implements the request client and event/state wiring, and mounts any new UI in the real application. It returns only `files` grouped as `frontend`, `API`, `FUNC`, `DB`, and `shared`, without a final summary. Parent nodes with visual references return frontend/shared paths and empty backend groups. Node sessions preserve these locations as `file_groups` and `materialized_files` for test generation, TDD, and related nodes.

The compiler derives stable backend file mapping IDs from requirement, layer, and canonical path, validates file existence/ownership, and stores lightweight rows in the existing interfaces table. Frontend and shared registration files are touched paths, not exclusive backend ownership. Function signatures, endpoint shapes, and comments live only in source; tests and TDD read those files. No model-generated interface descriptions or caller/callee graph are required. Test manifest items contain only `test_id`, `type`, and `file_path`; requirement ownership is system-assigned. Global database records, node states, tests, and Git checkpoints remain traceable. TDD completes the registered backend call skeletons and repairs frontend integration.

TDD receives an `implementation_scope` resolved from DESIGN's file groups. Backend edits are restricted to the node's exact API/FUNC/DB files and explicitly listed shared integration files; current test files can be repaired. Backend-wide discovery is blocked, while exact dependency reads remain available. Frontend discovery and edits are allowed within the frontend source area, starting from tracked pages/components/clients. Legacy workspaces recover backend targets from their traceability records; a missing tracked skeleton requires a DESIGN retry rather than a codebase search.

### CLI Usage

ARC expects a requirement directory containing `requirements.yaml`.

Minimal input layout:

```text
my-requirement-dir
|-- requirements.yaml
`-- reference/
    `-- homepage.png
```

**Basic usage:**

```bash
arc compile /path/to/my-requirement-dir -o workspace/output
```

**With options:**

```bash
arc compile example/ticketbooking-demo -o workspace/demo \
  --type web \
  --port 3301 \
  --clean
```

#### Available Commands

- **`arc compile`** - Compile requirements into a working application
- **`arc config`** - Configure ARC interactively (create/update .env)
- **`arc doctor`** - Check configuration and environment health

Run `arc --help` or `arc compile --help` for detailed usage.

#### Main Arguments

| Argument | Description |
| --- | --- |
| `requirement_path` | Requirement directory containing `requirements.yaml` |
| `-o, --output-dir` | Output workspace directory (required) |
| `-t, --type` | Application type: `web`, `android`, or `cli` (default: `web`) |
| `--port` | Backend port for web applications (default: 3301) |
| `--clean` | Remove existing output directory before compilation |
| `--resume` | Resume from saved compilation queue |
| `--retry-failed` | Retry all failed nodes (requires `--resume`) |
| `--retry NODE_ID...` | Retry specific node IDs (requires `--resume`) |

#### Runtime behavior

- ARC copies the requirement directory into `<output-dir>/requirements/` (you must specify `-o` explicitly)
- Compilation executes inside `output-dir`
- If `--clear-all` is not used and `.arc/processing_queue.json` already exists, ARC resumes from that workspace

#### Partial failure recovery

ARC now supports retrying failed nodes in an existing workspace without wiping generated code.

- `--retry-failed` retries every node whose queue state is `FAILED`
- `--retry REQ-1 REQ-2` retries only the named nodes, including nodes that already passed
- `--clear-all` cannot be combined with retry flags

Retry semantics are phase-aware:

- If a node's `DESIGN` task failed, ARC treats it as a design failure, resets both queue tasks for that node to `PENDING`, clears that node's design/test traceability artifacts, and restarts the node from `UNSEEN`
- If a node's `IMPLEMENT` task failed while `DESIGN` completed, ARC treats it as an implement-only failure, keeps the design artifacts, resets only `IMPLEMENT` to `PENDING`, and restarts the node from `DESIGNED`
- If a selected node is already completed, ARC restarts that node from `DESIGN` but preserves existing interfaces, tests, node-session artifacts, and implementation files so the agents can revise incrementally in the same workspace

This distinction comes from the queue itself, not from a separate manual flag. The workflow checks the task statuses for the node and chooses the narrowest safe reset for that node.

#### Model API mode

ARC supports two OpenAI-compatible API modes, configured via `ARC_OPENAI_API_MODE` in `.env`:

- `chat_completions` (default) - Uses `/v1/chat/completions` endpoint, most compatible
- `responses` - Uses `/v1/responses` endpoint for models that support it

Set this in your `.env` file (see Configuration section above).


## Visualization

If you want a visual execution workflow with progress tracking and result visualization, use **ARC-Bench**: [arc-bench.com](http://arc-bench.com).

### Use Built-in ARC Agent (Recommended)

When submitting a task on ARC-Bench, select **"ARC"** from the built-in agents dropdown. This uses the official ARC implementation maintained by the ARC-Bench team.


### Custom ARC Bundle (For Modified Versions)

If you've modified ARC, package and upload your custom version:

1. Copy the contents of `src/` into your submission bundle root
2. Keep `main.py` at the bundle root
3. Zip the bundle
4. Upload to ARC-Bench as a custom agent

A minimal bundle layout:

```text
submission/
|-- main.py
|-- requirements.txt
|-- agents/
|-- context/
|-- core/
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

The data reported in the paper comes from an earlier implementation whose workflow was built on Trae Agent. Due to difficulties with its installation and use, this repository has since switched to a minimal agent developed.

## Contact

For questions about ARC or the accompanying research, please contact Yun Lin at <lin_yun@sjtu.edu.cn> or Weiyu Kong at <kwy160034@sjtu.edu.cn>.

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
