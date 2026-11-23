"""Coordinator S0 loader; XYZ requires PR #9 collateral 0.4.0 and fresh b1-xyz state."""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/conformance"))
from checks import CheckError, read_json, validate_fixture  # noqa: E402


def commands(run_id, fixture, parties, stage="xy"):
    validate_fixture(fixture)
    mapped = {p: [] for p in {"xy": ("P_X", "P_Y"), "xyz": ("P_X", "P_Y", "P_Z")}[stage]}

    def create(participant, template, argument):
        mapped[participant].append({"CreateCommand": {
            "templateId": "#collateral:" + template,
            "createArguments": dict(runId=run_id, **argument),
        }})

    for b in fixture["bundles"]:
        if b["initial_holder"] != "BANK_A" or len(b["bundle_id"] + ":right:0") > 128:
            raise CheckError("fixture cannot be represented by current B1 hosting/IDs")
        create("P_X", "Collateral.Right:UseRight", {
            "bundle": {"bundleId": b["bundle_id"], "securityId": b["security_id"],
                       "quantity": b["quantity"], "referenceValue": b["reference_value"]},
            "rightId": b["bundle_id"] + ":right:0", "holder": parties[b["initial_holder"]],
            "cosigner": parties["BANK_B"],
            "createdVersion": "0", "status": "AVAILABLE", "allowedCommands": ["CONFIRM_REPO"],
            "predecessorRightId": None, "sourceRecordId": None,
        })
    for a in fixture["agreements"]:
        if a["type"] == "REPO":
            if (a["seller"], a["buyer"]) != ("BANK_A", "BANK_B"):
                raise CheckError("fixture does not match B1 Repo hosting")
            create("P_X", "Collateral.Agreement:RepoAgreement", {
                "agreementId": a["agreement_id"], "bundleId": a["bundle_id"],
                "seller": parties[a["seller"]], "buyer": parties[a["buyer"]],
                "reuseAllowed": a["reuse_allowed"],
            })
        else:
            if (a["lender"], a["borrower"]) != ("BANK_B", "BANK_C"):
                raise CheckError("fixture does not match B1 Loan hosting")
            c = a["collateral_condition"]
            create("P_Y", "Collateral.Agreement:LoanAgreement", {
                "agreementId": a["agreement_id"], "bundleId": a["bundle_id"],
                "lender": parties[a["lender"]], "borrower": parties[a["borrower"]],
                "ccpUseAllowed": a["ccp_use_allowed"], "collateral": {
                    "collateralId": c["collateral_id"], "conditionsMet": c["conditions_met"],
                    "provided": c["provided"],
                },
            })
    if stage == "xyz":
        for margin in fixture["margin_calls"]:
            create("P_Z", "Collateral.Margin:MarginCall", {
                "marginCallId": margin["margin_call_id"], "bundleId": margin["bundle_id"],
                "provider": parties[margin["provider"]], "ccp": parties[margin["ccp"]],
                "wholeBundleAccepted": margin["whole_bundle_accepted"],
                "eligibilityPassed": margin["eligibility_passed"], "status": margin["status"],
            })
    return mapped


def call(client, url, body):
    response = client.post(url, json=body)
    if response.status_code != 200:
        raise CheckError("Ledger API refused preparation/query")
    return response.json()


def existing_run(client, url, parties, run_id):
    query = {"eventFormat": {"filtersByParty": {
        party: {"cumulative": [{"identifierFilter": {"WildcardFilter": {
            "value": {"includeCreatedEventBlob": False}}}}]} for party in parties
    }, "verbose": False}, "maxPageSize": 200}
    tokens = set()
    while True:
        page = call(client, url + "/v2/state/active-contracts-page", query)
        for entry in page["activeContracts"]:
            event = entry["contractEntry"]["JsActiveContract"]["createdEvent"]
            if event.get("packageName") == "collateral" and event["createArgument"].get("runId") == run_id:
                return True
        token = page.get("nextPageToken")
        if not token:
            return False
        if token in tokens:
            raise CheckError("Ledger API returned a repeated ACS page")
        tokens.add(token)
        query["pageToken"] = token


def prepare(run_id, fixture, state, client, stage="xy"):
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", run_id) or set(run_id) == {"."}:
        raise CheckError("invalid run_id")
    ids = read_json(state / "ids.json")
    if stage == "xyz" and ids.get("topology") != "b1-xyz":
        raise CheckError("XYZ preparation requires fresh b1-xyz state from PR #9")
    parties = ids["parties"]
    mapped = commands(run_id, fixture, parties, stage)
    hosted = {"P_X": [parties["BANK_A"], parties["BANK_B"]],
              "P_Y": [parties["BANK_B"], parties["BANK_C"]]}
    if stage == "xyz":
        hosted["P_Z"] = [parties["BANK_C"], parties["CCP"]]
    urls = {p: ids["json_api"][p].rstrip("/") for p in mapped}
    for p, url in urls.items():
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1", "::1")
                or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path):
            raise CheckError("B1 preparation requires local unauthenticated JSON Ledger API URLs")
        if existing_run(client, url, hosted[p], run_id):
            raise CheckError("RUN_ALREADY_EXISTS")
    # LF 2.1 has no contract keys: persist an exclusive local claim before submitting.
    # A failed/uncertain submission keeps its claim; never reuse or clean up that run.
    directory = state / "conformance-preparations"
    directory.mkdir(exist_ok=True)
    marker = directory / (run_id + ".json")
    try:
        with marker.open("x", encoding="utf-8") as target:
            json.dump({"run_id": run_id, "status": "CLAIMED",
                       "fixture_sha256": hashlib.sha256(json.dumps(fixture, sort_keys=True).encode()).hexdigest()}, target)
            target.flush()
            os.fsync(target.fileno())
    except FileExistsError:
        raise CheckError("RUN_ALREADY_EXISTS") from None
    submitted = []
    try:
        for p, creates in mapped.items():
            reply = call(client, urls[p] + "/v2/commands/submit-and-wait-for-transaction", {
                "commands": {"commands": creates, "commandId": f"prepare-{run_id}-{p}",
                             "userId": "conformance-prepare", "actAs": hosted[p]},
            })
            if not isinstance(reply.get("transaction"), dict):
                raise CheckError("Ledger API did not return the preparation transaction")
            submitted.append(p)
        # Read-only runs emit no business events; initialize their real log without inventing any.
        evidence = Path(os.environ.get("CANTON_EVIDENCE_DIR") or ROOT / "implementations/canton/logs")
        log = evidence / run_id / "events.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8"):
            pass
    except Exception:
        print(json.dumps({"run_id": run_id, "acknowledged_participants": submitted,
                          "status": "FAILED_OR_UNKNOWN", "claim_preserved": True}), file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", nargs="?", choices=("xy", "xyz"), default="xy")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--fixture", required=True, type=Path)
    args = parser.parse_args()
    try:
        state = Path(os.environ["CANTON_STATE_DIR"])
        with httpx.Client(timeout=30, trust_env=False) as client:
            prepare(args.run_id, read_json(args.fixture), state, client, args.stage)
    except Exception as error:
        reason = str(error) if isinstance(error, CheckError) else type(error).__name__
        print(json.dumps({"run_id": args.run_id, "status": "FAILED", "reason": reason}))
        return 1
    print(json.dumps({"run_id": args.run_id, "status": "PREPARED"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
