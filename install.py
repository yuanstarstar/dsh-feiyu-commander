#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""肥鱼指挥官（dsh-feiyu-commander）安装器。

只用标准库；装 python 依赖时才调用 venv 里的 pip。

    python install.py                    装到 %USERPROFILE%\\feiyu-commander
    python install.py --target D:\\feiyu  换安装目录
    python install.py --dry-run          只检查，不动手
    python install.py --download-models  顺带下 ASR 模型（GitHub，约 250MB）
    python install.py --uninstall        卸载（删 junction 和 profile 里的条目，不删你的语音数据）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

KIT = Path(__file__).resolve().parent
HOME = Path(os.environ.get("USERPROFILE") or Path.home())
DEFAULT_TARGET = HOME / "feiyu-commander"
DEFAULT_PROFILE = HOME / ".dsh" / "profiles" / "desktop"

PLUGIN_PKG = "@local/dsh-feiyu-commander"
PLUGIN_ID = "feiyu-commander"

MODEL_TAR = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
             "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2")
VAD_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx")

# 离线 TTS（可选，--with-piper 才下）：不填豆包 Key 时用它出声。
PIPER_ZIP = ("https://github.com/rhasspy/piper/releases/download/2023.11.14-2/"
             "piper_windows_amd64.zip")
PIPER_VOICE = ("https://hf-mirror.com/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/"
               "zh_CN-huayan-medium.onnx")

PERSONA = ("蓝色大肥鱼指挥官是G.T.I.被迫营业的鲸鱼娘：蓝发鲸鳍大尾巴，穿不合身制服，"
           "开局正经演讲，中途嚼饭静默；战术理论满分、临场跑刀摸鱼，被叫“肥鱼”秒炸毛，"
           "劣势劝队友早休息，赢了请吃白饭。")

START_PROMPT = (
    "你现在是「大肥鱼」，接管《三角洲行动》全面战场的指挥。\n"
    "先用 read 工具读 {md}（首次指挥必须读完，之后换会话也要读）。\n"
    "然后按里面的规则循环：commander_watch{{seconds:8}} → 判断 → commander_say{{text,target:\"both\"}} → "
    "立刻回去 watch，一直做到收工。不要反问我、不要汇报进度。\n"
    "watch 返回里的 owner 是主人亲口说的话，当成你自己的判断直接下令，别说「主人说」。"
)

OK, WARN, BAD = "[ok]", "[!]", "[x]"


