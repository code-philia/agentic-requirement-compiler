"""core.termui — shared terminal primitives for ARC terminal tools (monitor, timeline).

Stdlib only. Provides ANSI styling, raw-keyboard input, text wrapping, and
terminal-size helpers used by the live dashboards.
"""

from __future__ import annotations

import os
import select
import sys
import termios
import tty
import unicodedata

# Module-level color switch; tools set this via set_color().
USE_COLOR = True
RESET = "\x1b[0m"

FG = {
    "red": "31", "green": "32", "yellow": "33", "blue": "34",
    "magenta": "35", "cyan": "36", "white": "37", "gray": "90", "orange": "38;5;208",
}


def set_color(enabled: bool) -> None:
    global USE_COLOR
    USE_COLOR = bool(enabled)


def col(text: str, fg: str | None = None, bold: bool = False, dim: bool = False, rev: bool = False) -> str:
    if not USE_COLOR:
        return text
    codes = []
    if bold:
        codes.append("1")
    if dim:
        codes.append("2")
    if rev:
        codes.append("7")
    if fg:
        codes.append(FG[fg])
    if not codes:
        return text
    return f"\x1b[{';'.join(codes)}m{text}{RESET}"


def state_color(state: str) -> tuple[str | None, bool]:
    return {
        "RUNNING": ("yellow", True), "COMPLETED": ("green", False),
        "FAILED": ("red", True), "PENDING": ("gray", False), "UNSEEN": ("gray", False),
        "DESIGNING": ("yellow", False), "DESIGNED": ("blue", False),
        "IMPLEMENTING": ("yellow", True), "IMPLEMENTED": ("green", False),
        "PASSED": ("green", True),
    }.get(state, (None, False))


def type_color(t: str) -> str | None:
    return {"UI": "blue", "API": "magenta", "FUNC": "cyan", "DB": "orange"}.get(t)


def phase_color(phase: str) -> str | None:
    return {"design": "blue", "implement": "yellow", "test": "green"}.get(phase, None)


def wrap_text(text: str, width: int) -> list[str]:
    """Wrap text at `width`, breaking long tokens (ids, URLs, JSON) as needed."""
    if width <= 0:
        return []
    lines: list[str] = []
    for raw in text.split("\n"):
        if not raw:
            lines.append("")
            continue
        lead = len(raw) - len(raw.lstrip(" "))
        if lead:
            raw = raw[lead:]
        cur = ""
        first = True
        for word in raw.split(" "):
            while len(word) > width:
                if cur:
                    lines.append(cur)
                    cur = ""
                lines.append(((" " * lead) if first else "") + word[:width])
                first = False
                word = word[width:]
            if cur and len(cur) + 1 + len(word) > width:
                lines.append(cur)
                cur = word
            elif cur:
                cur += " " + word
            else:
                cur = word
            if first and word:
                cur = (" " * lead) + word
                first = False
        lines.append(cur)
    return lines


class Row:
    """A renderable line that may carry a click/select target (kind + id)."""

    __slots__ = ("line", "kind", "id")

    def __init__(self, line: str, kind: str | None = None, id: str | None = None) -> None:
        self.line = line
        self.kind = kind
        self.id = id


