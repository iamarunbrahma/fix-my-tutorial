import pytest

from fixmytutorial.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, data_dir=tmp_path, sandbox_backend="fake")
