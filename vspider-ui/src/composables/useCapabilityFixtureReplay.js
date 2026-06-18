// 方向D · 从 App.vue 抽离：capability failure fixture / efficiency feedback 重放子系统。
//
// 持有 fixture/efficiency 重放的全部状态 + 派生 computed + 异步动作（生成/重放/拉取/批量重放），
// 并打包 capabilityReplayPaneProps 供 CapabilityReplayPane 绑定。网络经 apiFetch，
// 反馈经 ElMessage；外部依赖（url/prompt/fetchArtifacts/writeToClipboard + 3 个 capability-trace
// 计算值）由调用方注入。行为与原 App.vue 内联实现逐字一致。
import { ref, computed } from 'vue'
import { apiFetch } from '../api/client.js'
import { ElMessage } from 'element-plus'
import { buildFailureFixtureBatchReplaySummaryText } from './failureFixtureSummary'

export function useCapabilityFixtureReplay ({
  url,
  prompt,
  fetchArtifacts,
  writeToClipboard,
  latestCapabilityExecute,
  capabilityExecutionFailureBundle,
  capabilityExecutionEfficiencyCorrelationReport,
}) {
const capabilityFailureFixtureLoading = ref(false)

const capabilityFailureFixtureReplayLoading = ref(false)

const capabilityFailureFixtureReplayReport = ref(null)

const capabilityFailureFixtureReplayArtifact = ref(null)

const capabilityFailureFixtureLibraryLoading = ref(false)

const capabilityFailureFixtureBatchReplayLoading = ref(false)

const capabilityFailureFixtureBatchHistoryLoading = ref(false)

const capabilityFailureFixtureLibrary = ref([])

const capabilityFailureFixtureBatchHistory = ref([])

const capabilityFailureFixtureBatchHistoryTrend = ref(null)

const capabilityFailureFixtureBatchReplayReport = ref(null)

const capabilityFailureFixtureBatchReplayArtifact = ref(null)

const capabilityEfficiencyFeedbackReplayLoading = ref(false)

const capabilityEfficiencyFeedbackReplayLibraryLoading = ref(false)

const capabilityEfficiencyFeedbackReplayReport = ref(null)

const capabilityEfficiencyFeedbackReplayArtifact = ref(null)

const capabilityEfficiencyFeedbackReplayLibrary = ref([])

const capabilityEfficiencyFeedbackReplayChecks = computed(() =>
  Array.isArray(capabilityEfficiencyFeedbackReplayReport.value?.checks)
    ? capabilityEfficiencyFeedbackReplayReport.value.checks
    : [],
)

const capabilityEfficiencyFeedbackReplayFailedChecks = computed(() =>
  capabilityEfficiencyFeedbackReplayChecks.value.filter((check) => !check?.passed),
)

const capabilityEfficiencyFeedbackReplayStatus = computed(() =>
  capabilityEfficiencyFeedbackReplayReport.value?.passed ? 'passed' : 'failed',
)

const capabilityEfficiencyFeedbackReplayPlannerFeedback = computed(() =>
  capabilityEfficiencyFeedbackReplayReport.value?.planner_feedback || {},
)

const capabilityEfficiencyFeedbackReplayPreferredCapabilities = computed(() =>
  Array.isArray(capabilityEfficiencyFeedbackReplayPlannerFeedback.value?.preferred_capabilities)
    ? capabilityEfficiencyFeedbackReplayPlannerFeedback.value.preferred_capabilities.map((item) => String(item || '')).filter(Boolean)
    : [],
)

const capabilityEfficiencyFeedbackReplayAvoidActions = computed(() =>
  Array.isArray(capabilityEfficiencyFeedbackReplayPlannerFeedback.value?.avoid_actions)
    ? capabilityEfficiencyFeedbackReplayPlannerFeedback.value.avoid_actions.map((item) => String(item || '')).filter(Boolean)
    : [],
)

const capabilityEfficiencyFeedbackReplayLibraryCount = computed(() =>
  capabilityEfficiencyFeedbackReplayLibrary.value.length,
)

const capabilityFailureFixtureReplayChecks = computed(() =>
  Array.isArray(capabilityFailureFixtureReplayReport.value?.checks)
    ? capabilityFailureFixtureReplayReport.value.checks
    : [],
)

const capabilityFailureFixtureReplayFailedChecks = computed(() =>
  capabilityFailureFixtureReplayChecks.value.filter((check) => !check?.passed),
)

const capabilityFailureFixtureReplayStatus = computed(() =>
  capabilityFailureFixtureReplayReport.value?.passed ? 'passed' : 'failed',
)

const capabilityFailureFixtureLibraryCount = computed(() =>
  capabilityFailureFixtureLibrary.value.length,
)

const capabilityFailureFixtureBatchHistoryCount = computed(() =>
  capabilityFailureFixtureBatchHistory.value.length,
)

const capabilityFailureFixtureLatestBatchHistory = computed(() =>
  capabilityFailureFixtureBatchHistory.value[0] || null,
)

const capabilityFailureFixtureBatchHistoryTrendDirection = computed(() =>
  capabilityFailureFixtureBatchHistoryTrend.value?.direction || 'empty',
)

const capabilityFailureFixtureBatchHistoryTrendPassRateDelta = computed(() =>
  Math.round(Number(capabilityFailureFixtureBatchHistoryTrend.value?.pass_rate_delta || 0) * 1000) / 10,
)

const capabilityFailureFixtureBatchReplayItems = computed(() =>
  Array.isArray(capabilityFailureFixtureBatchReplayReport.value?.items)
    ? capabilityFailureFixtureBatchReplayReport.value.items
    : [],
)

const capabilityFailureFixtureBatchReplayFailedItems = computed(() =>
  capabilityFailureFixtureBatchReplayItems.value.filter((item) => !item?.passed),
)

const capabilityFailureFixtureBatchReplayFailedChecks = computed(() =>
  Array.isArray(capabilityFailureFixtureBatchReplayReport.value?.failed_checks)
    ? capabilityFailureFixtureBatchReplayReport.value.failed_checks
    : [],
)

const capabilityFailureFixtureBatchReplaySummary = computed(() =>
  capabilityFailureFixtureBatchReplayReport.value?.summary || {},
)

const capabilityFailureFixtureBatchReplayTopPrimaryFailures = computed(() =>
  Array.isArray(capabilityFailureFixtureBatchReplaySummary.value?.top_primary_failures)
    ? capabilityFailureFixtureBatchReplaySummary.value.top_primary_failures
    : [],
)

const capabilityFailureFixtureBatchReplayTopFailureCategories = computed(() =>
  Array.isArray(capabilityFailureFixtureBatchReplaySummary.value?.top_failure_categories)
    ? capabilityFailureFixtureBatchReplaySummary.value.top_failure_categories
    : [],
)

const capabilityFailureFixtureBatchReplayTopFailedChecks = computed(() =>
  Array.isArray(capabilityFailureFixtureBatchReplaySummary.value?.top_failed_checks)
    ? capabilityFailureFixtureBatchReplaySummary.value.top_failed_checks
    : [],
)

const capabilityFailureFixtureBatchReplayStatus = computed(() =>
  capabilityFailureFixtureBatchReplayReport.value?.passed ? 'passed' : 'failed',
)

const buildCapabilityFailureFixtureBatchReplaySummaryText = () => {
  return buildFailureFixtureBatchReplaySummaryText({
    report: capabilityFailureFixtureBatchReplayReport.value || {},
    summary: capabilityFailureFixtureBatchReplaySummary.value || {},
    artifact: capabilityFailureFixtureBatchReplayArtifact.value || {},
    failedChecks: capabilityFailureFixtureBatchReplayFailedChecks.value,
    topPrimaryFailures: capabilityFailureFixtureBatchReplayTopPrimaryFailures.value,
    topFailureCategories: capabilityFailureFixtureBatchReplayTopFailureCategories.value,
    topFailedChecks: capabilityFailureFixtureBatchReplayTopFailedChecks.value,
    failedItems: capabilityFailureFixtureBatchReplayFailedItems.value,
  })
}


const capabilityReplayPaneProps = computed(() => ({
  efficiencyReplay: {
    report: capabilityEfficiencyFeedbackReplayReport.value,
    status: capabilityEfficiencyFeedbackReplayStatus.value,
    plannerFeedback: capabilityEfficiencyFeedbackReplayPlannerFeedback.value,
    preferredCapabilities: capabilityEfficiencyFeedbackReplayPreferredCapabilities.value,
    avoidActions: capabilityEfficiencyFeedbackReplayAvoidActions.value,
    artifact: capabilityEfficiencyFeedbackReplayArtifact.value,
    checks: capabilityEfficiencyFeedbackReplayChecks.value,
    failedChecks: capabilityEfficiencyFeedbackReplayFailedChecks.value,
  },
  efficiencyLibrary: {
    items: capabilityEfficiencyFeedbackReplayLibrary.value,
    count: capabilityEfficiencyFeedbackReplayLibraryCount.value,
    loading: capabilityEfficiencyFeedbackReplayLibraryLoading.value,
  },
  fixtureReplay: {
    report: capabilityFailureFixtureReplayReport.value,
    status: capabilityFailureFixtureReplayStatus.value,
    artifact: capabilityFailureFixtureReplayArtifact.value,
    checks: capabilityFailureFixtureReplayChecks.value,
    failedChecks: capabilityFailureFixtureReplayFailedChecks.value,
  },
  fixtureLibrary: {
    items: capabilityFailureFixtureLibrary.value,
    count: capabilityFailureFixtureLibraryCount.value,
    loading: capabilityFailureFixtureLibraryLoading.value,
  },
  batchHistory: {
    items: capabilityFailureFixtureBatchHistory.value,
    count: capabilityFailureFixtureBatchHistoryCount.value,
    loading: capabilityFailureFixtureBatchHistoryLoading.value,
    latest: capabilityFailureFixtureLatestBatchHistory.value,
    trend: capabilityFailureFixtureBatchHistoryTrend.value,
    trendDirection: capabilityFailureFixtureBatchHistoryTrendDirection.value,
    trendPassRateDelta: capabilityFailureFixtureBatchHistoryTrendPassRateDelta.value,
  },
  batchReplay: {
    report: capabilityFailureFixtureBatchReplayReport.value,
    status: capabilityFailureFixtureBatchReplayStatus.value,
    summary: capabilityFailureFixtureBatchReplaySummary.value,
    topPrimaryFailures: capabilityFailureFixtureBatchReplayTopPrimaryFailures.value,
    topFailureCategories: capabilityFailureFixtureBatchReplayTopFailureCategories.value,
    topFailedChecks: capabilityFailureFixtureBatchReplayTopFailedChecks.value,
    artifact: capabilityFailureFixtureBatchReplayArtifact.value,
    failedChecks: capabilityFailureFixtureBatchReplayFailedChecks.value,
    items: capabilityFailureFixtureBatchReplayItems.value,
  },
}))


const copyCapabilityFailureFixtureBatchReplaySummary = async () => {
  if (!capabilityFailureFixtureBatchReplayReport.value) {
    ElMessage.warning('当前没有可复制的 batch replay 摘要')
    return
  }
  const ok = await writeToClipboard(buildCapabilityFailureFixtureBatchReplaySummaryText())
  if (ok) {
    ElMessage.success('已复制 batch replay 摘要')
  }
}


const generateCapabilityFailureFixture = async () => {
  const failureBundle = capabilityExecutionFailureBundle.value || {}
  if (failureBundle.version !== 'capability_execute_failure_bundle.v1') {
    ElMessage.warning('当前没有可生成 fixture 的 capability failure bundle')
    return
  }
  if (capabilityFailureFixtureLoading.value) return
  capabilityFailureFixtureLoading.value = true
  try {
    const response = await apiFetch('/api/capabilities/failure_fixture', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source: latestCapabilityExecute.value || { failure_bundle: failureBundle },
        name: `capability_${failureBundle.action || 'failure'}_${failureBundle.primary_failure || 'failure'}`,
        tags: ['frontend', 'capability_trace'],
        save: true,
      }),
    })
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.detail || result.message || '生成 failure fixture 失败')
    }
    const artifact = result.result?.artifact || {}
    if (artifact.url) await fetchArtifacts()
    if (artifact.url) await fetchCapabilityFailureFixtures(true)
    ElMessage.success(artifact.url ? `已生成 failure fixture: ${artifact.url}` : '已生成 failure fixture')
  } catch (err) {
    ElMessage.error(`生成 failure fixture 失败: ${String(err)}`)
  } finally {
    capabilityFailureFixtureLoading.value = false
  }
}


