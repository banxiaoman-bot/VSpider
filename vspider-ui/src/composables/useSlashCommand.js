import { computed, ref } from 'vue'

/**
 * Slash-command registry & parser.
 *
 * Each command: { name, aliases[], description, args?, handler(argsStr, ctx) }
 * ctx is an arbitrary bag of refs / callbacks the host passes in.
 */
export function createSlashCommandRegistry () {
  const commands = []

  function register (cmd) {
    commands.push({
      name: cmd.name,
      aliases: cmd.aliases || [],
      args: cmd.args || '',
      description: cmd.description,
      handler: cmd.handler,
    })
  }

  function matchCommand (input) {
    const trimmed = input.trim()
    if (!trimmed.startsWith('/')) return null
    const spaceIdx = trimmed.indexOf(' ')
    const name = (spaceIdx === -1 ? trimmed : trimmed.slice(0, spaceIdx)).toLowerCase()
    const argsStr = spaceIdx === -1 ? '' : trimmed.slice(spaceIdx + 1).trim()
    for (const cmd of commands) {
      const names = ['/' + cmd.name, ...cmd.aliases.map(a => '/' + a)]
      if (names.includes(name)) return { cmd, argsStr }
    }
    return null
  }

  function getSuggestions (partial) {
    const lower = partial.toLowerCase()
    if (!lower.startsWith('/')) return []
    const query = lower.slice(1)
    return commands.filter(c => {
      if (c.name.startsWith(query)) return true
      return c.aliases.some(a => a.startsWith(query))
    })
  }

  return { commands, register, matchCommand, getSuggestions }
}

