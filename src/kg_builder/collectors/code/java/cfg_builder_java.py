"""Statement-level CFG builder for Java backed by tree-sitter-java.

Produces the same ``{entry, exit, nodes, edges}`` dict schema as the Python
``cfg_builder.py`` so downstream consumers require zero changes. Node kinds
and edge kinds match: ``seq``, ``true``, ``false``, ``back``, ``catch``.
"""
from __future__ import annotations

from typing import Optional

try:
    from tree_sitter import Language, Parser, Node  # type: ignore[import-untyped]
    import tree_sitter_java  # type: ignore[import-untyped]
    _LANG = Language(tree_sitter_java.language())
except ImportError:
    _LANG = None  # Deferred: build_cfg raises at call time if unavailable.


def build_java_cfg(source: bytes | str, func_name: str) -> Optional[dict]:
    """Build a statement-level CFG dict for the named Java method in ``source``.

    Returns ``None`` if the method cannot be found or parsing fails.
    Output schema matches ``cfg_builder.py`` exactly: ``{entry, exit, nodes, edges}``.
    """
    if _LANG is None:
        raise ImportError(
            "tree-sitter-java stack required for Java CFG building. "
            "Install via `pip install tree-sitter>=0.21 tree-sitter-java>=0.21`."
        )

    if isinstance(source, str):
        source = source.encode("utf-8")

    parser = Parser(_LANG)
    tree = parser.parse(source)

    # Find the target method declaration.
    method_node = _find_method(tree.root_node, func_name)
    if method_node is None:
        return None

    body = method_node.child_by_field_name("body")
    if body is None:
        return {"entry": "ENTRY", "exit": "EXIT", "nodes": ["ENTRY", "EXIT"], "edges": []}

    builder = _JavaCFGBuild(body, source)
    return builder.build()


def _find_method(node: Node, name: str) -> Optional[Node]:
    """Recursively find a method_declaration node with the given name."""
    if node.type == "method_declaration":
        name_node = node.child_by_field_name("name")
        if name_node and name_node.text.decode("utf-8") == name:
            return node
    for child in node.children:
        result = _find_method(child, name)
        if result is not None:
            return result
    return None


