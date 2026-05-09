"""Cloudflare Temp Email 邮件服务 (dreamhunter2333/cloudflare_temp_email)"""
import hashlib
import re
import time

from .base import MailProvider


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class CFTempMailProvider(MailProvider):

    name = "cftempmail"
    display_name = "CF Temp Mail"

    def __init__(self, base_url: str = "", username: str = "", password: str = "",
                 domain: str = "", **_kwargs):
        import requests as _req
        self.api_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.domain = domain
        self.session = _req.Session()
        self.session.verify = False
        self.session.headers.update({"Content-Type": "application/json"})
        self._user_jwt = ""
        self._mail_jwt = ""
        self.address = None

    def _ensure_user_jwt(self):
        if self._user_jwt:
            return
        resp = self.session.post(
            f"{self.api_url}/user_api/login",
            json={"email": self.username, "password": _sha256(self.password)},
            timeout=15,
        )
        resp.raise_for_status()
        self._user_jwt = resp.json()["jwt"]

    def create_mailbox(self) -> str:
        self._ensure_user_jwt()
        resp = self.session.post(
            f"{self.api_url}/api/new_address",
            json={"name": "", "domain": self.domain},
            headers={"x-user-token": self._user_jwt},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._mail_jwt = data["jwt"]
        self.address = data["address"]
        return self.address

    def wait_otp(self, timeout: int = 120, poll_interval: int = 3) -> str:
        if not self._mail_jwt:
            return ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = self.session.get(
                f"{self.api_url}/api/mails?limit=10&offset=0",
                headers={"Authorization": f"Bearer {self._mail_jwt}"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", [])
                for mail in results:
                    mail_id = mail.get("id")
                    if not mail_id:
                        continue
                    detail = self.session.get(
                        f"{self.api_url}/api/mails/{mail_id}",
                        headers={"Authorization": f"Bearer {self._mail_jwt}"},
                        timeout=10,
                    )
                    if detail.status_code == 200:
                        body = detail.json()
                        text = body.get("text", "") or body.get("raw", "") or str(body)
                        match = re.search(r'\b(\d{6})\b', text)
                        if match:
                            return match.group(1)
            time.sleep(poll_interval)
        return ""

    def list_domains(self) -> list[dict]:
        resp = self.session.get(
            f"{self.api_url}/open_api/settings",
            timeout=10,
        )
        if resp.status_code == 200:
            domains = resp.json().get("domains", [])
            return [{"id": d, "domain": d} for d in domains if d]
        return []
