#!/usr/bin/env python3
"""Local, dependency-free guard for the hassfest rules this repo has tripped on.

The official Hassfest CI (home-assistant/actions/hassfest, Docker) is the source
of truth, but it only runs after a push. This script replicates the specific
checks that have broken `main` before so they're caught locally — ideally as a
pre-commit *pre-push* hook that scans the whole component, not just staged files
(a BOM/URL can hide in a file the current commit doesn't touch).

Checks:
  1. No UTF-8 BOM (U+FEFF) in any .py file  -> Python 3.14 (hassfest image)
     rejects it with "SyntaxError: invalid non-printable character".
  2. No http(s) URLs in translation/strings string values -> hassfest forbids
     URLs in translation strings (use description placeholders instead).
  3. manifest.json: valid JSON, required keys present, and keys in hassfest's
     order (domain, name, then the rest alphabetical).

Exit code 1 on any violation. Run: python3 scripts/check_integration.py
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "hvac_vent_optimizer"
URL_RE = re.compile(r"https?://")
REQUIRED_MANIFEST_KEYS = {"domain", "name", "version", "documentation", "codeowners"}

errors: list[str] = []


def check_bom() -> None:
    for path in PKG.rglob("*.py"):
        if path.read_bytes().startswith(b"\xef\xbb\xbf"):
            errors.append(f"BOM (U+FEFF) at start of {path.relative_to(ROOT)} — strip it (hassfest/py3.14 fails).")


def _walk_strings(obj, path, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk_strings(v, f"{path}.{k}", out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _walk_strings(v, f"{path}[{i}]", out)
    elif isinstance(obj, str):
        out.append((path, obj))


def check_translation_urls() -> None:
    files = list((PKG / "translations").glob("*.json"))
    if (PKG / "strings.json").exists():
        files.append(PKG / "strings.json")
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{f.relative_to(ROOT)}: invalid JSON ({exc}).")
            continue
        strings: list[tuple[str, str]] = []
        _walk_strings(data, f.name, strings)
        for where, value in strings:
            if URL_RE.search(value):
                errors.append(
                    f"{f.relative_to(ROOT)}: URL in translation string at {where!r} — "
                    f"hassfest forbids URLs in translations. Got: {value!r}"
                )


def check_manifest() -> None:
    mf = PKG / "manifest.json"
    try:
        manifest = json.loads(mf.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"manifest.json: invalid JSON ({exc}).")
        return
    missing = REQUIRED_MANIFEST_KEYS - manifest.keys()
    if missing:
        errors.append(f"manifest.json: missing required keys: {sorted(missing)}.")
    keys = list(manifest.keys())
    expected = sorted(keys, key=lambda k: (k != "domain", k != "name", k))
    if keys != expected:
        errors.append(f"manifest.json: keys not in hassfest order.\n   got:      {keys}\n   expected: {expected}")


def main() -> int:
    check_bom()
    check_translation_urls()
    check_manifest()
    if errors:
        print("Integration validation failed (mirrors hassfest):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("Integration checks passed (BOM / translation URLs / manifest order).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
