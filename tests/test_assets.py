import json
import socket
import subprocess
import sys
import time

import httpx

from fixmytutorial.sandbox import ASSETS_DIR, cap_output

RUNNER = str(ASSETS_DIR / "run_cells.py")


def run_cells(tmp_path, cells, report_from=None):
    f = tmp_path / "cells.json"
    f.write_text(json.dumps(cells))
    args = [sys.executable, RUNNER, str(f)] + (["--report-from", report_from] if report_from else [])
    return subprocess.run(args, capture_output=True, text=True, timeout=30)


def markers(stdout):
    return [json.loads(line[6:]) for line in stdout.splitlines() if line.startswith("@@FMT ")]


def test_cells_share_namespace(tmp_path):
    p = run_cells(tmp_path, [{"id": "s1", "code": "x = 41"}, {"id": "s2", "code": "print(x + 1)"}])
    assert p.returncode == 0
    assert "42" in p.stdout
    assert [m["status"] for m in markers(p.stdout)] == ["start", "ok", "start", "ok"]


def test_failing_cell_reports_error_and_traceback(tmp_path):
    p = run_cells(tmp_path, [{"id": "s1", "code": "import math"}, {"id": "s2", "code": "math.nope()"}])
    assert p.returncode == 1
    assert markers(p.stdout)[-1] == {"cell": "s2", "status": "error"}
    assert "AttributeError" in p.stderr


def test_report_from_silences_earlier_cells(tmp_path):
    p = run_cells(tmp_path, [{"id": "s1", "code": "print('early'); y = 2"}, {"id": "s2", "code": "print(y)"}],
                  report_from="s2")
    assert "early" not in p.stdout and "2" in p.stdout
    assert [m["cell"] for m in markers(p.stdout)] == ["s2", "s2"]


def test_magics_are_handled(tmp_path):
    p = run_cells(tmp_path, [{"id": "s1", "code": "%matplotlib inline\n!echo shell-ok\nprint('py-ok')"}])
    assert p.returncode == 0 and "shell-ok" in p.stdout and "py-ok" in p.stdout


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_openai_stub_serves_chat_models_and_embeddings():
    port = free_port()
    proc = subprocess.Popen([sys.executable, str(ASSETS_DIR / "openai_stub.py"), str(port)])
    try:
        base = f"http://127.0.0.1:{port}/v1"
        for _ in range(50):
            try:
                httpx.get(base + "/models", timeout=1)
                break
            except httpx.TransportError:
                time.sleep(0.1)
        assert "gpt-4o-mini" in httpx.get(base + "/models").text
        chat = httpx.post(base + "/chat/completions", json={"model": "gpt-4o-mini", "messages": []}).json()
        assert chat["choices"][0]["message"]["content"].startswith("This is an offline reply")
        emb = httpx.post(base + "/embeddings", json={"model": "e", "input": ["a", "b"]}).json()
        assert len(emb["data"]) == 2 and len(emb["data"][0]["embedding"]) == 1536
    finally:
        proc.kill()


def test_cap_output_keeps_tail():
    big = "a" * 70_000 + "TAIL"
    out = cap_output(big)
    assert out.endswith("TAIL") and out.startswith("[output truncated]") and len(out) < 66_000


async def test_capped_subprocess_read_keeps_memory_bounded():
    from fixmytutorial.sandbox import OUTPUT_CAP
    from fixmytutorial.sandbox.docker_backend import run_capped
    rc, out, err = await run_capped(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 5_000_000 + 'END'); sys.stderr.write('e')"],
        timeout=30)
    assert rc == 0 and out.endswith("END") and len(out) <= OUTPUT_CAP + 64 and err == "e"


def test_last_expression_is_shown_like_a_notebook(tmp_path):
    p = run_cells(tmp_path, [{"id": "s1", "code": "counts = {'setosa': 50}\ncounts"},
                             {"id": "s2", "code": "x = 1\nx + 41"},
                             {"id": "s3", "code": "y = 3"},
                             {"id": "s4", "code": "print('hi')"},
                             {"id": "s5", "code": "len('abc');"}])
    assert p.returncode == 0
    out = p.stdout
    assert "{'setosa': 50}" in out and "42" in out
    assert out.count("hi") == 1  # print() returns None: nothing extra is shown
    assert "\n3\n" not in out    # a trailing semicolon hides the value, as in Jupyter


def test_display_works_outside_a_notebook(tmp_path):
    p = run_cells(tmp_path, [{"id": "s1", "code": "display({'a': 1}, 'two')"}])
    assert p.returncode == 0 and "{'a': 1}" in p.stdout and "two" in p.stdout


def test_silent_cells_do_not_show_values(tmp_path):
    p = run_cells(tmp_path, [{"id": "s1", "code": "'secret-early'"}, {"id": "s2", "code": "print('late')"}],
                  report_from="s2")
    assert "secret-early" not in p.stdout and "late" in p.stdout


def _serve_404(port):
    """A server that is up but has no route at "/" (the review, 16 Oct: an app with only POST /data)."""
    code = ("import http.server, sys\n"
            "class H(http.server.BaseHTTPRequestHandler):\n"
            "    def do_GET(self): self.send_response(404); self.end_headers()\n"
            "    def log_message(self, *a): pass\n"
            "http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()\n")
    return subprocess.Popen([sys.executable, "-c", code, str(port)])


def probe(port, status):
    return subprocess.run([sys.executable, str(ASSETS_DIR / "probe.py"), "--port", str(port), "--path", "/",
                           "--status", str(status), "--wait", "10"], capture_output=True, text=True, timeout=30)


def test_probe_without_a_tutorial_url_counts_any_answer_as_up():
    port = free_port()
    proc = _serve_404(port)
    try:
        assert probe(port, 0).returncode == 0  # listening: a 404 at "/" is still an answer
        assert probe(port, 200).returncode == 1  # a URL the tutorial names must still match exactly
    finally:
        proc.kill()
