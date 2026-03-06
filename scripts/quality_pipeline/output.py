"""Color output helpers and formatting utilities."""

from __future__ import annotations

import sys


class ColorOutput:
    def __init__(self) -> None:
        out_tty = sys.stdout.isatty()
        err_tty = sys.stderr.isatty()
        self.RED = "\033[0;31m" if err_tty else ""
        self.GREEN = "\033[0;32m" if out_tty else ""
        self.YELLOW = "\033[1;33m" if out_tty else ""
        self.BLUE = "\033[0;34m" if out_tty else ""
        self.BOLD = "\033[1m" if out_tty else ""
        self.NC = "\033[0m" if (out_tty or err_tty) else ""
        self._err_nc = "\033[0m" if err_tty else ""

    def log(self, msg: str) -> None:
        print(f"{self.BLUE}[pipeline]{self.NC} {msg}", flush=True)

    def ok(self, msg: str) -> None:
        print(f"{self.GREEN}[pipeline]{self.NC} {msg}", flush=True)

    def warn(self, msg: str) -> None:
        print(f"{self.YELLOW}[pipeline]{self.NC} {msg}", flush=True)

    def err(self, msg: str) -> None:
        print(f"{self.RED}[pipeline]{self._err_nc} {msg}", file=sys.stderr, flush=True)


C = ColorOutput()


def gate_label(gate: str) -> str:
    labels = {
        "hard": f"{C.RED}HARD{C.NC}",
        "soft": f"{C.YELLOW}SOFT{C.NC}",
        "none": f"{C.BLUE}NONE{C.NC}",
    }
    return labels.get(gate, gate)


def format_duration(secs: int) -> str:
    if secs >= 3600:
        return f"{secs // 3600}h {secs % 3600 // 60}m {secs % 60}s"
    if secs >= 60:
        return f"{secs // 60}m {secs % 60}s"
    return f"{secs}s"
