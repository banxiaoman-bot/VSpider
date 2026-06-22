// 方向D · 从 App.vue 抽离：WebSocket 运行事件分发器（handleSocketMessage）。
//
// 把后端 WS 推送的 log / image|screenshot / done / phase / status 各类型路由到对应
// 子系统。所有依赖经 deps 注入（截图、HITL、browser-runtime、底部 Tab 红点、final
// answer、timeline replay、phase buffer、artifacts、模板 ref 等），本模块不持有状态，
// 只编排。行为与原 App.vue 内联 handleSocketMessage 逐字一致。
export const PHASE_LIMIT = 500

export function useRunEventRouter (deps) {
  const {
    appendLog,
    pushScreenshotFrame,
    isRunning,
    isHumanInterventionRequired,
    humanInterventionReason,
    hitlScreenshot,
    hitlFormFields,
    hitlFormReason,
    hitlFormScreenshot,
    hitlFormLoading,
    hitlFormVisible,
    fetchBrowserRuntimeStatus,
    runHistoryRefreshToken,
    activeBottomTab,
    hasNewRuns,
    hasNewCapability,
    hasNewPhase,
    timelineAutoScroll,
    failedRunsPaneRef,
    applyDoneAnswer,
    replayMode,
    phaseEvents,
    fetchArtifacts,
    hasNewArtifacts,
    currentImageBase64,
    timelinePanelRef,
  } = deps

  const handleSocketMessage = async (event) => {
    try {
      const payload = JSON.parse(event.data)
      if (payload.type === 'log') {
        const level = payload.level ? String(payload.level).toUpperCase() : 'INFO'
        const content = payload.content || ''
        await appendLog(`[${level}] ${content}`)
        return
      }

      if (payload.type === 'image' || payload.type === 'screenshot') {
        pushScreenshotFrame(payload.data)
        return
      }

      if (payload.type === 'done') {
        isRunning.value = false
        isHumanInterventionRequired.value = false
        fetchBrowserRuntimeStatus()
        runHistoryRefreshToken.value += 1
        if (activeBottomTab.value !== 'runs') {
          hasNewRuns.value = true
        }
        const text = payload.message || (payload.success ? '任务执行完成' : '任务执行结束')
        await appendLog(`[DONE] ${text}`)

        // K3: when the run failed, refetch the failed-runs list so the
        // drawer has the freshest entry. Pulse the badge dot if the user
        // isn't already viewing the panel. Fire-and-forget — we don't
        // await it so the rest of the done-handler stays responsive.
        if (payload.success === false) {
          failedRunsPaneRef.value?.fetchFailedRuns()
          if (activeBottomTab.value !== 'runs') {
            hasNewRuns.value = true
          }
        }

        applyDoneAnswer(payload, text)
        return
      }

      // G2/H1/I/L: phase events — agent timeline ticks
      if (payload.type === 'phase') {
        const phase = String(payload.phase || 'unknown')
        const sev = String(payload.severity || 'info')
        const dur = Number.isFinite(payload.duration_ms) ? `${payload.duration_ms}ms` : ''
        const stepStr = Number.isFinite(payload.step) ? `step ${payload.step}` : ''
        const msg = String(payload.message || '')
        // Compose: [PHASE/info] som_inject (step 3, 1240ms): 87 elements / 2 frames
        const tag = sev === 'warn' ? '[PHASE/WARN]' : sev === 'error' ? '[PHASE/ERR]' : '[PHASE]'
        const head = `${tag} ${phase}`
        const meta = [stepStr, dur].filter(Boolean).join(', ')
        const tail = msg ? `: ${msg}` : ''
        await appendLog(meta ? `${head} (${meta})${tail}` : `${head}${tail}`)
        // M: also push into the Timeline panel buffer. Keep the array
        // bounded so a 200-step run doesn't blow up memory.
        // W: drop incoming WS phase events while in replay mode so the
        //    imported buffer isn't contaminated.
        if (replayMode.value) {
          return
        }
        const evt = { ...payload, _ts: payload.ts || Date.now() / 1000 }
        phaseEvents.value.push(evt)
        if (phaseEvents.value.length > PHASE_LIMIT) {
          phaseEvents.value.splice(0, phaseEvents.value.length - PHASE_LIMIT)
        }
        if (
          ['capability_route', 'capability_execute'].includes(String(evt.phase || ''))
          && activeBottomTab.value !== 'capability'
        ) {
          hasNewCapability.value = true
        }
        if (activeBottomTab.value !== 'timeline') {
          hasNewPhase.value = true
        }
        // P: pin-to-bottom — only auto-scroll while the user is already
        // following the tail.
        if (timelineAutoScroll.value && activeBottomTab.value === 'timeline') {
          await timelinePanelRef.value?.scrollToBottom()
        }
        return
      }

      if (payload.type === 'status') {
        if (payload.status === 'new_artifact') {
          await fetchArtifacts()
          if (activeBottomTab.value !== 'artifacts') {
            hasNewArtifacts.value = true
          }
          await appendLog(`[ARTIFACT] ${payload.filename || 'new file'} ready`)
        }
        if (payload.status === 'human_intervention') {
          isHumanInterventionRequired.value = true
          humanInterventionReason.value = payload.reason || 'Agent 遇到需要人工处理的障碍'
          hitlScreenshot.value = payload.screenshot || currentImageBase64.value || ''
          await appendLog(`[HITL] ${humanInterventionReason.value}`)
        }
        if (payload.status === 'hitl_form') {
          hitlFormFields.value = Array.isArray(payload.fields) ? payload.fields : []
          hitlFormReason.value = payload.reason || '请填写以下信息'
          hitlFormScreenshot.value = payload.screenshot || ''
          hitlFormLoading.value = false
          hitlFormVisible.value = true
          await appendLog(`[HITL] 需要人工输入 ${hitlFormFields.value.length} 个字段`)
        }
        if (payload.status === 'human_resumed') {
          isHumanInterventionRequired.value = false
          humanInterventionReason.value = ''
          hitlFormVisible.value = false
          hitlFormLoading.value = false
          await appendLog('[HITL] Agent resumed')
        }
      }
    } catch (err) {
      await appendLog(`[WARN] 无法解析消息: ${String(err)}`)
    }
  }

  return { handleSocketMessage }
}
