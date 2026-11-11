"""Tests for the binary SCIP index decoder.

The decoder replaced ``protoc --decode_raw`` because the textual dump mangled
paths and silently produced near-empty indexes on real repositories. These
tests build protobuf bytes by hand so they run without scip-python installed.
"""
from __future__ import annotations

from kg_builder.collectors.code.python.scip_resolver import (
    _parse_index_bytes,
    load_index,
    resolve_call,
)


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _key(field_no: int, wire: int) -> bytes:
    return _varint((field_no << 3) | wire)


def _length_delimited(field_no: int, payload: bytes) -> bytes:
    return _key(field_no, 2) + _varint(len(payload)) + payload


def _int_field(field_no: int, value: int) -> bytes:
    return _key(field_no, 0) + _varint(value)


def _occurrence(line0: int, symbol: str, role: int, end_line0: int | None = None) -> bytes:
    rng = [line0, 4, 9] if end_line0 is None else [line0, 4, end_line0, 9]
    packed = b"".join(_varint(v) for v in rng)
    return (
        _length_delimited(1, packed)
        + _length_delimited(2, symbol.encode("utf-8"))
        + _int_field(3, role)
    )


def _document(path: str, *occurrences: bytes) -> bytes:
    return _length_delimited(1, path.encode("utf-8")) + b"".join(
        _length_delimited(2, occ) for occ in occurrences
    )


def _index(*documents: bytes) -> bytes:
    return b"".join(_length_delimited(2, doc) for doc in documents)


SYMBOL_MAIN = "scip-python python pkg 0.1.0 `pkg.main`."
SYMBOL_RUN = "scip-python python pkg 0.1.0 `pkg.main`.`Runner#run`()."


def test_decodes_definitions_and_references():
    data = _index(_document(
        "pkg/main.py",
        _occurrence(4, SYMBOL_RUN, role=1, end_line0=20),   # definition (0-based line 4)
        _occurrence(7, SYMBOL_RUN, role=8),                 # read reference
    ))

    index = _parse_index_bytes(data)

    defs = index.definitions_by_file["pkg/main.py"]
    assert len(defs) == 1
    assert defs[0].qname == "pkg.main.Runner.run"
    assert (defs[0].start_line, defs[0].end_line) == (5, 21)

    refs = index.references_by_pos[("pkg/main.py", 8)]
    assert len(refs) == 1
    assert refs[0].qname == "pkg.main.Runner.run"
    assert index.find_enclosing_definition("pkg/main.py", 10).qname == "pkg.main.Runner.run"


def test_decodes_multiple_documents_and_utf8_paths():
    data = _index(
        _document("pkg/io_读写.py", _occurrence(0, SYMBOL_MAIN, role=1)),
        _document("pkg/main.py", _occurrence(2, SYMBOL_RUN, role=8)),
    )

    index = _parse_index_bytes(data)

    assert set(index.definitions_by_file) == {"pkg/io_读写.py"}
    assert ("pkg/main.py", 3) in index.references_by_pos


def test_combined_definition_and_read_role_stays_a_definition():
    data = _index(_document("a.py", _occurrence(1, SYMBOL_RUN, role=1 | 8)))

    index = _parse_index_bytes(data)

    assert len(index.definitions_by_file["a.py"]) == 1
    assert index.references_by_pos == {}


def test_resolve_call_uses_reference_at_line():
    data = _index(_document(
        "pkg/main.py",
        _occurrence(4, SYMBOL_RUN, role=1, end_line0=20),
        _occurrence(7, SYMBOL_RUN, role=8),
    ))
    index = _parse_index_bytes(data)

    resolved = resolve_call(index, "pkg/main.py", 8, "run")

    assert resolved is not None
    assert resolved.callee_qname == "pkg.main.Runner.run"
    assert resolved.caller_qname == "pkg.main.Runner.run"


def test_local_definitions_are_not_treated_as_scopes():
    data = _index(_document(
        "a.py",
        _occurrence(4, "local 12", role=1),
        _occurrence(5, SYMBOL_RUN, role=8),
    ))

    index = _parse_index_bytes(data)

    assert index.definitions_by_file == {}
    assert ("a.py", 6) in index.references_by_pos


def test_load_index_reads_binary_file(tmp_path):
    path = tmp_path / "index.scip"
    path.write_bytes(_index(_document("pkg/main.py", _occurrence(0, SYMBOL_MAIN, role=1))))

    index = load_index(path, tmp_path)

    assert index.definitions_by_file["pkg/main.py"][0].qname == "pkg.main"
