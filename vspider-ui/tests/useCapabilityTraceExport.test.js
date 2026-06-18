// Behavior lock for composables/useCapabilityTraceExport.js (方向D · 从 App.vue 抽离)
//
// Pins the capability-trace export/summary/copy trio:
//   - export/copy guard on an empty trace (warn, no clipboard / no DOM work)
//   - buildCapabilityTraceSummaryText composes the same labelled lines from the
//     injected useCapabilityTrace bag (health/intent/alignment + optional sections)
//   - copy happy path writes the summary via the injected writeToClipboard + toasts
//   - export happy path strips _ts, names the file capability_trace_<stamp>.jsonl,
//     and toasts the exported count (DOM/URL stubbed since vitest runs in node env)
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { ref } from 'vue'

vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

import { useCapabilityTraceExport } from '../src/composables/useCapabilityTraceExport.js'
import { ElMessage } from 'element-plus'

function trace (over = {}) {
  const def = {
    capabilityTraceEvents: ref([]),
    capabilityTraceHealth: ref({ label: 'ok', route: 1, execute: 2, issues: 0 }),
    capabilityExecutionAlignment: ref({}),
    capabilityIntent: ref({}),
    capabilityRuntimePreflight: ref({}),
    capabilityExecutionRuntimeIssueSummary: ref({}),
    capabilityExecutionRuntimeDrift: ref({}),
    capabilityExecutionRuntimeIssues: ref([]),
    capabilityExecutionRuntimeActions: ref([]),
    capabilityExecutionActionTrace: ref({}),
    capabilityExecutionActionIssueSummary: ref({}),
    capabilityExecutionActionIssues: ref([]),
    capabilityExecutionActionIssueActions: ref([]),
    capabilityExecutionActionFailureSummary: ref({}),
    capabilityExecutionActionRecoveryActions: ref([]),
    capabilityActiveCrawlEfficiencyPlan: ref({}),
    capabilityExecutionCrawlEfficiencyCandidates: ref([]),
    capabilityExecutionCrawlEfficiencyAvailablePaths: ref([]),
    capabilityExecutionEfficiencyCorrelationReport: ref({}),
    capabilityExecutionEfficiencyCorrelationAlignment: ref({}),
    capabilityExecutionEfficiencyCorrelationRootCauses: ref([]),
    capabilityExecutionEfficiencyCorrelationActions: ref([]),
  }
  return { ...def, ...over }
}

function mk (over = {}) {
  const writeToClipboard = vi.fn(async () => true)
  const ex = useCapabilityTraceExport({ trace: trace(over), writeToClipboard })
  return { ex, writeToClipboard }
}

beforeEach(() => vi.clearAllMocks())

describe('useCapabilityTraceExport guards (empty trace)', () => {
  it('export warns and does no DOM work', () => {
    const { ex } = mk()
    expect(() => ex.exportCapabilityTraceAsJsonl()).not.toThrow()
    expect(ElMessage.warning).toHaveBeenCalledTimes(1)
    expect(ElMessage.success).not.toHaveBeenCalled()
  })

  it('copy warns and never touches the clipboard', async () => {
    const { ex, writeToClipboard } = mk()
    await ex.copyCapabilityTraceSummary()
    expect(ElMessage.warning).toHaveBeenCalledTimes(1)
    expect(writeToClipboard).not.toHaveBeenCalled()
  })
})

