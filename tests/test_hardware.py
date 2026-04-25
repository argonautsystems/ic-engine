#!/usr/bin/env python3
# Copyright 2026 InvestorClaw Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for setup.hardware — GPU detection parsers.

Uses unittest.mock.patch on subprocess.run so tests are hermetic and run
on any host regardless of installed hardware.

Vendored from ETLANTIS tests/test_hardware.py — keep in sync.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from setup.hardware import (
    GPUDevice,
    GPUInfo,
    HardwareProfile,
    _parse_int,
)


class ParseIntTests(unittest.TestCase):
    """_parse_int must tolerate rocm-smi / xpu-smi field shapes."""

    def test_none_returns_zero(self):
        self.assertEqual(_parse_int(None), 0)

    def test_empty_string_returns_zero(self):
        self.assertEqual(_parse_int(""), 0)

    def test_plain_int_string(self):
        self.assertEqual(_parse_int("1234"), 1234)

    def test_trailing_unit_stripped(self):
        self.assertEqual(_parse_int("45 %"), 45)
        self.assertEqual(_parse_int("8192 MB"), 8192)

    def test_float_truncated(self):
        self.assertEqual(_parse_int(99.9), 99)

    def test_unparseable_returns_zero(self):
        self.assertEqual(_parse_int("not-a-number"), 0)

    def test_negative_preserved(self):
        self.assertEqual(_parse_int("-5"), -5)


def _mock_run(returncode=0, stdout="", stderr=""):
    """Build a mock subprocess.run return value."""
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


class AMDDetectionTests(unittest.TestCase):
    """rocm-smi JSON parser."""

    ROCM_JSON = json.dumps(
        {
            "card0": {
                "GPU ID": "0x7408",
                "Device Name": "Instinct MI300X",
                "VRAM Total Memory (B)": "206158430208",
                "VRAM Total Used Memory (B)": "17179869184",
                "GPU use (%)": "12",
            },
            "system": {"driver_version": "6.1.0"},  # non-card key must be ignored
        }
    )

    def test_amd_json_parsed(self):
        profile = HardwareProfile.__new__(HardwareProfile)
        profile.cpu = MagicMock(platform="linux", architecture="x86_64")
        with patch("subprocess.run", return_value=_mock_run(0, self.ROCM_JSON)):
            gpu = profile._detect_amd_gpu()
        self.assertTrue(gpu.available)
        self.assertEqual(len(gpu.devices), 1)
        d = gpu.devices[0]
        self.assertEqual(d.vendor, "amd")
        self.assertEqual(d.name, "Instinct MI300X")
        self.assertEqual(d.memory_total_mb, 196608)  # 192 GiB → 196608 MiB
        self.assertEqual(d.memory_used_mb, 16384)
        self.assertEqual(d.memory_free_mb, 196608 - 16384)
        self.assertEqual(d.utilization_percent, 12)

    def test_amd_missing_rocm_smi(self):
        profile = HardwareProfile.__new__(HardwareProfile)
        profile.cpu = MagicMock(platform="linux", architecture="x86_64")
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            gpu = profile._detect_amd_gpu()
        self.assertFalse(gpu.available)
        self.assertEqual(gpu.devices, [])

    def test_amd_invalid_json_returns_empty(self):
        profile = HardwareProfile.__new__(HardwareProfile)
        profile.cpu = MagicMock(platform="linux", architecture="x86_64")
        with patch("subprocess.run", return_value=_mock_run(0, "not json")):
            gpu = profile._detect_amd_gpu()
        self.assertFalse(gpu.available)


