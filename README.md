# 肥鱼指挥官 · dsh-feiyu-commander

给 DSH 会话装一个**能听、会说、能看屏幕**的 AI 战场指挥官。装好之后，你在《三角洲行动》里打，
它听队友语音和你亲口下的令，用语音把具体指令播到游戏里（或只念给你听）。

本包**不含任何密钥**。语音合成需要你自己的豆包（火山引擎）API Key，见下文「语音怎么配」。

---

## 1. 它由三部分组成

| 部分 | 是什么 | 在哪 |
| --- | --- | --- |
| 插件 | DSH 设置页 + 9 个 `commander_*` 工具 | `<安装目录>\plugin` |
| 语音桥 | WASAPI 回环听游戏声、麦克风听你、TTS 说话 | `<安装目录>\tools\audio-bridge.py` |
| 指挥手册 | 怎么循环、怎么说话、怎么收工 | `<安装目录>\tools\COMMANDER.md` |

能力一览：

- 听游戏声（WASAPI 回环，**不需要**立体声混音，也不需要装虚拟声卡）
- 听队友语音（同一路回环，带 `[队友]` 标签）
- 听你亲口下令（物理麦克风，带 `[主人]` 标签，优先级最高）
- 说话（豆包 TTS → 扬声器 / 虚拟麦）
- 看屏幕（Windows 自带 OCR 读票数、倒计时、阵营）
- 抓画面（可选，需要 OBS；窗口被盖住也能抓）
- 进队内语音（可选，需要 VB-CABLE 或 VoiceMeeter 的虚拟输入）

---

## 2. 装之前需要什么

必需：

- Windows 10 / 11
- DSH Desktop（**至少启动过一次**，本安装器要往它的 profile 里注册插件）
- Python 3.10+（装的时候勾选 *Add python.exe to PATH*）
- 一个麦克风、一个耳机/扬声器

可选：

- **豆包 TTS Key** —— 想让它说话就得有
- **OBS Studio** —— 想让它「看一眼画面」就得有（OBS 32+，开启 WebSocket）
- **VB-CABLE / VoiceMeeter** —— 想让队友也听见它说话
- **SenseVoice 模型**（约 250MB）—— 想让它听懂人话就得有，见第 5 节

---

## 3. 三步安装

1. 把整个 `feiyu-commander-kit` 文件夹解压到任意位置（路径里可以有中文）
2. 双击 **`安装.bat`**（默认装到 `%USERPROFILE%\feiyu-commander`，也可以右键用管理员运行）
3. **重启 DSH Desktop** → 打开 设置 → 左侧出现「肥鱼指挥官」

安装器会做这些事（都可重复执行）：

- 复制 `plugin\` 和 `tools\` 到安装目录
- 建独立 python 环境 `.venv`，装 numpy / sherpa-onnx / soundcard / comtypes
- 在 DSH 的 profile 里建 junction、写 `dependencies` 和 `dsh.profile.bundles`（备份 `package.json.bak-feiyu`）
- 把真实路径写进插件的 `cordis.patch.yml`
- 生成 `_capture\session-settings.json`（人设、提示词、绑定会话）
- 找模型：优先复用 DSH 已装的 SenseVoice，其次 `--download-models`

命令行用法：

```bat
python install.py --dry-run              :: 只检查，不改文件
python install.py --target D:\feiyu      :: 换安装目录
python install.py --download-models      :: 顺带下 ASR 模型
python install.py --skip-deps            :: 跳过 pip（已经装过依赖）
python install.py --uninstall            :: 卸载
```

---

## 4. 语音怎么配（TTS）

**有豆包 Key（推荐）**：打开 设置 →「肥鱼指挥官」→ 提示词上方有「语音合成 Key」，
填进去保存；或者把 key 写进 `<安装目录>\_capture\voice.json`：

```json
{ "apiKey": "你自己的火山引擎 key" }
```

音色默认是 `zh_female_sajiaoxuemei_uranus_bigtts`（撒娇学妹），在 `audio-bridge.py` 顶部的
`TTS_SPEAKER` 改。

**没有 Key**：不报错，但说话会退回 Windows 自带语音（SAPI）。实测在 DSH 拉起的子进程里
（Low 完整性）**SAPI 和 WinRT 语音都用不了**，只会打一行警告、不出声。想听声音就配 Key。

---

## 5. 模型（听懂人话）

语音识别用 sherpa-onnx + SenseVoice，需要三个文件：

```
<安装目录>\models\sensevoice-onnx\model.int8.onnx   (约 228MB)
<安装目录>\models\sensevoice-onnx\tokens.txt
<安装目录>\models\silero\silero_vad.onnx
```

三种拿法：

1. **复用**：装过 DSH 的语音插件，模型在 `%USERPROFILE%\.dsh\speech-to-text\sensevoice\models`，
   安装器会自动拷过来
2. **自动下**：`python install.py --download-models`（GitHub，约 250MB）
3. **手动**：从 [sherpa-onnx releases](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models)
   下 `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2` 和 `silero_vad.onnx`，按上面的路径摆好

没有模型也能用，只是「听」这一路是空的（会打警告）。

---

## 6. 用起来

1. 双击 **`启动.bat`** —— 起语音桥（监听耳机回环 + 麦克风），约 2 秒出体检表
2. 打开 DSH → 设置 →「肥鱼指挥官」：
   - **绑定会话**：选你要它待着的那个会话（下拉里显示标题）
   - **提示词**：默认已经填好（247 字，指向 COMMANDER.md）
   - 保存
3. 在**那个会话**里说「开始指挥」，或者回设置页点 **「立刻开始指挥」**
4. 它会开始循环：`commander_watch` → 判断 → `commander_say` → 再 watch

会话里它手上有 9 个工具：

| 工具 | 干什么 |
| --- | --- |
| `commander_begin` | 拿提示词 + 方法 + 当前局势 + 最近听到的话 |
| `commander_watch` | 等新人声（长轮询），顺带给屏幕状态 |
| `commander_say` | 说一句（`target`: `both` / `cable` / `speaker`） |
| `commander_shot` | 抓一张游戏画面（要 OBS） |
| `commander_status` | 桥接在不在、听的哪个设备、说了多少句 |
| `commander_start` / `commander_stop` | 起停语音桥 |
| `commander_log` | 看桥接日志尾巴 |
| `commander_end` | 收工（不恢复现场） |

---

## 7. 目录结构

```
feiyu-commander-kit\
├─ 安装.bat / 启动.bat / 卸载.bat
├─ install.py               安装器（只用标准库）
├─ requirements.txt
├─ voice.example.json       语音 Key 示例（改成 voice.json 用）
├─ plugin\                  DSH 插件（host.js + client.js + 配置）
└─ tools\
   ├─ feiyu_paths.py        路径解析（换目录只改环境变量）
   ├─ audio-bridge.py       语音桥（听 + 说）
   ├─ session-cmd.py        begin / watch / say / shot / screen
   ├─ screen_state.py       屏幕状态（OCR）
   ├─ screen-ocr.ps1        Windows OCR 的 PowerShell 实现
   ├─ obs-setup.mjs         OBS 控制（抓帧/绑窗口）
   ├─ auto-trigger.py       「开始行动」哨兵（默认关闭）
   ├─ boot.py               一键启动体检
   ├─ check-env.py          环境体检
   └─ COMMANDER.md          指挥手册（它会先读这个）
