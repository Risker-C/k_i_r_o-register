"""
RoxyBrowser 指纹浏览器注册模块
通过 RoxyBrowser 本地 API 创建/管理指纹浏览器窗口，使用 Playwright CDP 连接进行自动注册。
相比 Playwright + Stealth 方案，指纹浏览器的 TES 通过率更高。
"""
import asyncio
import json
import time
import secrets
import hashlib
import base64
import socket
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests as _requests

from kiro_register import (
    _generate_password, _generate_name, _human_delay, _move_to_element,
    _click_submit, _dismiss_cookie, _human_type, _b64url, _sha1_hash,
    persist_tokens, inject_machine_ids, skip_onboarding,
    REG_OIDC, REG_SCOPES, REG_REDIRECT_URI, KIRO_SIGNIN_URL, ISSUER_URL,
)

import random as _random


class RoxyBrowser:
    """RoxyBrowser 本地 API 客户端"""

    def __init__(self, api_key: str, port: int = 50000):
        self.base_url = f"http://127.0.0.1:{port}"
        self.headers = {
            "Content-Type": "application/json",
            "token": api_key,
        }

    def health(self) -> bool:
        try:
            r = _requests.get(f"{self.base_url}/health", headers=self.headers, timeout=5)
            return r.status_code == 200 and r.json().get("code") == 0
        except Exception:
            return False

    def list_workspaces(self) -> list:
        r = _requests.get(f"{self.base_url}/browser/workspace", headers=self.headers, timeout=10)
        data = r.json()
        if data.get("code") == 0:
            return data.get("data", {}).get("rows", []) or data.get("data", {}).get("list", [])
        return []

    def list_windows(self, workspace_id: int) -> list:
        r = _requests.get(
            f"{self.base_url}/browser/list?workspaceId={workspace_id}",
            headers=self.headers, timeout=10
        )
        data = r.json()
        if data.get("code") == 0:
            return data.get("data", {}).get("rows", []) or data.get("data", {}).get("list", [])
        return []

    def create_window(self, workspace_id: int, name: str = "", proxy_info: dict = None) -> str | None:
        payload = {
            "workspaceId": workspace_id,
            "windowName": name or f"kiro_reg_{int(time.time())}",
            "coreVersion": "135",
            "os": "Windows",
            "fingerInfo": {
                "randomFingerprint": True,
                "canvas": True,
                "audioContext": True,
                "webGL": True,
                "webGLInfo": True,
                "clientRects": True,
                "deviceInfo": True,
                "deviceNameSwitch": True,
                "macInfo": True,
                "doNotTrack": True,
                "portScanProtect": True,
                "webRTC": 2,
                "webGpu": "webgl",
                "isLanguageBaseIp": True,
                "isTimeZone": True,
                "isPositionBaseIp": True,
                "position": 1,
                "openBattery": True,
                "clearCacheFile": True,
                "clearCookie": True,
                "clearLocalStorage": True,
                "syncCookie": True,
                "syncPassword": True,
                "syncTab": True,
            },
        }
        if proxy_info:
            payload["proxyInfo"] = proxy_info
        r = _requests.post(f"{self.base_url}/browser/create", headers=self.headers, json=payload, timeout=15)
        data = r.json()
        if data.get("code") == 0:
            return data.get("data", {}).get("dirId")
        return None

    def open_window(self, workspace_id: int, dir_id: str, headless: bool = False) -> dict | None:
        payload = {
            "workspaceId": workspace_id,
            "dirId": dir_id,
            "headless": headless,
        }
        r = _requests.post(f"{self.base_url}/browser/open", headers=self.headers, json=payload, timeout=60)
        data = r.json()
        if data.get("code") == 0:
            return data.get("data", {})
        return None

    def close_window(self, dir_id: str) -> bool:
        r = _requests.post(
            f"{self.base_url}/browser/close", headers=self.headers,
            json={"dirId": dir_id}, timeout=10
        )
        return r.json().get("code") == 0

    def delete_window(self, workspace_id: int, dir_id: str) -> bool:
        r = _requests.post(
            f"{self.base_url}/browser/delete", headers=self.headers,
            json={"workspaceId": workspace_id, "dirIds": [dir_id]}, timeout=10
        )
        return r.json().get("code") == 0

    def randomize_fingerprint(self, workspace_id: int, dir_id: str) -> bool:
        r = _requests.post(
            f"{self.base_url}/browser/random_env", headers=self.headers,
            json={"workspaceId": workspace_id, "dirId": dir_id}, timeout=10
        )
        return r.json().get("code") == 0


