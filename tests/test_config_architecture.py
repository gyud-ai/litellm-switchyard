"""Configuration errors, architecture direction, and sanitized observability."""

import ast
import io
import json
from pathlib import Path

import pytest

from switchyard_gateway.adapters.config import load_config
from switchyard_gateway.adapters.logging import JsonEvents
from switchyard_gateway.domain import GatewayError

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


class TestConfig:
    def test_jsonc_comments_trailing_comma_env_and_multiple_pairs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_GATEWAY_KEY", "private")
        path = tmp_path / "config.jsonc"
        path.write_text("""{
          // settings
          "server": {"api_key": {"env": "TEST_GATEWAY_KEY"}},
          "models": {
            "small": {"model_id": "small", "endpoints": [{"name":"a", "base_url":"http://a/v1"}]},
            "large": {"model_id": "large", "endpoints": [{"name":"b", "base_url":"http://b/v1"}]},
          },
          "pairs": {"one": {"capable":"large", "efficient":"small"},
                    "two": {"capable":"small", "efficient":"large"}},
        }""")
        settings = load_config(path)
        assert settings.api_key == "private"
        assert len(settings.pairs) == 2
        assert settings.compression

    @pytest.mark.parametrize(
        "change",
        [
            {"models": {}},
            {"pairs": {"bad": {"capable": "missing", "efficient": "small"}}},
            {"pairs": {"small": {"capable": "small", "efficient": "small"}}},
            {"forward_headers": ["authorization"]},
            {"unknown": "private"},
            {"server": {"api_key": {"env": "GATEWAY_TEST_UNSET"}}},
        ],
    )
    def test_bad_config_never_echoes_values(self, tmp_path, change):
        value = {
            "server": {"api_key": "private"},
            "models": {
                "small": {
                    "model_id": "small",
                    "endpoints": [{"name": "a", "base_url": "http://a/v1"}],
                }
            },
        } | change
        path = tmp_path / "config.jsonc"
        path.write_text(json.dumps(value))
        with pytest.raises(GatewayError) as caught:
            load_config(path)
        assert "private" not in str(caught.value)

    def test_duplicate_json_keys_rejected(self, tmp_path):
        path = tmp_path / "config.jsonc"
        path.write_text('{"server": {}, "server": {}}')
        with pytest.raises(GatewayError, match="invalid_configuration"):
            load_config(path)

    def test_checked_in_example_validates(self, monkeypatch):
        for name in ("GATEWAY_API_KEY", "EFFICIENT_API_KEY", "CAPABLE_API_KEY"):
            monkeypatch.setenv(name, "dummy")
        for name in ("EFFICIENT_API_BASE", "CAPABLE_API_BASE"):
            monkeypatch.setenv(name, "http://example.invalid/v1")
        assert (
            load_config(ROOT / "config.example.jsonc").pairs["switchyard"].efficient == "efficient"
        )


class TestArchitecture:
    def test_core_only_imports_standard_library_and_core(self):
        import sys

        allowed = sys.stdlib_module_names | {"domain", "ports", "application"}
        for filename in ("domain.py", "ports.py", "application.py"):
            tree = ast.parse((ROOT / "src/switchyard_gateway" / filename).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    assert all(alias.name.split(".")[0] in allowed for alias in node.names)
                if isinstance(node, ast.ImportFrom):
                    assert (node.module or "").split(".")[0] in allowed

    def test_vendor_imports_only_in_their_adapters(self):
        permitted = {"switchyard": "switchyard.py", "headroom": "headroom.py"}
        for path in (ROOT / "src/switchyard_gateway").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                for name in names:
                    if name.split(".")[0] in permitted:
                        assert path.name == permitted[name.split(".")[0]]

    def test_json_events_are_one_record_per_line(self):
        output = io.StringIO()
        sink = JsonEvents(output)
        sink.emit({"event": "request", "request_id": "id", "outcome": "completed"})
        record = json.loads(output.getvalue())
        assert record["outcome"] == "completed"
        assert "timestamp" in record
        assert len(output.getvalue().splitlines()) == 1
