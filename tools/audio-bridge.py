"""大战场指挥官 · 音频桥（听 + 说）

听：WASAPI 回环采集默认扬声器（= 游戏声 + 队友语音）→ silero VAD 切句 → SenseVoice 识别
说：火山豆包 TTS → 播放到指定设备（装了 VB-CABLE 就是 CABLE Input → 游戏麦克风）

用法:
  python audio-bridge.py serve                 # 常驻：监听 + 本地 HTTP 控制口
  python audio-bridge.py devices               # 列设备
  python audio-bridge.py say "文字" [--target cable|speaker]
  python audio-bridge.py comms [n]             # 看最近识别到的话

HTTP（默认 127.0.0.1:8766）:
  GET /status
  GET /comms?limit=20
  GET /say?text=...&target=cable
  GET /mute
"""
import base64
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import wave
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import feiyu_paths  # noqa: E402  统一路径解析（环境变量优先，换机器不用改代码）

WORK = feiyu_paths.HOME
TMP = feiyu_paths.TMP
LOG_DIR = feiyu_paths.LOG_DIR
PYLIBS = feiyu_paths.PYLIBS
COMMS = os.path.join(LOG_DIR, "comms.jsonl")
RECENT = []   # 所有听者共用的一条时间线（队友 + 主人），/comms 读它
FFMPEG = feiyu_paths.FFMPEG
MODELS = feiyu_paths.MODELS
MODEL = feiyu_paths.MODEL
TOKENS = feiyu_paths.TOKENS
VAD = feiyu_paths.VAD
feiyu_paths.ensure_dirs()

TTS_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
# key 只从环境变量 FEIYU_TTS_KEY / VOLC_KEY，或 <LOG_DIR>/voice.json 读；
# 都没配就走 Windows 自带 SAPI（离线、不需要任何 API）。
TTS_KEY = feiyu_paths.tts_key()
TTS_SPEAKER = "zh_female_sajiaoxuemei_uranus_bigtts"
TTS_RESOURCE = "seed-tts-2.0"

SR = 48000          # 采集采样率
PLAY_SR = 44100     # 播放采样率：本机 Realtek 耳机端点在「播放率 == 设备原生 48k」时
                    # 会被 Nahimic APO 把左右折叠到右声道；换任意非原生率即恢复双声道
ASR_SR = 16000
HTTP_PORT = 8766

SPOKEN = deque(maxlen=30)   # 自己刚说过的（防回环自听）
PLAY_SINCE = 0.0            # 自己这次开始播报的时刻
PLAY_UNTIL = 0.0            # 自己这次播报结束的时刻（含一点余量）

TAG = re.compile(r"<\|[^|]*\|>")
NOISE = {"。", "，", "嗯。", "啊。", "哦。", "谢谢观看", "字幕", "请不吝点赞", "订阅", "转发"}


def _norm(s):
    return re.sub(r"[，。！？、,.!?~…\s]", "", s or "")


def _ngrams(s, n=3):
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def is_self_echo(text, window=18.0, ratio=0.62, gram_overlap=0.45):
    """回环会把自己的 TTS 再听一遍，丢掉。
    ASR 会把「补位」听成「补喂」，所以除了整体相似度，还要看 3-gram 重合。"""
    import difflib
    now = time.time()
    t = _norm(text)
    if not t:
        return False
    gt = _ngrams(t)
    for ts, said in list(SPOKEN):
        if now - ts > window:
            continue
        s = _norm(said)
        if not s:
            continue
        if t == s or (len(t) >= 6 and (t in s or s in t)):
            return True
        gs = _ngrams(s)
        if gt and gs and len(gt & gs) / min(len(gt), len(gs)) >= gram_overlap:
            return True
        if difflib.SequenceMatcher(None, t, s).ratio() >= ratio:
            return True
    return False


def plausible_speech(text, min_cjk=3):
    """枪炮声被 VAD 切进来时 SenseVoice 会吐出乱码/零星符号，过滤掉。"""
    t = _norm(text)
    if len(t) < min_cjk:
        return False
    cjk = sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")
    return cjk >= min_cjk


