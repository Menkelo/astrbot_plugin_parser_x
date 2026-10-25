"""Build real archives from a disposable, runtime-source-only copy.

Needs setuptools, wheel and packaging in the test interpreter; no browser install.
"""

import shutil
import subprocess
import sys
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

import pytest
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "astrbot_plugin_parser_x"


def _requirements(lines):
    return {str(Requirement(line.strip())) for line in lines if line.strip() and not line.startswith("#")}


@pytest.fixture(scope="module")
def archives(tmp_path_factory):
    pytest.importorskip("setuptools")
    pytest.importorskip("wheel")
    source = tmp_path_factory.mktemp("packaging") / "source"
    source.mkdir()
    # Preserve all root Python modules, including historical standalone probes:
    # this must test what setuptools really builds, not mask stray module rules.
    for pattern in ("*.py", "*.toml", "*.md", "*.txt", "*.yaml", "*.json", "*.png"):
        for path in ROOT.glob(pattern):
            if path.is_file():
                shutil.copy2(path, source / path.name)
    for name in ("core", "assets", "pages", "licenses"):
        if (ROOT / name).is_dir():
            shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(ROOT / "LICENSE", source / "LICENSE")
    # Synthetic sentinels catch broad namespace/data discovery without copying
    # any real user config, credentials or caches into the build sandbox.
    for relative in ("data/cmd_config.json", "tests/secret.py", ".venv/secret.py"):
        sentinel = source / relative
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("PACKAGING_PRIVATE_SENTINEL", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", "from setuptools.build_meta import build_wheel, build_sdist; build_wheel('dist'); build_sdist('dist')"],
        cwd=source,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return source, next((source / "dist").glob("*.whl")), next((source / "dist").glob("*.tar.gz"))


def test_wheel_namespace_runtime_resources_and_dependencies(archives):
    source, wheel, _ = archives
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        required = {
            "__init__.py", "main.py", "_conf_schema.json", "metadata.yaml",
            "requirements.txt", "core/browser_backend.py", "core/parsers/__init__.py",
            "core/parsers/bilibili/__init__.py", "core/parsers/douyin/__init__.py",
            "core/parsers/xiaoheihe/__init__.py", "pages/debug/index.html",
            "pages/debug/app.js", "pages/debug/style.css", "THIRD_PARTY_NOTICES.md",
        }
        assert {f"{PACKAGE}/{name}" for name in required} <= names
        for pattern in ("core/**/*.py", "assets/fonts/*.woff2", "licenses/*.txt"):
            paths = list(source.glob(pattern))
            assert paths, pattern
            assert {f"{PACKAGE}/{p.relative_to(source).as_posix()}" for p in paths} <= names
        assert not any(n.startswith(("core/", "data/", "tests/")) for n in names)
        assert not any(f"/{part}/" in f"/{n}" for n in names for part in ("data", "tests", ".venv", "__pycache__"))
        assert not any("PACKAGING_PRIVATE_SENTINEL" in archive.read(n).decode("utf-8", errors="ignore") for n in names if not n.endswith("/"))
        metadata_path = next(n for n in names if n.endswith(".dist-info/METADATA"))
        metadata = BytesParser().parsebytes(archive.read(metadata_path))
        actual = {
            str(Requirement(value)) for value in metadata.get_all("Requires-Dist", [])
            if Requirement(value).marker is None
        }
        assert actual == _requirements((source / "requirements.txt").read_text().splitlines())
        assert "playwright<2,>=1.58" in actual
        assert not any("chromium" in n.lower() or "ms-playwright" in n.lower() for n in names)


def test_wheel_rebuilds_from_sdist(archives, tmp_path):
    _, _, sdist = archives
    # This is our freshly built local archive, never an untrusted download.
    shutil.unpack_archive(sdist, tmp_path)
    source = next(tmp_path.iterdir())
    result = subprocess.run(
        [sys.executable, "-c", "from setuptools.build_meta import build_wheel; build_wheel('rebuilt')"],
        cwd=source,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with zipfile.ZipFile(next((source / "rebuilt").glob("*.whl"))) as archive:
        assert f"{PACKAGE}/main.py" in archive.namelist()
        assert f"{PACKAGE}/pages/debug/index.html" in archive.namelist()
        assert any(n.endswith(".woff2") for n in archive.namelist())


def test_sdist_preserves_rebuild_inputs_without_runtime_data(archives):
    _, _, sdist = archives
    with tarfile.open(sdist) as archive:
        names = {"/".join(n.split("/")[1:]) for n in archive.getnames()}
        assert {"main.py", "pyproject.toml", "requirements.txt", "_conf_schema.json", "metadata.yaml", "pages/debug/index.html"} <= names
        assert any(n.startswith("assets/fonts/") and n.endswith(".woff2") for n in names)
        assert not any(n.startswith(("data/", "tests/", ".venv/")) for n in names)
