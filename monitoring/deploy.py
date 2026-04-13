#!/usr/bin/env python3
"""
IMAGIAX — Monitoring Deployment Script
Transfers files from Windows to server using base64-pipe over SSH.
Avoids CRLF corruption and shell variable expansion issues.

Usage:
    python monitoring/deploy.py

Prerequisites:
    - SSH key at ~/.ssh/do_imagiax
    - Server accessible at root@imagiax.dev
"""

import base64
import os
import subprocess
import sys

SSH_KEY = os.path.expanduser("~/.ssh/do_imagiax")
SSH_TARGET = "root@imagiax.dev"
SSH_BASE = ["ssh", "-i", SSH_KEY, SSH_TARGET]

# Working directory = repo root
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

# Files to deploy: (local_path, remote_path, mode)
MONITORING_FILES = [
    (
        os.path.join(SCRIPT_DIR, "health_check.sh"),
        "/root/monitoring/health_check.sh",
        "750",
    ),
    (
        os.path.join(SCRIPT_DIR, ".telegram_config"),
        "/root/monitoring/.telegram_config",
        "600",
    ),
    (
        os.path.join(SCRIPT_DIR, "install.sh"),
        "/root/monitoring/install.sh",
        "750",
    ),
    (
        os.path.join(SCRIPT_DIR, "MONITORING.md"),
        "/root/monitoring/MONITORING.md",
        "644",
    ),
]

COMPOSE_LOCAL = os.path.join(
    REPO_ROOT,
    "OHIF Medical Imaging Viewer",
    "orthanc-imagiax",
    "docker-compose.prod.yml",
)
COMPOSE_REMOTE = "/root/imagiax/OHIF Medical Imaging Viewer/orthanc-imagiax/docker-compose.prod.yml"


def run_ssh(command, description=""):
    """Run a command on the remote server via SSH."""
    print(f"  → {description or command[:60]}")
    result = subprocess.run(
        SSH_BASE + [command],
        capture_output=True,
        text=True,
    )
    if result.stdout:
        print(f"    {result.stdout.strip()}")
    if result.stderr:
        print(f"    STDERR: {result.stderr.strip()}")
    if result.returncode != 0:
        print(f"  ✗ FAILED (exit {result.returncode})")
        return False
    print(f"  ✓ OK")
    return True


def deploy_file_base64(local_path, remote_path, mode):
    """
    Deploy a file via base64-pipe over SSH.
    Avoids CRLF corruption (Windows line endings) and shell variable expansion.
    """
    print(f"\n  Deploying: {os.path.basename(local_path)} → {remote_path}")

    # Read and encode locally
    with open(local_path, "rb") as f:
        data = f.read()

    # Verify LF line endings for shell scripts
    if local_path.endswith(".sh") and b"\r\n" in data:
        print(f"  WARNING: {local_path} has CRLF endings — converting to LF")
        data = data.replace(b"\r\n", b"\n")

    encoded = base64.b64encode(data).decode("ascii")

    # Use python3 on server side — handles paths with spaces safely (no shell splitting issues)
    py_cmd = (
        f"python3 -c \""
        f"import base64; "
        f"open('{remote_path}', 'wb').write(base64.b64decode('{encoded}'))"
        f"\" && chmod {mode} '{remote_path}'"
    )
    result = subprocess.run(
        SSH_BASE + [py_cmd],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"  ✗ FAILED: {result.stderr.strip()}")
        return False

    # Verify remote file size using python3 to handle paths with spaces
    verify = subprocess.run(
        SSH_BASE + [f"python3 -c \"import os; print(os.path.getsize('{remote_path}'))\""],
        capture_output=True,
        text=True,
    )
    remote_size = int(verify.stdout.strip()) if verify.stdout.strip().isdigit() else -1
    local_size = len(data)

    if remote_size == local_size:
        print(f"  ✓ OK ({local_size} bytes, chmod {mode})")
        return True
    else:
        print(f"  ✗ Size mismatch: local={local_size}, remote={remote_size}")
        return False


def main():
    print("=" * 60)
    print("IMAGIAX — Monitoring Deployment")
    print("=" * 60)

    # Step 1: Create remote directories
    print("\n[1/4] Creating remote directories...")
    if not run_ssh(
        "mkdir -p /root/monitoring/state /root/backups/logs",
        "mkdir -p /root/monitoring/state /root/backups/logs",
    ):
        sys.exit(1)

    # Step 2: Deploy monitoring files
    print("\n[2/4] Deploying monitoring files...")
    for local_path, remote_path, mode in MONITORING_FILES:
        if not os.path.exists(local_path):
            print(f"  ✗ MISSING: {local_path}")
            sys.exit(1)
        if not deploy_file_base64(local_path, remote_path, mode):
            sys.exit(1)

    # Step 3: Deploy docker-compose.prod.yml
    print("\n[3/4] Deploying docker-compose.prod.yml...")
    if not os.path.exists(COMPOSE_LOCAL):
        print(f"  ✗ MISSING: {COMPOSE_LOCAL}")
        sys.exit(1)
    if not deploy_file_base64(COMPOSE_LOCAL, COMPOSE_REMOTE, "644"):
        sys.exit(1)

    # Step 4: Run install.sh on server
    print("\n[4/4] Running install.sh on server...")
    if not run_ssh(
        "bash /root/monitoring/install.sh",
        "bash /root/monitoring/install.sh",
    ):
        sys.exit(1)

    print("\n" + "=" * 60)
    print("✓ Deployment complete!")
    print("=" * 60)
    print("\nNext steps:")
    print("  1. Set Telegram credentials:")
    print("     ssh -i ~/.ssh/do_imagiax root@imagiax.dev")
    print("     nano /root/monitoring/.telegram_config")
    print("  2. Apply compose changes:")
    print("     cd '/root/imagiax/OHIF Medical Imaging Viewer/orthanc-imagiax'")
    print("     docker compose -f docker-compose.prod.yml up -d")
    print("  3. Test monitoring:")
    print("     /root/monitoring/health_check.sh")
    print("  4. Watch log:")
    print("     tail -f /root/backups/logs/health.log")
    print()


if __name__ == "__main__":
    main()