const replayCapabilityFailureFixture = async () => {
  const failureBundle = capabilityExecutionFailureBundle.value || {}
  if (failureBundle.version !== 'capability_execute_failure_bundle.v1') {
    ElMessage.warning('当前没有可验证 replay 的 capability failure bundle')
    return
  }
  if (capabilityFailureFixtureReplayLoading.value) return
  capabilityFailureFixtureReplayLoading.value = true
  try {
    const fixtureResponse = await apiFetch('/api/capabilities/failure_fixture', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source: latestCapabilityExecute.value || { failure_bundle: failureBundle },
        name: `capability_${failureBundle.action || 'failure'}_${failureBundle.primary_failure || 'failure'}_replay`,
        tags: ['frontend', 'capability_trace', 'replay'],
      }),
    })
    const fixtureResult = await fixtureResponse.json()
    if (!fixtureResponse.ok || fixtureResult.status !== 'success') {
      throw new Error(fixtureResult.detail || fixtureResult.message || '生成 replay fixture 失败')
    }
    const replayResponse = await apiFetch('/api/capabilities/failure_fixture/replay', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        fixture: fixtureResult.result?.fixture || {},
        save: true,
      }),
    })
    const replayResult = await replayResponse.json()
    if (!replayResponse.ok || replayResult.status !== 'success') {
      throw new Error(replayResult.detail || replayResult.message || '验证 failure fixture replay 失败')
    }
    capabilityFailureFixtureReplayReport.value = replayResult.result?.report || null
    capabilityFailureFixtureReplayArtifact.value = replayResult.result?.artifact || null
    if (capabilityFailureFixtureReplayArtifact.value?.url) await fetchArtifacts()
    const passed = Boolean(capabilityFailureFixtureReplayReport.value?.passed)
    if (passed) {
      ElMessage.success('Failure fixture replay 验证通过')
    } else {
      ElMessage.warning('Failure fixture replay 验证未通过')
    }
  } catch (err) {
    ElMessage.error(`验证 failure fixture replay 失败: ${String(err)}`)
  } finally {
    capabilityFailureFixtureReplayLoading.value = false
  }
}


