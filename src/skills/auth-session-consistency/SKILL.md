---
name: auth-session-consistency
description: Use for executable leaf-node work involving login, registration, logout, session, authenticated state, current user, account state, or authorization-sensitive navigation.
---

# auth-session-consistency

Use this skill when a leaf node owns authentication/session behavior.

## Instructions

1. Treat authentication state as a cross-cutting system contract, not a page-local success message.
2. Apply this skill to executable leaf-node work. Non-leaf UI/composition nodes may expose auth entry points or shell slots, but they do not design API, FUNC, or DB session contracts.
3. Design or reuse connected interfaces for UI session state, auth API routes, service logic, and session persistence when those layers are relevant.
4. Prefer existing shell/header/provider/client/server/database boundaries before creating new owners.
5. A registration or login success should update global authenticated state and expose a durable session recovery path.
6. Provide a session-loading API or equivalent runtime boundary when the app needs to restore authenticated state after refresh/navigation.
7. Header, navigation, protected/public route behavior, and current-user display should consume the same global session state rather than duplicating local page state.
8. Tests should verify observable global state transitions: unauthenticated chrome before the action, authenticated chrome/current-user state after the action, and session recovery when required by the scenario.
9. Do not satisfy authenticated-state requirements with only a page-local success banner, fake user object, local fallback array, or hardcoded header text.
10. Keep cookies/tokens/session records consistent across UI client, API responses, service logic, and DB schema.
11. Preserve parent shell ownership: if the header/shell is parent-designed, return and use that reused UI interface with callers/callees linked to leaf-owned auth/session interfaces.
