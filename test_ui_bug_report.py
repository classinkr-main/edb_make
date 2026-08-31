import json
import subprocess
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
APP_SOURCE = PROJECT_ROOT / "ui_prototype" / "app.jsx"
BOARD_SOURCE = PROJECT_ROOT / "ui_prototype" / "board.html"


class BugReportUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = APP_SOURCE.read_text(encoding="utf-8")
        cls.board = BOARD_SOURCE.read_text(encoding="utf-8")

    def test_settings_panel_has_accessible_bug_report_form(self):
        settings = self.app.split("{tab === 'board' && (", 1)[1].split(
            "function LoadingOverlay", 1
        )[0]
        self.assertIn('aria-label="버그 리포트"', settings)
        self.assertIn("사용 중 문제가 있었나요?", settings)
        self.assertIn('htmlFor="bug-report-description"', settings)
        self.assertIn('id="bug-report-description"', settings)
        self.assertIn('htmlFor="bug-report-contact"', settings)
        self.assertIn('id="bug-report-contact"', settings)
        self.assertIn("이 문제에 관한 회신에 동의", settings)
        self.assertIn("진단 정보 포함", settings)
        self.assertIn("리포트 보내기", settings)
        self.assertIn("원본 시험지, 세션 내용, API 키, 전체 로컬 경로", settings)

    def test_submit_posts_only_allowlisted_context_to_local_endpoint(self):
        helper = self.app.split("async function submitBugReport", 1)[1].split(
            "async function fetchAppUpdateStatus", 1
        )[0]
        handler = self.app.split("const handleBugReportSubmit", 1)[1].split(
            "const savedCrop", 1
        )[0]
        self.assertIn("fetch('/api/bug-report'", helper)
        self.assertIn("method: 'POST'", helper)
        self.assertIn("'Content-Type': 'application/json'", helper)
        for field in (
            "view",
            "settingsTab",
            "inputIntent",
            "reviewStatus",
            "itemCount",
            "pendingCount",
            "hangul",
            "runtimeErrors",
            "lastOperationError",
        ):
            self.assertIn(field, handler)
        self.assertIn("contact: bugReportContact.trim()", handler)
        self.assertIn("consentToContact", handler)
        self.assertNotIn("session,", handler)
        self.assertNotIn("pendingFile,", handler)

    def test_runtime_errors_are_reduced_to_bounded_primitives(self):
        helper = self.app.split("function runtimeErrorsForBugReport", 1)[1].split(
            "async function submitBugReport", 1
        )[0]
        self.assertIn("entries.slice(-10)", helper)
        self.assertIn("message: String(message).slice(0, 1500)", helper)
        self.assertIn("safe.filename", helper)
        self.assertIn("safe.operation", helper)
        self.assertIn("safe.code", helper)
        self.assertIn("safe.status", helper)
        self.assertNotIn("error: rawError", helper)

    def test_report_card_styles_match_existing_settings_panel(self):
        for selector in (
            ".bug-report-card",
            ".bug-report-summary",
            ".bug-report-form",
            ".bug-report-diagnostics",
            ".bug-report-privacy",
            ".bug-report-status.success",
            ".bug-report-actions",
            ".bug-report-contact",
        ):
            self.assertIn(selector, self.board)

    def test_publish_failure_has_persistent_recovery_actions(self):
        banner = self.app.split("function OperationRecoveryBanner", 1)[1].split(
            "function LoadingOverlay", 1
        )[0]
        for label in (
            "EDB 다시 제작",
            "최근 저장본 열기",
            "PNG로 대체 저장",
            "초기화 후 다시 시작",
            "버그 리포트 열기",
            "오류 내용 복사",
        ):
            self.assertIn(label, banner)
        self.assertIn("편집 내용은 안전합니다", banner)
        self.assertIn("초기화 후 원본 PDF를 다시 등록", banner)
        self.assertIn("설정 → 문제 신고 → 버그 리포트", banner)
        self.assertIn("operationRecoverySummary(error)", banner)
        self.assertIn("onReset", banner)
        self.assertIn("onReport", banner)
        publish = self.app.split("const onPublish = async", 1)[1].split(
            "return (", 1
        )[0]
        self.assertIn("operationErrorFromResponse", publish)
        self.assertIn("activateOperationRecovery(e, '제작 실패'", publish)
        self.assertIn("clearOperationRecovery()", publish)
        self.assertIn("publish_download_failed", self.app)
        self.assertIn("await downloadPublishSummary(normalizedPublishSummary)", publish)
        self.assertIn("captureRecoverableDiagnostic", self.app)
        self.assertIn("EDB_CAPTURE_RUNTIME_DIAGNOSTIC", self.board)
        self.assertIn(".operation-recovery-banner", self.board)

    def test_recovery_report_action_opens_global_dialog_with_error_snapshot(self):
        dialog = self.app.split("function BugReportDialog", 1)[1].split(
            "function OperationRecoveryBanner", 1
        )[0]
        self.assertIn('role="dialog"', dialog)
        self.assertIn('aria-modal="true"', dialog)
        self.assertIn("errorSnapshot", dialog)
        self.assertIn("lastOperationError: errorSnapshot", dialog)
        self.assertIn("global-bug-report-description", dialog)
        self.assertIn("자동 첨부 오류", dialog)
        app = self.app.split("function App()", 1)[1]
        self.assertIn("setBugReportRequest({ id: bugReportSequenceRef.current, errorSnapshot: snapshot })", app)
        self.assertIn("recovery={operationRecoveryDismissed ? null : operationRecovery}", app)
        self.assertIn("onReport={openBugReportAfterOperationError}", app)
        self.assertIn("onReset={resetAfterOperationError}", app)
        self.assertIn("<BugReportDialog", app)
        self.assertIn("draftText.length >= 5 || Boolean(errorSnapshot)", dialog)
        self.assertIn("자동 첨부 오류 리포트", dialog)
        self.assertIn("!result?.reportId", dialog)
        self.assertIn("disabled={Boolean(result?.reportId)}", dialog)
        self.assertIn("접수 완료", dialog)
        self.assertIn("const submitInFlightRef = useRef(false);", dialog)
        self.assertIn("!canSubmit || submitInFlightRef.current", dialog)
        self.assertIn("submitInFlightRef.current = true;", dialog)
        self.assertIn("submitInFlightRef.current = false;", dialog)
        self.assertLess(
            dialog.index("submitInFlightRef.current = true;"),
            dialog.index("await submitBugReport"),
        )
        self.assertIn("document.body.style.overflow = 'hidden'", dialog)
        self.assertIn('aria-describedby="bug-report-dialog-help"', dialog)

    def test_operation_recovery_state_transition_runs_activate_dismiss_clear(self):
        start = self.app.index("function operationRecoveryStateTransition")
        end = self.app.index("\nfunction isNetworkRequestError", start)
        helper = self.app[start:end]
        script = helper + """
const active = operationRecoveryStateTransition(null, {
  type: 'activate',
  recovery: { kind: 'recognition', error: { code: 'recognition_failed' } },
});
const dismissed = operationRecoveryStateTransition(active, { type: 'dismiss' });
const cleared = operationRecoveryStateTransition(dismissed, { type: 'clear' });
const conflict = operationRecoveryStateTransition(null, {
  type: 'activate',
  recovery: {
    kind: 'publish',
    error: { code: 'session_conflict' },
    conflict: { latestSession: { session_name: 'latest' } },
  },
});
const conflictDismissed = operationRecoveryStateTransition(conflict, { type: 'dismiss' });
const emptyConflict = operationRecoveryStateTransition(null, {
  type: 'activate',
  recovery: {
    kind: 'restore',
    error: { code: 'session_conflict' },
    conflict: { latestSession: null, hasLatestSessionState: true, sessionRevision: 9 },
  },
});
const emptyConflictDismissed = operationRecoveryStateTransition(emptyConflict, { type: 'dismiss' });
process.stdout.write(JSON.stringify({ active, dismissed, cleared, conflict, conflictDismissed, emptyConflict, emptyConflictDismissed }));
"""
        result = subprocess.run(
            ["node", "-e", script],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        payload = json.loads(result.stdout)
        self.assertEqual("recognition_failed", payload["active"]["recovery"]["error"]["code"])
        self.assertFalse(payload["active"]["dismissed"])
        self.assertTrue(payload["dismissed"]["dismissed"])
        self.assertIsNone(payload["cleared"]["recovery"])
        self.assertFalse(payload["cleared"]["dismissed"])
        self.assertEqual(payload["conflict"], payload["conflictDismissed"])
        self.assertEqual(payload["emptyConflict"], payload["emptyConflictDismissed"])
        self.assertIsNone(payload["emptyConflict"]["recovery"]["conflict"]["latestSession"])

    def test_null_latest_session_conflict_preserves_revision_contract(self):
        format_start = self.app.index("function formatApiError")
        format_end = self.app.index("\nfunction operationErrorFromResponse", format_start)
        operation_start = format_end + 1
        operation_end = self.app.index("\nfunction operationErrorClipboardText", operation_start)
        script = self.app[format_start:format_end] + self.app[operation_start:operation_end] + """
const error = operationErrorFromResponse({
  ok: false,
  code: 'session_conflict',
  session: null,
  sessionRevision: 9,
  sessionEpoch: 'epoch-9',
  recoverySteps: ['최신 상태 확인'],
}, { status: 409 }, 'session_restore_history', '복원 실패');
process.stdout.write(JSON.stringify({
  code: error.code,
  latestSession: error.latestSession,
  hasLatestSessionState: error.hasLatestSessionState,
  sessionRevision: error.sessionRevision,
  sessionEpoch: error.sessionEpoch,
}));
"""
        result = subprocess.run(
            ["node", "-e", script],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        payload = json.loads(result.stdout)
        self.assertEqual("session_conflict", payload["code"])
        self.assertIsNone(payload["latestSession"])
        self.assertTrue(payload["hasLatestSessionState"])
        self.assertEqual(9, payload["sessionRevision"])
        self.assertEqual("epoch-9", payload["sessionEpoch"])

    def test_conflict_recovery_never_puts_the_pre_conflict_session_in_undo(self):
        load_latest = self.app.split("const loadLatestConflictSession", 1)[1].split(
            "const rebaseConflictSession", 1
        )[0]
        rebase = self.app.split("const rebaseConflictSession", 1)[1].split(
            "return (", 1
        )[0]
        helper = self.app.split("function rebaseSessionBoardLayout", 1)[1].split(
            "function fitWidthPageAsIsSession", 1
        )[0]

        self.assertIn("historyStackRef.current = [];", load_latest)
        self.assertIn("setHistoryStack([]);", load_latest)
        self.assertNotIn("appendBoundedHistory", load_latest)
        self.assertIn("conflictSafeUndoHistory(conflict.latestSession)", rebase)
        self.assertNotIn("appendBoundedHistory(prev, localDraft", rebase)
        self.assertIn("latest.session_name = latest.session_name || '새 세션'", helper)
        self.assertNotIn("fileName || latest.session_name", helper)

        clone_start = self.app.index("function cloneSession")
        clone_end = self.app.index("\nfunction makeUniqueId", clone_start)
        safe_start = self.app.index("function conflictSafeUndoHistory")
        safe_end = self.app.index("\nfunction fitWidthPageAsIsSession", safe_start)
        script = self.app[clone_start:clone_end] + self.app[safe_start:safe_end] + """
const latest = {
  session_name: '서버 최신 이름',
  problems: [{ id: 'p1', reviewStatus: 'normal', placementXRatio: 0.2 }],
};
const history = conflictSafeUndoHistory(latest);
history[0].problems[0].reviewStatus = 'changed';
process.stdout.write(JSON.stringify({ history, latest }));
"""
        result = subprocess.run(
            ["node", "-e", script],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        payload = json.loads(result.stdout)
        self.assertEqual("서버 최신 이름", payload["history"][0]["session_name"])
        self.assertEqual("normal", payload["latest"]["problems"][0]["reviewStatus"])

    def test_conflict_reset_and_queue_failures_have_persistent_recovery_paths(self):
        self.assertIn("error.latestSession = payload?.session || null", self.app)
        self.assertIn("error.hasLatestSessionState = Boolean(", self.app)
        self.assertIn("최신 상태 불러오기", self.app)
        self.assertIn("내 배치 안전하게 합치기", self.app)
        self.assertIn("rebaseSessionBoardLayout", self.app)
        self.assertIn("resetInFlightRef.current", self.app)
        self.assertIn("artifact_cleanup_busy", self.app)
        restore = self.app.split("const restoreRecentSession = useCallback", 1)[1].split(
            "const triggerUpload", 1
        )[0]
        self.assertIn("최근 저장본을 열면 현재 화면의 저장되지 않은 배치를 덮어씁니다", restore)
        self.assertIn("appendBoundedHistory(prev, recoveryDraft", restore)
        self.assertIn("clearOperationRecovery()", restore)
        self.assertIn("const restoreInFlightRef = useRef(false);", self.app)
        self.assertIn("restoreInFlightRef.current || restoringSessionId", restore)
        self.assertIn("이미 최근 작업을 여는 중입니다", restore)
        self.assertIn("restoreInFlightRef.current = true;", restore)
        self.assertIn("restoreInFlightRef.current = false;", restore)
        self.assertLess(restore.index("restoreInFlightRef.current = true;"), restore.index("postRestoreSessionHistory(id)"))
        queue = self.app.split("const processQueuedFiles = useCallback", 1)[1].split(
            "const cancelRecognitionReview", 1
        )[0]
        self.assertIn("kind: 'recognition'", queue)
        self.assertIn("kind: 'registration'", queue)
        self.assertIn("페이지 PNG로 등록", queue)
        self.assertIn("수동 쪼개기로 열기", queue)

        retry = self.app.split("const retryRecoveryOperation", 1)[1].split(
            "const runRecoveryAlternative", 1
        )[0]
        banner = self.app.split("function OperationRecoveryBanner", 1)[1].split(
            "function LoadingOverlay", 1
        )[0]
        restore_retry = self.app.split("const restoreRecentSession = useCallback", 1)[1].split(
            "const triggerUpload", 1
        )[0]
        self.assertIn("recovery.conflict && recovery.kind !== 'reset'", retry)
        self.assertIn("먼저 최신 상태를 불러오거나 내 배치를 안전하게 합쳐 주세요", retry)
        self.assertIn("conflictNeedsResolution", banner)
        self.assertIn("먼저 최신 상태 선택", banner)
        self.assertIn("kind: 'restore'", restore_retry)
        self.assertIn("restoreSessionId: id", restore_retry)
        self.assertIn("restoreRecentSession(recovery.restoreSessionId", retry)

        load_latest = self.app.split("const loadLatestConflictSession", 1)[1].split(
            "const rebaseConflictSession", 1
        )[0]
        self.assertIn("const latestSessionIsEmpty = !conflict.latestSession", load_latest)
        self.assertIn("session: conflict.latestSession || null", load_latest)
        self.assertIn("setSession(null)", load_latest)
        self.assertIn("빈 최신 상태를 불러왔습니다", load_latest)

    def test_non_retryable_publish_and_missing_server_session_have_safe_exits(self):
        banner = self.app.split("function OperationRecoveryBanner", 1)[1].split(
            "function LoadingOverlay", 1
        )[0]
        publish = self.app.split("const onPublish = async", 1)[1].split(
            "const handlePublishDownload", 1
        )[0]
        retry = self.app.split("const retryRecoveryOperation", 1)[1].split(
            "const runRecoveryAlternative", 1
        )[0]
        self.assertIn("const retryDisallowed = error.retryable === false && !hasConflict", banner)
        self.assertIn("anyBusy || retryDisallowed || conflictNeedsResolution", banner)
        self.assertIn("권장 조치 후 다시 시도", banner)
        self.assertIn("recovery.error?.retryable === false && !recovery.conflict", retry)
        self.assertIn("resp.status === 404", publish)
        self.assertIn("missingSessionError.code = 'session_missing'", publish)
        self.assertIn("missingSessionError.hasLatestSessionState = true", publish)
        self.assertIn("await fetchLatestSessionState()", publish)
        self.assertIn("버그 리포트", banner)
        self.assertIn("onReset", banner)
        self.assertIn("disabled={anyBusy || resetBlocked}", banner)
        reset_from_recovery = self.app.split("const resetAfterOperationError", 1)[1].split(
            "const runRecoveryAlternative", 1
        )[0]
        self.assertIn("session: conflict.latestSession || null", reset_from_recovery)
        self.assertIn("captureSessionRevision(payload)", reset_from_recovery)
        self.assertIn("void resetSession()", reset_from_recovery)

    def test_recovery_banner_exposes_server_steps_and_hides_unavailable_download_alternative(self):
        banner = self.app.split("function OperationRecoveryBanner", 1)[1].split(
            "function LoadingOverlay", 1
        )[0]
        self.assertIn("const recoverySteps = Array.isArray(error.recoverySteps)", banner)
        self.assertIn('className="operation-recovery-steps"', banner)
        self.assertIn('aria-label="권장 해결 순서"', banner)
        self.assertIn("(!isDownload || canExport)", banner)
        self.assertIn("EDB 다운로드를 다시 시도해 주세요", banner)
        self.assertIn("error.code === 'artifact_cleanup_busy'", banner)
        self.assertIn('role="region"', banner)
        self.assertIn('role="group" aria-label="오류 복구 작업"', banner)
        self.assertIn("disabled={anyBusy || hasConflict}", banner)

    def test_unresolved_conflict_keeps_recovery_choices_visible_and_blocks_hidden_workspace_actions(self):
        app = self.app.split("function App()", 1)[1]
        self.assertIn(
            "const hasPendingSessionConflict = Boolean(operationRecovery?.conflict);",
            app,
        )
        self.assertIn("conflictBlocked={hasPendingSessionConflict}", app)
        self.assertIn("inert={hasPendingSessionConflict ? '' : undefined}", app)
        self.assertIn("hasRunningQueueRecognition || hasPendingSessionConflict", app)
        self.assertIn("mutating={mutating || hasPendingSessionConflict}", app)


if __name__ == "__main__":
    unittest.main()
