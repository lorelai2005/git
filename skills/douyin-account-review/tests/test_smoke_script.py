"""冒烟脚本自身也要能被导入（防止部署当天才发现脚本有语法/名字错误）。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "smoke_db.py"


@pytest.fixture(scope="module")
def smoke():
    spec = importlib.util.spec_from_file_location("smoke_db", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_importable_and_has_main(smoke):
    assert callable(smoke.main)


def test_fake_router_covers_all_endpoints_used_by_collector(smoke):
    def call(url, body=None):
        return smoke.fake_douyin_router("POST", url, {"access_token": "t"}, body)

    status, payload = call("https://open.douyin.com/api/douyin/v1/user/user_data/")
    assert status == 200
    assert payload["data"]["follower_count"] == 12345

    status, payload = call("https://open.douyin.com/api/douyin/v1/video/video_list/")
    assert len(payload["data"]["list"]) == 3
    assert payload["data"]["has_more"] is False

    status, payload = call("https://open.douyin.com/api/douyin/v1/video/video_data/",
                           {"item_ids": ["a", "b"]})
    assert [row["item_id"] for row in payload["data"]["list"]] == ["a", "b"]
    assert payload["data"]["list"][0]["statistics"]["play_count"] == 8000

    status, payload = call("https://open.douyin.com/api/douyin/v1/user/fan_data/")
    assert "gender" in payload["data"]


def test_fake_router_envelope_has_zero_error_code(smoke):
    _, payload = smoke.fake_douyin_router("GET", "https://x/user_data/", {}, None)
    assert payload["extra"]["error_code"] == 0


def test_transport_stub_counts_calls(smoke):
    transport = smoke._Transport()
    transport.request("POST", "https://x/user_data/")
    transport.request("POST", "https://x/video_list/")
    assert transport.calls == 2
