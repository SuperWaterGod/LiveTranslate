from __future__ import annotations

import argparse
import ctypes
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from app_info import (
    APP_DIR,
    APP_NAME,
    LAUNCHER_EXECUTABLE,
    MAIN_EXECUTABLE,
    PENDING_UPDATER,
    RUNTIME_DIR,
    UPDATER_EXECUTABLE,
)
from release_client import (
    copy_tree,
    download_file,
    extract_zip,
    prune_install_dir,
    terminate_processes_by_name,
)


def show_message(title: str, text: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)
    except Exception:
        print(f"{title}: {text}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--base-dir", required=True)
    parser.add_argument("--asset-url", required=True)
    parser.add_argument("--asset-name", required=True)
    parser.add_argument("--target-tag", required=True)
    parser.add_argument("--restart", action="store_true")
    return parser.parse_args()


def install_update(base_dir: Path, asset_url: str, asset_name: str) -> None:
    work_dir = Path(tempfile.mkdtemp(prefix="livetranslate_update_"))
    zip_path = work_dir / asset_name
    extract_dir = work_dir / "extract"

    download_file(asset_url, zip_path)
    extract_zip(zip_path, extract_dir)

    install_root = base_dir
    preserve = {
        "models",
        "logs",
        "launcher_config.json",
        "runtime",
        "setup.log",
        "startup.log",
        "transcripts",
        "user_settings.json",
        UPDATER_EXECUTABLE,
    }

    terminate_processes_by_name(MAIN_EXECUTABLE)
    prune_install_dir(install_root, preserve)
    copy_tree(extract_dir, install_root, skip_names={UPDATER_EXECUTABLE, RUNTIME_DIR})

    new_updater = extract_dir / UPDATER_EXECUTABLE
    if new_updater.exists():
        shutil.copy2(new_updater, install_root / PENDING_UPDATER)

    new_runtime = extract_dir / RUNTIME_DIR
    current_runtime = install_root / RUNTIME_DIR
    if new_runtime.exists() and not current_runtime.exists():
        shutil.copytree(new_runtime, current_runtime)

    shutil.rmtree(work_dir, ignore_errors=True)


def restart_launcher(base_dir: Path) -> None:
    launcher = base_dir / LAUNCHER_EXECUTABLE
    if not launcher.exists():
        return
    subprocess.Popen(
        [str(launcher)],
        cwd=str(base_dir),
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )


def main() -> int:
    args = parse_args()
    base_dir = Path(args.base_dir).resolve()

    try:
        install_update(base_dir, args.asset_url, args.asset_name)
    except Exception as exc:
        show_message(APP_NAME, f"Update failed: {exc}")
        return 1

    if args.restart:
        restart_launcher(base_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
