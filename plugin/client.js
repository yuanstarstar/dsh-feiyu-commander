/**
 * dsh-feiyu-commander —— 浏览器半端（设置里的一页「肥鱼指挥官」）
 * ============================================================================
 * ⭐ client bundle 不是 ESM，是自注册脚本：
 *     window.__ModuleLoader__.load({ id: '<包名>', factory: (require) => { ... } })
 * ⭐ 进「设置」的写法：slots.inject('settings.section', () => slots.register({...}, () => h(Panel)))
 * 这一页要给的：人设提示词、发进会话的提示词、绑定会话、说话目标、自动触发、运行态。
 * 后端在 host.js 的 /api/feiyu-commander。纯会话版：没有后台导演。
 * ============================================================================
 */
window.__ModuleLoader__.load({
  id: '@local/dsh-feiyu-commander',
  factory: (require) => {
    const React = require('react')
    const h = React.createElement
    const { useEffect, useState } = React

    const API = '/api/feiyu-commander'

    // apply(ctx) 里拿到的客户端根 ctx：用来借 sessions 服务，把一句话真的发进会话
    let rootCtx = null

    // 触发哨兵的通知：apply 里推，设置页里显示
    const notesSubs = new Set()

    /**
     * 往会话里发一条用户消息（= 在输入框里打字按发送）。
     * 依次试 candidates 里的会话 id：优先会话作用域上的 conversation.send，
     * 退而用 session.prompt。返回 { via, id }。
     */
    async function sendToSession(candidates, text) {
      const services =
        rootCtx && typeof rootCtx.get === 'function' ? rootCtx.get('sessions') : undefined
      const sessions = services || (rootCtx && rootCtx.sessions)
      if (!sessions) throw new Error('拿不到 sessions 服务')
      const errs = []
      // 会话 id 有两种写法：目录/设置里是裸 uuid，sessions 服务里是 "session-<uuid>"。
      // 两个都试，谁认就谁（diag 里 749 条 list 全是 session- 前缀）。
      const variants = (v) => {
        const s = String(v || '')
        if (!s) return []
        return s.startsWith('session-') ? [s, s.slice(8)] : [s, 'session-' + s]
      }
      const ids = []
      for (const c of candidates || []) {
        for (const v of variants(c)) if (v && ids.indexOf(v) < 0) ids.push(v)
      }
      if (!ids.length) throw new Error('没有绑定会话')
      // 记一份「这台浏览器看到的 sessions 服务长什么样」，失败时一起落盘
      let probe = {}
      try {
        const snap = sessions.list && typeof sessions.list.getSnapshot === 'function' ? sessions.list.getSnapshot() : null
        const listIds = snap && Array.isArray(snap.ids) ? snap.ids : []
        probe = {
          keys: Object.keys(sessions).slice(0, 24),
          retain: typeof sessions.retain,
          scope: typeof sessions.scope,
          binding: typeof sessions.binding,
          listCount: listIds.length,
          listFirst: listIds.slice(0, 3),
          boundFound: ids.some((id) => listIds.indexOf(id) >= 0),
        }
      } catch (e) {
        probe = { err: String((e && e.message) || e) }
      }
      for (const id of ids) {
        // ① 官方推荐路（dsh-whale-call 用的同一套）：retain → ready → binding.session.prompt
        //    会话没打开也能发；reference.ready 解析出 binding
        if (typeof sessions.retain === 'function') {
          try {
            const reference = sessions.retain(id, { source: 'feiyu-commander' })
            try {
              const binding = await reference.ready
              const session = binding && binding.session
              if (!session || typeof session.prompt !== 'function') throw new Error('binding 里没有 session')
              const r = await session.prompt([{ type: 'text', text }], 'queue')
              if (r && r.ok === false) throw new Error((r.error && (r.error.message || r.error.code)) || 'prompt 被拒')
              return { via: 'retain+prompt', id }
            } finally {
              try {
                reference.release()
              } catch (_) {
                /* 释放两次也没事 */
              }
            }
          } catch (e) {
            errs.push(id.slice(0, 8) + '·retain:' + ((e && e.message) || e))
          }
        }
        // ② 会话已经开着：作用域上的 conversation.send
        try {
          const sctx = typeof sessions.scope === 'function' ? sessions.scope(id) : undefined
          const conv =
            sctx && (sctx.conversation || (typeof sctx.get === 'function' ? sctx.get('conversation') : undefined))
          if (conv && typeof conv.send === 'function') {
            await conv.send(text)
            return { via: 'conversation.send', id }
          }
        } catch (e) {
          errs.push(id.slice(0, 8) + '·conv:' + ((e && e.message) || e))
        }
        // ③ 兜底：已有的 binding
        try {
          const binding = typeof sessions.binding === 'function' ? sessions.binding(id) : undefined
          const session = binding && binding.session
          if (session && typeof session.prompt === 'function') {
            const r = await session.prompt([{ type: 'text', text }], 'queue')
            if (r && r.ok === false) throw new Error((r.error && r.error.message) || 'prompt 失败')
            return { via: 'binding.prompt', id }
          }
        } catch (e) {
          errs.push(id.slice(0, 8) + '·bind:' + ((e && e.message) || e))
        }
      }
      const msg = errs.join('；') || '发送失败'
      // 发不出去时把原因落盘（_capture/client-diag.json），省得靠截图猜
      try {
        jpost('/diag', { at: Date.now(), text: String(text).slice(0, 60), ids, errs, probe })
      } catch (_) {
        /* 诊断失败不影响报错 */
      }
      throw new Error(msg)
    }

    const asJson = async (res) => {
      if (res.ok) return res.json()
      throw new Error('HTTP ' + res.status + (res.status === 403 ? '（被宿主拦了）' : ''))
    }
    const jget = (p) => fetch(API + p, { credentials: 'same-origin' }).then(asJson)
    const jpost = (p, body) =>
      fetch(API + p, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body || {}),
      }).then(asJson)

    const S = {
      page: { display: 'flex', flexDirection: 'column', gap: 14, maxWidth: 720, padding: '2px 2px 10px' },
      card: { border: '1px solid var(--dsw-alias-border-secondary,#2c2f36)', borderRadius: 12, padding: 14, display: 'flex', flexDirection: 'column', gap: 10 },
      title: { fontSize: 14, fontWeight: 600 },
      hint: { fontSize: 12, opacity: 0.66, lineHeight: 1.5 },
      row: { display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' },
      label: { fontSize: 12.5, opacity: 0.8, minWidth: 76 },
      area: { width: '100%', boxSizing: 'border-box', minHeight: 150, resize: 'vertical', padding: 10, borderRadius: 8, border: '1px solid var(--dsw-alias-border-secondary,#2c2f36)', background: 'transparent', color: 'inherit', fontSize: 13, lineHeight: 1.6, fontFamily: 'inherit' },
      input: { padding: '6px 8px', borderRadius: 8, border: '1px solid var(--dsw-alias-border-secondary,#2c2f36)', background: 'transparent', color: 'inherit', fontSize: 13, minWidth: 150 },
      btn: (primary) => ({ padding: '6px 14px', borderRadius: 8, cursor: 'pointer', fontSize: 13, border: '1px solid var(--dsw-alias-border-secondary,#2c2f36)', background: primary ? 'var(--dsw-alias-accent,#3b82f6)' : 'transparent', color: primary ? '#fff' : 'inherit' }),
      mono: { fontFamily: 'ui-monospace,Consolas,monospace', fontSize: 12 },
      ok: { color: '#22c55e', fontSize: 12.5 },
      err: { color: '#ef4444', fontSize: 12.5 },
    }

    const fmtTime = (ms) => {
      try {
        const d = new Date(ms)
        const p = (n) => String(n).padStart(2, '0')
        return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
      } catch {
        return '?'
      }
    }

    function Panel() {
      const [st, setSt] = useState(null)
      const [draft, setDraft] = useState(null)
      const [busy, setBusy] = useState('')
      const [msg, setMsg] = useState(null)

      const load = async () => {
        try {
          const r = await jget('/state')
          setSt(r)
          setDraft(r.settings)
          setMsg(null)
        } catch (e) {
          setMsg({ bad: true, text: '读取失败：' + (e && e.message) })
        }
      }

      useEffect(() => {
        load()
      }, [])

      // 「开始行动」触发的结果（apply 里的轮询推过来）
      useEffect(() => {
        const f = (n) => setMsg({ text: n.note, bad: !!n.bad })
        notesSubs.add(f)
        return () => {
          notesSubs.delete(f)
        }
      }, [])

      if (!st || !draft) {
        return h('div', { style: S.page }, h('div', { style: S.hint }, '正在读取指挥官设置…', msg ? h('div', { style: S.err }, msg.text) : null))
      }

      const set = (k, v) => setDraft({ ...draft, [k]: v })

      const save = async () => {
        setBusy('save')
        try {
          const r = await jpost('/save', { patch: draft })
          setSt({ ...st, settings: r.settings })
          if (r.sessionIdRejected) {
            setMsg({
              bad: true,
              text:
                '这个会话 id 在磁盘上不存在，绑定没改（还是 ' +
                String(r.settings.sessionId || '').slice(8, 16) +
                '…）',
            })
          } else {
            setMsg({ text: '已保存（提示词下一句话就生效，不用重启）' })
          }
        } catch (e) {
          setMsg({ bad: true, text: '保存失败：' + (e && e.message) })
        } finally {
          setBusy('')
        }
      }

      const act = async (kind) => {
        setBusy(kind)
        try {
          if (kind === 'start') {
            await jpost('/save', { patch: draft })
            const r = await jpost('/start', { target: draft.target })
            setMsg({ text: `桥接${r.bridge ? '已新起' : '已复用'}。` })
          } else if (kind === 'begin') {
            await jpost('/save', { patch: draft })
            const r = await jpost('/begin', { target: draft.target })
            const payload = String(draft.startPrompt || '').trim() || '开始指挥'
            let sent = ''
            try {
              const r2 = await sendToSession([draft.sessionId], payload)
              sent = `\n已往会话 ${String(r2.id).slice(0, 8)} 发了下面这段（走 ${r2.via}）：\n${payload.slice(0, 80)}${payload.length > 80 ? '…' : ''}`
              try {
                jpost('/diag', { at: Date.now(), ok: true, via: r2.via, id: r2.id, len: payload.length })
              } catch (_) {
                /* 诊断失败不影响结果 */
              }
            } catch (e) {
              sent = `\n会话消息没发出去：${(e && e.message) || e}——手动在对话里打一句「开始指挥」也可以。`
            }
            setMsg({
              text:
                `已切到会话指挥：桥接${r.bridge === 'started' ? '已新起' : '已在跑'}（${r.device || '设备未知'}）。` +
                sent,
              bad: !sent.startsWith('\n已往'),
            })
          } else {
            await jpost('/stop', {})
            setMsg({ text: '已停桥接（不再听、不再说）' })
          }
          await load()
        } catch (e) {
          setMsg({ bad: true, text: '操作失败：' + (e && e.message) })
        } finally {
          setBusy('')
        }
      }

      const rt = st.runtime || {}
      const br = rt.bridge
      const sessions = st.sessions || []
      // 会话 id 可能是 "session-<uuid>" 或裸 uuid，两种都认，免得下拉框选不中
      const sameSession = (a, b) => String(a || '').replace(/^session-/, '') === String(b || '').replace(/^session-/, '')
      const sel = sessions.find((s) => sameSession(s.id, draft.sessionId))

      return h(
        'div',
        { style: S.page },
        h(
          'div',
          { style: S.card },
          h('div', { style: S.title }, '大肥鱼指挥官'),
          h(
            'div',
            { style: S.hint },
            '听队友语音 → 读屏幕局势 → 用 DeepSeek 现写一句指令 → 说进虚拟麦克风。下面改的是它「怎么说话」和「归谁管」。',
          ),
        ),
        h(
          'div',
          { style: S.card },
          h('div', { style: S.title }, '提示词'),
          h(
            'div',
            { style: S.hint },
            '会拼在系统提示词后面，决定它的身份、口吻、称呼和禁区。例如：叫主人、只报点位不喊口号、队友倒下先安抚。留空则用默认人设。',
          ),
          h('textarea', {
            style: S.area,
            value: draft.prompt || '',
            placeholder: '你是大肥鱼，主人的 AI 战场指挥官。说话短、带方位；先安抚再下指令；不许说「兄弟们」……',
            onChange: (e) => set('prompt', e.target.value),
          }),
        ),
        h(
          'div',
          { style: S.card },
          h('div', { style: S.title }, '发进会话的提示词'),
          h(
            'div',
            { style: S.hint },
            '点「立刻开始指挥」，或者检测到游戏里「开始行动」被点（大厅→进局）时，把这段话原样发进绑定会话，等于你亲手在输入框里发出去。留空就只发「开始指挥」。',
          ),
          h('textarea', {
            style: { ...S.area, minHeight: 190 },
            value: draft.startPrompt || '',
            placeholder:
              '参考写法（按你真正要做的事改，别照抄）：\n现在轮到「大肥鱼」做这件事了（时段 9:00-23:00）。请你亲自做完，中途不用问我。\n1) 先挑一个……\n2) 看看它……\n3) 打开它……\n4) 怎么操作、用什么工具看画面……\n5) 时长最多 N 分钟，做的过程中不要说话。\n6) 收工后再评价，像人一样说，别用客服腔。\n7) 最后真的发一条……\n8) 约束：遇到登录/付费就换一个。',
            onChange: (e) => set('startPrompt', e.target.value),
          }),
          h(
            'div',
            { style: S.row },
            h('label', { style: { ...S.hint, display: 'flex', gap: 6, alignItems: 'center' } },
              h('input', {
                type: 'checkbox',
                checked: draft.autoOnStart !== false,
                onChange: (e) => set('autoOnStart', e.target.checked),
              }),
              '检测到「开始行动」被点时自动发这段（哨兵在后台每 5 秒看一次屏幕）',
            ),
            h('button', {
              style: S.btn(false),
              onClick: async () => {
                try {
                  await jpost('/trigger', { reason: '手动试一下' })
                  setMsg({ text: '已点火：4 秒内应该会往会话里发一段，看下面的回执。' })
                } catch (e) {
                  setMsg({ bad: true, text: '点火失败：' + ((e && e.message) || e) })
                }
              },
            }, '试一下'),
          ),
          st.watcher && st.watcher.pid ? h('div', { style: { ...S.hint, ...S.mono } }, `哨兵：在跑 pid=${st.watcher.pid}`) : h('div', { style: S.hint }, '哨兵：没在跑（重启 DSH 或点「启动」会带上）'),
        ),
        h(
          'div',
          { style: S.card },
          h('div', { style: S.title }, '绑定会话'),
          h('div', { style: S.hint }, '指挥官归哪个 DSH 会话管（记录与显示用）。下拉里是这台机器最近的会话，按最后活动时间排。'),
          h(
            'div',
            { style: S.row },
            h('select', {
              style: { ...S.input, minWidth: 380 },
              value: draft.sessionId || '',
              onChange: (e) => set('sessionId', e.target.value),
            },
              h('option', { value: '' }, '（不绑定）'),
              sessions.map((s) =>
                h('option', { key: s.id, value: s.id }, `${s.title || s.workspace} · ${fmtTime(s.mtime)} · ${String(s.bare || s.id).slice(0, 8)}`),
              ),
            ),
            h('button', { style: S.btn(false), onClick: load, disabled: !!busy }, '刷新'),
          ),
          sel ? h('div', { style: { ...S.hint, ...S.mono } }, `已绑定：${sel.id}`) : null,
          // 绑定会话失效时的补救：id 必须存在于会话列表里，否则消息发不出去（以前会静默发到别的会话）
          draft.sessionId && !sel
            ? h(
                'div',
                { style: { ...S.hint, color: '#c0392b' } },
                `⚠ 绑定的会话 ${draft.sessionId} 不在列表里（可能已被删除/换了机器）。`,
                h('button', {
                  style: { ...S.btn(false), marginLeft: 8 },
                  onClick: () => set('sessionId', sessions.length ? sessions[0].id : ''),
                }, '绑定最新会话'),
              )
            : null,
          h('div', { style: S.hint }, '改完记得点「保存」；消息只会发给这一个会话，发不出去会直接报错，不会偷偷发到别的会话。'),
        ),
        h(
          'div',
          { style: S.card },
          h('div', { style: S.title }, '说话与打法'),
          h(
            'div',
            { style: S.row },
            h('span', { style: S.label }, '说话目标'),
            h('select', { style: S.input, value: draft.target, onChange: (e) => set('target', e.target.value) },
              h('option', { value: 'both' }, 'both（队友能听见 + 自己也听见）'),
              h('option', { value: 'cable' }, 'cable（只进虚拟麦，队友听）'),
              h('option', { value: 'speaker' }, 'speaker（只自己听，耳语）'),
            ),
          ),
          h('div', { style: S.hint }, '说话目标就是大肥鱼把话播到哪里；指挥时一般用 both。'),
          h(
            'div',
            { style: S.row },
            h('span', { style: S.label }, '说话模式'),
            h('select', { style: S.input, value: draft.speakMode || 'auto', onChange: (e) => set('speakMode', e.target.value) },
              h('option', { value: 'auto' }, '自动（队友喊话就接）'),
              h('option', { value: 'owner' }, '只听主人（没主人命令不说话）'),
            ),
          ),
          h('div', { style: S.hint }, '「只听主人」= 队友说什么都当背景，只有你对麦克风开口它才指挥；适合你亲自带队、不想它抢话的时候。'),
        ),
        h(
          'div',
          { style: S.card },
          h('div', { style: S.title }, '运行'),
          h(
            'div',
            { style: { ...S.hint, ...S.mono } },
            br
              ? `桥接：${br.device}｜片段 ${br.segments}｜转写 ${br.texts}｜${br.lastTextTs ? '最后一句 ' + Math.round((Date.now() - br.lastTextTs) / 1000) + ' 秒前' : '还没听到人声'}`
              : '桥接：没在跑',
          ),
          h('div', { style: { ...S.hint, ...S.mono } }, `主人麦克风：${rt.owner || '（没找到）'}｜说话：${rt.speaker || draft.target}`),
          h(
            'div',
            { style: S.row },
            h('button', { style: S.btn(true), onClick: () => act('start'), disabled: !!busy }, busy === 'start' ? '启动中…' : '启动'),
            h('button', { style: S.btn(false), onClick: () => act('begin'), disabled: !!busy }, busy === 'begin' ? '切换中…' : '立刻开始指挥'),
            h('button', { style: S.btn(false), onClick: () => act('stop'), disabled: !!busy }, busy === 'stop' ? '停止中…' : '停止'),
            h('button', { style: S.btn(false), onClick: save, disabled: !!busy }, busy === 'save' ? '保存中…' : '保存'),
          ),
          msg ? h('div', { style: msg.bad ? S.err : S.ok }, msg.text) : null,
          h(
            'div',
            { style: S.hint },
            '「立刻开始指挥」= 把下面那段提示词直接发进绑定的会话，我在对话里接管：听语音、读屏幕、用 commander_say 说话。',
          ),
        ),
        h(
          'div',
          { style: S.hint },
          '提示词改了不用重启；启动/停止只影响本机这两个进程。设置存在 _capture/commander-settings.json。',
        ),
      )
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        rootCtx = ctx

        // 「开始行动」哨兵：python 侧盯着屏幕写 trigger.json，这里轮询到新的 at
        // 就把设置里那段「发进会话的提示词」真的发出去。放在 apply 里而不是面板里，
        // 所以关掉设置页也照样工作（只要这个页面开着）。
        const triggerState = { seenAt: null }
        const emitNote = (note) => {
          triggerState.note = note
          notesSubs.forEach((f) => {
            try {
              f(note)
            } catch {
              /* ignore */
            }
          })
        }
        // FRESH_MS：页面刚打开时，如果磁盘上的触发还很新（比如刚点完「开始行动」就刷新页面），
        // 也要补发一次；太老的（上一局的）只认领、不发，免得一刷新就莫名其妙指挥一局。
        const FRESH_MS = 120000
        const pollTrigger = async () => {
          try {
            const r = await jget('/state')
            const t = r.trigger
            if (!t || !t.at) return
            if (triggerState.seenAt == null) {
              triggerState.seenAt = t.at
              if (Date.now() - t.at > FRESH_MS) return
              // 新到的触发（页面刚开/刚刷新），落下去发
            } else if (t.at <= triggerState.seenAt) {
              return
            } else {
              triggerState.seenAt = t.at
            }
            if (triggerState.firedAt && t.at - triggerState.firedAt < 30000) return
            triggerState.firedAt = t.at
            const s = r.settings || {}
            const text = String(s.startPrompt || '').trim() || '开始指挥'
            const rr = await sendToSession([s.sessionId], text)
            triggerState.last = { at: Date.now(), via: rr.via, id: rr.id, len: text.length }
            emitNote({ note: `检测到「${t.reason || '开始行动'}」→ 已往会话 ${String(rr.id).slice(0, 8)} 发了 ${text.length} 字（${rr.via}）` })
            jpost('/diag', { at: Date.now(), ok: true, kind: 'trigger', via: rr.via, id: rr.id, len: text.length, reason: t.reason })
            // 发完就清掉触发文件：否则页面一刷新（120s 内）还会补发一次，变成重复指挥
            try {
              await jpost('/trigger/clear', {})
            } catch {
              /* 清不掉不影响这次发送 */
            }
          } catch (e) {
            triggerState.last = { at: Date.now(), error: String((e && e.message) || e) }
            emitNote({ note: '触发到了但消息没发出去：' + ((e && e.message) || e), bad: true })
          }
        }
        try {
          const timer = setInterval(pollTrigger, 4000)
          pollTrigger()
          if (typeof ctx.effect === 'function') ctx.effect(() => () => clearInterval(timer), 'commander: trigger poll')
        } catch {
          /* 轮询起不来不影响设置页 */
        }

        const slots = ctx.get('slots') || ctx.slots
        try {
          slots.inject('settings.section', () =>
            slots.register({ name: 'settings.section', id: 'feiyu-commander', order: 33, label: '肥鱼指挥官' }, () => h(Panel)),
          )
        } catch (e) {
          /* 老版本没有 section，走下面的 tab */
        }
        try {
          slots.inject('settings.plugins.tab', () =>
            slots.register({ name: 'settings.plugins.tab', id: 'feiyu-commander-tab', order: 41, label: '肥鱼指挥官' }, () => h(Panel)),
          )
        } catch (e) {
          /* 两个入口都没有就只能在插件列表里看到 */
        }
      },
    }
  },
})