describe('useCapabilityTraceExport.buildCapabilityTraceSummaryText', () => {
  it('composes header + health + intent + alignment lines', () => {
    const { ex } = mk({
      capabilityIntent: ref({ task_type: 'extract', output_mode: 'rows' }),
      capabilityExecutionAlignment: ref({
        planHead: 'list_extract', capability: 'list_extract', rank: 1,
        topChoice: true, completed: true,
      }),
    })
    const text = ex.buildCapabilityTraceSummaryText()
    expect(text.startsWith('# Capability Trace Summary')).toBe(true)
    expect(text).toContain('Health: ok')
    expect(text).toContain('Route events: 1')
    expect(text).toContain('Intent: extract')
    expect(text).toContain('Output mode: rows')
    expect(text).toContain('Preferred: list_extract')
    expect(text).toContain('Executed: list_extract')
    expect(text).toContain('Plan rank: #1')
    expect(text).toContain('Top choice: yes')
    expect(text).toContain('Completed: yes')
    expect(text.endsWith('\n')).toBe(true)
  })

  it('renders optional crawl-efficiency + runtime-issue sections only when versioned', () => {
    const { ex } = mk({
      capabilityActiveCrawlEfficiencyPlan: ref({ version: 1, recommended_path: 'api', skip_browser: true, skip_vlm: false }),
      capabilityExecutionRuntimeIssueSummary: ref({ version: 1, status: 'warn', issue_count: 2, recommended_action: 'retry' }),
      capabilityExecutionRuntimeIssues: ref([{ source: 'runtime', code: 'timeout' }]),
    })
    const text = ex.buildCapabilityTraceSummaryText()
    expect(text).toContain('Crawl efficiency path: api')
    expect(text).toContain('Crawl efficiency skip browser: yes')
    expect(text).toContain('Crawl efficiency skip VLM: no')
    expect(text).toContain('Runtime issues: warn (2)')
    expect(text).toContain('Runtime action: retry')
    expect(text).toContain('Runtime issue codes: runtime:timeout')
  })

  it('falls back to "not in plan" when capability is off-plan', () => {
    const { ex } = mk({
      capabilityExecutionAlignment: ref({ capability: 'mystery_cap' }),
    })
    expect(ex.buildCapabilityTraceSummaryText()).toContain('Plan rank: not in plan')
  })
})

describe('useCapabilityTraceExport.copyCapabilityTraceSummary', () => {
  it('writes the built summary + toasts on success', async () => {
    const { ex, writeToClipboard } = mk({
      capabilityTraceEvents: ref([{ phase: 'capability_route' }]),
    })
    await ex.copyCapabilityTraceSummary()
    expect(writeToClipboard).toHaveBeenCalledTimes(1)
    expect(String(writeToClipboard.mock.calls[0][0])).toContain('# Capability Trace Summary')
    expect(ElMessage.success).toHaveBeenCalledWith('已复制 capability trace 摘要')
  })

  it('no success toast when clipboard write fails', async () => {
    const writeToClipboard = vi.fn(async () => false)
    const ex = useCapabilityTraceExport({
      trace: trace({ capabilityTraceEvents: ref([{ phase: 'x' }]) }),
      writeToClipboard,
    })
    await ex.copyCapabilityTraceSummary()
    expect(writeToClipboard).toHaveBeenCalledTimes(1)
    expect(ElMessage.success).not.toHaveBeenCalled()
  })
})

describe('useCapabilityTraceExport.exportCapabilityTraceAsJsonl (DOM stubbed)', () => {
  let anchor
  beforeEach(() => {
    anchor = { href: '', download: '', click: vi.fn() }
    vi.stubGlobal('URL', { createObjectURL: vi.fn(() => 'blob:x'), revokeObjectURL: vi.fn() })
    vi.stubGlobal('document', {
      createElement: vi.fn(() => anchor),
      body: { appendChild: vi.fn(), removeChild: vi.fn() },
    })
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('strips _ts, names file capability_trace_<stamp>.jsonl, toasts count', () => {
    const { ex } = mk({
      capabilityTraceEvents: ref([
        { phase: 'capability_route', _ts: 123, action: 'go' },
        { phase: 'capability_execute', _ts: 456 },
      ]),
    })
    ex.exportCapabilityTraceAsJsonl()
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
    const blob = URL.createObjectURL.mock.calls[0][0]
    expect(blob).toBeInstanceOf(Blob)
    expect(anchor.download).toMatch(/^capability_trace_\d{8}_\d{6}\.jsonl$/)
    expect(anchor.click).toHaveBeenCalledTimes(1)
    expect(ElMessage.success).toHaveBeenCalledWith('已导出 2 条 capability trace 事件')
  })
})
