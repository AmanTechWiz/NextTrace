"""Failure questions over a NextTrace service map, answered inside FalkorDB, every answer citing its connections.

  blast_radius(g, service)       what breaks if `service` fails: everything that calls it, directly or through others
  failure_paths(g, service)      how a request reaches `service`, from the system's entry points or a given caller
  criticality(g)                 which services matter most: dependents, betweenness, PageRank
  check_answer(g, connections)   flag any call or service in an agent's answer that the map can't back up

A cited connection is the CALLS edge exactly as stored: caller, callee, confidence, sources and its first evidence line
(file:line or traffic count). Stale edges (gone from the code) are left out unless include_stale is set.
Raises ValueError with a readable message for bad input.
"""
CONFIDENCE = {"high": 3, "medium": 2, "inferred": 1}
MAX_DEPTH = 10

# One path as cited hops: [{caller, callee, confidence, sources, evidence, stale}, ...]
HOPS = """[i IN range(0, length(p) - 1) | {caller: nodes(p)[i].name, callee: nodes(p)[i + 1].name,
  confidence: relationships(p)[i].confidence, sources: relationships(p)[i].sources,
  evidence: relationships(p)[i].evidence[0], stale: coalesce(relationships(p)[i].stale, false)}]"""


def _depth(max_depth) -> int:
    d = int(max_depth)
    if not 1 <= d <= MAX_DEPTH:
        raise ValueError(f"max_depth must be between 1 and {MAX_DEPTH}")
    return d


def _require(g, service):
    rows = g.ro_query("MATCH (s:Service {name: $n}) RETURN s.kind, s.owner, coalesce(s.stale, false)", {"n": service}).result_set
    if not rows:
        names = [r[0] for r in g.ro_query("MATCH (s:Service) RETURN s.name ORDER BY s.name").result_set]
        close = [n for n in names if service.lower() in n.lower() or n.lower() in service.lower()][:8]
        raise ValueError(f"no service {service!r} in this map" + (f"; did you mean {close}?" if close else ""))
    return rows[0]


def _weakest(hops) -> str:
    return min((h["confidence"] or "inferred" for h in hops), key=lambda c: CONFIDENCE.get(c, 0))


def _path(hops) -> list:
    return [hops[0]["caller"]] + [h["callee"] for h in hops] if hops else []


def _cited(*hop_lists) -> list:
    """Every distinct connection an answer relies on, once."""
    seen = {}
    for hops in hop_lists:
        for h in hops:
            seen.setdefault((h["caller"], h["callee"]), h)
    return [seen[k] for k in sorted(seen)]


def _live(include_stale: bool) -> str:
    return "true" if include_stale else "all(r IN relationships(p) WHERE NOT coalesce(r.stale, false))"


def blast_radius(g, service: str, max_depth: int = 6, include_stale: bool = False) -> dict:
    """Everything that breaks if `service` fails: its callers, their callers, and so on, each with the shortest
    chain that links it to the failure and the evidence for every hop."""
    d = _depth(max_depth)
    kind, owner, _ = _require(g, service)
    rows = g.ro_query(f"""
        MATCH (s:Service {{name: $n}})
        MATCH p = (x:Service)-[:CALLS*1..{d}]->(s)
        WHERE x <> s AND {_live(include_stale)}
        WITH x, p ORDER BY length(p), [n IN nodes(p) | n.name]
        WITH x, collect(p)[0] AS p
        RETURN x.name, x.kind, x.owner, length(p), {HOPS}
        ORDER BY length(p), x.name""", {"n": service}, timeout=20000).result_set
    affected = [{"service": n, "kind": k, "owner": o or "", "depth": depth, "path": _path(hops),
                 "confidence": _weakest(hops), "hops": hops} for n, k, o, depth, hops in rows]
    entry = set(_entry_points(g, include_stale))
    by_depth = {}
    for a in affected:
        by_depth[a["depth"]] = by_depth.get(a["depth"], 0) + 1
    return {
        "service": service, "kind": kind, "owner": owner or "",
        "affected": affected, "affected_count": len(affected),
        "direct_callers": sorted(a["service"] for a in affected if a["depth"] == 1),
        "by_depth": by_depth,
        "owners_to_notify": sorted({a["owner"] for a in affected if a["owner"]} | ({owner} if owner else set())),
        "entry_points_hit": sorted(a["service"] for a in affected if a["service"] in entry),
        "cited_connections": _cited(*(a["hops"] for a in affected)),
        "max_depth": d, "include_stale": include_stale,
    }


def _entry_points(g, include_stale=False) -> list:
    """App services nothing else calls: where requests enter the system (UIs, gateways, load generators, jobs)."""
    cond = "true" if include_stale else "NOT coalesce(c.stale, false)"
    rows = g.ro_query(f"""
        MATCH (s:Service {{kind: 'service'}}) WHERE NOT coalesce(s.stale, false)
        OPTIONAL MATCH (x:Service)-[c:CALLS]->(s) WHERE x <> s AND {cond}
        WITH s, count(x) AS callers WHERE callers = 0
        RETURN s.name ORDER BY s.name""").result_set
    return [r[0] for r in rows]


