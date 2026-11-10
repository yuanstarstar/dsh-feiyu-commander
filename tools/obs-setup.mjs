// OBS 采集配置助手：通过 obs-websocket v5 直接配置 OBS（不用手点界面）
// 用法：
//   node tools/obs-setup.mjs setup     建采集源 + 设分辨率 + 设录像目录
//   node tools/obs-setup.mjs status    看当前场景/输入/录像状态
//   node tools/obs-setup.mjs bind      游戏开着时，把窗口采集绑到三角洲窗口
//   node tools/obs-setup.mjs bindwin "DeepSeek"  把窗口采集绑到标题含某词的窗口（调试用）
//   node tools/obs-setup.mjs shot [源名]  抓一帧 PNG 到 _capture\frames（被遮挡也能抓）
//   node tools/obs-setup.mjs launch    OBS 没开就拉起来
//   node tools/obs-setup.mjs record 8  录 8 秒做验证
import crypto from 'node:crypto';
import fs from 'node:fs';
import { spawn, spawnSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';

const PORT = Number(process.env.OBS_PORT || 4455);
const PASSWORD = process.env.OBS_PASSWORD || ''; // OBS 设了 WebSocket 密码时用环境变量 OBS_PASSWORD 传
const HOME = process.env.FEIYU_HOME || path.join(os.homedir(), 'feiyu-commander');
const REC_DIR = process.env.FEIYU_LOG_DIR || `${HOME}\\_capture`;
const OBS_EXE = process.env.OBS_EXE || 'C:\\Program Files\\obs-studio\\bin\\64bit\\obs64.exe';
const FFMPEG = process.env.FEIYU_FFMPEG || `${process.env.USERPROFILE || ''}\\.dsh\\tools\\ffmpeg\\bin\\ffmpeg.exe`;
const GAME_EXE_HINTS = ['DeltaForceClient.exe', '三角洲', 'Delta Force', 'UnrealWindow'];

const sha256b64 = (s) => crypto.createHash('sha256').update(s, 'utf8').digest('base64');

class Obs {
  constructor() { this.seq = 0; this.pending = new Map(); }
  connect() {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(`ws://127.0.0.1:${PORT}`);
      this.ws = ws;
      const timer = setTimeout(() => reject(new Error('连接超时')), 8000);
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.op === 0) {
          const auth = msg.d?.authentication;
          const payload = { rpcVersion: 1, eventSubscriptions: 0 };
          if (auth) payload.authentication = sha256b64(sha256b64(PASSWORD + auth.salt) + auth.challenge);
          ws.send(JSON.stringify({ op: 1, d: payload }));
        } else if (msg.op === 2) {
          clearTimeout(timer); resolve();
        } else if (msg.op === 7) {
          const p = this.pending.get(msg.d.requestId);
          if (!p) return;
          this.pending.delete(msg.d.requestId);
          if (msg.d.requestStatus.result) p.resolve(msg.d.responseData || {});
          else p.reject(new Error(msg.d.requestStatus.comment || 'OBS 拒绝'));
        }
      };
      ws.onerror = () => { clearTimeout(timer); reject(new Error('连不上 OBS，先确认 OBS 开着且 WebSocket 端口 4455 可用')); };
      ws.onclose = () => { for (const p of this.pending.values()) p.reject(new Error('连接被关闭')); };
    });
  }
  req(requestType, requestData = {}) {
    return new Promise((resolve, reject) => {
      const requestId = 'r' + (++this.seq);
      this.pending.set(requestId, { resolve, reject });
      this.ws.send(JSON.stringify({ op: 6, d: { requestType, requestId, requestData } }));
      setTimeout(() => { if (this.pending.delete(requestId)) reject(new Error(requestType + ' 超时')); }, 10000);
    });
  }
  close() { try { this.ws.close(); } catch {} }
}

const report = { ok: [], warn: [], fail: [], data: {} };
const step = async (label, fn) => {
  try { const v = await fn(); report.ok.push(label); return v; }
  catch (e) { report.fail.push(`${label}: ${e.message}`); return null; }
};

async function ensureInput(obs, sceneName, inputName, inputKind, settings = {}) {
  const { inputs = [] } = await obs.req('GetInputList');
  const hit = inputs.find((i) => i.inputName === inputName);
  if (hit) {
    await obs.req('SetInputSettings', { inputName, inputSettings: settings, overlay: true });
    return 'exists';
  }
  await obs.req('CreateInput', { sceneName, inputName, inputKind, inputSettings: settings, sceneItemEnabled: true });
  return 'created';
}

