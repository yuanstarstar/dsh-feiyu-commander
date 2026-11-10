/**
 * @local/dsh-feiyu-commander —— 肥鱼指挥官官（纯会话版）
 *
 * 只负责两件事：
 *   1. 把音频桥接（audio-bridge.py serve）拉起来/停掉 —— 听队友+主人、说进虚拟麦；
 *   2. 把「取局势 / 等语音 / 说一句」这几个动作做成工具，指挥由当前 DSH 会话里的大模型做。
 *
 * 这里**没有**后台 python 导演：不 spawn director.py、不管 director.pid、没有攻守模式轮播。
 *
 *   audio-bridge.py serve   回环采集(耳机)+主人麦克风 + SenseVoice 转写 + 豆包 TTS，控制口 127.0.0.1:8766
 *   session-cmd.py          begin/watch/say/screen，供本插件调用
 *
 * 工具：
 *   commander_begin   接管：确保桥接在跑，返回提示词+方法+局势+最近听到
 *   commander_watch   长轮询等新语音/阶段变化（默认 25 秒）
 *   commander_say     让大肥鱼说一句（both=虚拟麦+耳机）
 *   commander_end     结束会话指挥（只收手，不杀桥接）
 *   commander_status  看桥接运行态
 *   commander_start   只起桥接
 *   commander_stop    停桥接
 *   commander_log     看桥接日志
 */
