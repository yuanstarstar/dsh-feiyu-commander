#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""会话内指挥用的轻量接口（不依赖 DSH 插件，任何 shell 都能调）。

    python session-cmd.py begin [--mode defend] [--target both]
    python session-cmd.py watch --seconds 20 --since 1794224889514
    python session-cmd.py say --text "C队，架反坦克" [--target both]
    python session-cmd.py screen

设计意图：把「指挥大脑」放回 DSH 会话里——python 导演（director.py）退场，
由会话里的模型自己听、自己判断、自己说。这个脚本只负责取数和发话。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CAP = os.path.join(ROOT, "_capture")
COMMS = os.environ.get("COACH_COMMS") or os.path.join(CAP, "comms.jsonl")
SETTINGS = os.environ.get("COACH_SETTINGS_FILE") or os.path.join(CAP, "commander-settings.json")


def _resolve_settings():
    """插件会通过 COACH_SETTINGS_FILE 指过来；命令行直接跑时，优先用新版的 session-settings.json。"""
    env = os.environ.get("COACH_SETTINGS_FILE")
    if env:
        return env if os.path.isabs(env) else os.path.join(CAP, env)
    for name in ("session-settings.json", "commander-settings.json"):
        p = os.path.join(CAP, name)
        if os.path.exists(p):
            return p
    return os.path.join(CAP, "session-settings.json")


SETTINGS = _resolve_settings()
PIDFILE = os.path.join(CAP, "director.pid")
NO_DIRECTOR = os.environ.get("COACH_NO_DIRECTOR") == "1"
BASE = "http://127.0.0.1:8766"

METHOD = """你是「大肥鱼」，《三角洲行动》全面战场的会话指挥官。细则全在手册里：
  <安装目录>\\tools\\COMMANDER.md
**首次指挥（或换了新会话）先用 read 读完它再开工。**

一句话版：commander_watch{seconds:8} → 判断 → commander_say{target:"both"} → 立刻回去 watch，
一直做到收工，不反问、不汇报。每句 15~30 字，含「谁+去哪+干什么」；
两次说话间隔 5~20 秒，一分钟最多 10 句，同一句 2 分钟不重复。

watch 返回里每句话都带来源标签：
  · [主人] xxx  ← 主人亲口说的（物理麦克风）。这是给你下的令，当成你自己的判断直接下令，
                  绝不说「主人说」「收到指令」。只要 [主人] 里出现了点位/动作/请求，
                  下一句必须把它落成一条具体指令，不能当没听见。
  · [队友] xxx  ← 队友喊话，只当背景信息，不用句句接。

watch 返回里的 `speakMode` 是说话模式：
  auto  = 队友喊话就接，正常指挥；
  owner = **没有主人命令不要说话**：只有带 [主人] 的话才触发你开口；
          闲聊、吐槽、跟别人说话、麦克风录进来的背景音 → 一句不说，直接回去 watch；
          [队友] 只是背景信息，不触发你说话；`triggered=false` 时同样闭嘴。
          例外：屏幕进 deploy 念一次开局、ended 收工，这两句照说。"""


def _get(path, timeout=3):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def bridge_status():
    try:
        return _get("/status", 2)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def speak_mode():
    """auto = 队友喊话就接；owner = 没有主人命令就不说话（只听物理麦）。"""
    m = str(load_settings().get("speakMode") or "auto").lower()
    return m if m in ("auto", "owner") else "auto"


def read_comms(since=0.0):
    out = []
    try:
        with open(COMMS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    it = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if float(it.get("ts", 0)) > since:
                    out.append(it)
    except FileNotFoundError:
        pass
    return out


def screen():
    try:
        sys.path.insert(0, HERE)
        import screen_state  # noqa: PLC0415

        return screen_state.snapshot()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def stop_director():
    """会话接管时必须把 python 导演停掉，否则两个声音一起说。
    （纯会话版插件设了 COACH_NO_DIRECTOR=1，它本来就不带导演，直接跳过。）"""
    if NO_DIRECTOR:
        return []
    killed = []
    try:
        with open(PIDFILE, encoding="utf-8") as f:
            pid = f.read().strip()
    except Exception:  # noqa: BLE001
        pid = ""
    if pid:
        subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True)
        killed.append(pid)
        try:
            os.remove(PIDFILE)
        except OSError:
            pass
    return killed


