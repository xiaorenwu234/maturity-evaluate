"""Statement-level CFG builder backed by tree-sitter-python.

Produces the same ``{entry, exit, nodes, edges}`` dict schema as the legacy
``code/cfg.py`` so downstream consumers (side-car export, GNN structural
detectors) require zero changes. Node kinds and edge kinds match the legacy
builder: ``seq``, ``true``, ``false``, ``back``, ``catch``.

Why tree-sitter over stdlib ``ast``? The module standardises on
tree-sitter for all source-level analysis (CFG + clone fingerprint) so we
have a single parser dependency and can later extend to other languages
without adding stdlib-ast equivalents per language. For Python specifically
the output is semantically equivalent to the legacy builder.
"""
from __future__ import annotations

from typing import Optional

try:
    from tree_sitter import Language, Parser, Node  # type: ignore[import-untyped]
    import tree_sitter_python  # type: ignore[import-untyped]
    _LANG = Language(tree_sitter_python.language())
except ImportError:
    _LANG = None  # Deferred: build_cfg raises at call time if unavailable.


class _TSCFGBuilder:
    """Frontier-based CFG builder walking a tree-sitter function node."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.nodes: list[dict] = []
        self.edges: list[dict] = []
        self._i = 0
        self._exit: Optional[int] = None
        self._loop_stack: list[int] = []

    # ---------------------------------------------------------- primitives
    def _add(self, kind: str, line: Optional[int]) -> int:
        idx = self._i
        self._i += 1
        label = ""
        if line and 1 <= line <= len(self.lines):
            label = self.lines[line - 1].strip()[:80]
        self.nodes.append({"id": f"s{idx}", "kind": kind, "line": line, "label": label})
        return idx

    def _link(self, a: Optional[int], b: Optional[int], kind: str = "seq") -> None:
        if a is not None and b is not None:
            self.edges.append({"from": f"s{a}", "to": f"s{b}", "kind": kind})

    def _link_all(self, frontier: list[int], target: Optional[int], kind: str = "seq") -> None:
        if target is None:
            return
        for f in frontier:
            self._link(f, target, kind)

    # ----------------------------------------------------------- blocks
    def _block(self, children: list[Node]) -> tuple[Optional[int], list[int]]:
        entry: Optional[int] = None
        frontier: list[int] = []
        for child in children:
            if child.type == "comment" or child.type == "newline":
                continue
            e, x = self._stmt(child)
            if e is None:
                continue
            if entry is None:
                entry = e
            self._link_all(frontier, e)
            frontier = x
            if not frontier:
                return entry, []
        return entry, frontier

    # ------------------------------------------------------------ stmts
    def _stmt(self, node: Node) -> tuple[Optional[int], list[int]]:
        t = node.type
        line = node.start_point[0] + 1  # tree-sitter is 0-based

        if t in ("return_statement", "raise_statement"):
            nid = self._add(t, line)
            self._link(nid, self._exit)
            return nid, []

        if t == "if_statement":
            return self._if(node)
        if t in ("for_statement", "while_statement"):
            return self._loop(node, t)
        if t == "try_statement":
            return self._try(node)
        if t in ("with_statement",):
            return self._with(node)
        if t in ("break_statement", "continue_statement"):
            return self._jump(node, t)
        if t in ("function_definition", "class_definition"):
            nid = self._add(f"nested:{t}", line)
            return nid, [nid]

        # Everything else is a simple statement.
        nid = self._add(t, line)
        return nid, [nid]

    def _if(self, node: Node) -> tuple[int, list[int]]:
        line = node.start_point[0] + 1
        head = self._add("Branch", line)
        # tree-sitter if_statement children: condition, block (body), elif*, else?
        body_node = node.child_by_field_name("consequence")
        t_entry, t_exit = ([], []) if body_node is None else self._block(list(body_node.children))
        self._link(head, t_entry, "true")

        alt = node.child_by_field_name("alternative")
        if alt is not None:
            f_entry, f_exit = self._block(list(alt.children))
            self._link(head, f_entry, "false")
        else:
            f_entry, f_exit = None, [head]

        exits = [x for x in (t_exit or [head]) + (f_exit or [head]) if x is not None]
        return head, exits

    def _loop(self, node: Node, kind: str) -> tuple[int, list[int]]:
        line = node.start_point[0] + 1
        head = self._add(kind, line)
        self._loop_stack.append(head)
        body = node.child_by_field_name("body")
        b_entry, b_exit = ([], []) if body is None else self._block(list(body.children))
        self._link(head, b_entry, "true")
        for x in b_exit:
            self._link(x, head, "back")
        self._loop_stack.pop()
        exits: list[int] = [head]
        alt = node.child_by_field_name("alternative")
        if alt is not None:
            o_entry, o_exit = self._block(list(alt.children))
            self._link(head, o_entry, "false")
            exits = o_exit or [head]
        return head, exits

    def _try(self, node: Node) -> tuple[int, list[int]]:
        line = node.start_point[0] + 1
        body = node.child_by_field_name("body")
        b_entry, b_exit = ([], []) if body is None else self._block(list(body.children))
        head = b_entry if b_entry is not None else self._add("Try", line)
        h_exits: list[int] = []
        for child in node.children:
            if child.type == "except_clause":
                ex_body = child.child_by_field_name("body") or child
                h_e, h_x = self._block(list(ex_body.children))
                self._link(head, h_e, "catch")
                h_exits.extend(h_x or ([h_e] if h_e is not None else []))
        final = node.child_by_field_name("finally")
        if final is not None:
            f_entry, f_exit = self._block(list(final.children))
            tail = (b_exit + h_exits) or [head]
            self._link_all(tail, f_entry)
            return head, (f_exit or tail)
        return head, (b_exit + h_exits) or [head]

    def _with(self, node: Node) -> tuple[int, list[int]]:
        line = node.start_point[0] + 1
        body = node.child_by_field_name("body")
        b_entry, b_exit = ([], []) if body is None else self._block(list(body.children))
        head = self._add("with_statement", line)
        self._link(head, b_entry)
        return head, b_exit or [head]

    def _jump(self, node: Node, kind: str) -> tuple[int, list[int]]:
        line = node.start_point[0] + 1
        nid = self._add(kind, line)
        if self._loop_stack:
            self._link(nid, self._loop_stack[-1], "back")
        return nid, []

    # ------------------------------------------------------------ build
    def build(self, fn_node: Node) -> dict:
        end_line = fn_node.end_point[0] + 1
        self._exit = self._add("EXIT", end_line)
        body = fn_node.child_by_field_name("body")
        children = list(body.children) if body else []
        entry, frontier = self._block(children)
        if entry is None:
            entry = self._add("ENTRY", fn_node.start_point[0] + 1)
            frontier = [entry]
        self._link_all(frontier, self._exit)
        return {
            "entry": f"s{entry}",
            "exit": f"s{self._exit}",
            "nodes": self.nodes,
            "edges": self.edges,
        }


def build_cfg(source: bytes | str, func_name: str) -> Optional[dict]:
    """Build a statement-level CFG dict for the named function in ``source``.

    Returns ``None`` if the function cannot be found or parsing fails.
    Output schema matches ``code/cfg.py`` exactly: ``{entry, exit, nodes, edges}``.
    """
    if _LANG is None:
        raise ImportError(
            "tree-sitter stack required for CFG building. "
            "Install via `pip install tree-sitter>=0.21 tree-sitter-python>=0.21`."
        )
    if isinstance(source, str):
        source = source.encode("utf-8")
    parser = Parser(_LANG)
    tree = parser.parse(source)
    lines = source.decode("utf-8", errors="replace").splitlines()

    # Walk to find the target function definition.
    target: Optional[Node] = None
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in ("function_definition",) and node.child_by_field_name("name"):
            name_node = node.child_by_field_name("name")
            if name_node and name_node.text.decode("utf-8", errors="replace") == func_name:
                target = node
                break
        stack.extend(reversed(node.children))

    if target is None:
        return None
    return _TSCFGBuilder(lines).build(target)