def say(msg, mark="   "):
    print(f"{mark} {msg}", flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def copy_tree(src: Path, dst: Path):
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ 环境检查
def check_dsh(profile: Path) -> bool:
    pkg = profile / "package.json"
    if not pkg.exists():
        say(f"没找到 DSH 的 profile：{profile}", BAD)
        say("  先装 DSH Desktop 并至少启动一次，再来安装。")
        return False
    data = read_json(pkg)
    if "dsh" not in data:
        say(f"{pkg} 看起来不是 DSH 的 profile（没有 dsh 字段）", BAD)
        return False
    say(f"DSH profile：{profile}")
    return True


def pick_python() -> str:
    """返回一个能用的 python（用于创建 venv）。"""
    cands = [sys.executable]
    for name in ("py", "python"):
        exe = shutil.which(name)
        if exe:
            cands.append(exe)
    for c in cands:
        r = run([c, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"])
        if r.returncode != 0:
            continue
        ver = r.stdout.strip()
        try:
            major, minor = (int(x) for x in ver.split(".", 1))
        except ValueError:
            continue
        if major == 3 and minor >= 10:
            say(f"python：{c}  版本 {ver}")
            return c
    say("没找到 Python 3.10+（装的时候勾选 Add to PATH）", BAD)
    return ""


def ensure_venv(target: Path, py: str, skip_deps: bool) -> Path:
    venv = target / ".venv"
    vpy = venv / "Scripts" / "python.exe"
    if not vpy.exists():
        say("创建独立 python 环境（.venv）…")
        r = run([py, "-m", "venv", str(venv)])
        if r.returncode != 0:
            say("创建 venv 失败：" + (r.stderr or r.stdout)[-400:], BAD)
            return Path(py)
    say(f"python 环境：{vpy}")
    if skip_deps:
        say("跳过依赖安装（--skip-deps）", WARN)
        return vpy
    req = KIT / "requirements.txt"
    say("安装 python 依赖（第一次会慢，走默认源）…")
    r = run([str(vpy), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", str(req)])
    if r.returncode != 0:
        say("依赖装失败，换成清华镜像再试一次…", WARN)
        r = run([str(vpy), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                 "-r", str(req), "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"])
    if r.returncode != 0:
        say("依赖安装失败：\n" + (r.stderr or r.stdout)[-800:], BAD)
        say("  可以手动跑：.venv\\Scripts\\python.exe -m pip install -r requirements.txt")
    else:
        say("依赖装好了（numpy / sherpa-onnx / soundcard / comtypes）")
    return vpy


# ------------------------------------------------------------------ 插件安装
def install_files(target: Path):
    copy_tree(KIT / "plugin", target / "plugin")
    copy_tree(KIT / "tools", target / "tools")
    for sub in ("pylibs", "_capture", "tmp", "models"):
        (target / sub).mkdir(parents=True, exist_ok=True)
    # COMMANDER.md 里的 {{HOME}} 换成真实安装路径
    md = target / "tools" / "COMMANDER.md"
    if md.exists():
        md.write_text(md.read_text(encoding="utf-8").replace("{{HOME}}", str(target)), encoding="utf-8")
    say(f"文件已复制到 {target}")


def link_junction(profile: Path, plugin_dir: Path):
    parent = profile / "node_modules" / "@local"
    parent.mkdir(parents=True, exist_ok=True)
    link = parent / PLUGIN_ID.split("/")[-1]
    if link.exists():
        if link.is_dir() and not link.is_symlink():
            subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True)
        else:
            link.unlink()
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(plugin_dir)],
                       capture_output=True, text=True, encoding="gbk", errors="replace")
    if r.returncode != 0 or not link.exists():
        say("创建 junction 失败：" + (r.stderr or r.stdout)[-300:], BAD)
        say("  以管理员身份重跑一次安装.bat 试试。")
        return False
    say(f"已挂到 DSH：{link}")
    return True


def patch_profile(profile: Path, plugin_dir: Path):
    pkg = profile / "package.json"
    data = read_json(pkg)
    backup = pkg.with_name("package.json.bak-feiyu")
    if not backup.exists():
        shutil.copy2(pkg, backup)
        say(f"已备份 profile 配置：{backup.name}")
    deps = data.setdefault("dependencies", {})
    deps[PLUGIN_PKG] = "link:" + str(plugin_dir).replace("\\", "/")
    bundles = data.setdefault("dsh", {}).setdefault("profile", {}).setdefault("bundles", [])
    if PLUGIN_PKG not in bundles:
        bundles.append(PLUGIN_PKG)
    write_json(pkg, data)
    say(f"已写进 profile（bundles {len(bundles)} 项）")


def unpatch_profile(profile: Path):
    pkg = profile / "package.json"
    if not pkg.exists():
        return
    data = read_json(pkg)
    data.get("dependencies", {}).pop(PLUGIN_PKG, None)
    bundles = data.get("dsh", {}).get("profile", {}).get("bundles", [])
    if PLUGIN_PKG in bundles:
        bundles.remove(PLUGIN_PKG)
    write_json(pkg, data)
    say("已从 profile 移除插件")


def write_patch_yml(plugin_dir: Path, target: Path, vpy: Path):
    """把实际路径写进插件配置（换机器只改这里）。"""
    models = target / "models"
    cfg = {
        "home": str(target),
        "python": str(vpy),
        "toolsDir": str(target / "tools"),
        "pylibs": str(target / "pylibs"),
        "logDir": str(target / "_capture"),
        "models": str(models),
        "ffmpeg": "",
        "node": "",
        "port": 8766,
    }
    lines = [
        "- insert:",
        f"    - id: {PLUGIN_ID}",
        f"      name: '{PLUGIN_PKG}'",
        "      config:",
    ]
    for k, v in cfg.items():
        if isinstance(v, int):
            lines.append(f"        {k}: {v}")
        else:
            lines.append(f"        {k}: '{v}'")
    (plugin_dir / "cordis.patch.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    say("插件配置已写入（路径按本机实际情况）")


def write_settings(target: Path):
    path = target / "_capture" / "session-settings.json"
    if path.exists():
        say("已有 session-settings.json，保留不动", WARN)
        return
    data = {
        "prompt": PERSONA,
        "startPrompt": START_PROMPT.format(md=target / "tools" / "COMMANDER.md"),
        "autoOnStart": False,
        "sessionId": "",
        "target": "both",
        "speakMode": "owner",
        "llm": True,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    say(f"设置文件已生成：{path}")
    say("  打开 DSH 设置 →「肥鱼指挥官」，把人设/提示词/绑定会话调好", WARN)


# ------------------------------------------------------------------ 模型
def have_models(models: Path) -> bool:
    return ((models / "sensevoice-onnx" / "model.int8.onnx").exists()
            and (models / "sensevoice-onnx" / "tokens.txt").exists()
            and (models / "silero" / "silero_vad.onnx").exists())


def ensure_models(models: Path, download: bool):
    if have_models(models):
        say(f"模型已在：{models}")
        return
    legacy = HOME / ".dsh" / "speech-to-text" / "sensevoice" / "models"
    if have_models(legacy):
        say(f"复用已装的模型：{legacy}")
        for rel in ("sensevoice-onnx/model.int8.onnx", "sensevoice-onnx/tokens.txt", "silero/silero_vad.onnx"):
            src, dst = legacy / rel, models / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                shutil.copy2(src, dst)
        say("模型已复制到安装目录")
        return
    if not download:
        say("还没放 ASR 模型（SenseVoice）。", WARN)
        say("  三选一：① 装 DSH 的 sensevoice 语音插件（模型在 ~/.dsh/speech-to-text/…）")
        say("          ② python install.py --download-models")
        say("          ③ 手动把 model.int8.onnx / tokens.txt / silero_vad.onnx 放到 " + str(models))
        return
    say("下载模型（约 250MB，GitHub，慢就耐心等）…")
    tmp = models / "_download"
    tmp.mkdir(parents=True, exist_ok=True)
    tar = tmp / "sensevoice.tar.bz2"
    try:
        urllib.request.urlretrieve(MODEL_TAR, tar)
        with tarfile.open(tar, "r:bz2") as tf:
            tf.extractall(tmp)
        found = list(tmp.rglob("model.int8.onnx"))
        if not found:
            raise RuntimeError("压缩包里没找到 model.int8.onnx")
        src_dir = found[0].parent
        (models / "sensevoice-onnx").mkdir(parents=True, exist_ok=True)
        for name in ("model.int8.onnx", "tokens.txt"):
            s = src_dir / name
            if s.exists():
                shutil.copy2(s, models / "sensevoice-onnx" / name)
        urllib.request.urlretrieve(VAD_URL, models / "silero" / "silero_vad.onnx")
        say("模型就绪")
    except Exception as exc:  # noqa: BLE001
        say(f"下载失败：{exc}", BAD)
        say("  网络不稳就手动下载，或换一台有模型的机器拷过来。")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ 主流程
def install_piper(target: Path):
    """可选：装离线 TTS（piper + 中文音色，约 82MB）。没有豆包 Key 时靠它出声。"""
    dst = target / "piper"
    if (dst / "piper.exe").exists() and (dst / "voices" / "zh_CN-huayan-medium.onnx").exists():
        say("piper 已存在，跳过")
        return
    tmp = Path(tempfile.gettempdir()) / "feiyu-piper-dl"
    try:
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "voices").mkdir(parents=True, exist_ok=True)
        tmp.mkdir(parents=True, exist_ok=True)
        say("下载 piper（约 21MB）…")
        zp = tmp / "piper.zip"
        urllib.request.urlretrieve(PIPER_ZIP, zp)
        with zipfile.ZipFile(zp) as zf:
            zf.extractall(tmp / "x")
        roots = [p for p in (tmp / "x").rglob("piper.exe")]
        if not roots:
            raise RuntimeError("压缩包里没找到 piper.exe")
        src = roots[0].parent
        for item in src.iterdir():
            tgt = dst / item.name
            if item.is_dir():
                shutil.copytree(item, tgt, dirs_exist_ok=True)
            else:
                shutil.copy2(item, tgt)
        say("下载中文音色（约 60MB）…")
        urllib.request.urlretrieve(PIPER_VOICE, dst / "voices" / "zh_CN-huayan-medium.onnx")
        urllib.request.urlretrieve(PIPER_VOICE + ".json", dst / "voices" / "zh_CN-huayan-medium.onnx.json")
        say("piper 就绪（没填豆包 Key 时用它说话）", OK)
    except Exception as exc:  # noqa: BLE001
        say(f"piper 没装上：{exc}", BAD)
        say("  不影响其他功能：填了豆包 Key 就用云端 TTS。")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def verify(target: Path, profile: Path):
    py = target / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        py = Path(sys.executable)
    chk = target / "tools" / "check-env.py"
    if not chk.exists():
        return
    say("体检：")
    env = dict(os.environ)
    env.update({
        "FEIYU_HOME": str(target),
        "FEIYU_LOG_DIR": str(target / "_capture"),
        "FEIYU_PYLIBS": str(target / "pylibs"),
        "FEIYU_MODELS": str(target / "models"),
        "FEIYU_PROFILE": str(profile),
    })
    r = run([str(py), str(chk)], env=env)
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        if line.strip():
            print("   " + line)


def install(args):
    target = Path(args.target).expanduser().resolve()
    profile = Path(args.profile).expanduser().resolve()
    say(f"安装到：{target}")
    if args.dry_run:
        ok = check_dsh(profile)
        say("dry-run：只检查，没改任何文件", WARN)
        return 0 if ok else 1
    if not check_dsh(profile):
        return 1
    py = pick_python()
    if not py:
        return 1
    target.mkdir(parents=True, exist_ok=True)
    install_files(target)
    vpy = ensure_venv(target, py, args.skip_deps)
    plugin_dir = target / "plugin"
    if not link_junction(profile, plugin_dir):
        return 1
    patch_profile(profile, plugin_dir)
    write_patch_yml(plugin_dir, target, vpy)
    write_settings(target)
    ensure_models(target / "models", args.download_models)
    if args.with_piper:
        install_piper(target)
    verify(target, profile)
    print()
    say("装完了。下一步：", OK)
    say("  1) 重启 DSH Desktop（插件要重新加载）")
    say("  2) 打开 设置 →「肥鱼指挥官」：绑定当前会话、填提示词")
    say("  3) 双击「启动.bat」（或在会话里点「立刻开始指挥」）")
    say("  4) 回到会话里说「开始指挥」")
    return 0


def uninstall(args):
    target = Path(args.target).expanduser().resolve()
    profile = Path(args.profile).expanduser().resolve()
    link = profile / "node_modules" / "@local" / PLUGIN_ID.split("/")[-1]
    if link.exists():
        subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True)
        say(f"已移除 junction：{link}")
    unpatch_profile(profile)
    if target.exists() and not args.keep_files:
        shutil.rmtree(target, ignore_errors=True)
        say(f"已删除安装目录：{target}")
    say("卸载完成。重启 DSH 生效。", OK)
    return 0


def main():
    ap = argparse.ArgumentParser(description="肥鱼指挥官 安装器")
    ap.add_argument("--target", default=str(DEFAULT_TARGET))
    ap.add_argument("--profile", default=str(DEFAULT_PROFILE))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-deps", action="store_true")
    ap.add_argument("--download-models", action="store_true")
    ap.add_argument("--with-piper", action="store_true",
                    help="顺带装离线 TTS（piper + 中文音色，约 82MB）；不填豆包 Key 时用它出声")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--keep-files", action="store_true")
    args = ap.parse_args()
    print("=" * 60)
    print(" 肥鱼指挥官 · 安装器")
    print("=" * 60)
    t0 = time.time()
    code = uninstall(args) if args.uninstall else install(args)
    print(f"\n用时 {time.time() - t0:.1f}s")
    return code


if __name__ == "__main__":
    sys.exit(main())
