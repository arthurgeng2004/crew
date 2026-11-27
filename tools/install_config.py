"""Local installer bookkeeping; update only settings and links owned by this install."""
import json
import os
from pathlib import Path
import shutil
import sys
import time

mode, repo_arg, bin_arg, option = sys.argv[1:]
repo, bindir = Path(repo_arg).resolve(), Path(bin_arg).expanduser().absolute()
state = Path(os.environ.get("CREW_HOME") or Path.home() / ".crew").expanduser().absolute()
settings = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"
manifest = state / "install.json"
plugin = str(repo / "plugin")
link = bindir / "crew"


def load(path):
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        value = json.load(fh)
    if not isinstance(value, dict):
        raise ValueError(f"{path} 必须是 JSON 对象，未改动")
    return value


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".crew-tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(path)


def backup():
    if settings.exists():
        target = settings.with_name(settings.name + ".bak-crew-" + str(time.time_ns()))
        shutil.copy2(settings, target)
        print("设置已备份：" + str(target))


os.umask(0o077)
if mode == "install":
    # Validate before any installation mutations. Malformed user settings stay untouched.
    data = load(settings) if option == "1" else {}
    if option == "1":
        env = data.get("env", {})
        if not isinstance(env, dict) or not isinstance(env.get("CLAUDE_CODE_PLUGIN_DIRS", ""), str):
            raise ValueError("env / CLAUDE_CODE_PLUGIN_DIRS 格式无效，未改动")
    if link.is_symlink():
        if link.resolve() != repo / "bin/crew":
            raise ValueError(f"{link} 已指向别处；请用 BIN_DIR 选择其他目录")
    elif link.exists():
        raise ValueError(f"{link} 已存在；请用 BIN_DIR 选择其他目录")
    old = load(manifest)
    if old and old.get("repo") != str(repo):
        raise ValueError("此 CREW_HOME 已由另一安装目录管理，请先卸载或选择新的 CREW_HOME")
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(0o700)
    bindir.mkdir(parents=True, exist_ok=True)
    if not link.is_symlink():
        link.symlink_to(repo / "bin/crew")
    (state / "python-path").write_text(sys.executable + "\n", encoding="utf-8")
    added = bool(old.get("plugin_added"))
    created_settings = bool(old.get("created_settings"))
    created_env = bool(old.get("created_env"))
    created_dirs = bool(old.get("created_dirs"))
    if option == "1":
        dirs = [x for x in env.get("CLAUDE_CODE_PLUGIN_DIRS", "").split(os.pathsep) if x]
        if plugin not in dirs:
            created_settings = created_settings or not settings.exists()
            created_env = created_env or "env" not in data
            created_dirs = created_dirs or "CLAUDE_CODE_PLUGIN_DIRS" not in env
            backup()
            env["CLAUDE_CODE_PLUGIN_DIRS"] = os.pathsep.join(dirs + [plugin])
            data["env"] = env
            write(settings, data)
            added = True
    write(manifest, {"repo": str(repo), "bin_dir": str(bindir), "settings": str(settings), "plugin_added": added,
                     "created_settings": created_settings, "created_env": created_env, "created_dirs": created_dirs})
    sys.path.insert(0, str(repo))
    from crewlib import core
    core.config()
    print("命令：" + str(link))
    print("配置：" + str(state / "config.json"))
elif mode == "uninstall":
    old = load(manifest)
    if old and old.get("repo") != str(repo):
        raise ValueError("此状态目录属于另一安装，未撤销其设置")
    link = Path(old.get("bin_dir") or bindir) / "crew"
    if old.get("settings"):
        settings = Path(old["settings"])
    if old.get("plugin_added") and settings.exists():
        data = load(settings)
        env = data.get("env") or {}
        dirs = [x for x in env.get("CLAUDE_CODE_PLUGIN_DIRS", "").split(os.pathsep) if x != plugin]
        backup()
        if old.get("created_dirs") and not any(dirs):
            env.pop("CLAUDE_CODE_PLUGIN_DIRS", None)
        else:
            env["CLAUDE_CODE_PLUGIN_DIRS"] = os.pathsep.join(dirs)
        if not env and old.get("created_env"):
            data.pop("env", None)
        if not data and old.get("created_settings"):
            settings.unlink()
        else:
            write(settings, data)
    if link.is_symlink() and link.resolve() == repo / "bin/crew":
        link.unlink()
    if option == "1":
        if state in (Path.home(), Path("/")) or len(state.parts) < 3:
            raise ValueError("拒绝清理不安全的 CREW_HOME 路径")
        shutil.rmtree(state, ignore_errors=True)
    elif old:
        manifest.unlink(missing_ok=True)
else:
    raise ValueError("unknown installer operation")
