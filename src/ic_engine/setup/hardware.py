#!/usr/bin/env python3
# Copyright 2026 InvestorClaw Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""
InvestorClaw Hardware Detection — CPU, GPU, and memory probing.

Used by the setup/llm-config flow to auto-pick a local inference backend
(llama-server, Ollama, vLLM) based on available VRAM, or to fall back to
CPU-only paths on hosts without a GPU.

Supports:
- macOS (Apple Silicon M-series unified memory; Intel Macs)
- Linux x86_64 and ARM64 (NVIDIA, AMD, Intel, no-GPU Pi-class hosts)
- Windows: run under WSL2 Ubuntu 24.04 (the Linux path works unchanged;
  nvidia-smi passthrough is supported via DxCore). Native Windows is
  explicitly out of scope.

Usage:
    from investorclaw.setup.hardware import HardwareProfile

    hardware = HardwareProfile()
    if hardware.can_use_gpu(min_free_memory_gb=5.0):
        # use llama-server / Ollama / vLLM with GPU model
        ...
    else:
        # fall back to CPU inference or cloud provider
        ...

Vendored from the canonical upstream ETLANTIS module:
    etlantis/hardware.py  (Jason Perlow, same author; Apache 2.0 relicensed).

Re-sync workflow: upstream edits land here via a straight copy plus
re-applied license header and this docstring. Keep the detector logic
byte-for-byte identical to ETLANTIS to avoid drift — only the header
and this docstring differ.
"""

import json
import multiprocessing
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Callable, Dict, List, Optional

# Module-level logger - can be overridden by calling set_logger()
_log: Callable[[str], None] = print


def set_logger(log_fn: Callable[[str], None]) -> None:
    """
    Set custom logger function for etlantis output.

    Args:
        log_fn: Callable that accepts a string (e.g., logger.info)
    """
    global _log
    _log = log_fn


def _parse_int(value) -> int:
    """Coerce rocm-smi / xpu-smi fields (often strings like "1234" or "45 %") to int.

    Returns 0 for None, empty, or unparseable input rather than raising — the
    caller treats 0 as "unknown" and routes accordingly.
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    s = str(value).strip()
    if not s:
        return 0
    # Strip trailing unit like "%", "B", "MB"
    num = ""
    for ch in s:
        if ch.isdigit() or (ch == "-" and not num):
            num += ch
        elif num:
            break
    try:
        return int(num) if num else 0
    except ValueError:
        return 0


@dataclass
class CPUInfo:
    """CPU hardware information."""

    cores: int
    architecture: str  # "x86_64", "arm64"
    model: str
    platform: str  # "linux", "darwin", "windows"


@dataclass
class GPUDevice:
    """Single GPU device information.

    memory_total_mb is 0 for integrated GPUs (Intel iGPU, Apple unified memory
    when not reported by system_profiler) that share system RAM. Callers routing
    workloads by memory should treat 0 as 'use CPU memory ceiling' rather than
    'no memory.'
    """

    name: str
    vendor: str  # "nvidia", "amd", "intel", "apple"
    memory_total_mb: int
    memory_free_mb: int
    memory_used_mb: int
    utilization_percent: Optional[int] = None
    compute_capability: Optional[str] = None
    integrated: bool = False  # True for iGPUs / unified memory


@dataclass
class GPUInfo:
    """GPU hardware information."""

    available: bool
    devices: List[GPUDevice]


@dataclass
class MemoryInfo:
    """System memory information."""

    total_gb: float
    available_gb: float
    used_gb: float


