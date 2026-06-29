"""Command-line interface for InputAnalysisAgent."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from InputAnalysisAgent.analyzer import InputAnalysisError, analyze_post
from InputAnalysisAgent.hitl import HumanDecision, HumanGateRequired, WAITING_EXIT_CODE
from InputAnalysisAgent.runtime import ReproductionRuntime


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] in {"run", "resume"}:
        return _reproduction_main(argv)

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


def _reproduction_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m InputAnalysisAgent.cli",
        description="Analyze a DBA post and run a resumable anomaly reproduction.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="Plan and run a post-driven reproduction")
    run_parser.add_argument("--input", required=True, help="Input txt or JSON post")
    run_parser.add_argument("--output-root", default="InputAnalysisExperiment_runs")
    run_parser.add_argument("--interaction", choices=["interactive", "checkpoint"], default="checkpoint")

    resume_parser = subparsers.add_parser("resume", help="Respond to a human gate and resume")
    resume_parser.add_argument("--run-dir", required=True)
    resume_parser.add_argument("--decision", choices=["approve", "reject", "revise", "feedback", "retry"], required=True)
    resume_parser.add_argument("--patch", help="JSON Merge Patch file, required for revise")
    resume_parser.add_argument("--feedback", default="")
    args = parser.parse_args(argv)

    runtime = ReproductionRuntime()
    try:
        if args.command == "run":
            description, metadata = _load_input(Path(args.input))
            result = runtime.run(
                description,
                metadata=metadata,
                output_root=args.output_root,
                interaction=args.interaction,
            )
        else:
            patch = None
            if args.patch:
                patch_value = json.loads(Path(args.patch).read_text())
                if not isinstance(patch_value, dict):
                    raise ValueError("patch file must contain a JSON object")
                patch = patch_value
            result = runtime.resume(
                args.run_dir,
                HumanDecision(args.decision, patch=patch, feedback=args.feedback),
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "completed" else 1
    except HumanGateRequired as exc:
        print(
            f"Human approval required. Run directory: {exc.run_dir}\n"
            f"Review: {exc.run_dir / 'hitl_request.json'}",
            file=sys.stderr,
        )
        return WAITING_EXIT_CODE
    except Exception as exc:
        print(f"InputAnalysisAgent reproduction failed: {exc}", file=sys.stderr)
        return 1


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
