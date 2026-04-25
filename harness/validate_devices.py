#!/usr/bin/env python3
"""
Device Matrix Validation — Phase 3c

Validates network connectivity and SSH access to all devices:
- STUDIO (local)
- clawpi (Raspberry Pi 8GB)
- zeropi (Raspberry Pi 2GB)

Usage:
  python validate_devices.py
  python validate_devices.py --device clawpi
  python validate_devices.py --ssh-test
"""

import asyncio
import json
import logging
import socket
import subprocess
import sys
import time
from typing import Any, Dict, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

from harness.device_matrix import DEVICE_MATRIX, get_device


def check_ping(host: str, timeout: int = 2) -> bool:
    """Check if host is reachable via ping."""
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout * 1000), host],
            capture_output=True,
            timeout=timeout + 1,
        )
        return result.returncode == 0
    except Exception as e:
        logger.debug(f"Ping failed for {host}: {e}")
        return False


def check_ssh(host: str, port: int = 22, timeout: int = 3) -> bool:
    """Check if SSH port is open."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception as e:
        logger.debug(f"SSH check failed for {host}: {e}")
        return False


def test_ssh_command(host: str, port: int = 22, timeout: int = 5) -> Optional[str]:
    """Test SSH by running a simple command."""
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=3", "-p", str(port), f"jasonperlow@{host}", "uname -a"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except subprocess.TimeoutExpired:
        logger.warning(f"SSH timeout for {host}")
        return None
    except Exception as e:
        logger.debug(f"SSH command failed for {host}: {e}")
        return None


def get_device_info(host: str, port: int = 22) -> Optional[Dict[str, Any]]:
    """Get detailed device information via SSH."""
    try:
        # Get system info
        cmd = "echo 'hostname:'; hostname; echo 'uname:'; uname -a; echo 'memory:'; free -h | grep Mem"
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=3", "-p", str(port), f"jasonperlow@{host}", cmd],
            capture_output=True,
            text=True,
            timeout=8,
        )

        if result.returncode == 0:
            result.stdout.strip().split("\n")
            return {"host": host, "ssh_working": True, "info": result.stdout.strip()}
        return None
    except Exception as e:
        logger.debug(f"Device info failed for {host}: {e}")
        return None


async def validate_device(device_name: str) -> Dict[str, Any]:
    """Validate a single device."""
    try:
        device_config = get_device(device_name)
    except ValueError as e:
        return {"device": device_name, "status": "invalid", "error": str(e)}

    result = {
        "device": device_name,
        "host": device_config.host,
        "memory_mb": device_config.memory_mb,
        "timeout_multiplier": device_config.timeout_multiplier,
        "description": device_config.description,
        "connectivity": {"ping": False, "ssh_port": False, "ssh_command": False},
        "system_info": None,
        "overall_status": "unknown",
    }

    logger.info(f"\n{'=' * 60}")
    logger.info(f"Validating: {device_name}")
    logger.info(f"{'=' * 60}")
    logger.info(f"Host: {device_config.host}")
    logger.info(f"Memory: {device_config.memory_mb}MB")
    logger.info(f"Description: {device_config.description}")

    # For STUDIO (local), skip network tests
    if device_name == "STUDIO":
        logger.info("✓ STUDIO is local, skipping network tests")
        result["connectivity"]["ssh_command"] = True
        result["overall_status"] = "ready"
        return result

    # Test ping
    logger.info(f"\n→ Testing ping to {device_config.host}...")
    ping_ok = check_ping(device_config.host)
    result["connectivity"]["ping"] = ping_ok
    if ping_ok:
        logger.info("  ✓ Ping successful")
    else:
        logger.warning("  ✗ Ping failed (device may be offline)")
        result["overall_status"] = "offline"
        return result

    # Test SSH port
    logger.info(f"\n→ Testing SSH port {device_config.ssh_port}...")
    ssh_port_ok = check_ssh(device_config.host, device_config.ssh_port)
    result["connectivity"]["ssh_port"] = ssh_port_ok
    if ssh_port_ok:
        logger.info("  ✓ SSH port open")
    else:
        logger.warning("  ✗ SSH port closed (firewall or service not running)")
        result["overall_status"] = "ssh_blocked"
        return result

    # Test SSH command
    logger.info("\n→ Testing SSH command execution...")
    ssh_result = test_ssh_command(device_config.host, device_config.ssh_port)
    if ssh_result:
        result["connectivity"]["ssh_command"] = True
        logger.info("  ✓ SSH working")
        logger.info(f"  System: {ssh_result[:80]}...")
        result["overall_status"] = "ready"

        # Get detailed info
        logger.info("\n→ Fetching device information...")
        info = get_device_info(device_config.host, device_config.ssh_port)
        if info:
            result["system_info"] = info["info"]
            logger.info("  ✓ System info retrieved")
    else:
        logger.warning("  ✗ SSH command failed (authentication or permission issue)")
        result["overall_status"] = "ssh_failed"

    return result


async def validate_all_devices() -> Dict[str, Any]:
    """Validate all devices in parallel."""
    results = {"timestamp": time.time(), "devices": {}}

    tasks = [validate_device(device_name) for device_name in DEVICE_MATRIX.keys()]

    device_results = await asyncio.gather(*tasks)

    for device_result in device_results:
        device_name = device_result["device"]
        results["devices"][device_name] = device_result

    # Summary
    logger.info(f"\n{'=' * 60}")
    logger.info("VALIDATION SUMMARY")
    logger.info(f"{'=' * 60}")

    for device_name, device_result in results["devices"].items():
        status = device_result.get("overall_status", "unknown")
        icon = "✓" if status == "ready" else "✗" if status == "offline" else "⚠"
        logger.info(f"{icon} {device_name}: {status}")

    # Overall readiness
    ready_count = sum(1 for d in results["devices"].values() if d.get("overall_status") == "ready")
    total_count = len(results["devices"])

    results["summary"] = {
        "ready": ready_count,
        "total": total_count,
        "test_matrix_ready": ready_count >= 1,  # At least STUDIO
    }

    logger.info(f"\nTest Matrix Ready: {ready_count}/{total_count} devices")
    if results["summary"]["test_matrix_ready"]:
        logger.info("✓ Can proceed with Phase 3c testing")
    else:
        logger.warning("✗ Additional device setup required")

    logger.info(f"{'=' * 60}\n")

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Phase 3c: Device Matrix Validation")
    parser.add_argument("--device", default=None, help="Validate specific device")
    parser.add_argument("--ssh-test", action="store_true", help="Test SSH access only")

    args = parser.parse_args()

    try:
        if args.device:
            result = asyncio.run(validate_device(args.device))
        else:
            result = asyncio.run(validate_all_devices())

        # Save results
        results_file = "device_validation_results.json"
        with open(results_file, "w") as f:
            json.dump(result, f, indent=2, default=str)
        logger.info(f"Results saved to {results_file}")

        sys.exit(0)

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
