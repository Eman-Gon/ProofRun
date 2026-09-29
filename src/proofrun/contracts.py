"""Shared proofrun.v1 Python boundary. Coordinate changes through Person 1.

Evidence dictionaries are JSON data, never executable instructions. Artifact maps
use a safe public ID as key and an absolute, worker-local file path as value.
Only service.py turns validated artifacts into authenticated HTTP references.
"""
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

SCHEMA_VERSION = "proofrun.v1"
ExecutionStatus = Literal["queued", "running", "completed", "setup_failed", "timed_out", "interrupted"]
FindingStatus = Literal["not_tested", "regression_reproduced", "no_difference_observed", "inconclusive"]
RepairStatus = Literal["not_requested", "pending", "proposed", "verified", "rejected", "unavailable"]


@dataclass(frozen=True)
class SourceBundle:
    repository: str
    revision: str
    module: str
    sha256: str
    content: str
    bundle_sha256: str | None = None


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    contract_id: str
    contract_sha256: str
    root: Path
    artifact_dir: Path
    model_name: str = "Customer"
    field_name: str = "nickname"
    allowed_path: str = "demo/upgrade/app.py"
    baseline_version: str = "1.10.18"
    updated_version: str = "2.8.2"
    expected_case_ids: tuple[str, ...] = (
        "nickname_omitted", "nickname_null", "nickname_string",
        "nickname_object_rejected", "required_name_rejected",
    )
    original_test_ids: tuple[str, ...] = (
        "test_existing.TestExisting.test_explicit_nickname",
        "test_existing.TestExisting.test_explicit_none",
    )


# The repairer receives measured evidence and approved source/requirements only.
FailureContext = dict[str, Any]


@dataclass(frozen=True)
class PatchProposal:
    base_sha256: str
    allowed_path: str
    replacement: str
    rationale: str
    provenance: dict[str, Any]


class ProposalUnavailable(RuntimeError):
    """Provider/configuration/refusal failure; message must be safe for users."""


@dataclass
class ComparisonEvidence:
    execution_status: ExecutionStatus
    finding_status: FindingStatus
    bindings: dict[str, str]
    environments: dict[str, Any] = field(default_factory=dict)
    cases: list[dict[str, Any]] = field(default_factory=list)
    expected_case_ids: list[str] = field(default_factory=list)
    executed_case_ids: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    failure_context: FailureContext = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VerificationEvidence:
    execution_status: ExecutionStatus
    repair_status: RepairStatus
    bindings: dict[str, str]
    complete: bool = False
    environments: dict[str, Any] = field(default_factory=dict)
    cases: list[dict[str, Any]] = field(default_factory=list)
    expected_case_ids: list[str] = field(default_factory=list)
    executed_case_ids: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