def failure_paths(g, service: str, from_service: str = "", max_paths: int = 10, max_depth: int = 6,
                  include_stale: bool = False) -> dict:
    """The shortest call chains that reach `service`: from `from_service`, or from every entry point. Ties are all
    kept (up to max_paths). Entry points with no route to `service` are listed as not_reached."""
    d = _depth(max_depth)
    max_paths = max(1, min(int(max_paths), 100))
    _require(g, service)
    if from_service:
        _require(g, from_service)
        if from_service == service:
            raise ValueError("from_service and service are the same")
        sources = [from_service]
    else:
        sources = [e for e in _entry_points(g, include_stale) if e != service]
    rows = g.ro_query(f"""
        MATCH (t:Service {{name: $n}})
        MATCH p = (src:Service)-[:CALLS*1..{d}]->(t)
        WHERE src.name IN $sources AND {_live(include_stale)}
        RETURN src.name, length(p), {HOPS}
        ORDER BY length(p), src.name LIMIT 5000""", {"n": service, "sources": sources}, timeout=20000).result_set
    best = {}
    for src, length, hops in rows:
        if src not in best or length == best[src][0][0]:
            best.setdefault(src, []).append((length, hops))
    paths = []
    for src in sorted(best, key=lambda s: (best[s][0][0], s)):
        for length, hops in sorted(best[src], key=lambda x: _path(x[1])):
            paths.append({"from": src, "length": length, "path": _path(hops), "confidence": _weakest(hops), "hops": hops})
    return {
        "service": service, "from": sources, "paths": paths[:max_paths], "path_count": len(paths),
        "truncated": len(paths) > max_paths, "not_reached": sorted(set(sources) - set(best)),
        "cited_connections": _cited(*(p["hops"] for p in paths[:max_paths])),
        "max_depth": d, "include_stale": include_stale,
    }


def criticality(g, top: int = 10, service: str = "", kind: str = "", include_stale: bool = False) -> dict:
    """Rank services by how much depends on them.

    dependents: services that break if it fails (blast radius size, live edges only).
    betweenness: how many shortest call chains pass through it (a bridge or single point of failure), from FalkorDB's
      algo.betweenness over every source (not sampled). Its scale is FalkorDB's own, so compare services, not absolutes.
    pagerank: importance from being called by important services.
    Ranked by dependents, then betweenness, then pagerank. Each entry cites its direct callers' connections.
    kind: rank only services of this kind (service, datastore, infra, other); the metrics still use the whole map.
    The two FalkorDB algorithms run over every CALLS edge, stale ones included (they can't filter properties).
    """
    top = max(1, min(int(top), 100))
    names = {r[0]: (r[1], r[2] or "") for r in g.ro_query(
        "MATCH (s:Service) WHERE NOT coalesce(s.stale, false) RETURN s.name, s.kind, s.owner").result_set}
    if not names:
        raise ValueError("this map has no services")
    if service:
        _require(g, service)
    if kind and kind not in ("service", "datastore", "infra", "other"):
        raise ValueError("kind must be service, datastore, infra or other")
    n = len(names)
    pr = dict(g.ro_query("CALL algo.pageRank('Service', 'CALLS') YIELD node, score RETURN node.name, score").result_set)
    bt = dict(g.ro_query("CALL algo.betweenness({nodeLabels: ['Service'], relationshipTypes: ['CALLS'], "
                         f"samplingSize: {max(n, 1)}, samplingSeed: 1}}) YIELD node, score RETURN node.name, score").result_set)
    dep = dict(g.ro_query(f"""
        MATCH p = (x:Service)-[:CALLS*1..{MAX_DEPTH}]->(s:Service)
        WHERE x <> s AND {_live(include_stale)}
        RETURN s.name, count(DISTINCT x)""", timeout=30000).result_set)
    rows = [{"service": name, "kind": k, "owner": o, "dependents": dep.get(name, 0),
             "betweenness": round(bt.get(name, 0.0), 3), "pagerank": round(pr.get(name, 0.0), 5)}
            for name, (k, o) in names.items()]
    rows.sort(key=lambda r: (-r["dependents"], -r["betweenness"], -r["pagerank"], r["service"]))
    rows = [r for r in rows if not kind or r["kind"] == kind]
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    chosen = [r for r in rows if r["service"] == service] if service else rows[:top]
    cond = "true" if include_stale else "NOT coalesce(c.stale, false)"
    for r in chosen:
        r["direct_callers"] = [{"caller": a, "callee": r["service"], "confidence": conf, "sources": src,
                                "evidence": ev[0] if ev else "", "stale": bool(st)} for a, conf, src, ev, st in g.ro_query(
            f"MATCH (a:Service)-[c:CALLS]->(s:Service {{name: $n}}) WHERE {cond} "
            "RETURN a.name, c.confidence, c.sources, c.evidence, coalesce(c.stale, false) ORDER BY a.name",
            {"n": r["service"]}).result_set]
    stale = g.ro_query("MATCH ()-[c:CALLS]->() WHERE coalesce(c.stale, false) RETURN count(c)").result_set[0][0]
    return {
        "services": n, "ranked": chosen,
        "cited_connections": _cited(*(r["direct_callers"] for r in chosen)),
        "algorithms": ["algo.pageRank", "algo.betweenness (all sources, not sampled)"],
        "note": (f"{stale} stale connection(s) exist; PageRank and betweenness still count them, dependents does not."
                 if stale else ""),
    }



