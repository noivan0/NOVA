"""tests/unit/test_kb.py — Unit tests for the Knowledge Base."""
import tempfile

from nova.core.kb import KB


def test_write_and_read():
    with tempfile.TemporaryDirectory() as tmp:
        kb = KB(tmp)
        kb.write("projects/test", "# Test\nHello world")
        content = kb.read("projects/test")
        assert content is not None
        assert "Hello world" in content


def test_search():
    with tempfile.TemporaryDirectory() as tmp:
        kb = KB(tmp)
        kb.write("config/setting", "api_url = https://example.com")
        kb.write("config/other", "nothing relevant here")
        results = kb.search("example.com")
        assert len(results) == 1
        assert results[0]["key"] == "config/setting"


def test_append_log():
    with tempfile.TemporaryDirectory() as tmp:
        kb = KB(tmp)
        kb.append_log("test-run | my-harness — SUCCESS (3 phases, 12s)")
        log = kb.read("log")
        assert "my-harness" in log


def test_list_pages():
    with tempfile.TemporaryDirectory() as tmp:
        kb = KB(tmp)
        kb.write("projects/alpha", "alpha content")
        kb.write("projects/beta", "beta content")
        kb.write("config/cfg", "config content")
        projects = kb.list_pages(prefix="projects")
        assert "projects/alpha" in projects
        assert "projects/beta" in projects
        assert "config/cfg" not in projects
