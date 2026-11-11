"""Parse an ``index.scip`` file into lookup tables for call resolution.

The index is a protobuf ``scip.Index`` message. We decode the wire format
directly instead of shelling out to ``protoc --decode_raw``: the textual dump
is a best-effort rendering whose layout shifts between protoc releases (it
mangles non-ASCII paths and mis-nests fields), which silently produced empty
indexes on real repositories.

Hard-failure semantics: if the index is missing or malformed, raise
immediately. There is no fallback.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

#: ``SymbolRole.Definition`` -- a bitflag, not an enum. ``role`` may carry
#: several bits at once (e.g. definition + read access).
_ROLE_DEFINITION = 1


@dataclass
class DefinitionRecord:
    symbol: str
    qname: str
    file: str
    start_line: int       # 1-based inclusive
    end_line: int         # 1-based inclusive


@dataclass
class ReferenceRecord:
    symbol: str
    qname: str
    file: str
    line: int             # 1-based


@dataclass
class ScipIndex:
    definitions_by_file: Dict[str, List[DefinitionRecord]] = field(default_factory=dict)
    references_by_pos: Dict[Tuple[str, int], List[ReferenceRecord]] = field(default_factory=dict)
    symbols: set = field(default_factory=set)

    def find_enclosing_definition(self, file: str, line: int) -> Optional[DefinitionRecord]:
        defs = self.definitions_by_file.get(file)
        if not defs:
            return None
        best: Optional[DefinitionRecord] = None
        for d in defs:
            if d.start_line <= line <= d.end_line:
                if best is None or (d.end_line - d.start_line) < (best.end_line - best.start_line):
                    best = d
        return best

    def get_references_at(self, file: str, line: int) -> List[ReferenceRecord]:
        """Return references at exactly ``line`` or within ±1 line tolerance.

        AST CallRef.lineno points to the *call expression* start, while SCIP
        occurrences mark the *symbol token* position which can be one line
        above/below (e.g. multi-line calls, decorator-wrapped calls). The ±1
        tolerance bridges this gap without introducing false positives because
        we still require the callee_name_hint to match.
        """
        exact = self.references_by_pos.get((file, line), [])
        if exact:
            return exact
        # Try adjacent lines.
        for delta in (-1, 1):
            adj = self.references_by_pos.get((file, line + delta), [])
            if adj:
                return adj
        return []


# ---------------------------------------------------------------------------
# Symbol normalisation
# ---------------------------------------------------------------------------

_SCIP_PREFIX_RE = re.compile(r"^scip-python\s+python\s+\S+\s+")
# Version prefix like "0.1.0 " or "3.11 " before the descriptor.
_VERSION_PREFIX_RE = re.compile(r"^\S+\s+")


def _symbol_to_qname(symbol: str) -> str:
    """Convert a raw SCIP symbol string to a dotted qname.

    scip-python symbols look like:
        ``scip-python python kg_builder 0.1.0 `examples.sample_repo.services.order_service`.OrderService#create_order.(self``
    We strip the protocol+language+package prefix, version prefix, backticks,
    and convert ``#`` (method separator) and ``/`` to dots.
    """
    m = _SCIP_PREFIX_RE.match(symbol)
    if not m:
        return symbol.strip()
    rest = symbol[m.end():].strip()
    # Strip version prefix (e.g. "0.1.0 " or "3.11 ").
    vm = _VERSION_PREFIX_RE.match(rest)
    if vm:
        rest = rest[vm.end():]
    # Remove backticks.
    rest = rest.replace("`", "")
    # Convert # (method separator) and / to dots.
    rest = rest.replace("#", ".").replace("/", ".")
    # Split and clean segments.
    cleaned: list[str] = []
    for seg in rest.split("."):
        # Strip parameter lists like "(self", "(payload", "(order_id" and trailing colons.
        seg = re.sub(r"\(.*$", "", seg).rstrip(":").strip()
        if seg:
            cleaned.append(seg)
    return ".".join(cleaned) if cleaned else symbol.strip()


# ---------------------------------------------------------------------------
# Protobuf wire-format decoding
# ---------------------------------------------------------------------------

def _read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    """Read one base-128 varint; returns ``(value, next_pos)``."""
    value = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint in SCIP index")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long in SCIP index")


def _iter_fields(buf: bytes) -> Iterator[Tuple[int, int, Any]]:
    """Yield ``(field_number, wire_type, value)`` for a protobuf message."""
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        field_no, wire = key >> 3, key & 7
        if wire == 0:            # varint
            value, pos = _read_varint(buf, pos)
        elif wire == 1:          # fixed64
            value = buf[pos:pos + 8]
            pos += 8
        elif wire == 2:          # length-delimited
            length, pos = _read_varint(buf, pos)
            value = buf[pos:pos + length]
            pos += length
        elif wire == 5:          # fixed32
            value = buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire} in SCIP index")
        if pos > len(buf):
            raise ValueError("truncated field in SCIP index")
        yield field_no, wire, value


def _decode_packed_varints(raw: bytes) -> List[int]:
    values: List[int] = []
    pos = 0
    while pos < len(raw):
        value, pos = _read_varint(raw, pos)
        values.append(value)
    return values


def _parse_occurrence(blob: bytes, rel_path: str, result: ScipIndex) -> None:
    """Decode one ``Occurrence`` message into a definition or reference record."""
    range_vals: List[int] = []
    symbol = ""
    role = 0
    for field_no, wire, value in _iter_fields(blob):
        if field_no == 1:                       # range (packed or repeated int32)
            if wire == 2:
                range_vals.extend(_decode_packed_varints(value))
            elif wire == 0:
                range_vals.append(value)
        elif field_no == 2 and wire == 2:       # symbol
            symbol = value.decode("utf-8", "replace")
        elif field_no == 3 and wire == 0:       # symbol_roles (bitflags)
            role |= value
    if not symbol or not range_vals:
        return
    # Local variables are defined at their assignment line and would shadow the
    # real enclosing function/class when looking up "which def contains line X".
    if symbol.startswith("local "):
        return

    qname = _symbol_to_qname(symbol)
    result.symbols.add(symbol)
    start_line = range_vals[0] + 1              # SCIP lines are 0-based
    end_line = (range_vals[2] if len(range_vals) >= 4 else range_vals[0]) + 1
    if role & _ROLE_DEFINITION:
        result.definitions_by_file.setdefault(rel_path, []).append(DefinitionRecord(
            symbol=symbol, qname=qname, file=rel_path,
            start_line=start_line, end_line=end_line,
        ))
    else:
        result.references_by_pos.setdefault((rel_path, start_line), []).append(
            ReferenceRecord(symbol=symbol, qname=qname, file=rel_path, line=start_line)
        )


def _parse_document(blob: bytes, result: ScipIndex) -> None:
    rel_path = ""
    occurrences: List[bytes] = []
    for field_no, wire, value in _iter_fields(blob):
        if field_no == 1 and wire == 2:         # relative_path
            rel_path = value.decode("utf-8", "replace")
        elif field_no == 2 and wire == 2:       # occurrences
            occurrences.append(value)
    if not rel_path:
        return
    for occ in occurrences:
        _parse_occurrence(occ, rel_path, result)


def _parse_index_bytes(data: bytes) -> ScipIndex:
    """Decode a serialized ``scip.Index`` message."""
    result = ScipIndex()
    for field_no, wire, value in _iter_fields(data):
        if field_no == 2 and wire == 2:         # documents
            _parse_document(value, result)
    for defs in result.definitions_by_file.values():
        defs.sort(key=lambda d: d.start_line)
    return result


# ---------------------------------------------------------------------------
# Index loading
# ---------------------------------------------------------------------------

def load_index(scip_path: Path, repo_root: Path) -> ScipIndex:
    """Load and decode an ``index.scip`` file.

    ``repo_root`` is accepted for signature stability (paths in the index are
    already repo-relative); it is not needed for decoding.
    """
    scip_path = Path(scip_path)
    if not scip_path.exists():
        raise FileNotFoundError(f"SCIP index not found: {scip_path}")
    data = scip_path.read_bytes()
    if not data:
        raise ValueError(f"SCIP index is empty: {scip_path}")
    return _parse_index_bytes(data)


# ---------------------------------------------------------------------------
# Call resolution
# ---------------------------------------------------------------------------

@dataclass
class ResolvedCall:
    callee_qname: str
    caller_qname: Optional[str]
    resolved_by: str = "scip_symbol"
    confidence: float = 0.95


def resolve_call(
    index: ScipIndex, file: str, line: int, callee_name_hint: str = "",
) -> Optional[ResolvedCall]:
    refs = index.get_references_at(file, line)
    if not refs:
        return None

    # Filter out noise: skip local variables, builtins, and stdlib.
    meaningful = [
        r for r in refs
        if not r.qname.startswith("local ")
        and "builtins." not in r.qname
        and "python-stdlib" not in r.symbol
    ]
    if not meaningful:
        return None

    # When multiple candidates exist, prefer one whose qname tail matches
    # the AST callee text. This prevents ±1 line tolerance from grabbing
    # an adjacent but unrelated symbol.
    if callee_name_hint and len(meaningful) > 1:
        hint_parts = [p for p in callee_name_hint.split(".") if p]
        best: Optional[ReferenceRecord] = None
        best_score = -1
        for r in meaningful:
            score = 0
            rq_parts = r.qname.split(".")
            for hp in hint_parts:
                if any(hp == rp or hp == rp.rstrip("()") for rp in rq_parts):
                    score += 1
            if score > best_score:
                best_score = score
                best = r
        chosen = best if best and best_score > 0 else meaningful[0]
    else:
        chosen = meaningful[0]

    enclosing = index.find_enclosing_definition(file, line)
    caller_qname = enclosing.qname if enclosing else None

    return ResolvedCall(
        callee_qname=chosen.qname,
        caller_qname=caller_qname,
    )
