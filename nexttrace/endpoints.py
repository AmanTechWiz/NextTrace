"""Endpoint-level map: which routes each service exposes, and which route each call hits, with file:line.

Routes (server side):   Spring (@RequestMapping/@GetMapping...), Express (app.get), Flask/FastAPI (@app.route,
                        @router.post), Go (HandleFunc, gorilla Methods().Path()), gRPC (.proto service/rpc).
Calls (client side):    HTTP URLs with a path ("http://ts-seat-service:18898/api/v1/seatservice/seats"), method
                        read from the next few lines (HttpMethod.POST, .post(, method: "POST"); gRPC stubs
                        (pb.NewCartServiceClient(conn).GetCart, stub = ...Stub(channel); stub.GetCart).
A call is linked to the callee's route whose path template matches ({id} matches one segment).
Calls that match no known route are kept as undeclared endpoints, so nothing is dropped.
"""
import re
from dataclasses import dataclass
from pathlib import Path

from nexttrace.mapper import CODE_EXT, COMMENT, ServiceMap, _files, _rel

HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}


@dataclass(frozen=True, order=True)
class Route:
    service: str
    method: str    # GET/POST/... , ANY, or RPC
    path: str      # "/api/v1/seatservice/seats" or "CartService/GetCart"
    where: str

    @property
    def key(self):
        return f"{self.service} {self.method} {self.path}"


@dataclass(frozen=True, order=True)
class Call:
    caller: str
    callee: str
    method: str    # "" when unknown, RPC for gRPC
    path: str
    open_end: int   # runtime parts in the path ("{}"): "/order/" + date + "/" + id -> 2
    where: str
    text: str


# ---------- routes ----------

SPRING_ANN = re.compile(r"@(Request|Get|Post|Put|Delete|Patch)Mapping\b(\((?:[^()]|\([^()]*\))*\))?")
SPRING_PATH = re.compile(r"(?:value|path)\s*=\s*\{?\s*\"([^\"]*)\"|^\(\s*\{?\s*\"([^\"]*)\"")
EXPRESS = re.compile(r"\b(?:app|router|api|server)\.(get|post|put|delete|patch|all)\(\s*[\"'`]([^\"'`]+)[\"'`]")
FLASK = re.compile(r"@\w+\.route\(\s*[\"']([^\"']+)[\"'](.*)")
FASTAPI = re.compile(r"@\w+\.(get|post|put|delete|patch)\(\s*[\"']([^\"']+)[\"']")
GO_MUX = re.compile(r"Methods\(\s*\"(\w+)\"\s*\)\.(Path(?:Prefix)?)\(\s*\"([^\"]+)\"|\.(Path(?:Prefix)?)\(\s*\"([^\"]+)\"\s*\)\.Methods\(\s*\"(\w+)\"")
GO_HANDLE = re.compile(r"HandleFunc\(\s*\"([^\"]+)\"(.*)")
PROTO_SVC = re.compile(r"^\s*service\s+(\w+)\s*\{")
PROTO_RPC = re.compile(r"^\s*rpc\s+(\w+)\s*\(")


def _join(base, path):
    return "/" + "/".join(p for p in f"{base}/{path}".split("/") if p) if (base or path) else "/"


def _spring_routes(service, f, rel):
    text = f.read_text(errors="ignore")
    class_at = re.search(r"^\s*(public\s+)?(abstract\s+)?class\s", text, re.M)
    base, out = "", []
    for a in SPRING_ANN.finditer(text):
        kind, args = a.group(1), a.group(2) or ""
        p = SPRING_PATH.search(args.replace("\n", " "))
        path = (p.group(1) or p.group(2)) if p else ""
        if class_at and a.start() < class_at.start():
            base = path  # class-level prefix
            continue
        if kind == "Request":
            m = re.search(r"RequestMethod\.(\w+)", args)
            method = m.group(1) if m else "ANY"
        else:
            method = kind.upper()
        line = text.count("\n", 0, a.start()) + 1
        out.append(Route(service, method, _join(base, path), f"{rel}:{line}"))
    return out


