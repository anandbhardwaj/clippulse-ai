"""Insert or replace the marked QA block in a GitHub issue body.

Usage:
    python upsert_qa.py --body body.md --qa qa_block.md --out new_body.md

The QA block must start with `<!-- qa:start -->` and end with `<!-- qa:end -->`.
If the body already holds a block it is replaced in place, otherwise the block is
appended. Text outside the markers is never modified: the script re-checks this
before writing and exits non-zero if the check fails.
"""

import argparse
import sys
from pathlib import Path

START = "<!-- qa:start -->"
END = "<!-- qa:end -->"


class QABlockError(Exception):
    pass


def _span(text: str) -> tuple[int, int] | None:
    """Return (start, end) offsets of the existing block, or None if there is none."""
    starts, ends = text.count(START), text.count(END)
    if starts == 0 and ends == 0:
        return None
    if starts != 1 or ends != 1:
        raise QABlockError(
            f"expected exactly one {START} and one {END}, found {starts} and {ends}"
        )
    begin, finish = text.index(START), text.index(END)
    if finish < begin:
        raise QABlockError(f"{END} appears before {START}")
    return begin, finish + len(END)


def strip_block(text: str) -> str:
    span = _span(text)
    return text if span is None else text[: span[0]] + text[span[1] :]


def upsert(body: str, block: str) -> tuple[str, str]:
    """Return (new_body, action) where action is 'replaced' or 'appended'."""
    block = block.strip()
    if not (block.startswith(START) and block.endswith(END)) or _span(block) is None:
        raise QABlockError(f"QA block must start with {START} and end with {END}")
    newline = "\r\n" if "\r\n" in body else "\n"
    block = block.replace("\r\n", "\n").replace("\n", newline)

    span = _span(body)
    if span is not None:
        new_body = body[: span[0]] + block + body[span[1] :]
        action = "replaced"
    else:
        gap = "" if body.endswith(newline) else newline
        new_body = body + gap + newline + block + newline
        action = "appended"

    if strip_block(new_body).rstrip() != strip_block(body).rstrip():
        raise QABlockError("refusing to write: text outside the QA block would change")
    return new_body, action


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--body", required=True, type=Path, help="current issue body")
    parser.add_argument("--qa", required=True, type=Path, help="new QA block")
    parser.add_argument("--out", required=True, type=Path, help="file to write")
    args = parser.parse_args()

    body = args.body.read_text(encoding="utf-8", newline="")
    block = args.qa.read_text(encoding="utf-8", newline="")
    try:
        new_body, action = upsert(body, block)
    except QABlockError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    args.out.write_text(new_body, encoding="utf-8", newline="")
    print(f"{action}: wrote {args.out} ({len(new_body)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
