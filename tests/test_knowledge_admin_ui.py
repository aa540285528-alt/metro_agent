from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path


PREVIEW_PATH = Path(__file__).parents[1] / "src" / "metro_agent" / "static" / "preview.html"


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


def test_knowledge_admin_navigation_is_present_but_admin_gated() -> None:
    html = _preview()
    script = _inline_application_script()

    assert html.count("data-knowledge-admin-trigger") >= 2
    assert "setKnowledgeAdminAccess" in script
    assert "if (!isAdmin()) return" in script
    assert "button.classList.toggle('hidden', !allowed)" in script
    assert "button.disabled = !allowed" in script
    assert "/api/admin/knowledge/drafts" in script
    assert "/api/admin/knowledge/releases" in script
    assert "/api/admin/knowledge/jobs/" in script
    assert "/api/admin/knowledge/audits" in script
    assert "package_sha256" in script


def test_knowledge_admin_workspace_clears_transient_state_on_logout() -> None:
    script = _inline_application_script()
    cleanup = script.split("function clearKnowledgeAdminState()", 1)[1].split(
        "function clearAuthenticatedState()", 1
    )[0]
    poll_timer = script.split("function clearKnowledgeAdminPollTimer()", 1)[1].split(
        "function renderKnowledgeAdminMetric()", 1
    )[0]
    authenticated_cleanup = script.split("function clearAuthenticatedState()", 1)[1].split(
        "async function apiFetch", 1
    )[0]

    assert "activeKnowledgeAdminRequestController?.abort()" in cleanup
    assert "activeKnowledgeAdminRequestController = null" in cleanup
    assert "activeKnowledgeAdminDetailRequestController?.abort()" in cleanup
    assert "activeKnowledgeAdminDetailRequestController = null" in cleanup
    assert "activeKnowledgeAdminAuditRequestController?.abort()" in cleanup
    assert "activeKnowledgeAdminAuditRequestController = null" in cleanup
    assert "clearKnowledgeAdminPollTimer()" in cleanup
    assert "clearTimeout(knowledgeAdminJobPollTimer)" in poll_timer
    assert "knowledgeAdminJobPollTimer = null" in poll_timer
    assert "knowledgeAdminDrafts = []" in cleanup
    assert "knowledgeAdminReleases = []" in cleanup
    assert "knowledgeAdminAuditEvents = []" in cleanup
    assert "knowledgeAdminWorkspace?.classList.add('hidden')" in cleanup
    assert "if (knowledgeAdminPublishButton) knowledgeAdminPublishButton.disabled = false;" in cleanup
    assert "if (knowledgeAdminValidateButton) knowledgeAdminValidateButton.disabled = false;" in cleanup
    assert "if (knowledgeAdminRollbackSubmit) knowledgeAdminRollbackSubmit.disabled = false;" in cleanup
    assert "clearKnowledgeAdminState();" in authenticated_cleanup


def test_knowledge_admin_upload_and_rollback_ui_surfaces_the_expected_controls() -> None:
    html = _preview()
    script = _inline_application_script()

    assert 'accept=".zip"' in html
    assert "已上传" in html
    assert "校验中" in html
    assert "校验失败" in html
    assert "校验通过，待发布" in html
    assert "发布中" in html
    assert "已发布" in html
    assert "发布失败" in html
    assert 'id="knowledge-rollback-dialog"' in html
    assert 'id="knowledge-rollback-reason"' in html
    assert 'maxlength="500"' in html
    assert "回滚知识发布" in html
    assert "submitKnowledgeAdminRollback" in script
    assert "/rollback" in script
    assert "reason" in script


def test_knowledge_admin_polling_is_bounded_and_terminal_states_stop_it() -> None:
    script = _inline_application_script()

    assert "setInterval" not in script
    assert "setTimeout" in script
    assert "2000" in script
    assert "knowledgeAdminJobPollInFlight" in script
    assert "clearTimeout" in script
    assert "job.status === 'succeeded'" in script
    assert "job.status === 'failed'" in script
    assert "refreshKnowledgeAdminData" in script
    assert "loadKnowledgeAdminAuditEvents" in script


def test_knowledge_admin_renders_server_fields_with_text_content() -> None:
    script = _inline_application_script()

    assert "renderKnowledgeAdminDrafts" in script
    assert "renderKnowledgeAdminReleases" in script
    assert "renderKnowledgeAdminAuditEvents" in script
    assert "renderKnowledgeAdminValidationReport" in script
    assert "textContent" in script
    draft_render = script.split("function renderKnowledgeAdminDrafts(", 1)[1].split(
        "function renderKnowledgeAdminReleases(", 1
    )[0]
    release_render = script.split("function renderKnowledgeAdminReleases(", 1)[1].split(
        "function renderKnowledgeAdminAuditEvents(", 1
    )[0]
    audit_render = script.split("function renderKnowledgeAdminAuditEvents(", 1)[1].split(
        "function loadKnowledgeAdminAuditEvents()", 1
    )[0]
    report_render = script.split("function renderKnowledgeAdminValidationReport(", 1)[1].split(
        "function renderKnowledgeAdminDraftDetail(", 1
    )[0]

    for segment in (draft_render, release_render, audit_render, report_render):
        assert "textContent" in segment
        assert "innerHTML" not in segment


def test_knowledge_admin_workspace_has_expected_panels_and_status_container() -> None:
    html = _preview()
    ids = {
        "knowledge-admin-workspace",
        "knowledge-current-release",
        "knowledge-upload-form",
        "knowledge-draft-list",
        "knowledge-draft-detail",
        "knowledge-validation-report",
        "knowledge-release-history",
        "knowledge-audit-history",
        "knowledge-job-status",
    }

    for element_id in ids:
        assert f'id="{element_id}"' in html

    assert _element_by_id("knowledge-upload-form")[0] == "form"
    assert _element_by_id("knowledge-rollback-dialog")[0] in {"div", "dialog"}