def _line_routes(service, f, rel):
    out = []
    for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
        if line.lstrip().startswith(COMMENT):
            continue
        where = f"{rel}:{i}"
        for m in EXPRESS.finditer(line):
            out.append(Route(service, "ANY" if m.group(1) == "all" else m.group(1).upper(), m.group(2), where))
        for m in FASTAPI.finditer(line):
            out.append(Route(service, m.group(1).upper(), m.group(2), where))
        m = FLASK.search(line)
        if m:
            methods = re.findall(r"[\"'](GET|POST|PUT|DELETE|PATCH)[\"']", m.group(2).upper()) or ["GET"]
            out += [Route(service, mt, m.group(1), where) for mt in methods]
        for m in GO_MUX.finditer(line):
            method, kind, path = m.group(1, 2, 3) if m.group(1) else (m.group(6), m.group(4), m.group(5))
            out.append(Route(service, method.upper(), path.rstrip("/") + "/**" if kind == "PathPrefix" else path, where))
        m = GO_HANDLE.search(line)
        if m:
            mm = re.search(r"Methods\(\s*\"(\w+)\"", m.group(2))
            out.append(Route(service, mm.group(1).upper() if mm else "ANY", m.group(1), where))
    return out


def _proto_routes(repo, m):
    """gRPC: proto `service CartService { rpc GetCart }` belongs to the service named cartservice (or cart)."""
    by_name = {re.sub(r"[^a-z0-9]", "", n.lower()): n for n in m.services}
    seen, out, owners = set(), [], {}
    for f in _files(repo, {".proto"}):
        svc = None
        for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
            s = PROTO_SVC.match(line)
            if s:
                low = s.group(1).lower()
                svc = s.group(1)
                owners[svc] = by_name.get(low) or by_name.get(low.removesuffix("service"))
                continue
            r = PROTO_RPC.match(line)
            if r and svc and owners.get(svc) and (svc, r.group(1)) not in seen:
                seen.add((svc, r.group(1)))
                out.append(Route(owners[svc], "RPC", f"{svc}/{r.group(1)}", f"{_rel(repo, f)}:{i}"))
            if line.strip().startswith("}"):
                svc = None
    return out, {k: v for k, v in owners.items() if v}


def routes(repo: Path, m: ServiceMap):
    out, grpc_owner = _proto_routes(repo, m)
    for name, s in m.services.items():
        if not s["source_dir"]:
            continue
        for f in _files(repo / s["source_dir"], CODE_EXT):
            rel = _rel(repo, f)
            out += _spring_routes(name, f, rel) if f.suffix in (".java", ".kt") else _line_routes(name, f, rel)
    return sorted(set(out)), grpc_owner


# ---------- calls ----------

METHOD_NEAR = re.compile(r"HttpMethod\.([A-Z]+)|\b(get|post|put|delete|patch)(?:ForObject|ForEntity|ForLocation)?\s*\("
                         r"|method\s*[:=]\s*[\"'](\w+)[\"']", re.I)
GO_CLIENT = re.compile(r"(?:(\w+)\s*:?=\s*)?\w+\.New(\w+)Client\([^)]*\)(?:\s*\.\s*(\w+)\s*\()?")
PY_STUB = re.compile(r"(\w+)\s*=\s*[\w.]*?(\w+)Stub\(")


def _method_near(lines, i):
    for line in lines[i:i + 4]:
        m = METHOD_NEAR.search(line)
        if m:
            method = next(g for g in m.groups() if g).upper()
            if method in HTTP_METHODS:
                return method
    return ""


QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'|`[^`]*`")


CHAIN = re.compile(r"""\s*\+\s*(?:"([^"]*)"|'([^']*)'|`([^`]*)`|[\w$.]+(?:\([^()]*\))*(?:\.[\w$]+(?:\([^()]*\))*)*)""")


def _chain(rest):
    """`+ id + "/items"` after a URL -> "{}/items": literals kept, runtime values become {}."""
    out, pos = [], 0
    while m := CHAIN.match(rest, pos):
        lit = next((g for g in m.groups() if g is not None), None)
        out.append("{}" if lit is None else lit)
        pos = m.end()
    return "".join(out)


