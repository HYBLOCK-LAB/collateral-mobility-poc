"""Deterministic derived tables/figures; raw observations are read-only."""

import csv
import hashlib
import importlib.metadata
import json
import math
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from checks import CheckError, EvidenceMissing, parse_json, read_json, validate

METRICS = ("T_A", "T_W", "T_C", "T_E", "T_R")


def nearest_rank(values, probability):
    return sorted(values)[math.ceil(probability * len(values))-1] if values else None


def durations(events):
    selected = [e for e in events if e["producer_id"] == "conformance-runner"]
    clocks = {e["clock_id"] for e in selected}
    if len(clocks) != 1:
        raise EvidenceMissing("observer events do not share one clock")

    def first(kind, platform=None):
        values = [int(e["mono_ns"]) for e in selected if e["event_type"] == kind
                  and (platform is None or e["platform"] == platform)]
        if not values:
            raise EvidenceMissing("timing event missing: " + kind)
        return min(values)

    result = {}
    try:
        times = [first("COMMIT_OBSERVED", "X"), first("READY_OBSERVED", "Y"),
                 first("COMMAND_SUBMITTED", "Y"), first("COMMIT_OBSERVED", "Y"),
                 first("READY_OBSERVED", "Z"), first("COMMAND_SUBMITTED", "Z"),
                 first("COMMIT_OBSERVED", "Z")]
        if times != sorted(times):
            raise CheckError("observer timing order reversed")
        x, yr, ys, yc, zr, zs, zc = times
        result.update(T_A=(yr-x+zr-yc)/1e6, T_W=(ys-yr+zs-zr)/1e6,
                      T_C=(yc-ys+zc-zs)/1e6, T_E=(zc-x)/1e6)
        if not math.isclose(result["T_E"], result["T_A"]+result["T_W"]+result["T_C"]):
            raise CheckError("E2E identity failed")
    except EvidenceMissing:
        pass
    if any(e["event_type"] == "FAULT_ACTIVATED" for e in selected):
        start, end = first("FAULT_ACTIVATED"), first("RECOVERY_COMPLETED")
        if end < start:
            raise CheckError("recovery time reversed")
        result["T_R"] = (end-start)/1e6
    return result


