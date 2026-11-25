"""Stage 4 prototype integrator. One change (or one declared series) per run, replayed under
policies A, D and E against separate locks so the outcomes are comparable.

  stage     --module M --lock PATH --out PATH      write a build stage for M from a lock
  integrate --module M --kind module|tick|requeue  process one event under every policy

Rules (SA, 2026-11-25):
  - Each run computes current green plus this change when it starts; the event payload only
    names the module, and its current develop head is used.
  - Quarantine follows the blame. A and E quarantine the module whose change is in the run; a
    later version of it that passes clears it (a revert counts). D advances the change and drops
    the failing dependent and its dependents; a later successful rebuild returns it.
  - E holds as A for HOLD_MINUTES, then applies D to the held change.
  - Series come only from "Depends on owner/repo#N" lines in the merged PR body, matched
    exactly. Nothing integrates until every member is merged; after 24 h unresolved, notify only.
"""
import argparse
import time
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

OWNER = "BuroHappoldEngineeringSandbox"
CORE: set[str] = set()  # Stage 4b: core repos are ordinary modules
POLICIES = ("A", "D", "E")
HOLD_MINUTES = 15
NOTIFY_HOURS = 24
ROOT = Path(__file__).resolve().parent.parent
LOCKS = ROOT / "locks"
OUT = Path(os.environ.get("RUNNER_TEMP", ".")) / "proto-builds"
DEPENDS = re.compile(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#(\d+)")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def gh(path: str, raw: bool = False):
    cmd = ["gh", "api", path]
    if raw:
        cmd += ["-H", "Accept: application/vnd.github.raw"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {r.stdout.strip()} {r.stderr.strip()}")
    return r.stdout if raw else json.loads(r.stdout)


def head_of(module: str) -> str:
    return gh(f"repos/{OWNER}/{module}/branches/develop")["commit"]["sha"]


_deps_cache: dict[tuple[str, str], list[str]] = {}


def deps_of(module: str, sha: str) -> list[str]:
    key = (module, sha)
    if key not in _deps_cache:
        text = gh(f"repos/{OWNER}/{module}/contents/dependencies.txt?ref={sha}", raw=True)
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line or "/" not in line:
                continue
            name = line.split("/", 1)[1]
            if name not in CORE:
                out.append("proto-" + name)
        _deps_cache[key] = out
    return _deps_cache[key]


def version_of(sha: str, core_tag: str | None, dep_versions: dict[str, str]) -> str:
    basis = [f"core={core_tag or 'none'}"] + [f"{k}={dep_versions[k]}" for k in sorted(dep_versions)]
    return sha[:8] + "-" + hashlib.sha256(";".join(basis).encode()).hexdigest()[:8]


def load(policy: str):
    d = LOCKS / policy
    lock = json.loads((d / "lock.json").read_text())
    state = json.loads((d / "state.json").read_text())
    return lock, state


def save(policy: str, lock, state):
    d = LOCKS / policy
    (d / "lock.json").write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    (d / "state.json").write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def stage_for(module: str, sha: str, lock: dict, overlay: dict) -> dict | None:
    """Stage built against the lock with this run's new builds overlaid. None if a dep is missing."""
    mods = {**lock["modules"], **overlay}
    deps = {}
    for dep in deps_of(module, sha):
        if dep not in mods:
            return None
        e = mods[dep]
        deps[dep] = {"version": e["version"], "url": e["url"], "sha256": e["sha256"]}
    return {"core": lock["core"], "deps": deps}


_built: dict[str, dict] = {}


BUILDS: list[dict] = []


def core_tag(stage: dict) -> str | None:
    return stage["core"]["tag"] if stage.get("core") else None


def build(module: str, sha: str, stage: dict) -> dict:
    """Returns a lock entry plus ok. Reuses the module repo's own release when it was built
    against exactly this stage; otherwise builds here and publishes on proto-lock."""
    dep_versions = {k: v["version"] for k, v in stage["deps"].items()}
    version = version_of(sha, core_tag(stage), dep_versions)
    if version in _built:
        return _built[version]
    entry = {"sha": sha, "version": version,
             "built_against": {"core": core_tag(stage), "deps": dep_versions}}
    try:
        rel = gh(f"repos/{OWNER}/{module}/releases/tags/m-{version}")
        manifest_asset = next(a for a in rel["assets"] if a["name"] == "manifest.json")
        with urllib.request.urlopen(manifest_asset["browser_download_url"]) as resp:
            manifest = json.loads(resp.read().decode("utf-8-sig"))
        if manifest.get("ok") and manifest["version"] == version:
            BUILDS.append({"module": module, "version": version, "seconds": 0, "ok": True, "source": "module-release"})
            entry.update(ok=True, source="module-release",
                         url=f"https://github.com/{OWNER}/{module}/releases/download/m-{version}/{module}.zip",
                         sha256=manifest["payload_sha256"])
            _built[version] = entry
            return entry
    except RuntimeError:
        pass  # no module release for this exact version: build it here
    out = OUT / version
    sfile = out.with_suffix(".stage.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    sfile.write_text(json.dumps(stage))
    started = time.monotonic()
    r = subprocess.run(["pwsh", "-NoProfile", "-File", str(ROOT / "scripts" / "build.ps1"),
                        "-Module", module, "-Sha", sha, "-Stage", str(sfile), "-OutDir", str(out)])
    seconds = round(time.monotonic() - started, 1)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8-sig"))
    if r.returncode != 0 or not manifest.get("ok"):
        BUILDS.append({"module": module, "version": version, "seconds": seconds, "ok": False, "source": "integrator-build"})
        entry.update(ok=False, reason=manifest.get("reason", f"exit {r.returncode}"))
        _built[version] = entry
        return entry
    tag = f"a-{module}-{version}"
    rel = subprocess.run(["gh", "release", "create", tag, str(out / f"{module}.zip"), str(out / "manifest.json"),
                          "-R", f"{OWNER}/proto-lock", "--title", tag,
                          "--notes", f"Rebuilt by the integrator: {module}@{sha} against {json.dumps(dep_versions)}, core {core_tag(stage)}"],
                         capture_output=True, text=True)
    if rel.returncode != 0 and "already exists" not in rel.stderr:
        raise RuntimeError(f"release {tag} failed: {rel.stderr.strip()}")
    BUILDS.append({"module": module, "version": version, "seconds": seconds, "ok": True, "source": "integrator-build"})
    entry.update(ok=True, source="integrator-rebuild",
                 url=f"https://github.com/{OWNER}/proto-lock/releases/download/{tag}/{module}.zip",
                 sha256=manifest["payload_sha256"])
    _built[version] = entry
    return entry


def pr_for(module: str, sha: str) -> dict | None:
    prs = gh(f"repos/{OWNER}/{module}/commits/{sha}/pulls")
    merged = [p for p in prs if p.get("merged_at")]
    if not merged:
        return None
    p = merged[0]
    return {"repo": module, "number": p["number"], "body": p.get("body") or ""}


def depends_links(body: str) -> list[tuple[str, str, int]]:
    links = []
    for line in body.splitlines():
        if line.strip().lower().startswith("depends on"):
            links += [(o, r, int(n)) for o, r, n in DEPENDS.findall(line)]
    return links


def pr_merged(owner: str, repo: str, n: int) -> bool:
    return bool(gh(f"repos/{owner}/{repo}/pulls/{n}").get("merged_at"))


def dependents(lock_modules: dict, roots: set[str], extra: dict[str, str]) -> list[str]:
    """Modules in the lock (or extra: dropped ones to retry) that transitively depend on roots,
    in build order."""
    universe = {m: e["sha"] for m, e in lock_modules.items()}
    universe.update(extra)
    rev: dict[str, set[str]] = {}
    for m, sha in universe.items():
        for d in deps_of(m, sha):
            rev.setdefault(d, set()).add(m)
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        for x in rev.get(stack.pop(), ()):
            if x not in seen and x not in roots:
                seen.add(x)
                stack.append(x)
    order: list[str] = []

    def visit(m):
        if m in order:
            return
        for d in deps_of(m, universe[m]):
            if d in seen:
                visit(d)
        order.append(m)
    for m in sorted(seen):
        visit(m)
    return order


def topo(changes: dict[str, str]) -> list[str]:
    order: list[str] = []

    def visit(m):
        if m in order:
            return
        for d in deps_of(m, changes[m]):
            if d in changes:
                visit(d)
        order.append(m)
    for m in sorted(changes):
        visit(m)
    return order


def attempt(policy: str, lock: dict, state: dict, changes: dict[str, str], blame: list[dict],
            mode: str, held: frozenset = frozenset()) -> dict:
    """Integrate one change unit. mode: 'hold' (A, and E before T) or 'drop' (D, and E after T).
    held: upstreams in this unit whose change is quarantined (a joint run with fixes)."""
    overlay: dict[str, dict] = {}
    record = {"t": now(), "policy": policy, "mode": mode, "changes": changes, "blame": blame}
    for m in topo(changes):
        st = stage_for(m, changes[m], lock, overlay)
        if st is None:
            record["outcome"] = "deferred: upstream not in lock"
            state.setdefault("pending", [])
            for x in changes:
                if x not in state["pending"]:
                    state["pending"].append(x)
            return record
        e = build(m, changes[m], st)
        if not e["ok"]:
            # The change itself does not build. In a joint run only the failing fix is blamed;
            # the held upstream stays held as it was.
            culprits = [m] if held and m not in held else list(changes)
            for x in culprits:
                state["quarantine"].setdefault(x, {"sha": changes[x], "since": now(), "reason": f"own build failed: {m}", "blame": blame})
                state["quarantine"][x]["sha"] = changes[x]
                state["pending_on_upstream"].pop(x, None)
            record.update(outcome="rejected: change does not build", failing=[m])
            return record
        overlay[m] = e
    retry = {m: d["sha"] for m, d in state.get("dropped", {}).items()}
    failing: list[str] = []
    excluded: set[str] = set()
    for x in dependents(lock["modules"], set(changes), retry):
        if x in excluded:
            continue
        sha = lock["modules"][x]["sha"] if x in lock["modules"] else retry[x]
        st = stage_for(x, sha, lock, overlay)
        e = build(x, sha, st) if st is not None else {"ok": False, "reason": "upstream missing"}
        if e["ok"]:
            overlay[x] = e
            continue
        failing.append(x)
        if mode == "hold":
            break
        excluded |= {x} | set(dependents({**lock["modules"], **overlay}, {x}, retry))
    if failing and mode == "hold" and held:
        # A dependent whose code did not change still breaks: blame stays on the held upstream,
        # and the fixes wait for it, unblamed.
        for u in held:
            state["quarantine"][u]["reason"] = f"breaks {failing[0]}"
        for x in changes:
            if x not in held:
                state["pending_on_upstream"][x] = {"sha": changes[x], "upstream": sorted(held), "since": now()}
        record.update(outcome=f"held: upstream still breaks {failing[0]}; fixes pending on upstream",
                      failing=failing, pending_on_upstream=sorted(set(changes) - held))
        return record
    if failing and mode == "hold":
        for m in changes:
            state["quarantine"][m] = {"sha": changes[m], "since": state["quarantine"].get(m, {}).get("since", now()),
                                      "reason": f"breaks {failing[0]}", "blame": blame}
        record.update(outcome="held: change breaks a dependent", failing=failing)
        return record
    for x in excluded:
        sha = lock["modules"][x]["sha"] if x in lock["modules"] else retry[x]
        lock["modules"].pop(x, None)
        overlay.pop(x, None)
        state["dropped"].setdefault(x, {"sha": sha, "since": now(), "because": blame})
    for m, e in overlay.items():
        lock["modules"][m] = e
        state["dropped"].pop(m, None)
    cleared = [m for m in changes if m in state["quarantine"]]
    for m in cleared:
        del state["quarantine"][m]
    for m in changes:
        state["pending_on_upstream"].pop(m, None)
    for m in changes:
        state["integrated"][m] = changes[m]
    record.update(outcome="advanced" if not failing else "advanced, dependents dropped",
                  advanced=sorted(overlay), dropped=sorted(excluded), cleared=cleared, failing=failing)
    return record


def upstreams(module: str, sha: str, lock: dict) -> set[str]:
    """Transitive upstream modules of module at sha, via the lock's versions of each upstream."""
    seen: set[str] = set()
    stack = [(module, sha)]
    while stack:
        m, s2 = stack.pop()
        for d in deps_of(m, s2):
            if d not in seen:
                seen.add(d)
                if d in lock["modules"]:
                    stack.append((d, lock["modules"][d]["sha"]))
    return seen


def series_unit(state: dict, module: str, sha: str) -> tuple[dict[str, str] | None, list[dict], str]:
    """Return (changes, blame, note). None changes means wait."""
    pr = pr_for(module, sha)
    blame = [{"repo": module, "sha": sha, "pr": pr["number"] if pr else None}]
    waiting = state.setdefault("waiting", {})
    members = {module: sha}
    if pr:
        links = depends_links(pr["body"])
        unmerged = [f"{o}/{r}#{n}" for o, r, n in links if not pr_merged(o, r, n)]
        if unmerged:
            waiting[f"{module}#{pr['number']}"] = {"module": module, "since": now(), "unmerged": unmerged, "notified": False}
            return None, blame, f"series waiting on {', '.join(unmerged)}"
        for o, r, n in links:
            if o == OWNER:
                members[r] = head_of(r)
                blame.append({"repo": r, "sha": members[r], "pr": n})
    me = f"{OWNER}/{module}#{pr['number']}" if pr else None
    for key, w in list(waiting.items()):
        if me and me in w["unmerged"]:
            w["unmerged"].remove(me)
        if not w["unmerged"]:
            members[w["module"]] = head_of(w["module"])
            blame.append({"repo": w["module"], "sha": members[w["module"]], "pr": int(key.split("#")[1])})
            del waiting[key]
    return members, blame, "series" if len(members) > 1 else "single"


def run_policy(policy: str, module: str | None, kind: str) -> list[dict]:
    lock, state = load(policy)
    for k in ("quarantine", "dropped", "integrated", "pending", "waiting", "runs", "pending_on_upstream", "reverted_fix"):
        state.setdefault(k, [] if k in ("pending", "runs") else {})
    records = []
    t = dt.datetime.now(dt.timezone.utc)
    for key, w in state["waiting"].items():
        if not w["notified"] and t - parse_time(w["since"]) >= dt.timedelta(hours=NOTIFY_HOURS):
            w["notified"] = True
            records.append({"t": now(), "policy": policy, "outcome": f"notify: series {key} unresolved for {NOTIFY_HOURS} h"})
    if policy == "E":
        for m, q in list(state["quarantine"].items()):
            if q["reason"].startswith("breaks") and t - parse_time(q["since"]) >= dt.timedelta(minutes=HOLD_MINUTES):
                fixes = {x: f["sha"] for x, f in state["pending_on_upstream"].items() if m in f["upstream"]}
                rec = attempt(policy, lock, state, {m: q["sha"], **fixes}, q["blame"], "drop")
                rec["note"] = f"E hold of {HOLD_MINUTES} min expired"
                records.append(rec)
    todo = []
    if module:
        todo.append(module)
    todo += [m for m in state["pending"] if m not in todo]
    state["pending"] = []
    for m in todo:
        sha = head_of(m)
        if state["integrated"].get(m) == sha and m not in state["quarantine"]:
            records.append({"t": now(), "policy": policy, "outcome": f"skip: {m}@{sha[:8]} already integrated"})
            continue
        q = state["quarantine"].get(m)
        if q and q["sha"] == sha:
            records.append({"t": now(), "policy": policy, "outcome": f"skip: {m}@{sha[:8]} is the quarantined change"})
            continue
        changes, blame, note = series_unit(state, m, sha)
        if changes is None:
            records.append({"t": now(), "policy": policy, "outcome": note, "blame": blame})
            continue
        mode = "drop" if policy == "D" else "hold"
        held: set[str] = set()
        if mode == "hold":
            for u, uq in state["quarantine"].items():
                if u in changes or not uq["reason"].startswith("breaks"):
                    continue
                if any(u in upstreams(x, changes[x], lock) for x in changes):
                    held.add(u)
            if held:
                for u in held:
                    changes[u] = state["quarantine"][u]["sha"]
                    for x, f in state["pending_on_upstream"].items():
                        if u in f["upstream"] and x not in changes:
                            changes[x] = f["sha"]
                note += f"; joint run with held {', '.join(sorted(held))}"
        # A newer version of a held upstream (such as a revert) supersedes it: fixes waiting on it
        # re-run afterwards as ordinary changes.
        released = [x for x, f in state["pending_on_upstream"].items() if m in f["upstream"] and m not in held]
        rec = attempt(policy, lock, state, changes, blame, mode, frozenset(held))
        rec["note"] = note
        if any(x in state["reverted_fix"] for x in changes) and not rec["outcome"].startswith("advanced"):
            rec["alert"] = "this fix targeted an upstream change that was reverted"
        for x in changes:
            state["reverted_fix"].pop(x, None)
        records.append(rec)
        if released and m in rec.get("cleared", []):
            for x in released:
                state["pending_on_upstream"].pop(x, None)
                state["reverted_fix"][x] = True
                if x not in todo:
                    todo.append(x)
    state["runs"] += records
    save(policy, lock, state)
    return records


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stage")
    s.add_argument("--module", required=True)
    s.add_argument("--sha", required=True)
    s.add_argument("--lock", required=True)
    s.add_argument("--out", required=True)
    i = sub.add_parser("integrate")
    i.add_argument("--module", default="")
    i.add_argument("--kind", choices=["module", "tick", "requeue"], required=True)
    a = ap.parse_args()
    if a.cmd == "stage":
        lock = json.loads(Path(a.lock).read_text())
        st = stage_for(a.module, a.sha, lock, {})
        if st is None:
            print(f"::error::an upstream of {a.module} is not in the lock", file=sys.stderr)
            sys.exit(3)
        Path(a.out).write_text(json.dumps(st))
        return
    pending_before = {p: list(load(p)[1].get("pending", [])) for p in POLICIES}
    all_records = []
    for p in POLICIES:
        all_records += run_policy(p, a.module or None, a.kind)
    for r in all_records:
        print(json.dumps(r, sort_keys=True))
    with open(LOCKS / "timings.jsonl", "a", newline="\n") as f:
        f.write(json.dumps({"t": now(), "run_id": os.environ.get("GITHUB_RUN_ID"), "kind": a.kind,
                            "module": a.module, "builds": BUILDS}, sort_keys=True) + "\n")
    pending_after = {p: load(p)[1].get("pending", []) for p in POLICIES}
    need = any(pending_after[p] and pending_after[p] != pending_before[p] for p in POLICIES)
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"requeue={'true' if need else 'false'}\n")


if __name__ == "__main__":
    main()
