"""Variable extraction and optionality inference from a Jinja template.

The template is the single source of *which* variables exist; the DB holds what
they *mean*. We parse the AST (never execute) and infer:

  * the variable set — every undeclared name referenced;
  * optionality — a name is REQUIRED if it has at least one usage that would fail
    under StrictUndefined, and OPTIONAL if every usage is guarded. Guards are:
      - an enclosing ``{% if name %}`` / ``{% if name is defined %}`` block,
      - the tested expression of such an ``if`` (the name in the condition),
      - a ``{{ name | default(...) }}`` filter,
      - the iterable of a ``{% for x in name %}`` loop (empty iterable is benign).

This yields the design's example: ``customer_name`` required; ``plan_name`` and
``history`` optional.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jinja2 import Environment, nodes
from jinja2 import meta as jinja_meta


@dataclass
class ExtractedVars:
    names: frozenset[str] = field(default_factory=frozenset)
    required: frozenset[str] = field(default_factory=frozenset)
    optional: frozenset[str] = field(default_factory=frozenset)
    includes: tuple[str, ...] = ()  # static include targets (string literals)

    def as_dict(self) -> dict[str, dict]:
        return {
            "names": sorted(self.names),
            "required": sorted(self.required),
            "optional": sorted(self.optional),
            "includes": list(self.includes),
        }


def _guard_names(test: nodes.Node) -> set[str]:
    """Names whose truthiness in ``test`` guarantees they are defined in the body."""

    if isinstance(test, nodes.Name):
        return {test.name}
    if isinstance(test, nodes.Test) and test.name in ("defined",):
        if isinstance(test.node, nodes.Name):
            return {test.node.name}
    if isinstance(test, nodes.And):
        return _guard_names(test.left) | _guard_names(test.right)
    return set()


def _names_in(node: nodes.Node) -> set[str]:
    return {n.name for n in node.find_all(nodes.Name) if n.ctx == "load"}


def _walk(node: nodes.Node, guarded: frozenset[str], hard: set[str]) -> None:
    if isinstance(node, nodes.If):
        # Names anywhere in the test are guard usages, so we never descend into it
        # for hard usages. The body/elif are additionally guarded by whatever the
        # test proves defined ({% if name %}); the else branch is not.
        gnames = _guard_names(node.test)
        body_guard = guarded | gnames
        for child in node.body:
            _walk(child, body_guard, hard)
        for child in node.elif_:
            _walk(child, guarded, hard)
        for child in node.else_:
            _walk(child, guarded, hard)
        return

    if isinstance(node, nodes.For):
        # The iterable is a guard usage (an empty/undefined iterable renders nothing),
        # so we don't descend into it. The loop variable is Jinja-scoped and already
        # excluded from the undeclared set.
        for child in node.body:
            _walk(child, guarded, hard)
        for child in node.else_:
            _walk(child, guarded, hard)
        return

    if isinstance(node, nodes.Filter) and node.name == "default":
        # {{ x | default(...) }} — x is safe; still walk the default's arguments.
        for arg in node.args:
            _walk(arg, guarded, hard)
        for kw in node.kwargs:
            _walk(kw, guarded, hard)
        return

    if isinstance(node, nodes.Test) and node.name in ("defined", "undefined"):
        return  # `x is defined` — x is a guard usage, not a hard one

    if isinstance(node, nodes.Name):
        if node.ctx == "load" and node.name not in guarded:
            hard.add(node.name)
        return

    for child in node.iter_child_nodes():
        _walk(child, guarded, hard)


def _static_includes(ast: nodes.Node) -> tuple[str, ...]:
    out: list[str] = []
    for inc in ast.find_all(nodes.Include):
        tmpl = inc.template
        if isinstance(tmpl, nodes.Const) and isinstance(tmpl.value, str):
            out.append(tmpl.value)
    for imp in ast.find_all((nodes.Import, nodes.FromImport)):
        tmpl = imp.template
        if isinstance(tmpl, nodes.Const) and isinstance(tmpl.value, str):
            out.append(tmpl.value)
    return tuple(dict.fromkeys(out))  # de-dup, preserve order


def extract(source: str, env: Environment | None = None) -> ExtractedVars:
    """Parse ``source`` and return the variable set + inferred optionality."""

    env = env or Environment()
    ast = env.parse(source)
    names = frozenset(jinja_meta.find_undeclared_variables(ast))

    hard: set[str] = set()
    _walk(ast, frozenset(), hard)
    required = frozenset(hard & names)
    optional = frozenset(names - required)

    return ExtractedVars(
        names=names,
        required=required,
        optional=optional,
        includes=_static_includes(ast),
    )
