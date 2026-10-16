"""Map a repo's services, and who calls whom, into FalkorDB.

Services come from whatever the repo uses to declare them (first match wins per name):
  Kubernetes Deployments/StatefulSets, docker-compose services, Backstage catalog-info.yaml,
  and finally any folder with a Dockerfile (a repo-root Dockerfile makes the repo itself the service).
Owners come from Backstage spec.owner, else CODEOWNERS.

Every CALLS edge keeps its evidence, best source first:
  trace  - seen in recorded traffic (add_traces)
  config - Kubernetes env/args, compose depends_on/environment, Backstage dependsOn, the service's config files
  code   - a full address in source code: "http://ts-seat-service/...", "cartservice:7070"

Several repos can share one graph: names are the join key, and stale-marking is scoped to the repo being mapped.
"""
import argparse
import fnmatch
import json
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SKIP_DIRS = {".git", "node_modules", "vendor", "target", "build", "dist", "test", "tests", "__pycache__", ".venv"}
# docs/examples folders are skipped near the top of a repo only: org/springframework/samples/... is real code
SKIP_TOP = {"docs", "doc", "old-docs", "examples", "samples"}


def _skipped(parts) -> bool:
    return bool(SKIP_DIRS.intersection(parts) or SKIP_TOP.intersection(parts[:2]))
CODE_EXT = {".java", ".py", ".js", ".ts", ".go", ".cs", ".kt", ".rb", ".php", ".rs", ".scala"}
CONFIG_EXT = {".yml", ".yaml", ".properties", ".env", ".conf", ".toml", ".ini", ".json"}
INFRA = re.compile(r"prometheus|grafana|jaeger|zipkin|kibana|fluent|opentelemetry|otel|loki|kube-state-metrics"
                   r"|alertmanager|node-exporter|istio|elastic-?search")
# well-known engines: a datastore even when the repo ships its own image for it (a folder with a Dockerfile)
DB_ENGINE = re.compile(r"(?:^|[-_/])(?:mongo(?:db)?|mysql|redis|valkey|postgres(?:ql)?|mariadb|cassandra|rabbitmq|kafka"
                       r"|memcached?|sqlserver|mssql|nats)(?:[-_:]|$)")
DATASTORE = re.compile(r"mongo|mysql|redis|postgres|mariadb|cassandra|rabbitmq|kafka|memcache|-db\b|_db\b")
COMMENT = ("//", "#", "*", "/*", "<!--", ";")
RANK = {"trace": 3, "config": 2, "code": 1}
CONFIDENCE = {3: "high", 2: "high", 1: "medium"}
MAX_EVIDENCE = 5  # kept per edge; evidence_count has the total


@dataclass(frozen=True, order=True)
class Evidence:
    source: str  # trace | config | code
    where: str   # "path/to/File.java:212", or the traces file
    text: str    # the quoted line, or "seen 412 times"

    def __str__(self):
        return f"{self.source} | {self.where} | {self.text}"


@dataclass
class ServiceMap:
    repo: str
    commit: str
    root: str = ""  # absolute path, so AI-added evidence can be re-checked against the code
    services: dict = field(default_factory=dict)  # name -> {kind, source_dir, declared_in, owner}
    known: set = field(default_factory=set)       # services mapped from other repos into the same graph
    edges: dict = field(default_factory=lambda: defaultdict(set))  # (caller, callee) -> {Evidence}

    def declare(self, name, declared_in, source_dir="", hint="", owner=""):
        s = self.services.setdefault(name, {"declared_in": declared_in, "source_dir": "", "hint": "", "owner": ""})
        s["source_dir"] = s["source_dir"] or source_dir
        s["hint"] = f"{s['hint']} {hint}".strip()
        s["owner"] = s["owner"] or owner

    def add(self, caller, callee, ev):
        if caller != callee and caller in self.services and (callee in self.services or callee in self.known):
            self.edges[(caller, callee)].add(ev)


def _rel(repo, p):
    return str(Path(p).relative_to(repo))


