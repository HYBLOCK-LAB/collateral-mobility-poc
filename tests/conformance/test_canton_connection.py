"""B1 loader protocol/mapping tests with a mock Ledger API, not Canton acceptance."""

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from checks import ROOT, CheckError, read_json

spec = importlib.util.spec_from_file_location("canton_prepare", ROOT / "implementations/canton/prepare.py")
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)
PARTIES = {"BANK_A": "a::test", "BANK_B": "b::test", "BANK_C": "c::test"}


class CantonConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        (self.state / "ids.json").write_text(json.dumps({
            "parties": PARTIES,
            "json_api": {"P_X": "http://127.0.0.1:4513", "P_Y": "http://127.0.0.1:4523"},
        }))
        self.fixture = read_json(ROOT / "shared/fixtures/initial/f0-normal.json")
        self.creates = []
        self.fail_y = False
        self.exists = False

    def ledger(self, request):
        body = json.loads(request.content)
        if request.url.path.endswith("active-contracts-page"):
            self.assertEqual(body["maxPageSize"], 200)
            rows = [{"contractEntry": {"JsActiveContract": {"createdEvent": {
                "packageName": "collateral", "createArgument": {"runId": "run-test"}
            }}}}] if self.exists else []
            return httpx.Response(200, json={"activeContracts": rows})
        for command in body["commands"]["commands"]:
            create = command["CreateCommand"]
            if create["templateId"] == "#collateral:Collateral.Right:UseRight":
                self.assertEqual(create["createArguments"]["cosigner"], PARTIES["BANK_B"])
        self.creates.append((request.url.port, body["commands"]))
        if self.fail_y and request.url.port == 4523:
            raise httpx.ReadTimeout("uncertain submission", request=request)
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
