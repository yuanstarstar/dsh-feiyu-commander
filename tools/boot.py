# -*- coding: utf-8 -*-
"""肥鱼指挥官 · 一键启动 / 体检

用法：
    python tools/boot.py            拉起缺的东西（OBS / 桥接 / 开始行动哨兵），再体检一遍
    python tools/boot.py --check    只看状态，不动手
    python tools/boot.py --stop     停掉桥接和哨兵（OBS 留着）

它只负责「环境」，不负责指挥。指挥还是回 DSH 会话里点「立刻开始指挥」。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import feiyu_paths as P  # noqa: E402

ROOT = Path(P.HOME)
TOOLS = Path(P.TOOLS)
CAP = Path(P.LOG_DIR)
LOGS = CAP

PY = sys.executable
NODE = P.NODE
OBS_EXE = os.environ.get("FEIYU_OBS") or r"C:\Program Files\obs-studio\bin\64bit\obs64.exe"
OBS_HELPER = TOOLS / "obs-setup.mjs"
SETTINGS = CAP / "session-settings.json"
BRIDGE_URL = "http://127.0.0.1:8766/status"
SESSIONS_ROOT = Path(os.path.expanduser("~")) / ".dsh" / "sessions"

DETACH = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def _ascii(s) -> bool:
    try:
        str(s).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def ensure_piper_drive() -> str:
    """piper 只认 ASCII 路径。安装目录带中文时，给它 subst 一个盘符（重启后跑一次启动.bat 重建）。"""
    p = str(P.PIPER_DIR)
    if not (Path(p) / "piper.exe").exists():
        return ""
    if _ascii(p):
        return ""
    try:
        out = subprocess.run(["subst"], capture_output=True, text=True, encoding="gbk", errors="replace")
        for line in (out.stdout or "").splitlines():
            if "=>" not in line:
                continue
            left, right = line.split("=>", 1)
            if os.path.normcase(right.strip().rstrip("\\")) == os.path.normcase(p.rstrip("\\")):
                return left.strip().rstrip(":") + ":"
    except Exception:
        pass
    for ch in "ZYXWVUTSRQPONMLKJIHGFE":
        letter = ch + ":"
        if os.path.exists(letter + "\\"):
            continue
        try:
            r = subprocess.run(["subst", letter, p], capture_output=True, timeout=10)
            if r.returncode == 0 and os.path.exists(letter + "\\"):
                return letter
        except Exception:
            continue
    return ""


def env() -> dict:
    e = dict(os.environ)
    e["PYTHONIOENCODING"] = "utf-8"
    e["PYTHONPATH"] = str(ROOT / "pylibs")
    e["COACH_SETTINGS_FILE"] = str(SETTINGS)
    e["COACH_NO_DIRECTOR"] = "1"
    return e


def http_json(url: str, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def obs_up() -> bool:
    import socket

    s = socket.socket()
    s.settimeout(0.6)
    try:
        s.connect(("127.0.0.1", 4455))
        return True
    except Exception:
        return False
    finally:
        s.close()


def bridge_status():
    return http_json(BRIDGE_URL)


def find_watchers() -> list[int]:
    """找 auto-trigger.py：先看 pid 文件（快），文件丢了再按命令行扫一遍。"""
    out: list[int] = []
    pidf = CAP / "auto-trigger.pid"
    if pidf.exists():
        try:
            pid = int(pidf.read_text("utf-8").strip())
            if process_alive(pid):
                out.append(pid)
        except Exception:
            pass
    if out:
        return out
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*auto-trigger.py*' } | "
             "ForEach-Object { $_.ProcessId }"],
            capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace",
        )
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                out.append(int(line))
    except Exception:
        pass
    return out


def process_alive(pid: int) -> bool:
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=8,
            encoding="mbcs", errors="replace",
        )
        return str(pid) in (r.stdout or "")
    except Exception:
        return False


def spawn_detached(args: list[str], log_name: str) -> int:
    log = open(LOGS / log_name, "ab")
    p = subprocess.Popen(
        args,
        cwd=str(TOOLS),
        env=env(),
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        creationflags=DETACH,
        close_fds=True,
    )
    return p.pid


def start_obs() -> str:
    if obs_up():
        return "已在跑"
    try:
        subprocess.Popen(
            [OBS_EXE, "--minimize-to-tray"],
            cwd=r"C:\Program Files\obs-studio\bin\64bit",
            creationflags=DETACH,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception as e:  # noqa: BLE001
        return f"起不来：{e}"
    for _ in range(30):
        time.sleep(0.5)
        if obs_up():
            return "已拉起"
    return "起了进程但 4455 没监听"


def start_bridge() -> str:
    if bridge_status():
        return "已在跑"
    spawn_detached([PY, str(TOOLS / "audio-bridge.py"), "serve"], "bridge.log")
    for _ in range(30):
        time.sleep(0.5)
        if bridge_status():
            return "已拉起"
    return "起了但 8766 没响应（看 _capture\\bridge.log）"


def start_watcher() -> str:
    if find_watchers():
        return "已在跑"
    pid = spawn_detached([PY, str(TOOLS / "auto-trigger.py")], "auto-trigger.log")
    try:
        (CAP / "auto-trigger.pid").write_text(str(pid), "utf-8")
    except Exception:
        pass
    time.sleep(1.5)
    return "已拉起" if process_alive(pid) else "起了但马上退了（看 _capture\\auto-trigger.log）"


def stop_all() -> None:
    pidf = CAP / "auto-trigger.pid"
    for pid in find_watchers():
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    try:
        pidf.unlink()
    except Exception:
        pass
    st = bridge_status()
    if st:
        # 桥接没有 HTTP 退出接口，按端口找进程杀
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue | "
             "ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"],
            capture_output=True,
        )


def latest_session() -> str | None:
    best = None
    for ws in SESSIONS_ROOT.glob("*"):
        if not ws.is_dir():
            continue
        for d in ws.glob("session-*"):
            f = d / "session.v4.jsonl.zstd"
            if not f.exists():
                continue
            try:
                m = f.stat().st_mtime
            except Exception:
                continue
            if best is None or m > best[0]:
                best = (m, d.name)
    return best[1] if best else None


def check_settings() -> str:
    if not SETTINGS.exists():
        return "设置文件不存在（重启 DSH 后会自动生成）"
    try:
        j = json.loads(SETTINGS.read_text("utf-8"))
    except Exception as e:  # noqa: BLE001
        return f"设置文件坏了：{e}"
    sid = j.get("sessionId") or ""
    if not sid:
        return "还没绑定会话（去设置页选一个）"
    uuid = sid.split("session-", 1)[-1]
    hit = None
    for ws in SESSIONS_ROOT.glob("*"):
        if (ws / f"session-{uuid}").exists():
            hit = ws / f"session-{uuid}"
            break
    if hit:
        return f"绑定 {uuid[:8]}… 有效"
    # 以前这里会偷偷改成「最新会话」，用户会以为绑定自己跑了 —— 现在只报告，不动手。
    new = latest_session()
    tail = f"（最新会话是 {new[:20]}…）" if new else ""
    return f"绑定 {uuid[:8]}… 在磁盘上找不到{tail}，去设置页重选"


def auto_on_start() -> bool:
    """「开始行动」自动发提示词的总开关。默认关闭：一切手动。"""
    try:
        return json.loads(SETTINGS.read_text("utf-8")).get("autoOnStart") is True
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只看状态")
    ap.add_argument("--stop", action="store_true", help="停桥接和哨兵")
    a = ap.parse_args()

    if a.stop:
        stop_all()
        print("已停：桥接 + 哨兵（OBS 留着）")
        return 0

    drive = ensure_piper_drive()
    if drive:
        print(f"piper 盘符  {drive} -> {P.PIPER_DIR}")

    if not a.check:
        print("拉起环境…")
        print("  OBS   :", start_obs())
        print("  桥接  :", start_bridge())
        if auto_on_start():
            print("  哨兵  :", start_watcher())
        else:
            print("  哨兵  : 已关闭（autoOnStart=false，提示词手动点「立刻开始指挥」）")

    st = bridge_status() or {}
    ls = st.get("listen") or {}
    sp = st.get("speaker") or {}
    print()
    print("=" * 62)
    print(f"OBS 4455      {'OK' if obs_up() else 'XX  没开'}")
    print(f"桥接 8766     {'OK' if st else 'XX  没开'}")
    if st:
        print(f"  监听        {ls.get('device')}｜片段 {ls.get('segments', 0)}｜转写 {ls.get('texts', 0)}")
        print(f"  说话        {sp.get('target') or '（还没说过话）'}")
        print(f"  主人麦      {(st.get('owner') or {}).get('device')}")
    if auto_on_start():
        print(f"开始行动哨兵   {'OK' if find_watchers() else 'XX  没跑'}")
    else:
        print("开始行动哨兵   已关闭（不自动发提示词）")
    print(f"绑定会话      {check_settings()}")
    print("=" * 62)
    if not st:
        print(f"提示：桥接没起来就看 {CAP / 'bridge.log'}")
    print("下一步：回 DSH，设置 → 肥鱼指挥官 → 点「立刻开始指挥」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