async function setup(obs) {
  const ver = await step('读 OBS 版本', () => obs.req('GetVersion'));
  if (ver) report.data.obsVersion = ver.obsVersion;

  const scenes = await obs.req('GetSceneList');
  const scene = scenes.currentProgramSceneName;
  report.data.scene = scene;

  report.data.audio = {};
  report.data.audio.desktop = await step('建/更新 桌面音频', () =>
    ensureInput(obs, scene, '桌面音频', 'wasapi_output_capture', {}));
  report.data.audio.mic = await step('建/更新 麦克风', () =>
    ensureInput(obs, scene, '麦克风', 'wasapi_input_capture', {}));
  report.data.capture = await step('建/更新 三角洲-画面', () =>
    ensureInput(obs, scene, '三角洲-画面', 'window_capture', { method: 2, window: '' }));

  await step('设画布 2560x1600 → 输出 1920x1080@60', () =>
    obs.req('SetVideoSettings', {
      baseWidth: 2560, baseHeight: 1600,
      outputWidth: 1920, outputHeight: 1080,
      fpsNumerator: 60, fpsDenominator: 1,
    }));

  fs.mkdirSync(REC_DIR, { recursive: true });
  await step('设录像目录', async () => {
    try { await obs.req('SetRecordDirectory', { recordDirectory: REC_DIR }); } catch {}
    await obs.req('SetProfileParameter', { parameterCategory: 'SimpleOutput', parameterName: 'FilePath', parameterValue: REC_DIR });
  });
  await step('设录像格式 mkv', () =>
    obs.req('SetProfileParameter', { parameterCategory: 'SimpleOutput', parameterName: 'RecFormat2', parameterValue: 'mkv' }));

  const items = await obs.req('GetSceneItemList', { sceneName: scene });
  report.data.sceneItems = (items.sceneItems || []).map((i) => ({ name: i.sourceName, enabled: i.sceneItemEnabled }));
  // 原来那个绑在 DSH 窗口上的「窗口采集」留着会误录到自己的界面，默认关掉
  for (const it of items.sceneItems || []) {
    if (it.sourceName === '窗口采集' && it.sceneItemEnabled) {
      await step('关掉旧的 DSH 窗口采集', () =>
        obs.req('SetSceneItemEnabled', { sceneName: scene, sceneItemId: it.sceneItemId, sceneItemEnabled: false }));
    }
  }
  return scene;
}

async function status(obs) {
  const scenes = await obs.req('GetSceneList');
  report.data.scenes = scenes.scenes?.map((s) => s.sceneName);
  report.data.currentScene = scenes.currentProgramSceneName;
  const inputs = await obs.req('GetInputList');
  report.data.inputs = (inputs.inputs || []).map((i) => `${i.inputName} [${i.inputKind}]`);
  const vs = await obs.req('GetVideoSettings');
  report.data.video = `${vs.baseWidth}x${vs.baseHeight} → ${vs.outputWidth}x${vs.outputHeight} @${vs.fpsNumerator}/${vs.fpsDenominator}`;
  const rs = await obs.req('GetRecordStatus');
  report.data.record = { active: rs.outputActive, path: rs.outputPath, timecode: rs.outputTimecode };
  const ss = await obs.req('GetStreamStatus').catch(() => null);
  if (ss) report.data.stream = { active: ss.outputActive };
}

async function bind(obs) {
  const scenes = await obs.req('GetSceneList');
  const scene = scenes.currentProgramSceneName;
  // 先确保采集源在
  await ensureInput(obs, scene, '三角洲-画面', 'window_capture', { method: 2 });
  const props = await obs.req('GetInputPropertiesListPropertyItems', { inputName: '三角洲-画面', propertyName: 'window' });
  const all = (props.propertyItems || []).map((p) => p.itemValue).filter((v) => v && !v.startsWith('__'));
  report.data.windows = all.slice(0, 40);
  const hit = all.find((v) => GAME_EXE_HINTS.some((h) => v.toLowerCase().includes(h.toLowerCase())));
  if (!hit) {
    report.warn.push('没找到三角洲窗口，先把游戏开起来再跑 bind');
    return;
  }
  await obs.req('SetInputSettings', { inputName: '三角洲-画面', inputSettings: { window: hit, method: 2 }, overlay: true });
  report.data.boundWindow = hit;
}

async function bindwin(obs, title) {
  const scenes = await obs.req('GetSceneList');
  const scene = scenes.currentProgramSceneName;
  await ensureInput(obs, scene, '三角洲-画面', 'window_capture', { method: 2 });
  const props = await obs.req('GetInputPropertiesListPropertyItems', { inputName: '三角洲-画面', propertyName: 'window' });
  const all = (props.propertyItems || []).map((p) => p.itemValue).filter((v) => v && !v.startsWith('__'));
  const hit = all.find((v) => v.toLowerCase().includes(String(title || '').toLowerCase()));
  if (!hit) {
    report.warn.push('没找到标题含「' + title + '」的窗口');
    report.data.windows = all.slice(0, 60);
    return;
  }
  await obs.req('SetInputSettings', { inputName: '三角洲-画面', inputSettings: { window: hit, method: 2 }, overlay: true });
  report.data.boundWindow = hit;
}