def _files(root: Path, exts=None, names=None):
    for p in sorted(root.rglob("*")):
        if not p.is_file() or _skipped(p.relative_to(root).parts[:-1]):
            continue
        if (exts and p.suffix in exts) or (names and p.name in names):
            yield p


def _yaml_docs(f):
    try:
        return [d for d in yaml.safe_load_all(f.read_text(errors="ignore")) if isinstance(d, dict)]
    except yaml.YAMLError:  # helm templates and other non-plain YAML
        return []


def _line_of(f, needle):
    for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
        if needle in line:
            return i
    return 0


def _git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True).stdout.strip()


def _commit(repo: Path) -> str:
    """The commit of the code being mapped. Not the commit of a parent repo that doesn't track these files."""
    if _git(repo, "ls-files", "--", "."):  # tracked by the enclosing repo (the repo itself, or a monorepo folder)
        return _git(repo, "rev-parse", "HEAD")
    nested = [d for d in sorted(repo.iterdir()) if (d / ".git").exists()]  # a folder of separate repos
    return ",".join(f"{d.name}@{_git(d, 'rev-parse', '--short', 'HEAD')}" for d in nested) or "unversioned"


# ---------- readers: where services are declared ----------

def _k8s(repo, m, pending):
    for f in _files(repo, {".yml", ".yaml"}):
        for d in _yaml_docs(f):
            if d.get("kind") not in ("Deployment", "StatefulSet"):
                continue
            name = (d.get("metadata") or {}).get("name")
            if not name:
                continue
            spec = ((d.get("spec") or {}).get("template") or {}).get("spec") or {}
            images = " ".join(str(c.get("image", "")) for c in spec.get("containers") or [])
            m.declare(name, _rel(repo, f), hint=images)
            for c in spec.get("containers") or []:
                for v in [str(e.get("value", "")) for e in c.get("env") or []] + [str(a) for a in c.get("args") or []]:
                    pending.append((name, v, f, "addr"))


def _compose(repo, m, pending):
    names = {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}
    for f in _files(repo, names=names):
        for d in _yaml_docs(f):
            for name, spec in (d.get("services") or {}).items():
                spec = spec or {}
                build = spec.get("build")
                ctx = build.get("context") if isinstance(build, dict) else build
                src = (f.parent / ctx).resolve() if isinstance(ctx, str) else None
                src = _rel(repo, src) if src and src.is_dir() and repo in src.parents else ""
                m.declare(name, _rel(repo, f), source_dir=src, hint=str(spec.get("image", "")))
                deps = spec.get("depends_on") or []
                for dep in (deps if isinstance(deps, list) else list(deps)) + list(spec.get("links") or []):
                    pending.append((name, str(dep).split(":")[0], f, "name"))
                env = spec.get("environment") or []
                for v in (env.values() if isinstance(env, dict) else env):
                    pending.append((name, str(v), f, "addr"))


def _catalog(repo, m, pending):
    for f in _files(repo, names={"catalog-info.yaml", "catalog-info.yml"}):
        for d in _yaml_docs(f):
            if d.get("kind") not in ("Component", "Resource"):
                continue
            name, spec = d["metadata"]["name"], d.get("spec") or {}
            hint = "resource" if d["kind"] == "Resource" else ""
            src = _rel(repo, f.parent) if d["kind"] == "Component" else ""
            m.declare(name, _rel(repo, f), source_dir=src if src != "." else "", hint=hint,
                      owner=str(spec.get("owner", "")).split("/")[-1])
            for dep in spec.get("dependsOn") or []:
                pending.append((name, str(dep).split("/")[-1].split(":")[-1], f, "name"))


ASPIRE_KINDS = re.compile(r"\w*Project|\w*App|Container|Dockerfile|Yarp|Database|Redis|Valkey|Garnet|Postgres|SqlServer"
                          r"|MySql|MongoDB|RabbitMQ|Kafka|Nats|Elasticsearch|Qdrant|Milvus|Keycloak")
ASPIRE_ADD = re.compile(r"(?:\b(\w+)\s*=\s*)?[\w.]*?\.Add(\w+)(<[^>]*>)?\(\s*\"([^\"]+)\"(?:\s*,\s*\"([^\"]+)\")?")


