import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import prod_tree  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="needs a git checkout")


def test_prod_device_keeps_the_app_and_drops_development_files():
    keep, drop = prod_tree.split(prod_tree.tracked())
    assert "requirements.txt" in keep and "deploy/bongo-matrix.service" in keep and "deploy/bongo-update.path" in keep
    assert any(f.startswith("resources/fonts") for f in keep) and "bongo/web/static/index.html" in keep
    assert prod_tree.dev_only("resources/astronomy/de440s.bsp")  # v2's 30 MB ephemeris, unused
    assert not [f for f in keep if f.startswith(("tests/", "tools/", "docs/")) or "demo" in Path(f).name]
    assert ".gitlab-ci.yml" in drop and "tests/test_prod_tree.py" in drop


def test_the_kept_files_run_on_their_own(tmp_path):
    keep, _ = prod_tree.split(prod_tree.tracked())
    for f in keep:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        if (ROOT / f).is_dir():  # a linked folder in a dev checkout
            shutil.copytree(ROOT / f, tmp_path / f)
        else:
            shutil.copy2(ROOT / f, tmp_path / f)
    run = subprocess.run([sys.executable, "-m", "bongo", "list"], cwd=tmp_path, capture_output=True, text=True,
                         env={"BONGO_OFFLINE": "1", "PATH": "/usr/bin:/bin"})
    assert run.returncode == 0, run.stderr
    assert "lava_lamp" in run.stdout and "splash" in run.stdout
    page = (tmp_path / "bongo/web/static/index.html").read_text()
    assert "demo.js" not in page  # the demo is only loaded on ?demo, never by the device's page
