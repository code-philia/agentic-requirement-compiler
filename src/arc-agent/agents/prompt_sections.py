from __future__ import annotations


def _section(title: str, bullets: list[str]) -> str:
    lines = [f"### {title}"]
    lines.extend(f"- {bullet}" for bullet in bullets)
    return "\n".join(lines)


def get_common_session_guidance() -> str:
    return _section(
        "Session Guidance",
        [
            "Start from `<current_requirement>` and the prefetched node context. Treat explicit requirement text, scenarios, visual analysis, and frozen contracts as stronger evidence than generic repo priors.",
            "Before acting, form a compact working map: target behavior, likely owner files, reuse candidates, and hard constraints.",
            "Read code before proposing changes. Prefer files already named in context, entrypoints, route containers, top-level pages, providers, and nearby tests over broad repository scans.",
            "Prefer the smallest set of files that can prove or disprove the current hypothesis.",
            "When several searches or reads are independent, issue them in the same turn. When a later call depends on earlier evidence, keep them sequential.",
            "If an approach fails, identify the exact failed assumption before changing tactics. Gather only the next evidence needed to confirm or replace it.",
            "Reuse existing code and interfaces unless the requirement clearly forces a new boundary.",
        ],
    )


def get_interface_designer_guidance() -> str:
    return "\n\n".join(
        [
            _section(
                "Fast Codebase Understanding",
                [
                    "Start from the current requirement, visual/scenario evidence, and `<node_understanding>`.",
                    "Identify existing route, page, layout, provider, API, and domain owners before inventing new interfaces.",
                    "Use relation and interface search tools first to discover reusable boundaries before opening source files.",
                    "If ownership is unclear, inspect app entrypoints, route files, and top-level containers before reading leaf implementation details.",
                ],
            ),
            _section(
                "Fast Solution Design",
                [
                    "For leaf work, design only the smallest executable chain needed across UI -> API -> FUNC -> DB.",
                    "For non-leaf work, stay at shell boundaries: routes, layouts, providers, page containers, and mount points.",
                    "Prefer extending an existing interface over creating a parallel one that competes for the same responsibility.",
                    "Avoid speculative interfaces, future-proof abstractions, and contracts that are not required by the current node.",
                ],
            ),
            _section(
                "Using Your Tools",
                [
                    "Use `search_interfaces_by_keyword`, `search_interfaces_by_relation`, and `get_node_relations` to find reusable contracts and ownership.",
                    "Use `grep`, `glob`, and `list_directory` to locate likely owner files when context is insufficient.",
                    "Use `read_file` only on files that can confirm ownership, route wiring, or an interface contract.",
                    "Do not write code during design, understanding, or spec-generation steps.",
                ],
            ),
        ]
    )


def get_test_generator_guidance() -> str:
    return "\n\n".join(
        [
            _section(
                "Fast Codebase Understanding",
                [
                    "Start from `<interface_spec>` and `<current_requirement>`. Treat them as the contract to test, not as optional hints.",
                    "Inspect existing test patterns near the owner files before inventing new test structure, fixtures, or selector strategy.",
                    "Prefer one primary file per layer or one file per coherent scenario group.",
                ],
            ),
            _section(
                "Fast Solution Design",
                [
                    "Decide the coverage matrix first, then write the compact test files that implement it.",
                    "Prefer stable contract assertions over incidental DOM structure, transient styling, or private implementation details.",
                    "Reuse existing setup files, fixtures, helpers, mocks, and assertion idioms whenever possible.",
                ],
            ),
            _section(
                "Error Localization",
                [
                    "If `run_build` fails, treat it first as a syntax, path, import, framework, or configuration problem.",
                    "Read the failing test file and the nearest setup or config file before changing assertions or business expectations.",
                    "When a selector is unstable, prefer requirement-visible text first, then stable local selectors, then role-based queries.",
                ],
            ),
            _section(
                "Using Your Tools",
                [
                    "Use `grep` to find existing test files, setup conventions, fixtures, selectors, and assertion patterns.",
                    "Use `read_file` to confirm exact imports, setup, and test idioms in files you already know are relevant.",
                    "Use `run_build` after writing tests to catch placement, syntax, and framework mismatch early.",
                    "Avoid broad rescans once you already know the target layer, file family, and contract.",
                ],
            ),
        ]
    )


def get_tdd_guidance() -> str:
    return "\n\n".join(
        [
            _section(
                "Fast Codebase Understanding",
                [
                    "Start from the current test batch, `<interface_spec>`, `<test_plan>`, `<test_code>`, and `<node_understanding>`.",
                    "Identify one likely owner file and, if needed, one adjacent boundary file before editing.",
                    "Use the prefetched source and test context first; do not rediscover the whole repository unless the current evidence is insufficient.",
                ],
            ),
            _section(
                "Fast Solution Design",
                [
                    "Make one concrete, contract-preserving hypothesis at a time.",
                    "Prefer the smallest edit in existing code over broad refactors, helpers, or compatibility shims.",
                    "Keep changes local to owner files unless the failing output proves a boundary or wiring issue.",
                ],
            ),
            _section(
                "Error Localization",
                [
                    "Use the latest `run_tests` output as the source of truth.",
                    "Classify the failure, state a falsifiable root-cause hypothesis, and name the target files before new reads or reruns.",
                    "If the same failure repeats, assume the current hypothesis is wrong or incomplete and change the evidence plan explicitly.",
                ],
            ),
            _section(
                "Using Your Tools",
                [
                    "Use `grep` to locate symbols, selectors, routes, ownership boundaries, and likely edit locations when the file is not yet known.",
                    "Use `read_file` to confirm the exact current implementation in files you already know are relevant.",
                    "Use `edit_file` or `write_file` to make the smallest fix that can verify the current hypothesis.",
                    "Use `run_tests` only to verify a concrete hypothesis after a minimal change.",
                    "Use `execute_command` only for non-test terminal operations that truly require shell execution, like npm install...",
                ],
            ),
        ]
    )
