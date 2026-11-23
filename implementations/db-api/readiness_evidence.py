"""Model A XYZ adapters for the coordinator's existing prepare and collect commands."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

from prepare import CheckError, valid_run

PROOF_TIMEOUT = 4
COLLECT_TIMEOUT = 15
CREDENTIAL_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "DSN")
EVIDENCE_DSNS = {f"MODEL_A_{side}_EVIDENCE_DSN" for side in "XYZ"}


class EvidenceError(Exception):
    pass


def control(client, run_id, action):
    response = client.post(f"/internal/runs/{run_id}/readiness-evidence/{action}")
    if response.status_code != 200:
        raise EvidenceError(f"CONTROL_HTTP_{response.status_code}")
    value = response.json()
    if not isinstance(value, dict):
        raise EvidenceError("INVALID_CONTROL_RESPONSE")
    return value


def call_helper(args, mode, flag, path):
    environment = None
    if mode == "COLLECTED":
        environment = {key: value for key, value in os.environ.items()
                       if key in EVIDENCE_DSNS or not any(marker in key.upper() for marker in CREDENTIAL_MARKERS)}
    result = subprocess.run(
        [sys.executable, "-B", str(args.helper), args.stage, "--run-id", args.run_id, flag, str(path)],
        capture_output=True, text=True, encoding="utf-8", timeout=COLLECT_TIMEOUT if mode == "COLLECTED" else 60,
        env=environment,
    )
    try:
        value = json.loads(result.stdout)
    except ValueError:
        raise EvidenceError("INVALID_HELPER_RESPONSE") from None
    if (result.returncode or not isinstance(value, dict) or value.get("status") != mode
            or value.get("run_id") != args.run_id):
        raise EvidenceError("COORDINATOR_HELPER_FAILED")
    return value


def wait_for_proof(client, run_id, diagnostic):
    deadline = time.monotonic() + PROOF_TIMEOUT
    while True:
        proof = control(client, run_id, "seal")
        diagnostic["last_proof"] = proof
        if proof.get("status") == "INCOMPLETE":
            raise EvidenceError("READINESS_" + proof.get("reason", "INCOMPLETE"))
        if time.monotonic() >= deadline:
            raise EvidenceError("READINESS_PROOF_TIMEOUT")
        if proof.get("run_id") != run_id or proof.get("status") not in ("COMPLETE", "PENDING"):
            raise EvidenceError("INVALID_CONTROL_RESPONSE")
        if proof["status"] == "COMPLETE":
            return proof
        time.sleep(0.05)


def verify_export(path, run_id, proof, collection):
    ids = proof.get("event_ids")
    if (proof.get("run_id") != run_id or not isinstance(ids, list) or
            any(not isinstance(event_id, str) for event_id in ids) or
            any(not isinstance(proof.get(key), str) or not proof[key] for key in ("producer_id", "clock_id")) or
            not (len(ids) == len(set(ids)) == proof.get("generated") == proof.get("committed")) or
            any(proof.get(key) != 0 for key in ("in_flight", "queued", "writing"))):
        raise EvidenceError("INVALID_COMMIT_PROOF")
    with path.open(encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    if len(rows) != collection.get("event_count"):
        raise EvidenceError("COLLECTED_EVENT_COUNT_MISMATCH")
    events = [e for e in rows if e.get("platform") == "Z" and e.get("request_id") is None
              and e.get("command_type") == "ALLOCATE_MARGIN" and e.get("message_category") == "BUSINESS_INTEGRATION"
              and e.get("event_type") in ("MESSAGE_SENT", "MESSAGE_RECEIVED")]
    if (sorted(e["event_id"] for e in events) != sorted(ids) or
            any(e["run_id"] != run_id or e["producer_id"] != proof["producer_id"]
                or e["clock_id"] != proof["clock_id"] for e in events)):
        raise EvidenceError("COMMITTED_READINESS_EVENTS_MISSING_OR_UNEXPECTED")


def save_diagnostic(path, diagnostic, *, required=False):
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(diagnostic, stream)
            stream.write("\n")
    except OSError:
        if required:
            raise EvidenceError("DIAGNOSTIC_WRITE_FAILED") from None


def prepare_publication(args, client):
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise EvidenceError("OUTPUT_ALREADY_EXISTS")
    stem = f"{args.output.stem}.readiness-{uuid4().hex}"
    candidate = args.output.with_name(stem + ".jsonl")
    diagnostic_file = args.output.with_name(stem + ".json")
    diagnostic = {"run_id": args.run_id, "status": "FAILED"}
    helper_attempted = False
    try:
        proof = wait_for_proof(client, args.run_id, diagnostic)
        helper_attempted = True
        result = call_helper(args, "COLLECTED", "--output", candidate)
        verify_export(candidate, args.run_id, proof, result)
        if control(client, args.run_id, "seal") != proof:
            raise EvidenceError("READINESS_PROOF_CHANGED_DURING_COLLECTION")
    except (EvidenceError, OSError, ValueError, KeyError, TypeError, httpx.HTTPError, subprocess.SubprocessError) as error:
        diagnostic["reason"] = str(error) if isinstance(error, EvidenceError) else type(error).__name__
        if not helper_attempted:
            try:
                call_helper(args, "COLLECTED", "--output", candidate)
            except (EvidenceError, OSError, subprocess.SubprocessError):
                diagnostic["diagnostic_export"] = "FAILED"
        save_diagnostic(diagnostic_file, diagnostic)
        raise EvidenceError(diagnostic["reason"]) from None

    # This records verification of the candidate, independent of the publication outcome.
    save_diagnostic(diagnostic_file, diagnostic | {"status": "VERIFIED"}, required=True)
    return candidate, diagnostic_file, result


def publish(args, candidate, diagnostic_file, result):
    try:
        os.link(candidate, args.output)
    except OSError:
        failure_file = diagnostic_file.with_suffix(".publish-failure.json")
        save_diagnostic(failure_file, {"run_id": args.run_id, "status": "FAILED",
                                       "reason": "FINAL_PUBLICATION_FAILED", "diagnostic": diagnostic_file.name})
        raise EvidenceError("FINAL_PUBLICATION_FAILED") from None
    return result


def collect(args, client):
    return publish(args, *prepare_publication(args, client))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "collect"))
    parser.add_argument("control_url")
    parser.add_argument("token_file", type=Path)
    parser.add_argument("helper", type=Path)
    parser.add_argument("stage", choices=("xyz",))
    parser.add_argument("--run-id", required=True)
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--fixture", type=Path)
    output.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        valid_run(args.run_id)
        helper_name = "prepare.py" if args.action == "prepare" else "collect_events.py"
        if args.helper.resolve() != Path(__file__).resolve().with_name(helper_name):
            raise EvidenceError("UNEXPECTED_COORDINATOR_HELPER")
        if (args.action == "prepare") != (args.fixture is not None):
            raise EvidenceError("INVALID_ACTION_ARGUMENTS")
        url = httpx.URL(args.control_url)
        if url.scheme not in ("http", "https") or not url.host or url.userinfo or url.path != "/" or url.query or url.fragment:
            raise EvidenceError("INVALID_Z_CONTROL_URL")
        token = args.token_file.read_text(encoding="utf-8").strip()
        if not token or "\r" in token or "\n" in token:
            raise EvidenceError("INVALID_CONTROL_CREDENTIAL")
        with httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"},
                          timeout=0.8, trust_env=False) as client:
            if args.action == "prepare":
                if control(client, args.run_id, "register") != {"run_id": args.run_id, "status": "REGISTERED"}:
                    raise EvidenceError("INVALID_REGISTRATION_RESPONSE")
                result = call_helper(args, "PREPARED", "--fixture", args.fixture)
            else:
                publication = prepare_publication(args, client)
        if args.action == "collect":
            result = publish(args, *publication)
    except (EvidenceError, CheckError, OSError, ValueError, httpx.HTTPError, httpx.InvalidURL,
            subprocess.SubprocessError) as error:
        response = {"status": "FAILED", "reason": str(error) if isinstance(error, EvidenceError) else type(error).__name__}
        if args.action == "prepare":
            response["run_id"] = args.run_id
        print(json.dumps(response))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
