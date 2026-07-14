from __future__ import annotations

import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location("ark_mcp_main", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
ark_mcp_main = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ark_mcp_main)


class FakeResult:
    def __init__(self, records):
        self._records = records

    def __iter__(self):
        return iter(self._records)


class FakeSession:
    def __init__(self, records, run_calls):
        self._records = records
        self._run_calls = run_calls

    def run(self, cypher, params=None):
        self._run_calls.append((cypher, params))
        return FakeResult(self._records)


class FakeSessionContext:
    def __init__(self, records, run_calls):
        self._records = records
        self._run_calls = run_calls

    def __enter__(self):
        return FakeSession(self._records, self._run_calls)

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeDriver:
    def __init__(self, records, close_error=None):
        self.records = records
        self.run_calls = []
        self.session_calls = []
        self.closed = False
        self.close_error = close_error

    def session(self, **kwargs):
        self.session_calls.append(kwargs)
        return FakeSessionContext(self.records, self.run_calls)

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


def _install_fake_connection(monkeypatch, records):
    driver = FakeDriver(records)
    monkeypatch.setattr(
        ark_mcp_main, "_get_connection", lambda graph_name: (driver, "test-db")
    )
    return driver


class FakeRecord:
    def __init__(self, values):
        self._values = values

    def items(self):
        return self._values.items()

    def get(self, key, default=None):
        return self._values.get(key, default)

    def __getitem__(self, key):
        return self._values[key]


class FakeGraphNode:
    def __init__(self, element_id, labels, properties):
        self.element_id = element_id
        self.labels = labels
        self._properties = properties

    def __iter__(self):
        return iter(self._properties.items())


