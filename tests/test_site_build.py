import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_PATH = ROOT / "docs" / "site" / "build.py"


def _load_site_build():
    spec = importlib.util.spec_from_file_location("hyqs_site_build", BUILD_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_get_started_chapter_contains_supported_install_path():
    site_build = _load_site_build()
    index = next(i for i, chapter in enumerate(site_build.CHAPTERS) if chapter[0] == "get-started")

    rendered = site_build.page(index)

    assert "Get started" in rendered
    assert "git clone https://github.com/clementmilville/hyqs.git ~/hyqs-ai" in rendered
    assert "ssh -L 8787:127.0.0.1:8787" in rendered
    assert 'href="/how-it-works/pipeline/"' in rendered


def test_cover_links_to_install_guide_and_public_source():
    site_build = _load_site_build()

    rendered = site_build.page(0)

    assert 'href="/how-it-works/get-started/">Install Hyqs' in rendered
    assert 'href="https://github.com/clementmilville/hyqs">View source' in rendered
