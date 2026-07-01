import subprocess

import browser


def test_find_brave_executable_prefers_env_path(monkeypatch, tmp_path):
    brave = tmp_path / "brave.exe"
    brave.write_text("", encoding="utf-8")

    monkeypatch.setenv("FEINT_BROWSER_PATH", str(brave))

    assert browser.find_brave_executable() == str(brave)


def test_build_browser_args_defaults_to_fullscreen():
    assert browser.build_browser_args(["https://discord.test", "https://alpaca.test"]) == [
        "--new-window",
        "--start-fullscreen",
        "https://discord.test",
        "https://alpaca.test",
    ]


def test_build_browser_args_supports_maximized_and_normal():
    assert browser.build_browser_args("http://localhost:8501", mode="maximized") == [
        "--new-window",
        "--start-maximized",
        "http://localhost:8501",
    ]
    assert browser.build_browser_args("http://localhost:8501", mode="normal") == [
        "--new-window",
        "http://localhost:8501",
    ]


def test_invalid_browser_mode_falls_back_to_fullscreen(monkeypatch):
    monkeypatch.setenv("FEINT_BROWSER_MODE", "banana")

    assert browser.build_browser_args("http://localhost:8501") == [
        "--new-window",
        "--start-fullscreen",
        "http://localhost:8501",
    ]


def test_open_urls_uses_brave_without_opening_real_browser(monkeypatch):
    calls = []
    monkeypatch.setattr(browser, "find_brave_executable", lambda: r"C:\Brave\brave.exe")
    monkeypatch.setattr(subprocess, "Popen", lambda args: calls.append(args))

    assert browser.open_urls_in_feint_browser(["https://discord.test", "https://alpaca.test"])
    assert calls == [[
        r"C:\Brave\brave.exe",
        "--new-window",
        "--start-fullscreen",
        "https://discord.test",
        "https://alpaca.test",
    ]]


def test_open_urls_falls_back_to_default_browser(monkeypatch):
    opened = []
    monkeypatch.setattr(browser, "find_brave_executable", lambda: None)
    monkeypatch.setattr(browser.webbrowser, "open", lambda url: opened.append(("open", url)))
    monkeypatch.setattr(browser.webbrowser, "open_new_tab", lambda url: opened.append(("tab", url)))

    assert browser.open_urls_in_feint_browser(["https://discord.test", "https://alpaca.test"])
    assert opened == [
        ("open", "https://discord.test"),
        ("tab", "https://alpaca.test"),
    ]
