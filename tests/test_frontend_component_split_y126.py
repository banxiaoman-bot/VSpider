from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
APP_VUE = ROOT / "vspider-ui" / "src" / "App.vue"
CAPABILITY_TRACE_UTILS = ROOT / "vspider-ui" / "src" / "components" / "capabilityTraceUtils.js"
CAPABILITY_STATUS_BADGE = ROOT / "vspider-ui" / "src" / "components" / "CapabilityStatusBadge.vue"
CAPABILITY_TRACE_LIST = ROOT / "vspider-ui" / "src" / "components" / "CapabilityTraceList.vue"
CAPABILITY_RUNTIME_PANEL = ROOT / "vspider-ui" / "src" / "components" / "CapabilityRuntimePanel.vue"
CAPABILITY_ALIGNMENT_CARD = ROOT / "vspider-ui" / "src" / "components" / "CapabilityAlignmentCard.vue"
CAPABILITY_EFFICIENCY_PANEL = ROOT / "vspider-ui" / "src" / "components" / "CapabilityEfficiencyPanel.vue"
RUN_REGISTRY_PANEL = ROOT / "vspider-ui" / "src" / "components" / "RunRegistryPanel.vue"


@pytest.fixture(scope="module")
def app_src() -> str:
    return APP_VUE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def trace_utils_src() -> str:
    return CAPABILITY_TRACE_UTILS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def status_badge_src() -> str:
    return CAPABILITY_STATUS_BADGE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def trace_list_src() -> str:
    return CAPABILITY_TRACE_LIST.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def runtime_panel_src() -> str:
    return CAPABILITY_RUNTIME_PANEL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def alignment_card_src() -> str:
    return CAPABILITY_ALIGNMENT_CARD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def efficiency_panel_src() -> str:
    return CAPABILITY_EFFICIENCY_PANEL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def run_registry_panel_src() -> str:
    return RUN_REGISTRY_PANEL.read_text(encoding="utf-8")


def test_capability_trace_helpers_are_extracted(app_src: str, trace_utils_src: str) -> None:
    assert "from './components/capabilityTraceUtils'" in app_src
    for name in [
        "capabilityEventActionTrace",
        "capabilityEventActionIssueSummary",
        "capabilityEventTraceArtifact",
        "capabilityEventFailureBundle",
        "capabilityEventCrawlEfficiencyPlan",
        "capabilityEventEfficiencyCorrelationReport",
        "capabilityCrawlEfficiencyCandidateClass",
        "capabilityCrawlEfficiencyEvidence",
        "capabilityItemName",
        "capabilityItemMeta",
        "capabilityItemDetail",
        "capabilityAttemptClass",
    ]:
        assert f"  {name}," in app_src
        assert f"export const {name}" in trace_utils_src
        assert f"const {name}" not in app_src


def test_capability_status_badge_component_is_used(app_src: str, status_badge_src: str) -> None:
    assert "import CapabilityStatusBadge from './components/CapabilityStatusBadge.vue'" in app_src
    assert "<CapabilityStatusBadge" in app_src
    assert "class=\"capability-health-status\"" in status_badge_src
    assert "const normalizedClass = computed(" in status_badge_src
    assert "value.startsWith('is-') ? value : `is-${value}`" in status_badge_src


def test_capability_trace_list_component_is_used(app_src: str, trace_list_src: str) -> None:
    assert "import CapabilityTraceList from './components/CapabilityTraceList.vue'" in app_src
    assert "<CapabilityTraceList" in app_src
    assert 'v-model:filter="capabilityTraceFilter"' in app_src
    assert 'v-model:search-query="capabilityTraceSearchQuery"' in app_src
    assert ':rows="capabilityFilteredTraceRows"' in app_src
    assert '@open-row="openPhaseDialog"' in app_src
    assert "defineEmits(['update:filter', 'update:searchQuery', 'open-row'])" in trace_list_src
    assert 'class="capability-trace-row"' in trace_list_src
    assert 'class="capability-trace-filter"' in trace_list_src
    assert "@update:model-value=\"setSearchQuery\"" in trace_list_src


