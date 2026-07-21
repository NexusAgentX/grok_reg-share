"""Force-close Chromium instances and orphan project browsers on Windows."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
LogFn = Callable[[str], None]


def _browser_pid(browser: Any) -> int | None:
    for attr in ("process_id", "pid", "id"):
        try:
            v = getattr(browser, attr, None)
            if callable(v):
                v = v()
            if v is not None:
                return int(v)
        except Exception:
            continue
    # DrissionPage internal
    try:
        proc = getattr(browser, "process", None) or getattr(
            getattr(browser, "browser", None), "process", None
        )
        if proc is not None:
            return int(getattr(proc, "pid", None) or proc)
    except Exception:
        pass
    try:
        # some versions: browser._driver.process
        drv = getattr(browser, "driver", None) or getattr(browser, "_driver", None)
        if drv is not None:
            p = getattr(drv, "process", None)
            if p is not None:
                return int(getattr(p, "pid", p))
    except Exception:
        pass
    return None


def force_close_browser(
    browser: Any | None,
    tmp: str | Path | None = None,
    log: LogFn | None = None,
) -> None:
    """Quit browser and hard-kill process tree if still alive."""
    if browser is None:
        return
    pid = _browser_pid(browser)
    try:
        browser.quit()
    except Exception:
        try:
            browser.quit(del_data=True)  # type: ignore[call-arg]
        except Exception:
            pass
    time.sleep(0.3)
    if pid and sys.platform.startswith("win"):
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=8,
            )
        except Exception:
            pass
    if tmp:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass
    if log:
        log(f"[browser] closed pid={pid or '?'}")


def kill_project_orphan_browsers(
    log: LogFn | None = None,
    *,
    include_mint: bool = False,
) -> int:
    """Kill orphan register browsers (pure-api-*).

    By default does NOT kill oauth-headless mint browsers — concurrent mint
    workers would be destroyed mid-OAuth if we sweep them here.
    """
    if not sys.platform.startswith("win"):
        return 0
    markers = ["pure-api-"]
    if include_mint:
        markers.extend(["oauth-headless-", "oauth-no-extension-"])
    markers_ps = ", ".join("'{0}'".format(m.replace("'", "''")) for m in markers)
    ps = f"""
$ErrorActionPreference='SilentlyContinue'
$markers = @({markers_ps})
$names = @('chrome.exe','msedge.exe','chromium.exe')
$killed = 0
Get-CimInstance Win32_Process | Where-Object {{
  $names -contains $_.Name -and $_.CommandLine
}} | ForEach-Object {{
  $cl = $_.CommandLine
  foreach ($m in $markers) {{
    if ($cl -like ('*' + $m + '*')) {{
      try {{
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        $killed++
      }} catch {{}}
      break
    }}
  }}
}}
Write-Output $killed
"""
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=30,
        )
        n = int((r.stdout or "0").strip().splitlines()[-1] or "0")
        if log and n:
            log(f"[browser] killed {n} orphan register chrome processes")
        return n
    except Exception as exc:
        if log:
            log(f"[browser] orphan kill failed: {exc}")
        return 0
