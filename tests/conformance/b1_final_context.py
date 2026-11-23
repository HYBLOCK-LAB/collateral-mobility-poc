"""Bind B1 final validation to source hashes and read-only host/service evidence."""

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

from checks import ROOT, CheckError, read_json, validate_config
from runner import digest, environment_info, redact, secret_values

IMPLEMENTATION = "4b827d661551ef63033b76da401b273135aa2bdf"
BASELINE = "204eb5e3be1575f6d2741881e99a6850052059e2"
IMPL = ROOT / "implementations/canton"


def exclusive_json(path, value, secrets):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(redact(value, secrets), ensure_ascii=False, indent=2) + "\n")


def capture(argv):
    done = subprocess.run(argv, cwd=ROOT if argv[0] == "git" else IMPL,
                          capture_output=True, text=True, timeout=60)
    if done.returncode:
        raise CheckError("evidence command failed: " + str(argv[0]))
    return (done.stdout + done.stderr).strip()


def source_hashes():
    capture(["git", "cat-file", "-e", BASELINE + "^{commit}"])
    files = capture(["git", "ls-tree", "-r", "--name-only", IMPLEMENTATION,
                     "--", "implementations/canton"]).splitlines()
    if "implementations/canton/adapter/server.py" not in files:
        raise CheckError("pinned implementation source list missing")
    hashes = {}
    for name in files:
        path = ROOT / name
        expected = subprocess.run(["git", "show", IMPLEMENTATION + ":" + name],
                                  cwd=ROOT, capture_output=True, check=True).stdout
        # The coordinator's fixture loader is an explicit overlay, recorded by hash.
        if name != "implementations/canton/prepare.py" and path.read_bytes() != expected:
            raise CheckError("implementation differs from pinned source: " + name)
        hashes[name] = digest(path)
    dar = IMPL / "daml/collateral/.daml/dist/collateral-0.4.0.dar"
    hashes[str(dar.relative_to(ROOT))] = digest(dar)
    return hashes


def collect(config, resource_policy, restart_policy, network_notes):
    if platform.system() != "Darwin":
        raise CheckError("this host collector supports the verified macOS environment only")
    if config["model"] != "B1" or set(config["platforms"]) != set("XYZ"):
        raise CheckError("B1 XYZ config required")
    if Path(os.environ.get("CANTON_IMPL_DIR", "")).resolve() != IMPL.resolve():
        raise CheckError("source scripts/env.sh from this checkout first")
    evidence = (ROOT / config["evidence_dir"]).resolve()
    if Path(os.environ.get("CANTON_EVIDENCE_DIR", str(IMPL / "logs"))).resolve() != evidence:
        raise CheckError("config evidence_dir differs from adapter CANTON_EVIDENCE_DIR")
    state = Path(os.environ["CANTON_STATE_DIR"])
    ids = read_json(state / "ids.json")
    if ids.get("topology") != "b1-xyz" or "CCP" not in ids.get("parties", {}):
        raise CheckError("active b1-xyz topology with CCP required")
    hashes = source_hashes()
    statuses = {name: capture(["bash", "scripts/" + name + ".sh", "status"])
                for name in ("db", "canton", "adapter")}
    if "running and bootstrapped" not in statuses["canton"]:
        raise CheckError("Canton is not bootstrapped")
    if any("adapter.sh: " + p + " ready (pid " not in statuses["adapter"] for p in "XYZ"):
        raise CheckError("all three adapters must be ready")
    settings = dict(line.split(" = ", 1) for line in statuses["db"].splitlines() if " = " in line)
    if any(settings.get(k) != "on" for k in ("fsync", "synchronous_commit", "full_page_writes")):
        raise CheckError("PostgreSQL durability settings missing or not on")
    raw = {
        "host": {k: capture(["sysctl", "-n", key]) for k, key in
                 (("model", "hw.model"), ("logical_cpus", "hw.ncpu"), ("memory_bytes", "hw.memsize"))},
        "java": capture([str(Path(os.environ["JAVA_HOME"]) / "bin/java"), "-version"]),
        "canton": capture([str(Path(os.environ["CANTON_HOME"]) / "bin/canton"), "--version"]),
        "adapter_python": capture([str(IMPL / ".venv/bin/python"), "--version"]),
        "observer_shell_limits": capture(["/bin/bash", "-c", "ulimit -a"]),
        "status": statuses, "ids": ids,
    }
    environment = {
        "os": platform.platform(), "cpu": raw["host"], "memory": raw["host"]["memory_bytes"],
        "runtime": {"observer": environment_info(), "java": raw["java"],
                    "adapter": raw["adapter_python"]},
        "database_or_canton": {"canton": raw["canton"], "postgresql": settings},
        "topology": ids, "resource_limits": {"operator_service_policy": resource_policy,
                                               "observer_shell": raw["observer_shell_limits"]},
        "durability": {"cluster_admin_session": settings,
                       "scope": "db.sh status; runtime connection overrides require operator review"},
        "network": {"platforms": config["platforms"], "notes": network_notes},
        "service_restart": {"policy": restart_policy, "before_validation": statuses},
    }
    context = {"implementation_version": IMPLEMENTATION + " + coordinator loader and DAR hashes",
               "implementation_files": hashes, "xy_baseline_commit": BASELINE,
               "z_change_record": "implementations/canton/README.md", "environment": environment}
    return evidence, context, raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--resource-policy", required=True,
                        help="Actual service limits/JVM heap and other workloads; do not guess")
    parser.add_argument("--restart-policy", required=True,
                        help="Actual restart policy during validation, including external supervisors")
    parser.add_argument("--network-notes", required=True,
                        help="Actual local/network/proxy arrangement used by these services")
    args = parser.parse_args()
    try:
        if any(not x.strip() or x.strip() in ("NOT_RUN", "TODO") for x in
               (args.resource_policy, args.restart_policy, args.network_notes)):
            raise CheckError("actual operator observations required")
        config = read_json(args.config)
        validate_config(config)
        evidence = (ROOT / config["evidence_dir"]).resolve()
        if (evidence / "context.json").exists() or (evidence / "final-host-evidence.json").exists():
            raise CheckError("context already exists; preserve it and use a fresh evidence directory")
        evidence, context, raw = collect(config, args.resource_policy, args.restart_policy, args.network_notes)
        secrets = secret_values(config)
        exclusive_json(evidence / "final-host-evidence.json", raw, secrets)
        context["host_evidence_sha256"] = digest(evidence / "final-host-evidence.json")
        exclusive_json(evidence / "context.json", context, secrets)
        print("B1 context captured; review actual resource/durability/restart facts before --final")
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print("B1 context NOT_READY: " + str(redact(str(error), secret_values())), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
