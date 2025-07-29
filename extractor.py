#!/usr/bin/env python3
"""
Walk the repo with Tree-sitter, output two JSON files:
  1) all symbols in HEAD               -> --out-full
  2) only symbols whose *file* changed -> --out-delta
"""
import argparse, json, pathlib, subprocess, hashlib, re, os, sys
from collections import defaultdict

# ---------- CLI ----------
ap = argparse.ArgumentParser()
ap.add_argument("--out-full",  required=True)
ap.add_argument("--out-delta", required=True)
ap.add_argument("--base-sha",  required=True)
args = ap.parse_args()

ROOT = pathlib.Path(".").resolve()
VENDOR = ROOT / "vendor"
BUILD  = VENDOR / "build" / "lang.so"
if not BUILD.exists():
    raise SystemExit("lang.so not found; compile grammars step must run first")

# ---------- Tree-sitter setup ----------
from tree_sitter import Language, Parser
LANG = Language(str(BUILD), "python"), Language(str(BUILD), "javascript"), Language(str(BUILD), "typescript"), Language(str(BUILD), "go")
LANG_MAP = {
    ".py":  Language(str(BUILD), "python"),
    ".js":  Language(str(BUILD), "javascript"),
    ".ts":  Language(str(BUILD), "typescript"),
    ".tsx": Language(str(BUILD), "typescript"),
    ".go":  Language(str(BUILD), "go"),
}

# ---------- helpers ----------
def sha10(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:10]

def fq_python(node, src, path):
    """module.Class.func dotted path"""
    parts = []
    while node:
        if node.type in ("function_definition", "class_definition"):
            name_node = node.child_by_field_name("name")
            ident = src[name_node.start_byte:name_node.end_byte].decode()
            parts.insert(0, ident)
        node = node.parent
    module = path.with_suffix("").as_posix().replace("/", ".")
    return f"{module}." + ".".join(parts) if parts else module

def collect_python(parser, path):
    src   = path.read_bytes()
    tree  = parser.parse(src)
    root  = tree.root_node
    out   = []

    cursor = root.walk()
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "function_definition":
            name = fq_python(n, src, path)
            sig  = re.search(rb'def\s+' + name.split(".")[-1].encode() + rb'\s*(\(.*?\))',
                             src[n.start_byte:n.end_byte], re.S)
            sig  = sig.group(1).decode(errors="ignore") if sig else "()"
            raw  = f"python|{name}|{sig}|{path}"
            out.append({
                "id":       "CU-" + sha10(raw),
                "symbol":   name,
                "signature": sig,
                "lang":     "python",
                "file":     str(path),
                "line_start": n.start_point[0]+1,
                "line_end":   n.end_point[0]+1,
            })
        stack.extend(n.children)
    return out

# ---------- gather changed files ----------
changed_files = subprocess.check_output(
    ["git", "diff", "--name-only", args.base_sha, "HEAD"], text=True
).splitlines()
changed_files = {pathlib.Path(f).resolve() for f in changed_files}

# ---------- walk repo ----------
full, delta = [], []
for path in ROOT.rglob("*"):
    if path.suffix not in LANG_MAP:       # skip unsupported
        continue
    parser = Parser(); parser.set_language(LANG_MAP[path.suffix])
    if path.suffix == ".py":
        facts = collect_python(parser, path)
    else:
        # Fallback: one fact per file for other langs
        raw = f"{path.suffix}|{path}"
        facts = [{
            "id":     "CU-" + sha10(raw),
            "symbol": path.stem,
            "lang":   path.suffix.lstrip("."),
            "file":   str(path),
            "line_start": 1,
            "line_end":   sum(1 for _ in open(path, 'rb')),
        }]
    full.extend(facts)
    if path in changed_files:
        delta.extend(facts)

# ---------- write outputs ----------
def dump(obj, target):
    pathlib.Path(target).parent.mkdir(parents=True, exist_ok=True)
    json.dump(sorted(obj, key=lambda x: x["id"]),
              open(target, "w"), indent=2, sort_keys=True)

dump(full,  args.out_full)
dump(delta, args.out_delta)
print(f"Wrote {len(full)} symbols → {args.out_full}")
print(f"Wrote {len(delta)} delta  → {args.out_delta}")