const replayCapabilityEfficiencyFeedback = async () => {
  const report = capabilityExecutionEfficiencyCorrelationReport.value || {}
  if (report.version !== 'efficiency_correlation_report.v1') {
    ElMessage.warning('当前没有可验证 replay 的 efficiency correlation report')
    return
  }
  if (capabilityEfficiencyFeedbackReplayLoading.value) return
  capabilityEfficiencyFeedbackReplayLoading.value = true
  try {
    const response = await apiFetch('/api/capabilities/efficiency_feedback/replay', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source: latestCapabilityExecute.value || { efficiency_correlation_report: report },
        efficiency_correlation_report: report,
        goal: prompt.value,
        url: url.value,
        name: 'frontend_efficiency_feedback_replay',
        save: true,
      }),
    })
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.detail || result.message || '验证 efficiency feedback replay 失败')
    }
    capabilityEfficiencyFeedbackReplayReport.value = result.result?.report || null
    capabilityEfficiencyFeedbackReplayArtifact.value = result.result?.artifact || null
    if (capabilityEfficiencyFeedbackReplayArtifact.value?.url) await fetchArtifacts()
    if (capabilityEfficiencyFeedbackReplayArtifact.value?.url) await fetchCapabilityEfficiencyFeedbackReplays(true)
    if (capabilityEfficiencyFeedbackReplayReport.value?.passed) {
      ElMessage.success('Efficiency feedback replay 验证通过')
    } else {
      ElMessage.warning('Efficiency feedback replay 验证未通过')
    }
  } catch (err) {
    ElMessage.error(`验证 efficiency feedback replay 失败: ${String(err)}`)
  } finally {
    capabilityEfficiencyFeedbackReplayLoading.value = false
  }
}