def run_values(row, batch):
    for name, sha in row.get("evidence_sha256", {}).items():
        source = (batch / row["path"] / name).resolve()
        if not source.is_relative_to((batch / row["path"]).resolve()) or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != sha:
            raise CheckError("raw evidence changed since run verdict")
    result = {k: row.get(k) for k in ("model", "run_id", "test_id", "variant", "repeat", "verdict", "reason")}
    result.update(suite=row.get("suite", "CHECK"), phase="warmup" if row.get("warmup") else "main",
                  timing_valid=False, exclusion_reason=None, N_success=0, N_reject=0,
                  N_pending=None, N_retry=0, N_query=0, N_double=None,
                  **{k: None for k in METRICS})
    path = batch / row["path"] / "events.jsonl"
    events = [parse_json(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    for event in events:
        validate(event, "event.schema.json")
        if event["run_id"] != row["run_id"] or event["model"] != row["model"]:
            raise CheckError("report event identity mismatch")
    logical = {r["request_id"]: r["outcome"] for r in row.get("logical_results", [])}
    submitted = {e["request_id"] for e in events if e["event_type"] == "COMMAND_SUBMITTED"}
    preacceptance = {r["request_id"] for r in row.get("preacceptance_errors", [])}
    result.update(N_success=sum(v == "COMMITTED" for v in logical.values()),
                  N_reject=sum(v == "REJECTED" for v in logical.values()),
                  N_pending=len(submitted-set(logical)-preacceptance),
                  N_retry=sum(e["event_type"] == "COMMAND_RETRIED" for e in events),
                  N_query=sum(e["event_type"] == "RESULT_CHECKED" for e in events),
                  N_double=0 if row.get("evidence_complete") and row["verdict"] == "PASS" else None)
    try:
        times = durations(events) if events else {}
        result.update(times)
        normal = row.get("suite") in ("T1", "T4")
        timing_valid = row["verdict"] == "PASS" and row.get("evidence_complete") and (
            "T_E" in times if normal else "T_R" in times if row.get("suite") == "T3" else False)
        result["timing_valid"] = bool(timing_valid)
        if not timing_valid:
            result["exclusion_reason"] = row.get("reason") or "no complete, evidenced performance flow"
    except CheckError as error:
        result["exclusion_reason"] = str(error)
    return result


def reassignment_rows(raw_rows):
    rows = []
    for run in raw_rows:
        metrics = run.get("reassignment")
        if metrics:
            rows.extend(dict(row, run_id=run["run_id"], model=run["model"],
                             verdict=run["verdict"], warmup=run.get("warmup", False),
                             N_RA=metrics["N_RA"], total_preparation_ms=metrics["total_preparation_ms"],
                             **{k: metrics.get(k) for k in ("api_requests", "api_retries", "api_batches", "command_submissions", "total_preparation_basis")})
                        for row in metrics["contracts"])
    return rows


def complexity_rows(manifest, batch):
    models = [manifest["model"]] if "model" in manifest else [c["model"] for c in manifest.get("configs", [])]
    archived = batch / "evidence/implementation-context.json"
    context = read_json(archived).get("context", {}) if archived.is_file() else {}
    rows = []
    for model in models:
        for metric, key in (("N_interface", "interfaces"), ("N_changed", "changed_units"),
                            ("N_exception", "exceptions")):
            inventory = context.get("complexity", {}).get(key, []) if model == manifest.get("model") else []
            groups = defaultdict(set)
            for item in inventory:
                if all(item.get(k) for k in ("id", "kind", "evidence_ref")):
                    groups[item["kind"]].add((item["id"], item["evidence_ref"]))
            if not groups:
                rows.append(dict(model=model, metric=metric, kind=None, value=None,
                                 status="NOT_RUN", evidence_ref=None,
                                 note="Reviewed inventory and preserved X/Y-to-Z change evidence required"))
            for kind, items in sorted(groups.items()):
                rows.append(dict(model=model, metric=metric, kind=kind, value=len({i for i, _ in items}),
                                 status="PROVIDED_FOR_REVIEW", evidence_ref=";".join(sorted({r for _, r in items})),
                                 note="Counted by definition/type; no combined score"))
    return rows


def summary_rows(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row["model"], row["suite"], row["phase"]].append(row)
    summary = []
    for (model, suite, phase), group in sorted(groups.items()):
        counts = Counter(r["verdict"] for r in group)
        for metric in METRICS:
            sample = [r[metric] for r in group if r["timing_valid"] and r[metric] is not None and phase == "main"]
            summary.append({"model": model, "suite": suite, "phase": phase, "metric": metric,
                            "unit": "ms", "attempts": len(group), "pass": counts["PASS"],
                            "fail": counts["FAIL"], "inconclusive": counts["INCONCLUSIVE"],
                            "not_run": counts["NOT_RUN"],
                            "invalid_timing": sum(not r["timing_valid"] for r in group),
                            "n": len(sample), "p50": nearest_rank(sample, .5),
                            "p95": nearest_rank(sample, .95), "min": min(sample) if sample else None,
                            "max": max(sample) if sample else None})
    return summary


def write_csv(path, rows):
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def plot_figures(batch, rows, reassignment=(), synthetic=False):
    os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "collateral-mpl"))
    os.environ.setdefault("XDG_CACHE_HOME", str(Path(tempfile.gettempdir()) / "collateral-cache"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    matplotlib.rcParams.update({"svg.hashsalt": "collateral-mobility", "font.size": 9})
    folder = batch / "figures"
    folder.mkdir(exist_ok=True)

    def save(fig, name):
        if synthetic:
            fig.suptitle("SYNTHETIC HARNESS SELF-TEST - NOT MODEL RESULTS", fontsize=10)
        fig.tight_layout(rect=(0, 0, 1, .94) if synthetic else (0, 0, 1, 1))
        fig.savefig(folder / (name + ".png"), dpi=160)
        fig.savefig(folder / (name + ".svg"), metadata={"Date": None})
        plt.close(fig)

    fig, axes = plt.subplots(1, 3, figsize=(12, 3.7))
    for ax, metric in zip(axes, ("T_A", "T_C", "T_E")):
        groups = defaultdict(list)
        for row in rows:
            if row["suite"] in ("T1", "T4") and row["phase"] == "main" and row["timing_valid"]:
                groups[row["model"]].append(row[metric])
        if not groups:
            ax.text(.5, .5, "No valid measured samples", ha="center", transform=ax.transAxes)
        for i, (model, sample) in enumerate(sorted(groups.items())):
            ax.scatter([i]*len(sample), sample, alpha=.6, label=f"{model} n={len(sample)}")
            ax.scatter([i], [nearest_rank(sample, .5)], marker="_", s=250, color="black")
            ax.scatter([i], [nearest_rank(sample, .95)], marker="x", s=60, color="red")
        ax.set_xticks(range(len(groups)), sorted(groups))
        ax.set_title(metric + " (P50 black / P95 red)")
        ax.set_ylabel("ms")
        if groups:
            ax.legend()
    save(fig, "latency")
    fig, ax = plt.subplots(figsize=(8, 4))
    groups = sorted({(r["model"], r["suite"]) for r in rows})
    bottoms = [0]*len(groups)
    for verdict in ("PASS", "FAIL", "INCONCLUSIVE", "NOT_RUN"):
        values = [sum((r["model"], r["suite"]) == group and r["verdict"] == verdict for r in rows)
                  for group in groups]
        ax.bar(range(len(groups)), values, bottom=bottoms, label=verdict)
        bottoms = [a+b for a, b in zip(bottoms, values)]
    ax.set_xticks(range(len(groups)), ["/".join(g) for g in groups])
    ax.set_ylabel("Attempts (warmups included)")
    ax.legend()
    save(fig, "consistency")
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.7))
    recovery = [r for r in rows if r["suite"] == "T3"]
    valid = [r for r in recovery if r["timing_valid"] and r["T_R"] is not None]
    if valid:
        axes[0].scatter(range(len(valid)), [r["T_R"] for r in valid])
    else:
        axes[0].text(.5, .5, "No valid recovery samples", ha="center", transform=axes[0].transAxes)
    axes[0].set(ylabel="Recovery ms", xlabel=f"Valid runs (n={len(valid)})")
    for i, model in enumerate(sorted({r["model"] for r in recovery})):
        group = [r for r in recovery if r["model"] == model]
        for j, metric in enumerate(("N_query", "N_retry", "N_pending")):
            axes[1].bar(i*4+j, sum(r.get(metric) or 0 for r in group), label=f"{model}/{metric}")
    axes[1].set_ylabel("Calls / unresolved logical requests")
    if recovery:
        axes[1].legend()
    save(fig, "recovery")
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.7))
    sample = [r for r in reassignment if r["verdict"] == "PASS" and not r["warmup"]]
    for axis, metric in zip(axes, ("T_RA", "T_unavailable")):
        if sample:
            axis.scatter(range(len(sample)), [r[metric] for r in sample])
        else:
            axis.text(.5, .5, "No evidenced reassignment samples", ha="center", transform=axis.transAxes)
        axis.set(title=metric, ylabel="ms", xlabel=f"Contract observations (n={len(sample)})")
    save(fig, "reassignment")


