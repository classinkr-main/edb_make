from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


class TestUiReviewBulkConfirm(unittest.TestCase):
    def test_review_stage_exposes_bulk_confirm_actions(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        review_stage = source.split("function ReviewStage", 1)[1]
        review_stage = review_stage.split("// ─── LEFT:", 1)[0]

        self.assertIn("onConfirm", review_stage)
        self.assertIn("actionableProblemIds", review_stage)
        self.assertIn("모두 확인", review_stage)
        self.assertIn("표시 항목 확인", review_stage)
        self.assertIn("onConfirm?.(null, { problemIds: actionableProblemIds, bulk: true })", review_stage)
        self.assertIn("onConfirm?.(null, { problemIds: visibleReviewScope.problemIds, bulk: true })", review_stage)

    def test_app_passes_confirm_handler_to_review_stage(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        review_usage = source.split("<ReviewStage", 1)[1]
        review_usage = review_usage.split("/>", 1)[0]

        self.assertIn("onConfirm={onConfirm}", review_usage)

    def test_on_confirm_accepts_explicit_problem_ids(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        on_confirm = source.split("const onConfirm = async (id, options = {}) => {", 1)[1]
        on_confirm = on_confirm.split("  const onPublish = async () => {", 1)[0]

        self.assertIn("options.problemIds", on_confirm)
        self.assertIn("전체 ${confirmedIds.size}개 확인 완료", on_confirm)
        self.assertIn("await mutateSession('confirm', { problemIds: [...confirmedIds] })", on_confirm)
        self.assertNotIn("postRestore(nextSession)", on_confirm)
        self.assertIn("return nextSession;", on_confirm)
        self.assertIn("return false;", on_confirm)

    def test_final_and_bulk_confirmation_move_directly_to_board_preview(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        on_confirm = source.split("const onConfirm = async (id, options = {}) => {", 1)[1]
        on_confirm = on_confirm.split("  const onPublish = async () => {", 1)[0]

        self.assertIn("const beforeFlow = reviewFlowState(sessionReviewSummary(session));", on_confirm)
        self.assertIn("const afterFlow = reviewFlowState(sessionReviewSummary(nextSession));", on_confirm)
        self.assertIn("if (afterFlow.complete && (beforeFlow.remaining > 0 || options.bulk))", on_confirm)
        self.assertIn("requestViewChange('board', { force: true });", on_confirm)
        self.assertIn("검수 완료 · 칠판 미리보기로 이동했어요", on_confirm)
        self.assertIn("일괄 확인 완료 · 칠판 미리보기로 이동했어요", on_confirm)

    def test_bulk_confirmation_moves_mock_session_to_board_preview(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        on_confirm = source.split("const onConfirm = async (id, options = {}) => {", 1)[1]
        on_confirm = on_confirm.split("  const onPublish = async () => {", 1)[0]

        self.assertIn("const allItemsConfirmedByBulk = options.bulk", on_confirm)
        self.assertIn("items.every(item => confirmedIds.has(item.id))", on_confirm)
        self.assertIn("if (allItemsConfirmedByBulk)", on_confirm)
        self.assertIn("setReviewFocus(null);", on_confirm)

    def test_completed_restored_session_opens_on_board_preview(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        should_open_review = source.split("function shouldOpenReview(session){", 1)[1]
        should_open_review = should_open_review.split("\n}", 1)[0]

        self.assertIn(
            "return reviewFlowState(sessionReviewSummary(session)).remaining > 0;",
            should_open_review,
        )
        self.assertNotIn("problemCount > 1", should_open_review)

    def test_board_image_columns_default_to_one_in_source_and_bundle(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        bundle = (PROJECT_ROOT / "ui_prototype" / "app.bundle.js").read_text(encoding="utf-8")

        self.assertIn('"boardColumns": 1', source)
        self.assertIn('"boardColumns":1', bundle)

    def test_review_stage_exposes_persistent_completion_bar(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        html = (PROJECT_ROOT / "ui_prototype" / "board.html").read_text(encoding="utf-8")
        review_stage = source.split("function ReviewStage", 1)[1]
        review_stage = review_stage.split("// ─── LEFT:", 1)[0]

        self.assertIn("review-completion-bar", review_stage)
        self.assertIn("확인하면 다음 항목이 자동으로 선택됩니다.", review_stage)
        self.assertIn("마지막 확인 · 칠판 보기", review_stage)
        self.assertIn("확인하고 다음", review_stage)
        self.assertIn("칠판 미리보기", review_stage)
        self.assertIn("review-board-preview-button", review_stage)
        self.assertIn("onOpenBoard", review_stage)
        self.assertIn(".review-completion-bar", html)
        self.assertIn(".review-completion-primary", html)
        self.assertIn(".review-completion-shortcut", html)
        completion_css = html.split(".review-completion-bar{", 1)[1].split("}", 1)[0]
        primary_css = html.split(".review-completion-primary{", 1)[1].split("}", 1)[0]
        self.assertIn("min-height: 54px", completion_css)
        self.assertIn("min-height: 38px", primary_css)
        board_preview_css = html.split(".review-board-preview-button{", 1)[1].split("}", 1)[0]
        self.assertIn("justify-content: center", board_preview_css)
        self.assertIn("gap: 6px", board_preview_css)

    def test_review_confirmation_advances_and_has_a_keyboard_shortcut(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        review_stage = source.split("function ReviewStage", 1)[1]
        review_stage = review_stage.split("// ─── LEFT:", 1)[0]

        self.assertIn("const confirmSelectedAndAdvance = useCallback(async () => {", review_stage)
        self.assertIn("nextUnresolvedReviewTargetAfter(confirmedSession, anchorPageId", review_stage)
        self.assertIn("nextUnresolvedReviewTargetAfter(\n      confirmedSession,\n      normalizedPageId", review_stage)
        self.assertIn("pendingReviewSelectionProblemIdRef.current = nextProblemId || ''", review_stage)
        self.assertIn("pendingReviewSelectionProblemIdRef.current = nextTargetId || ''", review_stage)
        self.assertIn("focusReviewProblem(nextProblemId)", review_stage)
        self.assertIn("normalizedProblemId.startsWith('page:')", review_stage)
        self.assertIn("const focusedUnresolvedPageTarget", review_stage)
        self.assertIn("target.id === focusedPageReviewTargetId", review_stage)
        self.assertIn("const nextUnresolvedTarget = focusedUnresolvedPageTarget", review_stage)
        self.assertIn("페이지 검수 시작", review_stage)
        self.assertIn("지문 없음 확인하고 다음", review_stage)
        self.assertIn("mutateSession?.('confirm-page'", review_stage)
        self.assertIn("reviewFlowState(confirmedSummary).complete", review_stage)
        self.assertIn("onOpenBoard?.()", review_stage)
        self.assertIn("setPendingReviewNavigation", review_stage)
        self.assertIn("useLayoutEffect(() =>", review_stage)
        self.assertIn("const navigationDeadline = performance.now() + 1500", review_stage)
        self.assertIn("performance.now() < navigationDeadline", review_stage)
        self.assertIn("cancelSmoothScroll(reviewWrapRef.current)", review_stage)
        self.assertIn("evt.key !== 'Enter' || (!evt.metaKey && !evt.ctrlKey)", review_stage)
        self.assertIn('aria-keyshortcuts="Meta+Enter Control+Enter"', review_stage)
        self.assertIn("review-completion-shortcut", review_stage)
        self.assertNotIn("그대로 확인", review_stage)

    def test_review_target_navigation_uses_page_order_instead_of_payload_order(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        helper = source.split("function nextUnresolvedReviewTargetAfter", 1)[1]
        helper = helper.split("function ReviewStage", 1)[0]

        self.assertIn("let nextTargetIndex = Number.POSITIVE_INFINITY", helper)
        self.assertIn("targetIndex < nextTargetIndex", helper)
        self.assertIn("let wrapTargetIndex = Number.POSITIVE_INFINITY", helper)
        self.assertIn("nextTarget?.id || wrapTarget?.id", helper)

    def test_review_screen_has_only_one_confirmation_domain(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        side_panel = source.split("function SidePanel", 1)[1]
        side_panel = side_panel.split("function LoadingOverlay", 1)[0]
        box_editor = source.split("function BoxEditPanel", 1)[1].split("function ManualSplitEditor", 1)[0]

        self.assertIn("view !== 'review'", side_panel)
        self.assertIn("처리 방식 적용", side_panel)
        self.assertIn("전체 처리 적용", side_panel)
        self.assertNotIn("<button className=\"btn\" disabled={!item}>건너뛰기</button>", side_panel)
        self.assertIn("합치고 다음 검수", box_editor)

    def test_review_top_toolbar_prioritizes_status_remaining_and_quick_actions(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        html = (PROJECT_ROOT / "ui_prototype" / "board.html").read_text(encoding="utf-8")
        review_stage = source.split("function ReviewStage", 1)[1]
        review_stage = review_stage.split("// ─── LEFT:", 1)[0]

        self.assertIn("function ReviewFilterTabs", source)
        self.assertIn('aria-pressed={value === filterValue}', source)
        self.assertIn('className="stage-toolbar review-stage-toolbar"', source)
        self.assertIn('className="review-view-control-group"', source)
        self.assertIn("`남은 확인 ${reviewFlow.remaining}`", review_stage)
        self.assertIn('className="review-toolbar-actions"', review_stage)
        self.assertNotIn('label="상태 보기"', review_stage)
        self.assertNotIn('label="일괄 작업"', review_stage)
        self.assertNotIn("review-toolbar-guidance", review_stage)
        self.assertNotIn("표시 항목 선택", review_stage)
        self.assertIn(".review-toolbar-actions", html)
        self.assertIn(".review-toolbar-action:focus-visible", html)
        self.assertIn(".review-stage-heading", html)

    def test_review_diagnostics_are_collapsed_behind_one_disclosure(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        html = (PROJECT_ROOT / "ui_prototype" / "board.html").read_text(encoding="utf-8")
        review_stage = source.split("function ReviewStage", 1)[1]
        review_stage = review_stage.split("// ─── LEFT:", 1)[0]

        self.assertIn("const [reviewDiagnosticsOpen, setReviewDiagnosticsOpen] = useState(false)", review_stage)
        self.assertIn("aria-expanded={reviewDiagnosticsOpen}", review_stage)
        self.assertIn('aria-controls="review-diagnostics-detail"', review_stage)
        self.assertIn("상세 진단", review_stage)
        self.assertIn('id="review-diagnostics-detail" className="review-summary-details"', review_stage)
        self.assertIn(".review-summary-overview", html)
        self.assertIn(".review-summary-details", html)

    def test_board_uses_review_bulk_confirm_cache_bust(self) -> None:
        html = (PROJECT_ROOT / "ui_prototype" / "board.html").read_text(encoding="utf-8")

        self.assertIn("review_filters.js?v=review-mode-copy-20260818", html)
        self.assertIn("app.bundle.js?v=frontend-bundle-", html)
        self.assertNotIn("app.js?v=", html)

    def test_items_rail_uses_session_mode_for_filters_and_recent_counts(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        rail = source.split("function ItemsRail", 1)[1]
        rail = rail.split("function StageBoard", 1)[0]

        self.assertIn("sessionReviewMode?.(session)", rail)
        self.assertIn("['questions', '페이지 원본', materialCounts.questions]", rail)
        self.assertIn("recentSessionCountLabel(entry)", rail)
        review_usage = source.split("<ItemsRail", 1)[1].split("/>", 1)[0]
        self.assertIn("session={session}", review_usage)


if __name__ == "__main__":
    unittest.main()
