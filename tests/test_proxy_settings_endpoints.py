"""Tests for the dashboard settings API endpoints (Phases 2-4).

Covers GET /settings/schema, GET /settings, POST /settings, POST /settings/apply
and the GET /dashboard/settings route: loopback gating on all read/write settings
endpoints, registry validation (400 unknown key / 422 bad value), secret masking,
and the deployment-aware apply/restart dispatch (service 202 + background restart,
docker host command, foreground instruction).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from headroom import settings_store  # noqa: E402
from headroom.proxy.server import ProxyConfig, create_app  # noqa: E402


def _make_app():
    return create_app(
        ProxyConfig(
            optimize=False,
            cache_enabled=False,
            rate_limit_enabled=False,
            cost_tracking_enabled=False,
            log_requests=False,
            ccr_inject_tool=False,
            ccr_handle_responses=False,
            ccr_context_tracking=False,
            image_optimize=False,
        )
    )


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Isolate settings.json under a tmp workspace for each test."""
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("HEADROOM_SETTINGS_PATH", raising=False)
    # Make deployment detection deterministic: no supervisor env -> foreground.
    monkeypatch.delenv("HEADROOM_DEPLOYMENT_PROFILE", raising=False)
    monkeypatch.delenv("HEADROOM_DEPLOYMENT_PRESET", raising=False)
    return tmp_path


@pytest.fixture
def client(workspace):
    """Loopback client — passes both the client-IP and Host-header gates."""
    return TestClient(_make_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 12345))


@pytest.fixture
def network_client(workspace):
    """Default TestClient presents client.host='testclient' — treated as non-loopback."""
    return TestClient(_make_app())


class TestSchemaAndRead:
    def test_schema_returns_grouped_fields(self, client):
        resp = client.get("/settings/schema")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["groups"]
        assert body["fields"]
        assert body["needs_restart_keys"]
        assert "supervised" in body  # server-added deployment flag
        assert body["supervised"] is False  # foreground in tests

    def test_get_settings_reflects_saved_values(self, client, workspace):
        settings_store.save({"target_ratio": 0.5, "savings_profile": "balanced"})
        resp = client.get("/settings")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"target_ratio": 0.5, "savings_profile": "balanced"}


class TestEndpointsGroup:
    def test_schema_includes_endpoints_group_and_secret_flags(self, client):
        resp = client.get("/settings/schema")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "Endpoints" in body["groups"]
        by_key = {f["key"]: f for f in body["fields"]}
        for key in (
            "anthropic_base_url",
            "openai_base_url",
            "anthropic_extra_headers",
            "openai_extra_headers",
        ):
            assert key in by_key, f"missing field {key}"
        assert by_key["anthropic_extra_headers"]["secret"] is True
        assert by_key["openai_extra_headers"]["secret"] is True
        assert by_key["anthropic_base_url"]["secret"] is False

    def test_get_settings_masks_extra_headers(self, client, workspace):
        settings_store.save({"anthropic_extra_headers": '{"Api-Key": "super-secret"}'})
        resp = client.get("/settings")
        assert resp.status_code == 200, resp.text
        assert resp.json()["anthropic_extra_headers"] == settings_store._MASK

    def test_schema_values_mask_extra_headers(self, client, workspace):
        settings_store.save({"openai_extra_headers": '{"Api-Key": "super-secret"}'})
        resp = client.get("/settings/schema")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["values"]["openai_extra_headers"] == settings_store._MASK


class TestWriteValidation:
    def test_valid_write_persists(self, client, workspace):
        resp = client.post("/settings", json={"values": {"target_ratio": 0.4, "rpm": 30}})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["needs_restart"] is True
        assert set(body["changed_keys"]) == {"target_ratio", "rpm"}
        assert settings_store.load() == {"target_ratio": 0.4, "rpm": 30}

    def test_unknown_key_400(self, client):
        resp = client.post("/settings", json={"values": {"nope": 1}})
        assert resp.status_code == 400, resp.text
        assert "nope" in resp.json()["unknown_keys"]

    def test_bad_value_422(self, client):
        resp = client.post("/settings", json={"values": {"target_ratio": 5}})
        assert resp.status_code == 422, resp.text
        assert "target_ratio" in resp.json()["field_errors"]

    def test_missing_values_object_400(self, client):
        resp = client.post("/settings", json={"foo": 1})
        assert resp.status_code == 400, resp.text


