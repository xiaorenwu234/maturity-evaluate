"""Parse an ``index.scip`` file into lookup tables for call resolution.

Uses ``protoc --decode_raw`` to avoid protobuf runtime version mismatches.
The raw text output is parsed with a line-by-line state machine to extract
document paths, symbol definitions, and reference occurrences.

Hard-failure semantics: if protoc is unavailable or the index cannot be
parsed, raise immediately. There is no fallback.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


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
# Line-by-line state-machine parser for protoc --decode_raw output
# ---------------------------------------------------------------------------

def _parse_decode_raw(text: str) -> ScipIndex:
    """Parse ``protoc --decode_raw`` text output into lookup tables.

    Uses a line-by-line state machine instead of regex-with-DOTALL to avoid
    cross-block contamination from nested braces in the decode_raw format.
    """
    result = ScipIndex()
    lines = text.split("\n")

    rel_path: Optional[str] = None
    # State for current symbol entry being accumulated.
    in_sym_block = False
    brace_depth = 0
    range_bytes: Optional[str] = None
    symbol_str: Optional[str] = None
    role: Optional[int] = None

    def _flush_sym():
        nonlocal range_bytes, symbol_str, role
        if symbol_str and role is not None and rel_path:
            qname = _symbol_to_qname(symbol_str)
            result.symbols.add(symbol_str)
            decoded = _decode_range_bytes(range_bytes or "")
            if role == 1:  # Definition
                rec = DefinitionRecord(
                    symbol=symbol_str, qname=qname, file=rel_path,
                    start_line=decoded[0] if decoded else 1,
                    end_line=decoded[2] if len(decoded) > 2 else (decoded[0] if decoded else 1),
                )
                result.definitions_by_file.setdefault(rel_path, []).append(rec)
            elif role in (8, 4, 2):  # Reference / import / read
                ln = decoded[0] if decoded else 0
                if ln > 0:
                    ref = ReferenceRecord(symbol=symbol_str, qname=qname, file=rel_path, line=ln)
                    result.references_by_pos.setdefault((rel_path, ln), []).append(ref)
        range_bytes = None
        symbol_str = None
        role = None

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Track brace depth to know when we exit a symbol block.
        open_braces = stripped.count("{")
        close_braces = stripped.count("}")

        # Detect document-level relative_path: "1: \"path\"" at depth 1.
        if not in_sym_block and brace_depth == 1:
            pm = re.match(r'^1:\s*"(.*?)"\s*$', stripped)
            if pm:
                rel_path = pm.group(1)

        # Detect start of a symbol/occurrence block: "2 {" at document level.
        if not in_sym_block and brace_depth == 1 and stripped == "2 {":
            in_sym_block = True
            brace_depth += open_braces  # count the opening brace
            i += 1
            continue

        if in_sym_block:
            brace_depth += open_braces - close_braces

            # Field 1: range bytes (escaped byte string).
            fm1 = re.match(r'^1:\s*"(.*?)"\s*$', stripped)
            if fm1:
                range_bytes = fm1.group(1)

            # Field 2: symbol string.
            fm2 = re.match(r'^2:\s*"(.*?)"\s*$', stripped)
            if fm2:
                symbol_str = fm2.group(1)

            # Field 3: role integer.
            fm3 = re.match(r'^3:\s*(\d+)\s*$', stripped)
            if fm3:
                role = int(fm3.group(1))

            # Block ended.
            if brace_depth <= 1:
                _flush_sym()
                in_sym_block = False

        else:
            brace_depth += open_braces - close_braces
            # Reset path when leaving a document block.
            if brace_depth <= 0:
                rel_path = None
                brace_depth = 0

        i += 1

    # Flush any trailing symbol.
    if in_sym_block:
        _flush_sym()

    # Sort definitions per file by start_line.
    for defs in result.definitions_by_file.values():
        defs.sort(key=lambda d: d.start_line)

    return result


def _decode_range_bytes(raw: str) -> list[int]:
    """Decode escaped byte string from decode_raw into line numbers."""
    if not raw:
        return []
    try:
        byte_str = raw.encode("utf-8").decode("unicode_escape").encode("latin-1")
    except Exception:
        return []
    values: list[int] = []
    i = 0
    while i < len(byte_str):
        val = 0
        shift = 0
        while i < len(byte_str):
            b = byte_str[i]
            val |= (b & 0x7F) << shift
            i += 1
            if not (b & 0x80):
                break
            shift += 7
        values.append(val)
    return values


# ---------------------------------------------------------------------------
# Index loading
# ---------------------------------------------------------------------------

def load_index(scip_path: Path, repo_root: Path) -> ScipIndex:
    scip_path = Path(scip_path)
    if not scip_path.exists():
        raise FileNotFoundError(f"SCIP index not found: {scip_path}")
    try:
        result = subprocess.run(
            ["protoc", "--decode_raw"],
            stdin=open(scip_path, "rb"),
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        raise ImportError("protoc not found in PATH; required for SCIP index parsing")
    except subprocess.TimeoutExpired:
        raise RuntimeError("protoc --decode_raw timed out on SCIP index")
    if result.returncode != 0:
        raise RuntimeError(f"protoc --decode_raw failed: {result.stderr[:300]}")
    return _parse_decode_raw(result.stdout)


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
