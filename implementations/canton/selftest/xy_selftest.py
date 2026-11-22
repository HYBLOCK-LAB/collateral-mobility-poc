#!/usr/bin/env python3
"""Stage 2 check of the X→Y normal path on the running adapters and Canton.

    . scripts/env.sh && .venv/bin/python -I selftest/xy_selftest.py

What it does, with a fresh run_id:
  prepare    creates the S0 contracts of shared/fixtures/initial/f0-normal.json
             directly on the ledger, as README §3 describes (P_X actAs
             [BANK_A, BANK_B]: right 0 and the Repo agreement; P_Y actAs
             [BANK_B, BANK_C]: the loan agreement). This stands in for the
             loader the research lead connects; it is not the common runner.
  S0         snapshots of X and Y against the fixture (checks.initial_state)
  readiness  Y CONFIRM_LOAN before the Repo: not ready, PRECONDITION_NOT_MET;
             two queries leave both snapshots unchanged
  repo       POST CONFIRM_REPO to X as BANK_A: 200 COMMITTED (check_result)
  S1         snapshots against expected/s1.json (state_check)
  readiness  Y ready with observed_version 1; again unchanged snapshots
  loan       POST CONFIRM_LOAN to Y as BANK_B: 200 COMMITTED
  S2         snapshots against expected/s2.json
  results    GET of both requests returns the committed result
  readiness  Y after the loan: not ready, RIGHT_UNAVAILABLE
  events     <evidence>/<run_id>/events.jsonl: checks.history_check, and one
             COMMAND_FINISHED COMMITTED per request

Judgments come from tests/conformance/checks.py, the functions the runner uses.
Resubmission, contention and recovery are stage 3 and are not checked here.
The run's contracts stay on the ledger; every run uses its own run_id.
The report goes to implementations/canton/logs/xy-selftest/<run_id>.json, without
credentials. Exit status 0 when every expectation held, 1 otherwise.
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

IMPL_DIR = Path(__file__).resolve().parent.parent
ROOT = IMPL_DIR.parent.parent
sys.path.insert(0, str(ROOT / "tests/conformance"))
from checks import (  # noqa: E402
    CheckError,
    EvidenceMissing,
    canonical,
    check_result,
    expected_state,
    history_check,
    initial_state,
    parse_json,
    read_json,
    state_check,
    validate,
)

TOKEN_ENV = {
    "bank-a": "POC_BANK_A_TOKEN",
    "bank-b": "POC_BANK_B_TOKEN",
    "observer": "POC_OBSERVER_TOKEN",
}
PKG = "#collateral"
PLATFORMS = ("X", "Y")
STATE_WAIT = 30.0  # how long a lagging participant may take to show a state
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Failed(Exception):
    pass


def http(method: str, url: str, *, body=None, token: str | None = None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with OPENER.open(request, timeout=60) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, payload = error.code, error.read()
    except (urllib.error.URLError, OSError) as error:  # no answer: refused, timeout
        raise Failed(f"{method} {url}: {error}") from error
    try:
        parsed = parse_json(payload.decode("utf-8")) if payload else None
    except (ValueError, CheckError):
        parsed = payload.decode("utf-8", "replace")
    return status, parsed


class XYSelftest:
    def __init__(self):
        state = Path(os.environ.get("CANTON_STATE_DIR", ""))
        self.base = {}
        for p in PLATFORMS:
            ready = state / f"adapter-{p}.ready"
            if not ready.is_file():
                raise SystemExit(f"{ready} missing (scripts/adapter.sh start)")
            self.base[p] = json.loads(ready.read_text())["url"]
        ids = json.loads((state / "ids.json").read_text())
        self.parties = ids["parties"]
        self.ledger = ids["json_api"]
        missing = [name for name in TOKEN_ENV.values() if not os.environ.get(name)]
        if missing:
            raise SystemExit(f"{', '.join(missing)} not set (. scripts/env.sh)")
        self.tokens = {role: os.environ[name] for role, name in TOKEN_ENV.items()}
        self.run_id = "xy-selftest-" + secrets.token_hex(6)
        self.evidence = Path(os.environ.get("CANTON_EVIDENCE_DIR") or IMPL_DIR / "logs")
        self.fixture = read_json(ROOT / "shared/fixtures/initial/f0-normal.json")
        self.repo = read_json(ROOT / "shared/fixtures/commands/repo.json")
        self.loan = read_json(ROOT / "shared/fixtures/commands/loan.json")
        self.results: list[dict] = []
        self.previous: list[dict] = []
        self.committed: dict[str, tuple[dict, dict]] = {}
        # Record IDs of the expected states (checks.expected_state); a
        # contention case names the winning request's IDs.
        self.record_ids: dict[str, str] = {}

    # --- reporting -------------------------------------------------------

    def step(self, name, fn):
        started = time.monotonic()
        try:
            detail = fn()
            ok, problem = True, None
        except (Failed, CheckError, KeyError, TypeError, ValueError) as error:
            detail, ok = None, False
            problem = f"{type(error).__name__}: {error}"
        entry = {
            "check": name,
            "ok": ok,
            "ms": round((time.monotonic() - started) * 1000, 1),
        }
        if problem:
            entry["problem"] = problem
        if detail is not None:
            entry["detail"] = detail
        self.results.append(entry)
        return ok

    # --- ledger preparation (README §3) ----------------------------------

    def submit(self, participant: str, act_as: list[str], creates: list[tuple[str, dict]]):
        party = {name: self.parties[name] for name in act_as}
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
                "userId": "xy-selftest",
                "actAs": list(party.values()),
            }
        }
        status, reply = http(
            "POST",
            self.ledger[participant] + "/v2/commands/submit-and-wait-for-transaction",
            body=body,
        )
        if status != 200:
            raise Failed(f"{participant} preparation: HTTP {status} {str(reply)[:400]}")
        return {"participant": participant, "offset": reply["transaction"]["offset"]}

    def prepare(self):
        bundle = self.fixture["bundles"][0]
        repo = next(a for a in self.fixture["agreements"] if a["type"] == "REPO")
        loan = next(a for a in self.fixture["agreements"] if a["type"] == "LOAN")
        p = self.parties
        right = {
            "runId": self.run_id,
            "bundle": {
                "bundleId": bundle["bundle_id"],
                "securityId": bundle["security_id"],
                "quantity": dict(bundle["quantity"]),
                "referenceValue": dict(bundle["reference_value"]),
            },
            "rightId": bundle["bundle_id"] + ":right:0",
            "holder": p[bundle["initial_holder"]],
            # The initial right is cosigned by the Repo counterparty (README §3).
            "cosigner": p[repo["buyer"]],
            "createdVersion": "0",
            "status": "AVAILABLE",
            "allowedCommands": ["CONFIRM_REPO"],
            "predecessorRightId": None,
            "sourceRecordId": None,
        }
        repo_agreement = {
            "runId": self.run_id,
            "agreementId": repo["agreement_id"],
            "bundleId": repo["bundle_id"],
            "seller": p[repo["seller"]],
            "buyer": p[repo["buyer"]],
            "reuseAllowed": repo["reuse_allowed"],
        }
        condition = loan["collateral_condition"]
        loan_agreement = {
            "runId": self.run_id,
            "agreementId": loan["agreement_id"],
            "bundleId": loan["bundle_id"],
            "lender": p[loan["lender"]],
            "borrower": p[loan["borrower"]],
            "ccpUseAllowed": loan["ccp_use_allowed"],
            "collateral": {
                "collateralId": condition["collateral_id"],
                "conditionsMet": condition["conditions_met"],
                "provided": condition["provided"],
            },
        }
        return [
            self.submit(
                "P_X",
                [repo["seller"], repo["buyer"]],
                [
                    ("Collateral.Right:UseRight", right),
                    ("Collateral.Agreement:RepoAgreement", repo_agreement),
                ],
            ),
            self.submit(
                "P_Y",
                [loan["lender"], loan["borrower"]],
                [("Collateral.Agreement:LoanAgreement", loan_agreement)],
            ),
        ]

    # --- adapter calls ---------------------------------------------------

    def adapter(
        self, method, platform, path, role, *, command=None, params=None, test_id
    ):
        url = self.base[platform] + f"/runs/{self.run_id}" + path
        if params:
            url += "?" + urlencode(params)
        headers = {
            "X-Test-ID": test_id,
            "X-Attempt-ID": f"attempt-xy-{secrets.token_hex(8)}",
        }
        return http(method, url, body=command, token=self.tokens[role], headers=headers)

    def snapshots(self) -> dict:
        found = {}
        for p in PLATFORMS:
            status, body = self.adapter(
                "GET", p, "/snapshot", "observer", test_id="XY-SNAPSHOT"
            )
            if status != 200 or not isinstance(body, dict):
                raise Failed(f"snapshot {p}: HTTP {status} {str(body)[:400]}")
            found[p] = body
        return found

    def state(self, version: int) -> dict:
        expected = expected_state(self.fixture, version, **self.record_ids)
        deadline = time.monotonic() + STATE_WAIT
        while True:
            snapshots = self.snapshots()
            try:
                state_check(snapshots, self.run_id, expected, self.previous, PLATFORMS)
            except EvidenceMissing:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
                continue
            self.previous.append(expected)
            return snapshots

    def readiness(self, expected_ready: bool, version, reasons: list[str]) -> dict:
        params = {
            "command_type": self.loan["command_type"],
            "bundle_id": self.loan["bundle_id"],
            "agreement_id": self.loan["payload"]["agreement_id"],
        }
        status, body = self.adapter(
            "GET", "Y", "/readiness", "bank-b", params=params, test_id="XY-READINESS"
        )
        if status != 200 or not isinstance(body, dict) or "ready" not in body:
            raise Failed(f"readiness: HTTP {status} {body}")
        validate(body, "readiness.schema.json")
        identity = {
            "run_id": self.run_id,
            "platform": "Y",
            "bundle_id": params["bundle_id"],
            "command_type": params["command_type"],
        }
        if any(body[k] != v for k, v in identity.items()):
            raise Failed(f"readiness identity {body}")
        wanted = (expected_ready, version, reasons)
        got = (body["ready"], body["observed_version"], body["blocking_reasons"])
        if got != wanted:
            raise Failed(f"readiness {got}, expected {wanted}")
        return body

    def unchanged_readiness(self, expected_ready, version, reasons):
        """AC-02 shape: two readiness queries leave every snapshot unchanged."""
        before = self.snapshots()
        bodies = [self.readiness(expected_ready, version, reasons) for _ in range(2)]
        after = self.snapshots()
        for p in PLATFORMS:
            strip = {k: v for k, v in before[p].items() if k != "snapshot_seq"}
            again = {k: v for k, v in after[p].items() if k != "snapshot_seq"}
            if canonical(strip) != canonical(again):
                raise Failed(f"readiness changed the snapshot of {p}")
        return bodies[0]

    def confirm(self, platform, role, actor, command, test_id):
        status, body = self.adapter(
            "POST", platform, "/commands", role, command=command, test_id=test_id
        )
        check_result(body, self.run_id, command, actor, status)
        if status != 200 or body.get("outcome") != "COMMITTED":
            raise Failed(f"{command['command_type']}: HTTP {status} {body}")
        self.committed[command["request_id"]] = (command, body)
        return body

    def result(self, platform, role, command):
        status, body = self.adapter(
            "GET",
            platform,
            "/commands/" + command["request_id"],
            role,
            test_id="XY-RESULT",
        )
        actor = {"bank-a": "BANK_A", "bank-b": "BANK_B"}[role]
        check_result(body, self.run_id, command, actor, status)
        posted = self.committed[command["request_id"]][1]
        if status != 200 or body != posted:
            raise Failed(
                f"GET result {status} {body} differs from the POST answer {posted}"
            )
        return body

    def events(self):
        path = self.evidence / self.run_id / "events.jsonl"
        if not path.is_file():
            raise Failed(f"{path} missing")
        events = [json.loads(line) for line in path.read_text().splitlines() if line]
        history_check(events, list(self.committed.values()))
        for request_id in self.committed:
            finished = [
                e
                for e in events
                if e["event_type"] == "COMMAND_FINISHED"
                and e["request_id"] == request_id
            ]
            if [e["outcome"] for e in finished] != ["COMMITTED"]:
                raise Failed(f"{request_id}: COMMAND_FINISHED {finished}")
        kinds = {}
        for e in events:
            kinds.setdefault(e["platform"], []).append(e["event_type"])
        return {"events": len(events), "by_platform": kinds}

    # --- run -------------------------------------------------------------

    def run(self) -> int:
        steps = [
            ("prepare", self.prepare),
            (
                "s0-initial-state",
                lambda: (
                    initial_state(
                        self.fixture, self.snapshots(), self.run_id, PLATFORMS
                    )
                    or self.previous.append(expected_state(self.fixture, 0))
                ),
            ),
            (
                "readiness-before-repo",
                lambda: self.unchanged_readiness(False, None, ["PRECONDITION_NOT_MET"]),
            ),
            (
                "confirm-repo-X",
                lambda: self.confirm("X", "bank-a", "BANK_A", self.repo, "XY-REPO"),
            ),
            ("s1-state", lambda: self.state(1) and None),
            ("readiness-after-repo", lambda: self.unchanged_readiness(True, 1, [])),
            (
                "confirm-loan-Y",
                lambda: self.confirm("Y", "bank-b", "BANK_B", self.loan, "XY-LOAN"),
            ),
            ("s2-state", lambda: self.state(2) and None),
            ("result-repo-X", lambda: self.result("X", "bank-a", self.repo)),
            ("result-loan-Y", lambda: self.result("Y", "bank-b", self.loan)),
            (
                "readiness-after-loan",
                lambda: self.unchanged_readiness(False, None, ["RIGHT_UNAVAILABLE"]),
            ),
            ("events-history", self.events),
        ]
        for name, fn in steps:
            if not self.step(name, fn):
                break  # later steps depend on the earlier state
        final = self.snapshots_or_none()
        report = {
            "run_id": self.run_id,
            "finished_utc": datetime.now(timezone.utc).isoformat(),
            "checks": self.results,
            "final_snapshots": final,
            "passed": sum(r["ok"] for r in self.results),
            "planned": len(steps),
        }
        out = IMPL_DIR / "logs/xy-selftest" / f"{self.run_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        for r in self.results:
            print(
                f"{'ok  ' if r['ok'] else 'FAIL'} {r['check']:<24} {r['ms']:>8} ms"
                + (f"  {r['problem']}" if not r["ok"] else "")
            )
        print(f"{report['passed']}/{report['planned']} passed; report {out}")
        return 0 if report["passed"] == report["planned"] else 1

    def snapshots_or_none(self):
        try:
            return copy.deepcopy(self.snapshots())
        except Failed:
            return None


if __name__ == "__main__":
    sys.exit(XYSelftest().run())
