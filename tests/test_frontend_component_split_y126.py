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


CAPABILITY_OVERVIEW_PANE = ROOT / "vspider-ui" / "src" / "components" / "CapabilityOverviewPane.vue"
CAPABILITY_HERO_SECTION = ROOT / "vspider-ui" / "src" / "components" / "CapabilityHeroSection.vue"
CAPABILITY_EXEC_TELEMETRY = ROOT / "vspider-ui" / "src" / "components" / "CapabilityExecutionTelemetry.vue"
USE_CAPABILITY_TRACE = ROOT / "vspider-ui" / "src" / "composables" / "useCapabilityTrace.js"
USE_CAPABILITY_FIXTURE_REPLAY = ROOT / "vspider-ui" / "src" / "composables" / "useCapabilityFixtureReplay.js"


@pytest.fixture(scope="module")
def app_src() -> str:
    parts = [APP_VUE.read_text(encoding="utf-8")]
    for extra in (CAPABILITY_OVERVIEW_PANE, CAPABILITY_HERO_SECTION, CAPABILITY_EXEC_TELEMETRY):
        if extra.exists():
            parts.append(extra.read_text(encoding="utf-8"))
    return "\n".join(parts)


@pytest.fixture(scope="module")
def trace_utils_src() -> str:
    return CAPABILITY_TRACE_UTILS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def capability_trace_src() -> str:
    return USE_CAPABILITY_TRACE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def capability_replay_src() -> str:
    return USE_CAPABILITY_FIXTURE_REPLAY.read_text(encoding="utf-8")


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


def test_capability_trace_helpers_are_extracted(
    app_src: str, trace_utils_src: str, capability_trace_src: str
) -> None:
    # D-UI-12/14 moved the capability-event helper destructure out of App.vue and
    # into the useCapabilityTrace composable. The helpers stay the single source
    # of truth in capabilityTraceUtils.js (exported there + imported by the
    # composable) and must not be re-inlined back into App.vue.
    assert "from '../components/capabilityTraceUtils'" in capability_trace_src
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
        "capabilityAttemptClass",
        "capabilityItemMeta",
        "capabilityItemDetail",
    ]:
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
    assert 'capabilityTraceFilter' in app_src
    assert 'capabilityTraceSearchQuery' in app_src
    assert 'capabilityFilteredTraceRows' in app_src
    assert '@open-row="(evt) => timelinePanelRef.value?.openPhaseDialog(evt)"' in app_src
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
    assert "CapabilityAlignmentCard" in app_src
    assert "<CapabilityAlignmentCard" in app_src
    assert ":visible=" in app_src
    assert ":alignment=" in app_src
    assert "Route / Execute 对齐" in alignment_card_src
    assert "props.alignment.topChoice" in alignment_card_src
    assert "plan rank #{{ props.alignment.rank }}" in alignment_card_src
    assert ".capability-alignment-card" in alignment_card_src


def test_capability_efficiency_panel_component_is_used(app_src: str, efficiency_panel_src: str) -> None:
    assert "CapabilityEfficiencyPanel" in app_src
    assert "<CapabilityEfficiencyPanel" in app_src
    assert ':crawl-plan=' in app_src
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


def test_app_keeps_capability_state_and_api_flow(
    app_src: str, capability_trace_src: str, capability_replay_src: str
) -> None:
    # Capability-trace state moved into the useCapabilityTrace composable and the
    # efficiency-feedback fetch/replay API flow into useCapabilityFixtureReplay
    # (D-UI-12+). App.vue wires the trace composable; the implementations live in
    # the composables.
    assert "useCapabilityTrace(phaseEvents)" in app_src
    assert "const capabilityTraceEvents = computed(" in capability_trace_src
    assert "phaseEvents.value.filter((evt)" in capability_trace_src
    assert "const capabilityTraceRows = computed(" in capability_trace_src
    assert (
        "const fetchCapabilityEfficiencyFeedbackReplays = async (silent = false) =>"
        in capability_replay_src
    )
    assert "const replayCapabilityEfficiencyFeedback = async () =>" in capability_replay_src
