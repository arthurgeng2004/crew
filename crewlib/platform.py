"""POSIX process identity checks for Linux and macOS. No process-name-wide signals."""
import datetime
import os
import subprocess
import sys


def process_info(pid):
    """Return (start identity, command, state); an absent process returns None."""
    if not pid or int(pid) <= 0:
        return None
    try:
        # Keep Linux's start-ticks format compatible with Claude session records.
        if sys.platform.startswith("linux"):
            with open(f"/proc/{int(pid)}/stat") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
            with open(f"/proc/{int(pid)}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
            return fields[19], cmd, fields[0]
        env = dict(os.environ, LC_ALL="C")
        def ps(column):
            return subprocess.check_output(
                ["ps", "-o", column + "=", "-p", str(int(pid))],
                text=True, stderr=subprocess.DEVNULL, timeout=5, env=env).strip()
        return ps("lstart"), ps("command"), ps("stat")
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def session_alive(pid, expected_start=None, started_at=None):
    info = process_info(pid)
    if not info or info[2].startswith("Z"):
        return False
    if not expected_start or str(expected_start).strip() == info[0]:
        return True
    if sys.platform == "darwin":
        # Some clients store the process birth time as epoch seconds or ms.
        try:
            epoch = datetime.datetime.strptime(info[0], "%a %b %d %H:%M:%S %Y").timestamp()
            value = float(expected_start)
            if value > 1e12:
                value /= 1000
            return abs(epoch - value) < 2
        except (ValueError, TypeError):
            pass
    return False
