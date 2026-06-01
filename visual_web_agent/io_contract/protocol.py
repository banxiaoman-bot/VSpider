"""Three-part protocol surface for run intent.

This module ties together the normalized input contract, model output
prediction, and inferred output contract into one structured object that can
be passed around by routers, planners, and persistence code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .input_contract import InputContract
from .output_contract import OutputContract, OutputPrediction, infer_output_contract


@dataclass(frozen=True)
class RunOutputProtocol:
    input_contract: InputContract
    output_prediction: OutputPrediction = field(default_factory=OutputPrediction)
    output_contract: OutputContract | None = None
    verification_summary: dict[str, Any] = field(default_factory=dict)

    def resolved_output_contract(self) -> OutputContract:
        if self.output_contract is not None:
            return self.output_contract
        return infer_output_contract(
            self.input_contract.goal,
            model_predicted_kind=self.output_prediction.kind,
            model_predicted_mode=self.output_prediction.mode,
        )

    def to_dict(self) -> dict[str, Any]:
        output_contract = self.resolved_output_contract()
        return {
            "input_contract": self.input_contract.to_dict(),
            "output_prediction": self.output_prediction.to_dict(),
            "output_contract": output_contract.to_dict(),
            "verification_summary": dict(self.verification_summary or {}),
        }