class _JavaCFGBuild:
    """Build CFG from a Java method body block."""

    def __init__(self, body_node: Node, source: bytes):
        self.body = body_node
        self.source = source
        self.nodes: list[str] = ["ENTRY", "EXIT"]
        self.edges: list[tuple[str, str, str]] = []
        self._stmt_counter = 0

    def _next_id(self) -> str:
        self._stmt_counter += 1
        return f"S{self._stmt_counter}"

    def build(self) -> dict:
        entry_id = "ENTRY"
        first_stmts = self._process_block(self.body, entry_id, "EXIT")
        if not first_stmts:
            # Empty body: ENTRY -> EXIT
            self.edges.append((entry_id, "EXIT", "seq"))
        return {
            "entry": "ENTRY",
            "exit": "EXIT",
            "nodes": self.nodes,
            "edges": [(s, t, k) for s, t, k in self.edges],
        }

    def _process_block(self, block_node: Node, pred: str, exit_id: str) -> list[str]:
        """Process a block of statements. Returns list of successor node IDs."""
        successors = []
        current_pred = pred

        for child in block_node.children:
            if child.type in ("{", "}", ";"):
                continue

            stmt_ids = self._process_statement(child, current_pred, exit_id)
            if stmt_ids:
                successors.extend(stmt_ids)
                # For sequential flow, next statement's predecessor is the last
                # node of current statement (unless it's a terminal like return).
                if child.type != "return_statement":
                    current_pred = stmt_ids[-1]
                else:
                    # Return terminates this path; subsequent statements are unreachable
                    # but we still process them for completeness.
                    current_pred = exit_id

        return successors

    def _process_statement(self, node: Node, pred: str, exit_id: str) -> list[str]:
        """Process a single statement. Returns list of node IDs that are successors."""
        t = node.type

        if t == "expression_statement":
            sid = self._next_id()
            self.nodes.append(sid)
            self.edges.append((pred, sid, "seq"))
            return [sid]

        elif t == "local_variable_declaration":
            sid = self._next_id()
            self.nodes.append(sid)
            self.edges.append((pred, sid, "seq"))
            return [sid]

        elif t == "return_statement":
            sid = self._next_id()
            self.nodes.append(sid)
            self.edges.append((pred, sid, "seq"))
            self.edges.append((sid, exit_id, "seq"))
            return [sid]

        elif t == "if_statement":
            return self._process_if(node, pred, exit_id)

        elif t == "while_statement":
            return self._process_while(node, pred, exit_id)

        elif t == "for_statement":
            return self._process_for(node, pred, exit_id)

        elif t == "try_statement":
            return self._process_try(node, pred, exit_id)

        elif t == "block":
            return self._process_block(node, pred, exit_id)

        elif t == "throw_statement":
            sid = self._next_id()
            self.nodes.append(sid)
            self.edges.append((pred, sid, "seq"))
            self.edges.append((sid, exit_id, "seq"))
            return [sid]

        else:
            # Other statements (switch, synchronized, etc.) - treat as opaque.
            sid = self._next_id()
            self.nodes.append(sid)
            self.edges.append((pred, sid, "seq"))
            return [sid]

    def _process_if(self, node: Node, pred: str, exit_id: str) -> list[str]:
        cond_sid = self._next_id()
        self.nodes.append(cond_sid)
        self.edges.append((pred, cond_sid, "seq"))

        # Then branch
        then_body = node.child_by_field_name("consequence")
        then_succs = self._process_statement(then_body, cond_sid, exit_id) if then_body else []

        # Else branch
        else_body = node.child_by_field_name("alternative")
        if else_body:
            else_succs = self._process_statement(else_body, cond_sid, exit_id)
        else:
            else_succs = [cond_sid]  # No else: condition false goes to exit

        # Add true/false edges from condition
        if then_succs:
            self.edges.append((cond_sid, then_succs[0], "true"))
        if else_succs and else_body:
            self.edges.append((cond_sid, else_succs[0], "false"))
        elif not else_body:
            self.edges.append((cond_sid, exit_id, "false"))

        # Successors are the exits of both branches
        return then_succs + (else_succs if else_body else [])

    def _process_while(self, node: Node, pred: str, exit_id: str) -> list[str]:
        cond_sid = self._next_id()
        self.nodes.append(cond_sid)
        self.edges.append((pred, cond_sid, "seq"))

        body = node.child_by_field_name("body")
        if body:
            body_succs = self._process_statement(body, cond_sid, exit_id)
            # Back edge from end of body to condition
            for s in body_succs:
                self.edges.append((s, cond_sid, "back"))

        # False branch exits loop
        self.edges.append((cond_sid, exit_id, "false"))
        return [cond_sid]

    def _process_for(self, node: Node, pred: str, exit_id: str) -> list[str]:
        # Simplified: treat for as while-like loop
        cond_sid = self._next_id()
        self.nodes.append(cond_sid)
        self.edges.append((pred, cond_sid, "seq"))

        body = node.child_by_field_name("body")
        if body:
            body_succs = self._process_statement(body, cond_sid, exit_id)
            for s in body_succs:
                self.edges.append((s, cond_sid, "back"))

        self.edges.append((cond_sid, exit_id, "false"))
        return [cond_sid]

    def _process_try(self, node: Node, pred: str, exit_id: str) -> list[str]:
        try_body = node.child_by_field_name("body")
        if try_body:
            try_succs = self._process_statement(try_body, pred, exit_id)
        else:
            try_succs = [pred]

        # Catch blocks
        catch_nodes = [c for c in node.children if c.type == "catch_clause"]
        all_succs = list(try_succs)

        for catch in catch_nodes:
            catch_body = catch.child_by_field_name("body")
            if catch_body:
                catch_entry = self._next_id()
                self.nodes.append(catch_entry)
                # Exception edge from try entry to catch
                self.edges.append((pred, catch_entry, "catch"))
                catch_succs = self._process_statement(catch_body, catch_entry, exit_id)
                all_succs.extend(catch_succs)

        # Finally block
        finally_clause = next((c for c in node.children if c.type == "finally_clause"), None)
        if finally_clause:
            finally_body = finally_clause.child_by_field_name("body")
            if finally_body:
                finally_entry = self._next_id()
                self.nodes.append(finally_entry)
                # All paths go through finally
                for s in all_succs:
                    self.edges.append((s, finally_entry, "seq"))
                finally_succs = self._process_statement(finally_body, finally_entry, exit_id)
                return finally_succs

        return all_succs