async def register_with_roxy(
    api_key: str = "",
    port: int = 50000,
    headless: bool = False,
    auto_login: bool = True,
    skip_onboard: bool = True,
    mail_url: str = None,
    mail_key: str = None,
    mail_domain_id=None,
    mail_provider_instance=None,
    proxy_info: dict = None,
    delete_after: bool = True,
    log=print,
    cancel_check=None,
):
    """
    使用 RoxyBrowser 指纹浏览器完成 Kiro 自动注册。

    Args:
        api_key: RoxyBrowser API 密钥
        port: RoxyBrowser 本地 API 端口
        headless: 是否无头模式打开浏览器窗口
        auto_login: 注册完成后是否注入本地 token
        skip_onboard: 是否跳过 onboarding
        mail_url/mail_key/mail_domain_id: 邮件服务配置
        mail_provider_instance: 已创建的邮件提供商实例
        proxy_info: 代理配置 {"type": "socks5", "host": "...", "port": 1080, ...}
        delete_after: 注册完成后是否删除浏览器窗口
        log: 日志回调
        cancel_check: 取消检查回调

    Returns:
        dict with account info or None
    """
    from curl_cffi import requests as curl_requests
    from playwright.async_api import async_playwright

    if cancel_check and cancel_check():
        return None

    # ─── 初始化 RoxyBrowser ─────────────────────────────────────────────
    roxy = RoxyBrowser(api_key, port)
    if not roxy.health():
        log("RoxyBrowser 未运行或 API 不可达!", "err")
        return None

    log("RoxyBrowser 连接成功", "ok")

    workspaces = roxy.list_workspaces()
    if not workspaces:
        log("未找到工作空间，请先在 RoxyBrowser 中创建", "err")
        return None
    workspace_id = workspaces[0].get("id") or workspaces[0].get("workspaceId")
    ws_name = workspaces[0].get("workspaceName") or workspaces[0].get("name", str(workspace_id))
    log(f"使用工作空间: {ws_name} (id={workspace_id})")

    # 优先复用已有的关闭状态窗口，避免每次创建新窗口
    created_new = False
    dir_id = None
    existing_windows = roxy.list_windows(workspace_id)
    closed_windows = [w for w in existing_windows if w.get("openStatus") == 0]
    if closed_windows:
        chosen = _random.choice(closed_windows)
        dir_id = chosen.get("dirId")
        log(f"复用已有窗口: {chosen.get('windowName', '')} ({dir_id[:16]}...)")
        # 随机化指纹
        roxy.randomize_fingerprint(workspace_id, dir_id)
        log("已随机化指纹", "ok")
    else:
        # 没有可用窗口，创建新的
        window_name = f"kiro_reg_{int(time.time())}"
        dir_id = roxy.create_window(workspace_id, window_name, proxy_info=proxy_info)
        if not dir_id:
            log("创建浏览器窗口失败!", "err")
            return None
        created_new = True
        log(f"浏览器窗口已创建: {dir_id[:16]}...")

    # 打开窗口获取 CDP WebSocket URL
    open_data = roxy.open_window(workspace_id, dir_id, headless=headless)
    if not open_data:
        log("打开浏览器窗口失败!", "err")
        if created_new:
            roxy.delete_window(workspace_id, dir_id)
        return None

    ws_url = open_data.get("ws") or open_data.get("webSocketDebuggerUrl") or open_data.get("wsEndpoint")
    if not ws_url:
        log(f"未获取到 WebSocket URL! 返回数据: {open_data}", "err")
        roxy.close_window(dir_id)
        if created_new:
            roxy.delete_window(workspace_id, dir_id)
        return None
    log(f"CDP 连接: {ws_url[:60]}...", "ok")

    # ─── 准备注册信息 ───────────────────────────────────────────────────
    from kiro_register import ShiroMailClient
    s = curl_requests.Session(impersonate="chrome131")

    if mail_provider_instance:
        mail = mail_provider_instance
    else:
        mail = ShiroMailClient(base_url=mail_url, api_key=mail_key, domain_id=mail_domain_id)
    email = mail.create_mailbox()
    password = _generate_password()
    full_name = _generate_name()
    log(f"邮箱: {email}", "ok")
    log(f"密码: {password[:4]}****")
    log(f"姓名: {full_name}")

    def _partial_result(reason="unknown"):
        return {
            "email": email,
            "password": password,
            "full_name": full_name,
            "provider": "BuilderId",
            "authMethod": "IdC",
            "region": "us-east-1",
            "accessToken": "",
            "refreshToken": "",
            "incomplete": True,
            "failReason": reason,
            "browser": "RoxyBrowser",
        }

    def _cleanup():
        try:
            roxy.close_window(dir_id)
        except Exception:
            pass
        if delete_after and created_new:
            try:
                roxy.delete_window(workspace_id, dir_id)
            except Exception:
                pass

    # ─── Phase 1: OIDC 客户端注册 ──────────────────────────────────────
    log("阶段 1: OIDC 客户端注册")
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _b64url(hashlib.sha256(code_verifier.encode()).digest())
    state_val = secrets.token_urlsafe(32)

    reg_resp = s.post(f"{REG_OIDC}/client/register", json={
        "clientName": "Kiro IDE", "clientType": "public",
        "grantTypes": ["authorization_code", "refresh_token"],
        "issuerUrl": ISSUER_URL,
        "redirectUris": [REG_REDIRECT_URI], "scopes": REG_SCOPES,
    }, timeout=25, verify=False)
    reg = reg_resp.json()
    if "clientId" not in reg:
        log(f"OIDC 注册失败: {reg}", "err")
        _cleanup()
        return _partial_result("OIDC注册失败")
    client_id = reg["clientId"]
    client_secret = reg["clientSecret"]
    log("OIDC 客户端注册成功", "ok")

    signin_url = f"{KIRO_SIGNIN_URL}?" + urlencode({
        "state": state_val,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "redirect_uri": REG_REDIRECT_URI,
        "redirect_from": "KiroIDE",
    })

    # ─── Phase 2: 本地回调服务器 + CDP 连接 ─────────────────────────────
    log("阶段 2: 启动 CDP 浏览器连接")
    authorization_code = ""

    class CallbackHandler(BaseHTTPRequestHandler):
        signin_callback_params = {}

        def do_GET(self_h):
            nonlocal authorization_code
            parsed = urlparse(self_h.path)
            qs = parse_qs(parsed.query)
            code = qs.get("code", [""])[0]
            if code:
                authorization_code = code
                log("已收到授权回调", "ok")
                self_h.send_response(200)
                self_h.send_header("Content-Type", "text/html")
                self_h.end_headers()
                self_h.wfile.write(b"<html><body><h2>Registration complete!</h2></body></html>")
            elif "signin/callback" in parsed.path or qs.get("login_option"):
                CallbackHandler.signin_callback_params = {k: v[0] for k, v in qs.items()}
                log("收到登录回调", "ok")
                self_h.send_response(200)
                self_h.send_header("Content-Type", "text/html")
                self_h.end_headers()
                self_h.wfile.write(b"<html><body><p>Redirecting...</p></body></html>")
            else:
                self_h.send_response(200)
                self_h.send_header("Content-Type", "text/html")
                self_h.end_headers()
                self_h.wfile.write(b"<html><body><p>OK</p></body></html>")

        def log_message(self_h, *args):
            pass

    # 确保端口可用 - 强制清理
    import os, subprocess
    for _kill_attempt in range(3):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", 3128))
            sock.close()
            break
        except OSError:
            sock.close()
            try:
                r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True)
                for line in r.stdout.splitlines():
                    if ":3128" in line and "LISTENING" in line:
                        pid = line.strip().split()[-1]
                        if pid.isdigit() and int(pid) != os.getpid():
                            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                            log(f"已终止占用端口 3128 的进程 (PID={pid})", "warn")
            except Exception:
                pass
            await asyncio.sleep(1.5)
    else:
        log("端口 3128 无法释放!", "err")
        _cleanup()
        return _partial_result("端口3128被占用")

    try:
        callback_server = HTTPServer(("127.0.0.1", 3128), CallbackHandler)
        callback_server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError as e:
        log(f"回调服务器启动失败: {e}", "err")
        _cleanup()
        return _partial_result("回调服务器启动失败")
    srv_thread = threading.Thread(target=callback_server.serve_forever, daemon=True)
    srv_thread.start()

    # 验证服务器确实在监听
    await asyncio.sleep(0.3)
    try:
        _check_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _check_sock.settimeout(2)
        _check_sock.connect(("127.0.0.1", 3128))
        _check_sock.close()
        log("本地回调服务器已启动 (127.0.0.1:3128)", "ok")
    except Exception:
        log("回调服务器启动后无法连接!", "err")
        callback_server.shutdown()
        _cleanup()
        return _partial_result("回调服务器不可达")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(ws_url)
            contexts = browser.contexts
            if contexts:
                context = contexts[0]
            else:
                context = await browser.new_context()
            pages = context.pages
            if pages:
                page = pages[0]
            else:
                page = await context.new_page()

            log("CDP 浏览器已连接", "ok")

            # 拦截 profile.aws API 响应用于调试
            async def _on_profile_response(response):
                url = response.url
                if "profile.aws" in url and "/api/" in url:
                    try:
                        body = await response.text()
                        endpoint = url.split("/api/")[-1]
                        log(f"[API] {endpoint} → {response.status} {body[:150]}", "dbg")
                    except Exception:
                        pass
            page.on("response", _on_profile_response)

            # PLACEHOLDER_CONTINUE_2

            await page.goto(signin_url, timeout=60000)
            await page.wait_for_load_state("networkidle", timeout=30000)
            await asyncio.sleep(3)
            await _dismiss_cookie(page)

            # 点击 AWS Builder ID 按钮
            if "app.kiro.dev" in page.url:
                log("正在选择登录方式...")
                await asyncio.sleep(2)
                signin_clicked = False
                for sel in [
                    'xpath=//*[@id="layout-viewport"]/div/div/div/div[2]/div/div[1]/button[3]',
                    'xpath=//button[contains(text(),"AWS Builder ID")]',
                    'xpath=//button[contains(text(),"Builder ID")]',
                    'xpath=//button[contains(text(),"Sign in")]',
                    'xpath=//button[contains(text(),"Continue")]',
                ]:
                    loc = page.locator(sel)
                    try:
                        if await loc.count() > 0 and await loc.first.is_visible():
                            await loc.first.click()
                            signin_clicked = True
                            log("已点击登录按钮", "ok")
                            break
                    except Exception:
                        pass

                if signin_clicked:
                    await asyncio.sleep(3)
                    if not CallbackHandler.signin_callback_params:
                        try:
                            await page.evaluate("""() => {
                                const btn = document.querySelector('#layout-viewport button:nth-child(3)') ||
                                            document.querySelectorAll('#layout-viewport button')[2];
                                if (btn) btn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                            }""")
                        except Exception:
                            pass
                        await asyncio.sleep(3)

                for _ in range(20):
                    if CallbackHandler.signin_callback_params:
                        break
                    await asyncio.sleep(1)

            # 构造 OIDC authorize URL
            if CallbackHandler.signin_callback_params and not authorization_code:
                log("正在跳转到授权页面...")
                authorize_url = f"{REG_OIDC}/authorize?" + urlencode({
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": REG_REDIRECT_URI,
                    "scopes": ",".join(REG_SCOPES),
                    "state": state_val,
                    "code_challenge": code_challenge,
                    "code_challenge_method": "S256",
                })
                await page.goto(authorize_url, timeout=60000)
                await page.wait_for_load_state("networkidle", timeout=30000)
                await asyncio.sleep(3)

            # 等待到达 signin.aws 或 profile.aws
            for _ in range(10):
                if "signin.aws" in page.url or "profile.aws" in page.url:
                    break
                await asyncio.sleep(2)
            await asyncio.sleep(2)
            log("已到达注册页面", "ok")

            # 如果在 signin.aws，输入邮箱
            if "signin.aws" in page.url:
                email_input = page.locator('xpath=//input[@type="email"]')
                if await email_input.count() == 0:
                    email_input = page.locator('xpath=//input[@type="text"]').first
                await _move_to_element(page, email_input)
                await _human_type(page, email_input, email)
                await _human_delay(0.8, 1.5)
                log(f"邮箱已填入: {email}")
                await _click_submit(page)
                await page.wait_for_load_state("networkidle")
                await _human_delay(2, 4)

            # 等待 profile.aws
            for _ in range(15):
                if "profile.aws" in page.url:
                    break
                await asyncio.sleep(2)
            await asyncio.sleep(2)

            if "profile.aws" not in page.url:
                log(f"未能到达注册页面 (当前: {page.url})", "err")
                await browser.close()
                callback_server.shutdown()
                _cleanup()
                return _partial_result("未到达注册页面")

            # PLACEHOLDER_CONTINUE_3

            # 预热行为
            try:
                vp = page.viewport_size or {"width": 1280, "height": 800}
                for _ in range(3):
                    await page.mouse.move(
                        _random.randint(100, vp["width"] - 100),
                        _random.randint(100, vp["height"] - 100),
                        steps=_random.randint(10, 25)
                    )
                    await asyncio.sleep(_random.uniform(0.3, 0.8))
                await page.mouse.wheel(0, _random.randint(50, 150))
                await asyncio.sleep(_random.uniform(0.5, 1.0))
                await page.mouse.wheel(0, -_random.randint(30, 80))
                await asyncio.sleep(_random.uniform(0.3, 0.6))
            except Exception:
                pass

            # ─── 状态机 ───────────────────────────────────────────────
            async def detect_state():
                if authorization_code:
                    return "DONE"
                url = page.url
                if "127.0.0.1:3128" in url or "localhost:3128" in url:
                    return "CALLBACK"
                try:
                    result = await page.evaluate("""() => {
                        const url = location.href;
                        const pwds = document.querySelectorAll('input[type="password"]');
                        const visiblePwds = Array.from(pwds).filter(e => e.offsetWidth > 0);
                        const nameInput = document.querySelector('input[placeholder*="Silva"]');
                        const otpInput = document.querySelector('input[inputmode="numeric"]') ||
                                         document.querySelector('input[autocomplete="one-time-code"]') ||
                                         document.querySelector('input[name*="otp"]') ||
                                         document.querySelector('input[name*="code"]') ||
                                         document.querySelector('input[placeholder*="6-digit"]') ||
                                         document.querySelector('input[placeholder*="digit"]');
                        const emailInput = document.querySelector('input[type="email"]');
                        const urlHasOtp = url.includes('verify-otp') || url.includes('otp');
                        const buttons = Array.from(document.querySelectorAll('button'));
                        const visibleBtns = buttons.filter(b => b.offsetWidth > 0 && b.offsetHeight > 0);
                        const hasConsentBtn = visibleBtns.some(b => {
                            const t = (b.innerText || '').toLowerCase();
                            return t.includes('allow') || t.includes('authorize') ||
                                   t.includes('accept') || t.includes('confirm');
                        });
                        const hasAnyInput = document.querySelectorAll('input:not([type="hidden"])').length > 0;
                        const hasAnyButton = visibleBtns.length > 0;
                        const isLoading = !hasAnyInput && !hasAnyButton;
                        return {
                            url, visiblePwdCount: visiblePwds.length,
                            hasName: !!(nameInput && nameInput.offsetWidth > 0),
                            hasOtp: !!(otpInput && otpInput.offsetWidth > 0),
                            hasEmail: !!(emailInput && emailInput.offsetWidth > 0),
                            urlHasOtp, hasConsentBtn, isLoading,
                        };
                    }""")
                except Exception:
                    return "UNKNOWN"
                if "chrome-error" in result["url"]:
                    return "CALLBACK"
                if result["visiblePwdCount"] >= 1:
                    return "PASSWORD"
                if result["hasOtp"] or (result["urlHasOtp"] and not result["isLoading"]):
                    return "OTP"
                if result["hasName"]:
                    return "NAME"
                if result["hasEmail"]:
                    return "EMAIL"
                if "awsapps.com" in result["url"] and result["hasConsentBtn"]:
                    return "CONSENT"
                if "profile.aws" in result["url"] and not result["isLoading"]:
                    return "OTP"
                if result["isLoading"]:
                    return "LOADING"
                return "UNKNOWN"

            async def wait_for_state(target_states, timeout=60):
                deadline = time.time() + timeout
                while time.time() < deadline:
                    st = await detect_state()
                    if st in target_states or st == "DONE":
                        return st
                    if st == "CALLBACK":
                        return st
                    await asyncio.sleep(1.5)
                return await detect_state()

            # ─── Phase 3: 姓名填写 ────────────────────────────────────
            log("阶段 3: 填写注册表单")
            await asyncio.sleep(2)
            await _dismiss_cookie(page)

            state = await wait_for_state(["NAME", "OTP", "PASSWORD", "CONSENT", "DONE"], timeout=30)
            if state == "NAME":
                name_field = page.locator('xpath=//input[contains(@placeholder,"Silva")]')
                for attempt in range(3):
                    try:
                        await _move_to_element(page, name_field.first)
                        await _human_type(page, name_field.first, full_name)
                        await _human_delay(0.5, 1.0)
                        filled_val = await name_field.first.input_value()
                        if filled_val == full_name:
                            log(f"姓名已填入: '{full_name}'", "ok")
                            break
                    except Exception:
                        await asyncio.sleep(1)
                for attempt in range(3):
                    clicked = False
                    try:
                        for sel in [
                            'xpath=//form//button[@type="submit"]',
                            'xpath=//button[contains(text(),"Continue")]',
                            'xpath=//button[@type="submit"]',
                        ]:
                            btn = page.locator(sel)
                            if await btn.count() > 0 and await btn.first.is_visible():
                                await btn.first.click()
                                clicked = True
                                break
                        if not clicked:
                            await page.keyboard.press("Enter")
                    except Exception:
                        pass
                    await asyncio.sleep(4)
                    new_state = await detect_state()
                    if new_state != "NAME":
                        log("姓名已提交", "ok")
                        break
                state = await detect_state()

            # PLACEHOLDER_CONTINUE_4

            # ─── Phase 4: OTP 验证 ────────────────────────────────────
            if state not in ["DONE", "PASSWORD", "CONSENT", "CALLBACK"]:
                state = await wait_for_state(["OTP", "PASSWORD", "CONSENT", "DONE"], timeout=30)

            if state == "OTP":
                log("阶段 4: OTP 验证")
                await asyncio.sleep(3)
                otp_selectors = [
                    'xpath=//input[@inputmode="numeric"]',
                    'xpath=//input[@autocomplete="one-time-code"]',
                    'xpath=//input[contains(@placeholder,"6-digit") or contains(@placeholder,"digit")]',
                    'xpath=//input[contains(@name,"otp") or contains(@name,"code") or contains(@name,"verif")]',
                    'xpath=//input[contains(@id,"otp") or contains(@id,"code") or contains(@id,"verif")]',
                    'xpath=//input[contains(@placeholder,"code") or contains(@placeholder,"Code")]',
                    'xpath=//input[contains(@aria-label,"code") or contains(@aria-label,"verif")]',
                    'xpath=//input[contains(@class,"verification") or contains(@class,"otp")]',
                    'css=input[data-testid*="code"]',
                    'css=input[data-testid*="otp"]',
                    'css=input[data-testid*="verif"]',
                ]
                otp_input = None
                for retry in range(3):
                    for sel in otp_selectors:
                        loc = page.locator(sel)
                        if await loc.count() > 0 and await loc.first.is_visible():
                            otp_input = loc.first
                            break
                    if otp_input:
                        break
                    all_inp = page.locator('xpath=//input[not(@type="hidden") and not(@type="password") and not(@type="email")]')
                    for i in range(await all_inp.count()):
                        inp = all_inp.nth(i)
                        if await inp.is_visible():
                            inp_type = await inp.get_attribute("type") or "text"
                            if inp_type in ("text", "tel", "number", ""):
                                otp_input = inp
                                break
                    if otp_input:
                        break
                    if retry < 2:
                        log(f"OTP 输入框未就绪，等待重试 ({retry+1}/3)...")
                        await asyncio.sleep(2)

                if not otp_input:
                    log("未找到 OTP 输入框!", "err")
                    await browser.close()
                    callback_server.shutdown()
                    _cleanup()
                    return _partial_result("OTP输入框未找到")

                log(f"已找到 OTP 输入框, 等待验证码...", "ok")
                otp_code = mail.wait_otp(timeout=90, poll_interval=3)
                if not otp_code:
                    log("OTP 等待超时!", "err")
                    await browser.close()
                    callback_server.shutdown()
                    _cleanup()
                    return _partial_result("OTP超时")

                log(f"获取到验证码: {otp_code}", "ok")
                await _human_delay(2, 4)
                try:
                    vp = page.viewport_size or {"width": 1280, "height": 800}
                    await page.mouse.move(
                        vp["width"] * _random.uniform(0.3, 0.7),
                        vp["height"] * _random.uniform(0.3, 0.5),
                        steps=_random.randint(8, 20)
                    )
                    await asyncio.sleep(_random.uniform(0.3, 0.8))
                except Exception:
                    pass
                await _move_to_element(page, otp_input)
                await otp_input.click()
                await asyncio.sleep(_random.uniform(0.3, 0.6))
                for ch in otp_code:
                    await page.keyboard.type(ch, delay=0)
                    await asyncio.sleep(_random.uniform(0.05, 0.15))
                await _human_delay(0.8, 1.5)

                # 提交 OTP，TES 重试
                for attempt in range(5):
                    try:
                        submit_btn = page.locator('xpath=//form//button[@type="submit"]')
                        if await submit_btn.count() > 0 and await submit_btn.first.is_visible():
                            await _move_to_element(page, submit_btn.first)
                            await asyncio.sleep(_random.uniform(0.2, 0.5))
                            await submit_btn.first.click()
                        else:
                            await page.keyboard.press("Enter")
                    except Exception:
                        pass
                    log(f"验证码已提交 ({attempt+1}/5)")
                    wait_time = 3 + attempt * 2
                    await asyncio.sleep(wait_time)
                    new_state = await detect_state()
                    if new_state != "OTP":
                        log("OTP 验证通过", "ok")
                        state = new_state
                        break
                    try:
                        error_text = await page.evaluate("""() => {
                            const alerts = document.querySelectorAll('[role="alert"], [class*="error"], [class*="Error"]');
                            for (const el of alerts) {
                                const t = el.innerText.trim();
                                if (t && t.length > 3) return t;
                            }
                            return '';
                        }""")
                        if error_text:
                            log(f"TES 拦截 ({attempt+1}/5), 重新模拟输入...", "warn")
                            await page.mouse.move(
                                _random.randint(200, 800), _random.randint(200, 500),
                                steps=_random.randint(8, 15)
                            )
                            await _human_delay(1.5, 3.0)
                            await _move_to_element(page, otp_input)
                            await otp_input.click()
                            await asyncio.sleep(_random.uniform(0.2, 0.4))
                            await page.keyboard.press("Control+a")
                            await asyncio.sleep(_random.uniform(0.1, 0.3))
                            await page.keyboard.press("Backspace")
                            await asyncio.sleep(_random.uniform(0.3, 0.6))
                            for ch in otp_code:
                                await page.keyboard.type(ch, delay=0)
                                await asyncio.sleep(_random.uniform(0.06, 0.18))
                            await _human_delay(0.8, 1.5)
                    except Exception:
                        pass

            # PLACEHOLDER_CONTINUE_5

            # ─── Phase 5: 密码设置 ────────────────────────────────────
            if state not in ["DONE", "CONSENT", "CALLBACK"]:
                if state in ["UNKNOWN", "LOADING", "OTP"]:
                    await asyncio.sleep(3)
                state = await wait_for_state(["PASSWORD", "CONSENT", "DONE", "CALLBACK"], timeout=30)
                log(f"进入状态: {state}", "info")

            if state == "PASSWORD":
                log("阶段 5: 设置密码")
                await _human_delay(1.5, 3.0)
                pwd_inputs = page.locator('xpath=//input[@type="password"]')
                for attempt in range(3):
                    try:
                        count = await pwd_inputs.count()
                        if count < 2 and attempt == 0:
                            await asyncio.sleep(2)
                            count = await pwd_inputs.count()
                        await _move_to_element(page, pwd_inputs.first)
                        await _human_type(page, pwd_inputs.first, password, min_delay=30, max_delay=90)
                        await _human_delay(0.5, 1.2)
                        if count > 1:
                            await _move_to_element(page, pwd_inputs.nth(1))
                            await _human_type(page, pwd_inputs.nth(1), password, min_delay=30, max_delay=90)
                            await _human_delay(0.5, 1.0)
                        submit_btn = page.locator('xpath=//form//button[@type="submit"]')
                        if await submit_btn.count() > 0 and await submit_btn.first.is_visible():
                            await _move_to_element(page, submit_btn.first)
                            await submit_btn.first.click()
                        else:
                            await page.keyboard.press("Enter")
                    except Exception:
                        await asyncio.sleep(2)
                        continue
                    await asyncio.sleep(4)
                    new_state = await detect_state()
                    if new_state != "PASSWORD":
                        log("密码设置完成", "ok")
                        state = new_state
                        break

            # ─── Phase 6: 授权确认 ────────────────────────────────────
            if state not in ["DONE", "CALLBACK"]:
                state = await wait_for_state(["CONSENT", "DONE", "CALLBACK"], timeout=45)

            if state == "CONSENT":
                log("阶段 6: 授权同意页")
                await asyncio.sleep(3)
                for attempt in range(10):
                    try:
                        clicked = await page.evaluate("""() => {
                            const buttons = Array.from(document.querySelectorAll('button'));
                            const visible = buttons.filter(b => b.offsetWidth > 0 && b.offsetHeight > 0);
                            for (const b of visible) {
                                const t = (b.innerText || '').toLowerCase();
                                if (t.includes('allow') || t.includes('authorize') || t.includes('accept') || t.includes('confirm')) {
                                    b.click(); return true;
                                }
                            }
                            if (visible.length > 0) { visible[visible.length - 1].click(); return true; }
                            return false;
                        }""")
                    except Exception:
                        log("授权后页面已导航", "ok")
                        state = "CALLBACK"
                        break
                    if clicked:
                        log("自动点击授权按钮", "ok")
                        await asyncio.sleep(4)
                        try:
                            new_state = await detect_state()
                        except Exception:
                            state = "CALLBACK"
                            break
                        if new_state != "CONSENT":
                            state = new_state
                            break
                    await asyncio.sleep(2)

            # 等待回调 code
            log("等待 OAuth 回调...")
            for i in range(30):
                if cancel_check and cancel_check():
                    log("用户取消", "err")
                    await browser.close()
                    callback_server.shutdown()
                    _cleanup()
                    return _partial_result("用户取消")
                if authorization_code:
                    break
                current_url = page.url
                if "127.0.0.1:3128" in current_url or "localhost:3128" in current_url:
                    qs = parse_qs(urlparse(current_url).query)
                    authorization_code = qs.get("code", [""])[0]
                    if authorization_code:
                        break
                if "code=" in current_url and "code_challenge" not in current_url:
                    qs = parse_qs(urlparse(current_url).query)
                    code_val = qs.get("code", [""])[0]
                    if code_val and len(code_val) > 10:
                        authorization_code = code_val
                        break
                if "awsapps.com" in current_url:
                    try:
                        await page.evaluate("""() => {
                            const buttons = Array.from(document.querySelectorAll('button'));
                            const visible = buttons.filter(b => b.offsetWidth > 0 && b.offsetHeight > 0);
                            for (const b of visible) {
                                const t = (b.innerText || '').toLowerCase();
                                if (t.includes('allow') || t.includes('authorize') || t.includes('accept') || t.includes('confirm')) {
                                    b.click(); return;
                                }
                            }
                            if (visible.length > 0) visible[visible.length - 1].click();
                        }""")
                    except Exception:
                        pass
                await asyncio.sleep(2)

            await browser.close()
    finally:
        callback_server.shutdown()
        _cleanup()

    # ─── Phase 7: Token 交换 ──────────────────────────────────────────
    if not authorization_code:
        log("未获取到授权码!", "err")
        return _partial_result("未获取授权码")

    log("已获取授权码", "ok")
    log("正在交换 Token...")

    token_resp = s.post(f"{REG_OIDC}/token", json={
        "clientId": client_id,
        "clientSecret": client_secret,
        "grantType": "authorization_code",
        "code": authorization_code,
        "redirectUri": REG_REDIRECT_URI,
        "codeVerifier": code_verifier,
    }, timeout=25, verify=False)

    if token_resp.status_code != 200:
        log(f"Token 交换失败: HTTP {token_resp.status_code}", "err")
        return _partial_result("Token交换失败")

    tokens = token_resp.json()
    access_token = tokens.get("accessToken", "")
    refresh_token = tokens.get("refreshToken", "")
    expires_in = tokens.get("expiresIn", 28800)

    if not access_token:
        log("Token 交换未返回 accessToken", "err")
        return _partial_result("无accessToken")

    log("Token 获取成功", "ok")

    if auto_login:
        log("注入本地 Token...", "info")
        persist_tokens(client_id, client_secret, access_token, refresh_token, expires_in, log, email=email)
        machine_ids = inject_machine_ids(log)
        if skip_onboard:
            skip_onboarding(log)

    log("=" * 40, "ok")
    log("注册完成! (RoxyBrowser)", "ok")
    log(f"  Email: {email}", "ok")
    log(f"  Password: {password}", "ok")
    log("=" * 40, "ok")

    return {
        "email": email,
        "password": password,
        "full_name": full_name,
        "provider": "BuilderId",
        "authMethod": "IdC",
        "region": "us-east-1",
        "clientId": client_id,
        "clientSecret": client_secret,
        "clientIdHash": _sha1_hash(client_id),
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresAt": (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).strftime("%Y/%m/%d %H:%M:%S"),
        "browser": "RoxyBrowser",
    }
