"""Feint-standard browser launching helpers.

Prefer Brave in a full-screen window for operator dashboards and external URLs,
while keeping a safe default-browser fallback when Brave is unavailable.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Iterable


DEFAULT_BROWSER = "brave"
DEFAULT_MODE = "fullscreen"
VALID_MODES = {"fullscreen", "maximized", "normal", "kiosk"}

WINDOWS_BRAVE_PATHS = (
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
)

BRAVE_COMMANDS = ("brave", "brave-browser", "brave.exe")


def _expand_path(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(value.strip().strip('"')))


def _existing_file(value: str) -> str | None:
    if not value:
        return None
    path = Path(_expand_path(value))
    return str(path) if path.is_file() else None


def find_brave_executable() -> str | None:
    """Return the preferred Brave executable path, or None when unavailable."""
    override = _existing_file(os.getenv("FEINT_BROWSER_PATH", ""))
    if override:
        return override

    browser = os.getenv("FEINT_BROWSER", DEFAULT_BROWSER).strip()
    if browser and browser.lower() not in {"brave", "brave-browser", "brave.exe"}:
        explicit = _existing_file(browser)
        if explicit:
            return explicit
        found = shutil.which(browser)
        if found:
            return found

    for candidate in WINDOWS_BRAVE_PATHS:
        existing = _existing_file(candidate)
        if existing:
            return existing

    for command in BRAVE_COMMANDS:
        found = shutil.which(command)
        if found:
            return found

    return None


def browser_mode(mode: str | None = None) -> str:
    """Normalize the Feint browser mode; invalid values safely use fullscreen."""
    selected = (mode or os.getenv("FEINT_BROWSER_MODE") or DEFAULT_MODE).strip().lower()
    return selected if selected in VALID_MODES else DEFAULT_MODE


def build_browser_args(urls: str | Iterable[str], mode: str | None = None) -> list[str]:
    """Build browser arguments without launching, useful for tests."""
    if isinstance(urls, str):
        selected_urls = [urls]
    else:
        selected_urls = [str(url) for url in urls if str(url).strip()]
    if not selected_urls:
        raise ValueError("at least one URL is required")

    args = ["--new-window"]
    selected_mode = browser_mode(mode)
    if selected_mode == "fullscreen":
        args.append("--start-fullscreen")
    elif selected_mode == "maximized":
        args.append("--start-maximized")
    elif selected_mode == "kiosk":
        args.append("--kiosk")
    args.extend(selected_urls)
    return args


def open_urls_in_feint_browser(urls: str | Iterable[str], mode: str | None = None) -> bool:
    """Open one or more URLs in Feint's preferred browser.

    Returns True when a launch attempt was made successfully. Browser launch failures
    never raise through to callers because dashboards and OAuth flows should keep going.
    """
    if isinstance(urls, str):
        selected_urls = [urls]
    else:
        selected_urls = [str(url) for url in urls if str(url).strip()]
    if not selected_urls:
        return False

    executable = find_brave_executable()
    if executable:
        try:
            subprocess.Popen([executable, *build_browser_args(selected_urls, mode)])
            return True
        except Exception as exc:
            print(f"Warning: Brave launch failed ({exc}). Falling back to default browser.")
    else:
        print("Warning: Brave not found. Falling back to default browser.")

    try:
        webbrowser.open(selected_urls[0])
        for url in selected_urls[1:]:
            webbrowser.open_new_tab(url)
        return True
    except Exception as exc:
        print(f"Warning: default browser launch failed ({exc}).")
        return False


def open_in_feint_browser(url: str, mode: str | None = None) -> bool:
    """Open a single URL in Feint's preferred browser."""
    return open_urls_in_feint_browser(url, mode=mode)


def wait_for_url(url: str, timeout: float = 60.0, interval: float = 1.0) -> bool:
    """Wait briefly for a local service before opening its dashboard."""
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() <= deadline:
        try:
            with urllib.request.urlopen(url, timeout=min(interval, 5.0)) as response:
                if 200 <= getattr(response, "status", 200) < 500:
                    return True
        except Exception:
            time.sleep(interval)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open URLs in Feint's preferred browser.")
    parser.add_argument("command", nargs="?", default="open", choices=("open",))
    parser.add_argument("urls", nargs="+")
    parser.add_argument("--mode", choices=sorted(VALID_MODES))
    parser.add_argument("--wait-url", help="URL to wait for before opening the browser.")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)

    if args.wait_url:
        wait_for_url(args.wait_url, timeout=args.timeout)
    return 0 if open_urls_in_feint_browser(args.urls, mode=args.mode) else 1


if __name__ == "__main__":
    raise SystemExit(main())
