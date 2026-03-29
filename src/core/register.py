"""
注册流程引擎 (终极稳定闭环版)
1. 修复序列化异常：为 RegistrationResult 增加 to_dict() 方法，完美对接上游日志。
2. 规避循环依赖：对数据库层 (crud, get_db) 采用局部延迟导入。
3. 参数全量兼容：save_to_database 使用 **kwargs 动态吸收并透传扩展字段。
4. 物理防断连：采用后台静默新建标签页 (new_tab) 请求 API。
5. FSM 高阶探针：密码直通车 + 三连 TAB 盲打 + 1990-2000 固化年份。
"""

import time
import json
import logging
import secrets
import re
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from datetime import datetime

# 延迟导入数据库层以打破 Circular Import 循环依赖
import urllib.parse as _urlparse
from ..config.constants import (
    generate_random_user_info,
    OTP_CODE_PATTERN,
    DEFAULT_PASSWORD_LENGTH,
    PASSWORD_CHARSET,
    OAUTH_CLIENT_ID, OAUTH_REDIRECT_URI, OAUTH_SCOPE,
)
from .http_client import BrowserClient
from .openai.oauth import OAuthManager

logger = logging.getLogger(__name__)

@dataclass
class RegistrationResult:
    success: bool
    email: str = ""
    password: str = ""
    session_token: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    expired: str = ""
    error_message: str = ""
    logs: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """核心修复：提供给上游系统进行 JSON 序列化和 CPA 投递"""
        return {
            "success": self.success,
            "email": self.email,
            "password": self.password,
            "session_token": self.session_token,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "expired": self.expired,
            "error_message": self.error_message,
            "logs": self.logs,
            "metadata": self.metadata
        }

