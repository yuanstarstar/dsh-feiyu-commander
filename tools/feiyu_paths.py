"""肥鱼指挥官 —— 统一路径解析。

所有脚本都从这里拿路径，不再各自写死。优先级：

  1. 环境变量（插件 spawn 子进程时会传，安装脚本也会写进插件配置）
  2. 从本文件所在目录推导（tools 的上一级就是「家目录」）
  3. 最后才是本机原有的默认值（保证老机器不配置也能跑）

可覆盖的环境变量：

  FEIYU_HOME      家目录（默认：tools 的上一级）
  FEIYU_LOG_DIR   运行数据目录（默认 <HOME>/_capture）
  FEIYU_PYLIBS    额外 python 包目录（默认 <HOME>/pylibs）
  FEIYU_MODELS    SenseVoice 模型目录（默认 ~/.dsh/speech-to-text/sensevoice/models）
  FEIYU_FFMPEG    ffmpeg.exe 路径（默认 PATH，其次 ~/.dsh/tools/ffmpeg/bin/ffmpeg.exe）
  FEIYU_NODE      node.exe 路径（默认 PATH，其次 DSH 自带运行时）
  FEIYU_TTS_KEY   豆包语音合成 key（不配就用 Windows 自带 SAPI，离线可用）
"""

from __future__ import annotations

import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))


def _first_env(*names: str) -> str:
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if v:
            return v
    return ""


def _which(*names: str) -> str:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return ""


HOME = _first_env("FEIYU_HOME") or os.path.dirname(HERE)
TOOLS = HERE
TMP = os.path.join(HOME, "tmp")
LOG_DIR = _first_env("FEIYU_LOG_DIR") or os.path.join(HOME, "_capture")
CAPTURE = LOG_DIR
PYLIBS = _first_env("FEIYU_PYLIBS") or os.path.join(HOME, "pylibs")

MODELS = _first_env("FEIYU_MODELS") or os.path.join(
    os.path.expanduser("~"), ".dsh", "speech-to-text", "sensevoice", "models")
MODEL = os.path.join(MODELS, "sensevoice-onnx", "model.int8.onnx")
TOKENS = os.path.join(MODELS, "sensevoice-onnx", "tokens.txt")
VAD = os.path.join(MODELS, "silero", "silero_vad.onnx")

FFMPEG = (
    _first_env("FEIYU_FFMPEG")
    or _which("ffmpeg")
    or os.path.join(os.path.expanduser("~"), ".dsh", "tools", "ffmpeg", "bin", "ffmpeg.exe")
)

NODE = (
    _first_env("FEIYU_NODE")
    or _which("node")
    or os.path.join(
        os.path.expanduser("~"), ".dsh", "dsh-runtimes",
        "dsh-plugin-desktop-primary-runtime", "dependencies", "node", "bin", "node.exe")
)

PIPER_DIR = _first_env("FEIYU_PIPER") or os.path.join(HOME, "piper")
PIPER_MODEL = (_first_env("FEIYU_PIPER_MODEL")
               or os.path.join(PIPER_DIR, "voices", "zh_CN-huayan-medium.onnx"))

SCREEN_OCR = os.path.join(HERE, "screen-ocr.ps1")
OBS_HELPER = os.path.join(HERE, "obs-setup.mjs")
OBS_REPORT = os.path.join(TMP, "obs-report.json")

POWERSHELL = _which("powershell.exe", "powershell") or "powershell.exe"


def ensure_dirs() -> None:
    for d in (TMP, LOG_DIR, os.path.join(LOG_DIR, "ps-tmp")):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass


def tts_key() -> str:
    """豆包 key：环境变量 > <LOG_DIR>/voice.json。都没有返回空串（走 SAPI）。"""
    k = _first_env("FEIYU_TTS_KEY", "VOLC_KEY")
    if k:
        return k
    try:
        import json
        p = os.path.join(LOG_DIR, "voice.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return str(json.load(f).get("apiKey", "")).strip()
    except Exception:
        pass
    return ""


def describe() -> str:
    rows = [
        ("HOME", HOME), ("TOOLS", TOOLS), ("LOG_DIR", LOG_DIR), ("PYLIBS", PYLIBS),
        ("MODELS", MODELS), ("FFMPEG", FFMPEG), ("NODE", NODE),
        ("TTS", "豆包" if tts_key() else "Windows SAPI（离线）"),
    ]
    return "\n".join(f"  {k:<8} {v}" for k, v in rows)


if __name__ == "__main__":
    print(describe())
