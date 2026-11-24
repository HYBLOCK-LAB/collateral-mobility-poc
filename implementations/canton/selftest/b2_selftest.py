#!/usr/bin/env python3
"""Check of the B2 normal path on the running adapters and Canton (b2 topology).

    . scripts/env.sh && .venv/bin/python -I selftest/b2_selftest.py

The X→Y→Z steps of selftest/xyz_selftest.py with the contracts placed as
README §3 B2 describes, and the reassignment of BANK_B's right before Y:

  prepare      as xyz_selftest, each create given its synchronizer: on S1 the
               right 0 and the Repo agreement (P_X), on S2 the loan agreement
               (P_Y) and the margin calls (P_Z). This stands in for the loader
               the research lead connects.
  X            CONFIRM_REPO creates right 1 on S1: Y is not ready
               (RIGHT_UNAVAILABLE, the right is on S1), and X has declared
               the right in <evidence>/<run_id>/contracts.json
  reassign     POST REASSIGN_FOR_LENDING (shared/fixtures/commands/
               reassignment.json) to X as BANK_B: 200 COMMITTED; the S1 state
               is unchanged; the declared set is unchanged and
               checks.reassignment_check holds over the run's events, with
               every raw evidence file present under its hash, as the common
               runner checks them; GET returns the committed result
  Y, Z         as xyz_selftest: Y is ready on S2, CONFIRM_LOAN, then Z
  events       checks.history_check over the four requests

Judgments come from tests/conformance/checks.py. Refusals, replays, contention
and recovery of the reassignment are not checked here. The report goes to
implementations/canton/logs/b2-selftest/<run_id>.json, without credentials.
Exit status 0 when every expectation held, 1 otherwise.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

# -I leaves the script's directory off sys.path (as in authz_check).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from xy_selftest import PKG, ROOT, Failed, http  # noqa: E402
from xyz_selftest import XYZSelftest  # noqa: E402

from checks import (  # noqa: E402  (path set by xy_selftest)
    CheckError,
    EvidenceMissing,
    canonical,
    expected_state,
    read_json,
    reassignment_check,
)

# README §3 B2: where each participant's preparation creates its contracts.
PLACEMENT = {"P_X": "S1", "P_Y": "S2", "P_Z": "S2"}
EVIDENCE_KEYS = ("unassigned_evidence", "assigned_evidence")


def selection(manifest: dict) -> list:
    """The declared contracts without the evidence references the
    reassignment adds (runner.reassign)."""
    return [
        {k: v for k, v in c.items() if k not in EVIDENCE_KEYS}
        for c in manifest.get("required_contracts", [])
    ]


class B2Selftest(XYZSelftest):
    name = "b2-selftest"

    def __init__(self):
        super().__init__()
        ids = json.loads(
            (Path(os.environ["CANTON_STATE_DIR"]) / "ids.json").read_text()
        )
        if ids.get("topology") != "b2":
            raise SystemExit(
                f"the ledger is {ids.get('topology')}, not b2 (README §4 B2)"
            )
        self.synchronizers = ids["synchronizers"]
        self.reassignment = read_json(
            ROOT / "shared/fixtures/commands/reassignment.json"
        )
        self.selected: dict | None = None

    def submit(
        self, participant: str, act_as: list[str], creates: list[tuple[str, dict]]
    ):
        body = {
            "commands": {
                "commands": [
                    {
                        "CreateCommand": {
                            "templateId": f"{PKG}:{t}",
                            "createArguments": a,
                        }
                    }
                    for t, a in creates
                ],
                "commandId": f"prepare-{self.run_id}-{participant}",
                "userId": "b2-selftest",
                "actAs": [self.parties[name] for name in act_as],
                "synchronizerId": self.synchronizers[PLACEMENT[participant]],
            }
        }
        status, reply = http(
            "POST",
            self.ledger[participant] + "/v2/commands/submit-and-wait-for-transaction",
            body=body,
        )
        if status != 200:
            raise Failed(f"{participant} preparation: HTTP {status} {str(reply)[:400]}")
        transaction = reply["transaction"]
        if transaction.get("synchronizerId") != body["commands"]["synchronizerId"]:
            raise Failed(f"{participant} preparation went to another synchronizer")
        return {
            "participant": participant,
            "synchronizer": PLACEMENT[participant],
            "offset": transaction["offset"],
        }

    # --- reassignment ----------------------------------------------------

    def manifest_path(self) -> Path:
        return self.evidence / self.run_id / "contracts.json"

    def declared(self) -> dict:
        """X declared BANK_B's right 1 once CONFIRM_REPO created it."""
        path = self.manifest_path()
        if not path.is_file():
            raise Failed(f"{path} missing after CONFIRM_REPO")
        manifest = read_json(path)
        bundle = self.reassignment["bundle_id"]
        contracts = manifest.get("required_contracts", [])
        if manifest.get("run_id") != self.run_id:
            raise Failed(f"contracts.json run_id {manifest.get('run_id')}")
        if manifest.get("synchronizers") != self.synchronizers:
            raise Failed(
                f"contracts.json synchronizers {manifest.get('synchronizers')}"
            )
        if [c.get("business_refs") for c in contracts] != [[f"{bundle}:right:1"]]:
            raise Failed(f"contracts.json declares {contracts}")
        if manifest.get("api_calls") != [] or any(
            k in c for c in contracts for k in EVIDENCE_KEYS
        ):
            raise Failed("contracts.json has evidence before the reassignment")
        self.selected = manifest
        return {"required_contracts": len(contracts)}

    def reassigned(self) -> dict:
        """runner.reassign's checks over the final contracts.json and events."""
        source = (self.evidence / self.run_id).resolve()
        final = read_json(self.manifest_path())
        if self.selected is None:
            raise Failed("no declared set read before the reassignment")
        if canonical(selection(self.selected)) != canonical(selection(final)) or (
            self.selected.get("synchronizers") != final.get("synchronizers")
        ):
            raise CheckError("required contract set changed during reassignment")
        events = [
            json.loads(line)
            for line in (source / "events.jsonl").read_text().splitlines()
            if line
        ]
        expected = expected_state(self.fixture, 1)
        refs = [r["right_id"] for r in expected["rights"] if r["status"] == "AVAILABLE"]
        metrics = reassignment_check(final, events, self.run_id, refs)
        references = [
            contract[name]
            for contract in final["required_contracts"]
            for name in EVIDENCE_KEYS
        ]
        references += [call["evidence"] for call in final["api_calls"]]
        for reference in references:
            path = (source / reference["path"]).resolve()
            if (
                not path.is_relative_to(source)
                or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != reference["sha256"]
            ):
                raise EvidenceMissing(
                    "raw Canton reassignment evidence missing/hash mismatch"
                )
            if not path.read_text(encoding="utf-8").strip():
                raise EvidenceMissing("raw Canton event empty")
        return metrics

    # --- run -------------------------------------------------------------

    def steps(self) -> list:
        steps = super().steps()
        names = [name for name, _ in steps]
        steps[
            names.index("readiness-after-repo") : names.index("readiness-after-repo")
        ] = [
            (
                "readiness-Y-on-S1",
                lambda: self.unchanged_readiness(False, 1, ["RIGHT_UNAVAILABLE"]),
            ),
            ("contracts-declared", self.declared),
            (
                "reassign-X",
                lambda: self.confirm(
                    "X", "bank-b", "BANK_B", self.reassignment, "B2-REASSIGN"
                ),
            ),
            ("s1-after-reassign", lambda: self.state(1) and None),
            ("reassignment-check", self.reassigned),
            (
                "result-reassign-X",
                lambda: self.result("X", "bank-b", self.reassignment),
            ),
        ]
        return steps


if __name__ == "__main__":
    sys.exit(B2Selftest().run())