def _path(base, tail=""):
    """Normalize a call path: drop query, runtime parts (${x}, {x}, %s) become {}."""
    p = re.split(r"[?#]", base + tail)[0]
    p = re.sub(r"\$\{[^}]*\}|\{[^}]*\}|%[sdv]", "{}", p)
    return "/" + "/".join(x for x in p.split("/") if x)


def _after(hit):
    """Position right after the URL's closing quote (the optional `+` belongs to the chain)."""
    return hit.end() - len(hit.group(3) or "")


DECL = re.compile(r"(\w+)\s*(?::|:?=)\s*(?:[\w.]+\()?[\"'`]$")  # `url = "`, `url := "`, `cartsUrl: util.format("`


def _usage(line, var, qualified=False):
    """`base + "/x" + id` -> "/x/{}" for each use of `var` outside string literals on the line.

    qualified: only `config.var` counts (a URL defined in another file and used through its module or class).
    """
    strings = [q.span() for q in QUOTED.finditer(line)]
    prefix = r"(?<=[\w)\]]\.)" if qualified else r"(?<![\w.])"
    for u in re.finditer(prefix + re.escape(var) + r"\b", line):
        if not any(a <= u.start() < b for a, b in strings):
            yield _chain(line[u.end():])


def _client_var(g, line):
    """`cl := pb.NewXClient(conn)` names a client; `q, err := pb.NewXClient(c).GetQuote(...)` names a result."""
    chained = g.re is GO_CLIENT and (g.group(3) or line.rstrip().endswith("."))
    return bool(g.group(1)) and not chained