class TestLoopbackGating:
    def test_reads_and_writes_rejected_off_loopback(self, network_client):
        assert network_client.get("/settings/schema").status_code == 404
        assert network_client.get("/settings").status_code == 404
        assert network_client.post("/settings", json={"values": {}}).status_code == 404
        assert network_client.post("/settings/apply", json={}).status_code == 404

    def test_settings_page_rejected_off_loopback(self, network_client):
        assert network_client.get("/dashboard/settings").status_code == 404


class TestSameOriginGuard:
    """CSRF: a same-machine (loopback) attacker page can still send a
    non-preflighted 'simple' request straight to a known 127.0.0.1 URL --
    the Host header reads the real (loopback) destination either way, but
    a real browser's Origin header reflects the page's actual origin.
    require_loopback's Host-header check alone does not catch this.
    """

    def test_foreign_origin_rejected_on_settings_post(self, client, workspace):
        resp = client.post(
            "/settings",
            json={"values": {"target_ratio": 0.4}},
            headers={"Origin": "https://evil.example"},
        )
        assert resp.status_code == 403, resp.text

    def test_null_origin_rejected(self, client, workspace):
        resp = client.post(
            "/settings",
            json={"values": {"target_ratio": 0.4}},
            headers={"Origin": "null"},
        )
        assert resp.status_code == 403, resp.text

    def test_foreign_origin_rejected_on_settings_apply(self, client, monkeypatch):
        from headroom.install import runtime as rt

        monkeypatch.setattr(
            rt, "restart_current_deployment", lambda: {"restarted": False, "mode": "foreground"}
        )
        resp = client.post(
            "/settings/apply",
            json={},
            headers={"Origin": "https://evil.example"},
        )
        assert resp.status_code == 403, resp.text

    def test_loopback_origin_allowed(self, client, workspace):
        resp = client.post(
            "/settings",
            json={"values": {"target_ratio": 0.4}},
            headers={"Origin": "http://127.0.0.1:8787"},
        )
        assert resp.status_code == 200, resp.text

    def test_absent_origin_allowed(self, client, workspace):
        # No Origin header at all (CLI tools, curl, TestClient's default) must pass.
        resp = client.post("/settings", json={"values": {"target_ratio": 0.4}})
        assert resp.status_code == 200, resp.text


class TestSecretMasking:
    def test_secret_never_returned_unmasked(self, client, workspace, monkeypatch):
        # No curated knob is secret; flag one to exercise the masking path end-to-end.
        base_field = next(f for f in settings_store.SETTINGS if f.key == "log_file")
        secret_field = replace(base_field, secret=True)
        registry = tuple(
            secret_field if f.key == "log_file" else f for f in settings_store.SETTINGS
        )
        monkeypatch.setattr(settings_store, "SETTINGS", registry)
        monkeypatch.setattr(settings_store, "_BY_KEY", {f.key: f for f in registry})
        settings_store.save({"log_file": "/tmp/secret.log"})

        assert client.get("/settings").json()["log_file"] == settings_store._MASK
        schema = client.get("/settings/schema").json()
        log_field = next(f for f in schema["fields"] if f["key"] == "log_file")
        assert log_field["value"] == settings_store._MASK
        assert log_field["stored"] == settings_store._MASK


