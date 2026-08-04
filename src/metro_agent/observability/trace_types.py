from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from time import perf_counter_ns
from types import MappingProxyType
from typing import Any, Mapping


def _utc_iso_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _prepare_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        prepared: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("TraceEvent attributes must be JSON-compatible")
            prepared[key] = _prepare_json_value(child)
        return prepared
    if isinstance(value, (list, tuple)):
        return [_prepare_json_value(child) for child in value]
    return value


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json_value(child) for key, child in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json_value(child) for child in value)
    return value


def _json_safe_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_safe_copy(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_json_safe_copy(child) for child in value]
    return value


def _json_compatible_attributes(
    attributes: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    if attributes is None:
        return MappingProxyType({})
    if not isinstance(attributes, Mapping):
        raise TypeError("TraceEvent attributes must be JSON-compatible mappings")

    try:
        encoded = json.dumps(
            _prepare_json_value(attributes), allow_nan=False, separators=(",", ":")
        )
    except (TypeError, ValueError) as error:
        raise TypeError("TraceEvent attributes must be JSON-compatible") from error

    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise TypeError("TraceEvent attributes must be JSON-compatible mappings")
    return _freeze_json_value(decoded)


@dataclass(frozen=True, slots=True)
class TraceEvent:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    event_type: str
    status: str
    started_at: str
    finished_at: str | None
    duration_ms: float | None
    agent: str | None
    step_id: str | None
    attempt: int | None
    attributes: Mapping[str, Any]
    artifact_uri: str | None
    _started_perf_ns: int

    @classmethod
    def start(
        cls,
        trace_id: str,
        span_id: str,
        event_type: str,
        agent: str | None = None,
        parent_span_id: str | None = None,
        step_id: str | None = None,
        attempt: int | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> TraceEvent:
        for name, value in (
            ("trace_id", trace_id),
            ("span_id", span_id),
            ("event_type", event_type),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"TraceEvent {name} must be a non-empty string")
        if attempt is not None and attempt < 0:
            raise ValueError("TraceEvent attempt cannot be negative")

        return cls(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            event_type=event_type,
            status="running",
            started_at=_utc_iso_timestamp(),
            finished_at=None,
            duration_ms=None,
            agent=agent,
            step_id=step_id,
            attempt=attempt,
            attributes=_json_compatible_attributes(attributes),
            artifact_uri=None,
            _started_perf_ns=perf_counter_ns(),
        )

    def finish(
        self,
        status: str,
        attributes: Mapping[str, Any] | None = None,
        artifact_uri: str | None = None,
    ) -> TraceEvent:
        if not isinstance(status, str) or not status:
            raise ValueError("TraceEvent status must be a non-empty string")

        completed_perf_ns = perf_counter_ns()
        merged_attributes = _json_safe_copy(self.attributes)
        merged_attributes.update(_json_safe_copy(_json_compatible_attributes(attributes)))
        return replace(
            self,
            status=status,
            finished_at=_utc_iso_timestamp(),
            duration_ms=(completed_perf_ns - self._started_perf_ns) / 1_000_000,
            attributes=_json_compatible_attributes(merged_attributes),
            artifact_uri=artifact_uri if artifact_uri is not None else self.artifact_uri,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "event_type": self.event_type,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "agent": self.agent,
            "step_id": self.step_id,
            "attempt": self.attempt,
            "attributes": _json_safe_copy(self.attributes),
            "artifact_uri": self.artifact_uri,
        }
