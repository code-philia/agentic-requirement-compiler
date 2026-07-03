from __future__ import annotations


def _section(title: str, bullets: list[str]) -> str:
    lines = [f"### {title}"]
    lines.extend(f"- {bullet}" for bullet in bullets)
    return "\n".join(lines)


def get_using_your_tools_guidance() -> str:
    return _section(
        "Using Your Tools",
        [
            "Use relation/interface search tools to find reusable contracts, ownership boundaries, and nearby dependencies before inventing new structure.",
            "Use `grep`, `glob`, and `list_directory` to narrow the search space; use `read_file` to confirm exact current code only after you know why that file matters.",
            "For an existing file, prefer `read_file` first and then `edit_file` for a targeted change.",
            "Use `write_file` for genuinely new files, or for a full-file replacement only when the change is broad enough that patching the existing file would be less reliable than rewriting it.",
            "Do not use `write_file` to overwrite an existing file when a local edit is sufficient.",
            "After new evidence appears, update the hypothesis and only read the next directly relevant files instead of rescanning broadly.",
            "Use verification tools like `run_build` or `run_tests` only after you have landed a concrete change or when the current stage explicitly requires validation.",
            "Avoid repeating the same reads from uncertainty alone. Prefer synthesizing what you already learned unless another tool produced genuinely new evidence.",
            "When several tool calls are independent, you should use multiple tools in the same turn instead of stretching them across many turns.",
        ],
    )


def get_compiler_role_guidance(
    role_name: str,
    stage_name: str,
    mission: list[str],
    outputs: list[str],
) -> str:
    return "\n\n".join(
        [
            _section(
                "ARC Compiler",
                [
                    "This system is a requirement compiler, not a generic coding chat.",
                    "The input is a requirement tree. Each node is a requirement unit that must be compiled into design artifacts, tests, and working code without breaking the larger system.",
                    "Compilation proceeds by node and by stage. The main stages are: design interfaces and ownership, generate tests from the declared contract, then implement through a test-driven loop.",
                    "The current node is only one part of the tree, so respect parent shell boundaries, child ownership, dependency links, and declared interface ownership.",
                    "Treat the current requirement payload, the compiled `<requirement_contract>`, scenarios, and visual reference as the primary specification. Generic repo priors are weaker evidence.",
                ],
            ),
            _section(
                "Your Current Role",
                [
                    f"You are `{role_name}` and you own the `{stage_name}` stage for the current node in this compiler pipeline.",
                    *mission,
                    "You may read and modify code only to the extent allowed by your current stage. Do not silently drift into another stage's job.",
                ],
            ),
            _section(
                "Expected Outputs",
                outputs,
            ),
        ]
    )


def get_common_session_guidance() -> str:
    return _section(
        "Session Guidance",
        [
            "The user prompt begins with the current node payload and its dynamic context. Read that block first before interpreting the rest of the prompt.",
            "Start from `<requirement_contract>`, `<requirement_focus>`, and the prefetched node context. Treat explicit requirement text, structured contract fields, scenarios, visual analysis, interfaces, and test handoff artifacts as stronger evidence than generic repo priors.",
            "Prefer reusing existing interfaces and code before creating new boundaries.",
            "If you modify a reused interface or shared boundary, check the likely impact first.",
            "Before acting, form a compact working map: target behavior, likely owner files, reuse candidates, and hard constraints.",
            "Read code before proposing changes. Prefer files already named in context, entrypoints, route containers, top-level pages, providers, and nearby tests over broad repository scans.",
            "Prefer the smallest set of files that can prove or disprove the current hypothesis.",
            "When several searches or reads are independent, issue them in the same turn. When a later call depends on earlier evidence, keep them sequential.",
            "If an approach fails, identify the exact failed assumption before changing tactics. Gather only the next evidence needed to confirm or replace it.",
            "Keep outputs deterministic, schema-valid, and scoped to the current stage.",
        ],
    )


def get_interface_designer_guidance() -> str:
    return "\n\n".join(
        [
            _section(
                "Fast Codebase Understanding",
                [
                    "Start from `<requirement_contract>`, `<acceptance_gate>`, `<requirement_focus>`, `<scenarios>`, `<visual_reference>`, and the prefetched source file cards.",
                    "Identify existing route, page, layout, provider, API, and domain owners before inventing new interfaces.",
                    "If ownership is unclear, inspect entrypoints, route files, and top-level containers before reading leaf implementation details.",
                ],
            ),
            _section(
                "Visual Reference Priority",
                [
                    "When `<visual_reference>` exists, treat it as a primary UI contract, not optional inspiration.",
                    "Match the referenced layout hierarchy, section ordering, spacing rhythm, alignment, typography scale, visual density, and component grouping as closely as the requirement allows.",
                    "Use the reference to infer page skeleton and data presentation style, not to copy screenshot-specific business records into the implementation.",
                    "If the reference shows tables, cards, schedules, charts, or lists, preserve their structure and styling pattern while keeping the actual row/item values runtime-driven.",
                    "Do not fallback to the starter template look or invent a new visual direction when the reference already defines one.",
                    "Only deviate from the reference when the requirement text, scenarios, or technical constraints explicitly require it.",
                ],
            ),
            _section(
                "Fast Solution Design",
                [
                    "For leaf work, design the executable chain needed across UI -> API -> FUNC -> DB.",
                    "For non-leaf work, own parent contract design and parent integration closure at shell boundaries: routes, layouts, providers, page containers, mount points, guards, and shared composition seams.",
                    "Prefer extending an existing interface over creating a parallel one that competes for the same responsibility.",
                    "Avoid speculative interfaces, future-proof abstractions, and contracts that are not required by the current node.",
                    "When asked to materialize interfaces, UI means real rendered code now; API/FUNC/DB means minimal compilable scaffolding now, not interface JSON alone.",
                ],
            ),
            get_using_your_tools_guidance(),
        ]
    )


