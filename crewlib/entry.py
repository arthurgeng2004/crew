#!/usr/bin/env python3
"""crew：Claude 带队、GPT 工人（Codex）干活。见安装目录的 README.md。"""
import os
import sys

# A usable UTF-8 locale varies by platform. Never loop if no candidate exists.
import locale
try:
    locale.setlocale(locale.LC_ALL, "")
except locale.Error:
    pass
if not os.environ.get("CREW_REEXEC") and "utf" not in locale.getpreferredencoding(False).lower().replace("-", ""):
    for candidate in ("C.UTF-8", "en_US.UTF-8", "UTF-8"):
        try:
            locale.setlocale(locale.LC_ALL, candidate)
        except locale.Error:
            continue
        env = dict(os.environ, CREW_REEXEC="1", LC_ALL=candidate)
        os.execve(sys.executable, [sys.executable, __file__] + sys.argv[1:], env)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from crewlib.cli import main  # noqa: E402

main()
