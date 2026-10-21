"""Semgrep subprocess wrapper.

Runs the ``semgrep`` CLI once over a whole repository and exposes findings as
flat, language-neutral :class:`Finding` records. The framework-enrichment layer
(:mod:`kg_builder.collectors.code.frameworks`) maps each finding into a
:class:`RouteDef` / :class:`ResourceRef` / :class:`ConfigRef` on the matching
:class:`FileArtifact`.

How values flow (rule -> payload -> IR):

Semgrep's ``--json`` output hides ``extra.metavars`` behind a "requires login"
gate (as of 1.60+), but the ``message`` field is still fully interpolated. Our
YAML rules therefore encode every value we need as a small JSON payload prefixed
with ``kgb-json `` -- this module parses that marker back into a plain dict.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

# Project-owned rules ship inside the package so a ``pip install -e .`` picks them up.
RULES_DIR = Path(__file__).resolve().parent / "semgrep_rules"

# Reasonable hard ceiling for a single scan on a typical service repo.
_DEFAULT_TIMEOUT = 180

# Prefix that our YAML rules use to encode structured findings into ``message``.
_JSON_MARKER = "kgb-json "

# Unbound metavariables come back as literal ``$NAME`` tokens -- nullify them so
# ``json.loads`` doesn't choke.
_NULLIFY_UNKNOWN_VARS = re.compile(r"\$[A-Z_][A-Z0-9_]*")
# Values interpolated from identifier metavariables are *bare* (unquoted), which
# is not valid JSON. Wrap them -- but only after a JSON *key* colon (lookbehind on
# `"`): payload strings may legitimately contain colons (Spring `${a:default}`),
# and wrapping those would corrupt otherwise-valid JSON into a dropped finding.
_BARE_VALUE = re.compile(r'(?<=")(:)([A-Za-z_][A-Za-z0-9_.\-/{}]*)([,}])')
# Message interpolation keeps the *source* quoting of string literals: JS/TS
# single-quoted paths ('/ping') land verbatim inside the payload and break
# ``json.loads``. Convert ``:'x'`` -> ``"x"``. Raw-string prefixes (re_path's
# ``r"..."``) likewise: strip a bare ``r`` right before an opening quote.
_SINGLE_QUOTED = re.compile(r"(\s*:\s*)'([^']*)'")
_RAW_PREFIX = re.compile(r'(:\s*)r(?=[\x01\'])')


def _sanitize_tail(tail: str) -> str:
    """Make the interpolated payload strict JSON without touching double-quoted values."""
    guarded = tail.replace('\\"', '\x00').replace('"', '\x01')
    guarded = _RAW_PREFIX.sub(r'\1', guarded)
    guarded = _SINGLE_QUOTED.sub(lambda m: m.group(1) + '"' + m.group(2) + '"', guarded)
    return guarded.replace('\x00', '\\"').replace('\x01', '"')


@dataclass
class Finding:
    """A single Semgrep match, normalised for downstream mapping."""

    rule_id: str
    path: str                                # relative to the scanned repo root
    line: int
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    message: str = ""

    @property
    def kind(self) -> str:
        """Shorthand for ``payload["t"]`` -- route / resource / config."""
        return str(self.payload.get("t", ""))


def semgrep_executable() -> Optional[str]:
    """Absolute path to the ``semgrep`` binary, or ``None`` if not installed."""
    return shutil.which("semgrep")


@lru_cache(maxsize=1)
def semgrep_status() -> tuple[bool, str]:
    """Return ``(available, message)`` for the Semgrep CLI.

    Cached because the answer only depends on the installed environment, not on
    any user input -- mirrors the ``treesitter_status`` pattern already used for
    optional parsers.
    """
    exe = semgrep_executable()
    if not exe:
        return False, (
            "semgrep executable not found on PATH "
            "(install with: pip install 'kg_builder[semgrep]' or pip install semgrep)"
        )
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"semgrep --version failed: {exc.__class__.__name__}: {exc}"
    if proc.returncode != 0:
        return False, f"semgrep --version exited {proc.returncode}: {proc.stderr.strip()[:200]}"
    version = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else "unknown"
    return True, version


class SemgrepError(RuntimeError):
    """Raised for unrecoverable Semgrep failures (bad JSON, timeout, ...)."""


def scan_repo(
    repo: Path,
    rules_dir: Path = RULES_DIR,
    *,
    timeout: int = _DEFAULT_TIMEOUT,
    extra_excludes: Optional[list[str]] = None,
) -> list[Finding]:
    """Run Semgrep once against ``repo`` and return flattened findings.

    ``repo`` is used as ``cwd`` so output paths come back relative to it, which
    is exactly what :class:`FileArtifact` stores.
    """
    exe = semgrep_executable()
    if not exe:
        raise SemgrepError("semgrep executable not found")
    if not rules_dir.exists():
        raise SemgrepError(f"rule directory missing: {rules_dir}")

    cmd: list[str] = [
        exe,
        "--config", str(rules_dir),
        "--json",
        "--quiet",
        "--metrics=off",
        "--no-git-ignore",
        "--max-target-bytes", "1000000",
    ]
    for excl in (extra_excludes or []):
        cmd += ["--exclude", excl]
    cmd.append(".")

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(repo),
        timeout=timeout,
        check=False,
    )
    stdout = (proc.stdout or "").strip()
    if not stdout:
        # Non-zero exit without JSON means a rule / parse error -- surface stderr.
        if proc.returncode != 0:
            raise SemgrepError(
                f"semgrep exited {proc.returncode}: {(proc.stderr or '').strip()[:400]}"
            )
        return []
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise SemgrepError(f"semgrep produced non-JSON output: {exc}") from exc

    findings: list[Finding] = []
    for raw in payload.get("results", []) or []:
        path = _normalize_path(raw.get("path", ""))
        if not path:
            continue
        extra = raw.get("extra") or {}
        message = extra.get("message") or ""
        findings.append(
            Finding(
                rule_id=_short_rule_id(raw.get("check_id", "")),
                path=path,
                line=int((raw.get("start") or {}).get("line", 0)),
                payload=_extract_payload(message),
                metadata=extra.get("metadata") or {},
                message=message,
            )
        )
    return findings


def _extract_payload(message: str) -> dict[str, Any]:
    """Parse the ``kgb-json {...}`` payload embedded in a rule message.

    Returns an empty dict when the marker is absent or the JSON is malformed
    (e.g. an interpolated metavariable produced an expression that isn't valid
    JSON -- we would rather drop one finding than crash the whole scan).
    """
    if not message or _JSON_MARKER not in message:
        return {}
    tail = message.split(_JSON_MARKER, 1)[1].strip()
    tail = _NULLIFY_UNKNOWN_VARS.sub("null", tail)
    tail = _BARE_VALUE.sub(r'\1"\2"\3', tail)
    tail = _sanitize_tail(tail)
    try:
        parsed = json.loads(tail)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _short_rule_id(check_id: str) -> str:
    """Semgrep prefixes rule ids with the config path (dots for slashes), e.g.
    ``home.x.user.pkg.semgrep_rules.kgb-db-repo-write`` -- keep only the rule id.
    """
    return check_id.rsplit(".", 1)[-1] if check_id else ""


def _normalize_path(p: str) -> str:
    """Semgrep emits paths like ``./services/order_service.py`` -- strip the dot."""
    if not p:
        return ""
    q = p.replace("\\", "/")
    while q.startswith("./"):
        q = q[2:]
    # Absolute paths sneak in when the target is given as absolute -- make relative.
    return q
