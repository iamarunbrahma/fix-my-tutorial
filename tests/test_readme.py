import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_readme_states_how_many_tutorials_the_rot_index_shows():
    # 17 Oct: the README said 115 (the whole list) while the index showed 31; the count must match the published file
    stated = re.search(r"shows (\d+) popular Python and JavaScript tutorials", (ROOT / "README.md").read_text())
    published = json.loads((ROOT / "rot_index" / "results.json").read_text())["entries"]
    assert stated and int(stated.group(1)) == len(published)
