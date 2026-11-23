"""B1 loader protocol/mapping tests with a mock Ledger API, not Canton acceptance."""

import copy
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from checks import ROOT, CheckError, read_json
from runner import collect_history

spec = importlib.util.spec_from_file_location("canton_prepare", ROOT / "implementations/canton/prepare.py")
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)
PARTIES = {"BANK_A": "a::test", "BANK_B": "b::test", "BANK_C": "c::test"}


class CantonConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.evidence = self.state / "events"
        environment = patch.dict(os.environ, {"CANTON_EVIDENCE_DIR": str(self.evidence)})
        environment.start()
        self.addCleanup(environment.stop)
        (self.state / "ids.json").write_text(json.dumps({
            "parties": PARTIES,
            "json_api": {"P_X": "http://127.0.0.1:4513", "P_Y": "http://127.0.0.1:4523"},
        }))
        self.fixture = read_json(ROOT / "shared/fixtures/initial/f0-normal.json")
        self.creates = []
        self.fail_y = False
        self.exists = False
        self.existing_z = False
        self.fail_z = False

    def xyz_state(self):
        ids = json.loads((self.state / "ids.json").read_text())
        ids.update(topology="b1-xyz")
        ids["parties"]["CCP"] = "ccp::test"
        ids["json_api"]["P_Z"] = "http://127.0.0.1:4533"
        (self.state / "ids.json").write_text(json.dumps(ids))
        return ids

    def ledger(self, request):
        body = json.loads(request.content)
        if request.url.path.endswith("active-contracts-page"):
            self.assertEqual(body["maxPageSize"], 200)
            rows = [{"contractEntry": {"JsActiveContract": {"createdEvent": {
                "packageName": "collateral", "createArgument": {"runId": "run-test"}
            }}}}] if self.exists or (self.existing_z and request.url.port == 4533) else []
            return httpx.Response(200, json={"activeContracts": rows})
        for command in body["commands"]["commands"]:
            create = command["CreateCommand"]
            if create["templateId"] == "#collateral:Collateral.Right:UseRight":
                self.assertEqual(create["createArguments"]["cosigner"], PARTIES["BANK_B"])
        self.creates.append((request.url.port, body["commands"]))
        if self.fail_y and request.url.port == 4523:
            raise httpx.ReadTimeout("uncertain submission", request=request)
        if self.fail_z and request.url.port == 4533:
            raise httpx.ReadTimeout("uncertain Z submission", request=request)
        return httpx.Response(200, json={"transaction": {"offset": 10}})

    def test_success_only_creates_s0_on_expected_participants(self):
        with httpx.Client(transport=httpx.MockTransport(self.ledger)) as client:
            loader.prepare("run-test", self.fixture, self.state, client)
        self.assertEqual([p for p, _ in self.creates], [4513, 4523])
        x, y = [body for _, body in self.creates]
        self.assertEqual(x["actAs"], [PARTIES["BANK_A"], PARTIES["BANK_B"]])
        self.assertEqual(y["actAs"], [PARTIES["BANK_B"], PARTIES["BANK_C"]])
        self.assertEqual(len(x["commands"]), 2)
        self.assertEqual(len(y["commands"]), 1)
        self.assertTrue(all(set(c) == {"CreateCommand"} for body in (x,y) for c in body["commands"]))
        self.assertTrue((self.state / "conformance-preparations/run-test.json").exists())
        event_file = self.evidence / "run-test/events.jsonl"
        self.assertEqual(event_file.read_bytes(), b"")
        archive = self.state / "evidence"
        archive.mkdir()
        execution = SimpleNamespace(config={"evidence_dir": str(self.evidence), "model": "B1"},
                                    run_id="run-test", path=self.state, secrets=[], test_id="AC-02",
                                    results=[], race_requests=[])
        self.assertEqual(collect_history(execution), [])
        self.assertTrue((archive / "implementation-events.json").exists())

    def test_preparation_does_not_truncate_existing_history(self):
        event_file = self.evidence / "run-test/events.jsonl"
        event_file.parent.mkdir(parents=True)
        event_file.write_bytes(b"preserved existing history\n")
        with httpx.Client(transport=httpx.MockTransport(self.ledger)) as client:
            loader.prepare("run-test", self.fixture, self.state, client)
        self.assertEqual(event_file.read_bytes(), b"preserved existing history\n")

    def test_existing_ledger_run_is_never_submitted(self):
        self.exists = True
        with httpx.Client(transport=httpx.MockTransport(self.ledger)) as client:
            with self.assertRaisesRegex(CheckError, "RUN_ALREADY_EXISTS"):
                loader.prepare("run-test", self.fixture, self.state, client)
        self.assertEqual(self.creates, [])

    def test_uncertain_partial_run_keeps_claim_and_cannot_be_reused(self):
        self.fail_y = True
        with httpx.Client(transport=httpx.MockTransport(self.ledger)) as client:
            with self.assertRaises(httpx.ReadTimeout):
                loader.prepare("run-test", self.fixture, self.state, client)
            self.assertFalse((self.evidence / "run-test/events.jsonl").exists())
            self.fail_y = False
            with self.assertRaisesRegex(CheckError, "RUN_ALREADY_EXISTS"):
                loader.prepare("run-test", self.fixture, self.state, client)
        self.assertEqual(len(self.creates), 2)

    def test_fixture_variants_keep_terms_and_input_unchanged(self):
        for path in (ROOT / "shared/fixtures/initial").glob("*.json"):
            fixture = read_json(path)
            before = copy.deepcopy(fixture)
            mapped = loader.commands("run-test", fixture, PARTIES)
            self.assertEqual(fixture, before)
            arguments = {c["CreateCommand"]["templateId"]:c["CreateCommand"]["createArguments"]
                         for group in mapped.values() for c in group}
            repo = next(a for a in fixture["agreements"] if a["type"] == "REPO")
            loan = next(a for a in fixture["agreements"] if a["type"] == "LOAN")
            right = arguments['#collateral:Collateral.Right:UseRight']
            self.assertEqual(right['cosigner'], PARTIES[repo['buyer']])
            self.assertNotEqual(right['holder'], right['cosigner'])
            self.assertEqual(arguments['#collateral:Collateral.Agreement:RepoAgreement']['reuseAllowed'],repo['reuse_allowed'])
            result = arguments['#collateral:Collateral.Agreement:LoanAgreement']
            self.assertEqual(result['ccpUseAllowed'],loan['ccp_use_allowed'])
            self.assertEqual(result['collateral']['conditionsMet'],loan['collateral_condition']['conditions_met'])
            self.assertEqual(result['collateral']['provided'],loan['collateral_condition']['provided'])

    def test_nonlocal_ledger_url_is_rejected(self):
        ids = json.loads((self.state / "ids.json").read_text())
        ids['json_api']['P_X']='http://remote.example:4513'
        (self.state / "ids.json").write_text(json.dumps(ids))
        with httpx.Client(transport=httpx.MockTransport(self.ledger)) as client:
            with self.assertRaises(CheckError):
                loader.prepare("run-test", self.fixture, self.state, client)
        self.assertEqual(self.creates, [])

    def test_xyz_creates_margin_calls_with_both_signers_and_no_business_choices(self):
        ids = self.xyz_state()
        self.fixture = read_json(ROOT / "shared/fixtures/initial/two-margin-calls.json")
        with httpx.Client(transport=httpx.MockTransport(self.ledger)) as client:
            loader.prepare("run-test", self.fixture, self.state, client, "xyz")
        self.assertEqual([port for port, _ in self.creates], [4513, 4523, 4533])
        z = self.creates[-1][1]
        self.assertEqual(z["actAs"], [PARTIES["BANK_C"], ids["parties"]["CCP"]])
        self.assertEqual(len(z["commands"]), 2)
        for command, margin in zip(z["commands"], self.fixture["margin_calls"]):
            self.assertEqual(set(command), {"CreateCommand"})
            created = command["CreateCommand"]
            self.assertEqual(created["templateId"], "#collateral:Collateral.Margin:MarginCall")
            self.assertEqual(created["createArguments"]["marginCallId"], margin["margin_call_id"])
            self.assertEqual(created["createArguments"]["status"], "OPEN")
        self.assertEqual((self.evidence / "run-test/events.jsonl").read_bytes(), b"")

    def test_default_xy_does_not_seed_z_on_xyz_topology(self):
        self.xyz_state()
        with httpx.Client(transport=httpx.MockTransport(self.ledger)) as client:
            loader.prepare("run-test", self.fixture, self.state, client)
        self.assertEqual([port for port, _ in self.creates], [4513, 4523])

    def test_xyz_fixture_conditions_are_preserved_without_mutating_input(self):
        parties = self.xyz_state()["parties"]
        for path in (ROOT / "shared/fixtures/initial").glob("*.json"):
            fixture = read_json(path)
            original = copy.deepcopy(fixture)
            mapped = loader.commands("run-test", fixture, parties, "xyz")
            self.assertEqual(fixture, original)
            for create, margin in zip(mapped["P_Z"], fixture["margin_calls"], strict=True):
                self.assertEqual(create["CreateCommand"]["createArguments"], {
                    "runId": "run-test", "marginCallId": margin["margin_call_id"],
                    "bundleId": margin["bundle_id"], "provider": parties[margin["provider"]],
                    "ccp": parties[margin["ccp"]], "wholeBundleAccepted": margin["whole_bundle_accepted"],
                    "eligibilityPassed": margin["eligibility_passed"], "status": margin["status"],
                })

    def test_xyz_rejects_xy_topology_before_submitting_or_claiming(self):
        with httpx.Client(transport=httpx.MockTransport(self.ledger)) as client:
            with self.assertRaisesRegex(CheckError, "b1-xyz"):
                loader.prepare("run-test", self.fixture, self.state, client, "xyz")
        self.assertEqual(self.creates, [])
        self.assertFalse((self.state / "conformance-preparations").exists())

    def test_z_existing_run_is_checked_before_any_participant_submits(self):
        self.xyz_state()
        self.existing_z = True
        with httpx.Client(transport=httpx.MockTransport(self.ledger)) as client:
            with self.assertRaisesRegex(CheckError, "RUN_ALREADY_EXISTS"):
                loader.prepare("run-test", self.fixture, self.state, client, "xyz")
        self.assertEqual(self.creates, [])
        self.assertFalse((self.state / "conformance-preparations").exists())

    def test_uncertain_z_submission_keeps_claim_without_initializing_success_log(self):
        self.xyz_state()
        self.fail_z = True
        with httpx.Client(transport=httpx.MockTransport(self.ledger)) as client:
            with self.assertRaises(httpx.ReadTimeout):
                loader.prepare("run-test", self.fixture, self.state, client, "xyz")
            self.assertTrue((self.state / "conformance-preparations/run-test.json").exists())
            self.assertFalse((self.evidence / "run-test/events.jsonl").exists())
            self.fail_z = False
            with self.assertRaisesRegex(CheckError, "RUN_ALREADY_EXISTS"):
                loader.prepare("run-test", self.fixture, self.state, client, "xyz")
        self.assertEqual(len(self.creates), 3)