class Input:
    """Raw (cbreak) keyboard input with escape-sequence decoding."""

    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self.saved = None
        self._stash: bytes = b""

    def enter(self) -> None:
        try:
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        except (termios.error, OSError):
            self.saved = None

    def leave(self) -> None:
        if self.saved is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
            except (termios.error, OSError):
                pass
            self.saved = None

    def pending(self, timeout: float) -> bool:
        try:
            r, _, _ = select.select([self.fd], [], [], timeout)
            return bool(r)
        except (OSError, ValueError):
            return False

    def _read_esc_sequence(self, timeout: float) -> bytes | None:
        """Read a CSI/SS3 sequence after ESC. Returns None for a bare ESC."""
        if not self.pending(timeout):
            return None
        intro = os.read(self.fd, 1)
        if intro == b"[":
            buf = b""
            while True:
                if not self.pending(timeout):
                    return None
                b = os.read(self.fd, 1)
                if 0x40 <= b[0] <= 0x7E:  # final byte of the CSI sequence
                    return b"[" + buf + b
                buf += b
                if len(buf) > 16:
                    return None
        if intro == b"O":  # SS3 (application cursor keys)
            if not self.pending(timeout):
                return None
            return b"O" + os.read(self.fd, 1)
        self._stash = intro  # bare ESC followed by a plain char — keep the char
        return None

    def read_key(self, esc_timeout: float = 0.15) -> str | None:
        """Return a key name ('up','enter','q',...) or None when idle."""
        if self._stash:
            ch = self._stash
            self._stash = b""
        elif not self.pending(0.0):
            return None
        else:
            ch = os.read(self.fd, 1)
        if ch == b"\x1b":
            seq = self._read_esc_sequence(esc_timeout)
            if seq is None:
                return "esc"
            if seq in (b"[A", b"OA"):
                return "up"
            if seq in (b"[B", b"OB"):
                return "down"
            if seq in (b"[C", b"OC"):
                return "right"
            if seq in (b"[D", b"OD"):
                return "left"
            if seq in (b"[H", b"[1~"):
                return "home"
            if seq in (b"[F", b"[4~"):
                return "end"
            if seq == b"[5~":
                return "pgup"
            if seq == b"[6~":
                return "pgdn"
            if seq == b"[Z":
                return "shift-tab"
            return "esc"
        try:
            name = ch.decode()
        except UnicodeDecodeError:
            return None
        if name == "\r" or name == "\n":
            return "enter"
        if name == "\t":
            return "tab"
        if name == "\x7f":
            return "backspace"
        # Preserve case so distinct keys like `g` (top) and `G` (bottom) survive.
        return name


def hide_cursor() -> None:
    sys.stdout.write("\x1b[?25l")
    sys.stdout.flush()


def show_cursor() -> None:
    sys.stdout.write("\x1b[?25h")
    sys.stdout.flush()


def terminal_size() -> tuple[int, int]:
    try:
        return os.get_terminal_size(sys.stdout.fileno())
    except (OSError, ValueError):
        return (100, 40)


def fit_frame(frame: list[str], width: int, height: int) -> str:
    """Pad/truncate a frame to the terminal size and return it as one write."""
    padded = []
    for i in range(height):
        line = frame[i] if i < len(frame) else ""
        line = clip(line, width)
        line = line + " " * max(0, width - vis_width(line))
        padded.append(line)
    return "\x1b[H\x1b[J" + "\n".join(padded)


def _iter_cells(text: str):
    """Iterate a string as (kind, value, width) cells.

    kind is 'esc' for an ANSI escape sequence (width 0) or 'ch' for a display
    character (width 1, or 2 for wide East-Asian glyphs).
    """
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\x1b":
            j = i + 1
            if j < n and text[j] == "[":
                j += 1  # CSI: consume params/intermediates (< 0x40), then final byte
                while j < n and ord(text[j]) < 0x40:
                    j += 1
                if j < n:
                    j += 1
            else:
                j += 1  # short non-CSI escape
            yield ("esc", text[i:j], 0)
            i = j
        else:
            w = 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
            yield ("ch", c, w)
            i += 1


def strip_ansi(text: str) -> str:
    return "".join(v for k, v, _ in _iter_cells(text) if k == "ch")


def vis_width(text: str) -> int:
    """Visible column count of a string, ignoring ANSI escapes."""
    return sum(w for _, _, w in _iter_cells(text))


def clip(text: str, width: int) -> str:
    """Truncate `text` to `width` visible columns, ANSI-safe.

    Escape sequences consume no columns and are preserved verbatim. Any open
    SGR attribute (e.g. the reverse video of a selected row) is re-closed at
    the cut so it cannot bleed into the next line. Wide glyphs are never split.
    """
    if width <= 0:
        return ""
    out: list[str] = []
    w = 0
    sgr_open = False
    for kind, val, cw in _iter_cells(text):
        if kind == "esc":
            if val.startswith("\x1b[") and val.endswith("m"):
                sgr_open = val[2:-1] not in ("", "0")
            out.append(val)
        else:
            if w + cw > width:
                break
            out.append(val)
            w += cw
    if sgr_open:
        out.append(RESET)
    return "".join(out)