def test_capability_runtime_panel_component_is_used(app_src: str, runtime_panel_src: str) -> None:
    assert "import CapabilityRuntimePanel from './components/CapabilityRuntimePanel.vue'" in app_src
    assert "<CapabilityRuntimePanel" in app_src
    assert ':runtime-preflight="capabilityRuntimePreflight"' in app_src
    assert ':browser-runtime="browserRuntime"' in app_src
    assert ':backend-summary="browserRuntimeBackendSummary"' in app_src
    assert ':capacity="browserRuntimeCapacity"' in app_src
    assert "Runtime Preflight" in runtime_panel_src
    assert "Browser Runtime" in runtime_panel_src
    assert 'class="browser-runtime-card"' in runtime_panel_src
    assert ".browser-runtime-grid" in runtime_panel_src


def test_capability_alignment_card_component_is_used(app_src: str, alignment_card_src: str) -> None:
    assert "import CapabilityAlignmentCard from './components/CapabilityAlignmentCard.vue'" in app_src
    assert "<CapabilityAlignmentCard" in app_src
    assert ':visible="Boolean(latestCapabilityExecute)"' in app_src
    assert ':alignment="capabilityExecutionAlignment"' in app_src
    assert "Route / Execute 对齐" in alignment_card_src
    assert "props.alignment.topChoice" in alignment_card_src
    assert "plan rank #{{ props.alignment.rank }}" in alignment_card_src
    assert ".capability-alignment-card" in alignment_card_src


def test_capability_efficiency_panel_component_is_used(app_src: str, efficiency_panel_src: str) -> None:
    assert "import CapabilityEfficiencyPanel from './components/CapabilityEfficiencyPanel.vue'" in app_src
    assert "<CapabilityEfficiencyPanel" in app_src
    assert ':crawl-plan="capabilityActiveCrawlEfficiencyPlan"' in app_src
    assert ':candidate-evidence="capabilityCrawlEfficiencyEvidence"' in app_src
    assert ':correlation-report="capabilityExecutionEfficiencyCorrelationReport"' in app_src
    assert ':planner-hints="capabilityExecutionEfficiencyCorrelationPlannerHints"' in app_src
    assert "Crawl Efficiency" in efficiency_panel_src
    assert "Efficiency Correlation" in efficiency_panel_src
    assert 'class="capability-crawl-efficiency-candidate"' in efficiency_panel_src
    assert ".capability-efficiency-correlation-card" in efficiency_panel_src


def test_run_registry_panel_component_is_used(app_src: str, run_registry_panel_src: str) -> None:
    assert "import RunRegistryPanel from './components/RunRegistryPanel.vue'" in app_src
    assert "<RunRegistryPanel" in app_src
    assert "name=\"runs\"" in app_src
    assert "runHistoryRefreshToken" in app_src
    assert "apiFetch('/api/runs?limit=50')" in run_registry_panel_src
    assert "apiFetch(`/api/runs/${encodeURIComponent(row.run_id)}`)" in run_registry_panel_src
    assert "contracts.value.input_contract" in run_registry_panel_src
    assert "contracts.value.output_contract" in run_registry_panel_src
    assert "contracts.value.manifest" in run_registry_panel_src


def test_app_keeps_capability_state_and_api_flow(app_src: str) -> None:
    assert "const capabilityTraceEvents = computed(" in app_src
    assert "phaseEvents.value.filter((evt)" in app_src
    assert "const capabilityTraceRows = computed(" in app_src
    assert "const fetchCapabilityEfficiencyFeedbackReplays = async (silent = false) =>" in app_src
    assert "const replayCapabilityEfficiencyFeedback = async () =>" in app_src
