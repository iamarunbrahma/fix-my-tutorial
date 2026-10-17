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
import time
import traceback


def mark(cell_id, status):
    print("@@FMT " + json.dumps({"cell": cell_id, "status": status}), flush=True)


PYLAB = ("import numpy, matplotlib; from matplotlib import pylab, mlab, pyplot; np, plt = numpy, pyplot; "
         "from matplotlib.pylab import *; from numpy import *")  # what IPython's %pylab runs
TIMED = re.compile(r"(?:([\w.]+)\s*=\s*)?%(time(?:it)?)\s+((?:-[A-Za-z]+(?:\s*\d+)?\s+)*)(.+)")  # `t = %timeit -o x`
CELL_TIMED = re.compile(r"%%(time(?:it)?)(?:\s+-[A-Za-z]+(?:\s*\d+)?)*\s*(.*)")  # the rest of a %%timeit line is setup
CLOCK = "__import__('time').perf_counter()"
MAGIC = re.compile(r"%%?[A-Za-z_]")  # a magic's name follows the %; "% (a, b)" on a continuation line is the operator


class TimeitResult:
    """What `%timeit -o` hands back, timed from one run instead of many."""

    def __init__(self, seconds):
        self.best = self.worst = self.average = seconds
        self.stdev, self.loops, self.repeat, self.compile_time = 0.0, 1, 1, 0.0
        self.all_runs = self.timings = [seconds]

    def __repr__(self):
        return f"{self.average * 1e3:.3g} ms per loop (1 run, 1 loop each)"


def timeit_once(fn, quiet):
    start = time.perf_counter()
    fn()
    result = TimeitResult(time.perf_counter() - start)
    if not quiet:
        print(result)
    return result


def report(magic):
    """The line IPython prints after timing, from one run instead of many."""
    text = "Wall time: {:.3g} ms" if magic == "time" else "{:.3g} ms per loop (1 run, 1 loop each)"
    return f"print({text!r}.format(({CLOCK} - _fmt_t) * 1e3))"


def clean(code):
    """Turn notebook-only lines into plain Python: %pylab imports what IPython would, %time/%timeit lines and
    %%time/%%timeit cells run their code once and print how long it took, ! lines become shell calls, other magics
    are dropped."""
    out, cell_magic = [], None
    for line in code.splitlines():
        stripped = line.strip()
        indent = line[: len(line) - len(line.lstrip())]
        if not any(o.strip() for o in out) and (m := CELL_TIMED.fullmatch(stripped)):
            cell_magic = m.group(1)
            out += [m.group(2)] if m.group(2) else []
            out.append("_fmt_t = " + CLOCK)
            continue
        if stripped.startswith("%pylab"):
            out.append(indent + PYLAB)
            continue
        if m := TIMED.fullmatch(stripped):
            target, magic, opts, stmt = m.groups()
            quiet = any("q" in o for o in re.findall(r"-([A-Za-z]+)", opts))
            if target and magic == "timeit":  # IPython hands back the timing itself
                out.append(f"{indent}{target} = _fmt_timeit(lambda: ({stmt}), {quiet})")
                continue
            out += [indent + "_fmt_t = " + CLOCK, indent + (f"{target} = {stmt}" if target else stmt)]
            out += [] if quiet else [indent + report(magic)]
            continue
        if MAGIC.match(stripped):
            continue
        if stripped.startswith("!"):
            out.append(f"{indent}__import__('subprocess').run({stripped[1:]!r}, shell=True, check=True)")
            continue
        out.append(line)
    if cell_magic:
        out.append(report(cell_magic))
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
    ns = {"__name__": "__main__", "subprocess": subprocess, "display": display, "_fmt_timeit": timeit_once}
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