// 抓一帧：走 OBS 的 WGC 窗口采集，窗口被别的窗口盖住也能抓到（最小化不行）
async function shot(obs, sourceName) {
  const src = sourceName || '三角洲-画面';
  const r = await obs.req('GetSourceScreenshot', {
    sourceName: src,
    imageFormat: 'png',
    imageWidth: 1920,
    imageCompressionQuality: 90,
  });
  const m = /^data:image\/png;base64,(.*)$/s.exec(r.imageData || '');
  if (!m) throw new Error('OBS 没返回截图数据');
  const dir = REC_DIR + '\\frames';
  fs.mkdirSync(dir, { recursive: true });
  const ts = new Date().toISOString().replace(/[:.]/g, '-');
  const p = dir + '\\shot-' + ts + '.png';
  fs.writeFileSync(p, Buffer.from(m[1], 'base64'));
  report.data.shot = p;
  report.data.shotBytes = fs.statSync(p).size;
  const br = brightness(p);
  report.data.brightness = br;
  if (br !== null && br < 6) report.warn.push('画面几乎全黑：采集源可能没绑到窗口');
  return p;
}

function brightness(png) {
  const r = spawnSync(FFMPEG, ['-v', 'error', '-i', png, '-vf', 'scale=1:1', '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'gray', '-'], { encoding: 'buffer' });
  if (r.status !== 0 || !r.stdout || r.stdout.length < 1) return null;
  return r.stdout[0];
}

function portUp() {
  return new Promise((res) => {
    const ws = new WebSocket(`ws://127.0.0.1:${PORT}`);
    const t = setTimeout(() => { try { ws.close(); } catch {} res(false); }, 1200);
    ws.onopen = () => { clearTimeout(t); try { ws.close(); } catch {} res(true); };
    ws.onerror = () => { clearTimeout(t); res(false); };
  });
}

async function launch() {
  if (await portUp()) { report.data.alreadyRunning = true; return; }
  const p = spawn(OBS_EXE, ['--minimize-to-tray'], {
    cwd: 'C:\\Program Files\\obs-studio\\bin\\64bit',
    detached: true,
    stdio: 'ignore',
  });
  p.unref();
  for (let i = 0; i < 40; i++) {
    await new Promise((r) => setTimeout(r, 500));
    if (await portUp()) { report.data.launched = true; return; }
  }
  throw new Error('OBS 进程起了，但 4455 端口没监听');
}

// 一步到位：绑到游戏窗口 + 抓一帧（游戏被别的窗口盖住也能抓）
async function grab(obs) {
  if (!(await portUp())) await launch();
  try {
    await bind(obs);
  } catch (e) {
    report.warn.push('绑窗口失败（' + e.message + '），用当前采集源抓');
  }
  const p = await shot(obs, '三角洲-画面');
  report.data.grab = p;
  return p;
}

async function record(obs, seconds) {
  const before = await obs.req('GetRecordStatus');
  if (before.outputActive) throw new Error('已经在录制了');
  await obs.req('StartRecord');
  report.data.recording = true;
  await new Promise((r) => setTimeout(r, seconds * 1000));
  const stopped = await obs.req('StopRecord');
  report.data.recordPath = stopped.outputPath;
  const st = fs.statSync(stopped.outputPath);
  report.data.recordBytes = st.size;
}

const mode = process.argv[2] || 'status';
const arg = Number(process.argv[3] || 8);
const obs = new Obs();
try {
  if (mode === 'launch') await launch();
  await obs.connect();
  if (mode === 'setup') await setup(obs);
  else if (mode === 'bind') await bind(obs);
  else if (mode === 'bindwin') await bindwin(obs, process.argv[3]);
  else if (mode === 'shot') await shot(obs, process.argv[3]);
  else if (mode === 'grab') await grab(obs);
  else if (mode === 'record') await record(obs, arg);
  else await status(obs);
} catch (e) {
  report.fail.push(`主流程: ${e.message}`);
} finally {
  obs.close();
  const outDir = path.join(HOME, 'tmp');
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(path.join(outDir, 'obs-report.json'), JSON.stringify(report, null, 2), 'utf8');
  console.log(JSON.stringify({ ok: report.ok.length, warn: report.warn.length, fail: report.fail.length }));
  process.exit(report.fail.length ? 1 : 0);
}
