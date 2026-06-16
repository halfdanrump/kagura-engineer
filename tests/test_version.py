import tomllib
from pathlib import Path

import kagura_engineer


def test_version_exposed():
    assert kagura_engineer.__version__ == "0.5.0"


def test_kagura_brain_pinned_to_0_4_1_plus():
    # `select()`/`BrainHandle`/`BRAIN_API_KEY_ENV` (#63) need kagura-brain >= 0.4.0;
    # 0.4.1 adds the Windows `.cmd`-shim launch fix (core._run / _launch_argv,
    # brain #17) so a fresh install is launchable on native Windows (issue #78-B).
    # read_text needs encoding="utf-8": pyproject carries non-ASCII and a cp932
    # default codec would crash this test on a Japanese Windows console (#78-C).
    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    brain = [d for d in deps if d.replace(" ", "").startswith("kagura-brain")]
    assert brain == ["kagura-brain>=0.4.1,<0.5"], brain