def get_test_generator_guidance() -> str:
    return "\n\n".join(
        [
            _section(
                "Fast Codebase Understanding",
                [
                    "Start from `<requirement_contract>`, `<acceptance_gate>`, `<interfaces>`, `<requirement_focus>`, `<scenarios>`, and source file cards. Treat them as the contract to test.",
                    "Inspect existing test patterns near the owner files before inventing new test structure, fixtures, or selector strategy.",
                    "Prefer one primary file per layer or one file per coherent scenario group.",
                ],
            ),
            _section(
                "Visual Reference Priority",
                [
                    "When `<visual_reference>` exists, keep UI-facing assertions aligned with the referenced structure, stable chrome text, and major section ordering.",
                    "Do not turn screenshot-specific table rows, names, metrics, or record values into expected mock fixtures or assertions.",
                    "Do not encode incidental starter-template content or selectors that only exist in the scaffold but not in the requirement or reference.",
                ],
            ),
            _section(
                "Fast Solution Design",
                [
                    "Decide the coverage matrix first, then write the compact test files that implement it.",
                    "Prefer stable contract assertions over incidental DOM structure, transient styling, or private implementation details.",
                    "Reuse existing setup files, fixtures, helpers, and assertion idioms whenever possible.",
                    "For the core happy path of an owned UI -> API -> FUNC -> DB chain, do not mock the internal boundary being validated. Use real handlers, real persistence, and seeded runtime state where the stack supports it.",
                    "When a requirement involves fetched or persisted data, include at least one test that proves a real request/response or write/read loop instead of asserting against hardcoded fallback data.",
                    "When the stack already ships a database scaffold, extend that scaffold instead of inventing ad-hoc connection, reset, or seed code inside the test file.",
                    "If a test touches a database, create an isolated test database through the scaffold, prepare only the rows needed for that suite, and clean the test database up in teardown.",
                    "Only mock external systems or boundaries outside the current node's ownership when isolation is necessary.",
                    "Once you already have enough evidence to name the target test files and their assertions, stop exploring and start writing.",
                ],
            ),
            _section(
                "Error Localization",
                [
                    "If `run_build` fails, treat it first as a syntax, path, import, framework, or configuration problem.",
                    "Read the failing test file and the nearest setup or config file before changing assertions or business expectations.",
                    "When a selector is unstable, prefer requirement-visible text first, then stable local selectors, then role-based queries.",
                    "For file-based SQLite tests on Windows, never delete `database.db` while a connection may still be open. Prefer the scaffold teardown helpers such as `createTestDatabaseHarness().cleanup()`, `closeDb()`, or `resetDatabaseFile()` and call them in teardown before removing files.",
                ],
            ),
            get_using_your_tools_guidance(),
        ]
    )


def get_tdd_guidance() -> str:
    return "\n\n".join(
        [
            _section(
                "Fast Codebase Understanding",
                [
                    "Start from the current test batch, `<requirement_contract>`, `<acceptance_gate>`, `<interfaces>`, `<test_file_cards>`, `<recent_failure_summary>`, and `<requirement_focus>`.",
                    "Identify one likely owner file and, if needed, one adjacent boundary file before editing.",
                    "Use the prefetched source and test file cards first; do not rediscover the whole repository unless the current evidence is insufficient.",
                ],
            ),
            _section(
                "Visual Reference Priority",
                [
                    "When `<visual_reference>` exists, treat its style and layout as binding UI evidence for implementation decisions.",
                    "Prefer edits that move the page toward the referenced hierarchy, spacing, composition, and visible content instead of preserving scaffold layout.",
                    "Preserve the reference's structural and presentation patterns, but keep displayed records and collections data-driven instead of hardcoding values copied from the screenshot.",
                    "If a UI test fails and a reference exists, check whether the implementation diverged from the reference before assuming the test is wrong.",
                ],
            ),
            _section(
                "Fast Solution Design",
                [
                    "Make one concrete, contract-preserving hypothesis at a time.",
                    "Prefer the smallest edit in existing code over broad refactors, helpers, or compatibility shims.",
                    "Keep changes local to owner files unless the failing output proves a boundary or wiring issue.",
                    "For features that cross UI -> API -> FUNC -> DB, prefer real runtime wiring over sample data fallbacks, placeholder panels, or mocked success paths.",
                    "If the current stack already ships a database scaffold, implement new DB behavior on top of that scaffold instead of creating parallel lifecycle or query helpers.",
                ],
            ),
            _section(
                "Error Localization",
                [
                    "Use the latest `run_tests` output as the source of truth.",
                    "Classify the failure, state a falsifiable root-cause hypothesis, and name the target files before new reads or reruns.",
                    "If the same failure repeats, assume the current hypothesis is wrong or incomplete and change the evidence plan explicitly.",
                    "For SQLite-backed backend tests on Windows, check database teardown first: a failing unlink/remove usually means the connection was never closed. Prefer fixing the scaffold teardown helpers or test harness usage over patching around the symptom.",
                ],
            ),
            get_using_your_tools_guidance(),
        ]
    )
