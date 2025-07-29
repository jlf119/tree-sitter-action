#!/usr/bin/env python3
"""
Walk the repo with Tree-sitter (using the pre-built grammars from
tree-sitter-languages) and emit:

  • --out-full   - every symbol in HEAD
  • --out-delta  - only symbols whose *files* changed vs --base-sha
"""
import argparse, json, pathlib, subprocess, hashlib, re, sys
from tree_sitter_languages import get_parser   # <-- ready-to-use parsers

EXT_TO_LANG = {
    ".py":  "python",
    ".js":  "javascript",
    ".ts":  "typescript",
    ".tsx": "tsx",
    ".go":  "go",
}

def sha10(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:10]

def python_fq_name(node, src, path):
    parts = []
    while node:
        if node.type in ("function_definition", "class_definition"):
            ident = src[node.child_by_field_name("name")
                        .start_byte: node.child_by_field_name("name").end_byte].decode()
            parts.insert(0, ident)
        node = node.parent
    module = path.with_suffix('').as_posix().replace('/', '.')
    return f"{module}." + '.'.join(parts) if parts else module

def collect_python(path, parser):
    src  = path.read_bytes()
    tree = parser.parse(src)
    root = tree.root_node
    out  = []

    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "function_definition":
            name = python_fq_name(n, src, path)
            sig  = re.search(rb'def\s+' + name.split('.')[-1].encode() +
                             rb'\s*(\(.*?\))', src[n.start_byte:n.end_byte], re.S)
            sig  = sig.group(1).decode(errors="ignore") if sig else "()"
            raw  = f"python|{name}|{sig}|{path}"
            out.append({
                "id":        "CU-" + sha10(raw),
                "symbol":    name,
                "signature": sig,
                "lang":      "python",
                "file":      str(path),
                "line_start": n.start_point[0] + 1,
                "line_end":   n.end_point[0]   + 1,
            })
        stack.extend(n.children)
    return out

# ── CLI ───────────────────────────────────────────────────────────────
ap = argparse.ArgumentParser()
ap.add_argument("--out-full",  required=True)
ap.add_argument("--out-delta", required=True)
ap.add_argument("--base-sha",  required=True)
args = ap.parse_args()

ROOT = pathlib.Path(".").resolve()

changed_files = subprocess.check_output(
    ["git", "diff", "--name-only", args.base_sha, "HEAD"], text=True
).splitlines()
changed_files = {ROOT / f for f in changed_files}

full, delta = [], []
for path in ROOT.rglob("*"):
    lang_id = EXT_TO_LANG.get(path.suffix)
    if not lang_id:
        continue

    parser = get_parser(lang_id)

    if lang_id == "python":
        facts = collect_python(path, parser)
    else:
        # one generic fact per file for other languages
        raw = f"{lang_id}|{path}"
        facts = [{
            "id":     "CU-" + sha10(raw),
            "symbol": path.stem,
            "lang":   lang_id,
            "file":   str(path),
            "line_start": 1,
            "line_end":   sum(1 for _ in open(path, 'rb')),
        }]

    full.extend(facts)
    if path in changed_files:
        delta.extend(facts)

# ── Write outputs ─────────────────────────────────────────────────────
def dump(obj, target):
    pathlib.Path(target).parent.mkdir(parents=True, exist_ok=True)
    json.dump(sorted(obj, key=lambda x: x["id"]),
              open(target, "w"), indent=2, sort_keys=True)

dump(full,  args.out_full)
dump(delta, args.out_delta)
print(f"Wrote {len(full)} symbols → {args.out_full}")
print(f"Wrote {len(delta)} delta  → {args.out_delta}")
