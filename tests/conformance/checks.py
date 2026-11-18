"""Local schema and observable initial-state checks; no business state writes."""

import json
import copy
import re
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource

ROOT = Path(__file__).resolve().parents[2]
ROLES = {"bank-a", "bank-b", "bank-c", "experiment-observer"}


class CheckError(ValueError):
    pass


def parse_json(text):
    def reject_constant(value):
        raise CheckError("non-finite JSON")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CheckError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=unique, parse_constant=reject_constant)


def read_json(path):
    return parse_json(Path(path).read_text(encoding="utf-8"))


def offline_only(uri):
    raise NoSuchResource(ref=uri)


SCHEMAS = {p.name: read_json(p) for p in (ROOT / "shared/schemas").glob("*.json")}
REGISTRY = Registry(retrieve=offline_only).with_resources(
    (s["$id"], Resource.from_contents(s)) for s in SCHEMAS.values())


def validate(value, schema):
    validator = Draft202012Validator(SCHEMAS[schema], registry=REGISTRY,
                                    format_checker=FormatChecker())
    error = next(validator.iter_errors(value), None)
    if error:
        # Do not interpolate validation messages: they can contain supplied secrets.
        raise CheckError(f"{schema}: invalid field at /{'/'.join(map(str, error.absolute_path))}")


def validate_config(config):
    keys = {"model", "platforms", "auth_env", "prepare_command", "evidence_dir"}
    if not isinstance(config, dict) or set(config) != keys:
        raise CheckError("config fields do not match approved format")
    if config["model"] not in ("A", "B1", "B2"):
        raise CheckError("invalid model")
    addresses = config["platforms"]
    if not isinstance(addresses, dict) or set(addresses) != {"X", "Y", "Z"}:
        raise CheckError("platforms must contain X/Y/Z")
    for address in addresses.values():
        if not isinstance(address, str):
            raise CheckError("platform URL must be a string")
        url = urlsplit(address)
        if (url.scheme not in ("http", "https") or not url.hostname or url.username
                or url.password or url.query or url.fragment):
            raise CheckError("invalid platform URL; credentials/query/fragment prohibited")
        try:
            url.port
        except ValueError:
            raise CheckError("invalid platform port") from None
    auth = config["auth_env"]
    if (not isinstance(auth, dict) or set(auth) != ROLES
            or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v)
                   for v in auth.values()) or len(set(auth.values())) != len(ROLES)):
        raise CheckError("auth_env must map distinct role environment variable names")
    command = config["prepare_command"]
    if (not isinstance(command, list) or not command
            or any(not isinstance(v, str) or not v or "\0" in v for v in command)):
        raise CheckError("prepare_command must be a nonempty argument list")
    if not isinstance(config["evidence_dir"], str) or not config["evidence_dir"]:
        raise CheckError("evidence_dir must be a path")


def indexed(rows, key):
    result = {}
    for row in rows:
        identifier = row[key]
        if identifier in result:
            raise CheckError(f"duplicate {key}")
        result[identifier] = row
    return result


def validate_fixture(fixture):
    validate(fixture, "fixture.schema.json")
    bundles = indexed(fixture["bundles"], "bundle_id")
    agreements = indexed(fixture["agreements"], "agreement_id")
    calls = indexed(fixture["margin_calls"], "margin_call_id")
    principals = indexed(fixture["principals"], "principal_id")
    expected_roles = {"bank-a": "BANK_A", "bank-b": "BANK_B", "bank-c": "BANK_C",
                      "experiment-observer": None}
    for role, actor in expected_roles.items():
        if (role not in principals or principals[role]["actor"] != actor
                or principals[role]["observer"] != (actor is None)):
            raise CheckError("fixture principal mapping mismatch")
    for row in [*agreements.values(), *calls.values()]:
        if row["bundle_id"] not in bundles:
            raise CheckError("fixture references unknown bundle")
    for bundle in bundles:
        for kind in ("REPO", "LOAN"):
            if sum(a["bundle_id"] == bundle and a["type"] == kind
                   for a in agreements.values()) != 1:
                raise CheckError("fixture requires one REPO and LOAN agreement per bundle")
        if not any(c["bundle_id"] == bundle for c in calls.values()):
            raise CheckError("fixture bundle has no margin call")


