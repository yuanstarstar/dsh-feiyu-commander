# -*- coding: utf-8 -*-
"""读三角洲行动的屏幕状态：是否在局内 / 阶段 / 阵营 / 地图 / 剩余时间 / 票数 / 延迟丢包。

用 Windows 自带 OCR（tools/screen-ocr.ps1），不联网、不依赖第三方 OCR 库。
坐标按 2560x1600 全屏校准（游戏窗口在 0,0 铺满）。

两套布局：
  1) 开局「小队分配/竞选」画面 —— 左上角是 阵营 / 20:00 / 地图，中间是小队分配和 24 人
  2) 战斗 HUD —— 左上角是**小地图**（小地图右下角有 16:29 倒计时），顶部中间是票数，
     右上角固定是 温度/帧率/延迟/丢包，最上面一行是 CN UID 水印

所以「在不在局内」用 UID 水印或延迟那一簇来判，不再依赖开局画面的字段。

命令行：
    python screen_state.py            # 打印一行 JSON
    python screen_state.py --raw      # 连带原始 OCR 行一起打印
"""
import json
import os
import re
import subprocess
import sys

import feiyu_paths  # noqa: E402  统一路径解析

PS = feiyu_paths.POWERSHELL
SCRIPT = feiyu_paths.SCREEN_OCR

# 0: CN UID 水印（在局内的铁证）  1: 顶部中间票数  2: 右上角延迟丢包  3: 小地图右下角倒计时
# 4: 开局「小队分配」画面左上角的 阵营 / 20:00 / 地图
# 5: 右下角「开始行动」按钮（大厅开关：有它=在大厅，没它=在局内）
REGIONS = "0,0,540,40,4|1040,10,320,80,4|2060,20,470,60,3|175,440,110,50,8|100,20,420,120,4|2080,1380,400,110,3"

MAPS = ("烬区", "天际港", "零号大坝", "长弓溪谷", "航天基地", "巴克什", "攀升", "堑壕", "边境", "火线")


def _norm(s):
    """Windows OCR 会把中文按字拆开加空格，去掉它们；数字/英文之间的空格保留一个。"""
    s = s.strip()
    s = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def ocr(regions=REGIONS, timeout=40):
    # 测试用：COACH_SCREEN_FILE 指向一张截图，就走离线识别而不是抓屏
    src = os.environ.get("COACH_SCREEN_FILE", "")
    cmd = [PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", SCRIPT]
    if src:
        cmd += ["-File", src, "-Regions", regions, "-AsJson"]
    else:
        cmd += ["-Regions", regions, "-AsJson"]
    # Add-Type 要往 %TEMP% 写临时 cs 文件；低完整性/沙箱下那里会被拒，
    # 于是 DPI 修正和前台窗口标题都会静默失败（屏幕按 1707x1067 虚拟化）。
    # 把 TEMP 指到工作区里，两个都恢复正常。
    env = dict(os.environ)
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "_capture", "ps-tmp")
    try:
        os.makedirs(tmp, exist_ok=True)
        env["TEMP"] = env["TMP"] = os.path.abspath(tmp)
    except Exception:  # noqa: BLE001
        pass
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ocr timeout"}
    if p.returncode != 0:
        return {"ok": False, "error": (p.stderr or b"").decode("utf-8", "replace")[:400]}
    txt = (p.stdout or b"").decode("utf-8", "replace").strip()
    try:
        return json.loads(txt)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "bad json: %r %r" % (exc, txt[:200])}


def _digits(s):
    return re.sub(r"\D", "", s or "")


def _timer_from(text):
    """把 OCR 出来的一行时间读成秒。字体很细，识别经常丢字，所以只在能拼出 4 位数字时采信。"""
    d = _digits(text)
    if len(d) == 4:
        mm, ss = int(d[:2]), int(d[2:])
        if mm < 100 and ss < 60:
            return mm * 60 + ss
    return None


