# @local/dsh-feiyu-commander —— 肥鱼指挥官（纯会话版）

把「听队友/主人说话 → 看局势 → 说进麦克风」这套能力挂进 DSH 会话，
**指挥由当前会话里的大模型做**，不带后台 python 导演。

- 插件 id：`feiyu-commander`
- 包名：`@local/dsh-feiyu-commander`
- 设置页入口：设置 → **肥鱼指挥官**

## 工具（9 个）

| 工具 | 说明 |
| --- | --- |
| `commander_begin` | 拿提示词 + 指挥方法 + 当前局势 + 最近听到的话 |
| `commander_watch` | 等新人声（长轮询），带屏幕状态与 `owner`/`team` 分组 |
| `commander_say` | 说一句（`target`: `both` / `cable` / `speaker`） |
| `commander_shot` | 抓一张游戏画面（需要 OBS） |
| `commander_status` | 桥接设备、片段/转写数、主人麦克风 |
| `commander_start` / `commander_stop` | 起停语音桥 |
| `commander_log` | 桥接日志尾巴 |
| `commander_end` | 收工 |

## 配置

插件配置（`cordis.patch.yml` 的 `config`）由安装器按本机路径写入：

`home` / `python` / `toolsDir` / `pylibs` / `logDir` / `models` / `ffmpeg` / `node` / `port`

## 安装 / 卸载

用外层的 `安装.bat` / `卸载.bat`，或 `python install.py [--uninstall]`。
细节见外层 `README.md`。
