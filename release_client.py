from __future__ import annotations

import json
import os
import shutil
import ssl
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    size: int


def normalize_tag(tag: str) -> str:
    return tag.strip().lstrip("vV")


def same_version(a: str, b: str) -> bool:
    return normalize_tag(a) == normalize_tag(b)


def get_json(url: str, timeout: int = 10) -> dict[str, Any]:
    req = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "LiveTranslate-Updater",
        },
    )
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    with urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_latest_release(repo: str) -> dict[str, Any]:
    return get_json(f"https://api.github.com/repos/{repo}/releases/latest")


def select_windows_asset(release: dict[str, Any], token: str) -> ReleaseAsset | None:
    assets = release.get("assets") or []
    for asset in assets:
        name = asset.get("name", "")
        if token in name and name.lower().endswith(".zip"):
            return ReleaseAsset(
                name=name,
                url=asset["browser_download_url"],
                size=int(asset.get("size") or 0),
            )
    for asset in assets:
        name = asset.get("name", "")
        if name.lower().endswith(".zip"):
            return ReleaseAsset(
                name=name,
                url=asset["browser_download_url"],
                size=int(asset.get("size") or 0),
            )
    return None


def download_file(url: str, destination: Path, timeout: int = 30) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    req = Request(url, headers={"User-Agent": "LiveTranslate-Updater"})
    with urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
        with destination.open("wb") as fp:
            shutil.copyfileobj(resp, fp)


def extract_zip(zip_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(destination)


def copy_tree(src: Path, dst: Path, skip_names: set[str] | None = None) -> None:
    skip_names = skip_names or set()
    for item in src.iterdir():
        if item.name in skip_names:
            continue
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def prune_install_dir(root: Path, preserve_names: set[str]) -> None:
    for item in root.iterdir():
        if item.name in preserve_names:
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink(missing_ok=True)


def terminate_processes_by_name(exe_name: str, timeout: float = 8.0) -> None:
    try:
        import psutil
    except Exception:
        return

    target = exe_name.lower()
    victims = []
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info.get("name") or "").lower() == target:
                victims.append(proc)
        except Exception:
            continue

    for proc in victims:
        try:
            proc.terminate()
        except Exception:
            pass

    gone, alive = psutil.wait_procs(victims, timeout=timeout)
    for proc in alive:
        try:
            proc.kill()
        except Exception:
            pass

