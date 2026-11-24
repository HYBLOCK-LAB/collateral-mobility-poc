"""B2 fixture placement and refusal tests with a mock Ledger API."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from checks import ROOT, CheckError, read_json
from test_canton_connection import PARTIES, loader


class CantonB2ConnectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        self.evidence = self.state / "events"
        environment = patch.dict(os.environ, {"CANTON_EVIDENCE_DIR": str(self.evidence)})
        environment.start()
        self.addCleanup(environment.stop)
        (self.state / "ids.json").write_text(json.dumps({
            "topology": "b2",
            "synchronizers": {"S1": "source", "S2": "target"},
            "parties": PARTIES | {"CCP": "ccp::test"},
            "json_api": {f"P_{p}": f"http://127.0.0.1:{port}"
                         for p, port in zip("XYZ", (4513, 4523, 4533))},
        }))
        self.fixture = read_json(ROOT / "shared/fixtures/initial/f0-normal.json")

    def test_b2_preparation_pins_synchronizers_and_checks_ledger_receipts(self):
        submissions = []

        def ledger(request):
            body = json.loads(request.content)
            if request.url.path.endswith("active-contracts-page"):
                self.assertTrue(body["includeIncompleteReassignments"])
                return httpx.Response(200, json={"activeContracts": []})
            synchronizer = body["commands"]["synchronizerId"]
            submissions.append((request.url.port, synchronizer))
            return httpx.Response(200, json={"transaction": {"synchronizerId": synchronizer}})

        with httpx.Client(transport=httpx.MockTransport(ledger)) as client:
            with self.assertRaisesRegex(CheckError, "CANTON_PREPARE_TOPOLOGY"):
                loader.prepare("run-test", self.fixture, self.state, client, "xyz")
            self.assertEqual(submissions, [])
            loader.prepare("run-test", self.fixture, self.state, client, "xyz", b2=True)
        self.assertEqual(submissions, [(4513, "source"), (4523, "target"), (4533, "target")])
        self.assertTrue((self.evidence / "run-test/events.jsonl").is_file())

    def test_b2_existing_incomplete_reassignment_blocks_initial_submission(self):
        entry = {"JsIncompleteUnassigned": {"value": {"createdEvent": {
            "packageName": "collateral", "createArgument": {"runId": "run-test"}
        }}}}
        with httpx.Client(transport=httpx.MockTransport(lambda request:
                httpx.Response(200, json={"activeContracts": [{"contractEntry": entry}]}))) as client:
            with self.assertRaisesRegex(CheckError, "RUN_ALREADY_EXISTS"):
                loader.prepare("run-test", self.fixture, self.state, client, "xyz", b2=True)
        self.assertFalse((self.state / "conformance-preparations").exists())
