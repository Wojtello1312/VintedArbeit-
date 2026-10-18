"""Architecture contracts: static cycles, domain boundaries and isolated imports."""

import ast
import json
import subprocess
import sys
from pathlib import Path


def test_import_graph_has_no_cycles_and_domain_stays_independent():
    root = Path(__file__).parents[2] / "src"
    modules = {
        ".".join(p.relative_to(root).with_suffix("").parts).removesuffix(".__init__"): p
        for p in root.rglob("*.py")
        if p.name != "env.py"
    }
    graph = {name: set() for name in modules}
    for name, path in modules.items():
        for node in ast.walk(ast.parse(path.read_text())):
            imported = (
                [node.module]
                if isinstance(node, ast.ImportFrom) and node.module
                else ([alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            )
            for dependency in imported:
                if dependency in modules and dependency != name:
                    graph[name].add(dependency)
                if name.startswith("app.core."):
                    assert not dependency.startswith(
                        (
                            "fastapi",
                            "sqlalchemy",
                            "openai",
                            "app.admin",
                            "app.scheduler",
                            "app.storage",
                        )
                    )
    done = set()

    def visit(name, stack):
        assert name not in stack, " -> ".join([*stack, name])
        if name in done:
            return
        for dependency in graph[name]:
            visit(dependency, [*stack, name])
        done.add(name)

    for name in modules:
        visit(name, [])
    # Alembic env.py is a runner script, not an importable application module.
    names = [
        name
        for name in modules
        if ".migrations.versions." not in name and not name.endswith(".__main__")
    ]
    script = (
        "import importlib,json\nfor name in json.loads("
        + repr(json.dumps(names))
        + "): importlib.import_module(name)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