def calls(repo: Path, m: ServiceMap, grpc_owner: dict):
    # endpoints only for API services; datastore URLs (mongodb://db/name) have no routes
    names = sorted({n for n, s in m.services.items() if s["kind"] == "service"} | m.known, key=len, reverse=True)
    alts = "|".join(re.escape(n) for n in names)
    # `%s` / `${domain}` after the host: `util.format("http://carts%s/carts", domain)`
    url = re.compile(rf"[a-z]+://({alts})(?::\d+)?(?:%[sv]|\$\{{[^}}]*\}}|\{{\w*\}})?(/[^\"'\s`?#]*)?(?:[?#][^\"'`]*)?[\"'`](\s*\+)?")
    named = re.compile(rf"[\"']({alts})[\"']\s*\)\s*\+\s*[\"'](/[^\"']*)[\"'](\s*\+)?")  # getServiceUrl("x") + "/path"
    out = set()
    for caller, s in m.services.items():
        if not s["source_dir"]:
            continue
        files = [(f, _rel(repo, f), f.read_text(errors="ignore").splitlines()) for f in _files(repo / s["source_dir"], CODE_EXT)]
        # URLs defined in one file (a config module) and used as `config.name` in others
        shared = {}
        for _, rel, lines in files:
            for i, line in enumerate(lines):
                for hit in url.finditer(line):
                    d = DECL.search(line[:hit.start()])
                    if d and hit.group(1) != caller and not line.lstrip().startswith(COMMENT):
                        path = _path(hit.group(2) or "/", _chain(line[_after(hit):]))
                        shared.setdefault(d.group(1), []).append((hit.group(1), path, path.count("{}"), f"{rel}:{i + 1}", line.strip()[:160]))
        shared_used, unused = set(), []
        for f, rel, lines in files:
            # variable -> proto service, collected first: a stub made in main() is often used in a function above it
            stubs = {g.group(1): g.group(2) for line in lines for g in [PY_STUB.search(line) or GO_CLIENT.search(line)]
                     if g and _client_var(g, line)}
            consts = {}  # var -> [(callee, path, open_end, where, text)]; if/else may assign several URLs
            used, fresh = set(), {}  # fresh[var]: used since its last assignment
            for i, line in enumerate(lines):
                if line.lstrip().startswith(COMMENT):
                    continue
                where, text = f"{rel}:{i + 1}", line.strip()[:160]
                for hit in list(url.finditer(line)) + list(named.finditer(line)):
                    callee = hit.group(1)
                    path = _path(hit.group(2) or "/", _chain(line[_after(hit):]))
                    plus = path.count("{}")
                    if callee == caller:
                        continue
                    # a URL kept in a variable: the call happens where the variable is used (that's where the method is)
                    decl = DECL.search(line[:hit.start()])
                    if decl:
                        var = decl.group(1)
                        if fresh.get(var):  # used since its last assignment: this is a new value, not an if/else branch
                            if var not in used:  # earlier value never used: keep it at its assignment line
                                for c in consts[var]:
                                    out.add(Call(caller, c[0], _method_near(lines, int(c[3].rsplit(":", 1)[1]) - 1), c[1], c[2], c[3], c[4]))
                            consts[var], fresh[var] = [], False
                        consts.setdefault(var, []).append((callee, path, plus, where, text))
                        fresh.setdefault(var, False)
                    else:
                        out.add(Call(caller, callee, _method_near(lines, i), path, plus, where, text))
                for var, entries in consts.items():
                    if any(where == e[3] for e in entries):
                        continue
                    for tail in _usage(line, var):
                        used.add(var)
                        fresh[var] = True
                        for callee, base, _, _, _ in entries:
                            full = _path(base, tail)
                            out.add(Call(caller, callee, _method_near(lines, i), full, full.count("{}"), where, text))
                for var, entries in shared.items():
                    for tail in _usage(line, var, qualified=True):
                        shared_used.add(var)
                        for callee, base, _, at, _ in entries:
                            if not at.startswith(rel + ":"):
                                full = _path(base, tail)
                                out.add(Call(caller, callee, _method_near(lines, i), full, full.count("{}"), where, text))
                for g in GO_CLIENT.finditer(line):
                    var, svc, rpc = g.groups()
                    if _client_var(g, line):
                        stubs[var] = svc
                    if not rpc and i + 1 < len(lines) and line.rstrip().endswith("."):
                        nxt = re.match(r"\s*(\w+)\s*\(", lines[i + 1])
                        rpc = nxt.group(1) if nxt else None
                    if rpc and grpc_owner.get(svc) and grpc_owner[svc] != caller:
                        out.add(Call(caller, grpc_owner[svc], "RPC", f"{svc}/{rpc}", False, where, text))
                p = PY_STUB.search(line)
                if p:
                    stubs[p.group(1)] = p.group(2)
                for var, svc in stubs.items():
                    for u in re.finditer(rf"\b{re.escape(var)}\.(\w+)\s*\(", line):
                        if grpc_owner.get(svc) and grpc_owner[svc] != caller and not u.group(1).startswith("_"):
                            out.add(Call(caller, grpc_owner[svc], "RPC", f"{svc}/{u.group(1)}", False, where, text))
            unused += [(var, e, lines) for var, entries in consts.items() if var not in used for e in entries]
        for var, (callee, base, base_open, where, text), lines in unused:
            if var not in shared_used:  # never used anywhere: keep it at the assignment line
                out.add(Call(caller, callee, _method_near(lines, int(where.rsplit(":", 1)[1]) - 1), base, base_open, where, text))
    return sorted(out)


# ---------- linking ----------

PARAM = re.compile(r"\{[^}]*\}|:\w+|<[^>]*>")


def _matches(call_path, route_path):
    c, r = [x for x in call_path.split("/") if x], [x for x in route_path.split("/") if x]
    prefix = bool(r) and r[-1] == "**"
    if prefix:
        r = r[:-1]
    if len(c) < len(r) or (not prefix and len(c) != len(r)):
        return False
    # a runtime value fills a route parameter, never a fixed word: "/order/" + id is not "/order/admin"
    return all(PARAM.fullmatch(b) or (a == b and "{}" not in a) for a, b in zip(c, r))


