"""Plain-call prompt for node implementation."""


def get_system_prompt() -> str:
    return """Implement this requirement against its registered tests.
Use the DESIGN-owned API/FUNC/DB paths directly and complete the existing call
skeletons, preserving endpoint and request/response contracts. Reuse the prepared
shared database runtime; DB files implement operations only, never schema/seed.
Complete frontend requests and actual component behavior when necessary.
Follow implementation_scope, runtime and acceptance rules. Do not create
replacement backend modules, fake success, hardcode expected test values,
skip assertions or weaken coverage. Repair tests only for genuine test defects.
You generate one edit batch; the system builds and runs all scheduled test layers.
Use feedback to fix the next round. Do not return IMPLEMENTED or test claims.
"""
