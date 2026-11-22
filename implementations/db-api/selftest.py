"""Check an explicitly prepared S0 run through the public API."""
import argparse
import json
import os
import time
from pathlib import Path
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator
from referencing import Registry, Resource


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--x", default="http://127.0.0.1:8101")
    parser.add_argument("--y", default="http://127.0.0.1:8102")
    repo_shared = Path(__file__).resolve().parent.parent.parent / "shared"
    parser.add_argument("--fixtures", type=Path, default=repo_shared / "fixtures")
    parser.add_argument("--schemas", type=Path, default=repo_shared / "schemas")
    args = parser.parse_args()
    schemas = {path.name.split(".")[0]: load(path) for path in args.schemas.glob("*.schema.json")}
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()
    )
    urls = {"X": args.x, "Y": args.y}
    with httpx.Client(timeout=5, trust_env=False) as client:
        def call(platform, method, path, token, schema, statuses=(200,), **kwargs):
            headers = {
                "Authorization": f"Bearer {os.environ[token]}",
                "X-Test-ID": "SELFTEST-XY", "X-Attempt-ID": uuid4().hex,
            }
            response = client.request(method, f"{urls[platform]}/runs/{args.run_id}/{path}",
                                      headers=headers, **kwargs)
            check(response.status_code in statuses, f"{platform} {path}: {response.status_code} {response.text}")
            body = response.json()
            Draft202012Validator(schemas[schema], registry=registry).validate(body)
            check(body["run_id"] == args.run_id, f"{platform} returned a different run")
            return body

        def snapshots():
            return {platform: call(platform, "GET", "snapshot", "POC_OBSERVER_TOKEN", "snapshot")
                    for platform in urls}

        def assert_state(stage, state):
            expected = load(args.fixtures / "expected" / f"s{stage}.json")
            previous = [load(args.fixtures / "expected" / f"s{i}.json") for i in range(stage)]
            check(all(not body["pending_operations"] and not body["allocations"] for body in state.values()),
                  f"S{stage}: unexpected pending operation or allocation")
            for name, key in (("rights", "right_id"), ("contracts", "contract_id"),
                              ("obligations", "obligation_id")):
                current = {row[key]: row for row in expected[name]}
                seen = set()
                for body in state.values():
                    for row in body[name]:
                        if current.get(row[key]) == row:
                            seen.add(row[key])
                        else:
                            check(any(row in old[name] for old in previous), f"S{stage}: unexpected {name}: {row}")
                check(seen == set(current), f"S{stage}: missing or outdated {name}")
            owner = "Y" if stage == 2 else "X"
            fixture = load(args.fixtures / "initial" / "f0-normal.json")["bundles"][0]
            wanted_bundle = {key: fixture[key] for key in ("bundle_id", "security_id", "quantity", "reference_value")}
            check(state[owner]["bundles"] == [dict(wanted_bundle, version=stage)], f"S{stage}: bundle mismatch")
            if stage == 2:
                check(all(row["status"] == "CONSUMED" for row in state["X"]["rights"]),
                      "X still exposes an available right after the loan")
            print(f"{args.run_id} S{stage} PASS")

        loan = load(args.fixtures / "commands" / "loan.json")

        def readiness():
            return call("Y", "GET", "readiness", "POC_BANK_B_TOKEN", "readiness", params={
                "bundle_id": loan["bundle_id"], "command_type": loan["command_type"],
                "agreement_id": loan["payload"]["agreement_id"],
            })

        def verify_readiness(stage, ready):
            before = snapshots()
            assertion = readiness()
            check(assertion["ready"] is ready and assertion["observed_version"] == stage, "Unexpected readiness")
            after = snapshots()
            for platform in urls:
                before[platform].pop("snapshot_seq")
                after[platform].pop("snapshot_seq")
            check(before == after, "Readiness changed a snapshot")

        def trade(platform, command, token, before):
            result = call(platform, "POST", "commands", token, "result", (200, 202), json=command)
            path = "commands/" + command["request_id"]
            deadline = time.monotonic() + 5
            while result["outcome"] == "PENDING" and time.monotonic() < deadline:
                time.sleep(0.2)
                result = call(platform, "GET", path, token, "result", (200, 202))
            check(result["outcome"] == "COMMITTED" and result["replayed"] is False, "Command did not commit")
            expected_kind = "REPO" if before == 0 else "LOAN"
            check(result["effect"] == {
                "bundle_id": command["bundle_id"], "version_before": before, "version_after": before + 1,
                "record_type": expected_kind, "record_id": command["payload"][expected_kind.lower() + "_id"],
            }, "Unexpected transaction effect")
            check(call(platform, "GET", path, token, "result") == result, "Original result lookup mismatch")

        assert_state(0, snapshots())
        verify_readiness(0, False)
        trade("X", load(args.fixtures / "commands" / "repo.json"), "POC_BANK_A_TOKEN", 0)
        assert_state(1, snapshots())
        verify_readiness(1, True)
        trade("Y", loan, "POC_BANK_B_TOKEN", 1)
        assert_state(2, snapshots())
        print(f"{args.run_id} SELFTEST-XY PASS")


if __name__ == "__main__":
    main()