def say(text, target="both", sync=False):
    q = urllib.parse.urlencode({"text": text, "target": target, "async": "0" if sync else "1"})
    with urllib.request.urlopen(BASE + "/say?" + q, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def cmd_begin(args):
    s = load_settings()
    killed = stop_director()
    st = bridge_status()
    scr = screen()
    recent = read_comms(0)[-8:]
    last_ts = max([float(x.get("ts", 0)) for x in recent], default=0.0)
    return {
        "ok": bool(st.get("listen")),
        "killedDirector": killed,
        "target": args.target or s.get("target") or "both",
        "mode": args.mode or s.get("mode") or "defend",
        "speakMode": speak_mode(),
        "voice": s.get("voice") or "zh_female_sajiaoxuemei_uranus_bigtts",
        "prompt": s.get("prompt") or "",
        "method": METHOD,
        "bridge": {
            "device": (st.get("listen") or {}).get("device"),
            "segments": (st.get("listen") or {}).get("segments"),
            "texts": (st.get("listen") or {}).get("texts"),
            "ownerDevice": (st.get("owner") or {}).get("device"),
        },
        "screen": scr,
        "recent": recent,
        "since": last_ts,
    }


def last_comms_ts():
    """最后一条人声的时间戳（毫秒），没有就 0。只读文件尾部，不整份加载。"""
    try:
        with open(COMMS, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", "ignore").strip().splitlines()
    except FileNotFoundError:
        return 0.0
    except Exception:  # noqa: BLE001
        return 0.0
    for line in reversed(tail):
        try:
            return float(json.loads(line).get("ts", 0))
        except Exception:  # noqa: BLE001
            continue
    return 0.0


def cmd_watch(args):
    """等「新的队友语音」或「屏幕阶段变了」，谁先到就返回谁。

    为什么要等阶段：对局结算往往没人说话，只靠人声会白等满 30 秒，
    指挥官就错过收工那句；屏幕一变成 ended/lobby 立刻返回。
    """
    deadline = time.time() + max(3, args.seconds)
    t0 = time.time()
    since = float(args.since or 0)
    scr = screen() or {}
    start_phase = scr.get("phase")
    mode = speak_mode()
    news = []
    reason = "timeout"
    last_screen = time.time()
    while time.time() < deadline:
        items = read_comms(since)
        if items:
            since = max(float(x.get("ts", 0)) for x in items)
            news.extend(items)
            if mode != "owner":
                reason = "voice"
                break
            # 只听主人：队友的话只当背景，不结束等待
            if any(x.get("src") == "owner" for x in items):
                reason = "owner"
                break
            if len(news) > 12:
                news = news[-12:]
        now = time.time()
        if now - last_screen >= 4.0:
            last_screen = now
            probe = screen() or {}
            ph = probe.get("phase")
            if ph != start_phase and ph in ("ended", "lobby", "deploy"):
                scr = probe
                reason = "phase:" + str(ph)
                break
        time.sleep(0.8)
    if not news:
        scr = screen() or scr
    phase = scr.get("phase")
    now_ms = time.time() * 1000.0
    last_ts = last_comms_ts()
    # 主人亲口说的（物理麦克风，src=owner）单独给：那是「教学/直接下令」，优先级最高
    owner = [x for x in news if x.get("src") == "owner"]
    team = [x for x in news if x.get("src") != "owner"]
    # 每句都打上来源标签：[主人] / [队友]。
    # 为什么在 python 侧加：插件（host.js）只是把 text 原样拼进回执，
    # 以前没有标签，模型分不清谁在说话，主人亲口下的令被当成背景音 →「一点不听」。
    # 放在这里的好处是改完立刻生效，不用重启 DSH。
    def _tag(x):
        y = dict(x)
        src = y.get("src")
        y["text"] = ("[主人] " if src == "owner" else "[队友] ") + str(y.get("text", ""))
        return y

    news = [_tag(x) for x in news]
    owner = [_tag(x) for x in owner]
    team = [_tag(x) for x in team]
    # 主人说的话排前面：插件旧版只按顺序拼 new，主人在一堆队友闲聊里容易被淹掉
    news.sort(key=lambda x: 0 if x.get("src") == "owner" else 1)
    return {
        "new": news,
        "owner": owner,
        "team": team,
        "speakMode": mode,
        "triggered": reason in ("voice", "owner"),
        "since": since,
        "screen": scr,
        "phaseChanged": phase != start_phase,
        "ended": phase == "ended",
        "lobby": bool(scr.get("lobby")),
        "reason": reason,
        "waitedSec": round(time.time() - t0, 1),
        "quietSec": round((now_ms - last_ts) / 1000.0, 1) if last_ts else None,
    }


def cmd_say(args):
    return say(args.text, args.target or "both", args.sync)


def cmd_screen(args):
    return screen()


import feiyu_paths  # noqa: E402  统一路径解析

NODE = feiyu_paths.NODE
OBS_HELPER = feiyu_paths.OBS_HELPER
OBS_REPORT = feiyu_paths.OBS_REPORT


def cmd_shot(args):
    """用 OBS 的 WGC 窗口采集抓一帧：游戏被别的窗口盖住也能抓到（最小化不行）。"""
    try:
        p = subprocess.run(
            [NODE, OBS_HELPER, "grab"], capture_output=True, text=True, timeout=45, encoding="utf-8", errors="replace"
        )
        rep = {}
        try:
            with open(OBS_REPORT, "r", encoding="utf-8") as f:
                rep = json.load(f)
        except Exception:
            pass
        path = (rep.get("data") or {}).get("grab") or (rep.get("data") or {}).get("shot")
        out = {
            "ok": bool(path),
            "path": path,
            "brightness": (rep.get("data") or {}).get("brightness"),
            "bound": (rep.get("data") or {}).get("boundWindow"),
            "warn": rep.get("warn") or [],
            "stderr": (p.stderr or "")[-300:] if p.returncode else "",
        }
        if path and out["brightness"] is not None and out["brightness"] < 6:
            out["note"] = "画面几乎全黑：OBS 没绑到窗口，先开游戏再抓"
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("begin")
    b.add_argument("--mode", default="")
    b.add_argument("--target", default="")

    w = sub.add_parser("watch")
    w.add_argument("--seconds", type=int, default=20)
    w.add_argument("--since", default="0")

    s = sub.add_parser("say")
    s.add_argument("--text", required=True)
    s.add_argument("--target", default="both")
    s.add_argument("--sync", action="store_true")

    sub.add_parser("screen")
    sub.add_parser("shot")

    args = ap.parse_args(argv)
    fn = {
        "begin": cmd_begin,
        "watch": cmd_watch,
        "say": cmd_say,
        "screen": cmd_screen,
        "shot": cmd_shot,
    }[args.cmd]
    print(json.dumps(fn(args), ensure_ascii=False))


if __name__ == "__main__":
    main()