def test_get_connection_uses_env_and_caches_driver(monkeypatch):
    class DriverFactory:
        def __init__(self):
            self.calls = []

        def __call__(self, uri, auth):
            self.calls.append((uri, auth))
            return "driver-object"

    factory = DriverFactory()
    monkeypatch.setenv("NEO4J_URI", "bolt://127.0.0.1:7687")
    monkeypatch.setenv("NEO4J_USERNAME", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    monkeypatch.setenv("NEO4J_DATABASE", "app-db")
    monkeypatch.setattr(ark_mcp_main.GraphDatabase, "driver", factory)
    monkeypatch.setattr(ark_mcp_main, "_connections", {})

    first = ark_mcp_main._get_connection("anything")
    second = ark_mcp_main._get_connection("else")

    assert first == ("driver-object", "app-db")
    assert second == ("driver-object", "app-db")
    assert factory.calls == [("bolt://127.0.0.1:7687", ("neo4j", "secret"))]


def test_get_connection_ignores_graph_name_for_fixed_connection(monkeypatch):
    monkeypatch.setenv("NEO4J_URI", "bolt://127.0.0.1:7687")
    monkeypatch.setenv("NEO4J_USERNAME", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    monkeypatch.setattr(
        ark_mcp_main.GraphDatabase, "driver", lambda uri, auth: "driver-object"
    )
    monkeypatch.setattr(ark_mcp_main, "_connections", {})

    first = ark_mcp_main._get_connection("killbill")
    second = ark_mcp_main._get_connection("spring-petclinic")

    assert first == ("driver-object", "neo4j")
    assert second == ("driver-object", "neo4j")


def test_evict_connection_closes_cached_driver(monkeypatch):
    driver = FakeDriver([])
    monkeypatch.setattr(
        ark_mcp_main, "_connections", {"__default__": (driver, "neo4j")}
    )

    ark_mcp_main._evict_connection("unused")

    assert driver.closed is True
    assert ark_mcp_main._connections == {}


def test_evict_connection_ignores_close_errors(monkeypatch):
    driver = FakeDriver([], close_error=RuntimeError("boom"))
    monkeypatch.setattr(
        ark_mcp_main, "_connections", {"__default__": (driver, "neo4j")}
    )

    ark_mcp_main._evict_connection("unused")

    assert driver.closed is True
    assert ark_mcp_main._connections == {}


def test_node_ref_includes_readable_display():
    node = FakeGraphNode(
        "node-1",
        {"Function", "Code"},
        {
            "qualified_name": "org.example.Service#run()",
            "name": "run",
        },
    )

    result = ark_mcp_main._node_ref(node)

    assert result == {
        "id": "node-1",
        "labels": ["Code", "Function"],
        "display": "org.example.Service#run()",
    }


def test_query_graph_passes_params_and_respects_limit(monkeypatch):
    driver = _install_fake_connection(
        monkeypatch,
        [
            {"value": 1},
            {"value": 2},
            {"value": 3},
        ],
    )

    result = ark_mcp_main.queryGraph(
        graph_name="demo",
        cypher="RETURN $name AS value",
        params={"name": "ark"},
        limit=2,
    )

    assert result == [{"value": 1}, {"value": 2}]
    assert driver.run_calls == [("RETURN $name AS value", {"name": "ark"})]
    assert driver.session_calls == [
        {"database": "test-db", "default_access_mode": ark_mcp_main.READ_ACCESS}
    ]


def test_get_schema_uses_property_schema_queries(monkeypatch):
    driver = _install_fake_connection(monkeypatch, [])

    ark_mcp_main.getSchema("demo")

    cypher, params = driver.run_calls[0]
    assert "CALL db.schema.nodeTypeProperties()" in cypher
    assert "CALL db.schema.relTypeProperties()" in cypher
    assert "RETURN 'node' AS kind" in cypher
    assert "RETURN 'relationship' AS kind" in cypher
    assert params == {}


def _set_available_graphs(monkeypatch, graphs):
    monkeypatch.setattr(ark_mcp_main, "_list_available_graphs", lambda: graphs)
    monkeypatch.setattr(
        ark_mcp_main,
        "_fetch_graph_schema",
        lambda graph_name: {"kind": "schema", "graph": graph_name},
    )
    monkeypatch.setattr(
        ark_mcp_main,
        "_list_graph_codebases",
        lambda graph_name: [f"{graph_name}-repo"],
    )


def test_bootstrap_returns_single_graph_context(monkeypatch):
    _set_available_graphs(monkeypatch, [{"name": "local", "user_domain": "local"}])

    result = ark_mcp_main.bootstrap()

    assert result == {
        "graphs": ["local"],
        "graph": "local",
        "schema": {"kind": "schema", "graph": "local"},
        "codebases": ["local-repo"],
        "requires_graph_selection": False,
    }


def test_bootstrap_requires_graph_selection_when_multiple_entitled_graphs(monkeypatch):
    # two entitled (non-public) graphs -> the user must choose
    _set_available_graphs(
        monkeypatch,
        [
            {"name": "alpha", "user_domain": "acme.com"},
            {"name": "beta", "user_domain": "acme.com"},
        ],
    )

    result = ark_mcp_main.bootstrap()

    assert result == {
        "graphs": ["alpha", "beta"],
        "graph": None,
        "schema": None,
        "codebases": None,
        "requires_graph_selection": True,
    }


def test_bootstrap_auto_selects_single_entitled_graph_over_public(monkeypatch):
    # enterprise user with one granted graph should never see the picker, even
    # though public OSS graphs are also visible via RLS
    _set_available_graphs(
        monkeypatch,
        [
            {"name": "acme-app", "user_domain": "acme.com"},
            {"name": "spring-petclinic", "user_domain": "public"},
            {"name": "killbill", "user_domain": "public"},
        ],
    )

    result = ark_mcp_main.bootstrap()

    assert result["graph"] == "acme-app"
    assert result["requires_graph_selection"] is False
    assert result["codebases"] == ["acme-app-repo"]


def test_bootstrap_prompts_among_entitled_only_when_multiple(monkeypatch):
    _set_available_graphs(
        monkeypatch,
        [
            {"name": "acme-app", "user_domain": "acme.com"},
            {"name": "partner-shared", "user_domain": "partner.com"},  # email/xdomain grant
            {"name": "spring-petclinic", "user_domain": "public"},
        ],
    )

    result = ark_mcp_main.bootstrap()

    # the picker offers only the entitled graphs, not the OSS noise
    assert result["requires_graph_selection"] is True
    assert result["graphs"] == ["acme-app", "partner-shared"]


def test_bootstrap_prompts_among_all_when_only_public(monkeypatch):
    # OSS-only user (no grants) still gets to choose among the public graphs
    _set_available_graphs(
        monkeypatch,
        [
            {"name": "killbill", "user_domain": "public"},
            {"name": "spring-petclinic", "user_domain": "public"},
        ],
    )

    result = ark_mcp_main.bootstrap()

    assert result["requires_graph_selection"] is True
    assert result["graphs"] == ["killbill", "spring-petclinic"]


def test_bootstrap_resolves_explicit_graph_name(monkeypatch):
    _set_available_graphs(
        monkeypatch,
        [
            {"name": "alpha", "user_domain": "acme.com"},
            {"name": "beta", "user_domain": "acme.com"},
        ],
    )

    result = ark_mcp_main.bootstrap("beta")

    assert result == {
        "graphs": ["alpha", "beta"],
        "graph": "beta",
        "schema": {"kind": "schema", "graph": "beta"},
        "codebases": ["beta-repo"],
        "requires_graph_selection": False,
    }


def test_bootstrap_rejects_unknown_graph_name(monkeypatch):
    _set_available_graphs(monkeypatch, [{"name": "alpha", "user_domain": "acme.com"}])

    try:
        ark_mcp_main.bootstrap("missing")
    except ValueError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_list_graphs_returns_local_in_local_mode(monkeypatch):
    _install_fake_connection(monkeypatch, [])
    result = ark_mcp_main.listGraphs()
    assert result["graphs"] == ["local"]
    assert "schema" in result


def test_list_graphs_calls_supabase_when_jwt_is_set(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            # looms rows with the embedded instance name (PostgREST FK expansion)
            return [
                {"user_domain": "acme.com", "neo4j_instances": {"name": "alpha"}},
                {"user_domain": "public", "neo4j_instances": {"name": "beta"}},
            ]

    monkeypatch.setattr(ark_mcp_main.httpx, "get", lambda *a, **kw: FakeResponse())
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")
    _install_fake_connection(monkeypatch, [])
    token = ark_mcp_main._current_user_jwt.set("test-jwt")
    try:
        result = ark_mcp_main.listGraphs()
    finally:
        ark_mcp_main._current_user_jwt.reset(token)

    assert result["graphs"] == ["alpha", "beta"]
    assert "schema" in result


def test_list_graphs_via_supabase_parses_looms_embed(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [
                {"user_domain": "acme.com", "neo4j_instances": {"name": "acme-app"}},
                {"user_domain": "public", "neo4j_instances": {"name": "killbill"}},
                {"user_domain": "public", "neo4j_instances": None},  # loom w/o instance
                # same instance seen via a public and a granted loom -> granted wins
                {"user_domain": "public", "neo4j_instances": {"name": "shared"}},
                {"user_domain": "acme.com", "neo4j_instances": {"name": "shared"}},
            ]

    monkeypatch.setattr(ark_mcp_main.httpx, "get", lambda *a, **kw: FakeResponse())
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")

    graphs = ark_mcp_main._list_graphs_via_supabase("jwt")

    assert graphs == [
        {"name": "acme-app", "user_domain": "acme.com"},
        {"name": "killbill", "user_domain": "public"},
        {"name": "shared", "user_domain": "acme.com"},
    ]


def test_fetch_instance_credentials_raises_when_not_found(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    monkeypatch.setattr(ark_mcp_main.httpx, "get", lambda *a, **kw: FakeResponse())
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")

    try:
        ark_mcp_main._fetch_instance_credentials("jwt", "missing-graph")
    except ValueError as exc:
        assert "missing-graph" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_get_connection_uses_supabase_in_cloud_mode(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"uri": "bolt://cloud:7687", "username": "u", "password": "p"}]

    monkeypatch.setattr(ark_mcp_main.httpx, "get", lambda *a, **kw: FakeResponse())
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")
    monkeypatch.setattr(
        ark_mcp_main.GraphDatabase,
        "driver",
        lambda uri, auth: f"driver-for-{uri}",
    )
    monkeypatch.setattr(ark_mcp_main, "_connections", {})

    domain_tok = ark_mcp_main._current_user_domain.set("acme.com")
    jwt_tok = ark_mcp_main._current_user_jwt.set("test-jwt")
    try:
        driver, database = ark_mcp_main._get_connection("my-graph")
    finally:
        ark_mcp_main._current_user_domain.reset(domain_tok)
        ark_mcp_main._current_user_jwt.reset(jwt_tok)

    assert driver == "driver-for-bolt://cloud:7687"
    assert database == "neo4j"


def test_list_codebases_returns_scalar_values(monkeypatch):
    _install_fake_connection(
        monkeypatch,
        [
            FakeRecord({"codebase_id": "alpha"}),
            FakeRecord({"codebase_id": "beta"}),
        ],
    )

    result = ark_mcp_main.listCodebases("demo")

    assert result == ["alpha", "beta"]


def test_list_codebases_query_excludes_empty_string_ids(monkeypatch):
    driver = _install_fake_connection(monkeypatch, [])

    ark_mcp_main.listCodebases("demo")

    cypher, _ = driver.run_calls[0]
    assert "n.codebase_id <> ''" in cypher


def test_list_codebases_filters_out_empty_and_null_ids(monkeypatch):
    # unattributed stubs/ghosts can carry codebase_id '' or null; neither is a codebase
    _install_fake_connection(
        monkeypatch,
        [
            FakeRecord({"codebase_id": "alpha"}),
            FakeRecord({"codebase_id": ""}),
            FakeRecord({"codebase_id": None}),
            FakeRecord({"codebase_id": "beta"}),
        ],
    )

    assert ark_mcp_main.listCodebases("demo") == ["alpha", "beta"]


def test_trim_properties_drops_noise_and_truncates_long_strings():
    long_desc = "x" * 5000
    trimmed = ark_mcp_main._trim_properties(
        {
            "name": "run",
            "content_sha256": "deadbeef" * 8,
            "description": long_desc,
        }
    )

    assert "content_sha256" not in trimmed  # machine-only checksum, pure noise
    assert trimmed["name"] == "run"
    assert trimmed["description"].endswith("…[truncated]")
    assert len(trimmed["description"]) <= ark_mcp_main._MAX_PROPERTY_CHARS + len(
        "…[truncated]"
    )


def test_trim_properties_leaves_small_values_untouched():
    props = {"name": "x", "aliases": ["a", "b"], "start_line": 10}

    assert ark_mcp_main._trim_properties(props) == props


def test_node_payload_trims_properties():
    node = FakeGraphNode(
        "n1", {"Code", "Function"}, {"name": "run", "content_sha256": "z" * 64}
    )

    payload = ark_mcp_main._node_payload(node)

    assert payload["properties"] == {"name": "run"}


def test_node_payload_stamps_graph_name_when_provided():
    node = FakeGraphNode("n1", {"Code", "File"}, {"file_path": "a.py"})

    stamped = ark_mcp_main._node_payload(node, "billing-graph")
    unstamped = ark_mcp_main._node_payload(node)

    assert stamped["graph"] == "billing-graph"
    assert "graph" not in unstamped


def test_query_graph_surfaces_neo4j5_hint_on_syntax_error(monkeypatch):
    from neo4j.exceptions import CypherSyntaxError

    class RaisingSession:
        def run(self, cypher, params=None):
            raise CypherSyntaxError("Unknown function 'EXISTS'")

    class RaisingCtx:
        def __enter__(self):
            return RaisingSession()

        def __exit__(self, *a):
            return False

    class RaisingDriver:
        def session(self, **kwargs):
            return RaisingCtx()

    monkeypatch.setattr(
        ark_mcp_main, "_get_connection", lambda graph_name: (RaisingDriver(), "db")
    )

    try:
        ark_mcp_main.queryGraph("demo", "MATCH (n) WHERE EXISTS(n.x) RETURN n")
    except ValueError as exc:
        message = str(exc)
        assert "Neo4j 5" in message
        assert "EXISTS" in message  # original driver error is preserved
    else:
        raise AssertionError("expected ValueError with a dialect hint")


def test_build_context_query_compiles_groups_and_return():
    query = ark_mcp_main._build_context_query(
        "MATCH (n:Thing {id: $id})",
        "n",
        [
            ark_mcp_main._Expansion(name="a_paths", var="a", collect="(n)-[:R]->(:X)"),
            ark_mcp_main._Expansion(
                name="b_paths",
                var="b",
                collect="(other)-[:S]->(mid)",
                binds=("(n)-[:T]->(mid:Y)",),
            ),
        ],
    )

    assert query.startswith("MATCH (n:Thing {id: $id})")
    assert "WITH n" in query
    assert "OPTIONAL MATCH (n)-[:T]->(mid:Y)" in query  # bind, no path var
    assert "OPTIONAL MATCH a = (n)-[:R]->(:X)" in query
    assert "OPTIONAL MATCH b = (other)-[:S]->(mid)" in query
    assert "collect(DISTINCT a)[..$limit] AS a_paths" in query
    assert "collect(DISTINCT b)[..$limit] AS b_paths" in query
    assert query.rstrip().endswith("RETURN n, a_paths, b_paths")
    # one bind + two collected paths, and no stray OPTIONAL MATCH for the bind-less group
    assert query.count("OPTIONAL MATCH") == 3


def test_build_context_query_rejects_path_var_reused_as_node_var():
    # Neo4j rejects `MATCH ui = (ui:X)-[...]->()`; catch it at build time
    try:
        ark_mcp_main._build_context_query(
            "MATCH (n:Thing)",
            "n",
            [
                ark_mcp_main._Expansion(
                    name="ui_paths", var="ui", collect="(ui:UIComponent)-[:R]->(n)"
                )
            ],
        )
    except ValueError as exc:
        assert "ui" in str(exc)
    else:
        raise AssertionError("expected ValueError for reused path variable")


def test_build_context_query_allows_var_as_substring_of_node_var():
    # a node var that merely starts with the path var name must not false-positive
    query = ark_mcp_main._build_context_query(
        "MATCH (n:Thing)",
        "n",
        [
            ark_mcp_main._Expansion(
                name="ui_paths", var="ui", collect="(uiComponent:X)-[:R]->(n)"
            )
        ],
    )

    assert "OPTIONAL MATCH ui = (uiComponent:X)-[:R]->(n)" in query


def test_get_file_context_uses_expected_query_shape(monkeypatch):
    driver = _install_fake_connection(monkeypatch, [])

    ark_mcp_main.getFileContext("demo", "src/app.py", codebase_id="backend", limit=12)

    cypher, params = driver.run_calls[0]
    assert "MATCH (file:Code:File {file_path: $file_path})" in cypher
    assert "WHERE ($codebase_id IS NULL OR file.codebase_id = $codebase_id)" in cypher
    assert (
        "OPTIONAL MATCH (file)-[:HAS_TYPE|HAS_FUNC*1..3]->(owned_func:Code:Function)"
        in cypher
    )
    assert "collect(DISTINCT owned_path)[..$limit]" in cypher
    assert "collect(DISTINCT inbound)[..$limit]" in cypher
    assert "collect(DISTINCT data_link)[..$limit]" in cypher
    assert "collect(DISTINCT step_link)[..$limit]" in cypher
    assert "collect(DISTINCT entity_step_link)[..$limit]" in cypher
    assert "OPTIONAL MATCH (file)-[:HAS_FUNC]->(owned_func:Code:Function)" not in cypher
    assert (
        "RETURN file, owned_paths, inbound_paths, data_paths, step_paths, entity_step_paths"
        in cypher
    )
    assert params == {"codebase_id": "backend", "file_path": "src/app.py", "limit": 12}


def test_get_file_context_includes_rendered_ui(monkeypatch):
    driver = _install_fake_connection(monkeypatch, [])

    ark_mcp_main.getFileContext("demo", "resources/views/invoice.blade.php", limit=12)

    cypher, _ = driver.run_calls[0]
    # the deterministic UI layer grounds a page/component to its file via USES_RESOURCE
    assert "(:Business:UIComponent)-[:USES_RESOURCE]->(file)" in cypher
    assert "collect(DISTINCT ui_link)[..$limit] AS ui_paths" in cypher
    assert (
        "RETURN file, owned_paths, inbound_paths, data_paths, step_paths, "
        "entity_step_paths, ui_paths" in cypher
    )


def test_get_file_context_omits_codebase_filter_when_not_provided(monkeypatch):
    driver = _install_fake_connection(monkeypatch, [])

    ark_mcp_main.getFileContext("demo", "src/app.py", limit=12)

    _, params = driver.run_calls[0]
    assert params == {"codebase_id": None, "file_path": "src/app.py", "limit": 12}


def test_serialize_records_omits_all_null_scalar_rows():
    result = ark_mcp_main._serialize_records(
        [
            {"inbound": None, "data_link": None},
            {"inbound": None, "data_link": "present"},
        ]
    )

    assert result == [{"inbound": None, "data_link": "present"}]


def test_get_feature_context_uses_expected_query_shape(monkeypatch):
    driver = _install_fake_connection(monkeypatch, [])

    ark_mcp_main.getFeatureContext("demo", "Billing", limit=8)

    cypher, params = driver.run_calls[0]
    assert "MATCH (feature:Business:Feature {name: $feature_name})" in cypher
    assert "collect(DISTINCT feature_path)[..$limit]" in cypher
    assert "collect(DISTINCT func_path)[..$limit]" in cypher
    assert "collect(DISTINCT entity_path)[..$limit]" in cypher
    assert "RETURN feature, feature_paths, func_paths, entity_paths" in cypher
    assert params == {"feature_name": "Billing", "limit": 8}


def test_get_feature_context_includes_step_order_and_ui(monkeypatch):
    driver = _install_fake_connection(monkeypatch, [])

    ark_mcp_main.getFeatureContext("demo", "Billing", limit=8)

    cypher, _ = driver.run_calls[0]
    # step order lives only in NEXT_STEP; HAS_STEP collection order is meaningless
    assert "-[:NEXT_STEP]->(:Business:Step)" in cypher
    assert "collect(DISTINCT next_step)[..$limit] AS next_step_paths" in cypher
    # deterministic UI layer: Feature -[:USES_UI]-> UIComponent (page)
    assert "(feature)-[:USES_UI]->(:Business:UIComponent)" in cypher
    assert "collect(DISTINCT ui)[..$limit] AS ui_paths" in cypher
    assert (
        "RETURN feature, feature_paths, func_paths, entity_paths, "
        "next_step_paths, ui_paths" in cypher
    )


def test_get_data_flow_omits_codebase_filter_when_not_provided(monkeypatch):
    driver = _install_fake_connection(monkeypatch, [])

    ark_mcp_main.getDataFlow("demo", "PaymentTransaction", limit=5)

    cypher, params = driver.run_calls[0]
    assert "MATCH (entity:Data:DataEntity)" in cypher
    assert "entity.codebase_id = $codebase_id" in cypher
    assert "canonical_business_name" in cypher
    assert "coalesce(entity.aliases, [])" in cypher
    assert "ORDER BY" in cypher
    assert "LIMIT 1" not in cypher
    assert "collect(DISTINCT lifecycle)[..$limit]" in cypher
    assert "collect(DISTINCT association)[..$limit]" in cypher
    assert "RETURN entity, lifecycle_paths, association_paths" in cypher
    assert params == {
        "entity_name": "PaymentTransaction",
        "codebase_id": None,
        "limit": 5,
    }


def test_get_data_flow_adds_codebase_filter_when_provided(monkeypatch):
    driver = _install_fake_connection(monkeypatch, [])

    ark_mcp_main.getDataFlow(
        "demo", "PaymentTransaction", codebase_id="billing", limit=5
    )

    cypher, params = driver.run_calls[0]
    assert "MATCH (entity:Data:DataEntity)" in cypher
    assert "entity.codebase_id = $codebase_id" in cypher
    assert params == {
        "entity_name": "PaymentTransaction",
        "codebase_id": "billing",
        "limit": 5,
    }


def test_get_data_flow_passes_default_limit_param(monkeypatch):
    driver = _install_fake_connection(monkeypatch, [])

    ark_mcp_main.getDataFlow("demo", "Subscription")

    _, params = driver.run_calls[0]
    assert params["limit"] == 50


def test_get_call_chain_uses_requested_depth(monkeypatch):
    driver = _install_fake_connection(monkeypatch, [])

    ark_mcp_main.getCallChain(
        "demo", "billing.Service.process", codebase_id="billing", depth=3, limit=9
    )

    cypher, params = driver.run_calls[0]
    assert "MATCH (fn:Code:Function {qualified_name: $function_fqn})" in cypher
    assert "WHERE ($codebase_id IS NULL OR fn.codebase_id = $codebase_id)" in cypher
    assert "CALLS_FUNC*1..3" in cypher
    assert "collect(DISTINCT outbound)[..$limit]" in cypher
    assert "(caller:Code:Function)-[:CALLS_FUNC]->(fn)" in cypher
    assert "collect(DISTINCT inbound)[..$limit]" in cypher
    assert "RETURN fn, outbound_paths, inbound_paths" in cypher
    assert params == {
        "codebase_id": "billing",
        "function_fqn": "billing.Service.process",
        "limit": 9,
    }


def test_search_code_uses_expected_query_shape(monkeypatch):
    driver = _install_fake_connection(monkeypatch, [])

    ark_mcp_main.searchCode("demo", "Payment", codebase_id="billing", limit=10)

    cypher, params = driver.run_calls[0]
    assert "MATCH (n:Code)" in cypher
    assert "($codebase_id IS NULL OR n.codebase_id = $codebase_id)" in cypher
    assert "toLower(coalesce(n.name, '')) CONTAINS toLower($query)" in cypher
    assert "toLower(coalesce(n.qualified_name, '')) CONTAINS toLower($query)" in cypher
    assert "toLower(coalesce(n.file_path, '')) CONTAINS toLower($query)" in cypher
    # deterministic ordering so a truncated (LIMIT) result set is stable across calls
    assert "ORDER BY coalesce(n.qualified_name, n.name, n.file_path)" in cypher
    assert params == {"query": "Payment", "codebase_id": "billing", "limit": 10}


def test_search_code_omits_codebase_filter_when_not_provided(monkeypatch):
    driver = _install_fake_connection(monkeypatch, [])

    ark_mcp_main.searchCode("demo", "Payment")

    _, params = driver.run_calls[0]
    assert params == {"query": "Payment", "codebase_id": None, "limit": 25}


def test_get_call_chain_omits_codebase_filter_when_not_provided(monkeypatch):
    driver = _install_fake_connection(monkeypatch, [])

    ark_mcp_main.getCallChain("demo", "billing.Service.process", depth=3, limit=9)

    _, params = driver.run_calls[0]
    assert params == {
        "codebase_id": None,
        "function_fqn": "billing.Service.process",
        "limit": 9,
    }


def test_semantic_tools_reject_non_positive_limit(monkeypatch):
    _install_fake_connection(monkeypatch, [])

    for tool, args in [
        (ark_mcp_main.queryGraph, ("demo", "RETURN 1")),
        (ark_mcp_main.getFileContext, ("demo", "src/app.py")),
        (ark_mcp_main.getFeatureContext, ("demo", "Billing")),
        (ark_mcp_main.getDataFlow, ("demo", "PaymentTransaction")),
        (ark_mcp_main.getCallChain, ("demo", "billing.Service.process")),
        (ark_mcp_main.searchCode, ("demo", "Payment")),
    ]:
        try:
            tool(*args, limit=0)
        except ValueError as exc:
            assert "limit must be >= 1" in str(exc)
        else:
            raise AssertionError(f"{tool.__name__} accepted a non-positive limit")


def test_get_call_chain_rejects_non_positive_depth(monkeypatch):
    _install_fake_connection(monkeypatch, [])

    try:
        ark_mcp_main.getCallChain("demo", "billing.Service.process", depth=0)
    except ValueError as exc:
        assert "depth must be >= 1" in str(exc)
    else:
        raise AssertionError("getCallChain accepted a non-positive depth")


def test_validate_jwt_accepts_hs256_token_via_jwt_secret(monkeypatch):
    import jwt as pyjwt

    secret = "test-secret"
    token = pyjwt.encode(
        {"email": "alice@acme.com", "sub": "u1"}, secret, algorithm="HS256"
    )
    monkeypatch.setenv("SUPABASE_JWT_SECRET", secret)
    monkeypatch.delenv("SUPABASE_URL", raising=False)

    payload = ark_mcp_main._validate_jwt(token)

    assert payload["email"] == "alice@acme.com"


def test_validate_jwt_rejects_bad_hs256_token(monkeypatch):
    import jwt as pyjwt

    token = pyjwt.encode({"email": "alice@acme.com"}, "wrong-secret", algorithm="HS256")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "correct-secret")

    try:
        ark_mcp_main._validate_jwt(token)
    except Exception:
        pass
    else:
        raise AssertionError("expected validation to fail")


def test_validate_jwt_accepts_es256_token_via_jwks(monkeypatch):
    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()
    token = pyjwt.encode(
        {"email": "alice@acme.com", "sub": "u1"},
        private_key,
        algorithm="ES256",
        headers={"kid": "test-key"},
    )

    class FakeSigningKey:
        key = public_key

    class FakeJwksClient:
        def get_signing_key_from_jwt(self, token):
            return FakeSigningKey()

    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    monkeypatch.setattr(ark_mcp_main, "_get_jwks_client", lambda: FakeJwksClient())

    payload = ark_mcp_main._validate_jwt(token)

    assert payload["email"] == "alice@acme.com"


def test_validate_jwt_uses_jwks_for_es256_even_when_jwt_secret_exists(monkeypatch):
    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()
    token = pyjwt.encode(
        {"email": "alice@acme.com", "sub": "u1"},
        private_key,
        algorithm="ES256",
        headers={"kid": "test-key"},
    )

    class FakeSigningKey:
        key = public_key

    class FakeJwksClient:
        def get_signing_key_from_jwt(self, token):
            return FakeSigningKey()

    monkeypatch.setenv("SUPABASE_JWT_SECRET", "legacy-secret")
    monkeypatch.setattr(ark_mcp_main, "_get_jwks_client", lambda: FakeJwksClient())

    payload = ark_mcp_main._validate_jwt(token)

    assert payload["email"] == "alice@acme.com"


def _run_middleware(middleware, path="/mcp", auth_header=None):
    """Run an AuthMiddleware call to completion and return (status, body)."""
    import asyncio

    scope = {
        "type": "http",
        "path": path,
        "headers": ([[b"authorization", auth_header.encode()]] if auth_header else []),
    }
    sent = []

    async def send(event):
        sent.append(event)

    async def fake_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = ark_mcp_main.AuthMiddleware(fake_app)
    asyncio.run(app(scope, None, send))
    start = next(e for e in sent if e["type"] == "http.response.start")
    body_event = next(e for e in sent if e["type"] == "http.response.body")
    return start["status"], body_event["body"]


def test_auth_middleware_passes_through_when_no_supabase_url(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)

    status, body = _run_middleware(None)

    assert status == 200
    assert body == b"ok"


def test_auth_middleware_returns_401_when_token_missing(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")

    status, body = _run_middleware(None)

    assert status == 401


def test_auth_middleware_sets_context_vars_for_valid_token(monkeypatch):
    import asyncio

    import jwt as pyjwt

    secret = "test-secret"
    token = pyjwt.encode({"email": "alice@acme.com"}, secret, algorithm="HS256")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", secret)

    captured = {}

    async def fake_app(scope, receive, send):
        captured["domain"] = ark_mcp_main._current_user_domain.get()
        captured["jwt"] = ark_mcp_main._current_user_jwt.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    scope = {
        "type": "http",
        "path": "/mcp",
        "headers": [[b"authorization", f"Bearer {token}".encode()]],
    }
    sent = []

    async def send(event):
        sent.append(event)

    app = ark_mcp_main.AuthMiddleware(fake_app)
    asyncio.run(app(scope, None, send))

    assert captured["domain"] == "acme.com"
    assert captured["jwt"] == token


def test_auth_middleware_allows_well_known_without_token(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")

    status, body = _run_middleware(None, path="/.well-known/oauth-authorization-server")

    assert status == 200


def test_oauth_protected_resource_endpoint_returns_base_url(monkeypatch):
    import asyncio
    import json

    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")

    sent = []

    async def send(event):
        sent.append(event)

    scope = {
        "type": "http",
        "path": "/.well-known/oauth-protected-resource",
        "headers": [[b"host", b"ark-mcp.example.com"]],
    }
    asyncio.run(ark_mcp_main._handle_oauth_protected_resource(scope, None, send))

    body = json.loads(
        next(e for e in sent if e["type"] == "http.response.body")["body"]
    )
    assert body["resource"] == "https://ark-mcp.example.com/mcp"
    assert body["authorization_servers"] == [
        "https://proj.supabase.co/auth/v1",
    ]
    assert body["scopes_supported"] == ["openid", "email", "profile"]


def test_send_401_includes_resource_metadata_hint():
    import asyncio

    sent = []

    async def send(event):
        sent.append(event)

    scope = {"type": "http", "headers": [[b"host", b"ark-mcp.example.com"]]}
    asyncio.run(ark_mcp_main._send_401(scope, send))

    start = next(e for e in sent if e["type"] == "http.response.start")
    www_auth = dict(start["headers"])[b"www-authenticate"].decode()
    assert (
        'resource_metadata="https://ark-mcp.example.com/.well-known/oauth-protected-resource"'
        in www_auth
    )
    assert 'scope="openid email profile"' in www_auth


def test_oauth_metadata_endpoint_returns_supabase_urls(monkeypatch):
    import asyncio
    import json

    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")

    sent = []

    async def send(event):
        sent.append(event)

    asyncio.run(ark_mcp_main._handle_oauth_metadata({}, None, send))

    start = next(e for e in sent if e["type"] == "http.response.start")
    body = next(e for e in sent if e["type"] == "http.response.body")
    data = json.loads(body["body"])

    assert start["status"] == 200
    assert data["issuer"] == "https://proj.supabase.co/auth/v1"
    assert (
        data["authorization_endpoint"]
        == "https://proj.supabase.co/auth/v1/oauth/authorize"
    )
    assert data["token_endpoint"] == "https://proj.supabase.co/auth/v1/oauth/token"
    assert data["jwks_uri"] == "https://proj.supabase.co/auth/v1/.well-known/jwks.json"
    assert data["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert data["token_endpoint_auth_methods_supported"] == [
        "client_secret_basic",
        "client_secret_post",
        "none",
    ]
    assert "S256" in data["code_challenge_methods_supported"]


def _reset_activity(monkeypatch):
    """Isolate the in-process activity aggregator and capture background flushes.

    Marks the flusher daemon as already-started so _record_activity never spawns
    a real thread, and replaces the executor so _drain_activity's flushes are
    captured instead of hitting the network."""
    monkeypatch.setattr(ark_mcp_main, "_activity_state", {})
    monkeypatch.setattr(ark_mcp_main, "_activity_flusher_started", True)
    submitted = []

    class FakeExecutor:
        def submit(self, fn, *args):
            submitted.append((fn, args))

    monkeypatch.setattr(ark_mcp_main, "_activity_executor", FakeExecutor())
    return submitted


def _with_identity(user_jwt, user_id, fn):
    jwt_tok = ark_mcp_main._current_user_jwt.set(user_jwt)
    id_tok = ark_mcp_main._current_user_id.set(user_id)
    try:
        return fn()
    finally:
        ark_mcp_main._current_user_jwt.reset(jwt_tok)
        ark_mcp_main._current_user_id.reset(id_tok)


def test_record_activity_noop_without_identity(monkeypatch):
    # local mode / unauthenticated: no jwt + user_id -> nothing to attribute
    _reset_activity(monkeypatch)

    ark_mcp_main._record_activity("bootstrap", "demo")

    assert ark_mcp_main._activity_state == {}


def test_record_activity_accumulates_in_memory_without_blocking(monkeypatch):
    # the hot path only buffers; the network write is left to the drain
    submitted = _reset_activity(monkeypatch)

    def calls():
        ark_mcp_main._record_activity("bootstrap", "demo")
        ark_mcp_main._record_activity("queryGraph", "demo")
        ark_mcp_main._record_activity("searchCode", "demo")

    _with_identity("jwt", "user-1", calls)

    assert submitted == []  # nothing flushed synchronously
    assert ark_mcp_main._activity_state["user-1"]["count"] == 3
    assert ark_mcp_main._activity_state["user-1"]["tool"] == "searchCode"


def test_drain_activity_flushes_accumulated_count(monkeypatch):
    submitted = _reset_activity(monkeypatch)

    def calls():
        ark_mcp_main._record_activity("bootstrap", "demo")
        ark_mcp_main._record_activity("queryGraph", "demo")

    _with_identity("jwt", "user-1", calls)

    ark_mcp_main._drain_activity()

    assert len(submitted) == 1
    fn, args = submitted[0]
    assert fn is ark_mcp_main._flush_activity
    # (user_jwt, tool, graph, count, session_id)
    assert args[0] == "jwt"
    assert args[3] == 2  # both calls coalesced into one write
    # counter is reset but the user is retained (was active this interval)
    assert ark_mcp_main._activity_state["user-1"]["count"] == 0


def test_drain_activity_evicts_idle_users(monkeypatch):
    # a user with nothing buffered for a full interval is dropped to bound memory
    submitted = _reset_activity(monkeypatch)
    ark_mcp_main._activity_state["idle-user"] = {
        "count": 0,
        "session_id": "s",
        "jwt": "j",
        "tool": "bootstrap",
        "graph": None,
    }

    ark_mcp_main._drain_activity()

    assert "idle-user" not in ark_mcp_main._activity_state
    assert submitted == []


def test_drain_then_idle_interval_evicts(monkeypatch):
    # active -> flushed and kept; next idle drain -> evicted
    submitted = _reset_activity(monkeypatch)
    _with_identity("jwt", "user-1", lambda: ark_mcp_main._record_activity("bootstrap"))

    ark_mcp_main._drain_activity()  # flush, keep (count reset to 0)
    assert "user-1" in ark_mcp_main._activity_state
    ark_mcp_main._drain_activity()  # idle this interval -> evict

    assert "user-1" not in ark_mcp_main._activity_state
    assert len(submitted) == 1  # only the first drain wrote anything


def test_record_activity_starts_flusher_once(monkeypatch):
    monkeypatch.setattr(ark_mcp_main, "_activity_state", {})
    monkeypatch.setattr(ark_mcp_main, "_activity_flusher_started", False)
    threads = []

    class FakeThread:
        def __init__(self, *args, **kwargs):
            threads.append(kwargs.get("name"))

        def start(self):
            pass

    monkeypatch.setattr(ark_mcp_main.threading, "Thread", FakeThread)

    def calls():
        ark_mcp_main._record_activity("bootstrap")
        ark_mcp_main._record_activity("queryGraph")

    _with_identity("jwt", "user-1", calls)

    assert threads == ["mcp-activity-flush"]  # started exactly once
    assert ark_mcp_main._activity_flusher_started is True


def test_flush_activity_posts_expected_rpc(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return FakeResponse()

    monkeypatch.setattr(ark_mcp_main.httpx, "post", fake_post)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")

    ark_mcp_main._flush_activity("jwt-x", "bootstrap", "demo", 3, "session-1")

    assert captured["url"] == (
        "https://example.supabase.co/rest/v1/rpc/record_mcp_activity"
    )
    assert captured["json"] == {
        "p_tool": "bootstrap",
        "p_graph": "demo",
        "p_count": 3,
        "p_session_id": "session-1",
    }
    assert captured["headers"]["Authorization"] == "Bearer jwt-x"
    assert captured["headers"]["apikey"] == "anon-key"


def test_flush_activity_swallows_errors(monkeypatch):
    # telemetry must never break a query, even if Supabase is unreachable
    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(ark_mcp_main.httpx, "post", boom)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")

    # no exception should escape
    ark_mcp_main._flush_activity("jwt", "tool", None, 1, "session")


def test_tool_records_activity(monkeypatch):
    _install_fake_connection(monkeypatch, [])
    recorded = []
    monkeypatch.setattr(
        ark_mcp_main,
        "_record_activity",
        lambda tool, graph=None: recorded.append((tool, graph)),
    )

    ark_mcp_main.searchCode("demo", "Payment")

    assert ("searchCode", "demo") in recorded


def test_auth_middleware_sets_user_id_from_sub(monkeypatch):
    import asyncio

    import jwt as pyjwt

    secret = "test-secret"
    token = pyjwt.encode(
        {"email": "alice@acme.com", "sub": "user-123"}, secret, algorithm="HS256"
    )
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", secret)

    captured = {}

    async def fake_app(scope, receive, send):
        captured["user_id"] = ark_mcp_main._current_user_id.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    scope = {
        "type": "http",
        "path": "/mcp",
        "headers": [[b"authorization", f"Bearer {token}".encode()]],
    }
    sent = []

    async def send(event):
        sent.append(event)

    app = ark_mcp_main.AuthMiddleware(fake_app)
    asyncio.run(app(scope, None, send))

    assert captured["user_id"] == "user-123"
    # context var is reset after the request completes
    assert ark_mcp_main._current_user_id.get() is None


def test_main_defaults_to_http_transport(monkeypatch):
    called = {}

    def fake_run_http(host="127.0.0.1", port=8000):
        called["host"] = host
        called["port"] = port

    monkeypatch.setattr(ark_mcp_main, "_run_http", fake_run_http)

    ark_mcp_main.main([])

    assert called == {"host": "0.0.0.0", "port": 8000}


def test_main_http_defaults_accept_host_and_port(monkeypatch):
    called = {}

    def fake_run_http(host="127.0.0.1", port=8000):
        called["host"] = host
        called["port"] = port

    monkeypatch.setattr(ark_mcp_main, "_run_http", fake_run_http)

    ark_mcp_main.main(["--host", "0.0.0.0", "--port", "9000"])

    assert called == {"host": "0.0.0.0", "port": 9000}
