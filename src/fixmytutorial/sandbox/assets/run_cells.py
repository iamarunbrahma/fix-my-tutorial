"""Run tutorial code cells in one shared namespace, like a notebook, printing a marker around each cell.
As in Jupyter, a cell's last bare expression is shown (unless it is None or ends with ";"), and display() works.

Usage: python3 run_cells.py cells.json [--report-from CELL_ID]
Cells before CELL_ID re-run silently (their state is needed); markers and output start at CELL_ID.
"""
import ast
import contextlib
import io
import json
import re
import subprocess
import sys
import traceback


def mark(cell_id, status):
    print("@@FMT " + json.dumps({"cell": cell_id, "status": status}), flush=True)


PYLAB = "from pylab import *; from numpy import *; import numpy as np; import matplotlib.pyplot as plt"
TIMED = re.compile(r"%time(?:it)?\s+(?:-\w(?:\s*\d+)?\s+)*(.+)")


def clean(code):
    """Turn notebook-only lines into plain Python: %pylab imports what IPython would, %time/%timeit lines and
    %%time/%%timeit cells run their code once, ! lines become shell calls, other magics are dropped."""
    out = []
    for line in code.splitlines():
        stripped = line.strip()
        indent = line[: len(line) - len(line.lstrip())]
        if stripped.startswith("%pylab"):
            out.append(indent + PYLAB)
            continue
        if m := TIMED.fullmatch(stripped):
            out.append(indent + m.group(1))
            continue
        if stripped.startswith("%"):
            continue
        if stripped.startswith("!"):
            out.append(f"{indent}__import__('subprocess').run({stripped[1:]!r}, shell=True, check=True)")
            continue
        out.append(line)
    return "\n".join(out)


def display(*objs, **_kw):
    for obj in objs:
        print(obj if isinstance(obj, str) else repr(obj))


def run_cell(source, cell_id, ns):
    """Execute a cell; show the value of a trailing bare expression the way a notebook does."""
    name = f"<cell {cell_id}>"
    tree = ast.parse(source, filename=name, mode="exec")
    last = tree.body[-1] if tree.body else None
    if isinstance(last, ast.Expr) and not source.rstrip().endswith(";"):
        exec(compile(ast.Module(body=tree.body[:-1], type_ignores=[]), name, "exec"), ns)
        value = eval(compile(ast.Expression(last.value), name, "eval"), ns)
        if value is not None:
            print(repr(value))
    else:
        exec(compile(tree, name, "exec"), ns)


def main():
    cells = json.load(open(sys.argv[1]))
    report_from = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--report-from" else None
    reporting = report_from is None
    ns = {"__name__": "__main__", "subprocess": subprocess, "display": display}
    for cell in cells:
        if cell["id"] == report_from:
            reporting = True
        if reporting:
            mark(cell["id"], "start")
        try:
            source = clean(cell["code"])
            if reporting:
                run_cell(source, cell["id"], ns)
            else:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    run_cell(source, cell["id"], ns)
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
