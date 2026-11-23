"""Coordinator-owned S0 loader; XYZ requires the Model A Z tables from PR #8."""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/conformance"))
from checks import CheckError, read_json, validate_fixture  # noqa: E402


def valid_run(run_id):
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", run_id) or set(run_id) == {"."}:
        raise CheckError("invalid run_id")


def connect(platform, purpose):
    connection = psycopg.connect(os.environ[f"MODEL_A_{platform}_{purpose}_DSN"], connect_timeout=3)
    try:
        database, version = connection.execute(
            "SELECT current_database(), current_setting('server_version_num')"
        ).fetchone()
        if database != f"model_a_{platform.lower()}" or int(version) // 10000 != 17:
            raise CheckError("unexpected Model A database/version")
        connection.rollback()
        return connection
    except BaseException:
        connection.close()
        raise


def seed_rows(fixture, platform):
    """Map the approved fixture to local S0, never to committed transactions."""
    validate_fixture(fixture)
    for bundle in fixture["bundles"]:
        if (len(bundle["bundle_id"]) > 120 or bundle["initial_holder"] != "BANK_A"
                or bundle["quantity"]["unit"] != "KRW_FACE_VALUE"
                or bundle["reference_value"]["currency"] != "KRW"):
            raise CheckError("fixture cannot be represented by current Model A storage")
    kind = {"X": "REPO", "Y": "LOAN", "Z": None}[platform]
    agreements = []
    for row in fixture["agreements"]:
        if row["type"] == kind:
            terms = {k: v for k, v in row.items() if k not in ("agreement_id", "bundle_id", "type")}
            agreements.append((row["agreement_id"], row["bundle_id"], kind, terms))
    mapped = {
        "principals": [(p["principal_id"], p["actor"], p["observer"]) for p in fixture["principals"]],
        "bundles": [(b["bundle_id"], b["security_id"], b["quantity"]["value"],
                     b["reference_value"]["amount"]) for b in fixture["bundles"]],
        "agreements": agreements,
        "rights": [(b["bundle_id"], b["initial_holder"]) for b in fixture["bundles"]] if platform == "X" else [],
    }
    if platform == "Z":
        mapped["margin_calls"] = [tuple(row[key] for key in (
            "margin_call_id", "bundle_id", "provider", "ccp", "whole_bundle_accepted",
            "eligibility_passed", "status")) for row in fixture["margin_calls"]]
    return mapped


def prepare(run_id, fixture, stage="xy"):
    valid_run(run_id)
    mapped = {p: seed_rows(fixture, p) for p in {"xy": ("X", "Y"), "xyz": ("X", "Y", "Z")}[stage]}
    # Preflight every selected database before writing any. Primary keys reject races.
    for platform in mapped:
        with connect(platform, "SEED") as connection:
            if connection.execute("SELECT 1 FROM runs WHERE run_id = %s", (run_id,)).fetchone():
                raise CheckError("RUN_ALREADY_EXISTS")
    completed = []
    try:
        for platform, rows in mapped.items():
            with connect(platform, "SEED") as connection:
                connection.execute("INSERT INTO runs(run_id) VALUES (%s)", (run_id,))
                for principal, actor, observer in rows["principals"]:
                    connection.execute(
                        "INSERT INTO principals(run_id,principal_id,actor,observer) VALUES (%s,%s,%s,%s)",
                        (run_id, principal, actor, observer))
                for bundle, security, quantity, amount in rows["bundles"]:
                    connection.execute(
                        "INSERT INTO bundles(run_id,bundle_id,security_id,quantity,reference_amount,version) "
                        "VALUES (%s,%s,%s,%s,%s,0)", (run_id, bundle, security, quantity, amount))
                for agreement, bundle, kind, terms in rows["agreements"]:
                    connection.execute(
                        "INSERT INTO agreements(run_id,agreement_id,bundle_id,kind,terms) VALUES (%s,%s,%s,%s,%s)",
                        (run_id, agreement, bundle, kind, Jsonb(terms)))
                for bundle, holder in rows["rights"]:
                    connection.execute(
                        "INSERT INTO rights(run_id,bundle_id,created_version,holder,status,allowed_commands) "
                        "VALUES (%s,%s,0,%s,'AVAILABLE',%s)", (run_id, bundle, holder, Jsonb(["CONFIRM_REPO"])))
                for margin in rows.get("margin_calls", []):
                    connection.execute(
                        "INSERT INTO margin_calls(run_id,margin_call_id,bundle_id,provider,ccp,"
                        "whole_bundle_accepted,eligibility_passed,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (run_id, *margin))
            completed.append(platform)
    except Exception:
        # Separate databases cannot commit atomically. Never erase a partial run.
        print(json.dumps({"stage": "seed", "committed_platforms": completed}), file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", nargs="?", choices=("xy", "xyz"), default="xy")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--fixture", required=True, type=Path)
    args = parser.parse_args()
    try:
        prepare(args.run_id, read_json(args.fixture), args.stage)
    except Exception as error:
        # Driver errors and DSNs may contain credentials; never print their messages.
        reason = str(error) if isinstance(error, CheckError) else type(error).__name__
        print(json.dumps({"run_id": args.run_id, "status": "FAILED", "reason": reason}))
        return 1
    print(json.dumps({"run_id": args.run_id, "status": "PREPARED"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
