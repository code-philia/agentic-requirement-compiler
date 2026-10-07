from __future__ import annotations


def section(title: str, lines: list[str]) -> str:
    return "\n".join([f"### {title}", *(f"- {line}" for line in lines)])


def structured_contract_policy() -> str:
    return section("Structured data and interaction contracts", [
        "Use requirement.resolved_data as the compiler-resolved data.requires definitions. application_contract contains root conventions. Preserve node and scenario data/interactions; they are authoritative requirements, not optional hints.",
        "SEED is initialized by the real prepared database, never by frontend arrays or an E2E-only endpoint. CREATED is produced through its declared UI/runtime flow; example names are scenario roles, not mandatory initial rows. DERIVED is action-derived state; persistence depends on reload/saved-state requirements, not its lifecycle alone.",
        "Preserve identity within each scenario. When required, compose run/worker/case suffixes once and reuse the resulting title/label across creation, selection and assertions. Never hardcode dynamic example names in product behavior.",
        "interactions defines public role/accessibility-name semantics. Implement and test these exact contracts even if current markup is wrong. Interaction IDs are references, not prescribed DOM IDs or data-testid values. Do not create UI nodes or a UI inventory.",
        "Prefer getByRole(role, {name, exact:true}) or associated labels. Scope repeated actions to the declared navigation/dialog/item region. Substitute {title} with the actual scenario identity. Preserve declared selected/expanded/toggle states and keyboard access; do not rely on DOM order or screenshot text.",
        "For E2E prerequisite data declared CREATED via UI, use that creation flow rather than direct SQL or API/harness fixtures. Lower-layer Unit/Integration fixtures may arrange equivalent isolated states without replacing the product creation path. Keep the existing short smoke coverage policy while preserving declared semantics and preconditions.",
        "Explicit historical target time is a test/run clock contract, not the production default. Date-sensitive backend behavior must use a shared injectable business clock. Production uses real time; isolated acceptance configures its declared time explicitly. Freezing browser time alone does not freeze the backend. Never special-case titles or rewrite seed timestamps to pass retention assertions.",
        "Web shared clocks use nowMs() -> epoch milliseconds, optionally an injected clock for lower-layer tests. Read ARC_TEST_NOW only when ARC_TEST_CLOCK_ENABLED === '1'; validate configured time and fail on invalid values, otherwise use Date.now(). Reuse existing clock code or request_shared for the missing reusable capability. requirement.test_clock describes the actual runner configuration; do not assume a prose target time has automatically configured the backend. An unconfigured historical test must report the missing clock configuration rather than weaken its assertions.",
        "Legacy documents without these fields retain their prose/scenario rules. Structured contracts take precedence over conflicting locator/setup advice.",
    ])
