import os


WORKSPACE_ROOT = os.getcwd()
APP_TYPE = "web"
ANDROID_PACKAGE = "com.example.template"


def set_workspace_root(path: str) -> None:
    global WORKSPACE_ROOT
    WORKSPACE_ROOT = os.path.abspath(path)


def get_abs_path(rel_path: str) -> str:
    if os.path.isabs(rel_path):
        return rel_path
    return os.path.join(WORKSPACE_ROOT, rel_path)


def set_app_type(app_type: str) -> None:
    global APP_TYPE
    APP_TYPE = (app_type or "web").strip().lower()


def get_app_type() -> str:
    return APP_TYPE


def set_android_package(package_name: str) -> None:
    global ANDROID_PACKAGE
    ANDROID_PACKAGE = package_name.strip()


def get_android_package() -> str:
    return ANDROID_PACKAGE
