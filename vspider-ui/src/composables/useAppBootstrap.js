import { onMounted, onUnmounted, getCurrentInstance } from 'vue'

/**
 * Pure logic: the startup sequence called inside onMounted.
 * Exported separately so node-level tests can verify the call chain.
 */
export function runBootstrap (deps) {
  const {
    loadModelSettings,
    loadServerModelConfig,
    registerBuiltinCommands, slashRegistry, slashCommandDeps,
    connectWebSocket,
    loadAuthProfiles,
    loadCaptchaSolverStatus,
    fetchArtifacts,
    fetchBrowserRuntimeStatus,
    failedRunsPaneRef,
  } = deps

  loadModelSettings()
  if (typeof loadServerModelConfig === 'function') loadServerModelConfig()
  registerBuiltinCommands(slashRegistry, slashCommandDeps)
  connectWebSocket()
  loadAuthProfiles()
  loadCaptchaSolverStatus()
  fetchArtifacts()
  fetchBrowserRuntimeStatus()
  failedRunsPaneRef.value?.fetchFailedRuns()
}

/**
 * Composable: wraps onMounted → runBootstrap + onUnmounted → disconnectWebSocket.
 */
export function useAppBootstrap (deps) {
  if (getCurrentInstance()) {
    onMounted(() => runBootstrap(deps))
    onUnmounted(() => deps.disconnectWebSocket())
  }
}
