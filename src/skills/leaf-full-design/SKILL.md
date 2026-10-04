---
name: leaf-full-design
description: Complete a leaf requirement's frontend and request wiring, then create its independent backend call skeleton.
---

# leaf-full-design

1. Start from the actual page/component, its router/parent, and the nearest API client. Use materialized_files as code locations, not as UI contracts.
2. Extend an existing UI in place; create and mount new components/pages only when needed. Preserve unrelated UI and behavior.
3. Complete controls, events, frontend state/validation, and loading/error/success handling. Implement the real frontend request function and call it from the component or hook.
4. Match the backend endpoint method/path and request/response shapes. Use existing HTTP, auth/session, and error infrastructure. Do not fake successful responses or records.
5. Generate independent node-owned backend API -> FUNC/service -> DB/repository operation skeletons and register the route. Leave backend business behavior and database queries/mutations to TDD; an unfinished skeleton must fail honestly.
6. DB operation functions use the shared prepared schema/runtime. Reference GLOBAL:DB table ids without returning or modifying their global contracts. Do not generate schema or seeds.
7. Keep other nodes' backend business modules intact. Reuse shared runtime/auth infrastructure and extend only central registration as necessary.
8. Preserve working backend implementation and stable ids on retry. Never replace an implemented function with a stub.
9. Return only backend API/FUNC/DB contracts, plus summary and files_written for every frontend/backend change. Do not model UI or frontend clients as interfaces or separate nodes.
10. For auth/session work, wire shared session state and consumers in the frontend and use the auth-session-consistency skill.
11. For CLI/Android, complete the command/UI entrypoint and local call wiring with service/persistence skeletons; do not invent an HTTP backend.
