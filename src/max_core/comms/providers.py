"""Provider adapters. Each raises ToolError with a fixed code; never leaks secrets."""

from __future__ import annotations

import email.utils
import imaplib
import smtplib
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as default_policy

import httpx

from max_core.tools import ToolError

HttpFactory = Callable[[], httpx.Client]


def _client() -> httpx.Client:
    return httpx.Client(timeout=20.0, follow_redirects=False)


# ---------------------------------------------------------------- email -----
@dataclass(frozen=True, slots=True)
class EmailConfig:
    smtp_host: str
    smtp_port: int
    username: str
    password: str
    sender: str
    imap_host: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> EmailConfig | None:
        user = env.get("MAX_EMAIL_USERNAME", "").strip()
        password = env.get("MAX_EMAIL_PASSWORD", "")
        host = env.get("MAX_EMAIL_SMTP_HOST", "smtp.gmail.com").strip()
        if not user or not password:
            return None
        return cls(
            host,
            int(env.get("MAX_EMAIL_SMTP_PORT", "465")),
            user,
            password,
            env.get("MAX_EMAIL_FROM", user).strip(),
            env.get("MAX_EMAIL_IMAP_HOST", "imap.gmail.com").strip(),
        )


class EmailChannel:
    def __init__(
        self,
        config: EmailConfig,
        smtp: Callable[..., smtplib.SMTP] | None = None,
        imap: Callable[..., imaplib.IMAP4] | None = None,
    ) -> None:
        self.config = config
        self._smtp = smtp
        self._imap = imap

    def send(self, to: str, subject: str, body: str) -> str:
        message = EmailMessage()
        message["From"] = self.config.sender
        message["To"] = to
        message["Subject"] = subject
        message["Message-ID"] = email.utils.make_msgid(domain=self.config.sender.split("@")[-1])
        message.set_content(body)
        context = ssl.create_default_context()
        try:
            if self._smtp is not None:
                server = self._smtp(self.config.smtp_host, self.config.smtp_port)
            elif self.config.smtp_port == 465:
                server = smtplib.SMTP_SSL(self.config.smtp_host, 465, context=context, timeout=20)
            else:
                server = smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=20)
                server.starttls(context=context)
            with server:
                server.login(self.config.username, self.config.password)
                server.send_message(message)
        except smtplib.SMTPAuthenticationError:
            raise ToolError("email_auth_failed") from None
        except (smtplib.SMTPException, OSError):
            raise ToolError("email_send_failed") from None
        return str(message["Message-ID"])

    def unread(self, limit: int = 5) -> list[tuple[str, str, str]]:
        """(from, subject, first text) of the newest unread messages; peek only."""
        try:
            box = (
                self._imap(self.config.imap_host)
                if self._imap is not None
                else imaplib.IMAP4_SSL(
                    self.config.imap_host, ssl_context=ssl.create_default_context()
                )
            )
            box.login(self.config.username, self.config.password)
            box.select("INBOX", readonly=True)
            _, data = box.search(None, "UNSEEN")
            ids = (data[0] or b"").split()[-max(1, min(limit, 20)) :]
            out: list[tuple[str, str, str]] = []
            for message_id in reversed(ids):
                _, parts = box.fetch(message_id, "(BODY.PEEK[])")
                raw = parts[0][1] if parts and isinstance(parts[0], tuple) else b""
                parsed = BytesParser(policy=default_policy).parsebytes(raw)
                text_part = parsed.get_body(preferencelist=("plain",))
                text = text_part.get_content() if text_part is not None else ""
                out.append(
                    (
                        str(make_header(decode_header(parsed.get("From", "")))),
                        str(make_header(decode_header(parsed.get("Subject", "")))),
                        " ".join(str(text).split())[:600],
                    )
                )
            box.logout()
            return out
        except imaplib.IMAP4.error:
            raise ToolError("email_auth_failed") from None
        except OSError:
            raise ToolError("email_read_failed") from None


