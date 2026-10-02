#!/usr/bin/env python3
"""Validate YAML frontmatter in Markdown/MDX files.

Why this exists
---------------
An unquoted frontmatter value containing ": " is not a valid YAML plain
scalar -- YAML reads the second colon as the start of a nested mapping and
aborts with "mapping values are not allowed here".

When frontmatter fails to parse, Mintlify silently falls back to the
"Untitled" page title and renders the raw frontmatter block into the page
body. The damage is invisible in review and only shows up on the live site,
so it is checked here instead.

Usage
-----
    scripts/check_frontmatter.py --all           # every .md/.mdx in the repo
    scripts/check_frontmatter.py --staged        # staged files only (pre-commit)
    scripts/check_frontmatter.py FILE [FILE...]  # named files
    scripts/check_frontmatter.py --all --fix     # rewrite offenders in place

Exit status is 0 when every file is clean and 1 when any problem is found.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is optional
    yaml = None

EXTENSIONS = (".md", ".mdx")
SKIP_DIRS = {".git", "node_modules", ".next", "build", "dist", ".venv", "__pycache__"}

FM_OPEN_RE = re.compile(r"\A---\r?\n")
FM_BLOCK_RE = re.compile(r"\A---\r?\n(?P<body>.*?)(?:\r?\n)---[ \t]*(?:\r?\n|\Z)", re.S)
KV_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z0-9_.-]+):(?P<sep>[ \t]*)(?P<value>.*)$")
BLOCK_SCALAR_RE = re.compile(r"^[|>]")
QUOTED_RE = re.compile(r"^[\"']")
FLOW_RE = re.compile(r"^[\[{]")
# A plain (unquoted) scalar may not contain a colon followed by whitespace.
PLAIN_COLON_RE = re.compile(r":(?=\s)")


def relpath(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


def staged_files(root: str) -> list[str]:
    """Markdown/MDX files added or modified in the index."""
    proc = subprocess.run(
        ["git", "-C", root, "diff", "--cached", "--name-only",
         "--diff-filter=ACM", "-z"],
        capture_output=True, check=True,
    )
    names = [n for n in proc.stdout.decode("utf-8", "replace").split("\0") if n]
    return [os.path.join(root, n) for n in names if n.endswith(EXTENSIONS)]


def all_files(root: str) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        found.extend(
            os.path.join(dirpath, fn)
            for fn in filenames
            if fn.endswith(EXTENSIONS)
        )
    return sorted(found)


def needs_quoting(value: str) -> bool:
    """True when a frontmatter value is an invalid plain scalar."""
    v = value.strip()
    if not v or v.startswith("#"):
        return False
    if QUOTED_RE.match(v) or BLOCK_SCALAR_RE.match(v) or FLOW_RE.match(v):
        return False
    return bool(PLAIN_COLON_RE.search(v))


def quote_value(value: str) -> str:
    body = value.strip().replace("\\", "\\\\").replace('"', '\\"')
    return f'"{body}"'


def apply_fixes(body: str) -> tuple[str, list[str]]:
    """Quote offending values. Returns the new body and a list of notes."""
    notes: list[str] = []
    lines = body.split("\n")
    for index, line in enumerate(lines):
        match = KV_RE.match(line)
        if not match or not needs_quoting(match.group("value")):
            continue
        lines[index] = "{}{}:{}{}".format(
            match.group("indent"),
            match.group("key"),
            match.group("sep") or " ",
            quote_value(match.group("value")),
        )
        notes.append(f"line {index + 2}: quoted `{match.group('key')}`")
    return "\n".join(lines), notes


def check_file(path: str, root: str, do_fix: bool = False) -> tuple[list[str], list[str]]:
    """Return (problems, warnings) for one file."""
    problems: list[str] = []
    warnings: list[str] = []

    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        return [f"cannot read file: {exc}"], warnings

    if raw.startswith(b"\xef\xbb\xbf"):
        problems.append(
            "file starts with a UTF-8 BOM; remove it so frontmatter is recognised"
        )

    text = raw.decode("utf-8", "replace")

    if not FM_OPEN_RE.match(text):
        problems.append("frontmatter must start on line 1 with `---`")
        return problems, warnings

    block = FM_BLOCK_RE.match(text)
    if not block:
        problems.append("frontmatter block is never closed with `---`")
        return problems, warnings

    body = block.group("body")
    start, end = block.start("body"), block.end("body")

    if do_fix:
        fixed, notes = apply_fixes(body)
        if fixed != body:
            text = text[:start] + fixed + text[end:]
            with open(path, "wb") as handle:
                handle.write(text.encode("utf-8"))
            raw = text.encode("utf-8")
            body = fixed
            warnings.extend(notes)

    if yaml is not None:
        try:
            data = yaml.safe_load(body)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            problem = getattr(exc, "problem", None) or "invalid YAML"
            if mark is not None:
                problems.append(
                    f"frontmatter is not valid YAML at line {mark.line + 2}, "
                    f"column {mark.column + 1}: {problem}"
                )
            else:
                problems.append(f"frontmatter is not valid YAML: {problem}")
            problems.append(
                'hint: a plain value cannot contain ": " -- '
                'wrap it in quotes, e.g. description: "Topic: detail"'
            )
            return problems, warnings

        if not isinstance(data, dict):
            problems.append("frontmatter did not parse into a mapping of keys")
        else:
            title = data.get("title")
            if not isinstance(title, str) or not title.strip():
                problems.append("frontmatter is missing a non-empty `title`")
            description = data.get("description")
            if description is not None and not isinstance(description, str):
                problems.append("`description` must be a single string value")
    else:
        # PyYAML unavailable: fall back to targeted hazard detection.
        for index, line in enumerate(body.split("\n")):
            match = KV_RE.match(line)
            if match and needs_quoting(match.group("value")):
                problems.append(
                    f"line {index + 2}: `{match.group('key')}` contains an "
                    'unquoted ": ", which is invalid YAML -- wrap it in quotes'
                )
        if not re.search(r"^title:[ \t]*\S", body, re.M):
            problems.append("frontmatter is missing a non-empty `title`")

    if raw and not raw.endswith(b"\n"):
        warnings.append("file does not end with a newline")

    return problems, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate YAML frontmatter in Markdown/MDX files.",
    )
    parser.add_argument("files", nargs="*", help="explicit files to check")
    parser.add_argument("--all", action="store_true", help="check every .md/.mdx in the repo")
    parser.add_argument("--staged", action="store_true", help="check staged files only")
    parser.add_argument("--fix", action="store_true", help="quote offending values in place")
    args = parser.parse_args(argv)

    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, check=True, text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        root = os.getcwd()

    if args.staged:
        targets = staged_files(root)
    elif args.all:
        targets = all_files(root)
    elif args.files:
        targets = [os.path.abspath(f) for f in args.files]
    else:
        parser.error("choose --all, --staged, or pass explicit files")

    if not targets:
        print("frontmatter: nothing to check")
        return 0

    if args.fix and args.staged:
        print(
            "frontmatter: --fix cannot be combined with --staged (the fix would "
            "not be staged).\nRun `--fix` on the files, then `git add` them again.",
            file=sys.stderr,
        )
        return 1

    if yaml is None:
        print(
            "frontmatter: PyYAML not installed; falling back to targeted checks.\n"
            "             Install it with `pip install pyyaml` for full validation.\n",
            file=sys.stderr,
        )

    failed = 0
    for path in targets:
        problems, warnings = check_file(path, root, do_fix=args.fix)
        name = relpath(path, root)
        for warning in warnings:
            print(f"  fixed  {name}: {warning}")
        if problems:
            failed += 1
            print(f"\nFAIL  {name}")
            for problem in problems:
                print(f"        {problem}")
        elif not warnings:
            print(f"ok    {name}")

    print()
    if failed:
        print(f"{failed} of {len(targets)} file(s) failed frontmatter validation.")
        if not args.fix:
            print("Run with --fix to quote the offending values automatically.")
        return 1

    print(f"frontmatter: {len(targets)} file(s) valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())