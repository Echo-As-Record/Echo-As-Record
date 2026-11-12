"""Deterministic, synthetic module demo. No network, credentials or real personal data."""

from __future__ import annotations

import json
from dataclasses import replace

from ear_contracts import DEFAULT_REGISTRY, DataTypeSpec, Registry, new_id
from ear_storage_client import AppendRaw, Context, Create, MemoryStorage, Query, iter_query
from ear_storage_client.testing import make_raw, ts

from ear_extraction.domain import Call, Command, Interval, Rule
from ear_extraction.orchestration import ExtractionService


class DemoGateway:
    """Fixed responses solely for demonstration, not an inference implementation."""

    def complete(self, call: Call) -> dict:
        if call.purpose == "merge_activities":
            return {"merge": True, "title_md": "合成组会"}
        if call.purpose == "consolidation":
            parts = call.data["partial_results"]
            return {
                **parts[0],
                "evidence_ids": list(dict.fromkeys(id for p in parts for id in p["evidence_ids"])),
            }
        evidence = call.data["evidence"]
        if call.purpose == "segmentation":
            return {
                "activities": [
                    {
                        **call.data["interval"],
                        "title_md": "合成组会",
                        "evidence_ids": [evidence[0]["id"]],
                    }
                ]
            }
        if call.purpose == "selection":
            return {"rule_ids": [r["id"] for r in call.data["rules"]]}
        person = next(
            r for r in call.data["attributions"] if r["payload"]["target_type"] == "person"
        )
        pid = person["payload"]["target_id"]
        result = {
            "body_md": "- "
            + next(
                e["payload"].get("name") or "未命名人物"
                for e in call.data["entities"]
                if e["id"] == pid
            )
            + " 整理实验结果。",
            "evidence_ids": [evidence[0]["id"]],
        }
        rule = call.data["rule"]["payload"]
        if rule.get("action") == "reminder":
            result["action"] = {
                "kind": "reminder",
                "title": "整理实验结果",
                "due_at": "2026-11-06T09:00:00Z",
            }
        return result


def fixture_registry() -> Registry:
    """Synthetic opt-in only; production registration belongs to the coordinator."""
    types = dict(DEFAULT_REGISTRY.data_types)
    types["artifact"] = replace(
        types["artifact"], supports_reevaluation=True, requires_dependencies=True
    )
    return Registry(
        types, DEFAULT_REGISTRY.event_publishers, DEFAULT_REGISTRY.core_types
    ).extend_core(
        DataTypeSpec(
            "rule_selection",
            "semantic",
            "extraction",
            unique_fields=("activity_id",),
            requires_dependencies=True,
            supports_reevaluation=True,
        )
    )


def fixture() -> tuple[MemoryStorage, dict[str, str]]:
    storage = MemoryStorage(fixture_registry(), duplicate_delivery=True)
    person, voice, transcript, attribution = [new_id() for _ in range(4)]
    adapter = storage.client("adapter")
    adapter.write(
        [
            AppendRaw(
                make_raw(
                    "transcript_segment",
                    {
                        "text": "我在11月6日UTC九点前整理实验结果。",
                        "voiceprint_id": voice,
                    },
                    id=transcript,
                    start="2026-11-05T09:00:00Z",
                    end="2026-11-05T09:01:00Z",
                )
            ),
            AppendRaw(
                make_raw(
                    "voiceprint",
                    {"local_speaker": "A"},
                    id=voice,
                    start="2026-11-05T09:00:00Z",
                    end="2026-11-05T09:01:00Z",
                )
            ),
        ],
        Context.system(),
    ).check()
    storage.client("attribution").write(
        [
            Create("person", person, {"name": "合成人物甲", "references": [voice]}),
            Create(
                "attribution",
                attribution,
                {
                    "subject_id": voice,
                    "target_type": "person",
                    "target_id": person,
                },
            ),
        ],
        Context.system(),
    ).check()
    return storage, {
        "person": person,
        "voice": voice,
        "transcript": transcript,
        "attribution": attribution,
    }


def submit(storage: MemoryStorage, command: Command) -> str:
    feedback = make_raw(
        "feedback",
        {
            "module": "extraction",
            "command": command.model_dump(mode="json"),
        },
        start="2026-11-05T10:00:00Z",
        channel="api",
    )
    storage.client("api").write([AppendRaw(feedback)], Context.feedback(feedback.id)).check()
    storage.run_until_idle()
    runs = list(iter_query(storage.client("extraction"), Query("extraction_run")))
    return next(r.id for r in runs if r.payload["root_feedback_id"] == feedback.id)


def main() -> None:
    storage, _ = fixture()
    service = ExtractionService(storage.client("extraction"), DemoGateway())
    service.subscribe()
    submit(
        storage,
        Command(
            kind="rule_create",
            rule=Rule(scope="组会", instruction="提取明确的待办及截止日期", action="reminder"),
        ),
    )
    service.drain()
    submit(
        storage,
        Command(
            kind="extract",
            interval=Interval(start=ts("2026-11-05T09:00:00Z"), end=ts("2026-11-05T10:00:00Z")),
        ),
    )
    service.drain()
    result = {
        kind: [
            r.model_dump(mode="json") for r in iter_query(storage.client("extraction"), Query(kind))
        ]
        for kind in ("activity", "artifact", "action_suggestion")
    }
    result["runs"] = [
        {"id": r.id, "status": r.payload["status"], "error": r.payload["error"]}
        for r in service.runs()
    ]
    if any(r.payload["status"] != "succeeded" for r in service.runs()):
        raise RuntimeError("demo_failed")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
