"""Workspace-level invariants: every member imports, and package boundaries hold."""

import ast
import importlib
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MEMBER_DIRS = sorted(p.parent for p in ROOT.glob("packages/*/pyproject.toml")) + sorted(
    p.parent for p in ROOT.glob("services/*/pyproject.toml")
)


def _module_name(member: Path) -> str:
    (module_dir,) = (p for p in (member / "src").iterdir() if p.is_dir())
    return module_dir.name


def _imported_top_level_modules(source: Path) -> set[str]:
    tree = ast.parse(source.read_text(), filename=str(source))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_members_were_discovered() -> None:
    assert len(MEMBER_DIRS) >= 14


@pytest.mark.parametrize("member", MEMBER_DIRS, ids=lambda p: p.name)
def test_member_imports_and_is_typed(member: Path) -> None:
    module = _module_name(member)
    importlib.import_module(module)
    assert (member / "src" / module / "py.typed").exists()
    project = tomllib.loads((member / "pyproject.toml").read_text())["project"]
    assert project["requires-python"] == ">=3.12"


def test_kairos_evals_is_standalone() -> None:
    """kairos_evals must stay extractable into its own repo (ADR-004)."""
    evals_dir = ROOT / "packages" / "kairos_evals"
    offenders = {
        f"{path.relative_to(ROOT)} imports {name}"
        for path in evals_dir.rglob("*.py")
        for name in _imported_top_level_modules(path)
        if name.startswith("kairos_") and name != "kairos_evals"
    }
    assert not offenders

    project = tomllib.loads((evals_dir / "pyproject.toml").read_text())["project"]
    assert not [d for d in project["dependencies"] if d.startswith("kairos-")]
