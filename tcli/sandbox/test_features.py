"""Feature registry integrity: unique names, dep targets exist, no cycles."""
from .fixtures import get, post


def register(runner):

    def test_init():
        """POST /api/features/init should populate the registry."""
        _, data = post("/api/features/init", expect=200)
        assert data["ok"] is True

    def test_list():
        """GET /api/features should return a non-empty list."""
        _, data = get("/api/features", expect=200)
        assert len(data) > 0

    def test_unique_names():
        """Feature names must be unique."""
        _, data = get("/api/features", expect=200)
        names = [f["name"] for f in data]
        assert len(names) == len(set(names)), f"duplicate names: {[n for n in names if names.count(n) > 1]}"

    def test_deps_exist():
        """Every depends_on target must reference an existing feature name."""
        _, data = get("/api/features", expect=200)
        names = {f["name"] for f in data}
        for f in data:
            deps = f.get("depends_on") or []
            if isinstance(deps, str):
                import json
                deps = json.loads(deps)
            for dep in deps:
                assert dep in names, f"feature '{f['name']}' depends on '{dep}' which does not exist"

    def test_no_cycles():
        """Dependency graph must be acyclic (no circular deps)."""
        import json
        _, data = get("/api/features", expect=200)
        graph = {}
        for f in data:
            deps = f.get("depends_on") or []
            if isinstance(deps, str):
                deps = json.loads(deps)
            graph[f["name"]] = deps

        # Topological sort via DFS
        UNVISITED, IN_PROGRESS, DONE = 0, 1, 2
        state = {n: UNVISITED for n in graph}

        def visit(node):
            if state.get(node) == DONE:
                return
            if state.get(node) == IN_PROGRESS:
                raise AssertionError(f"cycle detected involving '{node}'")
            state[node] = IN_PROGRESS
            for dep in graph.get(node, []):
                visit(dep)
            state[node] = DONE

        for name in graph:
            visit(name)

    def test_contract_review_exists():
        """Contract Review feature should be in the registry."""
        _, data = get("/api/features", expect=200)
        names = {f["name"] for f in data}
        assert "Contract Review" in names

    runner.test("features:init", test_init)
    runner.test("features:list", test_list)
    runner.test("features:unique_names", test_unique_names)
    runner.test("features:deps_exist", test_deps_exist)
    runner.test("features:no_cycles", test_no_cycles)
    runner.test("features:contract_review", test_contract_review_exists)