class RegistrationEngine:
    def __init__(self, email_service, proxy_url=None, callback_logger=None, task_uuid=None):
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.callback_logger = callback_logger or (lambda msg: logger.info(msg))
        self.task_uuid = task_uuid
        self.browser_client = BrowserClient(proxy_url=proxy_url)
        self.page = None
        self.email = None
        self.password = None
        self.logs = []
        
    def _log(self, message: str, level: str = "info"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        msg = f"[{timestamp}] {message}"
        self.logs.append(msg)
        if self.callback_logger: self.callback_logger(msg)
        if self.task_uuid:
            try:
                # 局部延迟导入
                from ..database import crud
                from ..database.session import get_db
                with get_db() as db:
                    crud.append_task_log(db, self.task_uuid, msg)
            except Exception: pass
        if level == "error": logger.error(message)
        elif level == "warning": logger.warning(message)

    def _smart_fill(self, selector: str, value: str, click_first: bool = False) -> bool:
        """底层焦点与输入注入 (适用于非掩码普通框)"""
        try:
            elements = self.page.eles(selector, timeout=8)
            target_ele = next((ele for ele in elements if ele.wait.displayed(timeout=2)), None)
            
            if not target_ele:
                return False

            if click_first:
                target_ele.click()
                time.sleep(0.3) 
            else:
                self.page.run_js('arguments[0].focus();', target_ele)
                time.sleep(0.2)
                target_ele.click()
            
            self.page.actions.key_down('CONTROL').type('a').key_up('CONTROL').type('\ue003')
            time.sleep(0.2)
            
            for char in str(value):
                self.page.actions.type(char)
                time.sleep(0.05) 
                
            if not target_ele.value or target_ele.value != str(value):
                self.page.run_js(f'arguments[0].value = "{value}";', target_ele)
                self.page.run_js('arguments[0].dispatchEvent(new Event("input", { bubbles: true }));', target_ele)
                self.page.run_js('arguments[0].dispatchEvent(new Event("change", { bubbles: true }));', target_ele)
            
            return True
        except Exception as e:
            self._log(f"注入异常: {e}", "error")
            return False

    def run(self) -> RegistrationResult:
        result = RegistrationResult(success=False, logs=self.logs)
        try:
            self._log("=" * 60)
            self._log("启动 FSM 自动化引擎 (终极防线版)")
            
            self.email_info = self.email_service.create_email()
            self.email = str(self.email_info["email"]).strip().lower()
            self.password = ''.join(secrets.choice(PASSWORD_CHARSET) for _ in range(DEFAULT_PASSWORD_LENGTH))
            
            self.page = self.browser_client.init_browser()

            # --- 阶段 1: 入口寻址 ---
            self.page.get('https://chatgpt.com/')
            time.sleep(5) 
            
            signup_btn = (
                self.page.ele('text=Sign up for free', timeout=3) or
                self.page.ele('text=Sign up', timeout=2) or
                self.page.ele('text=免费注册', timeout=2) or
                self.page.ele('text=注册', timeout=2)
            )
            if signup_btn:
                signup_btn.click()
                self._log(f"点击注册按钮: {signup_btn.text.strip()}")
            else:
                # 直接访问 OAuth 注册页（含 screen_hint=signup）
                import urllib.parse, secrets as _sec
                from ..config.constants import OAUTH_CLIENT_ID, OAUTH_REDIRECT_URI, OAUTH_SCOPE
                _state = _sec.token_urlsafe(16)
                _params = urllib.parse.urlencode({
                    'client_id': OAUTH_CLIENT_ID,
                    'redirect_uri': OAUTH_REDIRECT_URI,
                    'response_type': 'code',
                    'scope': OAUTH_SCOPE,
                    'screen_hint': 'signup',
                    'state': _state,
                })
                self.page.get(f'https://auth.openai.com/oauth/authorize?{_params}')
                self._log("未找到注册按钮，直接跳转 OAuth 注册入口")
            time.sleep(3)

            # --- 阶段 2: 邮箱网关 ---
            email_input = (
                self.page.ele('xpath=//input[@type="email"]', timeout=20) or
                self.page.ele('xpath=//input[@name="email" or @id="email-address" or @autocomplete="email"]', timeout=10)
            )
            if not email_input:
                self._log(f"网关加载超时，当前 URL: {self.page.url}", "error")
                result.error_message = "网关加载超时"
                return result
            email_input.input(self.email)
            time.sleep(0.5)
            self.page.ele('xpath=//button[@type="submit" and (normalize-space(.)="Continue" or normalize-space(.)="继续")]').click()
            
            # --- 阶段 3: 无状态探针循环 (FSM) ---
            for fsm_round in range(15):
                time.sleep(4)
                self._log(f"FSM 第{fsm_round+1}轮 | URL: {self.page.url[:80]}")
                
                if self.page.ele('text=Your session has ended', timeout=2) or \
                   self.page.ele('text=Don\'t have an account?', timeout=2):
                    self._log("捕获会话逃逸，执行强行拉回...")
                    signup_link = self.page.ele('xpath=//a[text()="Sign up"]', timeout=3)
                    if signup_link: signup_link.click()
                    continue

                pwd_input = self.page.ele('xpath=//input[@type="password" or @name="password"]', timeout=2)
                if pwd_input and pwd_input.wait.displayed(timeout=2):
                    self._log("进入[密码注入]状态...")
                    self._smart_fill('xpath=//input[@type="password" or @name="password"]', self.password, click_first=True)
                    time.sleep(1.5)
                    btn = self.page.ele('xpath=//button[@type="submit" and (normalize-space(.)="Continue" or normalize-space(.)="继续")]', timeout=4)
                    if btn: btn.click()
                    else: self.page.actions.key_down('ENTER').key_up('ENTER')
                    continue
                
                otp_input = self.page.ele('xpath=//input[@autocomplete="one-time-code" or contains(@class, "code")]', timeout=2)
                if self.page.ele('text=Check your inbox', timeout=2) or otp_input:
                    pwd_bypass_btn = self.page.ele('text=Continue with password', timeout=1)
                    if pwd_bypass_btn and pwd_bypass_btn.wait.displayed(timeout=1):
                        self._log("探测到密码直通车，执行物理击破规避 OTP 邮件消耗...")
                        try:
                            pwd_bypass_btn.click()
                        except:
                            self.page.run_js('arguments[0].click();', pwd_bypass_btn)
                        continue 

                    self._log("进入[OTP 捕获]状态...")
                    _email_id = self.email_info.get("service_id") if self.email_info else None
                    otp = self.email_service.get_verification_code(
                        email=self.email,
                        email_id=_email_id,
                        timeout=180,
                        pattern=OTP_CODE_PATTERN,
                    )
                    if otp:
                        self._smart_fill('xpath=//input[@autocomplete="one-time-code" or contains(@class, "code")]', otp, click_first=True)
                        time.sleep(0.5)
                        self.page.actions.key_down('ENTER').key_up('ENTER')
                    continue

                if self.page.ele('text=confirm your age', timeout=2) or self.page.ele('xpath=//input[@name="name"]', timeout=2):
                    self._log("进入[档案组装]状态...")
                    info = generate_random_user_info()

                    # 从 birthdate 字段解析日期，与旧代码逻辑保持一致
                    birth_parts = info['birthdate'].split('-')
                    safe_year  = birth_parts[0]           # e.g. "1987"
                    safe_month = birth_parts[1]           # e.g. "06"
                    safe_day   = birth_parts[2]           # e.g. "05" (保留前导零，供 YYYYMMDD 拼接)
                    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                    month_abbr = month_names[int(safe_month) - 1]
                    self._log(f"使用生日: {info['birthdate']} → 月={month_abbr} 日={safe_day} 年={safe_year}")

                    self._smart_fill('xpath=//input[@name="name" or @placeholder="Full name"]', info['name'], click_first=True)
                    time.sleep(0.5)

                    comboboxes = self.page.eles('xpath=//select | //button[@aria-haspopup="listbox"] | //button[@role="combobox"]')
                    self._log(f"探测到 {len(comboboxes)} 个下拉框")

                    if len(comboboxes) >= 3:
                        self._log("[三段式下拉] 直接按索引点击各框，规避 TAB 错位...")
                        # 第 0 个：月份
                        comboboxes[0].click()
                        time.sleep(0.3)
                        for char in month_abbr:
                            self.page.actions.type(char)
                            time.sleep(0.05)

                        # 第 1 个：日
                        comboboxes[1].click()
                        time.sleep(0.3)
                        for char in str(int(safe_day)):  # 下拉框去掉前导零
                            self.page.actions.type(char)
                            time.sleep(0.05)

                        # 第 2 个：年
                        comboboxes[2].click()
                        time.sleep(0.3)
                        for char in safe_year:
                            self.page.actions.type(char)
                            time.sleep(0.05)
                    else:
                        self._log("探测到 [纯净单输入框]，执行降级物理盲打...")
                        self.page.actions.key_down('TAB').key_up('TAB')
                        time.sleep(0.3)

                        age_input = self.page.ele('xpath=//input[@name="age" or @placeholder="Age"]', timeout=1)
                        # 单一日期输入框通常期望 MM/DD/YYYY 或 MMDDYYYY
                        # <input type="date"> 按 YYYYMMDD 无分隔符输入，浏览器自动按格式分段
                        fill_value = "25" if age_input else f"{safe_year}{safe_month}{safe_day}"

                        for char in fill_value:
                            self.page.actions.type(char)
                            time.sleep(0.15)
                            
                    time.sleep(1.5)
                    finish_btn = self.page.ele('text=Finish creating account', timeout=5)
                    if finish_btn: finish_btn.click()
                    else: self.page.actions.key_down('ENTER').key_up('ENTER')
                    break 

            # --- 阶段 4: 静默守候 ---
            self._log("档案已提交，等待 OAuth 重定向...")
            self.page.wait.url_change('openai.com', timeout=60)
            self._log(f"已进入主站域: {self.page.url}")

            # --- 阶段 5: 新手村推土机与定向跃迁扳机 ---
            self._log("启动新手引导拆除协议...")
            
            kill_list = [
                'text=Continue',
                'text=Skip Tour',
                'text=Skip',
                'text=Next',
                'text=Done',
            ]
            
            for _ in range(12): 
                time.sleep(1.5)
                
                lets_go_btn = self.page.ele('text=Okay, let’s go', timeout=0.5)
                if lets_go_btn and lets_go_btn.wait.displayed(timeout=0.5):
                    self._log("定位到最终确权网关 [Okay, let’s go]，执行物理击破...")
                    try:
                        lets_go_btn.click()
                    except:
                        self.page.run_js('arguments[0].click();', lets_go_btn)
                    
                    self._log("确权通过！强制休眠 3 秒等待后端 Cookie 固化...")
                    time.sleep(3)
                    break 
                
                if self.page.ele('xpath=//textarea[@id="prompt-textarea"]', timeout=1):
                    self._log("主渲染完毕，中断推土机...")
                    break

                for target in kill_list:
                    btn = self.page.ele(target, timeout=0.5)
                    if btn and btn.wait.displayed(timeout=0.5):
                        self._log(f"击碎常规引导: {target}")
                        try:
                            btn.click()
                        except:
                            self.page.run_js('arguments[0].click();', btn)
                        break 

            # --- 阶段 6: 终极武器 - 后台标签页隔离提取 ---
            self._log("跃迁协议启动：新建静默后台标签页提取 API 凭证...")
            full_session_token = ""
            access_token = ""
            extracted_metadata = {}
            api_tab = None

            try:
                # 独立开窗，隔离主进程，彻底规避 Disconnected 崩溃
                api_tab = self.page.new_tab('https://chatgpt.com/api/auth/session')
                time.sleep(3) 
                
                page_text = api_tab.ele('tag:body').text if api_tab.ele('tag:body') else api_tab.html
                
                start_idx = page_text.find('{')
                end_idx = page_text.rfind('}') + 1
                
                if start_idx != -1 and end_idx != 0:
                    json_str = page_text[start_idx:end_idx]
                    auth_data = json.loads(json_str)
                    
                    full_session_token = auth_data.get('sessionToken', '')
                    access_token = auth_data.get('accessToken', '')
                    
                    if full_session_token:
                        self._log("API 解析执行完毕，成功捕获高权限凭证")
                        user_info = auth_data.get("user", {})
                        acc_info = auth_data.get("account", {})
                        extracted_metadata = {
                            "user_id": user_info.get("id", ""),
                            "email_verified": user_info.get("email_verified", False),
                            "plan_type": acc_info.get("planType", "free"),
                            "expires": auth_data.get("expires", ""),
                            "method": "api_json_parse"
                        }
            except Exception as e:
                self._log(f"后台标签页 API 解析链中断: {e}", "warning")
            finally:
                if api_tab:
                    try: api_tab.close()
                    except: pass

            if not full_session_token:
                self._log("激活后备机制：基于本地持久化 Cookie 进行逆向组装...")
                raw_cookies = self.page.cookies()
                cookies_dict = {c['name']: c['value'] for c in raw_cookies}
                
                token_parts = []
                if '__Secure-next-auth.session-token' in cookies_dict:
                    token_parts.append(cookies_dict['__Secure-next-auth.session-token'])
                    
                chunks = [k for k in cookies_dict.keys() if '__Secure-next-auth.session-token.' in k]
                if chunks:
                    chunks.sort(key=lambda x: int(x.split('.')[-1]))
                    for k in chunks:
                        token_parts.append(cookies_dict[k])
                        
                full_session_token = "".join(token_parts)
                extracted_metadata = {"method": "cookie_assembly_fallback"}

            # --- 阶段 6.5: PKCE OAuth 登录补全 refresh_token/id_token ---
            # 注册完成后用已有邮箱+密码走一次 OAuth 登录，换取全套 codex token
            try:
                _oauth_mgr = OAuthManager(proxy_url=self.proxy_url)
                _oauth_start = _oauth_mgr.start_oauth()
                # 去掉 prompt=login，让服务端用默认行为；不加 screen_hint=signup
                _login_url = _oauth_start.auth_url.replace('&prompt=login', '')
                self._log('PKCE OAuth 登录补全启动...')
                self.page.get(_login_url)
                _cb_url = ''
                for _r in range(15):
                    time.sleep(3)
                    _cur = self.page.url
                    if 'localhost' in _cur or '1455' in _cur:
                        _cb_url = _cur
                        self._log(f'PKCE 回调捕获: {_cur[:120]}')
                        break
                    # 填邮箱
                    _ei = self.page.ele('xpath=//input[@type="email"]', timeout=1)
                    if _ei and _ei.wait.displayed(timeout=1):
                        _ei.input(self.email)
                        time.sleep(0.5)
                        _btn = self.page.ele('xpath=//button[@type="submit"]', timeout=2)
                        if _btn: _btn.click()
                        continue
                    # 填密码
                    _pi = self.page.ele('xpath=//input[@type="password"]', timeout=1)
                    if _pi and _pi.wait.displayed(timeout=1):
                        self._smart_fill('xpath=//input[@type="password"]', self.password, click_first=True)
                        time.sleep(1)
                        _btn = self.page.ele('xpath=//button[@type="submit"]', timeout=2)
                        if _btn: _btn.click()
                        continue
                    # 处理邮箱 OTP（登录时可能触发）
                    if self.page.ele('text=Check your inbox', timeout=1):
                        _eid = self.email_info.get('service_id') if self.email_info else None
                        _otp = self.email_service.get_verification_code(
                            email=self.email, email_id=_eid, timeout=120, pattern=OTP_CODE_PATTERN
                        )
                        if _otp:
                            self._smart_fill('xpath=//input[@autocomplete="one-time-code"]', _otp, click_first=True)
                            time.sleep(0.5)
                            self.page.actions.key_down('ENTER').key_up('ENTER')
                        continue
                if _cb_url and 'error=' not in _cb_url:
                    _token_info = _oauth_mgr.handle_callback(
                        callback_url=_cb_url,
                        expected_state=_oauth_start.state,
                        code_verifier=_oauth_start.code_verifier,
                    )
                    result.refresh_token = _token_info.get('refresh_token', '')
                    result.id_token = _token_info.get('id_token', '')
                    result.expired = _token_info.get('expired', '')
                    if _token_info.get('access_token'):
                        result.access_token = _token_info['access_token']
                    self._log(f"PKCE 补全完成: refresh={'有' if result.refresh_token else '无'} id_token={'有' if result.id_token else '无'}")
                else:
                    self._log(f'PKCE 登录补全未拿到有效回调: {_cb_url[:80] if _cb_url else "超时"}', 'warning')
            except Exception as _e:
                self._log(f'PKCE 登录补全异常（不影响主流程）: {_e}', 'warning')

            # --- 阶段 7: 数据固化返回 ---
            if full_session_token:
                result.success = True
                result.email = self.email
                result.password = self.password
                result.session_token = full_session_token
                result.access_token = result.access_token or access_token
                result.metadata = extracted_metadata
                self._log("任务节点通过，全链路数据注入完毕。")
            else:
                result.error_message = "凭证收割失败"
                self._log("无法提取有效的 Session Token", "error")

            return result

        except Exception as e:
            self._log(f"内核总线异常: {e}", "error")
            result.error_message = str(e)
            return result
        finally:
            self.browser_client.close()

    def save_to_database(self, result: RegistrationResult, **kwargs) -> bool:
        if not result.success: return False
        
        # 局部延迟导入，避免启动时循环依赖引发 ModuleNotFoundError
        from ..database import crud
        from ..database.session import get_db
        
        with get_db() as db:
            email_svc = kwargs.pop('email_service', None)
            if not email_svc and hasattr(self, 'email_info') and self.email_info:
                email_svc = self.email_info.get("service_type", "unknown")
                
            account_id = result.metadata.get("user_id") if result.metadata else None

            crud.create_account(
                db=db,
                email=result.email,
                email_service=email_svc or 'unknown',
                password=result.password,
                session_token=result.session_token,
                access_token=result.access_token,
                refresh_token=result.refresh_token,
                id_token=result.id_token,
                account_id=account_id,
                extra_data=result.metadata,
                proxy_used=self.proxy_url,
                **kwargs
            )
            return True