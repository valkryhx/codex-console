"""
新版注册引擎测试 — 适配 DrissionPage BrowserClient FSM 架构
"""

from unittest.mock import MagicMock, patch
from dataclasses import fields

from src.core.register import RegistrationEngine, RegistrationResult
from src.core.http_client import BrowserClient, HTTPClient, OpenAIHTTPClient
from src.services.base import BaseEmailService
from src.config.constants import EmailServiceType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeEmailService(BaseEmailService):
    def __init__(self, otp_code="123456", fail_create=False):
        super().__init__(EmailServiceType.TEMPMAIL)
        self.otp_code = otp_code
        self.fail_create = fail_create

    def create_email(self, config=None):
        if self.fail_create:
            raise RuntimeError("邮箱服务不可用")
        return {"email": "tester@example.com", "service_id": "box-1"}

    def get_verification_code(self, email, email_id=None, timeout=120, pattern=None):
        return self.otp_code

    def list_emails(self, **kwargs):
        return []

    def delete_email(self, email_id):
        return True

    def check_health(self):
        return True


# ---------------------------------------------------------------------------
# RegistrationResult 单元测试
# ---------------------------------------------------------------------------

def test_registration_result_defaults():
    """logs 和 metadata 默认为空容器而非 None"""
    r = RegistrationResult(success=False)
    assert isinstance(r.logs, list)
    assert isinstance(r.metadata, dict)


def test_registration_result_to_dict_success():
    """to_dict() 包含所有必要字段"""
    r = RegistrationResult(
        success=True,
        email="a@b.com",
        password="pass123",
        session_token="sess-tok",
        access_token="acc-tok",
    )
    d = r.to_dict()
    assert d["success"] is True
    assert d["email"] == "a@b.com"
    assert d["session_token"] == "sess-tok"
    assert d["access_token"] == "acc-tok"
    assert "logs" in d
    assert "metadata" in d


def test_registration_result_to_dict_failed():
    """失败结果 to_dict() 仍正常序列化"""
    r = RegistrationResult(success=False, error_message="timeout")
    d = r.to_dict()
    assert d["success"] is False
    assert d["error_message"] == "timeout"


# ---------------------------------------------------------------------------
# BrowserClient 别名兼容测试
# ---------------------------------------------------------------------------

def test_http_client_aliases():
    """HTTPClient 和 OpenAIHTTPClient 是 BrowserClient 的别名"""
    assert HTTPClient is BrowserClient
    assert OpenAIHTTPClient is BrowserClient


def test_browser_client_init_no_proxy():
    """无代理时正常初始化，api_session 就绪"""
    client = BrowserClient()
    assert client.proxy_url is None
    assert client.api_session is not None
    client.api_session.close()


def test_browser_client_init_with_proxy():
    """代理 URL 被正确存储"""
    client = BrowserClient(proxy_url="http://127.0.0.1:7890")
    assert client.proxy_url == "http://127.0.0.1:7890"
    client.api_session.close()


# ---------------------------------------------------------------------------
# RegistrationEngine 初始化测试
# ---------------------------------------------------------------------------

def test_engine_init():
    """RegistrationEngine 正确绑定 email_service 和 BrowserClient"""
    svc = FakeEmailService()
    engine = RegistrationEngine(svc)
    assert engine.email_service is svc
    assert isinstance(engine.browser_client, BrowserClient)
    assert engine.logs == []


# ---------------------------------------------------------------------------
# run() 异常路径测试
# ---------------------------------------------------------------------------

def test_run_returns_failure_when_browser_init_raises():
    """浏览器初始化失败时，run() 返回 success=False 且含 error_message"""
    svc = FakeEmailService()
    engine = RegistrationEngine(svc)
    engine.browser_client = MagicMock()
    engine.browser_client.init_browser.side_effect = RuntimeError("浏览器启动失败")
    engine.browser_client.close = MagicMock()

    result = engine.run()

    assert result.success is False
    assert "浏览器启动失败" in result.error_message


def test_run_returns_failure_when_email_creation_raises():
    """邮箱服务异常时，run() 捕获并返回失败"""
    svc = FakeEmailService(fail_create=True)
    engine = RegistrationEngine(svc)
    engine.browser_client = MagicMock()
    engine.browser_client.init_browser.side_effect = Exception("不应被调用")
    engine.browser_client.close = MagicMock()

    result = engine.run()

    assert result.success is False
    # 邮箱创建抛出异常后，错误被捕获
    assert result.error_message != ""


def test_run_logs_are_populated_on_failure():
    """失败时 logs 列表有内容"""
    svc = FakeEmailService()
    engine = RegistrationEngine(svc)
    engine.browser_client = MagicMock()
    engine.browser_client.init_browser.side_effect = RuntimeError("崩溃")
    engine.browser_client.close = MagicMock()

    result = engine.run()

    assert len(result.logs) > 0


# ---------------------------------------------------------------------------
# save_to_database 测试
# ---------------------------------------------------------------------------

def test_save_to_database_skips_failed_result():
    """失败结果不写入数据库，直接返回 False"""
    svc = FakeEmailService()
    engine = RegistrationEngine(svc)
    result = RegistrationResult(success=False)
    assert engine.save_to_database(result) is False
