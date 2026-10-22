"""飞书推送测试：长文分片、代码块不被切坏、token 缓存、推送失败不抛异常。"""
from __future__ import annotations

import json

import pytest

from conftest import FakeFeishuTransport
from dyreview.feishu import FeishuClient, FeishuError, split_markdown


def test_short_text_single_chunk():
    assert split_markdown("hello", 100) == ["hello"]


def test_long_text_respects_limit():
    text = "\n".join(f"第 {i} 行内容" * 3 for i in range(400))
    chunks = split_markdown(text, 500)
    assert len(chunks) > 1
    assert all(len(c) <= 520 for c in chunks)
    assert all(c.strip() for c in chunks)
    # 切分不丢内容（切点都落在换行处，用换行拼回即还原原文）
    assert "\n".join(chunks).split() == text.split()


def test_code_fence_not_split():
    body = "```python\n" + "\n".join(f"line_{i}" for i in range(60)) + "\n```"
    chunks = split_markdown(body, 120)
    assert len(chunks) == 1, "代码块整体保留，不被切碎"


def test_empty_text():
    assert split_markdown("", 100) == []
    assert split_markdown("   ", 100) == []


@pytest.fixture
def client(make_cfg):
    cfg = make_cfg({"feishu.app_id": "cli_a", "feishu.app_secret": "sec_b",
                    "feishu.default_chat_id": "oc_test", "feishu.card_max_chars": 300})
    transport = FakeFeishuTransport()
    return FeishuClient(cfg, transport=transport, sleep=lambda s: None), transport


def test_send_card_chunks_and_returns_message_ids(client):
    feishu, transport = client
    long_md = "\n\n".join(f"## 小节 {i}\n" + "内容" * 80 for i in range(4))
    ids = feishu.send_card("oc_test", "标题", long_md)
    assert len(ids) >= 2
    assert all(i.startswith("om_") for i in ids)
    card_bodies = [json.loads(c["body"]["content"]) for c in transport.calls
                   if "im/v1/messages" in c["url"]]
    assert card_bodies[0]["header"]["title"]["content"] == "标题"
    assert all(b["elements"][0]["text"]["content"] for b in card_bodies)


def test_tenant_token_cached_across_sends(client):
    feishu, transport = client
    feishu.send_text("oc_test", "a")
    feishu.send_text("oc_test", "b")
    token_calls = [c for c in transport.calls if "tenant_access_token" in c["url"]]
    assert len(token_calls) == 1


def test_push_report_failure_does_not_raise(make_cfg):
    cfg = make_cfg({"feishu.app_id": "cli_a", "feishu.app_secret": "sec_b",
                    "feishu.default_chat_id": "oc_test"})
    feishu = FeishuClient(cfg, transport=lambda *a, **k: {"code": 230001, "msg": "bad"},
                          sleep=lambda s: None)
    result = feishu.push_report(receive_id="oc_test", title="t", markdown="m")
    assert result["ok"] is False
    assert "230001" in result["error"]


def test_push_report_uses_webhook_when_no_chat_id(make_cfg):
    cfg = make_cfg({"feishu.webhook_url": "https://open.feishu.cn/hook/x",
                    "feishu.default_chat_id": ""})
    sent = []
    feishu = FeishuClient(cfg, transport=lambda m, u, b, h: sent.append(u) or {"code": 0},
                          sleep=lambda s: None)
    result = feishu.push_report(receive_id=None, title="t", markdown="正文")
    assert result["ok"] is True
    assert result["channel"] == "webhook"
    assert sent == ["https://open.feishu.cn/hook/x"]


def test_receive_id_type_detection(client):
    feishu, transport = client
    feishu.send_text("ou_user", "hi")
    url = [c["url"] for c in transport.calls if "im/v1/messages" in c["url"]][0]
    assert "receive_id_type=open_id" in url