import { spawn, spawnSync } from 'node:child_process'
import { createWriteStream, existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { zstdDecompressSync } from 'node:zlib'

export const name = 'feiyu-commander'

const HERE = dirname(fileURLToPath(import.meta.url))

/**
 * 路径默认值：本机老路径存在就用老的（不破坏现有安装），否则回退到可移植写法。
 * 换机器时不用改代码 —— 安装脚本会把实际路径写进 cordis.patch.yml 的 config。
 */
// 默认装到用户目录下的 feiyu-commander；安装器会把真实路径写进 cordis.patch.yml 的 config（FEIYU_HOME）
const HOME_DIR = process.env.FEIYU_HOME || join(homedir(), 'feiyu-commander')
const USERPROFILE = process.env.USERPROFILE || ''
const LEGACY_PY = USERPROFILE ? `${USERPROFILE}\\.dsh\\bili-duty\\.venv\\Scripts\\python.exe` : ''

const DEFAULTS = {
  home: HOME_DIR,
  python: process.env.FEIYU_PYTHON || (existsSync(LEGACY_PY) ? LEGACY_PY : 'python'),
  toolsDir: `${HOME_DIR}\\tools`,
  pylibs: `${HOME_DIR}\\pylibs`,
  logDir: `${HOME_DIR}\\_capture`,
  models: `${USERPROFILE}\\.dsh\\speech-to-text\\sensevoice\\models`,
  ffmpeg: `${USERPROFILE}\\.dsh\\tools\\ffmpeg\\bin\\ffmpeg.exe`,
  node: '',
  ttsKey: '',
  port: 8766,
  llm: true,
  autoStart: false,
  target: 'both',
  mode: 'defend',
}

const TEXT_OUTPUT = {
  schema: {
    type: 'object',
    properties: { text: { type: 'string' } },
    required: ['text'],
  },
  render: (_args, value) => [{ type: 'text', text: String(value?.text ?? '') }],
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const alive = (pid) => {
  if (!pid) return false
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

/**
 * 设置页可改的东西（人设提示词 / 发进会话的提示词 / 绑定会话 / 说话目标 / 自动触发）。
 * 存在 <logDir>/session-settings.json。
 * 首次启动时若老插件的 commander-settings.json 还在，就从它那里继承一份，
 * 免得升级后提示词和绑定会话丢了。
 */
const SETTINGS_DEFAULT = {
  // 人设：会话里生成指令时读它（拼在提示词前面）
  prompt: '',
  // 点「立刻开始指挥」/ 检测到「开始行动」被点时，发进会话的那段话
  startPrompt: '',
  // 检测到大厅→战斗（「开始行动」消失）时自动发 startPrompt
  // 默认关闭：提示词一律手动点「立刻开始指挥」发，避免一进局就自动接管
  autoOnStart: false,
  sessionId: '',
  target: 'both',
  // 说话模式：auto=队友喊话就接；owner=没有主人命令就不说话
  speakMode: 'auto',
  llm: true,
}

/** 把 --D-my-project-- 这种目录名还原成 D:\my-project，只为显示好看 */
function decodeWorkspaceDir(dir) {
  let s = String(dir || '')
  s = s.replace(/^-+/, '').replace(/-+$/, '')
  s = s.replace(/~([0-9A-Fa-f]{4})/g, (_, hex) => String.fromCharCode(parseInt(hex, 16)))
  // 剩下的单个 '-' 是原路径里的分隔符（D:\x → D-x），补回冒号和斜杠
  const m = /^([A-Za-z])-(.*)$/.exec(s)
  if (m) s = `${m[1]}:\\${m[2]}`
  return s
}

/** 列出最近改动过的 DSH 会话，供设置页下拉选择 */
const TITLE_CACHE = new Map()
const ZSTD_MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd])

/** 会话文件是一串 zstd 帧，逐帧解到出现第一条用户消息为止，拿它当标题 */
function sessionTitle(file) {
  let st
  try {
    st = statSync(file)
  } catch {
    return ''
  }
  const key = `${file}:${st.size}:${Math.round(st.mtimeMs)}`
  if (TITLE_CACHE.has(key)) return TITLE_CACHE.get(key)
  let title = ''
  try {
    const raw = readFileSync(file)
    let pos = 0
    let text = ''
    while (text.length < 3_000_000) {
      const start = raw.indexOf(ZSTD_MAGIC, pos)
      if (start < 0) break
      const next = raw.indexOf(ZSTD_MAGIC, start + 4)
      const end = next < 0 ? raw.length : next
      pos = end
      try {
        text += zstdDecompressSync(raw.subarray(start, end)).toString('utf8')
      } catch {
        break
      }
      const hit = text.split('\n').find((l) => l.includes('"kind":"user"'))
      if (hit) {
        try {
          const obj = JSON.parse(hit)
          const ins = obj?.data?.inserted
          const first = Array.isArray(ins) ? ins[0] : null
          const c = first?.content?.[0]?.text
          if (typeof c === 'string' && c.trim()) title = c.trim().replace(/\s+/g, ' ').slice(0, 40)
        } catch {
          /* 不是我们要的那条 */
        }
        break
      }
    }
  } catch {
    /* ignore */
  }
  TITLE_CACHE.set(key, title)
  return title
}

function listSessions(limit = 30) {
  const root = join(homedir(), '.dsh', 'sessions')
  const out = []
  let dirs = []
  try {
    dirs = readdirSync(root, { withFileTypes: true }).filter((d) => d.isDirectory())
  } catch {
    return out
  }
  for (const ws of dirs) {
    let kids = []
    try {
      kids = readdirSync(join(root, ws.name), { withFileTypes: true }).filter((d) => d.isDirectory())
    } catch {
      continue
    }
    for (const k of kids) {
      const full = join(root, ws.name, k.name)
      // 目录的 mtime 只在增删文件时变，要用会话文件本身的 mtime 才准
      const file = join(full, 'session.v4.jsonl.zstd')
      let st
      try {
        st = statSync(file)
      } catch {
        try {
          st = statSync(full)
        } catch {
          continue
        }
      }
      out.push({
        // ⚠️ 客户端 sessions.retain 认的是 "session-<uuid>"，裸 uuid 会报
        // "sessions.retain: unknown session"（21:14 那次 diag 的根因）。
        // 所以这里直接给带前缀的 id，另给 bare 供显示/兼容。
        id: k.name,
        bare: k.name.replace(/^session-/, ''),
        dir: k.name,
        ws: ws.name,
        workspace: decodeWorkspaceDir(ws.name),
        mtime: st.mtimeMs,
      })
    }
  }
  out.sort((a, b) => b.mtime - a.mtime)
  const top = out.slice(0, limit)
  // 标题要解压，只给前几条算（其余靠时间认）
  for (const s of top.slice(0, 20)) {
    s.title = sessionTitle(join(root, s.ws, s.dir, 'session.v4.jsonl.zstd'))
  }
  return top
}

/** 全部会话 id（只 readdir，不算标题、不排序；用来校验绑定会话是否存在） */
function allSessionIds() {
  const root = join(homedir(), '.dsh', 'sessions')
  const ids = new Set()
  let dirs = []
  try {
    dirs = readdirSync(root, { withFileTypes: true }).filter((d) => d.isDirectory())
  } catch {
    return ids
  }
  for (const ws of dirs) {
    try {
      for (const k of readdirSync(join(root, ws.name), { withFileTypes: true })) {
        if (k.isDirectory()) ids.add(k.name)
      }
    } catch {
      /* 跳过没权限的工作区 */
    }
  }
  return ids
}

function killTree(pid) {
  if (!pid) return
  try {
    spawn('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
  } catch {
    /* ignore */
  }
}

export function apply(ctx, config) {
  const cfg = { ...DEFAULTS, ...(config ?? {}) }
  const log = (msg) => {
    try {
      ctx.logger?.info?.(`[commander] ${msg}`)
    } catch {
      /* ignore */
    }
  }

  const state = { bridge: null }
  const base = () => `http://127.0.0.1:${cfg.port}`
  const logFile = (n) => join(cfg.logDir, n)

  mkdirSync(cfg.logDir, { recursive: true })

  const settingsFile = () => join(cfg.logDir, 'session-settings.json')
  const legacySettingsFile = () => join(cfg.logDir, 'commander-settings.json')
  const loadSettings = () => {
    // 第一次跑（新文件还不存在）时，从老插件的设置里继承一份，别让提示词/绑定会话丢了
    if (!existsSync(settingsFile()) && existsSync(legacySettingsFile())) {
      try {
        const old = JSON.parse(readFileSync(legacySettingsFile(), 'utf-8'))
        const seed = {}
        for (const k of Object.keys(SETTINGS_DEFAULT)) {
          if (old[k] !== undefined) seed[k] = old[k]
        }
        writeFileSync(settingsFile(), JSON.stringify({ ...SETTINGS_DEFAULT, ...seed }, null, 2), 'utf-8')
      } catch {
        /* 继承失败就用默认值 */
      }
    }
    try {
      return { ...SETTINGS_DEFAULT, ...JSON.parse(readFileSync(settingsFile(), 'utf-8')) }
    } catch {
      return { ...SETTINGS_DEFAULT }
    }
  }
  const saveSettings = (patch) => {
    const cur = loadSettings()
    const next = { ...cur, ...(patch && typeof patch === 'object' ? patch : {}) }
    // 绑定会话只认真实存在的会话 id：旧页面/测试脚本把别的 id 写进来时保持原值，
    // 免得「绑定的会话自己变了」。
    // ⚠️ 必须用「全部会话」校验，不能只看最近 60 条——否则选了个老会话会被静默驳回，
    // 看起来就像绑定自己跑回去了。
    let rejected = false
    if (next.sessionId && next.sessionId !== cur.sessionId) {
      try {
        const ids = allSessionIds()
        if (ids.size && !ids.has(next.sessionId)) {
          next.sessionId = cur.sessionId
          rejected = true
        }
      } catch {
        next.sessionId = cur.sessionId
        rejected = true
      }
    }
    writeFileSync(settingsFile(), JSON.stringify(next, null, 2), 'utf-8')
    return { settings: next, rejected }
  }

  // ── 「开始行动」触发 ──
  // python 哨兵（auto-trigger.py）盯着屏幕：右下角「开始行动」一消失（大厅→战斗）
  // 就写 trigger.json；浏览器里的设置页轮询 /state 看到新的 at，就往会话发 startPrompt。
  const triggerFile = () => join(cfg.logDir, 'trigger.json')
  const readTrigger = () => {
    try {
      return JSON.parse(readFileSync(triggerFile(), 'utf-8'))
    } catch {
      return null
    }
  }
  const writeTrigger = (reason, extra) => {
    const payload = {
      at: Date.now(),
      reason: reason || '手动',
      phase: extra?.phase ?? null,
      side: extra?.side ?? null,
      map: extra?.map ?? null,
      timer: extra?.timer ?? null,
      tickets: extra?.tickets ?? null,
    }
    try {
      writeFileSync(triggerFile(), JSON.stringify(payload, null, 2), 'utf-8')
    } catch {
      /* ignore */
    }
    return payload
  }

  const watcher = { proc: null, pid: 0 }
  const watcherPidFile = () => join(cfg.logDir, 'auto-trigger.pid')
  // 插件被重复加载时会留下多个哨兵，每个都每 5 秒开一次 PowerShell OCR。
  // 起新的之前先把「上一个进程留下的」同命令行进程收掉。
  function killStaleWatchers() {
    try {
      const r = spawnSync(
        'powershell.exe',
        [
          '-NoProfile',
          '-Command',
          "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*auto-trigger.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
        ],
        { windowsHide: true, timeout: 15000 },
      )
      if (r.status !== 0) return false
      return true
    } catch {
      return false
    }
  }
  function startWatcher() {
    if (watcher.proc) return 'already'
    killStaleWatchers()
    try {
      const out = createWriteStream(logFile('auto-trigger.log'), { flags: 'a' })
      const p = spawn(cfg.python, [join(cfg.toolsDir, 'auto-trigger.py')], {
        cwd: cfg.toolsDir,
        env: env(),
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
        detached: true,
      })
      p.stdout.pipe(out)
      p.stderr.pipe(out)
      p.unref()
      watcher.proc = p
      watcher.pid = p.pid ?? 0
      try {
        writeFileSync(watcherPidFile(), String(watcher.pid), 'utf-8')
      } catch {
        /* ignore */
      }
      p.on('exit', () => {
        if (watcher.proc === p) {
          watcher.proc = null
          watcher.pid = 0
          try {
            rmSync(watcherPidFile(), { force: true })
          } catch {
            /* ignore */
          }
        }
      })
      return 'started'
    } catch (e) {
      console.error('[commander] 触发哨兵起不来：', e?.message || e)
      return 'failed'
    }
  }
  function stopWatcher() {
    const p = watcher.proc
    if (!p) return false
    try {
      killTree(p.pid)
    } catch {
      /* ignore */
    }
    watcher.proc = null
    watcher.pid = 0
    return true
  }

  const env = () => ({
    ...process.env,
    PYTHONIOENCODING: 'utf-8',
    PYTHONPATH: cfg.pylibs,
    // 路径统一交给 tools/feiyu_paths.py 解析：换机器只改插件配置，不改代码
    FEIYU_HOME: cfg.home,
    FEIYU_LOG_DIR: cfg.logDir,
    FEIYU_PYLIBS: cfg.pylibs,
    FEIYU_MODELS: cfg.models,
    FEIYU_FFMPEG: cfg.ffmpeg,
    FEIYU_NODE: cfg.node,
    FEIYU_TTS_KEY: cfg.ttsKey || '',
    // session-cmd.py 读这个设置文件（人设/说话目标/绑定会话）
    COACH_SETTINGS_FILE: settingsFile(),
    // 纯会话版：不带后台导演，session-cmd.py 不用去杀 director.pid
    COACH_NO_DIRECTOR: '1',
  })

  async function api(path, opts = {}, timeout = 5000) {
    const r = await fetch(base() + path, { ...opts, signal: AbortSignal.timeout(timeout) })
    if (!r.ok) throw new Error(`HTTP ${r.status} ${path}`)
    return r.json()
  }

  async function status(timeout = 1500) {
    try {
      return await api('/status', {}, timeout)
    } catch {
      return null
    }
  }

  // ── 会话模式：指挥大脑放在会话里，python 导演退场 ──
  const sessionMode = { active: false, since: 0 }
  const runPy = (args, timeout = 60000) => {
    const r = spawnSync(cfg.python, [join(cfg.toolsDir, 'session-cmd.py'), ...args], {
      cwd: cfg.toolsDir,
      env: env(),
      encoding: 'utf-8',
      timeout,
      windowsHide: true,
    })
    const out = String(r.stdout || '').trim()
    try {
      return JSON.parse(out)
    } catch {
      return { ok: false, error: (String(r.stderr || '') + out || '没有输出').slice(-800) }
    }
  }

  function spawnLogged(exe, args, file) {
    const out = createWriteStream(file, { flags: 'a' })
    const p = spawn(exe, args, {
      cwd: cfg.toolsDir,
      env: env(),
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    })
    p.stdout.pipe(out)
    p.stderr.pipe(out)
    p.on('exit', (code) => {
      try {
        out.write(`\n[exit ${code}]\n`)
        out.end()
      } catch {
        /* ignore */
      }
    })
    return p
  }

  async function ensureBridge() {
    const st = await status()
    if (st) return { started: false, status: st }
    if (state.bridge && alive(state.bridge.pid)) {
      // 进程在但还没监听：等一下
      for (let i = 0; i < 12; i++) {
        await sleep(700)
        const s = await status()
        if (s) return { started: false, status: s }
      }
    }
    const p = spawnLogged(cfg.python, [join(cfg.toolsDir, 'audio-bridge.py'), 'serve'], logFile('bridge.log'))
    state.bridge = p
    log(`桥接已启动 pid=${p.pid}`)
    for (let i = 0; i < 20; i++) {
      await sleep(700)
      const s = await status()
      if (s) return { started: true, status: s }
    }
    throw new Error('桥接启动后 14 秒内没监听 8766，看 _capture/bridge.log')
  }

  function tail(file, lines) {
    try {
      const txt = readFileSync(file, 'utf-8')
      return txt.trimEnd().split(/\r?\n/).slice(-lines).join('\n')
    } catch {
      return '（没有日志）'
    }
  }

  ctx.effect(() => () => {
    killTree(state.bridge?.pid)
    stopWatcher()
  }, 'session: dispose')

  // 「开始行动」哨兵：插件起来就挂上（设置里关掉 autoOnStart 就不起）
  if (loadSettings().autoOnStart !== false) {
    const r = startWatcher()
    if (r !== 'failed') log(`开始行动哨兵：${r}`)
  }

  // ── 设置页的后端（照 dsh-bili-toy 的写法：webServer 挂 prefix 路由） ──
  ctx.inject(['webServer'], (wsCtx) => {
    const sendJson = (res, status0, body) => {
      try {
        const buf = Buffer.from(JSON.stringify(body), 'utf8')
        res.writeHead(status0, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' })
        res.end(buf)
      } catch {
        /* 连接断了就算了 */
      }
    }
    const readBody = (req) =>
      new Promise((resolve) => {
        let data = ''
        req.on('data', (c) => {
          data += c
          if (data.length > 1e6) req.destroy()
        })
        req.on('end', () => {
          try {
            resolve(data ? JSON.parse(data) : {})
          } catch {
            resolve({})
          }
        })
        req.on('error', () => resolve({}))
      })
    const sameOriginOk = (req) => {
      try {
        const host = String(req.headers.host || '')
        if (!host) return true
        if (!/^(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$/i.test(host)) return false
        const pick = String(req.headers.origin || req.headers.referer || '')
        if (!pick) return true
        const u = new URL(pick)
        return /^(127\.0\.0\.1|localhost|\[::1\])$/i.test(u.hostname)
      } catch {
        return false
      }
    }

    const runtime = async () => {
      const st = await status(1200)
      const ls = st?.listen ?? {}
      const sp = st?.speaker ?? {}
      return {
        bridge: st ? { device: ls.device ?? '?', segments: ls.segments ?? 0, texts: ls.texts ?? 0, lastTextTs: ls.lastTextTs ?? null } : null,
        speaker: sp.target ?? null,
        owner: st?.owner?.device ?? null,
        sessionMode: sessionMode.active,
        settingsFile: settingsFile(),
      }
    }

    const handle = async (req, res, url) => {
      const p = url.pathname.replace(/^\/api\/feiyu-commander/, '') || '/'
      const method = String(req.method || 'GET').toUpperCase()

      if (method === 'GET' && (p === '/state' || p === '/')) {
        return sendJson(res, 200, {
          ok: true,
          settings: loadSettings(),
          sessions: listSessions(30),
          runtime: await runtime(),
          trigger: readTrigger(),
          watcher: { pid: watcher.pid },
        })
      }
      // 客户端发消息失败时把原因交上来，落到 _capture/client-diag.json 方便事后查
      if (method === 'POST' && p === '/diag') {
        const body = await readBody(req)
        try {
          writeFileSync(
            join(cfg.logDir, 'client-diag.json'),
            JSON.stringify({ ...body, receivedAt: Date.now() }, null, 2),
            'utf8',
          )
        } catch {
          /* 写不了就算了 */
        }
        return sendJson(res, 200, { ok: true })
      }
      // 手动点火（设置页「试一下」或外部脚本用）：写一条 trigger，客户端看到就发提示词
      if (method === 'POST' && p === '/trigger') {
        const body = await readBody(req)
        return sendJson(res, 200, { ok: true, trigger: writeTrigger(body.reason || '手动', body) })
      }
      if (method === 'POST' && p === '/trigger/clear') {
        try {
          rmSync(triggerFile(), { force: true })
        } catch {
          /* ignore */
        }
        return sendJson(res, 200, { ok: true })
      }
      if (method === 'POST' && p === '/watcher') {
        const body = await readBody(req)
        const want = body.on !== false
        return sendJson(res, 200, { ok: true, watcher: { pid: want ? (startWatcher(), watcher.pid) : (stopWatcher(), 0) } })
      }
      if (method === 'POST' && p === '/save') {
        const body = await readBody(req)
        const saved = saveSettings(body.patch || body || {})
        return sendJson(res, 200, { ok: true, settings: saved.settings, sessionIdRejected: saved.rejected })
      }
      if (method === 'POST' && p === '/start') {
        const body = await readBody(req)
        const s = loadSettings()
        try {
          const b = await ensureBridge()
          return sendJson(res, 200, { ok: true, bridge: b.started, target: body.target || s.target, runtime: await runtime() })
        } catch (e) {
          return sendJson(res, 500, { ok: false, error: e?.message || String(e) })
        }
      }
      if (method === 'POST' && p === '/begin') {
        // 切到会话指挥：桥接留着（会话自己听、自己说），这里只翻一下标记
        const body = await readBody(req)
        const s = loadSettings()
        try {
          const b = await ensureBridge()
          sessionMode.active = true
          sessionMode.since = Date.now()
          return sendJson(res, 200, {
            ok: true,
            bridge: b.started ? 'started' : 'reused',
            device: (b.status?.listen ?? {}).device ?? null,
            target: body.target || s.target,
            hint: '回到会话说「开始指挥」，用 commander_begin 接管',
          })
        } catch (e) {
          return sendJson(res, 500, { ok: false, error: e?.message || String(e) })
        }
      }
      if (method === 'POST' && p === '/end') {
        sessionMode.active = false
        return sendJson(res, 200, { ok: true })
      }
      if (method === 'POST' && p === '/stop') {
        killTree(state.bridge?.pid)
        state.bridge = null
        await sleep(400)
        return sendJson(res, 200, { ok: true, runtime: await runtime() })
      }
      if (method === 'GET' && p === '/sessions') {
        return sendJson(res, 200, { ok: true, sessions: listSessions(60) })
      }
      return sendJson(res, 404, { ok: false, error: '没有这个接口：' + p })
    }

    try {
      wsCtx.effect(
        () =>
          wsCtx.webServer.register({
            kind: 'prefix',
            path: '/api/feiyu-commander',
            handler: async (req, res) => {
              if (!sameOriginOk(req)) return sendJson(res, 403, { ok: false, error: '只接受本机同源请求' })
              try {
                await handle(req, res, new URL(req.url, 'http://127.0.0.1'))
              } catch (e) {
                sendJson(res, 500, { ok: false, error: e?.message || String(e) })
              }
            },
          }),
        'session: /api/feiyu-commander 路由',
      )
      log('设置页接口已挂上：/api/feiyu-commander')
    } catch (e) {
      log(`设置页接口没挂上：${e?.message || e}`)
    }
  })

  ctx.inject(['tools'], (toolsCtx) => {
    const tools = toolsCtx.tools

    tools.register({
      name: 'commander_start',
      description: '起大肥鱼的音频桥接（听队友/主人说话 + 把话播进虚拟麦）。已在跑就复用。指挥由当前会话里的大模型做，不涉及后台导演。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          target: { type: 'string', description: 'both（虚拟麦+耳机）/ speaker（只自己听）/ cable（只进虚拟麦）' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const a = args ?? {}
        const b = await ensureBridge()
        const st = (await status()) ?? {}
        const ls = st.listen ?? {}
        return {
          text: [
            `桥接：${b.started ? '新启动' : '已在跑'}｜监听 ${ls.device ?? '?'}｜已听 ${ls.texts ?? 0} 句`,
            `说话目标：${(st.speaker ?? {}).target ?? a.target ?? cfg.target}`,
            '接下来用 commander_begin 接管，然后进游戏。',
          ].join('\n'),
        }
      },
    })

    tools.register({
      name: 'commander_stop',
      description: '停掉音频桥接（不再听、不再说）。',
      parameters: { type: 'object', additionalProperties: false, properties: {} },
      output: TEXT_OUTPUT,
      async execute() {
        killTree(state.bridge?.pid)
        state.bridge = null
        await sleep(400)
        const st = await status(800)
        return { text: `已停桥接${st ? '（8766 还有响应，可能被别的进程接管了）' : '。'}` }
      },
    })

    tools.register({
      name: 'commander_status',
      description: '看运行态：桥接设备/听到多少句/主人麦克风/说话目标。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: { lines: { type: 'number', description: '日志行数，默认 8' } },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const n = Math.min(Math.max(Number(args?.lines) || 8, 1), 60)
        const st = await status()
        if (!st) {
          return { text: '桥接没在跑（8766 无响应）。用 commander_start 起。' }
        }
        const ls = st.listen ?? {}
        const sp = st.speaker ?? {}
        const now = Date.now()
        const age = ls.lastTextTs ? Math.round((now - ls.lastTextTs) / 1000) : null
        const logTxt = tail(logFile('bridge.log'), n)
        return {
          text: [
            `桥接：${ls.device ?? '?'}｜片段 ${ls.segments ?? 0}｜转写 ${ls.texts ?? 0}｜最后一句 ${age == null ? '还没有' : age + ' 秒前'}`,
            `主人麦克风：${st.owner?.device ?? '（没找到）'}`,
            `说话：${sp.target ?? cfg.target}${sp.muted ? '（已静音）' : ''}`,
            `日志：`,
            logTxt.trim() ? logTxt : '（空）',
          ].join('\n'),
        }
      },
    })

    tools.register({
      name: 'commander_say',
      description: '让大肥鱼说一句。默认 both = 虚拟麦（队友能听见）+ 耳机（自己也能听见）。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          text: { type: 'string', description: '要说的话' },
          target: { type: 'string', description: 'both / speaker / cable，默认 both' },
          sync: { type: 'boolean', description: 'true = 等它播完再返回' },
        },
        required: ['text'],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const text = String(args?.text ?? '').trim()
        if (!text) return { text: '没给 text。' }
        const target = String(args?.target ?? 'both')
        const q = `?target=${encodeURIComponent(target)}&text=${encodeURIComponent(text)}${args?.sync ? '' : '&async=1'}`
        try {
          const r = await api('/say' + q, {}, args?.sync ? 60000 : 8000)
          return { text: `已交给大肥鱼（${target}）：${r.queued ? '排队播放' : '播放完成'}｜${text}` }
        } catch (e) {
          return { text: `没说出来：${e?.message || e}。先 commander_start 确认桥接在跑。` }
        }
      },
    })

    tools.register({
      name: 'commander_begin',
      description:
        '立刻开始指挥（会话模式）：确保桥接在跑，把指挥权交给当前会话；返回 提示词 + 指挥方法 + 当前局势 + 最近听到的语音。之后用 commander_watch 听、用 commander_say 说。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          mode: { type: 'string', description: 'attack / defend，默认用设置里的' },
          target: { type: 'string', description: 'both / cable / speaker，默认用设置里的' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const a = args ?? {}
        const pyArgs = ['begin']
        if (a.mode) pyArgs.push('--mode', String(a.mode))
        if (a.target) pyArgs.push('--target', String(a.target))
        const r = runPy(pyArgs, 90000)
        if (!r || r.error) return { text: `没起来：${r?.error || '未知错误'}` }
        sessionMode.active = true
        sessionMode.since = Number(r.since) || 0
        const scr = r.screen ?? {}
        const recent = (r.recent ?? []).map((x) => `· ${x.text}`).join('\n') || '（还没有）'
        return {
          text: [
            '【会话指挥已接管】现在是你自己在指挥，后台没有任何自动导演。',
            `说话目标：${r.target}｜阵营：${r.mode || '按屏幕判断'}｜音色：${r.voice}`,
            `桥接：${r.bridge?.device ?? '?'}｜已转写 ${r.bridge?.texts ?? 0} 句`,
            '',
            '── 提示词（设置里写的）──',
            r.prompt || '（空）',
            '',
            '── 指挥方法 ──',
            r.method,
            '',
            `── 当前局势 ── phase=${scr.phase ?? '?'} side=${scr.side ?? '?'} map=${scr.map ?? '?'} 剩余=${scr.timerText ?? '?'} 票数=${scr.tickets ?? '?'} 在局内=${scr.inGame ? '是' : '否'}`,
            '── 最近听到 ──',
            recent,
            '',
            `起点 ts=${r.since}。下一步：commander_watch {seconds:25} 听新语音。`,
          ].join('\n'),
        }
      },
    })

    tools.register({
      name: 'commander_watch',
      description:
        '会话指挥的「听」：长轮询，最多等 seconds 秒，一有新的队友语音就返回（同时带当前屏幕局势）。没人说话就等到超时。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          seconds: { type: 'number', description: '最多等多久，默认 25 秒（上限 90）' },
          since: { type: 'number', description: '从这个时间戳之后开始听；不传就接着上次' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const secs = Math.min(Math.max(Number(args?.seconds) || 25, 3), 90)
        const since = args?.since != null ? Number(args.since) : sessionMode.since
        const r = runPy(['watch', '--seconds', String(secs), '--since', String(since || 0)], (secs + 20) * 1000)
        if (!r || r.error) return { text: `听失败：${r?.error || '未知错误'}` }
        sessionMode.since = Number(r.since) || since || 0
        const scr = r.screen ?? {}
        const head = `局势：phase=${scr.phase ?? '?'} 剩余=${scr.timerText ?? '?'} 票数=${scr.tickets ?? '?'} 在局内=${scr.inGame ? '是' : '否'}${r.ended ? '｜已结算' : ''}${r.lobby ? '｜在大厅' : ''}`
        // ⚠️ 这里必须把「主人」和「队友」分开写。
        // 之前只输出 `· ${text}`，模型分不清谁在说话，主人亲口下的令也被当成背景音，
        // 于是只会对着队友的闲聊回「Nothing」——这就是「大肥鱼一点不听」的根因。
        const ownerLines = (r.owner ?? []).map((x) => `· ${x.text}`)
        const teamLines = (r.team ?? []).map((x) => `· ${x.text}`)
        const allNew = r.new ?? []
        const out = [head]
        if (ownerLines.length) {
          out.push('主人亲口说的（这是给你下的令，当成你自己的判断直接下令，绝不说「主人说」「收到」）：')
          out.push(...ownerLines)
        }
        if (teamLines.length) {
          out.push('队友背景（可参考，不用句句接）：')
          out.push(...teamLines)
        }
        if (!allNew.length) out.push(`（${secs} 秒内没人说话）`)
        if (r.speakMode === 'owner' && !ownerLines.length) {
          out.push('→ owner 模式：主人没开口，闭嘴，直接回去 watch。')
        }
        if (r.ended) out.push('→ 对局已结束：说一句收工，然后 commander_end。')
        if (r.lobby) out.push('→ 在大厅（右下角有「开始行动」）：别说话，等开局。')
        return { text: out.filter(Boolean).join('\n') }
      },
    })

    tools.register({
      name: 'commander_shot',
      description: '抓一张游戏画面（OBS 的 WGC 窗口采集，游戏被别的窗口盖住也能抓，最小化不行）。返回 PNG 路径，用 read 工具看图。',
      parameters: { type: 'object', additionalProperties: false, properties: {} },
      output: TEXT_OUTPUT,
      async execute() {
        const r = runPy(['shot'], 60000)
        if (!r?.ok) {
          return { text: `抓图失败：${r?.error || '没拿到路径'}${r?.stderr ? '\n' + r.stderr : ''}` }
        }
        const bright = r.brightness
        const hint = bright != null && bright < 6 ? '（画面几乎全黑：OBS 没绑到窗口，先把游戏开起来）' : ''
        return { text: `已抓图：${r.path}\n亮度 ${bright ?? '?'}${hint}${r.warn?.length ? '\n注意：' + r.warn.join(' / ') : ''}` }
      },
    })

    tools.register({
      name: 'commander_end',
      description: '结束会话指挥（只收手：不再等你开口；音频桥接留着，随时可以再 begin）。',
      parameters: { type: 'object', additionalProperties: false, properties: {} },
      output: TEXT_OUTPUT,
      async execute() {
        sessionMode.active = false
        return { text: '会话指挥已结束。想再开：commander_begin。' }
      },
    })

    tools.register({
      name: 'commander_log',
      description: '看桥接日志（默认 20 行）。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: { lines: { type: 'number', description: '行数，默认 20' } },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const n = Math.min(Math.max(Number(args?.lines) || 20, 1), 200)
        return { text: tail(logFile('bridge.log'), n) }
      },
    })
  })

  if (cfg.autoStart) {
    ensureBridge().catch((e) => log(`自动启动失败：${e?.message || e}`))
  }

  log(`已加载（纯会话版）：工具 commander_begin/watch/say/shot/end/start/stop/status/log；tools=${cfg.toolsDir}`)
}