# ---------------------------------------------------------------- 音频设备
def _sc():
    sys.path.insert(0, PYLIBS)
    try:
        import comtypes  # noqa: F401  先初始化 COM，否则 soundcard 之后再 import comtypes 会报 WinError -2147417850
    except Exception:
        pass
    import soundcard as sc
    return sc


def list_devices():
    sc = _sc()
    return {
        "default_speaker": str(sc.default_speaker().name),
        "speakers": [str(s.name) for s in sc.all_speakers()],
        "microphones": [str(m.name) for m in sc.all_microphones(include_loopback=True)],
    }


def find_speaker(prefer=""):
    """按名字片段找播放设备；找不到退回默认。"""
    sc = _sc()
    if prefer:
        for s in sc.all_speakers():
            if prefer.lower() in str(s.name).lower():
                return s
    return sc.default_speaker()


VIRTUAL_KEYS = ("cable input", "voicemeeter input (2")


def find_virtual_input():
    """找虚拟麦克风的入口设备（VB-CABLE 或 VoiceMeeter 第二实例）。"""
    sc = _sc()
    names = [(str(s.name), s) for s in sc.all_speakers()]
    for key in VIRTUAL_KEYS:
        for name, dev in names:
            if key in name.lower():
                return dev
    return None


OWNER_KEYS = ("麦克风阵列", "microphone array", "realtek", "headset", "耳机")


def find_owner_mic():
    """主人的物理麦克风（不是回环）：用来听「主人亲口教的指令」。
    优先系统默认录音设备；没有就按名字找；都没有返回空串。"""
    sc = _sc()
    try:
        dev = sc.default_microphone()
        if dev is not None:
            return str(dev.name)
    except Exception:  # noqa: BLE001
        pass
    try:
        for m in sc.all_microphones(include_loopback=False):
            name = str(m.name)
            for k in OWNER_KEYS:
                if k.lower() in name.lower():
                    return name
    except Exception:  # noqa: BLE001
        pass
    return ""


def resolve_targets(target="cable"):
    """cable=虚拟声卡（进游戏语音）｜speaker=默认扬声器（自己听）｜both=两边一起。"""
    cable = find_virtual_input()
    default = _sc().default_speaker()
    if target == "both":
        return [cable, default] if cable is not None else [default]
    if target == "cable":
        return [cable] if cable is not None else []
    return [default]


