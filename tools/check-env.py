# -*- coding: utf-8 -*-
"""肥鱼指挥官 · 环境体检。

    python check-env.py            # 看当前安装
    python check-env.py --json     # 机器可读

路径全部来自 feiyu_paths（支持 FEIYU_* 环境变量），换机器/换目录不用改代码。
「缺件」只统计真正会让插件跑不起来的硬项；软项（没开游戏、桥接没起）只提示。
"""
import json
import os
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feiyu_paths as P  # noqa: E402

PLUGIN_DIR = os.path.join(P.HOME, "plugin")
PROFILE = (os.environ.get("FEIYU_PROFILE")
           or os.path.join(os.path.expanduser("~"), ".dsh", "profiles", "desktop"))
PKG = "@local/dsh-feiyu-commander"
JUNC = os.path.join(PROFILE, "node_modules", "@local", "feiyu-commander")
PORT = int(os.environ.get("FEIYU_PORT") or 8766)

rows = []


def chk(name, ok, detail="", soft=False):
    if ok:
        mark = "OK "
    else:
        mark = "!! " if soft else "XX "
    rows.append({"mark": mark, "name": name, "detail": str(detail), "soft": soft})
    return ok


# 1. python + 包
chk("Python 解释器", os.path.exists(sys.executable), sys.executable)
chk("Python 版本", sys.version_info >= (3, 10), sys.version.split()[0])
for mod in ("numpy", "comtypes", "cffi", "sherpa_onnx", "soundcard"):
    try:
        m = __import__(mod)
        chk("包 " + mod, True, getattr(m, "__version__", "?"))
    except Exception as e:  # noqa: BLE001
        chk("包 " + mod, False, str(e)[:70])

# 2. 模型（听人话用；没有只是听不见，不算装坏）
for rel in ("sensevoice-onnx/model.int8.onnx", "sensevoice-onnx/tokens.txt", "silero/silero_vad.onnx"):
    p = os.path.join(P.MODELS, rel.replace("/", os.sep))
    ok = os.path.exists(p)
    chk("模型 " + rel.split("/")[-1], ok,
        f"{os.path.getsize(p)/1e6:.1f} MB" if ok else p, soft=True)

# 3. ffmpeg / node（现在 TTS 走 pcm、抓帧走 node，都属可选）
chk("ffmpeg", os.path.exists(P.FFMPEG), P.FFMPEG, soft=True)
chk("node", os.path.exists(P.NODE), P.NODE, soft=True)

# 4. 脚本
for f in ("audio-bridge.py", "session-cmd.py", "auto-trigger.py", "screen_state.py",
          "screen-ocr.ps1", "COMMANDER.md"):
    p = os.path.join(P.TOOLS, f)
    chk("脚本 " + f, os.path.exists(p), f"{os.path.getsize(p)} B" if os.path.exists(p) else p)

# 5. 音频设备
try:
    import soundcard as sc

    def nm(x):
        return getattr(x, "name", str(x))

    spk = [nm(s) for s in sc.all_speakers()]
    mic_all = [nm(m) for m in sc.all_microphones(include_loopback=True)]
    spk_set = set(spk)
    loop = [n for n in mic_all if n in spk_set]          # 这版 soundcard 没有 _isloopback
    virt = [n for n in spk if "voicemeeter input (2" in n.lower() or "cable input" in n.lower()]
    real_mic = [n for n in mic_all if n not in spk_set]
    chk("扬声器", bool(spk), " | ".join(spk)[:110])
    chk("回环设备", bool(loop), " | ".join(loop)[:110])
    chk("虚拟麦输入端", bool(virt), " | ".join(virt)[:110] or "没装 VB-CABLE/VoiceMeeter（只念给自己听可不装）", soft=True)
    chk("物理麦克风", bool(real_mic), " | ".join(real_mic)[:110], soft=True)
