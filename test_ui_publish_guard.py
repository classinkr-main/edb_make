from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


class TestUiPublishGuard(unittest.TestCase):
    def test_publish_uses_synchronous_single_flight_guard(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        on_publish = source.split("const onPublish = async () => {", 1)[1]
        on_publish = on_publish.split("const handlePublishDownload", 1)[0]

        self.assertIn("const publishInFlightRef = useRef(false);", source)
        self.assertIn("if (publishInFlightRef.current)", on_publish)
        self.assertIn("이미 EDB를 제작 중입니다", on_publish)
        self.assertIn("publishInFlightRef.current = true;", on_publish)
        self.assertIn("setPublishBusy(true);", on_publish)
        self.assertIn("publishInFlightRef.current = false;", on_publish)
        self.assertIn("setPublishBusy(false);", on_publish)
        # The publish payload is assembled before the in-flight flag is set, so an
        # early bail-out cannot leave the guard stuck on. The payload moved from an
        # inline `placements` map to prepareSessionPublishRequest(); the order rule
        # is the same.
        self.assertIn("prepareSessionPublishRequest(", on_publish)
        self.assertLess(
            on_publish.index("const publishRequest ="),
            on_publish.index("publishInFlightRef.current = true;"),
        )
        self.assertIn(
            "operationBusy={Boolean(loading) || resetBusy || publishBusy || downloadBusy || hasPendingSessionConflict}",
            source,
        )
        self.assertIn("if (operationRecovery?.conflict)", on_publish)
        self.assertLess(
            on_publish.index("if (operationRecovery?.conflict)"),
            on_publish.index("clearOperationRecovery();"),
        )

    def test_publish_warns_and_returns_to_review_when_actionable_items_remain(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        on_publish = source.split("const onPublish = async () => {", 1)[1]
        on_publish = on_publish.split("  return (", 1)[0]
        warning_helper = source.split("function publishReviewWarningMessage", 1)[1]
        warning_helper = warning_helper.split("function normalizePublishSummary", 1)[0]

        self.assertIn("sessionReviewSummary(sessionForPublish)", on_publish)
        self.assertIn("publishReviewWarningMessage(sessionForPublish, publishReviewSummary)", on_publish)
        self.assertIn("actionableNeedsReviewCount", warning_helper)
        self.assertIn("window.confirm", on_publish)
        self.assertIn("setView('review')", on_publish)
        self.assertIn("검수 화면", warning_helper)

    def test_publish_warning_mentions_remaining_passage_review_queue(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        on_publish = source.split("const onPublish = async () => {", 1)[1]
        on_publish = on_publish.split("  return (", 1)[0]
        warning_helper = source.split("function publishReviewWarningMessage", 1)[1]
        warning_helper = warning_helper.split("function normalizePublishSummary", 1)[0]

        self.assertIn("publishReviewWarningMessage(sessionForPublish, publishReviewSummary)", on_publish)
        self.assertIn("passageReviewItemCount", warning_helper)
        self.assertIn("passageReviewLabel", warning_helper)
        self.assertIn("passageReviewPreview", warning_helper)
        self.assertIn("지문 확인", warning_helper)

    def test_publish_blocks_board_placement_overlap_before_request(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        on_publish = source.split("const onPublish = async () => {", 1)[1]
        on_publish = on_publish.split("  return (", 1)[0]

        self.assertIn("findBoardPlacementOverlaps", on_publish)
        self.assertIn("findBoardPlacementOverlaps(itemsForPublish", on_publish)
        self.assertIn("layoutGapMode,", on_publish)
        self.assertIn("resolvedPlacements: true", on_publish)
        self.assertIn("board_placement_overlap", on_publish)
        self.assertIn("문항 배치가 겹칠 수 있어", on_publish)
        self.assertIn("setView('board')", on_publish)

    def test_publish_blocks_source_problem_bbox_overlap_before_request(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        on_publish = source.split("const onPublish = async () => {", 1)[1]
        on_publish = on_publish.split("  return (", 1)[0]

        self.assertIn("findSourceProblemOverlaps", on_publish)
        self.assertIn("source_problem_bbox_overlap", on_publish)
        self.assertIn("firstIssue.problemIds || firstIssue.problem_ids", on_publish)
        self.assertIn("problemIds: focusProblemIds", on_publish)
        self.assertIn("source: 'source-overlap-preflight'", on_publish)
        self.assertIn("문항 영역이 겹칠 수 있어", on_publish)
        self.assertIn("setView('review')", on_publish)

    def test_publish_does_not_block_duplicate_problem_number_groups_before_request(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        on_publish = source.split("const onPublish = async () => {", 1)[1]
        on_publish = on_publish.split("  return (", 1)[0]

        self.assertNotIn("duplicateProblemNumberGroups.flatMap", on_publish)
        self.assertNotIn("source: 'duplicate-number-preflight'", on_publish)
        self.assertNotIn("중복 문항 번호가 있어 제작을 멈췄어요", on_publish)

    def test_publish_blocks_passage_group_source_reuse_before_request(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        on_publish = source.split("const onPublish = async () => {", 1)[1]
        on_publish = on_publish.split("  return (", 1)[0]

        self.assertIn("findPassageGroupSourceReuse", on_publish)
        self.assertIn("passage_group_source_reuse", on_publish)
        self.assertIn("firstIssue.problemIds || firstIssue.problem_ids", on_publish)
        self.assertIn("problemIds: focusProblemIds", on_publish)
        self.assertIn("source: 'passage-source-reuse-preflight'", on_publish)
        self.assertIn("지문 묶음 안에서 원본 영역이 반복될 수 있어", on_publish)
        self.assertIn("setView('review')", on_publish)

    def test_publish_surfaces_server_preflight_block_response(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        on_publish = source.split("const onPublish = async () => {", 1)[1]
        on_publish = on_publish.split("  return (", 1)[0]
        target_helper = source.split("function publishBlockedTarget", 1)[1]
        target_helper = target_helper.split("const NON_ACTIONABLE_RISK_FLAGS", 1)[0]
        review_stage = source.split("function ReviewStage", 1)[1]
        review_stage_signature = review_stage.split("){", 1)[0]
        review_stage_focus = review_stage.split("const onBoxClick", 1)[0]

        self.assertIn("normalizePublishPreflightBlock(json)", on_publish)
        self.assertIn("publishBlockedTarget(blockedPublish)", on_publish)
        self.assertIn("applyPublishPreflightIssuesToSession(sessionForPublish, blockedPublish)", on_publish)
        self.assertIn("pageChromePreflightBlockToast(blockedPublish)", on_publish)
        self.assertIn("setReviewFocus(blockedTarget.reviewFocus)", on_publish)
        self.assertIn("reviewFocus={reviewFocus}", source)
        self.assertIn("reviewFocus", review_stage_signature)
        self.assertIn("reviewFocus?.problemIds", review_stage_focus)
        self.assertIn("setSelectedIds(new Set(focusedProblemIds))", review_stage_focus)
        self.assertIn("setActive(focusedProblemIds[0])", review_stage_focus)
        self.assertIn("blockedPublish.issueSummaryLabel", on_publish)
        self.assertIn("passage_review_queue_remaining", target_helper)
        self.assertIn("passage-review", target_helper)
        self.assertIn("source_problem_bbox_overlap", target_helper)
        self.assertIn("duplicate_problem_number", target_helper)
        self.assertIn("passage_group_source_reuse", target_helper)
        self.assertIn("step2_page_chrome_artifact_rate", target_helper)
        self.assertIn("step3_page_chrome_artifact", target_helper)
        self.assertIn("blockedPublish?.blockingProblemIds", target_helper)
        self.assertIn("problemIds: focusedProblemIds", target_helper)
        self.assertIn("서버 사전점검", on_publish)
        self.assertIn("setView('review')", on_publish)

    def test_publish_blocked_page_chrome_artifacts_get_retry_ui(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        review_stage = source.split("function ReviewStage", 1)[1]
        review_stage = review_stage.split("// ─── LEFT:", 1)[0]
        review_usage = source.split("<ReviewStage", 1)[1]
        review_usage = review_usage.split("/>", 1)[0]
        html = (PROJECT_ROOT / "ui_prototype" / "board.css").read_text(encoding="utf-8")

        self.assertIn("hasPageChromeArtifactFlag", source)
        self.assertIn("selectedPageChromeProblemIds", review_stage)
        self.assertIn("onEnhanceImage?.(selectedPageChromeProblemIds, { mode: 'preserve' })", review_stage)
        self.assertIn("imageEnhanceBusy", review_stage)
        self.assertIn("3단계 원문 보존", review_stage)
        self.assertIn("page-chrome-artifact", review_stage)
        self.assertIn("onEnhanceImage={enhanceImageSession}", review_usage)
        self.assertIn("imageEnhanceBusy={hasRunningImageEnhance}", review_usage)
        self.assertIn(".item.page-chrome-artifact", html)
        self.assertIn(".stage-tile.page-chrome-artifact", html)
        self.assertIn(".review-bbox.page-chrome-artifact", html)

    def test_board_uses_publish_guard_cache_bust(self) -> None:
        html = (PROJECT_ROOT / "ui_prototype" / "board.html").read_text(encoding="utf-8")

        self.assertIn("app.bundle.js?v=frontend-bundle-", html)
        self.assertNotIn("app.js?v=", html)
        self.assertIn("publish_guard.js?v=preflight-layout-gap-fit-slot-20261118", html)


if __name__ == "__main__":
    unittest.main()
