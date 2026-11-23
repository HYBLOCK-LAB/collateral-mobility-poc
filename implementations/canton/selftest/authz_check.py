#!/usr/bin/env python3
"""Check of the UseRight signatories and visibility on the running Canton (README §3).

    . scripts/env.sh && .venv/bin/python -I selftest/authz_check.py

Three fresh run_ids, each prepared like selftest/xyz_selftest.py (README §3):
right 0 and the agreements on P_X and P_Y, the margin call on P_Z.

r1, the X→Y path:
  package     every contract of the run comes from the package named
              collateral, with the main package ID of the built DAR
  S0 view     which contracts each participant (its hosted parties) and each
              party alone sees, against the signatories README §3 lists; the
              signatories, holder and cosigner of every right
  solo S0     ledger submissions that must fail, each for the expected reason,
              leaving the run's contracts as they were: BANK_A alone creates
              right 0, creates a right it cosigns itself, archives right 0,
              consumes right 0 for BANK_B; BANK_A and the next holder the same
  repo        CONFIRM_REPO through the X adapter: COMMITTED
  S1 view     as at S0: right 1 held by BANK_B, cosigned by BANK_A
  solo S1     BANK_B alone creates, archives and consumes right 1 on P_Y
  loan        CONFIRM_LOAN through the Y adapter: COMMITTED
  S2 view     as at S0: right 2 held by BANK_C, cosigned by BANK_B; BANK_A
              sees right 1 consumed but none of the loan's contracts
  loan tree   the loan transaction as BANK_A sees it on P_X: the Consume of
              right 1 with nextHolder BANK_C and the consumed copy, nothing
              else; BANK_B sees the whole transaction
  solo S2     on P_Z: BANK_C alone locks right 2, locks it naming itself as
              the locker, creates a margin call, creates an allocation
  margin      ALLOCATE_MARGIN through the Z adapter: COMMITTED
  S3 view     as at S0: right 2 LOCKED with the same signatories, the margin
              call SATISFIED, the allocation signed by BANK_C and the CCP
r2, a right cosigned by the borrower:
  forged      P_Y actAs [BANK_B, BANK_C] creates right 1 as the Repo would,
              but cosigned by BANK_C: the joint submission the PoC trusts,
              which succeeds
  ledger      ConfirmLoan with it, actAs [BANK_B]: fails with "the right is
              cosigned by the borrower"
  adapter     CONFIRM_LOAN through the Y adapter: not committed; the adapter
              answers 503 TEMPORARILY_UNAVAILABLE (README §3)
r3, a right cosigned by the CCP:
  forged      P_Z actAs [BANK_C, CCP] creates right 2 as the loan would, but
              cosigned by the CCP: the joint submission the PoC trusts, which
              succeeds
  ledger      AllocateMargin with it, actAs [BANK_C]: fails with "the right is
              cosigned by the CCP"
  adapter     ALLOCATE_MARGIN through the Z adapter: REJECTED with
              PRECONDITION_NOT_MET (no loan created the right), nothing changes

The report goes to implementations/canton/logs/authz-check/<batch>.json,
without credentials. Exit status 0 when every expectation held, 1 otherwise.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

SELFTEST_DIR = Path(__file__).resolve().parent
IMPL_DIR = SELFTEST_DIR.parent
sys.path.insert(0, str(SELFTEST_DIR))
from xy_selftest import STATE_WAIT, Failed, http  # noqa: E402
from xyz_selftest import MARGIN_CALL, XYZSelftest  # noqa: E402

PACKAGE = "collateral"
PKG_DIR = IMPL_DIR / "daml/collateral"
RIGHT = "Collateral.Right:UseRight"
LOAN_AGREEMENT = "Collateral.Agreement:LoanAgreement"
ALLOCATION = "Collateral.Margin:Allocation"
WILDCARD = [
    {
        "identifierFilter": {
            "WildcardFilter": {"value": {"includeCreatedEventBlob": False}}
        }
    }
]
# Views of the ledger: a participant reads for the parties it hosts
# (README §3 Party table), a party alone on a participant hosting it.
VIEWS = {
    "P_X": ("P_X", ("BANK_A", "BANK_B")),
    "P_Y": ("P_Y", ("BANK_B", "BANK_C")),
    "BANK_A@P_X": ("P_X", ("BANK_A",)),
    "BANK_B@P_X": ("P_X", ("BANK_B",)),
    "BANK_B@P_Y": ("P_Y", ("BANK_B",)),
    "BANK_C@P_Y": ("P_Y", ("BANK_C",)),
    "P_Z": ("P_Z", ("BANK_C", "CCP")),
    "BANK_C@P_Z": ("P_Z", ("BANK_C",)),
    "CCP@P_Z": ("P_Z", ("CCP",)),
}
# Signatories by contract (README §3); rights are (holder, cosigner).
STAKEHOLDERS = {
    "UseRight right:0": ("BANK_A", "BANK_B"),
    "UseRight right:1": ("BANK_B", "BANK_A"),
    "UseRight right:2": ("BANK_C", "BANK_B"),
    "RepoAgreement repo-agreement-001": ("BANK_A", "BANK_B"),
    "LoanAgreement loan-agreement-001": ("BANK_B", "BANK_C"),
    "TradeContract repo-001": ("BANK_A", "BANK_B"),
    "Obligation repo-001:repurchase-payment": ("BANK_A", "BANK_B"),
    "Obligation repo-001:securities-return": ("BANK_B", "BANK_A"),
    "TradeContract loan-001": ("BANK_B", "BANK_C"),
    "Obligation loan-001:securities-return": ("BANK_C", "BANK_B"),
    "Obligation loan-001:collateral-return": ("BANK_B", "BANK_C"),
    "MarginCall margin-001": ("BANK_C", "CCP"),
    "Allocation allocation-001": ("BANK_C", "CCP"),
}
S0 = {
    "UseRight right:0 AVAILABLE",
    "RepoAgreement repo-agreement-001",
    "LoanAgreement loan-agreement-001",
    "MarginCall margin-001 OPEN",
}
S1 = (S0 - {"UseRight right:0 AVAILABLE"}) | {
    "UseRight right:0 CONSUMED",
    "UseRight right:1 AVAILABLE",
    "TradeContract repo-001",
    "Obligation repo-001:repurchase-payment",
    "Obligation repo-001:securities-return",
}
S2 = (S1 - {"UseRight right:1 AVAILABLE"}) | {
    "UseRight right:1 CONSUMED",
    "UseRight right:2 AVAILABLE",
    "TradeContract loan-001",
    "Obligation loan-001:securities-return",
    "Obligation loan-001:collateral-return",
}
S3 = (S2 - {"UseRight right:2 AVAILABLE", "MarginCall margin-001 OPEN"}) | {
    "UseRight right:2 LOCKED",
    "MarginCall margin-001 SATISFIED",
    "Allocation allocation-001",
}
# Ledger refusals by kind: the Canton error code and text the cause must
# contain, case ignored (Canton 3.7.3 with Daml 3.7.1: an ensure clause or an
# assertMsg that fails is a DAML_FAILURE naming the exception).
AUTHORIZATION = ("DAML_AUTHORIZATION_ERROR", "requires authorizers")
ENSURE = ("DAML_FAILURE", "PreconditionFailed")


def failure(text: str) -> tuple[str | None, str]:
    """An assertMsg of the choice."""
    return ("DAML_FAILURE", "AssertionFailed:AssertionFailed (error category 9): " + text)


def label(event: dict) -> str | None:
    """A contract of the run as `<template> <business id>`, rights with status."""
    entity = event["templateId"].split(":")[-1]
    a = event["createArgument"]
    if entity == "UseRight":
        return f"UseRight {':'.join(a['rightId'].split(':')[-2:])} {a['status']}"
    if entity in ("RepoAgreement", "LoanAgreement"):
        return f"{entity} {a['agreementId']}"
    if entity == "TradeContract":
        return f"TradeContract {a['tradeId']}"
    if entity == "Obligation":
        return f"Obligation {a['obligationId']}"
    if entity == "MarginCall":
        return f"MarginCall {a['marginCallId']} {a['status']}"
    if entity == "Allocation":
        return f"Allocation {a['allocationId']}"
    return None


def stakeholders_of(name: str) -> tuple[str, ...]:
    """Expected signatories of a labelled contract (the status does not matter);
    a contract outside the table fails the check."""
    key = " ".join(name.split()[:2])
    if key not in STAKEHOLDERS:
        raise Failed(f"unexpected contract {name}")
    return STAKEHOLDERS[key]


def dar_package() -> tuple[str, str]:
    """Version (daml.yaml) and main package ID (the DAR manifest) of the build."""
    version = re.search(r"^version: *(\S+)", (PKG_DIR / "daml.yaml").read_text(), re.M)
    if version is None:
        raise SystemExit("daml.yaml has no version")
    dar = PKG_DIR / f".daml/dist/{PACKAGE}-{version[1]}.dar"
    if not dar.is_file():
        raise SystemExit(f"{dar} missing (scripts/adapter.sh start builds it)")
    with zipfile.ZipFile(dar) as archive:
        manifest = archive.read("META-INF/MANIFEST.MF").decode()
    unfolded = re.sub(r"\r?\n ", "", manifest)  # continuation lines
    main = re.search(r"^Main-Dalf: *\S*-([0-9a-f]{64})\.dalf", unfolded, re.M)
    if main is None:
        raise SystemExit(f"{dar}: no main package in the manifest")
    return version[1], main[1]


class AuthzCheck(XYZSelftest):
    def __init__(self, run_id: str, package_id: str):
        super().__init__()
        self.run_id = run_id
        self.package_id = package_id
        self.names = {party: name for name, party in self.parties.items()}
        self.attempt = 0
        self.right1_cid: str | None = None
        self.right2_cid: str | None = None

    # --- ledger reads ----------------------------------------------------

    def acs(self, participant: str, parties: tuple[str, ...]) -> dict[str, dict]:
        """The run's active contracts the parties see on the participant."""
        query: dict = {
            "eventFormat": {
                "filtersByParty": {
                    self.parties[n]: {"cumulative": WILDCARD} for n in parties
                },
                "verbose": False,
            },
            "maxPageSize": 500,
        }
        found: dict[str, dict] = {}
        while True:
            status, page = http(
                "POST",
                self.ledger[participant] + "/v2/state/active-contracts-page",
                body=query,
            )
            if status != 200 or not isinstance(page, dict):
                raise Failed(f"ACS {participant}: HTTP {status} {str(page)[:400]}")
            for entry in page.get("activeContracts", []):
                event = entry["contractEntry"]["JsActiveContract"]["createdEvent"]
                if event["createArgument"].get("runId") == self.run_id:
                    found[event["contractId"]] = event
            token = page.get("nextPageToken")
            if not token:
                return found
            query["pageToken"] = token

    def views(self) -> dict[str, dict[str, dict]]:
        return {name: self.acs(p, parties) for name, (p, parties) in VIEWS.items()}

    def contract_ids(self) -> dict[str, list[str]]:
        """The run's contract IDs on each participant, to show nothing changed."""
        return {p: sorted(self.acs(p, VIEWS[p][1])) for p in ("P_X", "P_Y", "P_Z")}

    def find(self, view: str, name: str) -> str:
        found = [cid for cid, e in self.acs(*VIEWS[view]).items() if label(e) == name]
        if len(found) != 1:
            raise Failed(f"{view}: {len(found)} contracts {name}")
        return found[0]

    # --- ledger writes ---------------------------------------------------

    def ledger_submit(self, participant: str, act_as: tuple[str, ...], command: dict):
        """One command as the parties, under its own command ID. Returns the
        HTTP status and the reply (the transaction or the error)."""
        self.attempt += 1
        body = {
            "commands": {
                "commands": [command],
                "commandId": f"authz-{self.run_id}-{self.attempt}",
                "userId": "authz-check",
                "actAs": [self.parties[n] for n in act_as],
            }
        }
        return http(
            "POST",
            self.ledger[participant] + "/v2/commands/submit-and-wait-for-transaction",
            body=body,
        )

    @staticmethod
    def create(arguments: dict, template: str = RIGHT) -> dict:
        return {
            "CreateCommand": {
                "templateId": f"#{PACKAGE}:{template}",
                "createArguments": arguments,
            }
        }

    @staticmethod
    def exercise(cid: str, choice: str, argument: dict, template: str = RIGHT) -> dict:
        return {
            "ExerciseCommand": {
                "templateId": f"#{PACKAGE}:{template}",
                "contractId": cid,
                "choice": choice,
                "choiceArgument": argument,
            }
        }

    def refused(self, participant, act_as, command, expected: tuple[str | None, str]):
        """The submission fails with the expected code and text, and the run's
        contracts stay as they were."""
        before = self.contract_ids()
        status, reply = self.ledger_submit(participant, act_as, command)
        code = reply.get("code") if isinstance(reply, dict) else None
        cause = (reply.get("cause") or "") if isinstance(reply, dict) else str(reply)
        if status == 200:
            raise Failed(f"committed, expected a refusal {expected}")
        if (expected[0] is not None and code != expected[0]) or (
            expected[1].lower() not in cause.lower()
        ):
            raise Failed(f"HTTP {status} {code}: {cause[:400]}; expected {expected}")
        after = self.contract_ids()
        if after != before:
            raise Failed(f"the run's contracts changed: {before} -> {after}")
        return {"http": status, "code": code, "cause": cause[:400]}

    def right_arguments(self, version: int, holder: str, cosigner: str) -> dict:
        """A right of the run as the Repo creates it at version 1 and the loan
        at version 2 (README §3), or the initial right at version 0."""
        bundle = self.fixture["bundles"][0]
        bundle_id = bundle["bundle_id"]
        return {
            "runId": self.run_id,
            "bundle": {
                "bundleId": bundle_id,
                "securityId": bundle["security_id"],
                "quantity": dict(bundle["quantity"]),
                "referenceValue": dict(bundle["reference_value"]),
            },
            "rightId": f"{bundle_id}:right:{version}",
            "holder": self.parties[holder],
            "cosigner": self.parties[cosigner],
            "createdVersion": str(version),
            "status": "AVAILABLE",
            "allowedCommands": [
                ("CONFIRM_REPO", "CONFIRM_LOAN", "ALLOCATE_MARGIN")[version]
            ],
            "predecessorRightId": f"{bundle_id}:right:{version - 1}"
            if version
            else None,
            "sourceRecordId": (
                None,
                self.repo["payload"]["repo_id"],
                self.loan["payload"]["loan_id"],
            )[version],
        }

    def margin_call_arguments(self) -> dict:
        call = self.fixture["margin_calls"][0]
        return {
            "runId": self.run_id,
            "marginCallId": call["margin_call_id"],
            "bundleId": call["bundle_id"],
            "provider": self.parties[call["provider"]],
            "ccp": self.parties[call["ccp"]],
            "wholeBundleAccepted": call["whole_bundle_accepted"],
            "eligibilityPassed": call["eligibility_passed"],
            "status": call["status"],
        }

    def allocation_arguments(self) -> dict:
        payload = self.margin["payload"]
        return {
            "runId": self.run_id,
            "allocationId": payload["allocation_id"],
            "marginCallId": payload["margin_call_id"],
            "bundleId": self.margin["bundle_id"],
            "provider": self.parties["BANK_C"],
            "ccp": self.parties["CCP"],
            "rightId": self.margin["bundle_id"] + ":right:2",
            "requestId": self.margin["request_id"],
            "actor": self.parties["BANK_C"],
            "versionBefore": "2",
            "versionAfter": "3",
        }

    # --- checks ----------------------------------------------------------

    def until(self, fn):
        """fn() once the participants show it, within STATE_WAIT."""
        deadline = time.monotonic() + STATE_WAIT
        while True:
            try:
                return fn()
            except Failed:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)

    def package(self):
        events = [e for view in self.views().values() for e in view.values()]
        if not events:
            raise Failed("no contracts of the run")
        names = {e.get("packageName") for e in events}
        ids = {e["templateId"].split(":", 1)[0] for e in events}
        if names != {PACKAGE} or ids != {self.package_id}:
            raise Failed(
                f"packages {names} {ids}, expected {PACKAGE} {self.package_id}"
            )
        return {"package_name": PACKAGE, "package_id": self.package_id}

    def view(self, expected: set[str]):
        """Each view holds exactly the contracts one of its parties signs, and
        every right has the expected signatories, holder and cosigner."""
        views = self.views()
        table, problems = {}, []
        for name, (_, parties) in VIEWS.items():
            got = sorted(filter(None, (label(e) for e in views[name].values())))
            table[name] = got
            want = sorted(c for c in expected if set(stakeholders_of(c)) & set(parties))
            if got != want:
                problems.append(f"{name}: {got}, expected {want}")
        signed = {}
        for view in views.values():
            for event in view.values():
                name = label(event)
                if name is None:
                    continue
                parties = tuple(self.names[p] for p in event["signatories"])
                want = stakeholders_of(name)
                if set(parties) != set(want) or event.get("observers"):
                    problems.append(
                        f"{name}: signatories {parties} observers {event.get('observers')}"
                    )
                if name.startswith("UseRight"):
                    a = event["createArgument"]
                    if (self.names[a["holder"]], self.names[a["cosigner"]]) != want:
                        problems.append(
                            f"{name}: holder {a['holder']} cosigner {a['cosigner']}"
                        )
                signed[name] = sorted(parties)
        if problems:
            raise Failed("; ".join(problems))
        return {"views": table, "signatories": dict(sorted(signed.items()))}

    def loan_tree(self):
        """The loan transaction on P_X: BANK_A sees only the Consume of right 1
        and its consumed copy; BANK_B sees the trade too."""
        acs = self.acs(*VIEWS["BANK_A@P_X"])
        consumed = [e for e in acs.values() if label(e) == "UseRight right:1 CONSUMED"]
        if len(consumed) != 1:
            raise Failed(f"BANK_A sees {len(consumed)} consumed right 1")
        offset = consumed[0]["offset"]
        seen = {}
        for name in ("BANK_A@P_X", "BANK_B@P_X"):
            participant, parties = VIEWS[name]
            status, reply = http(
                "POST",
                self.ledger[participant] + "/v2/updates/update-by-offset",
                body={
                    "offset": offset,
                    "updateFormat": {
                        "includeTransactions": {
                            "eventFormat": {
                                "filtersByParty": {
                                    self.parties[n]: {"cumulative": WILDCARD}
                                    for n in parties
                                },
                                "verbose": True,
                            },
                            "transactionShape": "TRANSACTION_SHAPE_LEDGER_EFFECTS",
                        }
                    },
                },
            )
            if status != 200 or not isinstance(reply, dict):
                raise Failed(
                    f"update {offset} as {name}: HTTP {status} {str(reply)[:400]}"
                )
            events = reply["update"]["Transaction"]["value"]["events"]
            seen[name] = [self.event_summary(e) for e in events]
        a = seen["BANK_A@P_X"]
        want_a = [
            {
                "exercised": "UseRight.Consume",
                "contract": self.right1(),
                "nextHolder": "BANK_C",
                "acting": ["BANK_B", "BANK_C"],
            },
            {"created": "UseRight right:1 CONSUMED"},
        ]
        if a != want_a:
            raise Failed(f"BANK_A sees {a}, expected {want_a}")
        b = seen["BANK_B@P_X"]
        if {"created": "TradeContract loan-001"} not in b:
            raise Failed(f"BANK_B does not see the loan trade: {b}")
        return {"offset_p_x": offset, "events": seen}

    def event_summary(self, event: dict) -> dict:
        kind, body = next(iter(event.items()))
        if kind == "CreatedEvent":
            return {"created": label(body)}
        if kind == "ExercisedEvent":
            entity = body["templateId"].split(":")[-1]
            out = {
                "exercised": f"{entity}.{body['choice']}",
                "contract": body["contractId"],
            }
            argument = body.get("choiceArgument")
            if entity == "UseRight" and isinstance(argument, dict):
                out["nextHolder"] = self.names.get(argument.get("nextHolder"), argument)
            out["acting"] = sorted(self.names[p] for p in body.get("actingParties", []))
            return out
        return {kind: body.get("contractId")}

    def confirm_ok(self, platform, role, actor, command, test_id):
        body = self.confirm(platform, role, actor, command, test_id)
        return {"outcome": body["outcome"], "request_id": command["request_id"]}

    # --- runs ------------------------------------------------------------

    def right0(self) -> str:
        return self.find("BANK_A@P_X", "UseRight right:0 AVAILABLE")

    def r1_steps(self):
        bank_b = self.parties["BANK_B"]
        bank_c = self.parties["BANK_C"]
        bank_a = self.parties["BANK_A"]
        ccp = self.parties["CCP"]
        return [
            ("prepare", self.prepare),
            ("package", lambda: self.until(self.package)),
            ("s0-view", lambda: self.until(lambda: self.view(S0))),
            (
                "s0-bank-a-creates-right-0-alone",
                lambda: self.refused(
                    "P_X",
                    ("BANK_A",),
                    self.create(self.right_arguments(0, "BANK_A", "BANK_B")),
                    AUTHORIZATION,
                ),
            ),
            (
                "s0-holder-cosigns-own-right",
                lambda: self.refused(
                    "P_X",
                    ("BANK_A",),
                    self.create(self.right_arguments(0, "BANK_A", "BANK_A")),
                    ENSURE,
                ),
            ),
            (
                "s0-bank-a-archives-right-0-alone",
                lambda: self.refused(
                    "P_X",
                    ("BANK_A",),
                    self.exercise(self.right0(), "Archive", {}),
                    AUTHORIZATION,
                ),
            ),
            (
                "s0-bank-a-consumes-right-0-alone",
                lambda: self.refused(
                    "P_X",
                    ("BANK_A",),
                    self.exercise(self.right0(), "Consume", {"nextHolder": bank_b}),
                    AUTHORIZATION,
                ),
            ),
            (
                "s0-next-holder-is-holder",
                lambda: self.refused(
                    "P_X",
                    ("BANK_A",),
                    self.exercise(self.right0(), "Consume", {"nextHolder": bank_a}),
                    failure("the next holder is the holder"),
                ),
            ),
            (
                "confirm-repo-X",
                lambda: self.confirm_ok(
                    "X", "bank-a", "BANK_A", self.repo, "AUTHZ-REPO"
                ),
            ),
            ("s1-view", lambda: self.until(lambda: self.view(S1))),
            ("s1-right-1", self.find_right1),
            (
                "s1-bank-b-creates-right-1-alone",
                lambda: self.refused(
                    "P_Y",
                    ("BANK_B",),
                    self.create(self.right_arguments(1, "BANK_B", "BANK_A")),
                    AUTHORIZATION,
                ),
            ),
            (
                "s1-bank-b-archives-right-1-alone",
                lambda: self.refused(
                    "P_Y",
                    ("BANK_B",),
                    self.exercise(self.right1(), "Archive", {}),
                    AUTHORIZATION,
                ),
            ),
            (
                "s1-bank-b-consumes-right-1-alone",
                lambda: self.refused(
                    "P_Y",
                    ("BANK_B",),
                    self.exercise(self.right1(), "Consume", {"nextHolder": bank_c}),
                    AUTHORIZATION,
                ),
            ),
            (
                "confirm-loan-Y",
                lambda: self.confirm_ok(
                    "Y", "bank-b", "BANK_B", self.loan, "AUTHZ-LOAN"
                ),
            ),
            ("s2-view", lambda: self.until(lambda: self.view(S2))),
            ("s2-loan-transaction-tree", lambda: self.until(self.loan_tree)),
            ("s2-right-2", self.find_right2),
            (
                "s2-bank-c-locks-right-2-alone",
                lambda: self.refused(
                    "P_Z",
                    ("BANK_C",),
                    self.exercise(self.right2(), "Lock", {"locker": ccp}),
                    AUTHORIZATION,
                ),
            ),
            (
                "s2-locker-is-holder",
                lambda: self.refused(
                    "P_Z",
                    ("BANK_C",),
                    self.exercise(self.right2(), "Lock", {"locker": bank_c}),
                    failure("the locker is the holder"),
                ),
            ),
            (
                "s2-bank-c-creates-margin-call-alone",
                lambda: self.refused(
                    "P_Z",
                    ("BANK_C",),
                    self.create(self.margin_call_arguments(), MARGIN_CALL),
                    AUTHORIZATION,
                ),
            ),
            (
                "s2-bank-c-creates-allocation-alone",
                lambda: self.refused(
                    "P_Z",
                    ("BANK_C",),
                    self.create(self.allocation_arguments(), ALLOCATION),
                    AUTHORIZATION,
                ),
            ),
            (
                "allocate-margin-Z",
                lambda: self.confirm_ok(
                    "Z", "bank-c", "BANK_C", self.margin, "AUTHZ-MARGIN"
                ),
            ),
            ("s3-view", lambda: self.until(lambda: self.view(S3))),
        ]

    def right1(self) -> str:
        if self.right1_cid is None:
            raise Failed("right 1 not found yet")
        return self.right1_cid

    def right2(self) -> str:
        if self.right2_cid is None:
            raise Failed("right 2 not found yet")
        return self.right2_cid

    def find_right2(self):
        self.right2_cid = self.until(
            lambda: self.find("BANK_C@P_Z", "UseRight right:2 AVAILABLE")
        )
        return {"contract_id": self.right2_cid}

    def find_right1(self):
        self.right1_cid = self.until(
            lambda: self.find("BANK_B@P_Y", "UseRight right:1 AVAILABLE")
        )
        return {"contract_id": self.right1_cid}

    def r2_steps(self):
        forged = {}

        def create_forged():
            status, reply = self.ledger_submit(
                "P_Y",
                ("BANK_B", "BANK_C"),
                self.create(self.right_arguments(1, "BANK_B", "BANK_C")),
            )
            if status != 200:
                raise Failed(f"joint create: HTTP {status} {str(reply)[:400]}")
            forged["cid"] = self.until(
                lambda: self.find("BANK_B@P_Y", "UseRight right:1 AVAILABLE")
            )
            return {
                "contract_id": forged["cid"],
                "holder": "BANK_B",
                "cosigner": "BANK_C",
            }

        def ledger_confirm():
            loan = self.until(
                lambda: self.find("BANK_B@P_Y", "LoanAgreement loan-agreement-001")
            )
            argument = {
                "requestId": f"authz-{self.run_id}-ledger",
                "loanId": self.loan["payload"]["loan_id"],
                "expectedVersion": "1",
                "rightCid": forged["cid"],
            }
            return self.refused(
                "P_Y",
                ("BANK_B",),
                self.exercise(loan, "ConfirmLoan", argument, LOAN_AGREEMENT),
                failure("the right is cosigned by the borrower"),
            )

        def adapter_confirm():
            before = self.contract_ids()
            status, body = self.adapter(
                "POST",
                "Y",
                "/commands",
                "bank-b",
                command=self.loan,
                test_id="AUTHZ-FORGED",
            )
            code = body.get("error", {}).get("code") if isinstance(body, dict) else None
            if status != 503 or code != "TEMPORARILY_UNAVAILABLE":
                raise Failed(
                    f"HTTP {status} {body}; expected 503 TEMPORARILY_UNAVAILABLE"
                )
            after = self.contract_ids()
            if after != before:
                raise Failed(f"the run's contracts changed: {before} -> {after}")
            return {"http": status, "body": body}

        return [
            ("prepare", self.prepare),
            ("forged-right-1-created-jointly", create_forged),
            ("ledger-confirm-loan-refused", ledger_confirm),
            ("adapter-confirm-loan-not-committed", adapter_confirm),
        ]

    def r3_steps(self):
        forged = {}

        def create_forged():
            status, reply = self.ledger_submit(
                "P_Z",
                ("BANK_C", "CCP"),
                self.create(self.right_arguments(2, "BANK_C", "CCP")),
            )
            if status != 200:
                raise Failed(f"joint create: HTTP {status} {str(reply)[:400]}")
            forged["cid"] = self.until(
                lambda: self.find("BANK_C@P_Z", "UseRight right:2 AVAILABLE")
            )
            return {"contract_id": forged["cid"], "holder": "BANK_C", "cosigner": "CCP"}

        def ledger_allocate():
            call = self.until(
                lambda: self.find("BANK_C@P_Z", "MarginCall margin-001 OPEN")
            )
            argument = {
                "requestId": f"authz-{self.run_id}-ledger",
                "allocationId": self.margin["payload"]["allocation_id"],
                "expectedVersion": "2",
                "rightCid": forged["cid"],
            }
            return self.refused(
                "P_Z",
                ("BANK_C",),
                self.exercise(call, "AllocateMargin", argument, MARGIN_CALL),
                failure("the right is cosigned by the CCP"),
            )

        def adapter_allocate():
            before = self.contract_ids()
            status, body = self.adapter(
                "POST",
                "Z",
                "/commands",
                "bank-c",
                command=self.margin,
                test_id="AUTHZ-FORGED-Z",
            )
            outcome = body.get("outcome") if isinstance(body, dict) else None
            code = (body.get("error") or {}).get("code") if isinstance(body, dict) else None
            if (status, outcome, code) != (200, "REJECTED", "PRECONDITION_NOT_MET"):
                raise Failed(f"HTTP {status} {body}; expected REJECTED PRECONDITION_NOT_MET")
            after = self.contract_ids()
            if after != before:
                raise Failed(f"the run's contracts changed: {before} -> {after}")
            return {"http": status, "body": body}

        return [
            ("prepare", self.prepare),
            ("forged-right-2-created-jointly", create_forged),
            ("ledger-allocate-margin-refused", ledger_allocate),
            ("adapter-allocate-margin-not-committed", adapter_allocate),
        ]

    def execute(self, steps) -> dict:
        for name, fn in steps:
            if not self.step(name, fn):
                break  # later steps depend on the earlier state
        return {
            "run_id": self.run_id,
            "checks": self.results,
            "passed": sum(r["ok"] for r in self.results),
            "planned": len(steps),
        }


