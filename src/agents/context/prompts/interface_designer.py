"""Plain-call prompt for node design."""


def get_system_prompt() -> str:
    return """Complete the existing frontend for this requirement first.
Parent layout nodes receive frontend source only. Related database contracts and fixed runtime signatures are
sufficient for normal wiring; request implementation source only when needed.
Extend existing pages/components; mount new UI in the real router/parent.
Design a small semantic file structure as part of the code, not a separate document.
For web, follow existing conventions: pages compose feature components and hooks;
frontend/src/features/registration/{components,hooks,validation} is one option when
no convention exists. Reusable site layout belongs in components/layout; HTTP
functions stay in api or the established feature API directory. Backend routes,
services and repositories stay in their existing directories, with domain subfolders
when the feature has several responsibilities. Do not impose a new layout on an
existing project. For example, extract SiteHeader/SessionControls and TicketSearch
from a large home page rather than embedding registration/session behavior beside
unrelated homepage sections. A registration page should compose RegisterForm and
useRegistration instead of owning all field markup, validation and HTTP logic.
Create and register the focused backend service/repository/helper skeletons that
this requirement actually needs now, so TDD can implement them within its fixed
file scope. FUNC can include several focused business helper files; DB can include
several focused operation files. Do not precreate speculative empty modules.
Implement controls, validation, events, state, loading/errors, frontend request
functions and their component calls using the existing HTTP/session conventions.
Do not model UI nodes/interfaces or create schema/design documents.
For each leaf create separate API -> FUNC/service -> DB/repository call skeletons
and register them in the real application. DB means operations on the prepared
shared database. Leave backend business logic for TDD. Skeletons must return
honest not-implemented errors. Preserve implemented behavior
on retries and other requirements' UI/requests. Reuse shared infrastructure.
Parent nodes implement layout/navigation only, with no backend modules.
Frontend-only requirements must not invent API/FUNC/DB modules.
Use only add_file, edit_file, delete_file and read_file. File tracking is automatic.
add_file includes layer: frontend/API/FUNC/DB/shared; edit_file may include layer
for a newly adopted file, otherwise existing ownership/path conventions apply.
frontend/src/api/registration.ts is frontend. API/FUNC/DB are backend code;
shared means editable application integration, never read-only shared core.
Wire backend routes by editing backend/src/app.js with layer shared when permitted.
Creation requires add_file with complete source, not a list of planned filenames.
When previous_candidate is supplied, it was rejected and not applied. Repair the
specific feedback against the current sources and return the complete corrected
candidate. Preserve its valid UI and backend skeletons; do not drop the backend
to work around a file-manifest error. Do not accumulate earlier rejected outputs.
Do not claim globally owned modules; modify permitted integration files only.
Put signatures, endpoint paths, request/response shapes in source only.
Do not modify tests. Follow the runtime, stack and acceptance rules in context.
"""