const fetchCapabilityEfficiencyFeedbackReplays = async (silent = false) => {
  if (capabilityEfficiencyFeedbackReplayLibraryLoading.value) return
  capabilityEfficiencyFeedbackReplayLibraryLoading.value = true
  try {
    const response = await apiFetch('/api/capabilities/efficiency_feedback/replays?limit=20')
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.detail || result.message || '读取 efficiency feedback replay library 失败')
    }
    capabilityEfficiencyFeedbackReplayLibrary.value = Array.isArray(result.result?.reports)
      ? result.result.reports
      : []
    if (silent !== true) {
      ElMessage.success(`已加载 ${capabilityEfficiencyFeedbackReplayLibrary.value.length} 条 efficiency feedback replay reports`)
    }
  } catch (err) {
    ElMessage.error(`读取 efficiency feedback replay library 失败: ${String(err)}`)
  } finally {
    capabilityEfficiencyFeedbackReplayLibraryLoading.value = false
  }
}


const fetchCapabilityFailureFixtures = async (silent = false) => {
  if (capabilityFailureFixtureLibraryLoading.value) return
  capabilityFailureFixtureLibraryLoading.value = true
  try {
    const response = await apiFetch('/api/capabilities/failure_fixtures?limit=100')
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.detail || result.message || '读取 failure fixture library 失败')
    }
    capabilityFailureFixtureLibrary.value = Array.isArray(result.result?.fixtures)
      ? result.result.fixtures
      : []
    if (silent !== true) {
      ElMessage.success(`已加载 ${capabilityFailureFixtureLibrary.value.length} 个 failure fixtures`)
    }
  } catch (err) {
    ElMessage.error(`读取 failure fixture library 失败: ${String(err)}`)
  } finally {
    capabilityFailureFixtureLibraryLoading.value = false
  }
}