def report_batch(batch):
    batch = Path(batch).resolve()
    manifest, checks = read_json(batch / "manifest.json"), read_json(batch / "checks.json")
    rows = [run_values(row, batch) for row in checks.get("runs", [])]
    if manifest.get("scope") == "experiment":
        completed = {r["schedule_index"] for r in checks.get("runs", [])}
        for index, item in enumerate(manifest["order"]):
            if index not in completed:
                config = manifest["configs"][item["config_index"]]
                case = item["case"]
                rows.append(dict(model=config["model"], suite=case["suite"],
                                 phase="warmup" if case["warmup"] else "main", run_id=None,
                                 test_id=case["test_id"], variant=case["variant"], repeat=case["repeat"],
                                 verdict="NOT_RUN", timing_valid=False,
                                 exclusion_reason="prerequisite unavailable or batch interrupted",
                                 **{k: None for k in METRICS}))
    else:
        for case in checks.get("not_run", []):
            rows.append(dict(model=manifest["model"], suite="CHECK", phase="main", run_id=None,
                             test_id=case["test_id"], variant=case["variant"], repeat=case["repeat"],
                             verdict="NOT_RUN", timing_valid=False,
                             exclusion_reason="prerequisite unavailable or batch interrupted",
                             **{k: None for k in METRICS}))
    summary = summary_rows(rows)
    write_csv(batch / "runs.csv", rows)
    write_csv(batch / "summary.csv", summary)
    reassignment = reassignment_rows(checks.get("runs", []))
    write_csv(batch / "reassignment.csv", reassignment)
    complexity = complexity_rows(manifest, batch)
    for row in checks.get("runs", []):
        calls = [read_json(p) for p in (batch / row["path"] / "evidence").glob("call-*.json")]
        for kind, count in (("REQUEST", len(calls)), ("RESPONSE", sum("status_code" in c for c in calls))):
            complexity.append(dict(model=row["model"], run_id=row["run_id"], metric="N_message",
                                   kind="HARNESS/"+kind, value=count, status="OBSERVED",
                                   evidence_ref=row["path"]+"/evidence/call-*.json",
                                   note="Observer HTTP calls; separate from business integration/protocol messages"))
        wrapper = batch / row["path"] / "evidence/implementation-events.json"
        if not wrapper.is_file():
            continue
        events = [parse_json(line) for line in read_json(wrapper)["body"].splitlines() if line.strip()]
        grouped = defaultdict(set)
        for event in events:
            if event["event_type"] in ("MESSAGE_SENT", "MESSAGE_RECEIVED"):
                grouped[event["message_category"], event["message_kind"]].add(event["message_id"])
        for (category, kind), messages in sorted(grouped.items()):
            # Observer HTTP calls and implementation calls made for them are separate hops.
            label = "HARNESS/IMPLEMENTATION" if category == "HARNESS" else category
            complexity.append(dict(model=row["model"], run_id=row["run_id"], metric="N_message",
                                   kind=label+"/"+kind, value=len(messages), status="OBSERVED",
                                   evidence_ref=str(wrapper.relative_to(batch)),
                                   note="Send/receive duplicates collapsed; categories kept separate"))
    for model in {r["model"] for r in complexity}:
        if not any(r["model"] == model and r["metric"] == "N_message" for r in complexity):
            complexity.append(dict(model=model, metric="N_message", kind=None, value=None,
                                   status="NOT_RUN", evidence_ref=None, note="Implementation message logs unavailable"))
    write_csv(batch / "complexity.csv", complexity)
    plot_figures(batch, rows, reassignment, synthetic=manifest.get("synthetic", False))
    lines = ["# Common runner results", "", "Scope: " + manifest.get("scope", "unknown"), "",
             "Final handoff eligible: " + str(checks.get("final_handoff_eligible", False)), "",
             "Preparation attempts / business bodies started: " + str(checks.get("preparation_attempts"))
             + " / " + str(checks.get("business_bodies_started")), "",
             "P50/P95: nearest-rank; duration unit ms; warmups excluded from latency statistics.",
             "Empty samples are null (blank CSV cells). Failed/incomplete attempts are retained.", "",
             "| Model | Suite | Phase | Metric | Attempts | n | P50 | P95 |", "|---|---|---|---|---:|---:|---:|---:|"]
    for row in summary:
        lines.append("| " + " | ".join(str(row[k]) for k in
                     ("model", "suite", "phase", "metric", "attempts", "n", "p50", "p95")) + " |")
    if manifest.get("synthetic"):
        lines.insert(2, "SYNTHETIC HARNESS SELF-TEST ONLY — not actual A/B measurements.")
    lines += ["", "## Evidence / limitations", "",
              "Raw inputs: manifest.json, checks.json, each run's events.jsonl and evidence/.",
              "Snapshot-only state is not proof of safety during concurrency.",
              "Platform integration complexity needs reviewed architecture, baseline and Z-change evidence; no total score is inferred.",
              "Actual model environment and log bindings remain required; synthetic self-tests are not model results."]
    lines += ["", "| Model | Complexity metric | Type | Value | Status | Evidence |",
              "|---|---|---|---:|---|---|"]
    for row in complexity:
        lines.append("| " + " | ".join(str(row.get(k)) for k in
                     ("model", "metric", "kind", "value", "status", "evidence_ref")) + " |")
    for blocker in checks.get("blockers", []):
        lines.append("- " + blocker)
    (batch / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    raw = {"manifest.json": hashlib.sha256((batch / "manifest.json").read_bytes()).hexdigest(),
           "checks.json": hashlib.sha256((batch / "checks.json").read_bytes()).hexdigest()}
    (batch / "report-provenance.json").write_text(json.dumps({"raw_inputs_sha256": raw,
        "report_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "matplotlib": importlib.metadata.version("matplotlib"),
        "calculation": "nearest-rank / same observer clock / per-run sums first"}, indent=2) + "\n")
    return rows, summary