def link(found_calls, all_routes):
    """(call, route or None) for every call. A route matches when service, method and path segments agree."""
    by_service = {}
    for r in all_routes:
        by_service.setdefault(r.service, []).append(r)
    out = []
    for c in found_calls:
        best = best_score = None
        for r in by_service.get(c.callee, []):
            if c.method == "RPC" or r.method == "RPC":
                ok = c.method == r.method == "RPC" and c.path == r.path
            else:
                ok = (not c.method or r.method in ("ANY", c.method)) and _matches(c.path, r.path)
            if ok:  # most specific wins: exact method, exact path (not a prefix), fewest params
                fixed = sum(1 for x in r.path.split("/") if x and x != "**" and not PARAM.fullmatch(x))
                score = (r.method != "ANY", not r.path.endswith("/**"), fixed, -len(PARAM.findall(r.path)))
                if best is None or score > best_score:
                    best, best_score = r, score
        out.append((c, best))
    return out


# ---------- write ----------

UPSERT_ROUTES = """
UNWIND $rows AS r
MATCH (s:Service {name: r.service})
MERGE (e:Endpoint {key: r.key})
SET e.service = r.service, e.method = r.method, e.path = r.path, e.where = r.where, e.declared = true,
    e.repo = $repo, e.last_run = $run, e.stale = false
MERGE (s)-[x:EXPOSES]->(e)
SET x.repo = $repo, x.last_run = $run
"""

UPSERT_CALLS = """
UNWIND $rows AS r
MATCH (a:Service {name: r.caller})
MERGE (e:Endpoint {key: r.key})
ON CREATE SET e.service = r.callee, e.method = r.method, e.path = r.path, e.declared = false,
              e.repo = $repo, e.last_run = $run, e.stale = false
MERGE (a)-[c:CALLS_ENDPOINT]->(e)
SET c.evidence = r.evidence, c.evidence_count = r.count, c.matched = r.matched,
    c.repo = $repo, c.last_run = $run, c.stale = false
WITH e, r WHERE NOT e.declared
MATCH (b:Service {name: r.callee})
MERGE (b)-[:EXPOSES]->(e)
"""

MARK_STALE = [
    "MATCH (e:Endpoint {repo: $repo}) WHERE e.last_run <> $run SET e.stale = true RETURN count(e)",
    "MATCH ()-[c:CALLS_ENDPOINT {repo: $repo}]->() WHERE c.last_run <> $run SET c.stale = true RETURN count(c)",
]


def existing_routes(graph, repo_name):
    """Routes other repos already put in the graph, so calls into them can be linked."""
    try:
        rows = graph.ro_query("MATCH (e:Endpoint) WHERE e.declared AND e.repo <> $repo "
                              "RETURN e.service, e.method, e.path, e.where", {"repo": repo_name}).result_set
    except Exception:
        return []
    return [Route(*r) for r in rows]


def write(graph, m: ServiceMap, own_routes, linked, run) -> dict:
    from nexttrace.graphdb import ensure_unique
    ensure_unique(graph, "Endpoint", "key")
    p = {"repo": m.repo, "run": run}
    graph.query(UPSERT_ROUTES, {**p, "rows": [{"key": r.key, "service": r.service, "method": r.method,
                                               "path": r.path, "where": r.where} for r in own_routes]})
    grouped = {}
    for c, r in linked:
        key = r.key if r else f"{c.callee} {c.method or 'ANY'} {c.path}"
        g = grouped.setdefault((c.caller, key), {"caller": c.caller, "callee": c.callee, "key": key,
                                                 "method": r.method if r else (c.method or "ANY"),
                                                 "path": r.path if r else c.path, "matched": r is not None, "ev": set()})
        g["ev"].add(f"code | {c.where} | {c.text}")
    rows = [{**{k: v for k, v in g.items() if k != "ev"}, "evidence": sorted(g["ev"])[:5], "count": len(g["ev"])}
            for g in grouped.values()]
    graph.query(UPSERT_CALLS, {**p, "rows": rows})
    stale = [graph.query(q, p).result_set[0][0] for q in MARK_STALE]
    return {"endpoints": len(own_routes), "endpoint_calls": len(rows),
            "endpoint_calls_matched": sum(r["matched"] for r in rows), "stale_endpoints": stale[0],
            "stale_endpoint_calls": stale[1]}