def _aspire(repo, m, pending):
    """.NET Aspire: `var api = builder.AddProject<Projects.Basket_API>("basket-api").WithReference(redis);`"""
    for f in _files(repo, {".cs"}):
        text = f.read_text(errors="ignore")
        if "DistributedApplication.CreateBuilder" not in text:
            continue
        names = {}  # C# variable -> resource name
        for stmt in text.split(";"):
            a = ASPIRE_ADD.search(stmt)
            if not a or not ASPIRE_KINDS.fullmatch(a.group(2)):
                continue
            var, kind, generic, name, path = a.groups()
            if var:
                names[var] = name
            src = ""
            if path and (f.parent / path).is_dir():
                src = _rel(repo, (f.parent / path).resolve())
            elif generic and (proj := re.search(r"Projects\.(\w+)", generic)):
                folder = next((p for p in sorted(repo.rglob(proj.group(1).replace("_", ".")))
                               if p.is_dir() and not _skipped(p.relative_to(repo).parts)), None)
                src = _rel(repo, folder) if folder else ""
            app = kind.endswith(("Project", "App")) or kind in ("Container", "Dockerfile", "Yarp")
            m.declare(name, _rel(repo, f), source_dir=src, hint="" if app else "resource")
            for ref in re.findall(r"\.WithReference\(\s*(\w+)\s*\)", stmt):
                if ref in names:
                    pending.append((name, names[ref], f, "name"))


def _dockerfiles(repo, m):
    for f in _files(repo, names={"Dockerfile"}):
        name = repo.name if f.parent == repo else f.parent.name
        m.declare(name, _rel(repo, f), source_dir=_rel(repo, f.parent))


def _source_dirs(repo, m):
    """For services declared without a folder: the shallowest folder named like the service, like its image
    (springcommunity/spring-petclinic-customers-service), or ending in -<service>."""
    want = {}
    for n, s in m.services.items():
        if not s["source_dir"]:
            images = {i.rsplit("/", 1)[-1].split(":")[0].split("@")[0] for i in s["hint"].split()}
            want[n] = {n} | {i for i in images if i and "$" not in i}
    if not want:
        return
    dirs = [p for p in sorted(repo.rglob("*"), key=lambda p: (len(p.parts), str(p)))
            if p.is_dir() and not _skipped(p.relative_to(repo).parts)]
    for n, names in want.items():
        hit = next((p for p in dirs if p.name in names or p.name.endswith("-" + n)), None)
        if hit:
            m.services[n]["source_dir"] = _rel(repo, hit)


def _codeowners(repo, m):
    rules = []
    for f in (repo / "CODEOWNERS", repo / ".github" / "CODEOWNERS", repo / "docs" / "CODEOWNERS"):
        if f.is_file():
            for line in f.read_text(errors="ignore").splitlines():
                parts = line.split("#")[0].split()
                if len(parts) >= 2:
                    rules.append((parts[0].strip("/"), parts[1].lstrip("@")))
    for s in m.services.values():
        if s["owner"] or not s["source_dir"]:
            continue
        for pattern, owner in rules:  # GitHub semantics: last matching rule wins
            p = pattern.rstrip("*").rstrip("/")
            if pattern == "*" or s["source_dir"] == p or s["source_dir"].startswith(p + "/") or fnmatch.fnmatch(s["source_dir"], pattern):
                s["owner"] = owner


def _kind(name, s):
    if INFRA.search(name):
        return "infra"
    if DB_ENGINE.search(name) or "resource" in s["hint"]:
        return "datastore"
    if s["source_dir"]:
        return "service"
    if "resource" in s["hint"] or DATASTORE.search(f"{name} {s['hint']}"):
        return "datastore"
    return "other"


# ---------- scan ----------

