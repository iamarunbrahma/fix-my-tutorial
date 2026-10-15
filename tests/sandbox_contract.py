import asyncio

from fixmytutorial.sandbox import PRELUDE


async def check_echo(b):
    base = await b.base_image("python")
    r = await b.run(base, f"{PRELUDE}\necho hi", save=False)
    assert r.exit_code == 0 and r.stdout.strip() == "hi" and r.image is None


async def check_nonzero_exit(b):
    base = await b.base_image("python")
    r = await b.run(base, f"{PRELUDE}\necho bad >&2; exit 3", save=False)
    assert r.exit_code == 3 and "bad" in r.stderr


async def check_save_and_reuse(b):
    base = await b.base_image("python")
    r1 = await b.run(base, f"{PRELUDE}\necho data > f.txt", save=True)
    assert r1.exit_code == 0 and r1.image is not None
    r2 = await b.run(r1.image, f"{PRELUDE}\ncat f.txt", save=False)
    assert r2.stdout.strip() == "data"


async def check_files(b):
    base = await b.base_image("python")
    r = await b.run(base, f"{PRELUDE}\ncat a.txt", files={"/work/a.txt": b"from-file"}, save=False)
    assert r.stdout.strip() == "from-file"


async def check_parallel_branches_are_isolated(b):
    base = await b.base_image("python")
    cp = await b.run(base, f"{PRELUDE}\necho shared > s.txt", save=True)
    ra, rb = await asyncio.gather(
        b.run(cp.image, f"{PRELUDE}\necho A > a.txt; ls", save=True),
        b.run(cp.image, f"{PRELUDE}\necho B > b.txt; ls", save=True),
    )
    assert "a.txt" in ra.stdout and "b.txt" not in ra.stdout
    assert "b.txt" in rb.stdout and "a.txt" not in rb.stdout


async def check_timeout(b):
    base = await b.base_image("python")
    r = await b.run(base, f"{PRELUDE}\nsleep 60", timeout_s=3, save=False)
    assert r.timed_out and r.exit_code != 0


async def check_openai_stub(b):
    base = await b.base_image("python")
    script = (f"{PRELUDE}\npython3 -c \"import urllib.request;"
              f"print(urllib.request.urlopen('http://127.0.0.1:8099/v1/models').read().decode())\"")
    r = await b.run(base, script, save=False)
    assert r.exit_code == 0 and "gpt-4o-mini" in r.stdout


async def check_node_image(b):
    base = await b.base_image("javascript")
    r = await b.run(base, f"{PRELUDE}\nnode -e \"console.log(process.version)\"", save=False)
    assert r.exit_code == 0 and r.stdout.startswith("v22")


ALL_CHECKS = [check_echo, check_nonzero_exit, check_save_and_reuse, check_files,
              check_parallel_branches_are_isolated, check_timeout, check_openai_stub, check_node_image]
