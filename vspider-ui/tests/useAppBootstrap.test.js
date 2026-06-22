// Behavior lock for composables/useAppBootstrap.js (D-UI-29 part B)
//
// Pins the onMounted startup sequence previously inline in App.vue.
import { describe, it, expect, vi } from 'vitest'
import { ref } from 'vue'

import { runBootstrap } from '../src/composables/useAppBootstrap.js'

describe('runBootstrap', () => {
  function makeDeps (overrides = {}) {
    return {
      loadModelSettings: vi.fn(),
      registerBuiltinCommands: vi.fn(),
      slashRegistry: {},
      slashCommandDeps: {},
      connectWebSocket: vi.fn(),
      loadAuthProfiles: vi.fn(),
      loadCaptchaSolverStatus: vi.fn(),
      fetchArtifacts: vi.fn(),
      fetchBrowserRuntimeStatus: vi.fn(),
      failedRunsPaneRef: ref(null),
      ...overrides,
    }
  }

  it('calls all init functions once', () => {
    const deps = makeDeps()
    runBootstrap(deps)
    expect(deps.loadModelSettings).toHaveBeenCalledOnce()
    expect(deps.registerBuiltinCommands).toHaveBeenCalledOnce()
    expect(deps.connectWebSocket).toHaveBeenCalledOnce()
    expect(deps.loadAuthProfiles).toHaveBeenCalledOnce()
    expect(deps.loadCaptchaSolverStatus).toHaveBeenCalledOnce()
    expect(deps.fetchArtifacts).toHaveBeenCalledOnce()
    expect(deps.fetchBrowserRuntimeStatus).toHaveBeenCalledOnce()
  })

  it('passes slashRegistry + slashCommandDeps to registerBuiltinCommands', () => {
    const slashRegistry = { id: 'reg' }
    const slashCommandDeps = { foo: 'bar' }
    const deps = makeDeps({ slashRegistry, slashCommandDeps })
    runBootstrap(deps)
    expect(deps.registerBuiltinCommands).toHaveBeenCalledWith(slashRegistry, slashCommandDeps)
  })

  it('calls failedRunsPaneRef.value.fetchFailedRuns when ref is populated', () => {
    const fetchFailedRuns = vi.fn()
    const deps = makeDeps({ failedRunsPaneRef: ref({ fetchFailedRuns }) })
    runBootstrap(deps)
    expect(fetchFailedRuns).toHaveBeenCalledOnce()
  })

  it('does not throw when failedRunsPaneRef.value is null', () => {
    const deps = makeDeps({ failedRunsPaneRef: ref(null) })
    expect(() => runBootstrap(deps)).not.toThrow()
  })
})
