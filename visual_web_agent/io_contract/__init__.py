"""I/O contract layer for VSpider runs.

Defines three serializable artifacts that together describe a run end-to-end:

- ``input_contract.v1``  - normalized inputs (goal, urls, attachments, ...)
- ``output_contract.v1`` - inferred output shape (kind, container, post_process)
- ``manifest.v1``        - append-only registry of real produced artifacts

See ``docs/io_contract_design.md`` for the full design.

The three modules below are intentionally pure: no FS / network / browser side
effects. Persistence helpers that touch disk live alongside
``run_registry.py`` and ``artifact_manager.py``.
"""

from .input_contract import (
    InputContract,
    UrlSpec,
    AttachmentSpec,
    EntrySuggestion,
    ModelOverrides,
    Constraints,
    build_input_contract,
    infer_attachment_intent,
    infer_urls_from_goal,
    parse_urls_field,
    suggest_entry_url,
)
from .output_contract import (
    OutputPrediction,
    OutputContract,
    OUTPUT_KINDS,
    CONTAINERS,
    infer_output_contract,
    default_container_for_kind,
    normalize_output_fields,
    output_contract_fields,
    normalize_output_contract_dict,
)
from .manifest import (
    Manifest,
    ManifestItem,
    new_manifest,
    append_item,
    merge_source_url,
)
from .clarification import (
    ClarificationRequest,
    detect_input_clarifications,
)
from .preflight import Preflight, build_preflight
from .entry_llm import make_entry_llm, entry_llm_from_config
from .protocol import RunOutputProtocol
from .runtime import (
    clear_current_run,
    current_base_dir,
    current_run_context,
    current_run_id,
    set_current_run,
)
from .persistence import (
    INPUT_CONTRACT_FILENAME,
    OUTPUT_CONTRACT_FILENAME,
    MANIFEST_FILENAME,
    ARTIFACTS_DIRNAME,
    run_dir,
    default_runs_root,
    ensure_contract_skeleton,
    ensure_input_contract_skeleton,
    write_input_contract,
    read_input_contract,
    write_output_contract,
    write_output_prediction,
    read_output_contract,
    read_manifest,
    write_manifest,
    append_manifest_item,
    record_verification_evidence,
    record_template_experience,
)


__all__ = [
    "InputContract",
    "UrlSpec",
    "AttachmentSpec",
    "ModelOverrides",
    "Constraints",
    "EntrySuggestion",
    "build_input_contract",
    "infer_attachment_intent",
    "infer_urls_from_goal",
    "parse_urls_field",
    "suggest_entry_url",
    "OutputPrediction",
    "OutputContract",
    "OUTPUT_KINDS",
    "CONTAINERS",
    "infer_output_contract",
    "default_container_for_kind",
    "normalize_output_fields",
    "output_contract_fields",
    "normalize_output_contract_dict",
    "Manifest",
    "ManifestItem",
    "new_manifest",
    "append_item",
    "merge_source_url",
    "ClarificationRequest",
    "detect_input_clarifications",
    "Preflight",
    "build_preflight",
    "make_entry_llm",
    "entry_llm_from_config",
    "RunOutputProtocol",
    "INPUT_CONTRACT_FILENAME",
    "OUTPUT_CONTRACT_FILENAME",
    "MANIFEST_FILENAME",
    "ARTIFACTS_DIRNAME",
    "run_dir",
    "default_runs_root",
    "ensure_contract_skeleton",
    "ensure_input_contract_skeleton",
    "write_input_contract",
    "read_input_contract",
    "write_output_contract",
    "write_output_prediction",
    "read_output_contract",
    "read_manifest",
    "write_manifest",
    "append_manifest_item",
    "record_verification_evidence",
    "record_template_experience",
    "set_current_run",
    "clear_current_run",
    "current_run_id",
    "current_base_dir",
    "current_run_context",
]