def scan(repo, known=()) -> ServiceMap:
    repo = Path(repo).resolve()
    m = ServiceMap(repo=repo.name, commit=_commit(repo), root=str(repo), known=set(known))
    pending = []  # (caller, value, file, mode) found while reading declarations
    _k8s(repo, m, pending)
    _compose(repo, m, pending)
    _catalog(repo, m, pending)
    _aspire(repo, m, pending)
    if not m.services:
        _dockerfiles(repo, m)  # nothing declared: one service per Dockerfile folder (polyrepo default)
    if repo.name in m.services:  # polyrepo: a repo named after a service is that service's code
        m.services[repo.name]["source_dir"] = "."
    _source_dirs(repo, m)
    _codeowners(repo, m)
    for name, s in m.services.items():
        s["kind"] = _kind(name, s)

    names = sorted(set(m.services) | m.known, key=len, reverse=True)
    alts = "|".join(re.escape(n) for n in names)
    edge = r"(?![A-Za-z0-9-])"
    addr = re.compile(rf"[a-z][a-z0-9+.-]*://(?:[^/@\s]*@)?({alts}){edge}|(?<![A-Za-z0-9.-])({alts}):\d")
    # config: `host: ts-order-mongo`, `spring.rabbitmq.host=rabbitmq` (not `hostname:`, which names the service itself)
    keyed = re.compile(rf"(?i:host(?!name)|url|uri|addr|address|endpoint|server|upstream|broker|proxy_pass|serviceid)\w*[\"']?\s*[=:]"
                       rf"\s*\S*?(?<![A-Za-z0-9.-])({alts}){edge}")
    # code: the name passed straight to a host/URL call or a client annotation: new Hostname("payment"),
    # getServiceUrl("ts-seat-service"), @FeignClient(name = "carts"). Not any line that mentions "client".
    quoted = re.compile(rf"(?:@\w*Client|\b\w*(?i:host|url|uri|addr|endpoint|target|channel)\w*)\s*\(\s*(?:\w+\s*=\s*)?[\"']({alts})[\"']")
    # code: a host-like env var with the service as default: getenv("CART_HOST", "cart"), process.env.REDIS_HOST || 'redis'
    envdefault = re.compile(rf"(?i:[A-Z0-9_]*(?:HOST|URL|URI|ADDR|ENDPOINT|SERVER)[A-Z0-9_]*)[\"']?\s*(?:,|\|\||\?\?|\bor\b|:-)"
                            rf"\s*[\"']({alts})[\"']")

    def hits(text, patterns):
        for p in patterns:
            for h in p.finditer(text):
                yield next(g for g in h.groups() if g)

    for caller, value, f, mode in pending:
        callees = [value] if mode == "name" else hits(value, [addr])
        for callee in callees:
            m.add(caller, callee, Evidence("config", f"{_rel(repo, f)}:{_line_of(f, value)}", value.strip()[:160]))

    for name, s in m.services.items():
        if not s["source_dir"]:
            continue
        for f in _files(repo / s["source_dir"], CODE_EXT | CONFIG_EXT):
            is_config = f.suffix in CONFIG_EXT
            # Spring Cloud Config style: shared/account-service.yml holds account-service's settings, wherever it lives
            owner = f.stem if is_config and f.stem in m.services else name
            for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                if line.lstrip().startswith(COMMENT):
                    continue
                for callee in hits(line, [addr, keyed] if is_config else [addr, quoted, envdefault]):
                    m.add(owner, callee, Evidence("config" if is_config else "code", f"{_rel(repo, f)}:{i}", line.strip()[:160]))
    return m


def add_traces(m: ServiceMap, spans, where: str) -> Counter:
    """Add observed calls from spans (DataFrame with spanID, parentSpanID, serviceName).

    Returns call counts between services the repo doesn't declare, so nothing observed is silently dropped.
    """
    parent = dict(zip(spans["spanID"], spans["serviceName"]))
    calls = Counter((parent.get(p), s) for p, s in zip(spans["parentSpanID"], spans["serviceName"]) if parent.get(p))
    unknown = Counter()
    for (a, b), n in calls.items():
        if a == b:
            continue
        if a in m.services and (b in m.services or b in m.known):
            m.add(a, b, Evidence("trace", where, f"seen {n} times"))
        else:
            unknown[(a, b)] += n
    return unknown


