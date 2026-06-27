from .android import AndroidAppType
from .base import AppTypeHandler
from .web import WebAppType


APP_TYPE_HANDLERS = {
    "android": AndroidAppType,
    "web": WebAppType,
}


def normalize_app_type(app_type: str) -> str:
    normalized = (app_type or "web").strip().lower()
    return normalized if normalized in APP_TYPE_HANDLERS else "web"


def get_app_type_handler_class(app_type: str) -> type[AppTypeHandler]:
    return APP_TYPE_HANDLERS[normalize_app_type(app_type)]


def create_app_type_handler(
    app_type: str,
    workspace_path: str,
    requirement_path: str,
    interface_designer,
    log_cb,
) -> AppTypeHandler:
    handler_class = get_app_type_handler_class(app_type)
    return handler_class(
        workspace_path=workspace_path,
        requirement_path=requirement_path,
        interface_designer=interface_designer,
        log_cb=log_cb,
    )


def upsert_metadata(project_path: str, app_type: str) -> str:
    return get_app_type_handler_class(app_type).upsert_metadata(project_path)


def read_stack_summary(project_path: str, app_type: str) -> str:
    return get_app_type_handler_class(app_type).read_stack_summary(project_path)
