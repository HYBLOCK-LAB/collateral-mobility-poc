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


def seed_z_test(run_id, fixture, validator):
    """Seed only the isolated Z side of a Model A self-test."""
    import psycopg

    validate_fixture = validator("fixture")
    validate_fixture.validate(fixture)
    with psycopg.connect(os.environ["MODEL_A_Z_TEST_DSN"], connect_timeout=3) as connection:
        identity = connection.execute(
            "SELECT current_database(), current_setting('server_version_num')"
        ).fetchone()
        check(identity[0] == "model_a_z" and int(identity[1]) // 10000 == 17, "Wrong self-test database")
        connection.execute("INSERT INTO runs(run_id) VALUES (%s)", (run_id,))
        for principal in fixture["principals"]:
            connection.execute("INSERT INTO principals(run_id,principal_id,actor,observer) VALUES (%s,%s,%s,%s)",
                               (run_id, principal["principal_id"], principal["actor"], principal["observer"]))
        for bundle in fixture["bundles"]:
            connection.execute(
                "INSERT INTO bundles(run_id,bundle_id,security_id,quantity,reference_amount,version)"
                " VALUES (%s,%s,%s,%s,%s,0)",
                (run_id, bundle["bundle_id"], bundle["security_id"],
                 bundle["quantity"]["value"], bundle["reference_value"]["amount"]),
            )
        for call in fixture["margin_calls"]:
            connection.execute(
                "INSERT INTO margin_calls(run_id,margin_call_id,bundle_id,provider,ccp,"
                "whole_bundle_accepted,eligibility_passed,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (run_id, call["margin_call_id"], call["bundle_id"], call["provider"], call["ccp"],
                 call["whole_bundle_accepted"], call["eligibility_passed"], call["status"]),
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--x", default="http://127.0.0.1:8101")
    parser.add_argument("--y", default="http://127.0.0.1:8102")
    parser.add_argument("--z", help="Include an independently prepared Z DB and validate S3")
    parser.add_argument("--seed-z-test", action="store_true",
                        help="Seed F0 in an explicitly supplied isolated MODEL_A_Z_TEST_DSN")
    repo_shared = Path(__file__).resolve().parent.parent.parent / "shared"
    parser.add_argument("--fixtures", type=Path, default=repo_shared / "fixtures")
    parser.add_argument("--schemas", type=Path, default=repo_shared / "schemas")
    args = parser.parse_args()
    schemas = {path.name.split(".")[0]: load(path) for path in args.schemas.glob("*.schema.json")}
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()
    )

    def validator(name):
        return Draft202012Validator(schemas[name], registry=registry)

    if args.seed_z_test:
        if not args.z or not args.run_id.startswith("zdev-"):
            parser.error("--seed-z-test requires --z and a fresh zdev- run_id")
        seed_z_test(args.run_id, load(args.fixtures / "initial" / "f0-normal.json"), validator)

    urls = {"X": args.x, "Y": args.y}
    if args.z:
        urls["Z"] = args.z
    with httpx.Client(timeout=5, trust_env=False) as client:
        def call(platform, method, path, token, schema, statuses=(200,), **kwargs):
            headers = {
                "Authorization": f"Bearer {os.environ[token]}",
                "X-Test-ID": "SELFTEST-XYZ" if args.z else "SELFTEST-XY",
                "X-Attempt-ID": uuid4().hex,
            }
            response = client.request(method, f"{urls[platform]}/runs/{args.run_id}/{path}",
                                      headers=headers, **kwargs)
            check(response.status_code in statuses, f"{platform} {path}: {response.status_code} {response.text}")
            body = response.json()
            validator(schema).validate(body)
            check(body["run_id"] == args.run_id, f"{platform} returned a different run")
            return body

        def snapshots():
            return {platform: call(platform, "GET", "snapshot", "POC_OBSERVER_TOKEN", "snapshot")
                    for platform in urls}

        def assert_state(stage, state):
            expected = load(args.fixtures / "expected" / f"s{stage}.json")
            previous = [load(args.fixtures / "expected" / f"s{i}.json") for i in range(stage)]
            check(all(not body["pending_operations"] for body in state.values()),
                  f"S{stage}: unexpected pending operation")
            allocations = [row for body in state.values() for row in body["allocations"]]
            check(allocations == ([{
                **expected["allocations"][0], "bundle_id": "bundle-001", "provider": "BANK_C", "ccp": "CCP",
            }] if stage == 3 else []), f"S{stage}: allocation mismatch")
            if args.z:
                check(state["Z"]["margin_calls"] == expected["margin_calls"], f"S{stage}: margin call mismatch")
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
            owner = ("X", "X", "Y", "Z")[stage]
            fixture = load(args.fixtures / "initial" / "f0-normal.json")["bundles"][0]
            wanted_bundle = {key: fixture[key] for key in ("bundle_id", "security_id", "quantity", "reference_value")}
            check(state[owner]["bundles"] == [dict(wanted_bundle, version=stage)], f"S{stage}: bundle mismatch")
            if stage == 2:
                check(all(row["status"] == "CONSUMED" for row in state["X"]["rights"]),
                      "X still exposes an available right after the loan")
            elif stage == 3:
                check(all(row["status"] != "AVAILABLE" for body in state.values() for row in body["rights"]),
                      "S3 still exposes an available right")
            print(f"{args.run_id} S{stage} PASS")

        loan = load(args.fixtures / "commands" / "loan.json")

        def verify_readiness(stage, ready, platform="Y"):
            command, token, target = (loan, "POC_BANK_B_TOKEN", "agreement_id") if platform == "Y" else (
                margin, "POC_BANK_C_TOKEN", "margin_call_id")
            before = snapshots()
            params = {"bundle_id": command["bundle_id"], "command_type": command["command_type"],
                      target: command["payload"][target]}
            for _ in range(2):
                assertion = call(platform, "GET", "readiness", token, "readiness", params=params)
                check(assertion["ready"] is ready and assertion["observed_version"] == stage,
                      f"Unexpected {platform} readiness")
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
            expected_kind = ("REPO", "LOAN", "ALLOCATION")[before]
            check(result["effect"] == {
                "bundle_id": command["bundle_id"], "version_before": before, "version_after": before + 1,
                "record_type": expected_kind, "record_id": command["payload"][expected_kind.lower() + "_id"],
            }, "Unexpected transaction effect")
            check(call(platform, "GET", path, token, "result") == result, "Original result lookup mismatch")
            return result

        assert_state(0, snapshots())
        verify_readiness(0, False)
        if args.z:
            margin = load(args.fixtures / "commands" / "margin.json")
            verify_readiness(0, False, "Z")
        trade("X", load(args.fixtures / "commands" / "repo.json"), "POC_BANK_A_TOKEN", 0)
        assert_state(1, snapshots())
        verify_readiness(1, True)
        trade("Y", loan, "POC_BANK_B_TOKEN", 1)
        assert_state(2, snapshots())
        if args.z:
            verify_readiness(2, True, "Z")
            committed = trade("Z", margin, "POC_BANK_C_TOKEN", 2)
            assert_state(3, snapshots())
            replay = call("Z", "POST", "commands", "POC_BANK_C_TOKEN", "result", json=margin)
            check(replay == dict(committed, replayed=True), "Z replay did not return the stored allocation")
            verify_readiness(3, False, "Z")
            assert_state(3, snapshots())
        print(f"{args.run_id} SELFTEST-{'XYZ' if args.z else 'XY'} PASS")


if __name__ == "__main__":
    main()
