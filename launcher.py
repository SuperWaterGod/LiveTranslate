from __future__ import annotations

import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

from app_info import (
    APP_DIR,
    APP_NAME,
    APP_VERSION,
    GITHUB_REPOSITORY,
    LAUNCHER_EXECUTABLE,
    MAIN_EXECUTABLE,
    MAIN_GUI_EXECUTABLE,
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


def run_checked(args: list[str], cwd: Path, log_file: Path) -> None:
    with log_file.open("a", encoding="utf-8") as log:
        log.write(f"\n> {' '.join(args)}\n")
        log.flush()
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
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
    proc = subprocess.Popen(
        [str(python_exe), str(script)],
        cwd=str(script.parent),
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
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


def detect_gpu_info() -> dict:
    info = {"has_nvidia": False, "name": "", "compute_cap": "", "recommended": "cpu"}
    try:
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        if gpu.returncode == 0 and gpu.stdout.strip():
            info["has_nvidia"] = True
            info["name"] = gpu.stdout.strip().splitlines()[0]
    except Exception:
        return info

    try:
        cc = subprocess.run(
            ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        if cc.returncode == 0 and cc.stdout.strip():
            info["compute_cap"] = cc.stdout.strip().splitlines()[0]
            try:
                info["recommended"] = "cu128" if float(info["compute_cap"]) >= 12.0 else "cu126"
            except ValueError:
                info["recommended"] = "cu126"
        elif info["has_nvidia"]:
            info["recommended"] = "cu126"
    except Exception:
        if info["has_nvidia"]:
            info["recommended"] = "cu126"
    return info


def torch_index_from_choice(choice: str) -> str:
    if choice == "cu128":
        return "https://download.pytorch.org/whl/cu128"
    if choice == "cu126":
        return "https://download.pytorch.org/whl/cu126"
    return "https://download.pytorch.org/whl/cpu"


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


def get_install_config(base_dir: Path, selected: dict | None = None) -> dict:
    config = load_launcher_config(base_dir)
    if selected:
        config.update(selected)
        save_launcher_config(base_dir, config)

    pip_index = os.getenv("LIVETRANSLATE_PIP_INDEX_URL")
    if not pip_index:
        pip_index = config.get("pip_index_url", "")

    torch_index = os.getenv("LIVETRANSLATE_TORCH_INDEX")
    if not torch_index:
        torch_choice = config.get("torch_choice", "cpu")
        torch_index = torch_index_from_choice(torch_choice)

    return {
        "pip_index_url": pip_index or None,
        "torch_index_url": torch_index,
    }


def with_pip_index(args: list[str], index_url: str | None) -> list[str]:
    if index_url:
        return args + ["-i", index_url, "--trusted-host", index_url.split("//", 1)[-1].split("/", 1)[0]]
    return args


def _gui_python(base_dir: Path) -> Path:
    runtime_dir = base_dir / RUNTIME_DIR
    gui_python = runtime_dir / MAIN_GUI_EXECUTABLE
    if gui_python.exists():
        return gui_python
    return runtime_dir / MAIN_EXECUTABLE


def ensure_pip(python_exe: Path, runtime_dir: Path, log_file: Path) -> None:
    if python_has_module(python_exe, "pip"):
        return
    get_pip = runtime_dir / "get-pip.py"
    if not get_pip.exists():
        download_file("https://bootstrap.pypa.io/get-pip.py", get_pip, timeout=60)
    run_checked([str(python_exe), str(get_pip), "--no-warn-script-location"], runtime_dir, log_file)


def ensure_dependencies(base_dir: Path, install_config: dict | None = None, status_cb=None) -> None:
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

    if status_cb:
        status_cb("First startup will install Python dependencies. This may take several minutes.")
    else:
        show_message(
            APP_NAME,
            "First startup will install Python dependencies. This may take several minutes.",
        )

    runtime_dir.mkdir(parents=True, exist_ok=True)
    enable_embedded_site(runtime_dir)
    if status_cb:
        status_cb("Checking pip...")
    ensure_pip(python_exe, runtime_dir, log_file)
    install_config = install_config or get_install_config(base_dir)
    pip_index = install_config.get("pip_index_url")
    torch_index = install_config.get("torch_index_url") or detect_torch_index()

    if status_cb:
        status_cb("Upgrading pip...")
    run_checked(
        with_pip_index([str(python_exe), "-m", "pip", "install", "--upgrade", "pip"], pip_index),
        runtime_dir,
        log_file,
    )
    if status_cb:
        status_cb("Installing torch...")
    run_checked(
        [
            str(python_exe),
            "-m",
            "pip",
            "install",
            "torch",
            "torchaudio",
            "--index-url",
            torch_index,
        ],
        runtime_dir,
        log_file,
    )
    if status_cb:
        status_cb("Installing app dependencies...")
    run_checked(
        with_pip_index([str(python_exe), "-m", "pip", "install", "-r", str(requirements)], pip_index),
        runtime_dir,
        log_file,
    )
    if status_cb:
        status_cb("Installing FunASR...")
    run_checked(
        with_pip_index([str(python_exe), "-m", "pip", "install", "funasr", "--no-deps"], pip_index),
        runtime_dir,
        log_file,
    )
    stamp.write_text(APP_VERSION, encoding="utf-8")


def start_application(base_dir: Path, install_config: dict | None = None, status_cb=None) -> int:
    try:
        ensure_dependencies(base_dir, install_config=install_config, status_cb=status_cb)
    except Exception as exc:
        show_message(APP_NAME, f"Dependency setup failed: {exc}\n\nSee setup.log for details.")
        return 1

    return launch_python(_gui_python(base_dir), base_dir / APP_DIR / MAIN_SCRIPT, base_dir)


def show_progress_window(base_dir: Path) -> int:
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        return start_application(base_dir)

    runtime_dir = base_dir / RUNTIME_DIR
    python_exe = runtime_dir / MAIN_EXECUTABLE
    first_install = not (
        (runtime_dir / ".livetranslate_deps_ready").exists()
        and python_has_module(python_exe, "torch")
        and python_has_module(python_exe, "PyQt6")
    )
    gpu = detect_gpu_info()
    saved = load_launcher_config(base_dir)

    root = tk.Tk()
    root.title(f"{APP_NAME} Installer")
    root.geometry("640x420" if first_install else "560x180")
    root.resizable(False, False)

    status = tk.StringVar(value="Preparing...")
    ttk.Label(root, text=APP_NAME, font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=16, pady=(16, 6))

    selected = {"pip_index_url": saved.get("pip_index_url", ""), "torch_choice": saved.get("torch_choice")}

    if first_install:
        ttk.Label(root, text="First startup needs to install runtime dependencies.", wraplength=600).pack(anchor="w", padx=16)

        pip_frame = ttk.LabelFrame(root, text="Python package source")
        pip_frame.pack(fill="x", padx=16, pady=(12, 6))
        pip_var = tk.StringVar(value=selected["pip_index_url"] or "default")
        ttk.Radiobutton(pip_frame, text="Default PyPI", value="default", variable=pip_var).pack(anchor="w", padx=10, pady=2)
        ttk.Radiobutton(pip_frame, text="Tsinghua mirror", value="https://pypi.tuna.tsinghua.edu.cn/simple", variable=pip_var).pack(anchor="w", padx=10, pady=2)
        ttk.Radiobutton(pip_frame, text="Aliyun mirror", value="https://mirrors.aliyun.com/pypi/simple", variable=pip_var).pack(anchor="w", padx=10, pady=2)

        torch_frame = ttk.LabelFrame(root, text="PyTorch version")
        torch_frame.pack(fill="x", padx=16, pady=6)
        recommended = selected["torch_choice"] or gpu["recommended"]
        torch_var = tk.StringVar(value=recommended)
        gpu_text = f"NVIDIA GPU: {gpu['name']}" if gpu["has_nvidia"] else "No NVIDIA GPU detected"
        if gpu["compute_cap"]:
            gpu_text += f" | Compute capability: {gpu['compute_cap']}"
        ttk.Label(torch_frame, text=gpu_text, wraplength=590).pack(anchor="w", padx=10, pady=(6, 2))
        cu126_label = "CUDA 12.6"
        cu128_label = "CUDA 12.8"
        if gpu["recommended"] == "cu126":
            cu126_label += " (recommended)"
        elif gpu["recommended"] == "cu128":
            cu128_label += " (recommended)"
        else:
            cu126_label += " (NVIDIA only)"
            cu128_label += " (RTX 50 series / Blackwell)"
        cpu_label = "CPU only" + (" (recommended)" if gpu["recommended"] == "cpu" else "")
        ttk.Radiobutton(torch_frame, text=cpu_label, value="cpu", variable=torch_var).pack(anchor="w", padx=10, pady=2)
        ttk.Radiobutton(torch_frame, text=cu126_label, value="cu126", variable=torch_var).pack(anchor="w", padx=10, pady=2)
        ttk.Radiobutton(torch_frame, text=cu128_label, value="cu128", variable=torch_var).pack(anchor="w", padx=10, pady=(2, 8))

        button_row = ttk.Frame(root)
        button_row.pack(fill="x", padx=16, pady=(4, 8))
        start_button = ttk.Button(button_row, text="Install and start")
        start_button.pack(side="right")

    ttk.Label(root, textvariable=status, wraplength=600).pack(anchor="w", padx=16, pady=(6, 0))
    bar = ttk.Progressbar(root, mode="indeterminate")
    bar.pack(fill="x", padx=16, pady=16)
    if first_install:
        bar.stop()
    else:
        bar.start(12)

    result = {"code": 0}
    q: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def set_status(text: str) -> None:
        q.put(("status", text))

    def worker() -> None:
        try:
            install_config = None
            if first_install:
                pip_value = pip_var.get()
                install_config = get_install_config(
                    base_dir,
                    {
                        "pip_index_url": "" if pip_value == "default" else pip_value,
                        "torch_choice": torch_var.get(),
                    },
                )
            result["code"] = start_application(base_dir, install_config=install_config, status_cb=set_status)
        except Exception as exc:
            q.put(("error", str(exc)))
            result["code"] = 1
        finally:
            q.put(("done", None))

    def poll() -> None:
        try:
            while True:
                kind, value = q.get_nowait()
                if kind == "status" and value:
                    status.set(value)
                elif kind == "error" and value:
                    show_message(APP_NAME, value)
                elif kind == "done":
                    root.destroy()
                    return
        except queue.Empty:
            pass
        root.after(120, poll)

    def begin_install() -> None:
        if first_install:
            start_button.configure(state="disabled")
        bar.start(12)
        threading.Thread(target=worker, daemon=True).start()

    if first_install:
        start_button.configure(command=begin_install)
    else:
        threading.Thread(target=worker, daemon=True).start()
    root.after(120, poll)
    root.mainloop()
    return int(result["code"])


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

    return show_progress_window(base_dir)


if __name__ == "__main__":
    raise SystemExit(main())
