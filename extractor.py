#!/usr/bin/env python3
"""
Full Multi-language Tree-sitter fact extractor
--------------------------------------------
Emits all eight fact-types needed for EU MDR documentation across Python, JavaScript, TypeScript/TSX, and Go:

- symbol: functions, classes, methods
- import: import statements
- decorator: Python @decorators and JS/TS decorators
- call: function and method calls
- annotation: type hints (Python) and TS/Go type declarations
- test_case: unit tests (e.g., test_*, Jest 'it', Go testing functions)
- complexity: cyclomatic complexity per function
- docstring: docstrings or top-of-function comments

Usage: python extract_symbols.py --out-full full.json --out-delta delta.json [--base-sha SHA]
"""

import argparse, hashlib, json, logging, pathlib, re, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Dict, Iterable, List
from tree_sitter import Node
from tree_sitter_languages import get_parser

# ────────────── Language Configurations ──────────────
EXT_TO_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
}

LANG_QUERIES: Dict[str, Dict[str, str]] = {
    "python": {
        "symbol": """
            (function_definition name: (identifier) @sym.name)
            (class_definition name: (identifier) @sym.name)
        """,
        "import": """
            (import_statement name: (dotted_name) @import.module)
            (from_import_statement module_name: (dotted_name) @import.module)
        """,
        "decorator": """
            (decorator name: (identifier) @decorator.name)
        """,
        "call": """
            (call function: (identifier) @call.name)
        """,
        "annotation": """
            (type_hint (identifier) @type.name)
        """,
        "docstring": """
            (expression_statement (string) @doc.string)
        """,
        "test_case": """
            (function_definition name: (identifier) @test.name (#match? @test.name "^test_"))
        """,
    },
    "javascript": {
        "symbol": """
            (function_declaration name: (identifier) @sym.name)
            (class_declaration name: (identifier) @sym.name)
        """,
        "import": """
            (import_statement source: (string) @import.module)
        """,
        "decorator": """
            (decorator name: (identifier) @decorator.name)
        """,
        "call": """
            (call_expression function: (identifier) @call.name)
        """,
        "docstring": """
            (comment) @doc.string
        """,
        "test_case": """
            (call_expression function: (identifier) @test.name (#match? @test.name "^(it|test)$"))
        """,
    },
    "typescript": {
        "symbol": """
            (function_declaration name: (identifier) @sym.name)
            (class_declaration name: (identifier) @sym.name)
        """,
        "import": """
            (import_statement source: (string) @import.module)
        """,
        "decorator": """
            (decorator name: (identifier) @decorator.name)
        """,
        "call": """
            (call_expression function: (identifier) @call.name)
        """,
        "annotation": """
            (type_annotation (predefined_type) @type.name)
        """,
        "docstring": """
            (comment) @doc.string
        """,
        "test_case": """
            (call_expression function: (identifier) @test.name (#match? @test.name "^(it|test)$"))
        """,
    },
    "tsx": {
        "symbol": """
            (function_declaration name: (identifier) @sym.name)
            (class_declaration name: (identifier) @sym.name)
        """,
        "import": """
            (import_statement source: (string) @import.module)
        """,
        "decorator": """
            (decorator name: (identifier) @decorator.name)
        """,
        "call": """
            (call_expression function: (identifier) @call.name)
        """,
        "annotation": """
            (type_annotation (predefined_type) @type.name)
        """,
        "docstring": """
            (comment) @doc.string
        """,
        "test_case": """
            (call_expression function: (identifier) @test.name (#match? @test.name "^(it|test)$"))
        """,
    },
    "go": {
        "symbol": """
            (function_declaration name: (identifier) @sym.name)
            (method_declaration name: (field_identifier) @sym.name)
            (type_spec name: (type_identifier) @sym.name)
        """,
        "import": """
            (import_spec path: (interpreted_string_literal) @import.module)
        """,
        "call": """
            (call_expression function: (identifier) @call.name)
        """,
        "annotation": """
            (type_spec name: (type_identifier) @type.name)
        """,
        "docstring": """
            (comment) @doc.string
        """,
        "test_case": """
            (function_declaration name: (identifier) @test.name (#match? @test.name "^Test"))
        """,
    },
}

BRANCHING_TYPES = {
    "python": ["if_statement", "for_statement", "while_statement", "try_statement", "with_statement", "match_statement"],
    "javascript": ["if_statement", "for_statement", "while_statement", "do_statement", "switch_statement"],
    "typescript": ["if_statement", "for_statement", "while_statement", "do_statement", "switch_statement"],
    "tsx": ["if_statement", "for_statement", "while_statement", "do_statement", "switch_statement"],
    "go": ["if_statement", "for_statement", "switch_statement", "select_statement"],
}

# ────────────── Utility helpers ──────────────

def sha10(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:10]

@lru_cache(maxsize=None)
def parser_for(lang_id: str):
    return get_parser(lang_id)

def to_module(path: pathlib.Path, lang_id: str) -> str:
    if lang_id == "python" and path.name == "__init__.py":
        path = path.parent
    return path.with_suffix("").as_posix().replace("/", ".")

def cyclomatic(node: Node, lang_id: str) -> int:
    branching = set(BRANCHING_TYPES.get(lang_id, ()))
    score = 1
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in branching:
            score += 1
        stack.extend(n.children)
    return score

# ────────────── Extraction ──────────────