# ---------------------------------------------------------------- TTS
def tts_mp3(text, speaker=TTS_SPEAKER, resource=TTS_RESOURCE, api_key=None,
            speech_rate=0, pitch=0):
    api_key = (api_key or TTS_KEY).strip()
    body = json.dumps({"req_params": {
        "text": text, "speaker": speaker,
        "audio_params": {"format": "mp3", "sample_rate": 24000, "speech_rate": speech_rate},
        "post_process": {"pitch": pitch},
    }}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(TTS_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Api-Key", api_key)
    req.add_header("X-Api-Resource-Id", resource)
    req.add_header("X-Api-Request-Id", str(uuid.uuid4()))
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    buf = raw.decode("utf-8", "replace")
    dec = json.JSONDecoder()
    idx, audio = 0, bytearray()
    while idx < len(buf):
        while idx < len(buf) and buf[idx] in " \r\n\t":
            idx += 1
        if idx >= len(buf):
            break
        try:
            obj, end = dec.raw_decode(buf, idx)
        except json.JSONDecodeError:
            break
        idx = end
        if isinstance(obj, dict) and isinstance(obj.get("data"), str):
            audio += base64.b64decode(obj["data"])
    if not audio:
        raise RuntimeError("TTS 无音频返回")
    return bytes(audio)


def _mono_to_stereo(arr, sr, out_sr):
    """单声道 float32 → 目标采样率的双声道（纯 numpy，不依赖 ffmpeg）。"""
    import numpy as np

    if sr != out_sr and arr.size > 1:
        tgt = max(1, int(round(arr.size * out_sr / float(sr))))
        arr = np.interp(np.linspace(0, arr.size - 1, tgt), np.arange(arr.size), arr).astype(np.float32)
    if arr.size == 0:
        return np.zeros((0, 2), dtype=np.float32)
    return np.repeat(arr[:, None], 2, axis=1)


def tts_pcm(text, speaker=TTS_SPEAKER, resource=TTS_RESOURCE, api_key=None,
            speech_rate=0, pitch=0, samplerate=SR, sr=24000):
    """豆包 TTS 直接要 PCM（s16le 单声道），省掉 ffmpeg 解码 —— 换机器少一个依赖。"""
    import numpy as np

    api_key = (api_key or TTS_KEY).strip()
    body = json.dumps({"req_params": {
        "text": text, "speaker": speaker,
        "audio_params": {"format": "pcm", "sample_rate": sr, "speech_rate": speech_rate},
        "post_process": {"pitch": pitch},
    }}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(TTS_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Api-Key", api_key)
    req.add_header("X-Api-Resource-Id", resource)
    req.add_header("X-Api-Request-Id", str(uuid.uuid4()))
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    buf = raw.decode("utf-8", "replace")
    dec = json.JSONDecoder()
    idx, audio = 0, bytearray()
    while idx < len(buf):
        while idx < len(buf) and buf[idx] in " \r\n\t":
            idx += 1
        if idx >= len(buf):
            break
        try:
            obj, end = dec.raw_decode(buf, idx)
        except json.JSONDecodeError:
            break
        idx = end
        if isinstance(obj, dict) and isinstance(obj.get("data"), str):
            audio += base64.b64decode(obj["data"])
    if not audio:
        raise RuntimeError("TTS 无音频返回")
    mono = np.frombuffer(bytes(audio), dtype=np.int16).astype(np.float32) / 32768.0
    return _mono_to_stereo(mono, sr, samplerate)


def mp3_to_pcm(mp3_bytes, samplerate=SR):
    import numpy as np
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "2", "-ar", str(samplerate), "pipe:1"],
        input=mp3_bytes, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    if pcm.size % 2:
        pcm = pcm[:-1]
    return pcm.reshape(-1, 2)


# ---------------------------------------------------------------- 离线 TTS（piper）
_PIPER_STATE = {"rt": None}


def _ascii_safe(s):
    try:
        str(s).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _subst_for(path):
    """把带中文的目录映射成一个 ASCII 盘符（subst），piper 只认 ASCII 路径。"""
    try:
        out = subprocess.run(["subst"], capture_output=True, text=True, encoding="gbk", errors="replace")
        for line in (out.stdout or "").splitlines():
            if "=>" not in line:
                continue
            left, right = line.split("=>", 1)
            letter = left.strip().split(":")[0].strip().upper()
            if len(letter) == 1 and letter.isalpha():
                if os.path.normcase(right.strip().rstrip("\\")) == os.path.normcase(path.rstrip("\\")):
                    return letter + ":"
    except Exception:
        pass
    for ch in "ZYXWVUTSRQPONMLKJIHGFE":
        letter = ch + ":"
        if os.path.exists(letter + "\\"):
            continue
        try:
            r = subprocess.run(["subst", letter, path], capture_output=True, timeout=10)
            if r.returncode == 0 and os.path.exists(letter + "\\"):
                return letter
        except Exception:
            continue
    return ""


def _piper_runtime():
    """piper 只认 ASCII 路径：路径带中文时用 subst 映射成盘符再跑。"""
    if _PIPER_STATE["rt"] is not None:
        return _PIPER_STATE["rt"] or None
    src = feiyu_paths.PIPER_DIR
    exe = os.path.join(src, "piper.exe")
    model = feiyu_paths.PIPER_MODEL
    if not (os.path.exists(exe) and os.path.exists(model)):
        _PIPER_STATE["rt"] = ()
        return None
    if _ascii_safe(exe) and _ascii_safe(model):
        _PIPER_STATE["rt"] = (src, exe, model)
        return _PIPER_STATE["rt"]
    letter = _subst_for(src)
    if not letter:
        _PIPER_STATE["rt"] = ()
        return None
    rel_model = os.path.relpath(model, src)
    _PIPER_STATE["rt"] = (letter + "\\", letter + "\\piper.exe", letter + "\\" + rel_model)
    return _PIPER_STATE["rt"]


def tts_piper(text, samplerate=PLAY_SR):
    rt = _piper_runtime()
    if not rt:
        raise RuntimeError("piper 未安装（缺 piper.exe 或 voice 模型）")
    base, exe, model = rt
    txt = os.path.join(base, "say.txt")
    wav = os.path.join(base, "say.wav")
    with open(txt, "w", encoding="utf-8") as f:
        f.write(text)
    if os.path.exists(wav):
        try:
            os.remove(wav)
        except OSError:
            pass
    with open(txt, "rb") as fin:
        r = subprocess.run([exe, "-m", model, "-f", wav], stdin=fin,
                           capture_output=True, timeout=90, cwd=base)
    if r.returncode != 0 or not os.path.exists(wav):
        msg = (r.stderr or b"")[-200:].decode("utf-8", "replace")
        raise RuntimeError("piper 合成失败: " + msg)
    with open(wav, "rb") as f:
        return wav_to_pcm(f.read(), samplerate=samplerate)


def wav_to_pcm(wav_bytes, samplerate=SR):
    """WAV → float32 立体声 PCM（纯 python，不依赖 ffmpeg）。"""
    import io
    import wave as _wave

    import numpy as np

    with _wave.open(io.BytesIO(wav_bytes), "rb") as w:
        ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        data = w.readframes(n)
    if sw == 2:
        arr = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 1:
        arr = (np.frombuffer(data, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sw == 4:
        arr = np.frombuffer(data, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise RuntimeError("不认识的 wav 采样宽度：%s" % sw)
    if ch > 1 and arr.size:
        arr = arr.reshape(-1, ch).mean(axis=1)
    if sr != samplerate and arr.size > 1:
        tgt = max(1, int(round(arr.size * samplerate / float(sr))))
        arr = np.interp(np.linspace(0, arr.size - 1, tgt), np.arange(arr.size), arr).astype(np.float32)
    if arr.size == 0:
        return np.zeros((0, 2), dtype=np.float32)
    return np.repeat(arr[:, None], 2, axis=1)


def tts_sapi_wav(text):
    """Windows 自带语音合成（离线、零 API）。返回 WAV 字节。

    没配豆包 key 时用这个 —— 音色是「Microsoft Huihui」，比豆包机械，但不需要任何账号。
    """
    import tempfile

    fd, txt_path = tempfile.mkstemp(suffix=".txt", dir=TMP if os.path.isdir(TMP) else None)
    os.close(fd)
    wav_path = txt_path[:-4] + ".wav"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text)

    def q(p):
        return p.replace("'", "''")

    ps = (
        "$ErrorActionPreference='Stop';"
        "Add-Type -AssemblyName System.Speech;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.Rate=0;$s.Volume=100;"
        "$zh=$s.GetInstalledVoices()|Where-Object{$_.VoiceInfo.Culture.Name -eq 'zh-CN'}|Select-Object -First 1;"
        "if($zh){$s.SelectVoice($zh.VoiceInfo.Name)};"
        "$s.SetOutputToWaveFile('__WAV__');"
        "$s.Speak([IO.File]::ReadAllText('__TXT__',[Text.Encoding]::UTF8));"
        "$s.Dispose()"
    ).replace("__WAV__", q(wav_path)).replace("__TXT__", q(txt_path))
    try:
        subprocess.run(
            [feiyu_paths.POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, timeout=90)
        with open(wav_path, "rb") as f:
            return f.read()
    finally:
        for p in (txt_path, wav_path):
            try:
                os.remove(p)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- 说
class Speaker:
    def __init__(self):
        self.lock = threading.Lock()
        self.muted = False
        self.last = {"text": "", "target": "", "at": 0, "bytes": 0}
        self.count = 0
        self.q = queue.Queue()
        self.pending = 0
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        while True:
            text, target, voice, rate, pitch = self.q.get()
            self.pending = self.q.qsize()
            try:
                self.say(text, target=target, voice=voice, speech_rate=rate, pitch=pitch)
            except Exception as exc:  # noqa: BLE001
                print("[say err] %r" % exc, flush=True)

    def enqueue(self, text, target="cable", voice=TTS_SPEAKER, speech_rate=2, pitch=2):
        if self.muted:
            return {"ok": False, "error": "muted"}
        self.q.put((text, target, voice, speech_rate, pitch))
        self.pending = self.q.qsize()
        return {"ok": True, "queued": True, "pending": self.pending}

    def say(self, text, target="cable", voice=TTS_SPEAKER, speech_rate=2, pitch=2):
        if self.muted:
            return {"ok": False, "error": "muted"}
        devs = resolve_targets(target)
        if not devs:
            return {"ok": False, "error": "没有找到虚拟声卡（CABLE Input），先装 VB-CABLE 或用 target=speaker"}
        t0 = time.time()
        if TTS_KEY:
            engine = "doubao"
            pcm = tts_pcm(text, speaker=voice, speech_rate=speech_rate,
                          pitch=pitch, samplerate=PLAY_SR)
            audio = b""
        elif _piper_runtime():
            engine = "piper"   # 没配 key 但有 piper：本地离线合成，不联网
            pcm = tts_piper(text, samplerate=PLAY_SR)
            audio = b""
        else:
            engine = "sapi"   # 都没有：Windows 自带语音（低完整性进程里通常不可用）
            audio = tts_sapi_wav(text)
            pcm = wav_to_pcm(audio, samplerate=PLAY_SR)
        SPOKEN.append((time.time(), text))
        t1 = time.time()
        global PLAY_SINCE, PLAY_UNTIL
        PLAY_SINCE = time.time()
        PLAY_UNTIL = PLAY_SINCE + len(pcm) / float(PLAY_SR)
        with self.lock:
            if len(devs) == 1:
                devs[0].play(pcm, samplerate=PLAY_SR)
            else:
                ths = [threading.Thread(target=d.play, args=(pcm,), kwargs={"samplerate": PLAY_SR})
                       for d in devs]
                for th in ths:
                    th.start()
                for th in ths:
                    th.join()
        PLAY_UNTIL = time.time() + 0.45
        self.count += 1
        self.last = {"text": text, "target": " + ".join(str(d.name) for d in devs),
                     "at": int(time.time() * 1000), "engine": engine,
                     "bytes": len(audio) or int(pcm.size * 2), "synthSec": round(t1 - t0, 2),
                     "totalSec": round(time.time() - t0, 2)}
        return {"ok": True, **self.last}


# ---------------------------------------------------------------- 听
class Listener:
    def __init__(self, speaker=None, device_name="", source="team"):
        self.speaker = speaker or find_speaker()
        self.device_name = device_name or str(self.speaker.name)
        self.source = source      # team=队友（回环）｜owner=主人本人（物理麦克风）
        self.stop_flag = threading.Event()
        self.segments = 0
        self.texts = 0
        self.last_seg_ts = 0     # 最近一次 VAD 切出片段的时间（含被过滤的），用于判断「对局还在不在」
        self.last_text_ts = 0    # 最近一次「真听到人话」的时间
        self.recent = []
        self.error = ""
        self.asr = None
        self.vad = None
        self.started_at = 0
        self.thread = None

    def _load_asr(self):
        import sherpa_onnx
        rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=MODEL, tokens=TOKENS, num_threads=2, use_itn=True, language="zh")
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = VAD
        cfg.silero_vad.threshold = 0.5
        cfg.silero_vad.min_silence_duration = 0.35
        cfg.silero_vad.min_speech_duration = 0.45
        cfg.silero_vad.max_speech_duration = 12
        cfg.silero_vad.window_size = 512
        cfg.sample_rate = ASR_SR
        self.asr = rec
        self.vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=40)

    def transcribe(self, samples):
        stream = self.asr.create_stream()
        stream.accept_waveform(ASR_SR, samples)
        self.asr.decode_stream(stream)
        text = TAG.sub("", getattr(stream.result, "text", "") or "")
        text = re.sub(r"\s+", " ", text).strip()
        if text in NOISE:
            return ""
        if len(re.sub(r"[，。！？、,.!?~\s]", "", text)) < 2:
            return ""
        return text

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag.set()

    def run(self):
        import warnings
        warnings.filterwarnings("ignore", message="data discontinuity in recording")
        import numpy as np
        sc = _sc()
        try:
            self._load_asr()
        except Exception as exc:  # noqa: BLE001
            self.error = "ASR 加载失败：%r" % exc
            return
        mic = sc.get_microphone(id=self.device_name, include_loopback=True)
        self.started_at = int(time.time() * 1000)
        os.makedirs(LOG_DIR, exist_ok=True)
        chunk = 4096  # 48k 下约 85ms
        try:
            with mic.recorder(samplerate=SR, channels=2) as rec:
                while not self.stop_flag.is_set():
                    data = rec.record(numframes=chunk)
                    mono = data.mean(axis=1)
                    # 48k -> 16k：三点平均，压一点混叠
                    n = mono.shape[0] // 3 * 3
                    pcm16 = mono[:n].reshape(-1, 3).mean(axis=1).astype(np.float32)
                    self.vad.accept_waveform(pcm16)
                    while not self.vad.empty():
                        seg = self.vad.front
                        samples = list(seg.samples)
                        self.vad.pop()
                        self.segments += 1
                        self.last_seg_ts = int(time.time() * 1000)
                        dur = len(samples) / ASR_SR
                        if dur < 0.8:      # 枪炮/爆炸常被切出很短的碎片，直接丢
                            continue
                        # 自己正在/刚刚播报：这段音频里混着自己的声音，别当队友说话
                        # （TTS 从耳机外放，回环会原样录回来，ASR 还会把队友的话和自己的话拼成一句）
                        now_t = time.time()
                        # PLAY_SINCE 为 0 表示本进程还没说过话——这时不能拿 0 当窗口，
                        # 否则「片段起点 < 0.45」恒成立，开局所有人声都会被当成自听丢掉。
                        if PLAY_SINCE > 0 and now_t > PLAY_SINCE - 0.25 and (now_t - dur) < PLAY_UNTIL + 0.45:
                            print("[播报中丢弃] %.1fs" % dur, flush=True)
                            continue
                        text = self.transcribe(samples)
                        if not text:
                            continue
                        if not plausible_speech(text):
                            print("[非人声丢弃] %s" % text, flush=True)
                            continue
                        if is_self_echo(text):
                            print("[自听丢弃] %s" % text, flush=True)
                            continue
                        self.texts += 1
                        self.last_text_ts = int(time.time() * 1000)
                        item = {"ts": int(time.time() * 1000), "dur": round(dur, 2), "text": text,
                                "src": self.source}
                        self.recent.append(item)
                        self.recent = self.recent[-200:]
                        RECENT.append(item)
                        del RECENT[:-200]
                        with open(COMMS, "a", encoding="utf-8") as f:
                            f.write(json.dumps(item, ensure_ascii=False) + "\n")
                        print("[听到·%s] %s" % (self.source, text), flush=True)
        except Exception as exc:  # noqa: BLE001
            self.error = "采集失败：%r" % exc
            print("[bridge] %s" % self.error, flush=True)

    def status(self):
        return {"device": self.device_name, "segments": self.segments, "texts": self.texts,
                "recent": self.recent[-5:], "error": self.error,
                "lastAudioTs": self.last_seg_ts,
                "lastTextTs": self.last_text_ts,
                "uptimeSec": round((time.time() * 1000 - self.started_at) / 1000, 1) if self.started_at else 0}


# ---------------------------------------------------------------- HTTP
class Bridge:
    def __init__(self):
        self.speaker = Speaker()
        self.listener = Listener()
        # 主人的物理麦克风：他亲口说的指令从这里进来（src=owner）
        owner_dev = find_owner_mic()
        self.owner = None
        if owner_dev and owner_dev != self.listener.device_name:
            try:
                self.owner = Listener(device_name=owner_dev, source="owner")
            except Exception as exc:  # noqa: BLE001
                print("[bridge] 主人麦克风不可用：%r" % exc, flush=True)
        self.queue = queue.Queue()

    def handler(self):
        bridge = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, obj, code=200):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                try:
                    if u.path == "/status":
                        return self._send({"speaker": bridge.speaker.last,
                                           "count": bridge.speaker.count,
                                           "muted": bridge.speaker.muted,
                                           "listen": bridge.listener.status(),
                                           "owner": bridge.owner.status() if bridge.owner else None,
                                           "devices": list_devices()})
                    if u.path == "/comms":
                        limit = int(q.get("limit", ["20"])[0])
                        src = q.get("src", [""])[0]
                        items = RECENT
                        if src:
                            items = [x for x in items if x.get("src", "team") == src]
                        return self._send({"items": items[-limit:]})
                    if u.path == "/say":
                        text = q.get("text", [""])[0]
                        if not text:
                            return self._send({"ok": False, "error": "text 为空"}, 400)
                        target = q.get("target", ["cable"])[0]
                        voice = q.get("voice", [TTS_SPEAKER])[0]
                        rate = int(q.get("rate", ["2"])[0])
                        pitch = int(q.get("pitch", ["2"])[0])
                        if q.get("async", ["0"])[0] in ("1", "true", "yes"):
                            return self._send(bridge.speaker.enqueue(text, target=target,
                                                                    voice=voice, speech_rate=rate,
                                                                    pitch=pitch))
                        return self._send(bridge.speaker.say(text, target=target, voice=voice,
                                                             speech_rate=rate, pitch=pitch))
                    if u.path == "/inject":
                        text = q.get("text", [""])[0]
                        if not text:
                            return self._send({"ok": False, "error": "text 为空"}, 400)
                        item = {"ts": int(time.time() * 1000), "dur": 1.0,
                                "text": text, "fake": True, "src": q.get("src", ["team"])[0]}
                        bridge.listener.recent.append(item)
                        bridge.listener.recent = bridge.listener.recent[-200:]
                        RECENT.append(item)
                        del RECENT[:-200]
                        bridge.listener.last_text_ts = item["ts"]   # 让「对局是否还在」的判据也吃这条
                        with open(COMMS, "a", encoding="utf-8") as f:
                            f.write(json.dumps(item, ensure_ascii=False) + "\n")
                        return self._send({"ok": True, "item": item})
                    if u.path == "/mute":
                        bridge.speaker.muted = True
                        return self._send({"ok": True, "muted": True})
                    if u.path == "/unmute":
                        bridge.speaker.muted = False
                        return self._send({"ok": True, "muted": False})
                    return self._send({"error": "unknown"}, 404)
                except Exception as exc:  # noqa: BLE001
                    return self._send({"ok": False, "error": repr(exc)}, 500)

        return H

    def serve(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.listener.start()
        if self.owner:
            self.owner.start()
            print("[bridge] 主人麦克风：%s" % self.owner.device_name, flush=True)
        srv = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), self.handler())
        print("[bridge] 监听 %s，控制口 http://127.0.0.1:%d" % (self.listener.device_name, HTTP_PORT),
              flush=True)
        srv.serve_forever()


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]
    if cmd == "devices":
        print(json.dumps(list_devices(), ensure_ascii=False, indent=1))
    elif cmd == "serve":
        Bridge().serve()
    elif cmd == "say":
        text = args[1]
        target = "cable"
        if "--target" in args:
            target = args[args.index("--target") + 1]
        print(json.dumps(Speaker().say(text, target=target), ensure_ascii=False))
    elif cmd == "comms":
        n = int(args[1]) if len(args) > 1 else 10
        if not os.path.exists(COMMS):
            print("[]")
            return
        lines = open(COMMS, encoding="utf-8").read().strip().splitlines()[-n:]
        print("\n".join(lines))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
