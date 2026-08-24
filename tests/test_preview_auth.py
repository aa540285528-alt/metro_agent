from __future__ import annotations

import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path


PREVIEW_PATH = (
    Path(__file__).parents[1] / "src" / "metro_agent" / "static" / "preview.html"
)


class _ElementParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.elements.append((tag, dict(attrs)))


def _preview() -> str:
    return PREVIEW_PATH.read_text(encoding="utf-8")


def _elements() -> list[tuple[str, dict[str, str | None]]]:
    parser = _ElementParser()
    parser.feed(_preview())
    return parser.elements


def _element_by_id(element_id: str) -> tuple[str, dict[str, str | None]]:
    return next(
        element for element in _elements() if element[1].get("id") == element_id
    )


def _inline_application_script() -> str:
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", _preview(), re.DOTALL)
    return scripts[-1]


def test_preview_uses_cookie_auth_endpoints_and_no_browser_user_id() -> None:
    html = _preview()

    for endpoint in ("/api/auth/me", "/api/auth/login", "/api/auth/logout"):
        assert endpoint in html
    for legacy_fragment in (
        "metro-chat-user-id",
        "STORAGE_KEYS.userId",
        "browserUserId",
        "user_id:",
        "?user_id=",
        "searchParams.set('user_id'",
        'searchParams.set("user_id"',
    ):
        assert legacy_fragment not in html


def test_preview_keeps_only_thread_working_state_in_local_storage() -> None:
    script = _inline_application_script()

    assert "metro-chat-thread-id" in script
    assert "STORAGE_KEYS.threadId" in script
    assert "thread_id: currentThreadId" in script
    assert "localStorage" in script
    assert not re.search(
        r"(?:localStorage|sessionStorage)\.setItem\([^\n]*(?:password|credential)",
        script,
        re.IGNORECASE,
    )


def test_login_form_is_accessible_and_password_is_not_persisted() -> None:
    tag, form = _element_by_id("login-form")
    assert tag == "form"

    username_tag, username = _element_by_id("login-username")
    password_tag, password = _element_by_id("login-password")
    assert username_tag == password_tag == "input"
    assert username.get("autocomplete") == "username"
    assert password.get("autocomplete") == "current-password"
    assert password.get("type") == "password"

    labels = [attrs.get("for") for tag, attrs in _elements() if tag == "label"]
    assert "login-username" in labels
    assert "login-password" in labels
    assert 'id="login-submit"' in _preview()
    assert 'id="login-error"' in _preview()
    assert "注册" not in _preview()
    assert "找回密码" not in _preview()


def test_startup_distinguishes_unauthenticated_from_service_failure() -> None:
    script = _inline_application_script()

    assert "async function restoreSession()" in script
    assert "'/api/auth/me'" in script
    assert "response.status === 401" in script
    assert "showLoginView" in script
    assert "showAuthServiceError" in script
    assert 'id="auth-retry"' in _preview()


def test_api_fetch_uses_same_origin_cookies_and_centralizes_401_cleanup() -> None:
    script = _inline_application_script()

    assert "async function apiFetch" in script
    assert "credentials: 'same-origin'" in script
    assert "handleUnauthorized" in script
    assert "clearAuthenticatedState" in script
    assert "response.status === 401" in script
    assert "fetch('/api" not in script
    assert 'fetch("/api' not in script


def test_login_handles_invalid_credentials_throttling_and_clears_password() -> None:
    script = _inline_application_script()

    assert "'/api/auth/login'" in script
    assert "用户名或密码错误" in script
    assert "Retry-After" in script
    assert "请稍后再试" in script
    assert "loginPassword.value = ''" in script
    assert "loginSubmit.disabled" in script
    assert "JSON.stringify({ username, password })" in script


def test_role_controls_monitoring_visibility_and_requests() -> None:
    script = _inline_application_script()

    assert "currentUser?.role === 'admin'" in script
    assert "data-monitoring-trigger" in _preview()
    assert "setMonitoringAccess" in script
    assert "if (!isAdmin()) return" in script
    for endpoint in (
        "/api/monitoring/summary",
        "/api/monitoring/traces",
        "/api/monitoring/evaluations",
    ):
        assert endpoint in script


def test_logout_only_clears_client_state_after_server_logout_or_401() -> None:
    script = _inline_application_script()

    assert "'/api/auth/logout'" in script
    assert "async function logout" in script
    assert "response.ok || response.status === 401" in script
    assert "退出登录失败" in script
    assert "clearAuthenticatedState" in script


def test_mobile_header_exposes_identity_and_logout() -> None:
    html = _preview()

    assert html.count("data-current-username") >= 2
    assert html.count("data-current-role") >= 2
    assert html.count("data-logout-trigger") >= 2


def test_auth_cleanup_restores_a_blank_welcome_workspace() -> None:
    script = _inline_application_script()

    cleanup = script.split("function clearAuthenticatedState()", 1)[1].split(
        "async function apiFetch", 1
    )[0]
    assert "chatMessages?.replaceChildren()" in cleanup
    assert "welcomeState?.classList.remove('hidden')" in cleanup
    assert "chatThread?.classList.add('hidden')" in cleanup
    assert "monitoringWorkspace?.classList.add('hidden')" in cleanup


def test_inline_javascript_has_valid_syntax() -> None:
    node = shutil.which("node")
    if node is None:
        return

    result = subprocess.run(
        [node, "--check", "-"],
        input=_inline_application_script(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