class TestApplyRestart:
    def _patch_mode(self, monkeypatch, mode, restart_result):
        from headroom.install import runtime as rt

        monkeypatch.setattr(rt, "detect_current_deployment", lambda: (None, mode))
        calls = []

        def fake_restart():
            calls.append(True)
            return restart_result

        monkeypatch.setattr(rt, "restart_current_deployment", fake_restart)
        return calls

    def test_service_returns_202_and_runs_background_restart(self, client, monkeypatch):
        calls = self._patch_mode(monkeypatch, "service", {"restarted": True, "mode": "service"})
        resp = client.post("/settings/apply", json={})
        assert resp.status_code == 202, resp.text
        assert resp.json()["restarted"] is True
        # TestClient runs the BackgroundTask after sending the response.
        assert calls == [True]

    def test_docker_returns_host_command(self, client, monkeypatch):
        self._patch_mode(
            monkeypatch,
            "docker",
            {
                "restarted": False,
                "mode": "docker",
                "command": "headroom install restart --profile default",
            },
        )
        resp = client.post("/settings/apply", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["mode"] == "docker"
        assert "headroom install restart" in body["command"]

    def test_foreground_returns_instruction(self, client, monkeypatch):
        self._patch_mode(
            monkeypatch,
            "foreground",
            {
                "restarted": False,
                "mode": "foreground",
                "instruction": "Restart the proxy to apply the new settings.",
            },
        )
        resp = client.post("/settings/apply", json={})
        assert resp.status_code == 200, resp.text
        assert "instruction" in resp.json()

    def test_apply_persists_provided_values(self, client, workspace, monkeypatch):
        self._patch_mode(
            monkeypatch,
            "foreground",
            {"restarted": False, "mode": "foreground", "instruction": "x"},
        )
        resp = client.post("/settings/apply", json={"values": {"rpm": 42}})
        assert resp.status_code == 200, resp.text
        assert settings_store.load() == {"rpm": 42}


class TestSettingsPageRoute:
    def test_dashboard_settings_serves_html(self, client):
        resp = client.get("/dashboard/settings")
        assert resp.status_code == 200, resp.text
        assert "<" in resp.text  # rendered HTML page


class TestOpenRouterFallbackSchema:
    def test_schema_includes_openrouter_fallback_group(self, client):
        resp = client.get("/settings/schema")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "OpenRouter Fallback" in body["groups"]
        by_key = {f["key"]: f for f in body["fields"]}
        for key in (
            "session_limit_fallback",
            "openrouter_api_key",
            "session_limit_fallback_default_model",
            "session_limit_fallback_model_map",
            "session_limit_fallback_threshold",
        ):
            assert key in by_key, f"missing field {key}"
        assert by_key["session_limit_fallback"]["type"] == "bool"
        assert by_key["openrouter_api_key"]["secret"] is True
        assert by_key["session_limit_fallback_model_map"]["type"] == "model-map"
        assert by_key["session_limit_fallback_threshold"]["type"] == "float"

    def test_schema_includes_anthropic_models(self, client):
        resp = client.get("/settings/schema")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "anthropic_models" in body
        assert isinstance(body["anthropic_models"], list)
        assert len(body["anthropic_models"]) > 0
        assert "claude-sonnet-4-5" in body["anthropic_models"]

    def test_schema_includes_anthropic_model_families(self, client):
        resp = client.get("/settings/schema")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "anthropic_model_families" in body
        assert isinstance(body["anthropic_model_families"], list)
        assert len(body["anthropic_model_families"]) > 0
        # Family slugs derived from the known model IDs.
        for fam in ("sonnet-5", "opus-5", "opus-4-8", "fable-5", "haiku-4-5"):
            assert fam in body["anthropic_model_families"], f"missing family {fam}"
        # Full IDs must not leak into the family list.
        assert "claude-sonnet-4-5-20250929" not in body["anthropic_model_families"]


class TestOpenRouterFallbackWrite:
    def test_valid_write_persists(self, client, workspace):
        resp = client.post(
            "/settings",
            json={
                "values": {
                    "session_limit_fallback": True,
                    "openrouter_api_key": "sk-or-test",
                    "session_limit_fallback_default_model": "openai/gpt-4o",
                    "session_limit_fallback_model_map": '{"claude-x": "openai/gpt-4o"}',
                    "session_limit_fallback_threshold": 0.9,
                }
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert set(body["changed_keys"]) == {
            "session_limit_fallback",
            "openrouter_api_key",
            "session_limit_fallback_default_model",
            "session_limit_fallback_model_map",
            "session_limit_fallback_threshold",
        }
        stored = settings_store.load()
        assert stored["session_limit_fallback"] is True
        assert stored["openrouter_api_key"] == "sk-or-test"
        assert stored["session_limit_fallback_default_model"] == "openai/gpt-4o"
        assert stored["session_limit_fallback_model_map"] == '{"claude-x": "openai/gpt-4o"}'
        assert stored["session_limit_fallback_threshold"] == 0.9

    def test_get_settings_masks_api_key(self, client, workspace):
        settings_store.save({"openrouter_api_key": "sk-or-secret"})
        resp = client.get("/settings")
        assert resp.status_code == 200, resp.text
        assert resp.json()["openrouter_api_key"] == settings_store._MASK

    def test_schema_values_mask_api_key(self, client, workspace):
        settings_store.save({"openrouter_api_key": "sk-or-secret"})
        resp = client.get("/settings/schema")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["values"]["openrouter_api_key"] == settings_store._MASK

    def test_threshold_out_of_range_422(self, client, workspace):
        resp = client.post(
            "/settings",
            json={"values": {"session_limit_fallback_threshold": 2.0}},
        )
        assert resp.status_code == 422, resp.text
        assert "session_limit_fallback_threshold" in resp.json()["field_errors"]

    def test_model_map_invalid_json_422(self, client, workspace):
        resp = client.post(
            "/settings",
            json={"values": {"session_limit_fallback_model_map": "not json"}},
        )
        assert resp.status_code == 422, resp.text
        assert "session_limit_fallback_model_map" in resp.json()["field_errors"]