const fetchCapabilityFailureFixtureBatchHistory = async (silent = false) => {
  if (capabilityFailureFixtureBatchHistoryLoading.value) return
  capabilityFailureFixtureBatchHistoryLoading.value = true
  try {
    const response = await apiFetch('/api/capabilities/failure_fixture/replay_batches?limit=20')
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.detail || result.message || '读取 batch replay history 失败')
    }
    capabilityFailureFixtureBatchHistory.value = Array.isArray(result.result?.reports)
      ? result.result.reports
      : []
    capabilityFailureFixtureBatchHistoryTrend.value = result.result?.trend || null
    if (silent !== true) {
      ElMessage.success(`已加载 ${capabilityFailureFixtureBatchHistory.value.length} 条 batch replay history`)
    }
  } catch (err) {
    ElMessage.error(`读取 batch replay history 失败: ${String(err)}`)
  } finally {
    capabilityFailureFixtureBatchHistoryLoading.value = false
  }
}


const batchReplayCapabilityFailureFixtures = async () => {
  if (capabilityFailureFixtureBatchReplayLoading.value) return
  capabilityFailureFixtureBatchReplayLoading.value = true
  try {
    const response = await apiFetch('/api/capabilities/failure_fixture/replay_batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        limit: 100,
        name: 'frontend_capability_failure_fixture_batch_replay',
        save: true,
      }),
    })
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.detail || result.message || '批量验证 failure fixtures 失败')
    }
    capabilityFailureFixtureBatchReplayReport.value = result.result?.report || null
    capabilityFailureFixtureBatchReplayArtifact.value = result.result?.artifact || null
    if (capabilityFailureFixtureBatchReplayArtifact.value?.url) await fetchArtifacts()
    await fetchCapabilityFailureFixtures(true)
    await fetchCapabilityFailureFixtureBatchHistory(true)
    const count = Number(capabilityFailureFixtureBatchReplayReport.value?.fixture_count || 0)
    const passed = Boolean(capabilityFailureFixtureBatchReplayReport.value?.passed)
    if (!count) {
      ElMessage.warning('当前没有可批量验证的 failure fixtures')
    } else if (passed) {
      ElMessage.success(`Failure fixture batch replay 全部通过 (${count})`)
    } else {
      ElMessage.warning(`Failure fixture batch replay 未通过 ${capabilityFailureFixtureBatchReplayFailedItems.value.length}/${count}`)
    }
  } catch (err) {
    ElMessage.error(`批量验证 failure fixtures 失败: ${String(err)}`)
  } finally {
    capabilityFailureFixtureBatchReplayLoading.value = false
  }
}


  return {
    capabilityReplayPaneProps,
    copyCapabilityFailureFixtureBatchReplaySummary,
    generateCapabilityFailureFixture,
    replayCapabilityFailureFixture,
    replayCapabilityEfficiencyFeedback,
    fetchCapabilityEfficiencyFeedbackReplays,
    fetchCapabilityFailureFixtures,
    fetchCapabilityFailureFixtureBatchHistory,
    batchReplayCapabilityFailureFixtures,
  }
}
