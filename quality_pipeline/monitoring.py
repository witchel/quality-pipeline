"""GPU detection and resource monitoring."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .output import C, format_duration


def detect_gpu() -> str:
    if shutil.which("nvidia-smi"):
        try:
            subprocess.run(
                ["nvidia-smi"], capture_output=True, check=True, timeout=5
            )
            return "nvidia"
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
    if shutil.which("rocm-smi"):
        return "rocm"
    return "none"


def _get_cpu_info() -> str:
    """Return a short CPU load summary, e.g. 'load 2.1 (8 cores)'."""
    try:
        load1 = os.getloadavg()[0]
        ncpu = os.cpu_count() or "?"
        return f"load {load1:.1f} ({ncpu} cores)"
    except OSError:
        return "?"


def _get_memory_info() -> str:
    """Return a short memory usage summary, e.g. '4096/16384 MB (25%)'."""
    system = platform.system()
    if system == "Darwin":
        return _get_memory_darwin()
    if system == "Linux":
        return _get_memory_linux()
    return "?"


def _get_memory_darwin() -> str:
    """Read macOS memory stats via sysctl + vm_stat."""
    try:
        mem_total = int(
            subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, check=True, timeout=5,
            ).stdout.strip()
        ) // (1024 * 1024)
        page_size = int(
            subprocess.run(
                ["sysctl", "-n", "hw.pagesize"],
                capture_output=True, text=True, check=True, timeout=5,
            ).stdout.strip()
        )
        vm_out = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, check=True, timeout=5,
        ).stdout
        pages = {"active": 0, "wired": 0, "compressed": 0}
        for line in vm_out.splitlines():
            if "Pages active" in line:
                pages["active"] = int(re.sub(r"\D", "", line.split(":")[-1]))
            elif "Pages wired" in line:
                pages["wired"] = int(re.sub(r"\D", "", line.split(":")[-1]))
            elif "occupied by compressor" in line:
                pages["compressed"] = int(re.sub(r"\D", "", line.split(":")[-1]))
        used_mb = sum(pages.values()) * page_size // (1024 * 1024)
        if mem_total > 0:
            pct = used_mb * 100 // mem_total
            return f"{used_mb}/{mem_total} MB ({pct}%)"
    except Exception:
        pass
    return "?"


def _get_memory_linux() -> str:
    """Read Linux memory stats from /proc/meminfo."""
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return "?"
    try:
        data = meminfo.read_text()
        total = avail = 0
        for line in data.splitlines():
            if line.startswith("MemTotal:"):
                total = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                avail = int(line.split()[1])
        if total > 0:
            used = total - avail
            return (
                f"{used // 1024}/{total // 1024} MB "
                f"({used * 100 // total}%)"
            )
    except Exception:
        pass
    return "?"


def _get_gpu_info(gpu_type: str) -> str:
    """Return GPU utilization summary, or empty string if idle/unavailable."""
    if gpu_type == "nvidia":
        return _get_gpu_nvidia()
    if gpu_type == "rocm":
        return _get_gpu_rocm()
    return ""


def _get_gpu_nvidia() -> str:
    """Query nvidia-smi for GPU utilization."""
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip()
        parts = []
        any_active = False
        for line in out.splitlines():
            fields = [f.strip() for f in line.split(",")]
            if len(fields) >= 4:
                idx, util, mem_u, mem_t = fields[:4]
                if util.isdigit() and int(util) > 0:
                    any_active = True
                parts.append(f"GPU{idx}: {util}% VRAM {mem_u}/{mem_t} MB")
        if any_active:
            return ", ".join(parts)
    except Exception:
        pass
    return ""


def _get_gpu_rocm() -> str:
    """Query rocm-smi for GPU utilization."""
    try:
        out = subprocess.run(
            ["rocm-smi", "--showgpuuse"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout
        m = re.search(r"(\d+)\s*%", out)
        if m and int(m.group(1)) > 0:
            return f"GPU: {m.group(1)}%"
    except Exception:
        pass
    return ""


def get_resource_snapshot(gpu_type: str = "none") -> str:
    """Collect CPU, memory, and GPU stats into a one-line summary."""
    report = f"CPU: {_get_cpu_info()} | Mem: {_get_memory_info()}"
    gpu_info = _get_gpu_info(gpu_type)
    if gpu_info:
        report += f" | {gpu_info}"
    return report


class ResourceMonitor:
    """Daemon thread that logs resource usage periodically."""

    def __init__(
        self, interval: int, gpu_type: str, start_epoch: float,
    ) -> None:
        self._stop = threading.Event()
        self._interval = interval
        self._gpu_type = gpu_type
        self._start_epoch = start_epoch
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            elapsed = int(time.time() - self._start_epoch)
            snapshot = get_resource_snapshot(self._gpu_type)
            C.log(f"  \u23f1 {format_duration(elapsed)} | {snapshot}")
