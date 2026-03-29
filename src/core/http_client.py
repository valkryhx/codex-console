"""
浏览器客户端封装 (Docker 强制脱离系统代理版)
解决 ERR_PROXY_CONNECTION_FAILED：通过强制命令行标志禁用系统设置
"""

import os
import sys
import signal
import logging
import time
import shutil
import re
import tempfile
from typing import Optional, Tuple
from dataclasses import dataclass
from urllib.parse import urlparse

from DrissionPage import ChromiumPage, ChromiumOptions
from curl_cffi import requests as cffi_requests

logger = logging.getLogger(__name__)

@dataclass
class RequestConfig:
    timeout: int = 30
    max_retries: int = 3
    retry_delay: float = 1.0
    impersonate: str = "chrome"
    verify_ssl: bool = True
    follow_redirects: bool = True

class HTTPClientError(Exception):
    pass

class BrowserClient:
    def __init__(self, proxy_url: Optional[str] = None, config: Optional[RequestConfig] = None):
        self.proxy_url = proxy_url.strip() if proxy_url else None
        self.config = config or RequestConfig()
        self.page: Optional[ChromiumPage] = None
        self._pid: Optional[int] = None
        self._user_data_path: Optional[str] = None

        # API 引擎 (Python 测试通过 403 证明链路 OK)
        proxies = {"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else None
        self.api_session = cffi_requests.Session(
            proxies=proxies,
            impersonate=self.config.impersonate,
            verify=self.config.verify_ssl,
            timeout=self.config.timeout
        )

    @property
    def session(self):
        return self.api_session

    def get(self, url: str, **kwargs):
        return self.api_session.get(url, **kwargs)

    def post(self, url: str, data=None, json=None, **kwargs):
        return self.api_session.post(url, data=data, json=json, **kwargs)

    def check_ip_location(self) -> Tuple[bool, Optional[str]]:
        try:
            response = self.api_session.get("https://cloudflare.com/cdn-cgi/trace", timeout=10)
            loc = re.search(r"loc=([A-Z]+)", response.text).group(1)
            return (False if loc in ["CN", "HK", "MO", "TW"] else True, loc)
        except Exception:
            return True, "Unknown"

    def init_browser(self) -> ChromiumPage:
        co = ChromiumOptions()
        
        # 1. 基础环境参数
        co.set_argument('--no-sandbox')
        co.set_argument('--disable-setuid-sandbox')
        co.set_argument('--disable-dev-shm-usage')
        co.set_argument('--disable-gpu')
        co.set_argument('--disable-blink-features=AutomationControlled')
        
        # 2. 【核心修复】：解决截图中的报错，强制禁用系统代理探测
        co.set_argument('--no-proxy-server')  # 禁用系统自带代理
        co.set_argument('--proxy-bypass-list=<-loopback>')
        co.set_argument('--password-store=basic') # 防止去寻找系统 Keyring
        
        # Linux 环境指定 chromium 路径；Windows 由 DrissionPage 自动探测
        if sys.platform != 'win32' and os.path.exists('/usr/bin/chromium'):
            co.set_browser_path('/usr/bin/chromium')
        co.headless(False)

        # 3. 分配独立 Profile 目录（跨平台）
        import uuid
        self._user_data_path = os.path.join(tempfile.gettempdir(), f"chrome_user_{int(time.time())}_{uuid.uuid4().hex[:8]}")
        co.set_user_data_path(self._user_data_path)

        if self.proxy_url:
            try:
                # 解析 URL：http://user:pass@host:port
                p = urlparse(self.proxy_url if "://" in self.proxy_url else f"http://{self.proxy_url}")
                
                # 再次强制通过命令行设置 server，并配合认证插件
                co.set_argument(f'--proxy-server={p.hostname}:{p.port}')
                
                proxy_settings = {
                    'server': f"{p.hostname}:{p.port}",
                    'username': p.username,
                    'password': p.password,
                }
                # DrissionPage 此时会生成一个专门处理该账密的 Extension
                co.set_proxy(proxy_settings)
                logger.info(f"代理注入指令下达 -> {p.hostname}:{p.port}")
            except Exception as e:
                logger.error(f"代理解析异常: {e}")
                co.set_proxy(self.proxy_url)

        try:
            self.page = ChromiumPage(addr_or_opts=co)
            return self.page
        except Exception as e:
            raise HTTPClientError(f"环境初始化失败: {e}")

    def close(self):
        if self.page:
            try:
                pid = self.page.process_id
                self.page.quit()
                if sys.platform == 'win32':
                    os.kill(pid, signal.SIGTERM)
                else:
                    os.kill(pid, signal.SIGKILL)
            except: pass
        
        if self._user_data_path and os.path.exists(self._user_data_path):
            try: shutil.rmtree(self._user_data_path, ignore_errors=True)
            except: pass

        if hasattr(self, 'api_session'):
            try: self.api_session.close()
            except: pass
            
        self.page = None

    def __enter__(self): return self
    def __exit__(self, exc_type, exc_val, exc_tb): self.close()

# 工厂函数
HTTPClient = BrowserClient
OpenAIHTTPClient = BrowserClient
def create_http_client(proxy_url=None, config=None, *args, **kwargs):
    return BrowserClient(proxy_url=proxy_url, config=config)
def create_openai_client(proxy_url=None, config=None, *args, **kwargs):
    return BrowserClient(proxy_url=proxy_url, config=config)