def collect_facts(path: pathlib.Path, lang_id: str) -> List[dict]:
    parser = parser_for(lang_id)
    src = path.read_bytes()
    tree = parser.parse(src)
    language = parser.language

    facts: List[dict] = []

    for fact_type, query_src in LANG_QUERIES[lang_id].items():
        query = language.query(query_src)
        for node, cap in query.captures(tree.root_node):
            text_val = src[node.start_byte:node.end_byte].decode().strip("\"'")
            module_name = to_module(path, lang_id)

            if fact_type == "symbol":
                fq = f"{module_name}.{text_val}" if text_val else module_name
                facts.append({
                    "id": "CU-" + sha10(f"symbol|{fq}|{path}"),
                    "kind": "symbol",
                    "symbol": fq,
                    "signature": "()",  # placeholder for non-Python; could expand for others
                    "lang": lang_id,
                    "complexity": cyclomatic(node.parent if node.parent else node, lang_id),
                    "file": str(path),
                    "line_start": node.start_point[0] + 1,
                    "line_end": node.end_point[0] + 1,
                })
            elif fact_type == "import":
                facts.append({
                    "id": "CU-" + sha10(f"import|{module_name}|{text_val}"),
                    "kind": "import",
                    "module": module_name,
                    "imports": text_val,
                    "lang": lang_id,
                    "file": str(path),
                    "line_start": node.start_point[0] + 1,
                    "line_end": node.end_point[0] + 1,
                })
            elif fact_type == "decorator":
                fq = module_name
                facts.append({
                    "id": "CU-" + sha10(f"decorator|{fq}|{text_val}"),
                    "kind": "decorator",
                    "symbol": fq,
                    "decorator": text_val,
                    "lang": lang_id,
                    "file": str(path),
                    "line_start": node.start_point[0] + 1,
                    "line_end": node.end_point[0] + 1,
                })
            elif fact_type == "call":
                facts.append({
                    "id": "CU-" + sha10(f"call|{module_name}|{text_val}"),
                    "kind": "call",
                    "caller_module": module_name,
                    "callee": text_val,
                    "lang": lang_id,
                    "file": str(path),
                    "line_start": node.start_point[0] + 1,
                    "line_end": node.end_point[0] + 1,
                })
            elif fact_type == "annotation":
                facts.append({
                    "id": "CU-" + sha10(f"annotation|{module_name}|{text_val}"),
                    "kind": "annotation",
                    "symbol": module_name,
                    "annotation": text_val,
                    "lang": lang_id,
                    "file": str(path),
                    "line_start": node.start_point[0] + 1,
                    "line_end": node.end_point[0] + 1,
                })
            elif fact_type == "docstring":
                facts.append({
                    "id": "CU-" + sha10(f"docstring|{module_name}|{text_val}"),
                    "kind": "docstring",
                    "symbol": module_name,
                    "doc": text_val,
                    "lang": lang_id,
                    "file": str(path),
                    "line_start": node.start_point[0] + 1,
                    "line_end": node.end_point[0] + 1,
                })
            elif fact_type == "test_case":
                fq = f"{module_name}.{text_val}" if text_val else module_name
                facts.append({
                    "id": "CU-" + sha10(f"test_case|{fq}|{path}"),
                    "kind": "test_case",
                    "symbol": fq,
                    "lang": lang_id,
                    "file": str(path),
                    "line_start": node.start_point[0] + 1,
                    "line_end": node.end_point[0] + 1,
                })

    return facts

# ────────────── File iteration ──────────────
def iter_source_files(root: pathlib.Path) -> Iterable[pathlib.Path]:
    for path in root.rglob("*"):
        if path.is_dir() or path.is_symlink():
            continue
        if any(part.startswith(".") for part in path.parts):
            continue
        if path.suffix in EXT_TO_LANG:
            yield path

# ────────────── IO helpers ──────────────
def write_json(objs: List[dict], target: pathlib.Path, jsonl: bool = False) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if jsonl:
        with target.open("w", encoding="utf-8") as fh:
            for obj in sorted(objs, key=lambda o: o["id"]):
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    else:
        json.dump(sorted(objs, key=lambda o: o["id"]), target.open("w"), indent=2, sort_keys=True)

# ────────────── CLI parsing ──────────────
def parse_args():
    p = argparse.ArgumentParser(description="Full Tree-sitter fact extractor")
    p.add_argument("--out-full", required=True)
    p.add_argument("--out-delta", required=True)
    p.add_argument("--base-sha", default="HEAD~1")
    p.add_argument("--jsonl", action="store_true")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--verbose", action="count", default=0)
    return p.parse_args()

# ────────────── main routine ──────────────
def main():
    args = parse_args()
    logging.basicConfig(level=max(logging.WARNING - 10 * args.verbose, logging.DEBUG))

    root = pathlib.Path(".").resolve()

    diff_cmd = ["git", "diff", "--name-only", args.base_sha, "HEAD"]
    changed_files = {root / f for f in subprocess.check_output(diff_cmd, text=True).splitlines()}

    full, delta = [], []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for facts in pool.map(lambda p: collect_facts(p, EXT_TO_LANG[p.suffix]), iter_source_files(root)):
            if not facts:
                continue
            full.extend(facts)
            if pathlib.Path(facts[0]["file"]) in changed_files:
                delta.extend(facts)

    write_json(full, pathlib.Path(args.out_full), args.jsonl)
    write_json(delta, pathlib.Path(args.out_delta), args.jsonl)

    logging.info(f"Wrote {len(full)} facts → {args.out_full}")
    logging.info(f"Wrote {len(delta)} delta facts → {args.out_delta}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