def check_answer(g, connections: list, services: list = (), target: str = "") -> dict:
    """Check an agent's answer against the map before anyone trusts it.

    connections: the calls the answer relies on, [{caller, callee}, ...] or [(caller, callee), ...]. Each is looked up
    as stored: live on the map -> verified (with the map's own evidence); stale -> flagged; absent -> not_on_map.
    services: the services the answer names. Each must exist on the map; with `target`, each must also reach the target
    through the answer's own verified connections (so a claimed chain has to hold hop by hop), else unsupported.
    A connection that quotes code ({file, line, quote}) has the quote checked in the mapped repo(s), the same check the
    server applies to agent edits; a quote that isn't there is flagged as bad_evidence even when the call is real.
    ok is true only when nothing is flagged."""
    from pathlib import Path
    from nexttrace.mapper import verify_quote
    roots = [Path(r[0]) for r in g.ro_query("MATCH (r:MapRun) WHERE r.root IS NOT NULL RETURN DISTINCT r.root").result_set]
    pairs, bad = [], []
    for c in connections:
        a, b = (c.get("caller"), c.get("callee")) if isinstance(c, dict) else tuple(c)[:2]
        if not a or not b:
            raise ValueError(f"each connection needs a caller and a callee, got {c!r}")
        pairs.append((str(a), str(b)))
        if isinstance(c, dict) and c.get("quote"):
            why = "no mapped repo to check it against" if not roots else ""
            for root in roots:
                try:
                    verify_quote(root, str(c.get("file", "")), int(c.get("line") or 0), str(c["quote"]), str(b), slack=5)
                    why = ""
                    break
                except (ValueError, TypeError) as e:
                    why = str(e)[:200]
            if why:
                bad.append({"caller": str(a), "callee": str(b), "file": c.get("file"), "line": c.get("line"), "reason": why})
    if target:
        _require(g, target)
    stored = {(r[0], r[1]): r[2:] for r in g.ro_query(
        "UNWIND $p AS p MATCH (a:Service {name: p[0]})-[c:CALLS]->(b:Service {name: p[1]}) "
        "RETURN a.name, b.name, c.confidence, c.evidence[0], coalesce(c.stale, false)", {"p": pairs}).result_set}
    names = {r[0] for r in g.ro_query("UNWIND $n AS n MATCH (s:Service {name: n}) RETURN s.name",
                                      {"n": sorted({*map(str, services), *(x for p in pairs for x in p)})}).result_set}
    verified, stale, missing = [], [], []
    for a, b in dict.fromkeys(pairs):
        if (a, b) not in stored:
            missing.append({"caller": a, "callee": b})
        elif stored[(a, b)][2]:
            stale.append({"caller": a, "callee": b, "evidence": stored[(a, b)][1]})
        else:
            verified.append({"caller": a, "callee": b, "confidence": stored[(a, b)][0], "evidence": stored[(a, b)][1]})
    unknown = sorted({x for x in (*map(str, services), *(x for p in pairs for x in p)) if x not in names})
    unsupported = []
    if target:  # walk the answer's verified hops backwards from the target
        callers, reach, todo = {}, set(), [target]
        for v in verified:
            callers.setdefault(v["callee"], []).append(v["caller"])
        while todo:
            for c in callers.get(todo.pop(), ()):
                if c not in reach:
                    reach.add(c)
                    todo.append(c)
        unsupported = sorted(s for s in map(str, services) if s in names and s != target and s not in reach)
    flagged = len(stale) + len(missing) + len(unknown) + len(unsupported) + len(bad)
    return {"ok": flagged == 0, "verified": verified, "stale": stale, "not_on_map": missing,
            "unknown_services": unknown, "unsupported_services": unsupported, "bad_evidence": bad,
            "summary": f"{len(verified)} verified, {flagged} flagged"
                       + (f" ({len(missing)} not on map, {len(stale)} stale, {len(unknown)} unknown services, "
                          f"{len(unsupported)} unsupported, {len(bad)} bad evidence)" if flagged else "")}