def canonical(value):
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return sorted((canonical(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    return value


def initial_state(fixture, snapshots, run_id, platforms):
    """Compare the union of local records, preserving contradictory replicas as failures."""
    expected = {"bundles": [], "rights": [], "contracts": [], "obligations": [],
                "allocations": [], "pending_operations": [],
                "margin_calls": fixture["margin_calls"]}
    for bundle in fixture["bundles"]:
        expected["bundles"].append({k: v for k, v in bundle.items() if k != "initial_holder"}
                                   | {"version": 0})
        expected["rights"].append({
            "right_id": bundle["bundle_id"] + ":right:0", "bundle_id": bundle["bundle_id"],
            "holder": bundle["initial_holder"], "created_version": 0, "status": "AVAILABLE",
            "allowed_commands": ["CONFIRM_REPO"], "predecessor_right_id": None,
            "source_record_id": None})
    ids = {"bundles": "bundle_id", "rights": "right_id", "contracts": "contract_id",
           "obligations": "obligation_id", "margin_calls": "margin_call_id",
           "allocations": "allocation_id", "pending_operations": "request_id"}
    merged = {key: {} for key in ids}
    if set(snapshots) != set(platforms):
        raise CheckError("missing platform snapshot")
    for platform, snapshot in snapshots.items():
        validate(snapshot, "snapshot.schema.json")
        if ("error" in snapshot or snapshot["run_id"] != run_id
                or snapshot["platform"] != platform):
            raise CheckError("snapshot identity/error mismatch")
        for key, identifier in ids.items():
            local = indexed(snapshot[key], identifier)
            for rid, row in local.items():
                if rid in merged[key] and canonical(merged[key][rid]) != canonical(row):
                    raise CheckError(f"contradictory replicas in {key}")
                merged[key][rid] = row
    for key in ids:
        wanted = indexed(expected[key], ids[key])
        for rid, row in merged[key].items():
            if rid not in wanted or canonical(row) != canonical(wanted[rid]):
                raise CheckError(f"unexpected initial record/content in {key}")
        # Z-only data need not be visible in an X/Y-only preparation check.
        if key != "margin_calls" or "Z" in platforms:
            if set(merged[key]) != set(wanted):
                raise CheckError(f"missing initial records in {key}")


class EvidenceMissing(CheckError):
    """The result cannot be decided from the observations available."""


RECORD_IDS = {"bundles": "bundle_id", "rights": "right_id", "contracts": "contract_id",
              "obligations": "obligation_id", "margin_calls": "margin_call_id",
              "allocations": "allocation_id", "pending_operations": "request_id"}


def expected_state(fixture, version, repo_id="repo-001", loan_id="loan-001",
                   allocation_id="allocation-001", margin_call_id="margin-001"):
    """Instantiate approved S0–S3 records for the selected fixture and winning IDs."""
    if len(fixture["bundles"]) != 1:
        raise CheckError("scenario execution requires the approved single-bundle fixtures")
    bundle = fixture["bundles"][0]
    repo = next(a for a in fixture["agreements"] if a["type"] == "REPO")
    loan = next(a for a in fixture["agreements"] if a["type"] == "LOAN")
    replacements = {"bundle-001": bundle["bundle_id"], "TEST-KTB-001": bundle["security_id"],
                    "repo-agreement-001": repo["agreement_id"],
                    "loan-agreement-001": loan["agreement_id"], "repo-001": repo_id,
                    "loan-001": loan_id, "allocation-001": allocation_id,
                    "margin-001": margin_call_id,
                    "separate-collateral-001": loan["collateral_condition"]["collateral_id"]}

    def replace(value):
        if isinstance(value, dict):
            return {k: replace(v) for k, v in value.items()}
        if isinstance(value, list):
            return [replace(v) for v in value]
        if isinstance(value, str):
            for old, new in replacements.items():
                if value == old or value.startswith(old + ":"):
                    return new + value[len(old):]
        return value

    base = replace(read_json(ROOT / f"shared/fixtures/expected/s{version}.json"))
    expected = {k: base.get(k, []) for k in RECORD_IDS}
    expected["bundles"] = [{k: copy.deepcopy(v) for k, v in bundle.items()
                             if k != "initial_holder"} | {"version": version}]
    expected["margin_calls"] = copy.deepcopy(fixture["margin_calls"])
    if version == 1 and not repo["reuse_allowed"]:
        expected["rights"][-1]["allowed_commands"] = []
    if version == 2 and not loan["ccp_use_allowed"]:
        expected["rights"][-1]["allowed_commands"] = []
    for obligation in expected["obligations"]:
        if "security_id" in obligation["target"]:
            obligation["target"]["quantity"] = copy.deepcopy(bundle["quantity"])
    if version == 3:
        for call in expected["margin_calls"]:
            if call["margin_call_id"] == margin_call_id:
                call["status"] = "SATISFIED"
        expected["allocations"][0].update(bundle_id=bundle["bundle_id"], provider="BANK_C", ccp="CCP")
    return expected


def state_check(snapshots, run_id, expected, previous=(), platforms=("X", "Y", "Z")):
    if set(snapshots) != set(platforms):
        raise EvidenceMissing("platform snapshot missing")
    found = {key: {} for key in RECORD_IDS}
    for platform, body in snapshots.items():
        validate(body, "snapshot.schema.json")
        if "error" in body or body["run_id"] != run_id or body["platform"] != platform:
            raise CheckError("snapshot identity mismatch")
        for key, id_key in RECORD_IDS.items():
            wanted = indexed(expected[key], id_key)
            for rid, row in indexed(body[key], id_key).items():
                if rid in wanted and canonical(row) == canonical(wanted[rid]):
                    found[key][rid] = row
                elif any(canonical(row) == canonical(prior) for old in previous for prior in old[key]):
                    # A proven prior state can remain visible on a lagging local replica.
                    continue
                else:
                    if key == "pending_operations":
                        raise EvidenceMissing("local processing has not settled")
                    raise CheckError(f"unexpected {key} record/content")
    for key, id_key in RECORD_IDS.items():
        if key == "margin_calls" and "Z" not in platforms:
            continue
        if set(found[key]) != set(indexed(expected[key], id_key)):
            raise EvidenceMissing(f"current {key} records not yet visible")
    return found


def check_result(body, run_id, command, actor, status, replay=None):
    validate(body, "result.schema.json")
    if body["run_id"] != run_id or body["request_id"] != command["request_id"]:
        raise CheckError("response identity mismatch")
    if "outcome" not in body:
        wanted_status = {"INVALID_REQUEST": 400, "UNAUTHENTICATED": 401, "FORBIDDEN": 403,
                         "NOT_FOUND": 404, "REQUEST_ID_CONFLICT": 409,
                         "TEMPORARILY_UNAVAILABLE": 503, "OUTCOME_UNKNOWN": 503, "INTERNAL_ERROR": 503}
        if status != wanted_status.get(body["error"]["code"]):
            raise CheckError("preacceptance/query error HTTP/code mismatch")
        return body
    if body["actor"] != actor or body["command_type"] != command["command_type"]:
        raise CheckError("response actor/command mismatch")
    outcome = body["outcome"]
    if status != {"COMMITTED": 200, "REJECTED": 200, "PENDING": 202, "UNKNOWN": 503}[outcome]:
        raise CheckError("HTTP status/outcome mismatch")
    if replay is not None and body["replayed"] is not replay:
        raise CheckError("replayed flag mismatch")
    if outcome == "COMMITTED":
        effect = body["effect"]
        kind, record_key = {"CONFIRM_REPO": ("REPO", "repo_id"),
                            "CONFIRM_LOAN": ("LOAN", "loan_id"),
                            "ALLOCATE_MARGIN": ("ALLOCATION", "allocation_id"),
                            "REASSIGN_FOR_LENDING": ("REASSIGNMENT", None)}[command["command_type"]]
        wanted = {"bundle_id": command["bundle_id"], "version_before": command["expected_version"],
                  "version_after": command["expected_version"] + (record_key is not None),
                  "record_type": kind,
                  "record_id": command["payload"][record_key] if record_key else command["request_id"]}
        if record_key is None:
            wanted.update(command["payload"])
        if effect != wanted:
            raise CheckError("committed effect mismatch")
    return body


def history_check(events, commands, race_requests=()):
    """Require per-request internal effects and monotone rights on each producer clock."""
    for event in events:
        validate(event, "event.schema.json")
    seen_ids = set()
    clocks = {}
    for event in events:
        if event["event_id"] in seen_ids:
            raise CheckError("duplicate implementation event ID")
        seen_ids.add(event["event_id"])
        clocks.setdefault((event["producer_id"], event["clock_id"]), []).append(event)
    for rows in clocks.values():
        ordered = sorted(rows, key=lambda e: e["sequence"])
        if len({e["sequence"] for e in ordered}) != len(ordered):
            raise CheckError("duplicate producer event sequence")
        if any(int(a["mono_ns"]) > int(b["mono_ns"]) for a, b in zip(ordered, ordered[1:])):
            raise CheckError("implementation clock order reversed")
        states = {}
        bundles = {}
        for event in ordered:
            if event["event_type"].startswith("RIGHT_"):
                right = event.get("right_id")
                state = event.get("right_status")
                if right not in {f"{event['bundle_id']}:right:{i}" for i in range(3)}:
                    raise CheckError("unexpected common right ID in history")
                if event["holder"] != {"0": "BANK_A", "1": "BANK_B", "2": "BANK_C"}[right[-1]]:
                    raise CheckError("right history holder mismatch")
                expected_status = {"RIGHT_CREATED": "AVAILABLE", "RIGHT_CONSUMED": "CONSUMED",
                                   "RIGHT_LOCKED": "LOCKED"}[event["event_type"]]
                if state != expected_status:
                    raise CheckError("right event kind/status mismatch")
                if states.get(right) in ("CONSUMED", "LOCKED") and state == "AVAILABLE":
                    raise CheckError("consumed/locked right restored")
                states[right] = state
                bundles[right] = event["bundle_id"]
                if sum(value == "AVAILABLE" and bundles[rid] == event["bundle_id"]
                       for rid, value in states.items()) > 1:
                    raise CheckError("conflicting available rights in authoritative producer history")
    committed_ids = {c["request_id"] for c, r in commands if r.get("outcome") == "COMMITTED"}
    if any(e["event_type"] == "INTERNAL_COMMITTED" and e["request_id"] not in committed_ids for e in events):
        raise CheckError("unobserved or rejected request has an internal committed effect")
    for command, result in commands:
        if result.get("outcome") != "COMMITTED":
            continue
        internal = [e for e in events if e["event_type"] == "INTERNAL_COMMITTED"
                    and e["request_id"] == command["request_id"]]
        if not internal:
            raise EvidenceMissing("internal commit evidence missing")
        effects = {(e["record_id"], e["version_before"], e["version_after"]) for e in internal}
        effect = result["effect"]
        if effects != {(effect["record_id"], effect["version_before"], effect["version_after"])}:
            raise CheckError("internal effect disagrees with observed result")
        version = effect["version_after"]
        if command["command_type"] != "REASSIGN_FOR_LENDING":
            bundle = command["bundle_id"]
            required = ([("RIGHT_CONSUMED", f"{bundle}:right:{version-1}"),
                         ("RIGHT_CREATED", f"{bundle}:right:{version}")] if version < 3 else [
                            ("RIGHT_LOCKED", f"{bundle}:right:2")])
            for kind, right in required:
                if not any(e["event_type"] == kind and e.get("right_id") == right
                           and e["request_id"] == command["request_id"] for e in events):
                    raise EvidenceMissing("right transition evidence missing")
            platform = {"CONFIRM_REPO": "X", "CONFIRM_LOAN": "Y", "ALLOCATE_MARGIN": "Z"}[
                command["command_type"]]
            if not any(e["event_type"] == "LOCAL_APPLIED" and e["platform"] == platform
                       and e["request_id"] == command["request_id"] for e in events):
                raise EvidenceMissing("local application evidence missing")
            if version < 3:
                consumed = [e for e in events if e["event_type"] == "RIGHT_CONSUMED"
                            and e.get("right_id") == f"{bundle}:right:{version-1}"
                            and e["request_id"] == command["request_id"]]
                created = [e for e in events if e["event_type"] == "RIGHT_CREATED"
                           and e.get("right_id") == f"{bundle}:right:{version}"
                           and e["request_id"] == command["request_id"]]
                def before(a, b):
                    return (a["producer_id"], a["clock_id"]) == (b["producer_id"], b["clock_id"]) and a["sequence"] < b["sequence"]

                def consumption_precedes_creation(a, b):
                    if before(a, b):
                        return True
                    return any(before(a, sent) and before(received, b)
                               and sent.get("message_id") == received.get("message_id")
                               and sent.get("source_id") == received.get("source_id")
                               and sent.get("destination_id") == received.get("destination_id")
                               for sent in events if sent["event_type"] == "MESSAGE_SENT"
                               for received in events if received["event_type"] == "MESSAGE_RECEIVED")

                if not any(consumption_precedes_creation(a, b) for a in consumed for b in created):
                    raise EvidenceMissing("cannot establish prior-right consumption before successor creation")
    if race_requests:
        overlap = False
        for rows in clocks.values():
            intervals = []
            requests = set(race_requests)
            attempts = {(e["request_id"], e["attempt_id"]) for e in rows
                        if e["event_type"] == "COMMAND_RECEIVED" and e["request_id"] in requests}
            for request, attempt in attempts:
                starts = [int(e["mono_ns"]) for e in rows if e["event_type"] == "COMMAND_RECEIVED"
                          and e["request_id"] == request and e["attempt_id"] == attempt]
                ends = [int(e["mono_ns"]) for e in rows if e["event_type"] == "COMMAND_FINISHED"
                        and e["request_id"] == request and e["attempt_id"] == attempt]
                if starts and ends:
                    intervals.append((request, min(starts), max(ends)))
            overlap |= any((a[0] != b[0] or len(requests) == 1) and max(a[1], b[1]) < min(a[2], b[2])
                           for i, a in enumerate(intervals) for b in intervals[i+1:])
        if not overlap:
            raise EvidenceMissing("server-side concurrent processing overlap not evidenced")


def reassignment_check(manifest, events, run_id, expected_refs):
    if manifest.get("run_id") != run_id or manifest.get("synchronizers", {}).keys() != {"S1", "S2"}:
        raise CheckError("reassignment manifest identity/synchronizer mapping mismatch")
    mapping = manifest["synchronizers"]
    if not all(isinstance(v, str) and v for v in mapping.values()) or mapping["S1"] == mapping["S2"]:
        raise CheckError("actual source/target synchronizers must be distinct")
    contracts = manifest.get("required_contracts", [])
    if not contracts:
        raise EvidenceMissing("required reassignment contract set missing")
    ids = indexed(contracts, "contract_id")
    refs = set()
    spans, clock_keys, observed_spans = [], set(), []
    for contract_id, contract in ids.items():
        if not contract.get("reason") or not contract.get("business_refs"):
            raise EvidenceMissing("reassignment contract selection rationale/business mapping missing")
        refs.update(contract["business_refs"])
        if not set(contract.get("dependencies", [])) <= ids.keys():
            raise EvidenceMissing("required contract dependency not included")
        rows = [e for e in events if e.get("contract_id") == contract_id and e["run_id"] == run_id
                and e["event_type"] in ("UNASSIGNMENT_STARTED", "UNASSIGNMENT_COMPLETED",
                    "ASSIGNMENT_STARTED", "ASSIGNMENT_COMPLETED", "SOURCE_UNUSABLE", "TARGET_USABLE")]
        for event in rows:
            validate(event, "event.schema.json")
            if event.get("source_synchronizer") != "S1" or event.get("target_synchronizer") != "S2":
                raise CheckError("unexpected reassignment path")
        if len({e.get("reassignment_id") for e in rows}) != 1:
            raise EvidenceMissing("ambiguous/missing contract reassignment IDs")
        clock = {(e["producer_id"], e["clock_id"]) for e in rows}
        if len(clock) != 1:
            raise EvidenceMissing("contract reassignment clocks differ")
        clock_keys.update(clock)
        kinds = {}
        for kind in ("UNASSIGNMENT_STARTED", "UNASSIGNMENT_COMPLETED", "ASSIGNMENT_COMPLETED",
                     "SOURCE_UNUSABLE", "TARGET_USABLE"):
            matches = [e for e in rows if e["event_type"] == kind]
            if len(matches) != 1:
                raise EvidenceMissing("contract reassignment event missing/ambiguous")
            kinds[kind] = matches[0]
        start, end = (int(kinds[k]["mono_ns"]) for k in ("UNASSIGNMENT_STARTED", "ASSIGNMENT_COMPLETED"))
        unusable, usable = (int(kinds[k]["mono_ns"]) for k in ("SOURCE_UNUSABLE", "TARGET_USABLE"))
        unassigned = int(kinds["UNASSIGNMENT_COMPLETED"]["mono_ns"])
        if not start <= unusable <= unassigned <= end <= usable:
            raise CheckError("contract reassignment/availability order reversed")
        for key, kind in (("unassigned_evidence", "UNASSIGNMENT_COMPLETED"),
                          ("assigned_evidence", "ASSIGNMENT_COMPLETED")):
            reference = contract.get(key, {})
            if reference.get("event_id") != kinds[kind]["event_id"] or not reference.get("path") or not reference.get("sha256"):
                raise EvidenceMissing("raw reassignment event mapping missing")
        spans.append({"contract_id": contract_id, "reassignment_id": rows[0]["reassignment_id"],
                      "clock_id": next(iter(clock))[1], "T_RA": (end-start)/1e6,
                      "T_unavailable": (usable-unusable)/1e6,
                      "start_ns": str(start), "complete_ns": str(end)})
        observed_spans.append((kinds["UNASSIGNMENT_STARTED"].get("observation"),
                               kinds["ASSIGNMENT_COMPLETED"].get("observation")))
    if not set(expected_refs) <= refs:
        raise EvidenceMissing("required business rights/contracts/obligations not mapped")
    # Every observed reassignment must belong to the declared necessary set.
    if any(e["event_type"] in ("UNASSIGNMENT_STARTED", "ASSIGNMENT_COMPLETED")
           and e.get("contract_id") not in ids for e in events):
        raise CheckError("undeclared/additional contract reassignment")
    calls = manifest.get("api_calls", [])
    if not calls:
        raise EvidenceMissing("actual reassignment API/batch invocation evidence missing")
    call_ids = indexed(calls, "call_id")
    for call in calls:
        if call.get("operation") not in ("UNASSIGN", "ASSIGN") or not call.get("contract_ids") or not set(call["contract_ids"]) <= ids.keys():
            raise CheckError("reassignment API call contract/operation mismatch")
        reference = call.get("evidence", {})
        if not reference.get("path") or not reference.get("sha256") or not any(
                e["event_type"] == "MESSAGE_SENT" and e.get("message_id") == call["call_id"]
                and e.get("message_kind") == "REQUEST" and e["event_id"] == reference.get("event_id")
                for e in events):
            raise EvidenceMissing("reassignment API call raw/message mapping missing")
        visited = {call["call_id"]}
        retry = call.get("retry_of")
        while retry is not None:
            if retry not in call_ids or retry in visited:
                raise CheckError("invalid/cyclic reassignment API retry chain")
            visited.add(retry)
            original = call_ids[retry]
            if original["operation"] != call["operation"] or set(original["contract_ids"]) != set(call["contract_ids"]):
                raise CheckError("API retry changed reassignment operation")
            retry = original.get("retry_of")
    for contract in ids:
        if any(not any(c["operation"] == operation and contract in c["contract_ids"] for c in calls)
               for operation in ("UNASSIGN", "ASSIGN")):
            raise EvidenceMissing("contract reassignment API coverage missing")
    total = (max(int(s["complete_ns"]) for s in spans)-min(int(s["start_ns"]) for s in spans))/1e6 if len(clock_keys) == 1 else None
    basis = "producer" if total is not None else None
    if total is None and all(a and b for a, b in observed_spans):
        observation_clocks = {o["clock_id"] for pair in observed_spans for o in pair}
        if len(observation_clocks) == 1 and all(int(a["mono_ns"]) <= int(b["mono_ns"]) for a, b in observed_spans):
            total = (max(int(b["mono_ns"]) for _, b in observed_spans)-min(int(a["mono_ns"]) for a, _ in observed_spans))/1e6
            basis = "observer-receipt"
    return {"contracts": spans, "N_RA": len(spans), "logical_boundary_transitions": 1,
            "api_requests": len(calls), "api_retries": sum(c.get("retry_of") is not None for c in calls),
            "api_batches": sum(len(c["contract_ids"]) > 1 for c in calls),
            "total_preparation_ms": total, "total_preparation_basis": basis,
            "total_preparation_limit": None if total is not None else "cross-clock total not computable"}