export function registerBuiltinCommands (registry, ctx) {
  registry.register({
    name: 'model',
    aliases: ['m'],
    args: '<model-id>',
    description: '切换 VLM 模型',
    handler (argsStr) {
      if (!argsStr) {
        ctx.showMessage('info', `当前模型: ${ctx.selectedModel.value}`)
        return
      }
      ctx.selectedModel.value = argsStr
      ctx.showMessage('success', `VLM 模型已切换为 ${argsStr}`)
    },
  })

  registry.register({
    name: 'semantic',
    aliases: ['sm'],
    args: '<model-id>',
    description: '切换 Semantic 模型',
    handler (argsStr) {
      if (!argsStr) {
        ctx.showMessage('info', `当前 Semantic 模型: ${ctx.selectedSemanticModel.value}`)
        return
      }
      ctx.selectedSemanticModel.value = argsStr
      ctx.showMessage('success', `Semantic 模型已切换为 ${argsStr}`)
    },
  })

  registry.register({
    name: 'login',
    aliases: ['auth'],
    args: '[url]',
    description: '打开登录管理 / 保存 Cookie',
    handler (argsStr) {
      if (argsStr) ctx.targetUrl.value = argsStr
      ctx.authDialogOpen.value = true
    },
  })

  registry.register({
    name: 'proxy',
    aliases: ['px'],
    args: '<proxy-url>',
    description: '设置代理服务器',
    handler (argsStr) {
      if (!argsStr) {
        ctx.showMessage('info', `代理: ${ctx.proxyServer.value || '未设置'}`)
        return
      }
      ctx.proxyServer.value = argsStr
      ctx.showMessage('success', `代理已设置为 ${argsStr}`)
    },
  })

  registry.register({
    name: 'resume',
    aliases: ['r'],
    args: '[on|off]',
    description: '断点续跑开关',
    handler (argsStr) {
      const lower = argsStr.toLowerCase()
      if (lower === 'on' || lower === '1') {
        ctx.resumeEnabled.value = true
        ctx.showMessage('success', '断点续跑已开启')
      } else if (lower === 'off' || lower === '0') {
        ctx.resumeEnabled.value = false
        ctx.showMessage('success', '断点续跑已关闭')
      } else {
        ctx.resumeEnabled.value = !ctx.resumeEnabled.value
        ctx.showMessage('info', `断点续跑: ${ctx.resumeEnabled.value ? '开' : '关'}`)
      }
    },
  })

  registry.register({
    name: 'batch',
    aliases: ['b'],
    args: '<max-runs>',
    description: '设置批处理最大并发数',
    handler (argsStr) {
      const n = parseInt(argsStr, 10)
      if (isNaN(n) || n < 0 || n > 16) {
        ctx.showMessage('warning', '批处理并发数范围: 0-16')
        return
      }
      ctx.batchMaxRuns.value = n
      ctx.showMessage('success', `批处理并发数设为 ${n}`)
    },
  })

  registry.register({
    name: 'upload',
    aliases: ['up', 'file'],
    description: '打开文件上传',
    handler () {
      ctx.settingsDrawerOpen.value = true
      ctx.settingsActivePanels.value = ['file']
      ctx.showMessage('info', '请在附件面板选择文件')
    },
  })

  registry.register({
    name: 'stop',
    aliases: ['s', 'kill'],
    description: '强制终止当前任务',
    handler () {
      if (ctx.forceStop) ctx.forceStop()
    },
  })

  registry.register({
    name: 'settings',
    aliases: ['set', 'config'],
    description: '打开高级配置面板',
    handler () {
      ctx.settingsDrawerOpen.value = true
    },
  })

  registry.register({
    name: 'help',
    aliases: ['h', '?'],
    description: '显示可用命令列表',
    handler () {
      ctx.helpDialogVisible.value = true
    },
  })

  registry.register({
    name: 'temperature',
    aliases: ['temp', 't'],
    args: '<0-2>',
    description: '设置模型 Temperature',
    handler (argsStr) {
      const v = parseFloat(argsStr)
      if (isNaN(v) || v < 0 || v > 2) {
        ctx.showMessage('warning', 'Temperature 范围: 0-2')
        return
      }
      ctx.modelTemperature.value = v
      ctx.showMessage('success', `Temperature 设为 ${v}`)
    },
  })

  registry.register({
    name: 'maxtokens',
    aliases: ['tokens', 'mt'],
    args: '<512-32768>',
    description: '设置 Max Tokens',
    handler (argsStr) {
      const n = parseInt(argsStr, 10)
      if (isNaN(n) || n < 512 || n > 32768) {
        ctx.showMessage('warning', 'Max Tokens 范围: 512-32768')
        return
      }
      ctx.modelMaxTokens.value = n
      ctx.showMessage('success', `Max Tokens 设为 ${n}`)
    },
  })

  registry.register({
    name: 'status',
    aliases: ['st'],
    description: '显示当前配置摘要',
    handler () {
      const lines = [
        `VLM: ${ctx.selectedModel.value}`,
        `Semantic: ${ctx.selectedSemanticModel.value}`,
        `Temp: ${ctx.modelTemperature.value} / Tokens: ${ctx.modelMaxTokens.value}`,
        `代理: ${ctx.proxyServer.value || '无'}`,
        `续跑: ${ctx.resumeEnabled.value ? '开' : '关'}`,
        `并发: ${ctx.batchMaxRuns.value}`,
      ]
      ctx.showMessage('info', lines.join('\n'))
    },
  })
}

/**
 * Composable: provides reactive command palette state for a text input.
 */
export function useSlashCommand (registry) {
  const paletteVisible = ref(false)
  const suggestions = ref([])

  function updateSuggestions (inputValue) {
    const trimmed = inputValue.trim()
    if (trimmed.startsWith('/') && !trimmed.includes('\n')) {
      const spaceIdx = trimmed.indexOf(' ')
      if (spaceIdx === -1) {
        const list = registry.getSuggestions(trimmed)
        suggestions.value = list
        paletteVisible.value = list.length > 0
        return
      }
    }
    paletteVisible.value = false
    suggestions.value = []
  }

  function tryExecute (inputValue, ctx) {
    const match = registry.matchCommand(inputValue)
    if (!match) return false
    match.cmd.handler(match.argsStr, ctx)
    paletteVisible.value = false
    return true
  }

  function dismiss () {
    paletteVisible.value = false
  }

  return { paletteVisible, suggestions, updateSuggestions, tryExecute, dismiss }
}
