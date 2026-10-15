"""Run tutorial code cells in one shared namespace, like a notebook, printing a marker around each cell.

Usage: python3 run_cells.py cells.json [--report-from CELL_ID]
Cells before CELL_ID re-run silently (their state is needed); markers and output start at CELL_ID.
"""
import contextlib
import io
import json
import subprocess
import sys
import traceback


def mark(cell_id, status):
    print("@@FMT " + json.dumps({"cell": cell_id, "status": status}), flush=True)


def clean(code):
    out = []
    for line in code.splitlines():
        stripped = line.strip()
        indent = line[: len(line) - len(line.lstrip())]
        if stripped.startswith("%"):
            continue
        if stripped.startswith("!"):
            out.append(f"{indent}__import__('subprocess').run({stripped[1:]!r}, shell=True, check=True)")
            continue
        out.append(line)
    return "\n".join(out)


def main():
    cells = json.load(open(sys.argv[1]))
    report_from = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--report-from" else None
    reporting = report_from is None
    ns = {"__name__": "__main__", "subprocess": subprocess}
    for cell in cells:
        if cell["id"] == report_from:
            reporting = True
        if reporting:
            mark(cell["id"], "start")
        try:
            code = compile(clean(cell["code"]), f"<cell {cell['id']}>", "exec")
            if reporting:
                exec(code, ns)
            else:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    exec(code, ns)
        except SystemExit as e:
            if e.code not in (None, 0):
                traceback.print_exc()
                mark(cell["id"], "error")
                sys.exit(1)
        except BaseException:
            traceback.print_exc()
            sys.stdout.flush()
            mark(cell["id"], "error")
            sys.exit(1)
        sys.stdout.flush()
        if reporting:
            mark(cell["id"], "ok")


if __name__ == "__main__":
    main()
