from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

from app_info import (
    APP_DIR,
    APP_NAME,
    APP_VERSION,
    GITHUB_REPOSITORY,
    LAUNCHER_EXECUTABLE,
    MAIN_EXECUTABLE,
    MAIN_SCRIPT,
    PENDING_UPDATER,
    RUNTIME_DIR,
    UPDATER_EXECUTABLE,
    WINDOWS_ASSET_TOKEN,
)
from release_client import download_file, get_latest_release, same_version, select_windows_asset


def is_windows() -> bool:
    return sys.platform.startswith("win")


def exe_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def show_message(title: str, text: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)
    except Exception:
        print(f"{title}: {text}", file=sys.stderr)


def ask_yes_no_cancel(title: str, text: str) -> int:
    try:
        return ctypes.windll.user32.MessageBoxW(0, text, title, 0x23)
    except Exception:
        print(f"{title}: {text}", file=sys.stderr)
        return 6


def run_checked(args: list[str], cwd: Path, log_file: Path) -> None:
    with log_file.open("a", encoding="utf-8") as log:
        log.write(f"\n> {' '.join(args)}\n")
        log.flush()
        proc = subprocess.run(args, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {proc.returncode}: {' '.join(args)}")


def launch_python(python_exe: Path, script: Path, base_dir: Path) -> int:
    if not python_exe.exists():
        show_message(APP_NAME, f"Missing runtime executable: {python_exe}")
        return 1
    if not script.exists():
        show_message(APP_NAME, f"Missing application entry: {script}")
        return 1
    env = os.environ.copy()
    env["LIVETRANSLATE_HOME"] = str(base_dir)
    proc = subprocess.Popen([str(python_exe), str(script)], cwd=str(script.parent), env=env)
    return proc.wait()


def apply_pending_updater(base_dir: Path) -> None:
    pending = base_dir / PENDING_UPDATER
    updater = base_dir / UPDATER_EXECUTABLE
    if not pending.exists():
        return
    try:
        pending.replace(updater)
    except OSError:
        pending.unlink(missing_ok=True)


def start_updater(base_dir: Path, asset_url: str, asset_name: str, tag: str) -> int:
    updater = base_dir / UPDATER_EXECUTABLE
    if not updater.exists():
        show_message(APP_NAME, f"Update required, but {UPDATER_EXECUTABLE} is missing.")
        return start_application(base_dir)

    subprocess.Popen(
        [
            str(updater),
            "--base-dir",
            str(base_dir),
            "--asset-url",
            asset_url,
            "--asset-name",
            asset_name,
            "--target-tag",
            tag,
            "--restart",
        ],
        cwd=str(base_dir),
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    return 0


def enable_embedded_site(runtime_dir: Path) -> None:
    for pth_file in runtime_dir.glob("python*._pth"):
        text = pth_file.read_text(encoding="utf-8")
        lines = text.splitlines()
        if "Lib/site-packages" not in lines and "Lib\\site-packages" not in lines:
            lines.append("Lib/site-packages")
        text = "\n".join(lines) + "\n"
        text = text.replace("#import site", "import site")
        if "import site" not in text:
            text = text.rstrip() + "\nimport site\n"
        pth_file.write_text(text, encoding="utf-8")


def python_has_module(python_exe: Path, module: str) -> bool:
    result = subprocess.run(
        [str(python_exe), "-c", f"import {module}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def detect_torch_index() -> str:
    override = os.getenv("LIVETRANSLATE_TORCH_INDEX")
    if override:
        return override

    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name",
                "--format=csv,noheader",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except Exception:
        result = None

    if not result or result.returncode != 0 or not result.stdout.strip():
        return "https://download.pytorch.org/whl/cpu"

    gpu_names = result.stdout.lower()
    if "rtx 50" in gpu_names or "5090" in gpu_names or "5080" in gpu_names or "5070" in gpu_names or "5060" in gpu_names:
        return "https://download.pytorch.org/whl/cu128"
    return "https://download.pytorch.org/whl/cu126"


def load_launcher_config(base_dir: Path) -> dict:
    path = base_dir / "launcher_config.json"
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_launcher_config(base_dir: Path, config: dict) -> None:
    path = base_dir / "launcher_config.json"
    try:
        path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def select_pip_index(base_dir: Path) -> str | None:
    override = os.getenv("LIVETRANSLATE_PIP_INDEX_URL")
    if override:
        return override

    config = load_launcher_config(base_dir)
    if "pip_index_url" in config:
        value = config["pip_index_url"]
        return value or None

    choice = ask_yes_no_cancel(
        APP_NAME,
        "Select Python package source for first-time dependency installation:\n\n"
        "Yes: Default PyPI\n"
        "No: Tsinghua mirror\n"
        "Cancel: Aliyun mirror",
    )

    if choice == 7:
        value = "https://pypi.tuna.tsinghua.edu.cn/simple"
    elif choice == 2:
        value = "https://mirrors.aliyun.com/pypi/simple"
    else:
        value = ""

    config["pip_index_url"] = value
    save_launcher_config(base_dir, config)
    return value or None


def with_pip_index(args: list[str], index_url: str | None) -> list[str]:
    if index_url:
        return args + ["-i", index_url, "--trusted-host", index_url.split("//", 1)[-1].split("/", 1)[0]]
    return args


def ensure_pip(python_exe: Path, runtime_dir: Path, log_file: Path) -> None:
    if python_has_module(python_exe, "pip"):
        return
    get_pip = runtime_dir / "get-pip.py"
    if not get_pip.exists():
        download_file("https://bootstrap.pypa.io/get-pip.py", get_pip, timeout=60)
    run_checked([str(python_exe), str(get_pip), "--no-warn-script-location"], runtime_dir, log_file)


def ensure_dependencies(base_dir: Path) -> None:
    runtime_dir = base_dir / RUNTIME_DIR
    app_dir = base_dir / APP_DIR
    python_exe = runtime_dir / MAIN_EXECUTABLE
    requirements = app_dir / "requirements.txt"
    stamp = runtime_dir / ".livetranslate_deps_ready"
    log_file = base_dir / "setup.log"

    if stamp.exists() and python_has_module(python_exe, "torch") and python_has_module(python_exe, "PyQt6"):
        return

    if not python_exe.exists():
        raise RuntimeError(f"Missing embedded Python runtime: {python_exe}")
    if not requirements.exists():
        raise RuntimeError(f"Missing requirements file: {requirements}")

    show_message(
        APP_NAME,
        "First startup will install Python dependencies. This may take several minutes.",
    )

    runtime_dir.mkdir(parents=True, exist_ok=True)
    enable_embedded_site(runtime_dir)
    ensure_pip(python_exe, runtime_dir, log_file)
    pip_index = select_pip_index(base_dir)

    run_checked(
        with_pip_index([str(python_exe), "-m", "pip", "install", "--upgrade", "pip"], pip_index),
        runtime_dir,
        log_file,
    )
    run_checked(
        [
            str(python_exe),
            "-m",
            "pip",
            "install",
            "torch",
            "torchaudio",
            "--index-url",
            detect_torch_index(),
        ],
        runtime_dir,
        log_file,
    )
    run_checked(
        with_pip_index([str(python_exe), "-m", "pip", "install", "-r", str(requirements)], pip_index),
        runtime_dir,
        log_file,
    )
    run_checked(
        with_pip_index([str(python_exe), "-m", "pip", "install", "funasr", "--no-deps"], pip_index),
        runtime_dir,
        log_file,
    )
    stamp.write_text(APP_VERSION, encoding="utf-8")


def start_application(base_dir: Path) -> int:
    try:
        ensure_dependencies(base_dir)
    except Exception as exc:
        show_message(APP_NAME, f"Dependency setup failed: {exc}\n\nSee setup.log for details.")
        return 1

    return launch_python(base_dir / RUNTIME_DIR / MAIN_EXECUTABLE, base_dir / APP_DIR / MAIN_SCRIPT, base_dir)


def main() -> int:
    if not is_windows():
        show_message(APP_NAME, "This build only supports Windows.")
        return 1

    base_dir = exe_dir()
    apply_pending_updater(base_dir)

    try:
        release = get_latest_release(GITHUB_REPOSITORY)
        latest_tag = str(release.get("tag_name") or "")
        asset = select_windows_asset(release, WINDOWS_ASSET_TOKEN)
    except Exception:
        asset = None
        latest_tag = ""

    if asset and latest_tag and not same_version(APP_VERSION, latest_tag):
        return start_updater(base_dir, asset.url, asset.name, latest_tag)

    return start_application(base_dir)


if __name__ == "__main__":
    raise SystemExit(main())