class HardwareProfile:
    """
    Detect and profile system hardware capabilities.

    Provides a unified interface for CPU/GPU/memory detection across
    Linux and macOS hosts, regardless of specific system names or IPs.
    """

    def __init__(self):
        """Initialize hardware detection.

        Order matters: CPU → memory → GPU. Metal detection on Apple Silicon
        needs ``self.memory`` to approximate unified-memory free space.
        """
        self.cpu = self._detect_cpu()
        self.memory = self._detect_memory()
        self.gpu = self._detect_gpu()

    def _detect_cpu(self) -> CPUInfo:
        """
        Detect CPU information across platforms.

        Returns:
            CPUInfo with cores, architecture, model, platform
        """
        cores = multiprocessing.cpu_count()
        arch = platform.machine()
        system = platform.system().lower()
        model = self._get_cpu_model()

        return CPUInfo(cores=cores, architecture=arch, model=model, platform=system)

    def _get_cpu_model(self) -> str:
        """Get CPU model name."""
        system = platform.system().lower()

        if system == "darwin":
            # macOS - use sysctl
            try:
                result = subprocess.run(
                    ["sysctl", "-n", "machdep.cpu.brand_string"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if result.returncode == 0:
                    return result.stdout.strip()
            except (subprocess.TimeoutExpired, OSError):
                _log(f"[ETLANTIS WARNING] Failed to detect macOS CPU model: {sys.exc_info()[1]}")
                pass
            return "Apple Silicon"

        elif system == "linux":
            # Linux - use lscpu
            try:
                result = subprocess.run(["lscpu"], capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    for line in result.stdout.split("\n"):
                        if line.startswith("Model name:"):
                            return line.split(":", 1)[1].strip()
            except (subprocess.TimeoutExpired, OSError):
                _log(f"[ETLANTIS WARNING] Failed to detect Linux CPU model: {sys.exc_info()[1]}")
                pass
            return "Unknown Linux CPU"

        return "Unknown CPU"

    def _detect_gpu(self) -> GPUInfo:
        """
        Detect GPU devices across vendors.

        Tries in order:
        1. NVIDIA (nvidia-smi)
        2. AMD (rocm-smi)
        3. Intel (xpu-smi, then lspci)
        4. Apple Metal (system_profiler)

        NVIDIA/AMD/Intel discrete cards outrank integrated Intel iGPUs, so
        the Intel branch is tried after AMD. On macOS, Metal is tried last
        because Apple Silicon CPU detection already runs first; Intel Macs
        with discrete AMD chips will hit the AMD branch earlier.

        Returns:
            GPUInfo with availability and device list
        """
        # Try NVIDIA first
        nvidia = self._detect_nvidia_gpu()
        if nvidia.available:
            return nvidia

        # Try AMD ROCm
        amd = self._detect_amd_gpu()
        if amd.available:
            return amd

        # Try Intel (Arc, Data Center Max, iGPU)
        intel = self._detect_intel_gpu()
        if intel.available:
            return intel

        # Try Apple Metal (macOS)
        if self.cpu.platform == "darwin":
            metal = self._detect_metal_gpu()
            if metal.available:
                return metal

        return GPUInfo(available=False, devices=[])

    def _detect_nvidia_gpu(self) -> GPUInfo:
        """
        Detect NVIDIA GPU using nvidia-smi.

        Returns:
            GPUInfo with NVIDIA device(s) or empty if not available
        """
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.total,memory.free,memory.used,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )

            if result.returncode == 0:
                devices = []
                for line in result.stdout.strip().split("\n"):
                    if not line.strip():
                        continue

                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 4:
                        name = parts[0]
                        mem_total = int(parts[1])
                        mem_free = int(parts[2])
                        mem_used = int(parts[3])
                        util = int(parts[4]) if len(parts) > 4 else None

                        devices.append(
                            GPUDevice(
                                name=name,
                                vendor="nvidia",
                                memory_total_mb=mem_total,
                                memory_free_mb=mem_free,
                                memory_used_mb=mem_used,
                                utilization_percent=util,
                            )
                        )

                if devices:
                    return GPUInfo(available=True, devices=devices)

        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        return GPUInfo(available=False, devices=[])

    def _detect_amd_gpu(self) -> GPUInfo:
        """
        Detect AMD GPU using rocm-smi.

        Prefers ``rocm-smi --json`` (ROCm 5.0+). Each top-level key is a card
        ID (e.g. ``"card0"``). Field names differ between minor ROCm versions,
        so the parser is lenient — missing fields yield 0, not failure.

        Returns:
            GPUInfo with AMD device(s) or empty if not available
        """
        try:
            result = subprocess.run(
                [
                    "rocm-smi",
                    "--showproductname",
                    "--showmeminfo",
                    "vram",
                    "--showuse",
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )

            if result.returncode != 0 or not result.stdout.strip():
                return GPUInfo(available=False, devices=[])

            data = json.loads(result.stdout)
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return GPUInfo(available=False, devices=[])
        except OSError:
            return GPUInfo(available=False, devices=[])

        devices: List[GPUDevice] = []
        for card_id, card in data.items():
            if not card_id.startswith("card") or not isinstance(card, dict):
                continue

            name = (
                card.get("Device Name")
                or card.get("Card series")
                or card.get("Card model")
                or f"AMD GPU {card_id}"
            )

            total_b = _parse_int(card.get("VRAM Total Memory (B)"))
            used_b = _parse_int(card.get("VRAM Total Used Memory (B)"))
            total_mb = total_b // (1024 * 1024) if total_b else 0
            used_mb = used_b // (1024 * 1024) if used_b else 0
            free_mb = max(0, total_mb - used_mb)

            util = card.get("GPU use (%)")
            util_pct = _parse_int(util) if util is not None else None

            devices.append(
                GPUDevice(
                    name=str(name).strip(),
                    vendor="amd",
                    memory_total_mb=total_mb,
                    memory_free_mb=free_mb,
                    memory_used_mb=used_mb,
                    utilization_percent=util_pct,
                )
            )

        if devices:
            return GPUInfo(available=True, devices=devices)

        return GPUInfo(available=False, devices=[])

    def _detect_intel_gpu(self) -> GPUInfo:
        """
        Detect Intel GPU using xpu-smi, then fall back to lspci.

        ``xpu-smi discovery -j`` covers Intel Arc and Data Center Max with
        real VRAM readings. If xpu-smi is absent (typical on desktops with
        only an iGPU), ``lspci`` is used for bare detection — memory is
        reported as 0 because iGPU VRAM is shared system RAM and not
        separately addressable.

        Intel iGPU utilization monitoring (``intel_gpu_top -J``) requires
        root and is deliberately skipped; utilization stays ``None`` for
        lspci-detected devices.

        Returns:
            GPUInfo with Intel device(s) or empty if not available
        """
        # xpu-smi first — canonical Intel discrete GPU interface
        try:
            result = subprocess.run(
                ["xpu-smi", "discovery", "-j"], capture_output=True, text=True, timeout=5
            )

            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                devices: List[GPUDevice] = []
                for entry in data.get("device_list", []):
                    if entry.get("device_function_type") not in (None, "physical"):
                        continue  # skip virtual functions

                    name = entry.get("device_name") or "Intel GPU"
                    mem_bytes = _parse_int(entry.get("memory_physical_size_byte"))
                    mem_total_mb = mem_bytes // (1024 * 1024) if mem_bytes else 0

                    devices.append(
                        GPUDevice(
                            name=str(name).strip(),
                            vendor="intel",
                            memory_total_mb=mem_total_mb,
                            # xpu-smi discovery doesn't report free/used;
                            # `xpu-smi stats` does but needs device_id + is rate-limited.
                            memory_free_mb=mem_total_mb,
                            memory_used_mb=0,
                            integrated=(mem_total_mb == 0),
                        )
                    )

                if devices:
                    return GPUInfo(available=True, devices=devices)
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            pass

        # lspci fallback — bare detection, Linux only
        if self.cpu.platform != "linux":
            return GPUInfo(available=False, devices=[])

        try:
            # class 0300 = VGA, 0302 = 3D controller (dGPUs often show as 3D)
            result = subprocess.run(
                ["lspci", "-d", "8086::0300"],  # Intel vendor 8086, VGA class
                capture_output=True,
                text=True,
                timeout=3,
            )
            lines_300 = (
                [l for l in result.stdout.splitlines() if l.strip()]
                if result.returncode == 0
                else []
            )

            result = subprocess.run(
                ["lspci", "-d", "8086::0302"], capture_output=True, text=True, timeout=3
            )
            lines_302 = (
                [l for l in result.stdout.splitlines() if l.strip()]
                if result.returncode == 0
                else []
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return GPUInfo(available=False, devices=[])

        devices = []
        for line in lines_300 + lines_302:
            # Format: "00:02.0 VGA compatible controller: Intel Corporation AlderLake-S GT1 [UHD Graphics 730]"
            parts = line.split(":", 2)
            name = parts[-1].strip() if parts else line.strip()
            # Strip leading "Intel Corporation " if present for cleaner output
            if name.lower().startswith("intel corporation "):
                name = name[len("intel corporation ") :]
            devices.append(
                GPUDevice(
                    name=name or "Intel GPU",
                    vendor="intel",
                    memory_total_mb=0,
                    memory_free_mb=0,
                    memory_used_mb=0,
                    integrated=True,
                )
            )

        if devices:
            return GPUInfo(available=True, devices=devices)

        return GPUInfo(available=False, devices=[])

    def _detect_metal_gpu(self) -> GPUInfo:
        """
        Detect Apple Metal GPU using system_profiler.

        Apple Silicon uses unified memory — system_profiler does not report
        ``spdisplays_vram`` on M-series chips. Those devices are still
        surfaced (with ``memory_total_mb=0`` and ``integrated=True``) so that
        callers can distinguish "no GPU" from "GPU present but shares RAM".
        Intel Macs with discrete GPUs still report concrete VRAM.

        Returns:
            GPUInfo with Metal device or empty if not available
        """
        try:
            result = subprocess.run(
                ["system_profiler", "SPDisplaysDataType", "-json"],
                capture_output=True,
                text=True,
                timeout=5,
            )

            if result.returncode == 0:
                data = json.loads(result.stdout)
                devices = []

                # Parse system_profiler JSON
                for display_item in data.get("SPDisplaysDataType", []):
                    chipset = display_item.get("sppci_model", "Apple GPU")
                    vram_str = display_item.get("spdisplays_vram", "")

                    # Parse VRAM ("32768 MB" / "32 GB"). Apple Silicon omits this field.
                    vram_mb = 0
                    if vram_str:
                        if "GB" in vram_str:
                            try:
                                vram_mb = int(float(vram_str.split()[0]) * 1024)
                            except (ValueError, IndexError):
                                vram_mb = 0
                        elif "MB" in vram_str:
                            try:
                                vram_mb = int(vram_str.split()[0])
                            except (ValueError, IndexError):
                                vram_mb = 0

                    # Unified-memory Apple Silicon: approximate free/used from
                    # total system RAM so can_use_gpu() can still gate correctly.
                    # memory_total_mb stays 0 to flag this is not separately allocated.
                    unified = vram_mb == 0 and self.cpu.architecture == "arm64"
                    free_mb = vram_mb if not unified else int(self.memory.available_gb * 1024)

                    devices.append(
                        GPUDevice(
                            name=chipset,
                            vendor="apple",
                            memory_total_mb=vram_mb,
                            memory_free_mb=free_mb,
                            memory_used_mb=0,  # macOS doesn't report GPU-specific usage
                            integrated=unified,
                        )
                    )

                if devices:
                    return GPUInfo(available=True, devices=devices)

        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass

        return GPUInfo(available=False, devices=[])

    def _detect_memory(self) -> MemoryInfo:
        """
        Detect system memory across platforms.

        Returns:
            MemoryInfo with total, available, used in GB
        """
        system = self.cpu.platform

        if system == "darwin":
            return self._detect_memory_macos()
        elif system == "linux":
            return self._detect_memory_linux()
        else:
            # Fallback - estimate
            return MemoryInfo(total_gb=16.0, available_gb=8.0, used_gb=8.0)

    def _detect_memory_macos(self) -> MemoryInfo:
        """Detect memory on macOS using vm_stat and sysctl."""
        try:
            # Get total memory
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=2
            )
            total_bytes = int(result.stdout.strip())
            total_gb = total_bytes / (1024**3)

            # Get memory usage from vm_stat
            result = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2)

            # Parse vm_stat output
            free_pages = 0
            inactive_pages = 0
            page_size = 4096  # Default page size

            for line in result.stdout.split("\n"):
                if "Pages free" in line:
                    free_pages = int(line.split(":")[1].strip().rstrip("."))
                elif "Pages inactive" in line:
                    inactive_pages = int(line.split(":")[1].strip().rstrip("."))
                elif "page size of" in line:
                    page_size = int(line.split("of")[1].strip().split()[0])

            available_bytes = (free_pages + inactive_pages) * page_size
            available_gb = available_bytes / (1024**3)
            used_gb = total_gb - available_gb

            return MemoryInfo(
                total_gb=round(total_gb, 2),
                available_gb=round(available_gb, 2),
                used_gb=round(used_gb, 2),
            )

        except (subprocess.TimeoutExpired, OSError):
            _log(f"[ETLANTIS WARNING] Failed to detect macOS memory: {sys.exc_info()[1]}")
            return MemoryInfo(total_gb=32.0, available_gb=16.0, used_gb=16.0)

    def _detect_memory_linux(self) -> MemoryInfo:
        """Detect memory on Linux using /proc/meminfo or free."""
        try:
            # Try free command first (more reliable)
            result = subprocess.run(["free", "-b"], capture_output=True, text=True, timeout=2)

            if result.returncode == 0:
                lines = result.stdout.strip().split("\n")
                mem_line = lines[1]  # Second line is memory
                parts = mem_line.split()

                total_bytes = int(parts[1])
                available_bytes = int(parts[6]) if len(parts) > 6 else int(parts[3])

                total_gb = total_bytes / (1024**3)
                available_gb = available_bytes / (1024**3)
                used_gb = total_gb - available_gb

                return MemoryInfo(
                    total_gb=round(total_gb, 2),
                    available_gb=round(available_gb, 2),
                    used_gb=round(used_gb, 2),
                )

        except (subprocess.TimeoutExpired, OSError):
            _log(f"[ETLANTIS WARNING] Failed to run free command: {sys.exc_info()[1]}")
            pass

        # Fallback: read /proc/meminfo
        try:
            with open("/proc/meminfo", "r") as f:
                meminfo = {}
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        key = parts[0].strip()
                        value = int(parts[1].strip().split()[0])  # Remove 'kB'
                        meminfo[key] = value

                total_kb = meminfo.get("MemTotal", 0)
                available_kb = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))

                total_gb = total_kb / (1024**2)
                available_gb = available_kb / (1024**2)
                used_gb = total_gb - available_gb

                return MemoryInfo(
                    total_gb=round(total_gb, 2),
                    available_gb=round(available_gb, 2),
                    used_gb=round(used_gb, 2),
                )

        except (OSError, ValueError):
            _log(
                f"[ETLANTIS WARNING] Failed to detect Linux memory from /proc/meminfo: {sys.exc_info()[1]}"
            )
            return MemoryInfo(total_gb=64.0, available_gb=32.0, used_gb=32.0)

    # -------------------------------------------------------------------------
    # Utility Methods
    # -------------------------------------------------------------------------

    def optimal_workers(self, reserve_cores: int = 2) -> int:
        """
        Calculate optimal worker pool size for multiprocessing.

        Args:
            reserve_cores: Number of cores to reserve for system (default: 2)

        Returns:
            Optimal number of worker processes
        """
        return max(1, self.cpu.cores - reserve_cores)

    def can_use_gpu(self, min_free_memory_gb: float = 5.0) -> bool:
        """
        Check if GPU has enough free memory for acceleration.

        Args:
            min_free_memory_gb: Minimum free VRAM required (default: 5.0 GB)

        Returns:
            True if GPU is available with enough free memory
        """
        if not self.gpu.available:
            return False

        min_free_mb = min_free_memory_gb * 1024

        for device in self.gpu.devices:
            if device.memory_free_mb >= min_free_mb:
                return True

        return False

    def get_gpu_free_memory_gb(self) -> float:
        """
        Get maximum free GPU memory across all devices.

        Returns:
            Free VRAM in GB, or 0.0 if no GPU available
        """
        if not self.gpu.available:
            return 0.0

        max_free_mb = max(device.memory_free_mb for device in self.gpu.devices)
        return max_free_mb / 1024.0

    def to_dict(self) -> Dict:
        """
        Convert hardware profile to dictionary.

        Returns:
            Dictionary representation of hardware profile
        """
        return {
            "cpu": asdict(self.cpu),
            "gpu": {
                "available": self.gpu.available,
                "devices": [asdict(d) for d in self.gpu.devices],
            },
            "memory": asdict(self.memory),
            "optimal_workers": self.optimal_workers(),
            "can_use_gpu": self.can_use_gpu(),
        }

    def __str__(self) -> str:
        """Human-readable hardware summary."""
        lines = [
            "=" * 60,
            "ETLANTIS Hardware Profile",
            "=" * 60,
            f"Platform: {self.cpu.platform}",
            f"CPU: {self.cpu.model}",
            f"  Cores: {self.cpu.cores}",
            f"  Architecture: {self.cpu.architecture}",
            "",
            f"Memory: {self.memory.total_gb:.1f} GB total",
            f"  Available: {self.memory.available_gb:.1f} GB",
            f"  Used: {self.memory.used_gb:.1f} GB",
            "",
        ]

        if self.gpu.available:
            lines.append(f"GPU: {len(self.gpu.devices)} device(s)")
            for i, device in enumerate(self.gpu.devices):
                lines.append(f"  [{i}] {device.name} ({device.vendor})")
                lines.append(
                    f"      VRAM: {device.memory_total_mb / 1024:.1f} GB total, "
                    f"{device.memory_free_mb / 1024:.1f} GB free"
                )
                if device.utilization_percent is not None:
                    lines.append(f"      Utilization: {device.utilization_percent}%")
        else:
            lines.append("GPU: Not available")

        lines.extend(
            [
                "",
                "Recommendations:",
                f"  Optimal Workers: {self.optimal_workers()} processes",
                f"  Can Use GPU: {'Yes' if self.can_use_gpu() else 'No'}",
                "=" * 60,
            ]
        )

        return "\n".join(lines)


def main():
    """Test hardware detection."""
    print("Detecting hardware...")
    hardware = HardwareProfile()
    print(hardware)

    print("\nJSON export:")
    import json

    print(json.dumps(hardware.to_dict(), indent=2))


if __name__ == "__main__":
    main()
