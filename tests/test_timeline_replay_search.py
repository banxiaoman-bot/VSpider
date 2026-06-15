"""Source-pin tests for V/W/X frontend enhancements.

These tests assert the shape of the implementation rather than executing
Vue. They protect the wiring for:

* V: Timeline phase stats / histogram panel
* W: Offline phase JSONL replay import mode
* X: Live Terminal in-content search

The phase stats panel, replay file input, and timeline-specific UI were
extracted from App.vue into TimelinePanel.vue during the Y126 component
split. Tests now read both files where needed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_UI_SRC = Path(__file__).resolve().parent.parent / "vspider-ui" / "src"
APP_VUE = _UI_SRC / "App.vue"
TIMELINE_PANEL = _UI_SRC / "components" / "TimelinePanel.vue"
TERMINAL_LOG_PANE = _UI_SRC / "components" / "TerminalLogPane.vue"
FAILURE_FIXTURE_SUMMARY = Path(__file__).resolve().parent.parent / "vspider-ui" / "src" / "composables" / "failureFixtureSummary.js"
CAPABILITY_TRACE_UTILS = Path(__file__).resolve().parent.parent / "vspider-ui" / "src" / "components" / "capabilityTraceUtils.js"
CAPABILITY_TRACE_LIST = Path(__file__).resolve().parent.parent / "vspider-ui" / "src" / "components" / "CapabilityTraceList.vue"
CAPABILITY_ALIGNMENT_CARD = Path(__file__).resolve().parent.parent / "vspider-ui" / "src" / "components" / "CapabilityAlignmentCard.vue"
CAPABILITY_EFFICIENCY_PANEL = Path(__file__).resolve().parent.parent / "vspider-ui" / "src" / "components" / "CapabilityEfficiencyPanel.vue"
CAPABILITY_REPLAY_PANE = Path(__file__).resolve().parent.parent / "vspider-ui" / "src" / "components" / "CapabilityReplayPane.vue"
CAPABILITY_SHARED_CSS = Path(__file__).resolve().parent.parent / "vspider-ui" / "src" / "styles" / "capability-shared.css"


@pytest.fixture(scope="module")
def src() -> str:
    return APP_VUE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def failure_fixture_summary_src() -> str:
    return FAILURE_FIXTURE_SUMMARY.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def capability_trace_utils_src() -> str:
    return CAPABILITY_TRACE_UTILS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def capability_trace_list_src() -> str:
    return CAPABILITY_TRACE_LIST.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def capability_alignment_card_src() -> str:
    return CAPABILITY_ALIGNMENT_CARD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def capability_efficiency_panel_src() -> str:
    return CAPABILITY_EFFICIENCY_PANEL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def capability_replay_pane_src() -> str:
    return CAPABILITY_REPLAY_PANE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def capability_shared_css_src() -> str:
    return CAPABILITY_SHARED_CSS.read_text(encoding="utf-8")


class TestVPhaseStats:
    @pytest.fixture(scope="class")
    def combined_src(self) -> str:
        """Phase stats were extracted into TimelinePanel.vue; read both."""
        app = APP_VUE.read_text(encoding="utf-8")
        timeline = TIMELINE_PANEL.read_text(encoding="utf-8")
        timeline_css_path = _UI_SRC / "styles" / "timeline-panel.css"
        timeline_css = timeline_css_path.read_text(encoding="utf-8") if timeline_css_path.exists() else ""
        return app + "\n" + timeline + "\n" + timeline_css

    def test_stats_state_refs_exist(self, combined_src: str) -> None:
        assert "const phaseStatsExpanded = ref(false)" in combined_src
        assert "const phaseStatsSortBy = ref('count')" in combined_src

    def test_phase_filter_options_carry_stats(self, combined_src: str) -> None:
        body = re.search(r"const phaseFilterOptions = computed\(\(\) => \{([\s\S]*?)\n\}\)", combined_src)
        assert body, "phaseFilterOptions computed must exist"
        text = body.group(1)
        for token in ["mean:", "p50:", "p95:", "max:", "sevCounts:", "durations:"]:
            assert token in text
        assert "pickPercentile(sorted, 0.95)" in text

    def test_stats_sorted_computed_exists(self, combined_src: str) -> None:
        assert "const phaseStatsSorted = computed" in combined_src
        assert "const key = phaseStatsSortBy.value" in combined_src
        assert "return bv - av" in combined_src

    def test_sparkline_helper_exists(self, combined_src: str) -> None:
        assert "const phaseSparklinePath = (durations) =>" in combined_src
        assert "viewBox=\"0 0 100 24\"" in combined_src
        assert ":d=\"phaseSparklinePath(row.durations)\"" in combined_src

    def test_stats_panel_template_wired(self, combined_src: str) -> None:
        assert "class=\"timeline-stats-panel\"" in combined_src
        assert "v-if=\"phaseStatsExpanded && phaseEvents.length\"" in combined_src
        assert "v-for=\"row in phaseStatsSorted\"" in combined_src
        assert "@click=\"togglePhaseFilter(row.phase)\"" in combined_src
        assert "Phase 耗时分布" in combined_src

    def test_stats_sort_buttons_present(self, combined_src: str) -> None:
        assert "v-for=\"key in ['count', 'mean', 'p95', 'max']\"" in combined_src
        assert "@click=\"phaseStatsSortBy = key\"" in combined_src

    def test_stats_css_present(self, combined_src: str) -> None:
        for cls in [
            ".timeline-stats-panel",
            ".timeline-stats-grid-head",
            ".timeline-stats-row",
            ".stat-spark",
            ".stat-sev-pill.sev-error",
        ]:
            assert cls in combined_src


class TestWReplayMode:
    @pytest.fixture(scope="class")
    def combined_src(self) -> str:
        """Replay UI was partially extracted to TimelinePanel.vue."""
        app = APP_VUE.read_text(encoding="utf-8")
        timeline = TIMELINE_PANEL.read_text(encoding="utf-8")
        timeline_css_path = _UI_SRC / "styles" / "timeline-panel.css"
        timeline_css = timeline_css_path.read_text(encoding="utf-8") if timeline_css_path.exists() else ""
        return app + "\n" + timeline + "\n" + timeline_css

    def test_replay_state_refs_exist(self, combined_src: str) -> None:
        assert "const replayMode = ref(false)" in combined_src
        assert "const replaySourceName = ref('')" in combined_src
        assert "const replayInputRef = ref(null)" in combined_src
        assert "const replayImportTarget = ref('timeline')" in combined_src

    def test_ws_phase_gate_drops_events_in_replay_mode(self, combined_src: str) -> None:
        gate = re.search(
            r"if \(replayMode\.value\) \{\s*return\s*\}\s*const evt = \{ \.\.\.payload",
            combined_src,
            flags=re.S,
        )
        assert gate, "WS phase ingestion must return before pushing phaseEvents in replay mode"

    def test_submit_task_exits_replay_mode(self, combined_src: str) -> None:
        m = re.search(
            r"if \(replayMode\.value\) \{\s*replayMode\.value = false\s*replaySourceName\.value = ''",
            combined_src,
            flags=re.S,
        )
        assert m

    def test_replay_parser_tolerates_bad_lines(self, combined_src: str) -> None:
        assert "const _parseJsonlText = (text) =>" in combined_src
        assert "const _capabilityExecuteArtifactPhaseEvent = (doc) =>" in combined_src
        assert "String(doc.type || '') !== 'capability_execute_trace'" in combined_src
        assert "phase: 'capability_execute'" in combined_src
        assert "execution_status: result.status" in combined_src
        assert "action_trace: result.action_trace" in combined_src
        assert "const artifactEvent = _capabilityExecuteArtifactPhaseEvent(doc)" in combined_src
        assert "if (artifactEvent) return { events: [artifactEvent], total: 1, bad: 0 }" in combined_src
        assert "replace(/\\r\\n?/g, '\\n').split('\\n')" in combined_src
        assert "JSON.parse(line)" in combined_src
        assert "const artifactEvent = _capabilityExecuteArtifactPhaseEvent(obj)" in combined_src
        assert "const phaseEvent = obj?.detail?.phase_event" in combined_src
        assert "out.push(phaseEvent && typeof phaseEvent === 'object' && !Array.isArray(phaseEvent) ? phaseEvent : obj)" in combined_src
        assert "bad += 1" in combined_src
        assert "return { events: out, total, bad }" in combined_src

    def test_replay_import_replaces_buffer_and_enters_mode(self, combined_src: str) -> None:
        assert "const handleReplayFileChange = async (event) =>" in combined_src
        assert "phaseEvents.value = events" in combined_src
        assert "replayMode.value = true" in combined_src
        assert "replaySourceName.value = file.name || 'imported.jsonl'" in combined_src
        assert "setActiveBottomTab(replayImportTarget.value === 'capability' ? 'capability' : 'timeline')" in combined_src

    def test_replay_ui_wired(self, combined_src: str) -> None:
        assert "@command=\"handleTimelineMoreAction\"" in combined_src
        assert "@command=\"handleCapabilityMoreAction\"" in combined_src
        assert "command=\"importReplay\"" in combined_src
        assert "importReplay:" in combined_src
        assert "ref=\"replayInputRef\"" in combined_src
        assert "@change=\"handleReplayFileChange\"" in combined_src
        assert "class=\"timeline-replay-banner\"" in combined_src
        assert "class=\"timeline-replay-banner capability-replay-banner\"" in combined_src
        assert "@click=\"exitReplayMode\"" in combined_src

    def test_replay_css_present(self, combined_src: str) -> None:
        assert ".replay-file-input" in combined_src
        assert ".timeline-replay-banner" in combined_src
        assert ".timeline-replay-banner .replay-exit-btn" in combined_src
        assert ".capability-replay-banner" in combined_src


class TestXTerminalSearch:
    @pytest.fixture(scope="class")
    def combined_src(self) -> str:
        """Terminal search was extracted into TerminalLogPane.vue + CSS."""
        app = APP_VUE.read_text(encoding="utf-8")
        terminal = TERMINAL_LOG_PANE.read_text(encoding="utf-8")
        css_path = _UI_SRC / "styles" / "terminal-log-pane.css"
        css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
        return app + "\n" + terminal + "\n" + css

    def test_search_state_refs_exist(self, combined_src: str) -> None:
        assert "const terminalSearchVisible = ref(false)" in combined_src
        assert "const terminalSearchQuery = ref('')" in combined_src
        assert "const terminalSearchCurrent = ref(0)" in combined_src
        assert "const terminalSearchInputRef = ref(null)" in combined_src

    def test_search_computeds_exist(self, combined_src: str) -> None:
        for token in [
            "const terminalSearchActive = computed",
            "const terminalSearchMatches = computed",
            "const terminalSearchSegments = computed",
            "const terminalSearchTotal = computed",
        ]:
            assert token in combined_src
        assert "line.indexOf(q, from)" in combined_src
        assert "kind: m.gIdx === cur ? 'current' : 'hit'" in combined_src

    def test_search_helpers_exist(self, combined_src: str) -> None:
        for token in [
            "const openTerminalSearch = () =>",
            "const closeTerminalSearch = () =>",
            "const terminalSearchNext = () =>",
            "const terminalSearchPrev = () =>",
        ]:
            assert token in combined_src
        assert "terminalSearchVisible.value = true" in combined_src
        assert "terminalSearchVisible.value = false" in combined_src

    def test_search_keyboard_shortcuts_wired(self, src: str) -> None:
        ctrl_f = re.search(
            r"activeBottomTab\.value === 'terminal'[\s\S]*?event\.key === 'f'[\s\S]*?openTerminalSearch\(\)",
            src,
        )
        assert ctrl_f, "Ctrl+F on terminal tab must open terminal search"
        assert "closeTerminalSearch()" in src
        assert "terminalSearchPrev()" in src
        assert "terminalSearchNext()" in src
        assert "event.key === 'Escape'" in src
        assert "event.key === 'Enter' && event.shiftKey" in src

    def test_search_template_wired(self, combined_src: str) -> None:
        assert "v-if=\"terminalSearchVisible\"" in combined_src
        assert "ref=\"terminalSearchInputRef\"" in combined_src
        assert "v-model=\"terminalSearchQuery\"" in combined_src
        assert "terminalSearchSegments.get(idx)" in combined_src
        assert "'terminal-search-hit': seg.kind === 'hit'" in combined_src
        assert "'terminal-search-current': seg.kind === 'current'" in combined_src

    def test_search_css_present(self, combined_src: str) -> None:
        for cls in [
            ".terminal-search-bar",
            ".terminal-search-input",
            ".terminal-search-count",
            ".terminal-search-hit",
            ".terminal-search-current",
        ]:
            assert cls in combined_src


class TestY33CapabilityTracePanel:
    def test_capability_state_and_computeds_exist(self, src: str, capability_trace_utils_src: str) -> None:
        for token in [
            "const hasNewCapability = ref(false)",
            "const capabilityFailureFixtureLoading = ref(false)",
            "const capabilityFailureFixtureReplayLoading = ref(false)",
            "const capabilityFailureFixtureReplayReport = ref(null)",
            "const capabilityFailureFixtureReplayArtifact = ref(null)",
            "const capabilityFailureFixtureLibraryLoading = ref(false)",
            "const capabilityFailureFixtureBatchReplayLoading = ref(false)",
            "const capabilityFailureFixtureBatchHistoryLoading = ref(false)",
            "const capabilityFailureFixtureLibrary = ref([])",
            "const capabilityFailureFixtureBatchHistory = ref([])",
            "const capabilityFailureFixtureBatchHistoryTrend = ref(null)",
            "const capabilityFailureFixtureBatchReplayReport = ref(null)",
            "const capabilityFailureFixtureBatchReplayArtifact = ref(null)",
            "const capabilityEfficiencyFeedbackReplayLoading = ref(false)",
            "const capabilityEfficiencyFeedbackReplayLibraryLoading = ref(false)",
            "const capabilityEfficiencyFeedbackReplayReport = ref(null)",
            "const capabilityEfficiencyFeedbackReplayArtifact = ref(null)",
            "const capabilityEfficiencyFeedbackReplayLibrary = ref([])",
            "const capabilityTraceFilter = ref('all')",
            "const capabilityTraceSearchQuery = ref('')",
            "const latestCapabilityRoute = computed",
            "const latestCapabilityExecute = computed",
            "const capabilityTraceEvents = computed",
            "from './components/capabilityTraceUtils'",
            "import CapabilityTraceList from './components/CapabilityTraceList.vue'",
            "const capabilityBackendPlan = computed",
            "const capabilityRouteCrawlEfficiencyPlan = computed",
            "const capabilityFallbackChain = computed",
            "const capabilityModelRoles = computed",
            "const capabilityTraceJson = computed",
            "const capabilityExecutionAttempts = computed",
            "const capabilityExecutionChecks = computed",
            "const capabilityExecuteJson = computed",
            "const capabilityTraceRows = computed",
            "const capabilityTraceSummary = computed",
            "const capabilityFilteredTraceRows = computed",
            "const capabilityExecutionAlignment = computed",
            "const capabilityTraceHealth = computed",
            "const capabilityExecutionRuntimeIssues = computed",
            "const capabilityExecutionRuntimeActions = computed",
            "const capabilityExecutionActionTrace = computed",
            "const capabilityExecutionActionIssueSummary = computed",
            "const capabilityExecutionActionIssues = computed",
            "const capabilityExecutionActionIssueActions = computed",
            "const capabilityExecutionActionFailureSummary = computed",
            "const capabilityExecutionActionRecoveryActions = computed",
            "const capabilityEfficiencyCorrelationStatusClass = computed",
            "const capabilityExecutionFailureBundle = computed",
            "const capabilityExecutionCrawlEfficiencyPlan = computed",
            "const capabilityActiveCrawlEfficiencyPlan = computed",
            "const capabilityExecutionCrawlEfficiencyCandidates = computed",
            "const capabilityExecutionCrawlEfficiencyAvailablePaths = computed",
            "const capabilityExecutionCrawlEfficiencySummary = computed",
            "const capabilityExecutionEfficiencyCorrelationReport = computed",
            "const capabilityExecutionEfficiencyCorrelationAlignment = computed",
            "const capabilityExecutionEfficiencyCorrelationRootCauses = computed",
            "const capabilityExecutionEfficiencyCorrelationPlannerHints = computed",
            "const capabilityExecutionEfficiencyCorrelationActions = computed",
            "const capabilityEfficiencyFeedbackReplayChecks = computed",
            "const capabilityEfficiencyFeedbackReplayFailedChecks = computed",
            "const capabilityEfficiencyFeedbackReplayStatus = computed",
            "const capabilityEfficiencyFeedbackReplayPlannerFeedback = computed",
            "const capabilityEfficiencyFeedbackReplayPreferredCapabilities = computed",
            "const capabilityEfficiencyFeedbackReplayAvoidActions = computed",
            "const capabilityEfficiencyFeedbackReplayLibraryCount = computed",
            "const capabilityFailureFixtureReplayChecks = computed",
            "const capabilityFailureFixtureReplayFailedChecks = computed",
            "const capabilityFailureFixtureReplayStatus = computed",
            "const capabilityFailureFixtureLibraryCount = computed",
            "const capabilityFailureFixtureBatchHistoryCount = computed",
            "const capabilityFailureFixtureLatestBatchHistory = computed",
            "const capabilityFailureFixtureBatchHistoryTrendDirection = computed",
            "const capabilityFailureFixtureBatchHistoryTrendPassRateDelta = computed",
            "const capabilityFailureFixtureBatchReplayItems = computed",
            "const capabilityFailureFixtureBatchReplayFailedItems = computed",
            "const capabilityFailureFixtureBatchReplayFailedChecks = computed",
            "const capabilityFailureFixtureBatchReplaySummary = computed",
            "const capabilityFailureFixtureBatchReplayTopPrimaryFailures = computed",
            "const capabilityFailureFixtureBatchReplayTopFailureCategories = computed",
            "const capabilityFailureFixtureBatchReplayTopFailedChecks = computed",
            "const capabilityFailureFixtureBatchReplayStatus = computed",
            "const hasRouteRuntimePreflightIssue = phase === 'capability_route'",
            "runtimePreflightStatus === 'warn'",
            "runtimePreflightWarningCount > 0",
            "const hasRuntimeIssue = phase === 'capability_execute'",
            "runtimeIssueStatus === 'warn'",
            "runtimeIssueCount > 0",
            "runtimeDriftStatus === 'warn'",
            "const hasActionIssue = phase === 'capability_execute'",
            "actionIssueStatus === 'warn'",
            "actionIssueStatus === 'error'",
            "actionIssueCount > 0",
            "const hasEfficiencyIssue = phase === 'capability_execute'",
            "['suboptimal', 'needs_repair', 'needs_replan'].includes(efficiencyCorrelationStatus)",
            "const hasTraceRuntimeIssue = hasRuntimeIssue || hasRouteRuntimePreflightIssue || hasActionIssue || hasEfficiencyIssue",
            "const previewSeverity = hasTraceRuntimeIssue && formatPhasePreviewSeverity(evt?.severity) === 'info'",
            "severity: previewSeverity",
            "const runtimeAlignment = runtimeIssueStatus === 'warn' || runtimeIssueCount > 0",
            "runtimeIssueAction && runtimeIssueAction !== 'continue'",
            "const routePreflightAlignment = routePreflightStatus === 'warn' || routePreflightWarnings.length > 0",
            "routePreflightAction && routePreflightAction !== 'continue'",
            "parts.push(`preflight=${runtimePreflightStatus}`)",
            "parts.push(`preflight_warnings=${runtimePreflightWarningCount}`)",
            "parts.push(`preflight_codes=${runtimePreflightWarnings.slice(0, 3).join(',')}`)",
            "parts.push(`runtime=${runtimeIssueStatus}`)",
            "parts.push(`runtime_codes=${runtimeIssueCodes.slice(0, 3).join(',')}`)",
            "parts.push(`browser_action=${actionTrace.action}`)",
            "parts.push(`action_codes=${actionIssueCodes.slice(0, 3).join(',')}`)",
            "parts.push(`failure=${actionFailureCode}`)",
            "parts.push(`failure_category=${actionFailureCategory}`)",
            "parts.push(`recovery=${actionRecoveryActions.slice(0, 3).join(',')}`)",
            "parts.push('trace_artifact=available')",
            "parts.push(`crawl_efficiency=${crawlEfficiencyPlan.recommended_path}`)",
            "parts.push(`skip_browser=${Boolean(crawlEfficiencyPlan.skip_browser)}`)",
            "parts.push(`efficiency_correlation=${efficiencyCorrelationReport.status}`)",
            "parts.push(`executed_path=${efficiencyCorrelationAlignment.executed_path}`)",
            "parts.push(`efficiency_causes=${efficiencyCorrelationRootCauses.slice(0, 3).join(',')}`)",
            "crawlEfficiencyCandidates.map((item) => `${item?.name || ''} ${item?.reason || ''}`).join(' ')",
            "const actionSearchText = [",
            "crawlEfficiencyPlan?.recommended_path",
            "efficiencyCorrelationReport?.status",
            "efficiencyCorrelationAlignment.executed_path",
            "efficiencyCorrelationRootCauses.join(' ')",
            "actionTrace?.target?.selector",
            "actionTrace?.action_ref?.selector",
            "actionWarningCodes.join(' ')",
            "actionFailureCode",
            "actionFailureCategory",
            "actionRecoveryActions.join(' ')",
            "traceArtifact.path",
            "traceArtifact.url",
            "searchText: actionSearchText",
            "const q = String(capabilityTraceSearchQuery.value || '').trim().toLowerCase()",
            "String(row.searchText || row.detail || '').toLowerCase().includes(q)",
            "import { buildFailureFixtureBatchReplaySummaryText } from './composables/failureFixtureSummary'",
            "const buildCapabilityTraceSummaryText = () =>",
            "const copyCapabilityTraceSummary = async () =>",
            "const buildCapabilityFailureFixtureBatchReplaySummaryText = () =>",
            "return buildFailureFixtureBatchReplaySummaryText({",
            "topPrimaryFailures: capabilityFailureFixtureBatchReplayTopPrimaryFailures.value",
            "const copyCapabilityFailureFixtureBatchReplaySummary = async () =>",
            "const generateCapabilityFailureFixture = async () =>",
            "const replayCapabilityFailureFixture = async () =>",
            "const replayCapabilityEfficiencyFeedback = async () =>",
            "const fetchCapabilityEfficiencyFeedbackReplays = async (silent = false) =>",
            "const fetchCapabilityFailureFixtures = async (silent = false) =>",
            "const fetchCapabilityFailureFixtureBatchHistory = async (silent = false) =>",
            "const batchReplayCapabilityFailureFixtures = async () =>",
        ]:
            assert token in src
        for token in [
            "export const capabilityEventActionTrace = (evt) =>",
            "evt?.detail?.action_trace",
            "export const capabilityEventActionIssueSummary = (evt, actionTrace = capabilityEventActionTrace(evt)) =>",
            "evt?.detail?.action_issue_summary",
            "export const capabilityEventTraceArtifact = (evt) =>",
            "evt?.detail?.trace_artifact",
            "export const capabilityEventFailureBundle = (evt) =>",
            "evt?.detail?.phase_event?.failure_bundle",
            "export const capabilityEventCrawlEfficiencyPlan = (evt) =>",
            "evt?.detail?.phase_event?.crawl_efficiency_plan",
            "export const capabilityEventEfficiencyCorrelationReport = (evt) =>",
            "evt?.detail?.phase_event?.efficiency_correlation_report",
            "export const capabilityCrawlEfficiencyCandidateClass = (item) =>",
            "export const capabilityCrawlEfficiencyEvidence = (item) =>",
        ]:
            assert token in capability_trace_utils_src

    def test_capability_ws_badge_wired(self, src: str) -> None:
        assert "['capability_route', 'capability_execute'].includes(String(evt.phase || ''))" in src
        assert "hasNewCapability.value = true" in src
        assert "if (name === 'capability') hasNewCapability.value = false" in src

    def test_failure_fixture_summary_helper_extracted(self, failure_fixture_summary_src: str) -> None:
        for token in [
            "export const formatFailureFixtureRows",
            "export const buildFailureFixtureBatchReplaySummaryText",
            "# Failure Fixture Batch Replay Summary",
            "Top primary failures: ${topPrimary}",
            "Top failure categories: ${topCategories}",
            "Top failed checks: ${topChecks}",
            "Failed fixtures:",
            "return `${lines.join('\\n')}\\n`",
        ]:
            assert token in failure_fixture_summary_src

    def test_capability_tab_template_wired(
        self,
        src: str,
        capability_trace_list_src: str,
        capability_alignment_card_src: str,
        capability_efficiency_panel_src: str,
        capability_replay_pane_src: str,
    ) -> None:
        for token in [
            '<el-tab-pane name="capability">',
            ':is-dot="hasNewCapability"',
            "Route-Aware Agent Guidance",
            "v-if=\"latestCapabilityRoute || latestCapabilityExecute\"",
            "@click=\"exportCapabilityTraceAsJsonl\"",
            "@command=\"handleCapabilityMoreAction\"",
            "copySummary: copyCapabilityTraceSummary,",
            "command=\"copySummary\"",
            "复制摘要",
            "generateFixture: generateCapabilityFailureFixture,",
            "command=\"generateFixture\"",
            "生成 Fixture",
            "capabilityFailureFixtureLoading",
            "replayFixture: replayCapabilityFailureFixture,",
            "command=\"replayFixture\"",
            "验证 Fixture",
            "capabilityFailureFixtureReplayLoading",
            "refreshFixtures: fetchCapabilityFailureFixtures,",
            "command=\"refreshFixtures\"",
            "刷新 Fixture 库",
            "capabilityFailureFixtureLibraryLoading",
            "refreshBatchHistory: fetchCapabilityFailureFixtureBatchHistory,",
            "command=\"refreshBatchHistory\"",
            "刷新 Replay 历史",
            "capabilityFailureFixtureBatchHistoryLoading",
            "batchReplay: batchReplayCapabilityFailureFixtures,",
            "command=\"batchReplay\"",
            "批量验证 Fixture",
            "capabilityFailureFixtureBatchReplayLoading",
            "capabilityExecutionFailureBundle.version !== 'capability_execute_failure_bundle.v1'",
            "importReplay: () => triggerReplayImport('capability'),",
            "command=\"importReplay\"",
            "Capability 回放模式",
            "<CapabilityTraceList",
            "v-model:filter=\"capabilityTraceFilter\"",
            "v-model:search-query=\"capabilityTraceSearchQuery\"",
            ":rows=\"capabilityFilteredTraceRows\"",
            "@open-row=\"(evt) => timelinePanelRef.value?.openPhaseDialog(evt)\"",
            "capabilityTraceHealth.label",
            "route {{ capabilityTraceHealth.route }}",
            "issues {{ capabilityTraceHealth.issues }}",
            "<CapabilityAlignmentCard",
            ":alignment=\"capabilityExecutionAlignment\"",
            "<CapabilityEfficiencyPanel",
            ":crawl-plan=\"capabilityActiveCrawlEfficiencyPlan\"",
            ":correlation-report=\"capabilityExecutionEfficiencyCorrelationReport\"",
            "执行遥测",
            "v-for=\"(issue, idx) in capabilityExecutionRuntimeIssues\"",
            "v-for=\"action in capabilityExecutionRuntimeActions\"",
            "v-if=\"capabilityExecutionActionIssueSummary.version\"",
            "capabilityExecutionActionFailureSummary.failure_code",
            "capabilityExecutionActionFailureSummary.failure_category",
            "v-for=\"(issue, idx) in capabilityExecutionActionIssues\"",
            "v-for=\"action in capabilityExecutionActionIssueActions\"",
            "v-for=\"action in capabilityExecutionActionRecoveryActions\"",
            "v-for=\"(attempt, idx) in capabilityExecutionAttempts\"",
            "v-for=\"check in capabilityExecutionChecks\"",
            "<CapabilityReplayPane",
            "v-bind=\"capabilityReplayPaneProps\"",
            "<CapabilityDiagnosticsPane",
            ":fallback-chain=\"capabilityFallbackChain\"",
            ":role-rows=\"capabilityRoleRows\"",
            "capabilityTraceJson || capabilityExecuteJson",
        ]:
            assert token in src
        for token in [
            "Failure Fixture Replay",
            "fixtureReplay.report.fixture?.primary_failure",
            "fixtureReplay.report.planner_feedback?.passed",
            "fixtureReplay.report.route?.passed",
            "fixtureReplay.report.execution_plan?.passed",
            "Failure Fixture Library",
            "Failure Fixture Batch History",
            "Failure Fixture Batch Replay",
            "Efficiency Feedback Replay",
        ]:
            assert token in capability_replay_pane_src
        for token in [
            "Trace 历史",
            "v-for=\"item in filters\"",
            "props.summary[item] || 0",
            "placeholder=\"搜索 action / selector / issue code\"",
            "{{ props.rows.length }}/{{ props.totalRows }}",
            "v-for=\"row in props.rows\"",
            "@click=\"openRow(row)\"",
        ]:
            assert token in capability_trace_list_src
        for token in [
            "Route / Execute 对齐",
            "props.alignment.topChoice",
            "plan rank #{{ props.alignment.rank }}",
            "fallback: {{ props.alignment.fallbackReason }}",
        ]:
            assert token in capability_alignment_card_src
        for token in [
            "Crawl Efficiency",
            "path: {{ props.crawlPlan.recommended_path || 'unknown' }}",
            "skip browser: {{ props.crawlPlan.skip_browser ? 'yes' : 'no' }}",
            "skip VLM: {{ props.crawlPlan.skip_vlm ? 'yes' : 'no' }}",
            "v-for=\"path in props.availablePaths\"",
            "v-for=\"candidate in props.candidates\"",
            ":class=\"props.candidateClass(candidate)\"",
            "props.candidateEvidence(candidate)",
            "runtime={{ props.summary.runtime_status }}",
            "Efficiency Correlation",
            "{{ props.correlationReport.status || 'unknown' }}",
            "recommended: {{ props.correlationAlignment.recommended_path || 'unknown' }}",
            "executed: {{ props.correlationAlignment.executed_path || 'unknown' }}",
            "rank delta {{ props.correlationAlignment.rank_gap }}",
            "v-for=\"cause in props.rootCauses\"",
            "v-for=\"(hint, idx) in props.plannerHints.slice(0, 4)\"",
            "v-for=\"action in props.actions\"",
        ]:
            assert token in capability_efficiency_panel_src

    def test_capability_css_present(
        self,
        src: str,
        capability_trace_list_src: str,
        capability_alignment_card_src: str,
        capability_efficiency_panel_src: str,
        capability_shared_css_src: str,
    ) -> None:
        for cls in [
            ".capability-scroll",
            ".capability-hero",
            ".capability-hero-actions",
        ]:
            assert cls in src
        for cls in [
            ".capability-chain",
            ".capability-export-btn",
            ".capability-section-head",
            ".capability-health-strip",
            ".capability-health-status",
            ".capability-fixture-replay-card",
            ".capability-fixture-replay-grid",
            ".capability-fixture-replay-artifact",
            ".capability-fixture-replay-checks",
            ".capability-fixture-replay-check-list",
            ".capability-fixture-library-card",
            ".capability-efficiency-feedback-replay-card",
            ".capability-efficiency-feedback-library-card",
            ".capability-fixture-history-card",
            ".capability-fixture-history-trend",
            ".capability-fixture-history-trend.is-improved",
            ".capability-fixture-history-trend.is-regressed",
            ".capability-fixture-library-actions",
            ".capability-fixture-library-list",
            ".capability-efficiency-feedback-library-list",
            ".capability-fixture-history-list",
            ".capability-fixture-library-item",
            ".capability-efficiency-feedback-library-item",
            ".capability-fixture-history-item",
            ".capability-fixture-library-empty",
            ".capability-fixture-triage",
            ".capability-fixture-triage-focus",
            ".capability-fixture-triage-grid",
            ".capability-fixture-batch-head-actions",
            ".capability-fixture-batch-list",
            ".capability-fixture-batch-item",
            ".capability-exec-summary",
            ".capability-attempt",
            ".capability-check",
            ".capability-fallback-pill",
            ".capability-role-card",
            ".capability-json",
        ]:
            assert cls in capability_shared_css_src
        for cls in [
            ".capability-trace-filters",
            ".capability-trace-filter",
            ".capability-trace-search",
            ".capability-trace-search-count",
            ".capability-trace-list",
            ".capability-trace-row",
            ".capability-trace-detail",
            ".capability-trace-empty",
        ]:
            assert cls in capability_trace_list_src
        for cls in [
            ".capability-alignment-card",
            ".capability-alignment-status",
        ]:
            assert cls in capability_alignment_card_src
        for cls in [
            ".capability-crawl-efficiency-card",
            ".capability-efficiency-correlation-card",
            ".capability-efficiency-correlation-head",
            ".capability-efficiency-hints",
            ".capability-crawl-efficiency-head",
            ".capability-crawl-efficiency-paths",
            ".capability-crawl-efficiency-candidates",
            ".capability-crawl-efficiency-candidate",
        ]:
            assert cls in capability_efficiency_panel_src

    def test_capability_trace_export_wired(
        self, src: str, failure_fixture_summary_src: str, capability_replay_pane_src: str,
    ) -> None:
        app_tokens = [
            "const exportCapabilityTraceAsJsonl = () =>",
            "capabilityTraceEvents.value",
            "capability_trace_${stamp}.jsonl",
            "当前没有可导出的 capability trace 事件",
            "已导出 ${lines.length} 条 capability trace 事件",
            "capability_route / capability_execute",
            "# Capability Trace Summary",
            "Route preflight: ${routePreflight.status || 'unknown'}",
            "Route preflight action: ${routePreflight.recommended_action}",
            "Route preflight warnings: ${routePreflightWarnings.slice(0, 5).join(', ')}",
            "Runtime issues: ${runtimeIssue.status || 'unknown'}",
            "Runtime drift: ${runtimeDrift.status || 'unknown'}",
            "Runtime action: ${runtimeIssue.recommended_action}",
            "Runtime issue codes: ${issueCodes.join(', ')}",
            "Runtime actions: ${runtimeActions.join(', ')}",
            "Browser action issues: ${actionIssue.status || 'unknown'}",
            "Browser action failure: ${actionFailure.failure_code}",
            "Browser action failure category: ${actionFailure.failure_category}",
            "Browser action recommendation: ${actionIssue.recommended_action}",
            "Browser action issue codes: ${actionIssueCodes.join(', ')}",
            "Browser action recommendations: ${actionActions.join(', ')}",
            "Browser action recovery actions: ${actionRecoveryActions.join(', ')}",
            "Crawl efficiency path: ${crawlEfficiency.recommended_path || 'unknown'}",
            "Crawl efficiency skip browser: ${crawlEfficiency.skip_browser ? 'yes' : 'no'}",
            "Crawl efficiency skip VLM: ${crawlEfficiency.skip_vlm ? 'yes' : 'no'}",
            "Crawl efficiency available paths: ${crawlAvailablePaths.join(', ')}",
            "Crawl efficiency candidates: ${topCandidates.join(' | ')}",
            "Efficiency correlation: ${efficiencyCorrelation.status || 'unknown'}",
            "Efficiency recommended path: ${efficiencyAlignment.recommended_path || 'unknown'}",
            "Efficiency executed path: ${efficiencyAlignment.executed_path || 'unknown'}",
            "Efficiency root causes: ${efficiencyRootCauses.join(', ')}",
            "Efficiency actions: ${efficiencyActions.join(', ')}",
            "已复制 capability trace 摘要",
            "/api/capabilities/failure_fixture",
            "当前没有可生成 fixture 的 capability failure bundle",
            "capability_execute_failure_bundle.v1",
            "tags: ['frontend', 'capability_trace']",
            "save: true",
            "await fetchArtifacts()",
            "已生成 failure fixture",
            "/api/capabilities/failure_fixture/replay",
            "当前没有可验证 replay 的 capability failure bundle",
            "tags: ['frontend', 'capability_trace', 'replay']",
            "capabilityFailureFixtureReplayReport.value = replayResult.result?.report || null",
            "capabilityFailureFixtureReplayArtifact.value = replayResult.result?.artifact || null",
            "Failure fixture replay 验证通过",
            "Failure fixture replay 验证未通过",
            "/api/capabilities/failure_fixtures?limit=100",
            "capabilityFailureFixtureLibrary.value = Array.isArray(result.result?.fixtures)",
            "已加载 ${capabilityFailureFixtureLibrary.value.length} 个 failure fixtures",
            "/api/capabilities/failure_fixture/replay_batches?limit=20",
            "capabilityFailureFixtureBatchHistory.value = Array.isArray(result.result?.reports)",
            "capabilityFailureFixtureBatchHistoryTrend.value = result.result?.trend || null",
            "已加载 ${capabilityFailureFixtureBatchHistory.value.length} 条 batch replay history",
            "读取 batch replay history 失败",
            "/api/capabilities/failure_fixture/replay_batch",
            "name: 'frontend_capability_failure_fixture_batch_replay'",
            "capabilityFailureFixtureBatchReplayReport.value = result.result?.report || null",
            "capabilityFailureFixtureBatchReplayArtifact.value = result.result?.artifact || null",
            "await fetchCapabilityFailureFixtureBatchHistory(true)",
            "当前没有可批量验证的 failure fixtures",
            "Failure fixture batch replay 全部通过",
            "Failure fixture batch replay 未通过",
            "/api/capabilities/efficiency_feedback/replay",
            "当前没有可验证 replay 的 efficiency correlation report",
            "name: 'frontend_efficiency_feedback_replay'",
            "capabilityEfficiencyFeedbackReplayReport.value = result.result?.report || null",
            "capabilityEfficiencyFeedbackReplayArtifact.value = result.result?.artifact || null",
            "await fetchCapabilityEfficiencyFeedbackReplays(true)",
            "Efficiency feedback replay 验证通过",
            "Efficiency feedback replay 验证未通过",
            "/api/capabilities/efficiency_feedback/replays?limit=20",
            "capabilityEfficiencyFeedbackReplayLibrary.value = Array.isArray(result.result?.reports)",
            "已加载 ${capabilityEfficiencyFeedbackReplayLibrary.value.length} 条 efficiency feedback replay reports",
            "读取 efficiency feedback replay library 失败",
            "当前没有可复制的 batch replay 摘要",
            "已复制 batch replay 摘要",
            "failure_bundle: result.failure_bundle",
        ]
        for token in app_tokens:
            assert token in src
        for token in [
            "Efficiency Feedback Replay",
            "Efficiency Feedback Replay Library",
            "source capability/efficiency_feedback_replays",
        ]:
            assert token in capability_replay_pane_src
        for token in [
            "# Failure Fixture Batch Replay Summary",
            "Top primary failures: ${topPrimary}",
            "Top failure categories: ${topCategories}",
            "Top failed checks: ${topChecks}",
        ]:
            assert token in failure_fixture_summary_src