# ------------------------------------------------------------- whatsapp -----
@dataclass(frozen=True, slots=True)
class WhatsAppConfig:
    token: str
    phone_number_id: str
    app_secret: str
    verify_token: str
    api_version: str = "v21.0"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> WhatsAppConfig | None:
        token = env.get("MAX_WHATSAPP_TOKEN", "").strip()
        phone_id = env.get("MAX_WHATSAPP_PHONE_NUMBER_ID", "").strip()
        if not token or not phone_id:
            return None
        return cls(
            token,
            phone_id,
            env.get("MAX_WHATSAPP_APP_SECRET", ""),
            env.get("MAX_WHATSAPP_VERIFY_TOKEN", ""),
            env.get("MAX_WHATSAPP_API_VERSION", "v21.0"),
        )


class WhatsAppChannel:
    def __init__(self, config: WhatsAppConfig, http: HttpFactory = _client) -> None:
        self.config = config
        self._http = http

    def send(self, to_phone: str, text: str) -> str:
        url = (
            f"https://graph.facebook.com/{self.config.api_version}/"
            f"{self.config.phone_number_id}/messages"
        )
        payload = {
            "messaging_product": "whatsapp",
            "to": to_phone.lstrip("+"),
            "type": "text",
            "text": {"body": text, "preview_url": False},
        }
        try:
            with self._http() as client:
                response = client.post(
                    url, json=payload, headers={"Authorization": f"Bearer {self.config.token}"}
                )
        except httpx.HTTPError:
            raise ToolError("whatsapp_unreachable") from None
        if response.status_code in (401, 403):
            raise ToolError("whatsapp_auth_failed")
        if response.status_code >= 400:
            # 131047: outside the 24 h window, a template message is required.
            code = ""
            try:
                code = str(response.json().get("error", {}).get("code", ""))
            except ValueError:
                pass
            raise ToolError("whatsapp_needs_template" if code == "131047" else "whatsapp_rejected")
        try:
            return str(response.json()["messages"][0]["id"])
        except (ValueError, KeyError, IndexError, TypeError):
            return ""


# ---------------------------------------------------------------- phone -----
@dataclass(frozen=True, slots=True)
class ExotelConfig:
    account_sid: str
    api_key: str
    api_token: str
    caller_id: str  # Exotel virtual number (ExoPhone)
    owner_phone: str  # MAX rings the owner first, then connects the contact
    subdomain: str = "api.exotel.com"  # or api.in.exotel.com (Mumbai cluster)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ExotelConfig | None:
        values = [
            env.get(k, "").strip()
            for k in (
                "MAX_EXOTEL_SID",
                "MAX_EXOTEL_API_KEY",
                "MAX_EXOTEL_API_TOKEN",
                "MAX_EXOTEL_CALLER_ID",
                "MAX_OWNER_PHONE",
            )
        ]
        if not all(values):
            return None
        sid, key, token, caller, owner = values
        return cls(
            sid,
            key,
            token,
            caller,
            owner,
            subdomain=env.get("MAX_EXOTEL_SUBDOMAIN", "api.exotel.com").strip(),
        )


class PhoneChannel:
    def __init__(self, config: ExotelConfig, http: HttpFactory = _client) -> None:
        self.config = config
        self._http = http

    def connect(self, to_phone: str, status_url: str | None = None) -> str:
        """Ring the owner; when they answer, Exotel dials ``to_phone`` and bridges."""
        url = (
            f"https://{self.config.subdomain}/v1/Accounts/{self.config.account_sid}"
            "/Calls/connect.json"
        )
        data = {
            "From": self.config.owner_phone,
            "To": to_phone,
            "CallerId": self.config.caller_id,
            "TimeLimit": "1800",
            "Record": "false",
        }
        if status_url:
            data["StatusCallback"] = status_url
        try:
            with self._http() as client:
                response = client.post(
                    url, data=data, auth=(self.config.api_key, self.config.api_token)
                )
        except httpx.HTTPError:
            raise ToolError("phone_unreachable") from None
        if response.status_code in (401, 403):
            raise ToolError("phone_auth_failed")
        if response.status_code >= 400:
            raise ToolError("phone_rejected")
        try:
            return str(response.json()["Call"]["Sid"])
        except (ValueError, KeyError, TypeError):
            return ""
