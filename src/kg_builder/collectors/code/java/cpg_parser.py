"""Parse Joern dot export into call-edge lookup tables for Java.

Joern's dot export produces one .dot file per method in a directory tree.
Each dot file contains METHOD nodes (definitions) and CALL nodes (invocations)
with their attributes. We parse these to build a lookup table mapping
``(file, line) -> callee_full_name``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class JavaMethodRecord:
    full_name: str       # e.g. "com.example.OrderService.createOrder:void"
    name: str            # bare method name
    class_name: str      # owning class
    file: str
    line: int
    end_line: int


@dataclass
class JavaCallRecord:
    caller_full_name: str
    callee_full_name: str
    file: str
    line: int


@dataclass
class JavaCallIndex:
    methods_by_file: Dict[str, List[JavaMethodRecord]] = field(default_factory=dict)
    calls_by_pos: Dict[Tuple[str, int], List[JavaCallRecord]] = field(default_factory=dict)
    methods_by_name: Dict[str, JavaMethodRecord] = field(default_factory=dict)

    def find_enclosing_method(self, file: str, line: int) -> Optional[JavaMethodRecord]:
        methods = self.methods_by_file.get(file, [])
        best: Optional[JavaMethodRecord] = None
        for m in methods:
            if m.line <= line <= m.end_line:
                if best is None or (m.end_line - m.line) < (best.end_line - best.line):
                    best = m
        return best

    def get_calls_at(self, file: str, line: int) -> List[JavaCallRecord]:
        return self.calls_by_pos.get((file, line), [])


@dataclass
class ResolvedJavaCall:
    callee_qname: str
    caller_qname: Optional[str] = None


def _parse_dot_attributes(line: str) -> dict[str, str]:
    """Extract key=value pairs from a dot node/edge definition."""
    attrs = {}
    # Match KEY="value" or KEY=value patterns
    for match in re.finditer(r'(\w+)="([^"]*)"', line):
        attrs[match.group(1)] = match.group(2)
    for match in re.finditer(r'(\w+)=([^\s\[\];]+)', line):
        key, val = match.group(1), match.group(2)
        if key not in attrs:  # Prefer quoted values
            attrs[key] = val
    return attrs


def _clean_java_qname(full_name: str) -> str:
    """Clean Joern's Java full name format to our qname convention.

    Joern: ``com.example.OrderService.createOrder:void(java.lang.String,double)``
    Ours:  ``com.example.OrderService.createOrder``
    """
    # Strip return type and parameters: everything after ':'
    if ':' in full_name:
        full_name = full_name.split(':')[0]
    return full_name.strip()


def parse_dot_export(dot_dir: Path) -> JavaCallIndex:
    """Parse all .dot files in the export directory into a JavaCallIndex."""
    index = JavaCallIndex()

    if not dot_dir.exists():
        return index

    # Walk all .dot files recursively
    for dot_file in sorted(dot_dir.rglob("*.dot")):
        try:
            content = dot_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        # Parse nodes and edges from this dot file
        methods_in_file: list[JavaMethodRecord] = []
        calls_in_file: list[tuple[str, int, str]] = []  # (caller_fn, line, callee_fn)

        current_method: Optional[str] = None
        current_file: Optional[str] = None

        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue

            # Node definition: "id" [label="TYPE" ATTR1="val1" ...];
            if re.match(r'^"[^"]+"\s*\[', line):
                attrs = _parse_dot_attributes(line)
                label = attrs.get("label", "")

                if label == "METHOD":
                    full_name = attrs.get("FULL_NAME", "")
                    name = attrs.get("NAME", "")
                    filename = attrs.get("FILENAME", "")
                    line_num = int(attrs.get("LINE_NUMBER", "0"))
                    line_end = int(attrs.get("LINE_NUMBER_END", str(line_num)))

                    if full_name and filename:
                        clean_name = _clean_java_qname(full_name)
                        # Extract class name from full_name (second-to-last segment)
                        parts = clean_name.rsplit(".", 1)
                        class_name = parts[0].rsplit(".", 1)[-1] if len(parts) > 1 else ""

                        rec = JavaMethodRecord(
                            full_name=clean_name,
                            name=name,
                            class_name=class_name,
                            file=filename,
                            line=line_num,
                            end_line=line_end,
                        )
                        methods_in_file.append(rec)
                        index.methods_by_name[clean_name] = rec
                        current_method = clean_name
                        current_file = filename

                elif label == "CALL":
                    callee_full = attrs.get("METHOD_FULL_NAME", "")
                    line_num = int(attrs.get("LINE_NUMBER", "0"))

                    if callee_full and current_method and current_file:
                        # Skip operator calls like <operator>.assignment
                        if not callee_full.startswith("<"):
                            clean_callee = _clean_java_qname(callee_full)
                            calls_in_file.append((current_method, line_num, clean_callee))

        # Add methods to index
        for m in methods_in_file:
            if m.file not in index.methods_by_file:
                index.methods_by_file[m.file] = []
            index.methods_by_file[m.file].append(m)

        # Add calls to index
        for caller_fn, line_num, callee_fn in calls_in_file:
            # Find the file for this call (use caller's file)
            caller_rec = index.methods_by_name.get(caller_fn)
            call_file = caller_rec.file if caller_rec else ""

            if call_file:
                key = (call_file, line_num)
                if key not in index.calls_by_pos:
                    index.calls_by_pos[key] = []
                index.calls_by_pos[key].append(JavaCallRecord(
                    caller_full_name=caller_fn,
                    callee_full_name=callee_fn,
                    file=call_file,
                    line=line_num,
                ))

    return index


def resolve_java_call(
    index: JavaCallIndex, file: str, line: int, callee_hint: str = "",
) -> Optional[ResolvedJavaCall]:
    """Resolve a Java call site using the Joern CPG index.

    Falls back to simple heuristic matching when Joern index has no entry
    for this position.
    """
    calls = index.get_calls_at(file, line)
    if not calls:
        # Try ±1 line tolerance.
        for delta in (-1, 1):
            calls = index.get_calls_at(file, line + delta)
            if calls:
                break

    if calls:
        # Pick best match by callee hint.
        if callee_hint and len(calls) > 1:
            chosen = next(
                (c for c in calls if c.callee_full_name.endswith(callee_hint) or callee_hint in c.callee_full_name),
                calls[0],
            )
        else:
            chosen = calls[0]

        enclosing = index.find_enclosing_method(file, line)
        return ResolvedJavaCall(
            callee_qname=chosen.callee_full_name,
            caller_qname=enclosing.full_name if enclosing else None,
        )

    # No heuristic fallback - if Joern CPG doesn't have this call site,
    # return None (follows "no silent fallback" principle).
    return None
