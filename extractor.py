#!/usr/bin/env python3
"""
Walk the repo with Tree‑sitter (using the pre‑built grammars from
``tree‑sitter‑languages``) and emit JSON facts about every symbol it can
find.

 • ``--out-full``   – all symbols in ``HEAD``
 • ``--out-delta``  – only symbols whose *files* changed vs ``--base-sha``

Supported languages (dedicated collectors):
 • Python     ``.py``
 • Dart       ``.dart``

For JavaScript/TypeScript/Go – and for any other language that has a
Tree‑sitter grammar available but no bespoke collector – a single generic
fact is emitted per file.

If a file’s language *isn’t* recognised *or* a grammar is missing, the
script **falls back gracefully** to a one‑fact‑per‑file strategy instead
of aborting. This means it now *never* crashes just because it encounters
an unfamiliar source file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys
from contextlib import suppress
from typing import List

try:
    from tree_sitter_languages import get_parser  # ready‑to‑use parsers
except ImportError as exc:  # pragma: no‑cover – clearly actionable error
    sys.stderr.write(
        "tree_sitter_languages not importable - install it with\n"
        "    pip install tree-sitter-languages\n"
    )
    raise

# ── Language ↔︎ file‑extension map ────────────────────────────────────
EXT_TO_LANG = {
    ".py":   "python",
    ".js":   "javascript",
    ".ts":   "typescript",
    ".tsx":  "tsx",
    ".go":   "go",
    ".dart": "dart",
}

# ── Helpers ───────────────────────────────────────────────────────────

def sha10(text: str) -> str:
    """Return the first 10 hex chars of an SHA‑1 hash (deterministic id)."""
    return hashlib.sha1(text.encode()).hexdigest()[:10]

# ── Python collector ─────────────────────────────────────────────────

def _py_fq_name(node, src: bytes, path: pathlib.Path) -> str:
    parts: List[str] = []
    while node:
        if node.type in ("function_definition", "class_definition"):
            ident_node = node.child_by_field_name("name")
            ident = src[ident_node.start_byte : ident_node.end_byte].decode()
            parts.insert(0, ident)
        node = node.parent
    module = path.with_suffix("").as_posix().replace("/", ".")
    return f"{module}." + ".".join(parts) if parts else module


def collect_python(path: pathlib.Path, parser):
    src = path.read_bytes()
    tree = parser.parse(src)
    out: list[dict] = []

    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        if n.type == "function_definition":
            name = _py_fq_name(n, src, path)
            sig = re.search(
                rb"def\s+" + name.split(".")[-1].encode() + rb"\s*(\(.*?\))",
                src[n.start_byte : n.end_byte],
                re.S,
            )
            sig_text = sig.group(1).decode(errors="ignore") if sig else "()"
            raw = f"python|{name}|{sig_text}|{path}"
            out.append(
                {
                    "id": "CU-" + sha10(raw),
                    "symbol": name,
                    "signature": sig_text,
                    "lang": "python",
                    "file": str(path),
                    "line_start": n.start_point[0] + 1,
                    "line_end": n.end_point[0] + 1,
                }
            )
        stack.extend(n.children)
    return out

# ── Dart collector ───────────────────────────────────────────────────

_DART_DECL_NODES = {
    "function_declaration",
    "method_declaration",
    "constructor_declaration",
}


def _dart_fq_name(node, src: bytes, path: pathlib.Path) -> str:
    """Best‑effort dotted name like ``lib.src.foo.Bar.baz``."""
    parts: List[str] = []
    while node:
        if node.type in _DART_DECL_NODES or node.type == "class_declaration":
            ident_node = node.child_by_field_name("name")
            if ident_node:
                ident = src[ident_node.start_byte : ident_node.end_byte].decode()
                parts.insert(0, ident)
        node = node.parent
    module = path.with_suffix("").as_posix().replace("/", ".")
    return f"{module}." + ".".join(parts) if parts else module


def collect_dart(path: pathlib.Path, parser):
    src = path.read_bytes()
    tree = parser.parse(src)
    out: list[dict] = []

    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        if n.type in _DART_DECL_NODES:
            name = _dart_fq_name(n, src, path)
            local_src = src[n.start_byte : n.end_byte]
            pattern = rb"\b" + name.split(".")[-1].encode() + rb"\s*(\(.*?\))"
            sig = re.search(pattern, local_src, re.S)
            sig_text = sig.group(1).decode(errors="ignore") if sig else "()"
            raw = f"dart|{name}|{sig_text}|{path}"
            out.append(
                {
                    "id": "CU-" + sha10(raw),
                    "symbol": name,
                    "signature": sig_text,
                    "lang": "dart",
                    "file": str(path),
                    "line_start": n.start_point[0] + 1,
                    "line_end": n.end_point[0] + 1,
                }
            )
        stack.extend(n.children)
    return out

# ── Fallback: one fact per file ───────────────────────────────────────

def file_fact(path: pathlib.Path, lang_id: str):
    raw = f"{lang_id}|{path}"
    return [
        {
            "id": "CU-" + sha10(raw),
            "symbol": path.stem,
            "lang": lang_id,
            "file": str(path),
            "line_start": 1,
            "line_end": sum(1 for _ in open(path, "rb")),
        }
    ]

# ── CLI ───────────────────────────────────────────────────────────────

ap = argparse.ArgumentParser()
ap.add_argument("--out-full", required=True)
ap.add_argument("--out-delta", required=True)
ap.add_argument("--base-sha", required=True)
args = ap.parse_args()

ROOT = pathlib.Path(".").resolve()

with suppress(subprocess.CalledProcessError):
    # If the git command fails (e.g. not a repo), we treat as no‑changes.
    changed_files_raw = subprocess.check_output(
        ["git", "diff", "--name-only", args.base_sha, "HEAD"],
        text=True,
    ).splitlines()
    changed_files: set[pathlib.Path] = {ROOT / f for f in changed_files_raw}
else:
    changed_files = set()

full: list[dict] = []
delta: list[dict] = []

for path in ROOT.rglob("*"):
    if not path.is_file():
        continue

    lang_id = EXT_TO_LANG.get(path.suffix)
    if not lang_id:
        # Unknown extension → silently skip.
        continue

    # Obtain parser if possible.
    parser = None
    with suppress(Exception):  # grammar may be unavailable
        parser = get_parser(lang_id)

    # Collect facts with best‑effort fallbacks.
    try:
        if parser and lang_id == "python":
            facts = collect_python(path, parser)
        elif parser and lang_id == "dart":
            facts = collect_dart(path, parser)
        elif parser:
            facts = file_fact(path, lang_id)
        else:  # no parser – still emit something
            facts = file_fact(path, lang_id)
    except Exception:
        # Any unexpected failure must *not* crash the whole run.
        facts = file_fact(path, lang_id)

    full.extend(facts)
    if path in changed_files:
        delta.extend(facts)

# ── Write outputs ─────────────────────────────────────────────────────

def dump(objs: list[dict], target: str):
    pathlib.Path(target).parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(sorted(objs, key=lambda x: x["id"]), fh, indent=2, sort_keys=True)


dump(full, args.out_full)
dump(delta, args.out_delta)
print(f"Wrote {len(full)} symbols → {args.out_full}")
print(f"Wrote {len(delta)} delta  → {args.out_delta}")
