"""Small cross-platform helpers for running generated-project commands."""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import time
from typing import Any, Mapping


def resolve_executable(name: str, environment: Mapping[str, str]) -> str | None:
    """Resolve a command using the child process environment's PATH."""

    # Windows environment names are case-insensitive, even when callers pass
    # an ordinary dict with the common spelling "Path". POSIX remains sensitive.
    path = environment.get("PATH")
    if path is None and os.name == "nt":
        path = next((value for key, value in environment.items() if key.upper() == "PATH"), None)
    return shutil.which(name, path=path if path is not None else os.defpath)


def process_group_kwargs() -> dict[str, Any]:
    """Return Popen options that isolate a command and its child processes."""

    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate_process_tree(
    process: subprocess.Popen[Any],
    *,
    grace_seconds: float = 2.0,
) -> None:
    """Terminate a command and its descendants on Windows and POSIX systems."""

    if process.poll() is not None:
        return

    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill is not None:
            try:
                subprocess.run(
                    [taskkill, "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=5.0,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass

    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def terminate_tcp_listeners(
    port: int,
    *,
    wait_seconds: float = 5.0,
) -> tuple[list[int], list[str]]:
    """Terminate processes listening on one compiler-owned test port.

    This is used only before starting an isolated generated-app E2E server. It
    prevents a server left behind by an interrupted Playwright run from being
    mistaken for the application produced by the current iteration.
    """

    normalized_port = max(1, min(65535, int(port)))
    pids, lookup_errors = _tcp_listener_pids(normalized_port)
    pids = sorted({pid for pid in pids if pid > 0 and pid != os.getpid()})
    if lookup_errors:
        return [], lookup_errors
    if not pids:
        return [], []

    errors: list[str] = []
    for pid in pids:
        if os.name == "nt":
            taskkill = shutil.which("taskkill")
            if taskkill is None:
                errors.append("taskkill is unavailable")
                continue
            try:
                completed = subprocess.run(
                    [taskkill, "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                errors.append(f"PID {pid}: {exc}")
                continue
            if completed.returncode != 0 and _pid_exists(pid):
                detail = (completed.stderr or completed.stdout).strip()
                errors.append(f"PID {pid}: {detail or 'taskkill failed'}")
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            except OSError as exc:
                errors.append(f"PID {pid}: {exc}")

    deadline = time.monotonic() + max(0.1, float(wait_seconds))
    while time.monotonic() < deadline:
        remaining, _ = _tcp_listener_pids(normalized_port)
        remaining = [pid for pid in remaining if pid > 0 and pid != os.getpid()]
        if not remaining:
            return pids, errors
        time.sleep(0.1)

    if os.name != "nt":
        remaining, _ = _tcp_listener_pids(normalized_port)
        for pid in remaining:
            if pid <= 0 or pid == os.getpid():
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            except OSError as exc:
                errors.append(f"PID {pid}: {exc}")
        time.sleep(0.1)

    remaining, final_lookup_errors = _tcp_listener_pids(normalized_port)
    errors.extend(final_lookup_errors)
    remaining = [pid for pid in remaining if pid > 0 and pid != os.getpid()]
    if remaining:
        errors.append(
            f"port {normalized_port} is still owned by PID(s) {sorted(set(remaining))}"
        )
    return pids, list(dict.fromkeys(errors))


def _tcp_listener_pids(port: int) -> tuple[list[int], list[str]]:
    if os.name == "nt":
        return _windows_tcp_listener_pids(port)
    return _posix_tcp_listener_pids(port)


def _windows_tcp_listener_pids(port: int) -> tuple[list[int], list[str]]:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is not None:
        command = (
            f"Get-NetTCPConnection -LocalPort {int(port)} -State Listen "
            "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess"
        )
        try:
            completed = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return [], [f"cannot inspect TCP port {port}: {exc}"]
        if completed.returncode == 0:
            return _integer_lines(completed.stdout), []

    netstat = shutil.which("netstat")
    if netstat is None:
        return [], [f"cannot inspect TCP port {port}: netstat is unavailable"]
    try:
        completed = subprocess.run(
            [netstat, "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], [f"cannot inspect TCP port {port}: {exc}"]
    pids: list[int] = []
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0].upper() != "TCP":
            continue
        if _endpoint_port(fields[1]) != port or fields[-2].upper() != "LISTENING":
            continue
        try:
            pids.append(int(fields[-1]))
        except ValueError:
            continue
    return pids, []


def _posix_tcp_listener_pids(port: int) -> tuple[list[int], list[str]]:
    lsof = shutil.which("lsof")
    if lsof is not None:
        try:
            completed = subprocess.run(
                [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return [], [f"cannot inspect TCP port {port}: {exc}"]
        if completed.returncode in {0, 1}:
            return _integer_lines(completed.stdout), []

    # Linux iproute2 provides ss even on many systems without lsof. Unlike
    # fuser, it can select listeners rather than every socket using the port.
    ss = shutil.which("ss")
    if ss is not None:
        try:
            completed = subprocess.run(
                [ss, "-H", "-ltnp", f"sport = :{port}"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return [], [f"cannot inspect TCP port {port}: {exc}"]
        if completed.returncode == 0:
            rows = [line for line in completed.stdout.splitlines() if line.strip()]
            if any(not re.search(r"\bpid=(\d+)", line) for line in rows):
                return [], [f"cannot identify all listeners on TCP port {port}: ss did not expose process IDs; check permissions"]
            return sorted({int(pid) for pid in re.findall(r"\bpid=(\d+)", completed.stdout)}), []

    if not _tcp_port_accepts_connections(port):
        return [], []
    return [], [
        f"cannot identify the process listening on TCP port {port}; install lsof or ss (iproute2)"
    ]


def _integer_lines(value: str) -> list[int]:
    result: list[int] = []
    for token in str(value or "").split():
        try:
            result.append(int(token))
        except ValueError:
            continue
    return result


def _endpoint_port(endpoint: str) -> int | None:
    try:
        return int(str(endpoint).rsplit(":", 1)[-1])
    except ValueError:
        return None


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        tasklist = shutil.which("tasklist")
        if tasklist is None:
            return True
        try:
            completed = subprocess.run(
                [tasklist, "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return True
        return str(pid) in completed.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _tcp_port_accepts_connections(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def run_command(
    command: list[str],
    *,
    cwd: str | os.PathLike[str],
    environment: Mapping[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run a captured command while ensuring timeout cleanup includes children."""

    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **process_group_kwargs(),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        terminate_process_tree(process)
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            command,
            timeout,
            output=stdout or getattr(exc, "output", None),
            stderr=stderr or getattr(exc, "stderr", None),
        ) from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
