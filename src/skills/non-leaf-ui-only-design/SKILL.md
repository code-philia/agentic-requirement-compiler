---
name: non-leaf-ui-only-design
description: Use for ARC InterfaceDesigner work when a parent node only owns visual shell, layout reference behavior, route slots, or child mount surfaces. This is intentionally weak design, not feature implementation.
---

# non-leaf-ui-only-design

Use this skill when you determine the current node owns only visual shell, layout, route slots, navigation surface, or mount-point behavior.

Non-leaf UI-only design must stay thin. The purpose is to give children a stable visual/composition boundary, not to pre-implement child features or create test-heavy behavior.

## Instructions

1. Use this skill only when the non-leaf node has visual references or explicit parent-owned UI composition requirements.
2. Focus on visual shell, layout hierarchy, navigation surface, container structure, and presentation scaffolding.
3. Match visual reference structure where present: ordering, grouping, spacing, density, major visible regions, and style language.
4. Do not implement child-owned business behavior or detailed runtime interactions.
5. If visual references exist, materialize the UI shell files and style boundary that preserve the parent composition; otherwise return no interfaces and do not read or edit files.
6. Prefer one or two broad interface contracts for the shell or mount boundary; avoid one interface per visual subsection.
7. Interface specifications should describe stable regions and allowed child attachment points, not exact child copy, form internals, route-transition mechanics, or backend data loops.
8. Do not create API, FUNC, or DB contracts for non-leaf UI-only design.
9. Keep `test_focus` minimal: smoke-level renderability, presence of the parent shell, and absence of fake child data are enough.
10. The interface `type` must be one of `UI`, `API`, `FUNC`, or `DB`; for non-leaf UI-only design, default to `UI`.
11. Keep data dynamic or placeholder-neutral; never copy screenshot rows as business data.
