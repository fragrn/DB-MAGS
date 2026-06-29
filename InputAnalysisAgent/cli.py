"""Command-line interface for InputAnalysisAgent."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from InputAnalysisAgent.analyzer import InputAnalysisError, analyze_post


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m InputAnalysisAgent.cli",
        description="Generate a database anomaly reproduction design from a DBA forum post.",
    )
    parser.add_argument("--input", required=True, help="Input txt or json file")
    parser.add_argument("--output", help="Output JSON path; defaults to stdout")
    parser.add_argument("--pretty", action="store_true", default=True, help="Pretty-print JSON output")
    parser.add_argument("--compact", action="store_true", help="Write compact JSON output")
    args = parser.parse_args(argv)

    try:
        description, metadata = _load_input(Path(args.input))
        design = analyze_post(description, metadata=metadata)
        indent = None if args.compact else (2 if args.pretty else None)
        output_text = json.dumps(design.to_dict(), ensure_ascii=False, indent=indent)
        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(output_text + "\n")
        else:
            print(output_text)
        return 0
    except Exception as exc:
        if isinstance(exc, (InputAnalysisError, ValueError, FileNotFoundError, json.JSONDecodeError)):
            print(f"InputAnalysisAgent failed: {exc}", file=sys.stderr)
            return 1
        raise


def _load_input(path: Path) -> tuple[str, dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"input file not found: {path}")

    text = path.read_text().strip()
    if path.suffix.lower() != ".json":
        if not text:
            raise ValueError("input post is empty")
        return text, {}

    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("json input must be an object")
    description = str(data.get("dba_description") or data.get("description") or "").strip()
    if not description:
        raise ValueError("json input must contain dba_description")
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    return description, metadata


if __name__ == "__main__":
    sys.exit(main())
