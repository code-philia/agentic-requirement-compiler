---
name: leaf-full-design
description: Use for ARC InterfaceDesigner work when the current requirement node owns an executable feature chain.
---

# leaf-full-design

Use this skill when you determine the current node owns the smallest executable feature slice required by its scenarios.

## Instructions

1. Treat the current node as owning the smallest executable feature slice required by its scenarios.
2. Design only interfaces needed for the owned chain, which may span UI -> API -> FUNC -> DB when those layers are truly owned by the leaf.
3. Prefer existing entrypoints, boundary files, services, and persistence scaffolds before creating new owners.
4. Reuse existing parent/dependency interfaces when the leaf should implement, extend, or call them; return the existing `interface_id` so the current node can be attached to the same contract.
5. If a parent-designed UI shell is the user-facing entry for the leaf, include that UI interface in the leaf's interface list and connect it to leaf-owned API/FUNC/DB interfaces through callers/callees.
6. Materialize only minimal compilable owner files or skeleton code during DESIGN when the interface would otherwise be only abstract JSON.
7. Do not implement full validation, persistence, authentication/session semantics, encryption, or business behavior in DESIGN; that belongs to TestDrivenDeveloper.
8. Avoid large file writes in DESIGN. Prefer route/client/service/database placeholders with explicit TODO-free contract shape over complete feature implementation.
9. Do not hardcode screenshot data, sample rows, fake success messages, or fallback arrays as completed behavior.
10. Return interfaces that make later test generation straightforward: include `file_path`, `first_line`, responsibility, specification, callers/callees, and test focus.
11. The interface `type` must be one of `UI`, `API`, `FUNC`, or `DB`.
12. Keep the chain connected to existing routes, handlers, tests, and persistence so the leaf compiles into the current system rather than a detached fragment.
13. If the feature affects auth/session/authenticated state/current user/account state, use the auth-session-consistency skill and include global session state, session API, service, and persistence interfaces as needed.
