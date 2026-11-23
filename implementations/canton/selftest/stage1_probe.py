#!/usr/bin/env python3
"""Stage 1 checks against the running Canton (scripts/canton.sh start).

What it checks, through the JSON Ledger API of P_X and P_Y only:
  visibility   which party sees which contract on which participant
  submission   which party may submit on which participant, and the errors
  contention   one consuming choice submitted at the same time from P_X and P_Y
  propagation  time from a commit on P_X until P_Y returns the contract
  acs          ACS query time for growing numbers of active contracts

The DAR goes to every participant in ids.json: on b1-xyz P_Z also hosts
BANK_C, and a party's package must be vetted on all of its participants.

Every contract carries a fresh probe id, and all of them are archived at the
end (unless --keep), so reruns do not see each other's contracts.
The report goes to implementations/canton/logs/stage1/<probe id>.json.
Timings are reference values, not measurements under spec/measurement.md.

Standard library only. Exit status 0 when every expectation held, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

IMPL_DIR = Path(__file__).resolve().parent.parent
DAR = IMPL_DIR / "daml/stage1-probe/.daml/dist/stage1-probe-0.1.0.dar"
PKG = "#stage1-probe:Stage1Probe"
NOTE, GRANT, RECEIPT = f"{PKG}:Note", f"{PKG}:Grant", f"{PKG}:Receipt"
CLEANUP_SETTLE_S = 30  # how long archives may take to reach the other participant
USER_ID = "stage1-probe"
HTTP_TIMEOUT = 120


def default_state_dir() -> Path | None:
    """$CANTON_STATE_DIR from scripts/env.sh, the one place its default is set."""
    value = os.environ.get("CANTON_STATE_DIR")
    return Path(value) if value else None


class Reply:
    def __init__(self, status: int, body, ms: float, size: int):
        self.status, self.body, self.ms, self.size = status, body, ms, size

    @property
    def code(self) -> str | None:
        return self.body.get("code") if isinstance(self.body, dict) else None

    def brief(self) -> dict:
        out = {"http": self.status, "ms": round(self.ms, 1)}
        if self.status != 200 and isinstance(self.body, dict):
            out["code"] = self.code
            ctx = self.body.get("context") or {}
            if "error_id" in ctx:
                out["error_id"] = ctx["error_id"]
            cause = self.body.get("cause")
            if isinstance(cause, str):
                out["cause"] = cause[:240]
        elif self.status != 200:
            out["body"] = str(self.body)[:240]
        return out


class Participant:
    def __init__(self, name: str, base: str):
        self.name, self.base = name, base.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        body=None,
        raw: bytes | None = None,
        content_type: str = "application/json",
    ) -> Reply:
        data = (
            raw
            if raw is not None
            else (json.dumps(body).encode() if body is not None else None)
        )
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": content_type},
        )
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                payload = r.read()
                status = r.status
        except urllib.error.HTTPError as e:
            payload, status = e.read(), e.code
        ms = (time.monotonic() - t0) * 1000
        try:
            parsed = json.loads(payload) if payload else {}
        except ValueError:
            parsed = payload.decode(errors="replace")
        return Reply(status, parsed, ms, len(payload))

    def submit(self, act_as: list[str], commands: list[dict], command_id: str) -> Reply:
        return self.request(
            "POST",
            "/v2/commands/submit-and-wait-for-transaction",
            {
                "commands": {
                    "commands": commands,
                    "commandId": command_id,
                    "userId": USER_ID,
                    "actAs": act_as,
                }
            },
        )

    def ledger_end(self) -> int:
        r = self.request("GET", "/v2/state/ledger-end")
        assert r.status == 200, r.brief()
        return r.body["offset"]

    @staticmethod
    def event_format(party: str, template: str) -> dict:
        tf = {"templateId": template, "includeCreatedEventBlob": False}
        return {
            "filtersByParty": {
                party: {"cumulative": [{"identifierFilter": {"TemplateFilter": {"value": tf}}}]}
            },
            "verbose": False,
        }

    @staticmethod
    def created_events(items: list) -> list[dict]:
        out = []
        for item in items:
            ac = (item.get("contractEntry") or {}).get("JsActiveContract")
            if ac:
                out.append(ac["createdEvent"])
        return out

    def acs_full(self, party: str, template: str) -> tuple[Reply, list[dict]]:
        """One-shot ACS. The node refuses it (413) above 200 matching contracts."""
        body = {"activeAtOffset": self.ledger_end(), "eventFormat": self.event_format(party, template)}
        r = self.request("POST", "/v2/state/active-contracts", body)
        return r, self.created_events(r.body) if r.status == 200 else []

    def acs(self, party: str, template: str, page_size: int = 200,
            strict: bool = True) -> tuple[Reply, list[dict], dict]:
        """Paged ACS at one offset. Returns the last reply, all events and page stats.

        A refused page raises unless strict is False: an error must never be
        read as "no contracts" (an earlier version did, and its cleanup then
        archived nothing while reporting nothing left).
        """
        body = {"activeAtOffset": self.ledger_end(), "eventFormat": self.event_format(party, template),
                "maxPageSize": page_size}
        events, pages, ms, size = [], 0, 0.0, 0
        while True:
            r = self.request("POST", "/v2/state/active-contracts-page", body)
            pages += 1
            ms += r.ms
            size += r.size
            if r.status != 200:
                if strict:
                    raise RuntimeError(f"{self.name} ACS page refused: {r.brief()}")
                break
            events += self.created_events(r.body.get("activeContracts") or [])
            token = r.body.get("nextPageToken")
            if not token:
                break
            body["pageToken"] = token
        return r, events, {"pages": pages, "ms": ms, "bytes": size}

    def sees(self, party: str, contract_id: str) -> Reply:
        return self.request(
            "POST",
            "/v2/events/events-by-contract-id",
            {
                "contractId": contract_id,
                "eventFormat": {"filtersByParty": {party: {}}, "verbose": False},
            },
        )


def created(reply: Reply, template_suffix: str) -> str:
    """Contract id of the first created event of the given template."""
    for ev in reply.body["transaction"]["events"]:
        ce = ev.get("CreatedEvent")
        if ce and ce["templateId"].endswith(template_suffix):
            return ce["contractId"]
    raise AssertionError(f"no {template_suffix} created: {reply.body}")


def create_cmd(template: str, args: dict) -> dict:
    return {"CreateCommand": {"templateId": template, "createArguments": args}}


def exercise_cmd(template: str, cid: str, choice: str, arg: dict) -> dict:
    return {
        "ExerciseCommand": {
            "templateId": template,
            "contractId": cid,
            "choice": choice,
            "choiceArgument": arg,
        }
    }


class Probe:
    def __init__(
        self, ids: dict, rounds: int, acs_levels: list[int], prop_samples: int
    ):
        api = ids["json_api"]
        self.px, self.py = (
            Participant("P_X", api["P_X"]),
            Participant("P_Y", api["P_Y"]),
        )
        # Only for the DAR upload; the checks run on P_X and P_Y.
        self.every = [Participant(name, url) for name, url in api.items()]
        self.A, self.B, self.C = (
            ids["parties"][k] for k in ("BANK_A", "BANK_B", "BANK_C")
        )
        self.alias = {self.A: "BANK_A", self.B: "BANK_B", self.C: "BANK_C"}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.probe_id = f"stage1-{stamp}-{secrets.token_hex(2)}"
        self.rounds, self.acs_levels, self.prop_samples = (
            rounds,
            acs_levels,
            prop_samples,
        )
        self.checks: list[dict] = []
        self.report: dict = {
            "probe_id": self.probe_id,
            "ids": ids,
            "checks": self.checks,
        }
        self._n = 0

    # -- helpers ---------------------------------------------------------
    def cid(self, name: str) -> str:
        self._n += 1
        return f"{self.probe_id}-{self._n:04d}-{name}"

    def check(self, cid: str, what: str, expected: str, ok: bool, observed) -> bool:
        self.checks.append(
            {
                "id": cid,
                "what": what,
                "expected": expected,
                "pass": bool(ok),
                "observed": observed,
            }
        )
        print(
            f"  [{'PASS' if ok else 'FAIL'}] {cid} {what}: {json.dumps(observed, ensure_ascii=False)}"
        )
        return ok

    def note(self, owner: str, viewers: list[str], tag: str) -> dict:
        return create_cmd(
            NOTE,
            {"owner": owner, "viewers": viewers, "probeId": self.probe_id, "tag": tag},
        )

    def grant(self, issuer: str, holder: str, tag: str, qty: int = 5) -> dict:
        return create_cmd(
            GRANT,
            {
                "issuer": issuer,
                "holder": holder,
                "probeId": self.probe_id,
                "tag": tag,
                "qty": str(qty),
            },
        )

    def use(self, cid: str, need: int, via: str) -> dict:
        return exercise_cmd(GRANT, cid, "Use", {"need": str(need), "via": via})

    def mine(self, events: list[dict]) -> list[dict]:
        return [
            e for e in events if e["createArgument"].get("probeId") == self.probe_id
        ]

    def visible(self, p: Participant, party: str, template: str, tag: str) -> bool:
        _, evs, _ = p.acs(party, template)
        return any(e["createArgument"]["tag"] == tag for e in self.mine(evs))

    def wait_visible(
        self, p: Participant, party: str, contract_id: str, limit_s: float = 30
    ) -> float | None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < limit_s:
            if p.sees(party, contract_id).status == 200:
                return (time.monotonic() - t0) * 1000
            time.sleep(0.02)
        return None

    # -- steps -----------------------------------------------------------
    def upload(self) -> None:
        print("== upload DAR to " + ", ".join(p.name for p in self.every))
        raw = DAR.read_bytes()
        for p in self.every:
            r = p.request(
                "POST",
                "/v2/dars?vetAllPackages=true",
                raw=raw,
                content_type="application/octet-stream",
            )
            self.check(
                f"dar-{p.name}",
                f"upload {DAR.name} to {p.name}",
                "200",
                r.status == 200,
                r.brief(),
            )

    def visibility_and_submission(self) -> None:
        A, B, C, px, py = self.A, self.B, self.C, self.px, self.py
        print("== contracts for visibility")
        r = px.submit([A, B], [self.grant(A, B, "g-ab")], self.cid("g-ab"))
        self.report["first_command_ms"] = round(r.ms, 1)
        self.check(
            "c1",
            "P_X creates Grant(A->B), actAs [A,B] (first command after start)",
            "200",
            r.status == 200,
            r.brief(),
        )
        g_ab = created(r, ":Grant")
        r = px.submit([A], [self.note(A, [], "n-a")], self.cid("n-a"))
        self.check(
            "c2",
            "P_X creates Note(A, no viewers), actAs [A]",
            "200",
            r.status == 200,
            r.brief(),
        )
        r = py.submit([C], [self.note(C, [], "n-c")], self.cid("n-c"))
        self.check(
            "c3",
            "P_Y creates Note(C, no viewers), actAs [C]",
            "200",
            r.status == 200,
            r.brief(),
        )
        r = py.submit([C, B], [self.grant(C, B, "g-cb")], self.cid("g-cb"))
        self.check(
            "c4",
            "P_Y creates Grant(C->B), actAs [C,B]",
            "200",
            r.status == 200,
            r.brief(),
        )
        g_cb = created(r, ":Grant")

        # Contracts created on the other participant may arrive a moment later.
        for p, party, cid_ in ((py, B, g_ab), (px, B, g_cb)):
            self.wait_visible(p, party, cid_)

        print("== visibility")
        cases = [
            (
                "v1",
                py,
                B,
                GRANT,
                "g-ab",
                True,
                "P_Y as BANK_B sees Grant(A->B) made on P_X",
            ),
            (
                "v2",
                py,
                C,
                GRANT,
                "g-ab",
                False,
                "P_Y as BANK_C does not see Grant(A->B)",
            ),
            ("v3", py, B, NOTE, "n-a", False, "P_Y as BANK_B does not see Note(A)"),
            (
                "v4",
                px,
                B,
                GRANT,
                "g-cb",
                True,
                "P_X as BANK_B sees Grant(C->B) made on P_Y",
            ),
            (
                "v5",
                px,
                A,
                GRANT,
                "g-cb",
                False,
                "P_X as BANK_A does not see Grant(C->B)",
            ),
            ("v6", px, B, NOTE, "n-c", False, "P_X as BANK_B does not see Note(C)"),
        ]
        for cid_, p, party, tpl, tag, want, what in cases:
            got = self.visible(p, party, tpl, tag)
            self.check(
                cid_,
                what,
                "visible" if want else "not visible",
                got == want,
                {"visible": got},
            )
        r, evs, _ = px.acs(C, NOTE, strict=False)
        self.check(
            "v7",
            "P_X ACS for BANK_C, which P_X does not host",
            "no BANK_C contracts",
            r.status != 200 or not self.mine(evs),
            {**r.brief(), "contracts": len(self.mine(evs))},
        )

        print("== submission rights")
        r = py.submit([A], [self.note(A, [], "s1")], self.cid("s1"))
        self.check(
            "s1",
            "P_Y submits for BANK_A (hosted only on P_X)",
            "rejected",
            r.status != 200,
            r.brief(),
        )
        r = px.submit([C], [self.note(C, [], "s2")], self.cid("s2"))
        self.check(
            "s2",
            "P_X submits for BANK_C (not hosted on P_X)",
            "rejected",
            r.status != 200,
            r.brief(),
        )
        r = px.submit([A, C], [self.grant(A, C, "s3")], self.cid("s3"))
        self.check(
            "s3",
            "P_X submits Grant(A->C) with actAs [A,C]",
            "rejected",
            r.status != 200,
            r.brief(),
        )
        r = py.submit([B], [self.use(g_ab, 1, "P_Y")], self.cid("s4"))
        ok = r.status == 200
        self.check(
            "s4",
            "P_Y uses Grant(A->B) as BANK_B (contract made on P_X)",
            "200",
            ok,
            r.brief(),
        )
        if ok:
            rc = created(r, ":Receipt")
            ms = self.wait_visible(px, A, rc)
            self.check(
                "s4b",
                "Receipt of s4 reaches BANK_A on P_X",
                "visible",
                ms is not None,
                {"wait_ms": None if ms is None else round(ms, 1)},
            )
        r = px.submit([B], [self.use(g_cb, 1, "P_X")], self.cid("s5"))
        self.check(
            "s5",
            "P_X uses Grant(C->B) as BANK_B (contract made on P_Y)",
            "200",
            r.status == 200,
            r.brief(),
        )

        r = px.submit([A, B], [self.grant(A, B, "g3")], self.cid("g3"))
        g3 = created(r, ":Grant")
        self.wait_visible(py, B, g3)
        r = py.submit([C], [self.use(g3, 1, "P_Y")], self.cid("s6"))
        self.check(
            "s6",
            "P_Y uses Grant(A->B) as BANK_C (not a stakeholder)",
            "rejected",
            r.status != 200,
            r.brief(),
        )
        r = px.submit([A], [self.use(g3, 1, "P_X")], self.cid("s7"))
        self.check(
            "s7",
            "P_X uses Grant(A->B) as BANK_A (signatory, not controller)",
            "rejected",
            r.status != 200,
            r.brief(),
        )
        r = py.submit([B], [self.use(g3, 9, "P_Y")], self.cid("s8"))
        err = r.brief()
        self.check(
            "s8",
            "P_Y uses Grant(A->B, qty 5) as BANK_B with need 9",
            "400 DAML_FAILURE, error_id RIGHT_UNAVAILABLE",
            r.status == 400 and err.get("error_id") == "RIGHT_UNAVAILABLE",
            err,
        )
        still = py.sees(B, g3)
        archived = isinstance(still.body, dict) and bool(still.body.get("archived"))
        self.check(
            "s8b",
            "Grant of s8 is still active after the rejection",
            "active",
            still.status == 200 and not archived,
            {"http": still.status, "archived": archived},
        )

    def contention(self) -> None:
        print(
            f"== contention: {self.rounds} rounds, BANK_B uses one Grant from P_X and P_Y at once"
        )
        A, B, px, py = self.A, self.B, self.px, self.py
        rounds = []
        for i in range(self.rounds):
            tag = f"race-{i:02d}"
            g = created(
                px.submit([A, B], [self.grant(A, B, tag)], self.cid(tag)), ":Grant"
            )
            self.wait_visible(py, B, g)
            gate = threading.Barrier(2)
            out: dict[str, Reply] = {}

            def go(p: Participant) -> None:
                command_id = self.cid(f"{tag}-{p.name}")
                gate.wait()
                out[p.name] = p.submit([B], [self.use(g, 1, p.name)], command_id)

            ts = [threading.Thread(target=go, args=(p,)) for p in (px, py)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            winners = [n for n, r in out.items() if r.status == 200]
            # A winning use on P_Y reaches P_X only after propagation, so wait
            # for every winner's Receipt on P_X before counting there.
            unseen = [
                n for n in winners
                if self.wait_visible(px, A, created(out[n], ":Receipt")) is None
            ]
            _, receipts, _ = px.acs(A, RECEIPT)
            n_receipts = sum(
                1 for e in self.mine(receipts) if e["createArgument"]["tag"] == tag
            )
            rounds.append(
                {
                    "round": i,
                    "winners": winners,
                    "receipts": n_receipts,
                    **({"receipt_not_on_P_X": unseen} if unseen else {}),
                    **{n: r.brief() for n, r in out.items()},
                }
            )
        one_winner = all(len(r["winners"]) == 1 for r in rounds)
        one_receipt = all(r["receipts"] == 1 for r in rounds)
        loser_codes: dict[str, int] = {}
        wins = {"P_X": 0, "P_Y": 0}
        for r in rounds:
            for n in ("P_X", "P_Y"):
                if n in r["winners"]:
                    wins[n] += 1
                else:
                    key = f"{r[n]['http']} {r[n].get('code')}"
                    loser_codes[key] = loser_codes.get(key, 0) + 1
        summary = {"rounds": len(rounds), "wins": wins, "loser_codes": loser_codes}
        self.report["contention"] = {"summary": summary, "rounds": rounds}
        self.check(
            "r1",
            "exactly one of the two concurrent uses commits, every round",
            "1 winner per round",
            one_winner,
            summary,
        )
        self.check(
            "r2",
            "exactly one Receipt per round on P_X",
            "1 receipt per round",
            one_receipt,
            {"receipts": [r["receipts"] for r in rounds]},
        )

    def propagation(self) -> None:
        print(
            f"== propagation: {self.prop_samples} Notes(A, viewer B) made on P_X, wait on P_Y"
        )
        samples, submit_ms = [], []
        for i in range(self.prop_samples):
            r = self.px.submit(
                [self.A],
                [self.note(self.A, [self.B], f"prop-{i}")],
                self.cid(f"prop-{i}"),
            )
            submit_ms.append(r.ms)
            ms = self.wait_visible(self.py, self.B, created(r, ":Note"))
            samples.append(ms)
        ok = all(s is not None for s in samples)
        observed = {"submit_ms": self.stat(submit_ms)}
        if ok:
            observed["visible_on_P_Y_after_ms"] = self.stat(samples)
        self.report["propagation"] = observed
        self.check(
            "p1",
            "every Note committed on P_X becomes visible on P_Y",
            "all visible",
            ok,
            observed,
        )

    def acs_timing(self) -> None:
        print(f"== ACS query time on P_Y as BANK_B, template Note, levels {self.acs_levels}")
        made, levels = 0, []
        for level in self.acs_levels:
            while made < level:
                batch = min(100, level - made)
                cmds = [self.note(self.A, [self.B], f"acs-{made + k}") for k in range(batch)]
                r = self.px.submit([self.A], cmds, self.cid(f"acs-{made}"))
                assert r.status == 200, r.brief()
                made += batch
            # Wait until P_Y has all of them before timing.
            deadline = time.monotonic() + 60
            while True:
                _, evs, _ = self.py.acs(self.B, NOTE)
                n = sum(1 for e in self.mine(evs) if e["createArgument"]["tag"].startswith("acs-"))
                if n >= made or time.monotonic() > deadline:
                    break
                time.sleep(0.2)
            row: dict = {"probe_notes": made}
            full = [self.py.acs_full(self.B, NOTE) for _ in range(5)]
            r0 = full[-1][0]
            row["full"] = {"http": r0.status, "code": r0.code if r0.status != 200 else None,
                           "returned": len(full[-1][1]) if r0.status == 200 else None,
                           "ms": self.stat([r.ms for r, _ in full])}
            paged = [self.py.acs(self.B, NOTE) for _ in range(5)]
            rp, evs, st = paged[-1]
            row["paged"] = {"http": rp.status, "code": rp.code if rp.status != 200 else None,
                            "page_size": 200, "pages": st["pages"], "returned": len(evs),
                            "bytes": st["bytes"], "ms": self.stat([s["ms"] for _, _, s in paged])}
            levels.append(row)
            print(f"  level {made}: {json.dumps(row)}")
        self.report["acs"] = levels
        self.check("a1", "paged ACS answers at every level", "200 at every level",
                   all(lv["paged"]["http"] == 200 for lv in levels),
                   [lv["paged"]["http"] for lv in levels])
        over = [lv for lv in levels if (lv["full"]["returned"] or 0) > 200 or lv["full"]["http"] != 200]
        self.check("a2", "one-shot ACS is refused once more than 200 contracts match",
                   "413 JSON_API_MAXIMUM_LIST_ELEMENTS_NUMBER_REACHED",
                   all(lv["full"]["http"] == 413 for lv in over),
                   [[lv["probe_notes"], lv["full"]["http"]] for lv in levels])

    @staticmethod
    def stat(xs: list[float]) -> dict:
        return {"median": round(statistics.median(xs), 1), "max": round(max(xs), 1)}

    def cleanup(self, sweep: bool) -> None:
        """Archive this probe's contracts; with sweep, every stage1-probe contract."""
        print("== cleanup: archive " + ("every stage1-probe contract" if sweep
                                        else "every contract of this probe"))
        pick = (lambda evs: evs) if sweep else self.mine
        for p, act_as, party in ((self.px, [self.A, self.B], self.A),
                                 (self.py, [self.C, self.B], self.C)):
            for tpl in (NOTE, GRANT, RECEIPT):
                _, evs, _ = p.acs(party, tpl)
                cids = [e["contractId"] for e in pick(evs)]
                for k in range(0, len(cids), 100):
                    cmds = [exercise_cmd(tpl, c, "Archive", {}) for c in cids[k:k + 100]]
                    r = p.submit(act_as, cmds, self.cid(f"archive-{p.name}-{k}"))
                    assert r.status == 200, r.brief()
        # BANK_B is hosted on both participants: an archive submitted on one
        # reaches the other's copy only after propagation, so poll for zero.
        deadline = time.monotonic() + CLEANUP_SETTLE_S
        while True:
            left = 0
            for p, party in ((self.px, self.A), (self.px, self.B), (self.py, self.B), (self.py, self.C)):
                for tpl in (NOTE, GRANT, RECEIPT):
                    left += len(pick(p.acs(party, tpl)[1]))
            if left == 0 or time.monotonic() > deadline:
                break
            time.sleep(0.2)
        self.check("z1", "no active probe contract remains" + (" (any probe id)" if sweep else ""),
                   "0", left == 0, {"left": left})


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--state-dir", type=Path, default=default_state_dir())
    ap.add_argument("--rounds", type=int, default=10, help="contention rounds")
    ap.add_argument("--acs-levels", default="0,100,500,1000,2000")
    ap.add_argument("--propagation-samples", type=int, default=10)
    ap.add_argument(
        "--keep", action="store_true", help="do not archive the probe contracts"
    )
    ap.add_argument("--sweep", action="store_true",
                    help="also archive stage1-probe contracts left by earlier runs")
    args = ap.parse_args()

    if not DAR.exists():
        sys.exit(f"missing {DAR}; build it with: (cd {DAR.parents[2]} && dpm build)")
    if args.state_dir is None:
        sys.exit("CANTON_STATE_DIR not set (source scripts/env.sh) and no --state-dir")
    ids = json.loads((args.state_dir / "ids.json").read_text())
    probe = Probe(
        ids,
        args.rounds,
        [int(x) for x in args.acs_levels.split(",")],
        args.propagation_samples,
    )
    probe.report["started_at"] = datetime.now(timezone.utc).isoformat()
    probe.report["versions"] = {
        k: os.environ.get(k)
        for k in ("CANTON_VERSION", "CANTON_SDK_VERSION", "CANTON_DPM_VERSION")
    }
    print(f"probe {probe.probe_id}")
    try:
        probe.upload()
        probe.visibility_and_submission()
        probe.contention()
        probe.propagation()
        probe.acs_timing()
    except BaseException as e:
        probe.report["aborted"] = f"{type(e).__name__}: {e}"[:500]
        raise
    finally:
        if not args.keep:
            try:
                probe.cleanup(args.sweep)
            except Exception as e:
                probe.check("z1", "cleanup finished", "no error", False,
                            {"error": f"{type(e).__name__}: {e}"[:500]})
        probe.report["finished_at"] = datetime.now(timezone.utc).isoformat()
        out = IMPL_DIR / "logs/stage1" / f"{probe.probe_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(probe.report, indent=2, ensure_ascii=False) + "\n")
        failed = [c["id"] for c in probe.checks if not c["pass"]]
        print(f"report {out}")
        print(
            f"{len(probe.checks) - len(failed)}/{len(probe.checks)} checks passed"
            + (f"; failed: {', '.join(failed)}" if failed else "")
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