def edge_rows(m: ServiceMap):
    rows = []
    for (a, b), evs in sorted(m.edges.items()):
        best = sorted(evs, key=lambda e: (-RANK[e.source], e))
        rows.append({"caller": a, "callee": b, "sources": sorted({e.source for e in evs}, key=lambda s: -RANK[s]),
                     "confidence": CONFIDENCE[RANK[best[0].source]], "evidence": [str(e) for e in best[:MAX_EVIDENCE]],
                     "evidence_count": len(evs)})
    return rows


# ---------- write ----------

UPSERT_SERVICES = """
UNWIND $rows AS r
MERGE (s:Service {name: r.name})
SET s.kind = r.kind, s.source_dir = r.source_dir, s.declared_in = r.declared_in, s.owner = r.owner,
    s.repo = $repo, s.last_run = $run, s.commit = $commit, s.stale = false, s.origin = 'scanner'
"""

UPSERT_CALLS = """
UNWIND $rows AS r
MATCH (a:Service {name: r.caller}), (b:Service {name: r.callee})
MERGE (a)-[c:CALLS]->(b)
SET c.sources = r.sources, c.confidence = r.confidence, c.evidence = r.evidence, c.evidence_count = r.evidence_count,
    c.repo = $repo, c.last_run = $run, c.commit = $commit, c.stale = false
"""

# Gone from this repo since the last run: keep, but mark stale, so past incidents that reference them still make sense.
MARK_STALE_SERVICES = "MATCH (s:Service {repo: $repo}) WHERE s.last_run <> $run SET s.stale = true RETURN count(s)"
MARK_STALE_CALLS = "MATCH ()-[c:CALLS {repo: $repo}]->() WHERE c.last_run <> $run SET c.stale = true RETURN count(c)"

RECORD_RUN = """
OPTIONAL MATCH (r:MapRun) WITH count(r) + 1 AS run
CREATE (:MapRun {run: run, repo: $repo, root: $root, commit: $commit, services: $services, calls: $calls, at: timestamp()})
RETURN run
"""


def apply_aliases(graph, m: ServiceMap) -> int:
    """Rename services an agent merged into their real name (merge_services), so a re-map doesn't split them again."""
    try:
        pairs = graph.ro_query("MATCH (s:Service) WHERE s.aliases IS NOT NULL UNWIND s.aliases AS a RETURN a, s.name").result_set
    except Exception:  # graph doesn't exist yet
        return 0
    real = {a: r for a, r in pairs if a != r}
    for alias in [a for a in m.services if a in real]:
        info, target = m.services.pop(alias), real[alias]
        keep = m.services.setdefault(target, info)
        for k in ("source_dir", "owner", "declared_in"):
            keep[k] = keep[k] or info[k]
    if real:
        edges = defaultdict(set)
        for (a, b), evs in m.edges.items():
            a, b = real.get(a, a), real.get(b, b)
            if a != b:
                edges[(a, b)] |= evs
        m.edges = edges
        m.known = {real.get(k, k) for k in m.known}
    return len(real)


def write(graph, m: ServiceMap) -> dict:
    apply_aliases(graph, m)
    rows = edge_rows(m)
    p = {"repo": m.repo, "commit": m.commit}
    run = graph.query(RECORD_RUN, {**p, "root": m.root, "services": len(m.services), "calls": len(rows)}).result_set[0][0]
    from nexttrace.graphdb import ensure_unique
    ensure_unique(graph, "Service", "name")
    p["run"] = run
    graph.query(UPSERT_SERVICES, {**p, "rows": [{"name": n, **{k: s[k] for k in ("kind", "source_dir", "declared_in", "owner")}}
                                               for n, s in sorted(m.services.items())]})
    graph.query(UPSERT_CALLS, {**p, "rows": rows})
    kept = recheck_inferred(graph, m.repo, Path(m.root), run) if m.root else 0
    kept += recheck_inferred_services(graph, m.repo, Path(m.root), run) if m.root else 0
    return {"inferred_kept": kept, "run": run, "repo": m.repo, "commit": m.commit, "services": len(m.services), "calls": len(rows),
            "stale_services": graph.query(MARK_STALE_SERVICES, p).result_set[0][0],
            "stale_calls": graph.query(MARK_STALE_CALLS, p).result_set[0][0],
            "by_kind": dict(Counter(s["kind"] for s in m.services.values())),
            "by_confidence": dict(Counter(r["confidence"] for r in rows)),
            "cross_repo_calls": sum(1 for r in rows if r["callee"] in m.known and r["callee"] not in m.services)}


