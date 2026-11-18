"""Synthetic target behavior used solely to exercise the common runner."""

import asyncio
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from checks import (ROOT, CheckError, EvidenceMissing, check_result, expected_state,
                    history_check, read_json, state_check, validate)
from runner import Execution, case_plan, final_gate


class ScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        (self.path / "evidence").mkdir()
        self.fixture = read_json(ROOT / "shared/fixtures/initial/f0-normal.json")
        self.config = read_json(ROOT / "tests/conformance/config.example.json")
        self.env = patch.dict(os.environ, {v: "synthetic-" + k for k, v in self.config["auth_env"].items()})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.version, self.stored = 0, {}
        self.loan_id = "loan-001"
        self.allocation_id, self.margin_id = "allocation-001", "margin-001"
        self.execution = Execution(self.config, self.fixture, self.path, "run-synthetic", "AC-01", ["X", "Y"])
        await self.execution.client.aclose()
        self.execution.client = httpx.AsyncClient(transport=httpx.MockTransport(self.target))
        self.addAsyncCleanup(self.execution.client.aclose)

    def snapshot(self, platform):
        # Expected contract files are the independent oracle for the synthetic target.
        sample = read_json(ROOT / f"shared/fixtures/expected/s{self.version}.json")
        text = json.dumps(sample).replace("loan-001", self.loan_id).replace("allocation-001", self.allocation_id)
        sample = json.loads(text)
        bundle = {k: v for k, v in self.fixture["bundles"][0].items() if k != "initial_holder"}
        body = {"run_id": "run-synthetic", "platform": platform, "snapshot_seq": 1,
                "bundles": [bundle | {"version": self.version}], "pending_operations": []}
        for key in ("rights", "contracts", "obligations", "margin_calls", "allocations"):
            body[key] = sample[key]
        body["margin_calls"] = copy.deepcopy(self.fixture["margin_calls"])
        if self.version == 3:
            for call in body["margin_calls"]:
                if call["margin_call_id"] == self.margin_id:
                    call["status"] = "SATISFIED"
            body["allocations"][0].update(bundle_id="bundle-001", provider="BANK_C", ccp="CCP",
                                           margin_call_id=self.margin_id)
        if self.version == 1 and not self.fixture["agreements"][0]["reuse_allowed"]:
            body["rights"][-1]["allowed_commands"] = []
        if self.version == 2 and not self.fixture["agreements"][1]["ccp_use_allowed"]:
            body["rights"][-1]["allowed_commands"] = []
        return body

    async def target(self, request):
        platform = {"8101": "X", "8102": "Y", "8103": "Z"}[str(request.url.port)]
        roles = {"Bearer synthetic-bank-a": "BANK_A", "Bearer synthetic-bank-b": "BANK_B",
                 "Bearer synthetic-bank-c": "BANK_C", "Bearer synthetic-experiment-observer": "OBSERVER"}
        actor = roles.get(request.headers.get("Authorization"))

        def error(status, code, rid=None):
            return httpx.Response(status, json={"run_id": "run-synthetic", "request_id": rid,
                                               "error": {"code": code, "message": code}})

        if not actor:
            rid = json.loads(request.content).get("request_id") if request.content else None
            return error(401, "UNAUTHENTICATED", rid)
        if request.url.path.endswith("snapshot"):
            if actor != "OBSERVER":
                return error(403, "FORBIDDEN")
            return httpx.Response(200, json=self.snapshot(platform))
        if request.url.path.endswith("readiness"):
            command = self.execution.commands[platform]
            ready = self.version == command["expected_version"]
            return httpx.Response(200, json={"run_id": "run-synthetic", "platform": platform,
                "bundle_id": "bundle-001", "command_type": command["command_type"],
                "observed_version": self.version, "ready": ready,
                "blocking_reasons": [] if ready else ["PRECONDITION_NOT_MET"]})
        if request.method == "GET":
            original = self.stored[request.url.path.split("/")[-1]]
            if actor != original["actor"]:
                return error(403, "FORBIDDEN", original["request_id"])
            return httpx.Response(200, json=original)
        command = json.loads(request.content)
        try:
            validate(command, "command.schema.json")
        except CheckError:
            return error(400, "INVALID_REQUEST", command.get("request_id"))
        if command["command_type"] != {"X": "CONFIRM_REPO", "Y": "CONFIRM_LOAN", "Z": "ALLOCATE_MARGIN"}[platform]:
            return error(400, "INVALID_REQUEST", command["request_id"])
        if actor != {"X": "BANK_A", "Y": "BANK_B", "Z": "BANK_C"}[platform]:
            return error(403, "FORBIDDEN", command["request_id"])
        await asyncio.sleep(.002)
        rid = command["request_id"]
        if rid in self.stored:
            if command != self.originals[rid]:
                return error(409, "REQUEST_ID_CONFLICT", rid)
            return httpx.Response(200, json=self.stored[rid] | {"replayed": True})
        error = None
        if command["expected_version"] != self.version:
            error = "VERSION_CONFLICT"
        elif platform == "Y" and not self.fixture["agreements"][0]["reuse_allowed"]:
            error = "USE_NOT_PERMITTED"
        elif platform == "Y" and self.version == 0 or platform == "Z" and self.version < 2:
            error = "PRECONDITION_NOT_MET"
        elif platform == "Y" and not all(self.fixture["agreements"][1]["collateral_condition"][k]
                                         for k in ("conditions_met", "provided")):
            error = "PRECONDITION_NOT_MET"
        elif platform == "Z" and self.version == 3:
            error = "RIGHT_UNAVAILABLE"
        elif platform == "Z" and not self.fixture["agreements"][1]["ccp_use_allowed"]:
            error = "USE_NOT_PERMITTED"
        elif platform == "Z" and not self.fixture["margin_calls"][0]["eligibility_passed"]:
            error = "COLLATERAL_NOT_ELIGIBLE"
        kind, key = {"X": ("REPO", "repo_id"), "Y": ("LOAN", "loan_id"),
                     "Z": ("ALLOCATION", "allocation_id")}[platform]
        body = {"run_id": "run-synthetic", "request_id": rid,
                "actor": actor,
                "command_type": command["command_type"], "outcome": "REJECTED" if error else "COMMITTED",
                "replayed": False, "effect": None, "error": {"code": error, "message": error} if error else None}
        if not error:
            body["effect"] = {"bundle_id": "bundle-001", "version_before": self.version,
                              "version_after": self.version+1, "record_type": kind,
                              "record_id": command["payload"][key]}
            self.version += 1
            if platform == "Y":
                self.loan_id = command["payload"][key]
            if platform == "Z":
                self.allocation_id = command["payload"][key]
                self.margin_id = command["payload"]["margin_call_id"]
        self.originals = getattr(self, "originals", {})
        self.originals[rid] = command
        self.stored[rid] = body
        return httpx.Response(200, json=body)

    async def test_xy_normal_replay_and_missing_history(self):
        await self.execution.scenario_xy("normal")
        self.assertEqual(self.version, 2)
        for _ in range(2):
            self.execution.committed(await self.execution.submit("Y", replay=True))
        self.assertEqual(self.version, 2)
        with self.assertRaises(EvidenceMissing):
            history_check([], self.execution.results)

    async def test_xy_no_reuse(self):
        self.fixture["agreements"][0]["reuse_allowed"] = False
        await self.execution.scenario_xy("no-reuse")
        self.assertEqual(self.version, 1)

    async def test_distinct_race_one_effect(self):
        await self.execution.scenario_xy("race-loan")
        self.assertEqual(self.version, 2)
        self.assertEqual(sum(r["outcome"] == "COMMITTED" for r in self.stored.values()), 2)

    async def test_same_request_race(self):
        await self.execution.scenario_xy("race-same")
        self.assertEqual(self.version, 2)
        self.assertEqual(len(self.stored), 2)

    async def test_committed_effect_and_identity_must_match(self):
        await self.execution.advance(1)
        body = copy.deepcopy(self.stored["request-repo-001"])
        body["effect"]["version_after"] = 2
        with self.assertRaises(CheckError):
            check_result(body, "run-synthetic", self.execution.commands["X"], "BANK_A", 200)

    async def test_stale_replica_needs_current_observation(self):
        before = expected_state(self.fixture, 0)
        after = expected_state(self.fixture, 1)
        snapshots = {p: self.snapshot(p) for p in ("X", "Y")}
        with self.assertRaises(EvidenceMissing):
            state_check(snapshots, "run-synthetic", after, [before], ["X", "Y"])
        self.version = 1
        snapshots["X"] = self.snapshot("X")
        state_check(snapshots, "run-synthetic", after, [before], ["X", "Y"])

    async def test_pending_is_queried_without_duplicate_submission(self):
        calls = []

        async def delayed(request):
            calls.append(request.method)
            if request.method == "POST":
                response = await self.target(request)
                body = response.json()
                return httpx.Response(202, json=body | {"outcome": "PENDING", "effect": None})
            return await self.target(request)

        await self.execution.client.aclose()
        self.execution.client = httpx.AsyncClient(transport=httpx.MockTransport(delayed))
        self.execution.committed(await self.execution.submit("X"))
        self.assertEqual(calls, ["POST", "GET"])
        self.assertEqual(sum(e["event_type"] == "COMMIT_OBSERVED" for e in self.execution.events), 1)

    async def test_temporary_error_does_not_become_definitive_failure(self):
        calls = []

        async def unavailable(request):
            calls.append(request.method)
            if request.method == "POST":
                await self.target(request)
                return httpx.Response(503, json={"run_id": "run-synthetic", "request_id": "request-repo-001",
                    "error": {"code": "TEMPORARILY_UNAVAILABLE", "message": "response unavailable"}})
            return await self.target(request)

        await self.execution.client.aclose()
        self.execution.client = httpx.AsyncClient(transport=httpx.MockTransport(unavailable))
        self.execution.committed(await self.execution.submit("X"))
        self.assertEqual(calls, ["POST", "GET"])

    async def test_all_xyz_variants_against_synthetic_target(self):
        seen = set()
        for case in case_plan("A"):
            if case["variant"] == "recovery" or case["variant"] in seen:
                continue
            seen.add(case["variant"])
            with self.subTest(variant=case["variant"]):
                self.version, self.stored, self.originals = 0, {}, {}
                self.loan_id, self.allocation_id, self.margin_id = "loan-001", "allocation-001", "margin-001"
                self.fixture = read_json(ROOT / f"shared/fixtures/initial/{case['fixture']}.json")
                self.execution.fixture = self.fixture
                self.execution.platforms = ["X", "Y", "Z"]
                self.execution.results, self.execution.events, self.execution.previous = [], [], []
                self.execution.commands = {p: read_json(ROOT / f"shared/fixtures/commands/{name}.json")
                                           for p, name in (("X", "repo"), ("Y", "loan"), ("Z", "margin"))}
                await self.execution.scenario(case["variant"])

    async def test_final_gate_rejects_missing_repeat_duplicate_or_model(self):
        plan = [c for c in case_plan("A") if c["test_id"] == "AC-09"]
        self.assertEqual(len(plan), 10)
        rows = []
        for i, case in enumerate(plan):
            path = self.path / str(i)
            (path / "evidence").mkdir(parents=True)
            for name in ("manifest.json", "checks.json", "events.jsonl", "evidence/implementation-events.json"):
                (path / name).write_text("{}")
            rows.append(dict(case, model="A", run_id=f"run-{i}", path=str(i),
                             verdict="PASS", evidence_complete=True))
        self.assertTrue(final_gate("A", rows, plan, self.path))  # Empty placeholder files are not evidence.
        self.assertTrue(final_gate("A", rows[:-1], plan, self.path))
        self.assertTrue(final_gate("B1", rows, plan, self.path))
        rows[-1]["run_id"] = rows[0]["run_id"]
        self.assertTrue(final_gate("A", rows, plan, self.path))
