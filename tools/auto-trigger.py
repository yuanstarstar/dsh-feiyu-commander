#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""盯「开始行动」按钮：从大厅（右下角有「开始行动」）变成战斗（按钮消失）时，
写一个 trigger 文件，DSH 插件读到就往会话里发一段提示词。

用法：
    python auto-trigger.py            # 常驻，每 5 秒看一次
    python auto-trigger.py --once     # 只看一次，打印当前阶段
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import screen_state  # noqa: E402

CAPTURE = os.path.join(HERE, "..", "_capture")
TRIGGER = os.path.join(CAPTURE, "trigger.json")
LOG = os.path.join(CAPTURE, "auto-trigger.log")

POLL = 5.0
# 只有「大厅 → 进局」这一条边算「开始行动被点了」。
# 点完可能先进「小队分配/竞选」（deploy）再进战斗（battle），两种都算；
# unknown（比如切到 DSH 被挡住）不算，免得误触发。
ENTER_STATES = ("deploy", "battle")


def log(msg):
    line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


_MUTEX = None


def already_running():
    """全局互斥：插件、boot.py、手动启动可能同时拉起哨兵，只允许一个活着。

    用命名 Mutex 而不是 pid 文件——进程被强杀时 Mutex 由系统自动释放，
    不会像 pid 文件那样留下僵尸锁。句柄要一直持有到进程退出。
    """
    global _MUTEX
    try:
        import ctypes  # noqa: PLC0415

        k32 = ctypes.windll.kernel32
        k32.SetLastError(0)  # 先清掉本线程可能残留的错误码，否则第一个实例也会被误判
        # 用 Local\ 而不是 Global\：Global 命名空间要 SeCreateGlobalPrivilege（管理员），
        # 普通用户下 CreateMutexW 会直接失败 → 互斥形同虚设（之前真的起了 2 个）。
        # 哨兵都跑在同一个用户会话里，Local\ 足够。
        _MUTEX = k32.CreateMutexW(None, False, "Local\\feiyu-auto-trigger")
        return k32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    except Exception:  # noqa: BLE001
        return False


def write_trigger(reason, st):
    os.makedirs(CAPTURE, exist_ok=True)
    payload = {
        "at": int(time.time() * 1000),
        "reason": reason,
        "phase": st.get("phase"),
        "side": st.get("side"),
        "map": st.get("map"),
        "timer": st.get("timer"),
        "tickets": st.get("tickets"),
    }
    tmp = TRIGGER + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, TRIGGER)
    return payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    if not args.once and already_running():
        log("已有哨兵在跑，这个退出（pid=%s）" % os.getpid())
        return 0

    last = None
    log("start: 盯「开始行动」（大厅→战斗 触发）")
    while True:
        try:
            st = screen_state.snapshot()
        except Exception as e:  # noqa: BLE001
            log("screen error: %s" % e)
            if args.once:
                return 1
            time.sleep(POLL)
            continue

        phase = st.get("phase")
        if args.once:
            print(json.dumps(st, ensure_ascii=False))
            return 0

        if phase != last:
            log(
                "phase %s→%s lobby=%s side=%s map=%s timer=%s tickets=%s"
                % (last, phase, st.get("lobby"), st.get("side"), st.get("map"), st.get("timer"), st.get("tickets"))
            )
            # 「开始行动」被点：上一帧还看得见按钮，这一帧已经进战斗
            if last == "lobby" and phase in ENTER_STATES:
                t = write_trigger("开始行动", st)
                log("TRIGGER %s at=%s" % (t["reason"], t["at"]))
            last = phase

        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