except Exception as e:  # noqa: BLE001
    chk("音频设备枚举", False, str(e)[:110])

# 6. 桥接在线（没起不算缺件，用 启动.bat 起）
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/status", timeout=2) as r:
        st = json.loads(r.read().decode("utf-8"))
    chk("桥接端口", True, f"监听 {st.get('listen', {}).get('device')}｜转写 {st.get('listen', {}).get('texts')}")
    chk("说话目标", True, str(st.get("speaker", {}).get("target") or "（还没说过话）"), soft=True)
    chk("主人麦克风", bool(st.get("owner")), str(st.get("owner", {}).get("device")), soft=True)
except Exception as e:  # noqa: BLE001
    chk("桥接端口", False, f"没在跑（{str(e)[:50]}）—— 双击 启动.bat", soft=True)

# 7. 插件安装
for f in ("package.json", "host.js", "client.js", "cordis.patch.yml"):
    p = os.path.join(PLUGIN_DIR, f)
    chk("插件文件 " + f, os.path.exists(p), p if not os.path.exists(p) else "")
chk("junction", os.path.exists(JUNC), JUNC)
try:
    with open(os.path.join(PROFILE, "package.json"), encoding="utf-8-sig") as f:
        pkg = json.load(f)
    bundles = pkg.get("dsh", {}).get("profile", {}).get("bundles", [])
    chk("bundles 已登记", PKG in bundles, f"共 {len(bundles)} 个")
except Exception as e:  # noqa: BLE001
    chk("profile package.json", False, str(e)[:80])

# 8. 设置文件
sp = os.path.join(P.LOG_DIR, "session-settings.json")
try:
    with open(sp, encoding="utf-8-sig") as f:
        s = json.load(f)
    chk("设置文件", True, "绑定=%s" % (s.get("sessionId") or "（还没绑）"))
    chk("人设提示词", bool(str(s.get("prompt", "")).strip()), "%d 字" % len(str(s.get("prompt", ""))))
    chk("发进会话的提示词", bool(str(s.get("startPrompt", "")).strip()), "%d 字" % len(str(s.get("startPrompt", ""))))
    chk("语音 Key", bool(P.tts_key()), "已配" if P.tts_key() else "没配（说话会没声）", soft=True)
except Exception as e:  # noqa: BLE001
    chk("设置文件", False, str(e)[:80])

# 9. PowerShell + OCR
try:
    out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", "$PSVersionTable.PSVersion.Major"],
                         capture_output=True, text=True, timeout=25)
    chk("PowerShell", out.returncode == 0, "版本 " + (out.stdout or "").strip(), soft=True)
except Exception as e:  # noqa: BLE001
    chk("PowerShell", False, str(e)[:80], soft=True)

try:
    ps = ("[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null;"
          "([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag }) -join ','")
    out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True, timeout=40)
    langs = (out.stdout or "").strip()
    chk("Windows OCR 语言", "zh-Hans" in langs, langs, soft=True)
except Exception as e:  # noqa: BLE001
    chk("Windows OCR 语言", False, str(e)[:80], soft=True)


def main():
    if "--json" in sys.argv:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print("=" * 78)
    print(f" 肥鱼指挥官 · 环境体检")
    print(f" 家目录 {P.HOME}")
    print(f" profile {PROFILE}")
    print("=" * 78)
    for r in rows:
        print("%s %-24s %s" % (r["mark"], r["name"], r["detail"]))
    print("=" * 78)
    hard = [r for r in rows if r["mark"] == "XX "]
    soft = [r for r in rows if r["mark"] == "!! "]
    print("缺件：%d 项" % len(hard))
    if soft:
        print("可选未就绪：%d 项（不影响启动）" % len(soft))
    if not hard:
        print("可以用了：重启 DSH → 设置里绑定会话 → 启动.bat → 会话里说「开始指挥」")
    return 0 if not hard else 1


if __name__ == "__main__":
    sys.exit(main())
