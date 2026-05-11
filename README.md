# Agentic Requirement Compiler (ARC)

![License](https://img.shields.io/badge/license-MIT-blue.svg)

> **Turn your requirement documents into running full-stack projects with full traceability.**

## Introduction

**Agentic Requirement Compiler (ARC)** is an open-source tool powered by Multi-Agent Systems (MAS). It parses structured requirement documents and compiles them into executable projects.

Unlike standard code generators, ARC focuses on **Traceability**. It maintains a rigorous chain of custody from the initial requirement node down to the specific lines of code and test cases.

## Table of Contents

- [Introduction](#introduction)
- [Key Features](#key-features)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
  - [CLI Mode (Recommended)](#cli-mode-recommended)
  - [WebSocket Mode](#websocket-mode)
- [Target Project Directory Format](#target-project-directory-format)
- [Requirements YAML Format](#requirements-yaml-format)
- [Contributing](#contributing)
- [License](#license)

## Key Features

- **Multi-Agent Pipeline**: InterfaceDesigner → TestGenerator → TestDrivenDeveloper, with local build verification at each stage
- **Traceability Database**: SQLite-backed tracking from requirement → interface → test → code
- **TDD Workflow**: Tests generated first, then code iteratively implemented until tests pass
- **DAG-Aware**: Non-leaf nodes design shared DB infrastructure; leaf nodes implement full UI/API/FUNC layers
- **Auto-Compact**: Context window managed automatically to prevent overflow during long agent runs
- **Debug Logging**: Full LLM responses and tool outputs logged to `.arc/debug.log` in debug mode
- **Android & Web**: Supports both Android (Java/Room/Gradle) and Web (React/Express/SQLite) project generation

## Prerequisites

- **Python** 3.11+
- **uv** (Python package manager, [install guide](https://docs.astral.sh/uv/getting-started/installation/))
- **LLM API Key** (OpenAI-compatible endpoint)

### Android Projects (additional)

- **JDK 21** (required by Gradle 8.4 + AGP 8.1.4)
- **Android SDK** with `platforms;android-34` and `build-tools;34.0.0`
  - Set `ANDROID_SDK_ROOT` environment variable, or install to a default location

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/your-username/Agentic-Requirement-Compiler.git
cd Agentic-Requirement-Compiler

# 2. Navigate to the agent source
cd src/arc-agent

# 3. Create virtual environment and install dependencies with uv
uv venv
uv pip install -e .
```

## Configuration

Create a `.env` file in `src/arc-agent/`:

```bash
# Required: LLM API credentials (OpenAI-compatible)
OPENAI_API_KEY=your-api-key-here
OPENAI_API_BASE_URL=https://api.openai.com/v1
MODEL=your-model-here

# Optional: Visual model for image analysis
VISUAL_OPENAI_API_KEY=your-api-key-here
VISUAL_OPENAI_API_BASE_URL=https://api.openai.com/v1
VISUAL_MODEL=your-vision-model-here

# Optional: Debug mode (1 = enabled, logs to .arc/debug.log)
ARC_DEBUG=1
```

### Android SDK Setup

If generating Android projects, ensure the SDK is accessible:

```bash
# Option A: Set environment variable
export ANDROID_SDK_ROOT=/path/to/android/sdk

# Option B: Install to a default location (auto-detected)
# Windows: C:\Users\<user>\AppData\Local\Android\Sdk
# Linux:   ~/Android/Sdk
# macOS:   ~/Library/Android/sdk
```

## Usage

### CLI Mode (Recommended)

Run ARC directly from the terminal without a WebSocket UI:

```bash
cd src/arc-agent

# Activate the virtual environment
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

# Basic usage: generate an Android project
python run_compilation_cli.py /path/to/target/project --app-type android

# Generate a web project
python run_compilation_cli.py /path/to/target/project --app-type web

# Clear existing workspace and recompile
python run_compilation_cli.py /path/to/target/project --app-type android --clear-all

# Specify a custom requirements file path
python run_compilation_cli.py /path/to/target/project --app-type android --requirement-path /path/to/requirements.yaml
```

**CLI flags:**

| Flag | Description |
|------|-------------|
| `project_path` | Target project root directory (positional, required) |
| `--requirement-path` | Path to requirements YAML (absolute, or relative to project) |
| `--clear-all` | Clear workspace and recompile from scratch |
| `--app-type` | `web` or `android` (default: `web`) |

**What happens during CLI execution:**

1. Prerequisites check (Java, Android SDK for android; Node.js for web)
2. Template files copied to target directory
3. `local.properties` written (Android SDK path, for android projects)
4. Requirements DAG parsed from YAML
5. For each node (top-down DFS order):
   - **Non-leaf nodes**: InterfaceDesigner designs shared DB layer only (Entity/DAO)
   - **Leaf nodes**: Full pipeline — InterfaceDesigner → TestGenerator → TDD loop
   - Local build verification after each agent stage
6. Debug log written to `<target>/.arc/debug.log` (if `ARC_DEBUG=1`)

## Target Project Directory Format

The target project directory must contain a `requirements/` folder with a `requirements.yaml` file:

```
my-project/
├── requirements/
│   └── requirements.yaml    # Required: ARC input file
├── .arc/                     # Auto-created by ARC
│   ├── database.db           # Traceability database
│   ├── metadata.md           # Tech stack metadata
│   └── debug.log             # Debug log (if ARC_DEBUG=1)
├── app/                      # Auto-created from template
│   ├── src/main/
│   ├── src/test/
│   └── build.gradle
└── ...
```

If the `requirements.yaml` is at a non-standard path, use `--requirement-path` to specify it explicitly.

## Requirements YAML Format

The input is a hierarchical DAG of requirements with scenarios and steps:

```yaml
id: ROOT
name: My Application
description: A brief description of the application.
dependencies: []
scenarios: []
children:
  - id: REQ-1
    name: Feature Group
    description: Description of this feature group.
    dependencies: []
    scenarios: []
    children:
      - id: REQ-1-1
        name: Specific Feature
        description: Detailed description of what this feature does.
        dependencies: []
        scenarios:
          - id: REQ-1-1:SCE-0
            name: Scenario Name
            prerequisites: []
            steps:
              - action: User clicks button "X"
                expectation: Result "Y" is displayed.
  - id: REQ-2
    name: Another Feature Group
    description: ...
    children:
      - id: REQ-2-1
        name: Another Feature
        description: ...
```

**Key fields:**

| Field | Description |
|-------|-------------|
| `id` | Unique node identifier (ROOT, REQ-1, REQ-1-1, etc.) |
| `name` | Short human-readable name |
| `description` | Detailed requirement text |
| `dependencies` | List of node IDs this node depends on |
| `scenarios` | List of test scenarios with steps |
| `children` | Sub-requirements (forms a DAG) |

**Node types:**
- **Non-leaf** (has `children`): ARC designs only shared DB infrastructure (Room Entity/DAO)
- **Leaf** (no `children`): ARC implements the full stack (UI → API → FUNC → DB)

## Contributing

1. Fork the Project
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the Branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## License

Distributed under the MIT License. See `LICENSE` for more information.
