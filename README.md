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
- [Target Project Directory Format](#target-project-directory-format)
- [Requirements YAML Format](#requirements-yaml-format)
- [Contributing](#contributing)
- [License](#license)

## Key Features

- **Multi-Agent Pipeline**: InterfaceDesigner -> TestGenerator -> TestDrivenDeveloper, with local build verification at each stage
- **Traceability Database**: SQLite-backed tracking from requirement -> interface -> test -> code
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

# 3. Create virtual environment and install dependencies
uv venv
uv pip install -r requirements.txt
```

## Configuration

ARC reads configuration in this order:

1. `src/arc-agent/.env` if the file exists
2. existing system / shell environment variables if `.env` is missing or a variable is not defined there

Create a `.env` file in `src/arc-agent/` if you want file-based configuration:

```bash
# Required: LLM API credentials (OpenAI-compatible)
OPENAI_API_KEY=your-api-key-here
OPENAI_BASE_URL=https://api.openai.com/v1
MODEL=your-model-here

# Optional: Visual model for image analysis (If not set, defaults to the same config as above)
VISUAL_API_KEY=your-api-key-here
VISUAL_BASE_URL=https://api.openai.com/v1
VISUAL_MODEL=your-vision-model-here

# Optional: Debug mode (1 = enabled, logs to .arc/debug.log)
ARC_DEBUG=1
```

If you do not want a `.env` file, you can export the variables in the shell before running ARC.

PowerShell:

```powershell
$env:OPENAI_API_KEY="your-api-key-here"
$env:OPENAI_BASE_URL="https://api.openai.com/v1"
$env:MODEL="your-model-here"
```

Bash:

```bash
export OPENAI_API_KEY="your-api-key-here"
export OPENAI_BASE_URL="https://api.openai.com/v1"
export MODEL="your-model-here"
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

Run ARC from `src/arc-agent/main.py`:

```bash
cd src/arc-agent

# Activate the virtual environment
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

# Basic usage: compile a web project
python main.py /path/to/target/project --app-type web

# Compile a web project on a specific single backend port
python main.py /path/to/target/project --app-type web --web-port 3301

# Compile an Android project
python main.py /path/to/target/project --app-type android

# Clear existing workspace and recompile
python main.py /path/to/target/project --app-type web --clear-all

```

**CLI flags:**

| Flag | Description |
|------|-------------|
| `project_path` | Target project root directory (positional, required) |
| `--requirement-path` | Path to requirements YAML. Can be absolute, or relative to `project_path` |
| `--clear-all` | Clear project workspace and recompile |
| `--app-type` | `web` or `android` (default: `web`) |
| `--web-port` | Web only. Single backend port used to start the website; the backend serves the built frontend on this same port (default: `3301`) |

## Target Project Directory Format

The target project directory is an existing directory passed as `project_path`.

At minimum, ARC expects a requirements file in one of these locations:

- `<project_path>/requirements/requirements.yaml`
- `<project_path>/requirements/requirents.yaml`
- or a custom path supplied via `--requirement-path`

Minimal input layout:

```text
my-project/
|-- requirements/
|   `-- requirements.yaml
```

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


## Contributing

1. Fork the Project
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the Branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## License

Distributed under the MIT License. See `LICENSE` for more information.
