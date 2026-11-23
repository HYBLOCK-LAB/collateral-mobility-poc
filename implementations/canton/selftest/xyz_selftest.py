#!/usr/bin/env python3
"""Check of the X→Y→Z normal path on the running adapters and Canton (b1-xyz).

    . scripts/env.sh && .venv/bin/python -I selftest/xyz_selftest.py

The X→Y steps of selftest/xy_selftest.py on all three platforms, and Z:

  prepare    as xy_selftest, and on P_Z actAs [BANK_C, CCP] the margin calls
             of shared/fixtures/initial/f0-normal.json (README §3). This stands
             in for the loader the research lead connects.
  readiness  Z ALLOCATE_MARGIN in S0: not ready, PRECONDITION_NOT_MET (the
             right has not reached Z); two queries leave the snapshots unchanged
  X→Y        repo, S1, loan, S2 and their readiness checks, as xy_selftest
  readiness  Z after the loan: ready with observed_version 2 once P_Z shows
             the loan; again unchanged snapshots
  margin     POST ALLOCATE_MARGIN to Z as BANK_C: 200 COMMITTED (check_result)
  S3         snapshots of X, Y and Z against expected/s3.json (state_check)
  results    GET of the three requests returns the committed result
  readiness  Z after the allocation: not ready, RIGHT_UNAVAILABLE and
             PRECONDITION_NOT_MET (right LOCKED, margin call SATISFIED)
  events     checks.history_check over the three requests (RIGHT_LOCKED and
             LOCAL_APPLIED on Z for the allocation), one COMMAND_FINISHED each

Judgments come from tests/conformance/checks.py. Refusals, replays, contention
and recovery of Z are not checked here. The report goes to
implementations/canton/logs/xyz-selftest/<run_id>.json, without credentials.
Exit status 0 when every expectation held, 1 otherwise.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# -I leaves the script's directory off sys.path (as in authz_check).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from xy_selftest import ROOT, STATE_WAIT, TOKEN_ENV, Failed, XYSelftest  # noqa: E402

from checks import read_json  # noqa: E402  (path set by xy_selftest)

MARGIN_CALL = "Collateral.Margin:MarginCall"


class XYZSelftest(XYSelftest):
    name = "xyz-selftest"
    platforms = ("X", "Y", "Z")
    token_env = TOKEN_ENV | {"bank-c": "POC_BANK_C_TOKEN"}
    actors = XYSelftest.actors | {"bank-c": "BANK_C"}
    targets = XYSelftest.targets | {"Z": ("margin", "bank-c", "margin_call_id")}

    def __init__(self):
        super().__init__()
        self.margin = read_json(ROOT / "shared/fixtures/commands/margin.json")

    def prepare(self):
        done = super().prepare()
        p = self.parties
        calls = [
            {
                "runId": self.run_id,
                "marginCallId": call["margin_call_id"],
                "bundleId": call["bundle_id"],
                "provider": p[call["provider"]],
                "ccp": p[call["ccp"]],
                "wholeBundleAccepted": call["whole_bundle_accepted"],
                "eligibilityPassed": call["eligibility_passed"],
                "status": call["status"],
            }
            for call in self.fixture["margin_calls"]
        ]
        done.append(
            self.submit("P_Z", ["BANK_C", "CCP"], [(MARGIN_CALL, c) for c in calls])
        )
        return done

    def ready_z(self):
        """Z sees the loan's right later than the union of the snapshots may
        show S2; wait for readiness as the runner does, then check it."""
        deadline = time.monotonic() + STATE_WAIT
        while True:
            try:
                return self.unchanged_readiness(True, 2, [], "Z")
            except Failed:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)

    def steps(self) -> list:
        steps = super().steps()
        names = [name for name, _ in steps]
        after_s0 = names.index("s0-initial-state") + 1
        steps.insert(
            after_s0,
            (
                "readiness-Z-in-s0",
                lambda: self.unchanged_readiness(
                    False, None, ["PRECONDITION_NOT_MET"], "Z"
                ),
            ),
        )
        events = [name for name, _ in steps].index("events-history")
        steps[events:events] = [
            ("readiness-Z-after-loan", self.ready_z),
            (
                "allocate-margin-Z",
                lambda: self.confirm(
                    "Z", "bank-c", "BANK_C", self.margin, "XYZ-MARGIN"
                ),
            ),
            ("s3-state", lambda: self.state(3) and None),
            ("result-margin-Z", lambda: self.result("Z", "bank-c", self.margin)),
            (
                "readiness-Z-after-margin",
                lambda: self.unchanged_readiness(
                    False, None, ["RIGHT_UNAVAILABLE", "PRECONDITION_NOT_MET"], "Z"
                ),
            ),
        ]
        return steps


if __name__ == "__main__":
    sys.exit(XYZSelftest().run())
