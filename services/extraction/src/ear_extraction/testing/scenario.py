"""Repeatable mock integration acceptance run; prints a machine-readable report."""

from __future__ import annotations

import json
from datetime import timedelta

from ear_contracts import SemanticRecord, new_id
from ear_storage_client import AppendRaw, Context, Create, Query, iter_query
from ear_storage_client.testing import make_raw, ts

from ear_extraction.demo import fixture
from ear_extraction.domain import Command, Interval, Rule
from ear_extraction.orchestration import ExtractionService
from ear_extraction.persistence import records
from ear_extraction.testing.gateway import mock_http_gateway
from ear_extraction.testing.peers import MockPeers, feedback


def run_scenario(*, fault: str | None = None) -> dict:
    storage, ids = fixture()
    gps, place, gps_attr, second_person = [new_id() for _ in range(4)]
    storage.client("adapter").write(
        [
            AppendRaw(
                make_raw(
                    "gps_point",
                    {"latitude": 0, "longitude": 0},
                    id=gps,
                    start="2026-11-05T09:00:00Z",
                )
            ),
            AppendRaw(
                make_raw("phone_info", {"text": "合成组会开始"}, start="2026-11-05T09:00:00Z")
            ),
        ],
        Context.system(),
    ).check()
    storage.client("attribution").write(
        [
            Create("place", place, {"name": "合成会议室", "references": [gps]}),
            Create(
                "attribution",
                gps_attr,
                {"subject_id": gps, "target_type": "place", "target_id": place},
            ),
            Create("person", second_person, {"name": "合成人物乙", "references": []}),
        ],
        Context.system(),
    ).check()
    peers = MockPeers(storage)
    now = ts("2026-11-05T10:00:00Z")
    checks: dict[str, bool] = {}

    def check(name: str, passed: bool) -> None:
        checks[name] = passed
        if not passed:
            states = [
                (r.payload["command"].get("kind"), r.payload["status"], r.payload["error"])
                for r in service.runs()
            ]
            raise RuntimeError(f"mock_integration_failed:{name}:{states}")

    with mock_http_gateway(fault) as (gateway, probe):
        service = ExtractionService(storage.client("extraction"), gateway, clock=lambda: now)
        service.subscribe()

        def settle() -> None:
            for _ in range(20):
                storage.run_until_idle(max_deliveries=2000)
                service.drain()
                peers.poll_completions()
                if storage.bus.pending() == 0:
                    break
            else:
                raise RuntimeError("mock_event_loop_did_not_settle")
            check("no_dead_letters", not storage.dead_letters)

        def send(command: Command) -> str:
            root = feedback(storage, "extraction", command.model_dump(mode="json"))
            settle()
            return root

        for instruction, action in [("提取明确待办", "reminder"), ("仅记实验议题", None)]:
            send(
                Command(
                    kind="rule_create",
                    rule=Rule(scope="组会", instruction=instruction, action=action),
                )
            )
        interval = Interval(start=ts("2026-11-05T09:00:00Z"), end=now)
        root = send(Command(kind="extract", interval=interval))
        extraction = next(r for r in service.runs() if r.payload["root_feedback_id"] == root)
        if fault:
            expected = "retry_wait" if fault == "http_503" else "failed"
            check("fault_is_visible", extraction.payload["status"] == expected)
            check("failure_is_atomic", not records(service.storage, "activity"))
            if expected == "failed":
                service.retry(extraction.id)
            now += timedelta(seconds=31)
            settle()
        check(
            "extraction_succeeded", all(r.payload["status"] == "succeeded" for r in service.runs())
        )
        activities = records(service.storage, "activity")
        artifacts = records(service.storage, "artifact")
        check("one_activity_two_artifacts", len(activities) == 1 and len(artifacts) == 2)
        activity_id = activities[0].id
        fixed = next(a for a in artifacts if a.payload["body_md"] == "实验议题：结果整理。")
        changed = next(a for a in artifacts if a.id != fixed.id)
        old_suggestion = records(service.storage, "action_suggestion")[0]
        check("nothing_executed_before_approval", not peers.external_actions)
        correction = feedback(
            storage, "attribution", {"target_id": ids["attribution"], "person_id": second_person}
        )
        settle()
        revised = service.storage.get(changed.id)
        assert isinstance(revised, SemanticRecord)
        check(
            "owner_corrected", revised.version == 2 and "合成人物乙" in revised.payload["body_md"]
        )
        check("plain_markdown", "[[person:" not in revised.payload["body_md"])
        check("activity_preserved", records(service.storage, "activity")[0].id == activity_id)
        check("unaffected_artifact_unchanged", service.storage.get(fixed.id) == fixed)
        previous = service.storage.get(old_suggestion.id)
        check(
            "old_pending_suggestion_superseded",
            isinstance(previous, SemanticRecord) and previous.superseded,
        )
        propagation = list(iter_query(service.storage, Query("propagation_record")))
        check(
            "both_completions_polled",
            len(propagation) == 2
            and {r.payload["detail"]["outcome"] for r in propagation} == {"changed", "unchanged"},
        )
        check(
            "root_and_depth_preserved",
            revised.root_feedback_id == correction and revised.depth == 1,
        )
        check(
            "no_stale_attribution_dependency",
            not service.storage.stale_dependencies([ids["attribution"]]),
        )
        suggestion = records(service.storage, "action_suggestion")[0]
        send(
            Command(
                kind="review_suggestion",
                target_id=suggestion.id,
                expected_version=suggestion.version,
                decision="approved",
            )
        )
        executed = service.storage.get(suggestion.id)
        check(
            "execution_receipt_applied",
            executed is not None and executed.payload["state"] == "executed",
        )
        check("duplicate_delivery_one_external_action", len(peers.external_actions) == 1)
        check("schedule_skips_success", service.schedule(interval) == [])
        rejudge_calls = [c for c in probe.calls if c.purpose == "reevaluation"]
        check(
            "http_request_trace",
            len(rejudge_calls) == 2
            and all(
                c.request_id == c.run_id and c.root_feedback_id == correction for c in rejudge_calls
            ),
        )
        check(
            "segmentation_has_no_entities",
            all(
                "entities" not in c.data and "attributions" not in c.data
                for c in probe.calls
                if c.purpose == "segmentation"
            ),
        )
        check(
            "metadata_only_by_default",
            all(
                "input" not in c and "output" not in c
                for r in service.runs()
                for c in r.payload["model_calls"]
            ),
        )
        check(
            "completion_markers",
            all(service.storage.is_processed("extraction.completed", r.id) for r in service.runs()),
        )
        return {
            "status": "passed",
            "baseline": "v1.2 partial migration; current shared contracts",
            "storage": "MemoryStorage",
            "gateway": "loopback HTTP mock",
            "mock_modules": ["adapter", "attribution", "dependency", "api", "llm_gateway"],
            "fault": fault,
            "checks": checks,
            "model_requests": len(probe.calls),
            "external_actions": len(peers.external_actions),
            "runs": [
                {"id": r.id, "status": r.payload["status"], "detail": r.payload.get("detail", {})}
                for r in service.runs()
            ],
        }


def main() -> None:
    print(json.dumps(run_scenario(), ensure_ascii=False, indent=2))