class IntelDetectionTests(unittest.TestCase):
    """xpu-smi JSON, then lspci fallback."""

    XPU_JSON = json.dumps(
        {
            "device_list": [
                {
                    "device_function_type": "physical",
                    "device_id": 0,
                    "device_name": "Intel(R) Arc(TM) A770 Graphics",
                    "memory_physical_size_byte": 17179869184,
                },
                {
                    "device_function_type": "virtual",  # must be skipped
                    "device_id": 1,
                    "device_name": "Intel VF",
                    "memory_physical_size_byte": 0,
                },
            ]
        }
    )

    def test_xpu_smi_discrete(self):
        profile = HardwareProfile.__new__(HardwareProfile)
        profile.cpu = MagicMock(platform="linux", architecture="x86_64")
        with patch("subprocess.run", return_value=_mock_run(0, self.XPU_JSON)):
            gpu = profile._detect_intel_gpu()
        self.assertTrue(gpu.available)
        self.assertEqual(len(gpu.devices), 1)
        d = gpu.devices[0]
        self.assertEqual(d.vendor, "intel")
        self.assertEqual(d.name, "Intel(R) Arc(TM) A770 Graphics")
        self.assertEqual(d.memory_total_mb, 16384)
        self.assertFalse(d.integrated)

    def test_lspci_igpu_fallback(self):
        """When xpu-smi is absent, lspci detection populates integrated=True."""
        profile = HardwareProfile.__new__(HardwareProfile)
        profile.cpu = MagicMock(platform="linux", architecture="x86_64")
        lspci_line = "00:02.0 VGA compatible controller: Intel Corporation AlderLake-S GT1 [UHD Graphics 730]"

        call_count = {"n": 0}

        def side_effect(cmd, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # xpu-smi — raise FileNotFoundError to trigger fallback
                raise FileNotFoundError()
            if call_count["n"] == 2:
                # lspci -d 8086::0300 → returns iGPU
                return _mock_run(0, lspci_line + "\n")
            # lspci -d 8086::0302 → empty
            return _mock_run(0, "")

        with patch("subprocess.run", side_effect=side_effect):
            gpu = profile._detect_intel_gpu()

        self.assertTrue(gpu.available)
        self.assertEqual(len(gpu.devices), 1)
        d = gpu.devices[0]
        self.assertEqual(d.vendor, "intel")
        self.assertTrue(d.integrated)
        self.assertEqual(d.memory_total_mb, 0)
        self.assertIn("UHD Graphics 730", d.name)
        self.assertFalse(d.name.lower().startswith("intel corporation"))

    def test_lspci_skipped_on_non_linux(self):
        profile = HardwareProfile.__new__(HardwareProfile)
        profile.cpu = MagicMock(platform="darwin", architecture="arm64")
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            gpu = profile._detect_intel_gpu()
        self.assertFalse(gpu.available)


class MetalDetectionTests(unittest.TestCase):
    """Apple Silicon unified-memory path."""

    APPLE_SILICON_JSON = json.dumps(
        {
            "SPDisplaysDataType": [
                {
                    "sppci_model": "Apple M1 Max",
                    # spdisplays_vram deliberately absent — this is the real-world
                    # Apple Silicon response
                }
            ]
        }
    )

    def test_apple_silicon_surfaces_with_unified_memory(self):
        profile = HardwareProfile.__new__(HardwareProfile)
        profile.cpu = MagicMock(platform="darwin", architecture="arm64")
        profile.memory = MagicMock(available_gb=16.0)
        with patch("subprocess.run", return_value=_mock_run(0, self.APPLE_SILICON_JSON)):
            gpu = profile._detect_metal_gpu()
        self.assertTrue(gpu.available)
        d = gpu.devices[0]
        self.assertEqual(d.vendor, "apple")
        self.assertEqual(d.memory_total_mb, 0)
        self.assertTrue(d.integrated)
        self.assertEqual(d.memory_free_mb, 16384)  # 16 GB * 1024


class DetectGPUPriorityTests(unittest.TestCase):
    """_detect_gpu must try vendors in order and short-circuit on first hit."""

    def test_nvidia_wins_over_amd_intel(self):
        profile = HardwareProfile.__new__(HardwareProfile)
        profile.cpu = MagicMock(platform="linux", architecture="x86_64")

        nvidia_hit = GPUInfo(
            available=True,
            devices=[
                GPUDevice(
                    name="RTX 5060",
                    vendor="nvidia",
                    memory_total_mb=8151,
                    memory_free_mb=2560,
                    memory_used_mb=5591,
                )
            ],
        )
        amd_called = intel_called = metal_called = False

        def _amd_spy():
            nonlocal amd_called
            amd_called = True
            return GPUInfo(available=False, devices=[])

        def _intel_spy():
            nonlocal intel_called
            intel_called = True
            return GPUInfo(available=False, devices=[])

        def _metal_spy():
            nonlocal metal_called
            metal_called = True
            return GPUInfo(available=False, devices=[])

        with (
            patch.object(profile, "_detect_nvidia_gpu", return_value=nvidia_hit),
            patch.object(profile, "_detect_amd_gpu", side_effect=_amd_spy),
            patch.object(profile, "_detect_intel_gpu", side_effect=_intel_spy),
            patch.object(profile, "_detect_metal_gpu", side_effect=_metal_spy),
        ):
            gpu = profile._detect_gpu()

        self.assertTrue(gpu.available)
        self.assertEqual(gpu.devices[0].vendor, "nvidia")
        self.assertFalse(amd_called)
        self.assertFalse(intel_called)
        self.assertFalse(metal_called)


if __name__ == "__main__":
    unittest.main()