def parse(res):
    st = {"ok": True, "inGame": False, "lobby": False, "phase": "unknown", "side": None, "map": None,
          "timer": None, "timerText": None, "tickets": None,
          "latency": None, "loss": None, "lines": []}
    if not res.get("ok"):
        st["ok"] = False
        st["error"] = res.get("error")
        return st

    per = []
    for r in res.get("results", []):
        lines = r.get("lines", [])
        # ConvertTo-Json 会把只有一条的数组拆成对象，这里统一成列表
        if isinstance(lines, dict):
            lines = [lines]
        elif isinstance(lines, str):
            lines = [{"text": lines}]
        row = []
        for ln in lines:
            if isinstance(ln, str):
                ln = {"text": ln}
            t = _norm(ln.get("text", ""))
            if t:
                row.append(t)
        per.append(row)

    texts = [t for row in per for t in row]
    st["lines"] = texts
    joined = " ".join(texts)

    # 在局内：CN UID 水印里那串 20 位数字，或者右上角的延迟读数，或者前台窗口就是游戏
    fg = (res.get("fg") or "").strip()
    st["fg"] = fg
    st["visible"] = bool(fg and "三角洲" in fg)
    if re.search(r"\d{15,}", joined):
        st["inGame"] = True
    if "三角洲" in fg:
        st["inGame"] = True
    for t in texts:
        m = re.search(r"延迟\s*[：:]?\s*(\d+)", t)
        if m:
            st["latency"] = int(m.group(1))
            st["inGame"] = True
        m = re.search(r"丢包\s*[：:]?\s*(\d+)\s*/\s*(\d+)", t)
        if m:
            st["loss"] = "%s/%s" % (m.group(1), m.group(2))

    for t in texts:
        if "守方" in t or "防守方" in t:
            st["side"] = "守方"
        elif "攻方" in t or "进攻方" in t:
            st["side"] = "攻方"
        if not st["map"]:
            for name in MAPS:
                if name in t:
                    st["map"] = name
                    break
        m = re.search(r"(\d{1,2})\s*[：:]\s*(\d{2})", t)
        if m and st["timer"] is None:
            st["timer"] = int(m.group(1)) * 60 + int(m.group(2))
            st["timerText"] = "%02d:%02d" % (int(m.group(1)), int(m.group(2)))

    # 第 3 块是小地图右下角的倒计时
    if len(per) > 3:
        for t in per[3]:
            sec = _timer_from(t)
            if sec is not None and sec <= 60 * 60:
                st["timer"] = sec
                st["timerText"] = "%02d:%02d" % (sec // 60, sec % 60)
                break

    # 第 1 块是顶部中间的票数
    if len(per) > 1:
        for t in per[1]:
            m = re.search(r"(\d{2,3})", t)
            if m:
                st["tickets"] = int(m.group(1))
                break

    # 阶段：只有「小队分配/竞选」是开局；结算画面才算结束；其余在局内一律算战斗
    # 大厅开关（用户定的）：右下角「开始行动」还在 = 没进对局；它一消失 = 这局开了，
    # 它重新出现 = 这局结束。（顶部那个「开始游戏」是导航页签，只留作调试字段）
    st["lobbyAlt"] = bool(re.search(r"开始游戏", joined))
    st["lobby"] = bool(re.search(r"开始行动", joined))
    if re.search(r"小队分配|竞选|选择分配方式", joined):
        st["phase"] = "deploy"
    elif st["lobby"]:
        st["phase"] = "lobby"
    elif re.search(r"对局结束|比赛结束|战斗结束|结算", joined):
        st["phase"] = "ended"
    elif st["inGame"]:
        st["phase"] = "battle"
    return st


def snapshot():
    return parse(ocr())


if __name__ == "__main__":
    s = snapshot()
    if "--raw" not in sys.argv:
        s.pop("lines", None)
    print(json.dumps(s, ensure_ascii=False))
