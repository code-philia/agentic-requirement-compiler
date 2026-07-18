---
name: leaf-test-layer-selection
description: Use for ARC TestGenerator work when a leaf node must choose Unit, Integration, and E2E coverage based on owned interfaces.
---

# leaf-test-layer-selection

Use this skill when generating tests for a node that owns executable behavior.

## Instructions

1. Choose only layers that match the current node's owned interfaces and scenarios.
2. If the requirement declares scenarios, generate E2E tests for those scenarios. Scenario presence is a strong signal for E2E coverage.
3. Use Unit tests for owned FUNC or DB contracts when direct logic or persistence behavior is present.
4. Use Integration tests for API, boundary wiring, and service collaboration.
5. Use E2E tests for every current-node user-visible scenario flow; do not reduce scenario coverage to a simple render smoke test.
6. Prefer one stable file per layer unless scenarios naturally split into coherent groups.
7. Do not create tests for parent-owned shell behavior or child-owned future behavior.
8. Before writing tests, translate every declared scenario into a private checklist: GIVEN is setup, WHEN is action, THEN is assertion. The generated test must follow that checklist without reversing any condition.
9. Never assert the opposite of a requirement precondition. If the scenario says global navigation is visible, do not assert the logo/nav is absent. If it says a user can click a control, do not start by asserting the control is missing.
10. Match the project's existing Vitest module style. Do not use `require('vitest')`; prefer ESM `import { describe, expect, it, vi } from 'vitest'`, and use `createRequire(import.meta.url)` only when an ESM test must load CommonJS application modules.
11. For web frontend Vitest tests, use `.test.tsx` or `.spec.tsx` whenever the file contains JSX. JSX includes `<App />`, `<RouterProvider />`, `<MemoryRouter>`, and `render(<...>)`.
12. Use `.test.ts` or `.spec.ts` only when the file has no JSX. Do not create a `.ts` file that merely imports a `.tsx` test as a bridge; the manifest must point at the real executable test file.
13. Avoid brittle React Router tests that rerender a `MemoryRouter` to change `initialEntries`; render separate router instances or assert only the current route under test.
14. Mount providers and routers in the same shape the app expects. If nearby tests already use `MemoryRouter`, `BrowserRouter`, `createMemoryRouter`, or custom providers, follow that pattern instead of inventing a competing harness.
15. When the scenario depends on data that is fetched, submitted, or persisted, prefer a real request/response or write/read loop over fallback arrays or mock success payloads.
16. For E2E selectors, prefer requirement-stated labels, roles, routes, and visible outcomes first; if the requirement leaves the selector unspecified, define a stable accessible contract and keep implementation aligned to it.
17. For labels that are substrings of other labels, such as `Password` and `Confirm password`, avoid ambiguous `getByLabel('Password')`. Use exact accessible-role selectors, stable ids, or scoped locators in the generated test.
18. If a link/control label can match another accessible name, scope the locator to the correct landmark or container, such as `page.getByRole('navigation', { name: 'Global' }).getByRole('link', { name: 'Books', exact: true })`.
19. Never make a leaf-node test pass by asserting temporary design scaffolds such as `NOT_IMPLEMENTED`, HTTP 501, placeholder payloads, TODO text, or no-op behavior. Tests must describe the final behavior the TDD agent should implement.
20. If the scenario changes auth/session/authenticated state/current user/account state, use the auth-session-consistency skill and verify global state transitions or session recovery, not only a local success banner.
21. For Playwright tests, do not invent custom fixtures unless you define and consume the same fixture name exactly. If you define `runtime: async ({}, use) => use(runtime)`, consume `{ runtime }`; if you define `harness`, consume `{ harness }`. Avoid `use({ runtime })` unless the consumed fixture is the object wrapper.
22. If the scenario changes cart, checkout, account, product, order, catalog, inventory, or persisted user-owned data, verify the connected domain path from UI/API/client through service or persistence when that path is part of the interface contract. Do not let a local-only counter, static array, or fake success message satisfy durable behavior.
23. Final quality gate before returning: every manifest path matches its file contents and runner, every assertion maps to a requirement/interface outcome, and no assertion contradicts GIVEN/WHEN/THEN.