# ---------- connections added by an AI agent (inferred, always with a verified quote) ----------

GENERIC = {"service", "services", "svc", "api", "app", "server", "srv", "web", "ts", "ms", "the", "and"}


def callee_tokens(callee: str) -> set:
    """Words a quote must contain to plausibly refer to `callee`: catalog-api -> {catalog}."""
    words = {w for w in re.split(r"[^a-z0-9]+", callee.lower()) if len(w) >= 3 and w not in GENERIC}
    return words or {callee.lower()}


def verify_quote(root: Path, file: str, line: int, quote: str, callee: str, slack: int = 3):
    """The citation check: `quote` must be real code in `file` (inside `root`), within `slack` lines of `line`,
    and must mention the callee. Returns (relative path, actual line). Raises ValueError with the reason."""
    root = root.resolve()
    path = (root / file).resolve() if not Path(file).is_absolute() else Path(file).resolve()
    if root != path and root not in path.parents:
        raise ValueError(f"{file} is outside the mapped repo {root}")
    if not path.is_file():
        raise ValueError(f"no such file in the repo: {file}")
    norm = lambda t: " ".join(t.split())
    q = norm(quote)
    if len(q) < 8:
        raise ValueError("quote too short: copy the exact code line that makes the call")
    lines = path.read_text(errors="ignore").splitlines()
    near = [i for i in range(max(1, line - slack), min(len(lines), line + slack) + 1) if q in norm(lines[i - 1])]
    if not near:
        raise ValueError(f"the quote is not in {file} near line {line}")
    if not any(t in q.lower() for t in callee_tokens(callee)):
        raise ValueError(f"the quote does not mention {callee!r}; cite the line that names or addresses it")
    return str(path.relative_to(root)), min(near, key=lambda i: abs(i - line))


INFERRED = """
MATCH (a:Service {repo: $repo})-[c:CALLS]->(b:Service)
WHERE c.sources = ['ai']
RETURN a.name, b.name, c.evidence
"""


def recheck_inferred(graph, repo, root: Path, run) -> int:
    """Keep AI-added calls whose quoted line is still in the code; the rest become stale with everything else."""
    kept = 0
    for a, b, evidence in graph.ro_query(INFERRED, {"repo": repo}).result_set:
        _, where, quote = evidence[0].split(" | ", 2)
        file, line = where.rsplit(":", 1)
        try:
            verify_quote(root, file, int(line), quote, b, slack=10_000)  # code may have moved within the file
        except ValueError:
            continue
        graph.query("MATCH (:Service {name: $a})-[c:CALLS]->(:Service {name: $b}) SET c.last_run = $run, c.stale = false",
                    {"a": a, "b": b, "run": run})
        kept += 1
    return kept


# ---------- services an AI agent added or merged (each with a verified quote) ----------

BUILD_MARKERS = {"Dockerfile", "package.json", "pom.xml", "build.gradle", "build.gradle.kts", "go.mod", "Cargo.toml",
                 "requirements.txt", "pyproject.toml", "setup.py", "Gemfile", "composer.json", "mix.exs"}
SERVICE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


def names_in(quote: str, name: str) -> bool:
    """`name` appears in the quote as a whole name, not as part of a longer one: order-service is not in order-service-main."""
    return bool(re.search(rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])", quote, re.I))


def deployable(folder: Path) -> bool:
    return folder.is_dir() and any((folder / f).exists() for f in BUILD_MARKERS) or any(folder.glob("*.csproj"))