def main() -> int:
    version, package_id = dar_package()
    batch = secrets.token_hex(4)
    runs = []
    for suffix, steps in (
        ("r1", AuthzCheck.r1_steps),
        ("r2", AuthzCheck.r2_steps),
        ("r3", AuthzCheck.r3_steps),
    ):
        check = AuthzCheck(f"authz-{batch}-{suffix}", package_id)
        runs.append(check.execute(steps(check)))
    ids = json.loads((Path(os.environ["CANTON_STATE_DIR"]) / "ids.json").read_text())
    report = {
        "batch": batch,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "package": {"name": PACKAGE, "version": version, "main_package_id": package_id},
        "state_dir": os.environ["CANTON_STATE_DIR"],
        "synchronizers": ids["synchronizers"],
        "participants": ids["participants"],
        "runs": runs,
        "passed": sum(r["passed"] for r in runs),
        "planned": sum(r["planned"] for r in runs),
    }
    out = check.evidence / "authz-check" / f"{batch}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    for run in runs:
        for r in run["checks"]:
            print(
                f"{'ok  ' if r['ok'] else 'FAIL'} {run['run_id']:<20} {r['check']:<36} {r['ms']:>8} ms"
                + (f"  {r['problem']}" if not r["ok"] else "")
            )
    print(f"{report['passed']}/{report['planned']} passed; report {out}")
    return 0 if report["passed"] == report["planned"] else 1


if __name__ == "__main__":
    sys.exit(main())
