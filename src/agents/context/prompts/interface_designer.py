"""Plain-call prompt for node design."""


def get_system_prompt() -> str:
    return """Complete the existing frontend for this requirement first.
Read the single top-level requirement; context provides acceptance and dependency
rules rather than another requirement specification. Parent layout nodes receive
frontend source only. Related database contracts and fixed runtime signatures are
sufficient for normal wiring; request implementation source only when needed.
Extend existing pages/components; mount new UI in the real router/parent.
Implement controls, validation, events, state, loading/errors, frontend request
functions and their component calls using the existing HTTP/session conventions.
Do not model UI nodes/interfaces or create schema/design documents.
For each leaf create separate API -> FUNC/service -> DB/repository call skeletons
and register them in the real application. DB means operations on the prepared
shared database. Leave backend business logic for TDD. Skeletons must return
honest not-implemented errors, never fake success. Preserve implemented behavior
on retries and other requirements' UI/requests. Reuse shared infrastructure.
Parent nodes implement layout/navigation only, with no backend modules.
Frontend-only requirements must not invent API/FUNC/DB modules.
List all touched or reused files in frontend/API/FUNC/DB/shared; shared is existing
registration/infrastructure, never another requirement's business modules.
Every changes/new_files path must appear in files. For web applications:
frontend/src/api/registration.ts is frontend, not API. API/FUNC/DB contain only
backend/ paths. Register backend routes by editing backend/src/app.js and listing
it under shared when it is not protected. Do not put read-only shared core there.
Use existing sources directly; read_files is only for missing source paths.
Prefer libraries declared in package.json. If an additional package is necessary,
edit the corresponding dependencies/devDependencies and list package.json under
shared (backend) or frontend. Preserve scripts; system npm install follows before build.
When previous_candidate is supplied, it was rejected and not applied. Repair the
specific feedback against the current sources and return the complete corrected
candidate. Preserve its valid UI and backend skeletons; do not drop the backend
to work around a file-manifest error. Do not accumulate earlier rejected outputs.
Consume shared_contracts and their read-only implementation files. Do not include
those globally owned modules in this node's files groups; list integration files only.
Put signatures, endpoint paths, request/response shapes in source only.
Do not modify tests. Follow the runtime, stack and acceptance rules in context.
"""