```

装好之后你的数据在 `<安装目录>\_capture\`：

- `comms.jsonl` —— 听到的每一句（`src`: `team` / `owner`）
- `bridge.log` / `auto-trigger.log`
- `session-settings.json` —— 设置页存的东西
- `voice.json` —— 你的 TTS Key（**不要外发这个文件**）

---

## 8. 常见问题

**设置页里没有「肥鱼指挥官」**
重启 DSH。插件是在启动时加载的。

**它不说话**
看 `_capture\bridge.log`：没配 Key 会写「没有 TTS key」；配了 Key 还不出声，看是不是扬声器设备名变了。

**它听不到队友**
桥接默认监听**默认扬声器**的回环。耳机和扬声器不是同一个设备时，声音从哪个设备出就得监听哪个。

**它听不到我**
看 `启动.bat` 的体检表里「主人麦克风」那一行，应该是你的物理麦（如「麦克风阵列 (Realtek(R) Audio)」）。
不对就在 `audio-bridge.py` 的 `OWNER_KEYS` 里加你的设备名关键词。

**队友听不到它**
需要虚拟麦：装 VB-CABLE 或 VoiceMeeter，游戏里把麦克风选成 `VoiceMeeter Output (2- ...)`，
然后 `commander_say{target:"both"}`。没装的话只能 `target:"speaker"` 念给你自己听。

**绑定会话老是变**
设置页只会保存**磁盘上真实存在**的会话 id，选错了会红字提示、不会静默改掉。

**pip 装不动**
安装器失败会自动换清华源；还不行就手动：
`.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`

**不想用豆包 Key，能离线出声吗**
能：装的时候加 --with-piper（约 82MB），会下 piper + 中文音色到 piper\。
之后不填 Key 就用 piper（音质一般但免费、离线）。合成一句约 0.7–1 秒。
安装路径带中文也没事：启动.bat 会临时给它映射一个盘符（piper 只认 ASCII 路径）。

**它说「系统上没有安装语音」**
Windows 自带语音在 DSH 的子进程里被完整性级别挡住，属正常现象。配豆包 Key 即可。

---

## 9. 卸载

双击 **`卸载.bat`**，或：

```bat
python install.py --uninstall
```

会删掉 DSH profile 里的插件条目和安装目录（`--keep-files` 可保留目录）。
**不会**动你的 `_capture` 录音和 `voice.json`（如果它们在别处）。

---

## 10. 已知限制

- 抓画面依赖 OBS；OBS 窗口最小化时抓不到（被别的窗口盖住可以）
- 屏幕 OCR 只认 `zh-Hans-CN`，且小字需要放大才能读准
- 语音延迟：说一句约 1.5–3 秒（TTS 合成 + 播放）
- 听和说走同一条回环，所以它说话期间会主动丢弃「自己听到的自己」
- DSH 重启后插件才更新；`tools\` 里的 python 改动立即生效