def unmapped_folders(root: Path, mapped: list, depth: int = 5) -> list:
    """Deployable folders (a build file inside) that no mapped service covers: the checklist for missed services.
    A folder counts as covered when it is a service's folder, inside one, or contains one."""
    root, mapped = root.resolve(), [(root / d).resolve() for d in mapped if d]
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).parts
        if p == root or len(rel) > depth or not p.is_dir() or _skipped(rel) or not deployable(p):
            continue
        if any(p == d or d in p.parents or p in d.parents for d in mapped):
            continue
        if any(o in p.parents for o in out):  # report the outermost folder only
            continue
        out.append(p)
    return [str(p.relative_to(root)) for p in out]


def _norm_name(n: str) -> str:
    n = re.sub(r"[^a-z0-9]", "", n.lower())
    for suffix in ("main", "service", "svc", "app", "api", "server"):
        if n.endswith(suffix) and len(n) > len(suffix) + 2:
            n = n[: -len(suffix)]
    return n.rstrip("s")


def possible_duplicates(services: list) -> list:
    """Pairs that look like one service under two names: same or nested folder, or the same name once
    punctuation and suffixes like -main/-service are dropped (front-end / frontend, order-service / order-service-main)."""
    out = []
    for i, (a, da) in enumerate(services):
        for b, db in services[i + 1:]:
            pa, pb = Path(da) if da else None, Path(db) if db else None
            nested = pa and pb and (pa == pb or pa in pb.parents or pb in pa.parents)
            if nested or _norm_name(a) == _norm_name(b):
                out.append({"names": [a, b], "why": "same folder" if nested else "similar names"})
    return out


INFERRED_SERVICES = "MATCH (s:Service {repo: $repo, origin: 'ai'}) RETURN s.name, s.evidence"


def recheck_inferred_services(graph, repo, root: Path, run) -> int:
    """Keep agent-added services whose quoted line still exists; the rest go stale."""
    kept = 0
    for name, evidence in graph.ro_query(INFERRED_SERVICES, {"repo": repo}).result_set:
        _, where, quote = evidence[0].split(" | ", 2)
        file, line = where.rsplit(":", 1)
        try:
            verify_quote(root, file, int(line), quote, name, slack=10_000)
        except ValueError:
            continue
        graph.query("MATCH (s:Service {name: $n}) SET s.last_run = $run, s.stale = false", {"n": name, "run": run})
        kept += 1
    return kept


def known_services(graph, repo_name) -> set:
    """Services other repos already mapped into this graph (the cross-repo join keys)."""
    try:
        rows = graph.ro_query("MATCH (s:Service) WHERE s.repo <> $repo RETURN s.name", {"repo": repo_name}).result_set
    except Exception:  # graph doesn't exist yet
        return set()
    return {r[0] for r in rows}


def map_repo(path, graph_name=None, traces=(), host="localhost", port=6379) -> dict:
    """Scan a repo and write its service map into FalkorDB graph `graph_name` (default: the repo folder name)."""
    from falkordb import FalkorDB
    path = Path(path).resolve()
    graph = FalkorDB(host=host, port=port).select_graph(graph_name or path.name)
    m = scan(path, known=known_services(graph, path.name))
    unknown = Counter()
    if traces:
        import pandas as pd
        for t in traces:
            unknown += add_traces(m, pd.read_csv(t, usecols=["spanID", "parentSpanID", "serviceName"]), Path(t).name)
    summary = write(graph, m)
    from nexttrace import endpoints  # imports this module, so import late
    own, grpc_owner = endpoints.routes(path, m)
    linked = endpoints.link(endpoints.calls(path, m, grpc_owner), own + endpoints.existing_routes(graph, m.repo))
    summary.update(endpoints.write(graph, m, own, linked, summary["run"]))
    summary["graph"] = graph.name
    summary["observed_but_undeclared"] = [f"{a} -> {b} ({n})" for (a, b), n in unknown.most_common(20)]
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Map a repo's services into FalkorDB.")
    ap.add_argument("repo")
    ap.add_argument("--graph", help="graph name (default: repo folder name)")
    ap.add_argument("--traces", nargs="*", default=[], help="CSV files with spanID, parentSpanID, serviceName")
    a = ap.parse_args()
    print(json.dumps(map_repo(a.repo, a.graph, a.traces), indent=2))
