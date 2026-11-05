"""旧 Ear Evidence 只读 API 的响应（只取 ② 用到的字段）。

对应旧 Ear 的 `ear/models/evidence_read.py`。未知字段忽略，旧 Ear 新增字段不影响解析。
时间带时区（旧 Ear 输出本地时区），转换成 UTC 在 convert.py 里做。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

_READ = ConfigDict(extra="ignore", frozen=True)


class Artifact(BaseModel):
    model_config = _READ
    artifact_id: str
    checksum: str
    """形如 sha256:<hex>。"""
    size_bytes: int
    media_type: str

    @property
    def sha256(self) -> str:
        return self.checksum.removeprefix("sha256:")


class Capture(BaseModel):
    model_config = _READ
    capture_id: str
    instance_id: str
    record_id: str
    group_id: str
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    audio_ref: Artifact


class AudioChunk(BaseModel):
    model_config = _READ
    audio_chunk_id: str
    started_at: AwareDatetime
    ended_at: AwareDatetime
    model_version: str


class SpeechSegment(BaseModel):
    model_config = _READ
    speech_segment_id: str
    audio_chunk_id: str
    started_at: AwareDatetime
    ended_at: AwareDatetime
    original_text: str
    """模型原始输出。不用 text：那是旧 Ear 里用户纠正后的值，纠正在新系统里属于用户反馈。"""
    language: str | None = None
    diarization_scope_id: str
    local_speaker_id: str
    overlap: bool
    model_version: str


class AudioEvent(BaseModel):
    model_config = _READ
    audio_event_id: str
    audio_chunk_id: str
    started_at: AwareDatetime
    ended_at: AwareDatetime
    original_label: str
    taxonomy: str
    model_version: str


class Location(BaseModel):
    model_config = _READ
    latitude: float
    longitude: float
    accuracy_m: float | None = None


class SensorObservation(BaseModel):
    model_config = _READ
    sensor_observation_id: str
    kind: str
    observed_at: AwareDatetime
    location: Location | None = None


class ContextSnapshot(BaseModel):
    model_config = _READ
    context_snapshot_id: str
    observed_at: AwareDatetime
    state: dict[str, Any]


class ContextEvent(BaseModel):
    model_config = _READ
    context_event_id: str
    event_type: str
    occurred_at: AwareDatetime
    details: dict[str, Any]
    inferred: bool


class SpeakerResolution(BaseModel):
    model_config = _READ
    resolution_id: str
    diarization_scope_id: str
    local_speaker_id: str
    evidence_segment_ids: list[str]
    embedding_ref: Artifact | None = None
    sample_quality: float | None = None
    model_version: str


class EvidenceRef(BaseModel):
    model_config = _READ
    type: str
    id: str


class EvidenceItem(BaseModel):
    model_config = _READ
    ref: EvidenceRef
    payload: dict[str, Any]


class Availability(BaseModel):
    model_config = _READ
    type: str
    state: Literal["available", "empty", "missing", "failed", "partial"]


class CaptureEvidence(BaseModel):
    """GET /api/captures/{capture_id}/evidence 的响应（view=current）。"""

    model_config = _READ
    capture_id: str
    pipeline_run_id: str | None = None
    run_revision: int | None = None
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    analyzable: bool
    availability: tuple[Availability, ...]
    evidence: tuple[EvidenceItem, ...]

    def of_type[M: BaseModel](self, kind: str, model: type[M]) -> list[M]:
        return [model.model_validate(e.payload) for e in self.evidence if e.ref.type == kind]


class Embedding(BaseModel):
    """GET /api/evidence/speaker_resolution/{id}/embedding 的响应。"""

    model_config = _READ
    model_version: str
    shape: tuple[int, ...]
    dtype: str
    values: list[float]
    provenance: dict[str, Any] | None = None


class CaptureSummary(BaseModel):
    """列出录音的接口里的一项（旧 Ear 待补的接口，见 ai-docs/implementation.md）。"""

    model_config = _READ
    capture_id: str
    updated_at: datetime | None = None
