"""Wait for a local server and check one URL. Exit 0 when the status matches (--status 0: any answer below 500)."""
import argparse
import sys
import time
import urllib.error
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, required=True)
ap.add_argument("--path", default="/")
ap.add_argument("--status", type=int, default=200)
ap.add_argument("--wait", type=float, default=30)
a = ap.parse_args()
deadline = time.time() + a.wait
while time.time() < deadline:
    try:
        code = urllib.request.urlopen(f"http://127.0.0.1:{a.port}{a.path}", timeout=3).status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        time.sleep(0.5)
        continue
    print(f"probe: GET {a.path} -> {code} (expected {a.status or 'any answer'})")
    sys.exit(0 if (code < 500 if a.status == 0 else code == a.status) else 1)
print(f"probe: no server answered on port {a.port} within {a.wait:.0f}s")
sys.exit(1)
