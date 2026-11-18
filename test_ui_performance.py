from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def board_stylesheet() -> str:
    """The editor theme lives in board.css; board.html only links it."""

    return (PROJECT_ROOT / "ui_prototype" / "board.css").read_text(encoding="utf-8")


class TestUiPerformance(unittest.TestCase):
    def setUp(self) -> None:
        self.source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")

    def test_board_scroll_state_is_coalesced_per_animation_frame(self) -> None:
        board_stage = self.source.split("function BoardStage", 1)[1]
        board_stage = board_stage.split("function downloadPublishSummary", 1)[0]

        self.assertIn("const scrollSyncFrameRef = useRef(null)", board_stage)
        self.assertIn("if (scrollSyncFrameRef.current != null) return", board_stage)
        self.assertIn("scrollSyncFrameRef.current = window.requestAnimationFrame", board_stage)
        self.assertIn("window.cancelAnimationFrame(scrollSyncFrameRef.current)", board_stage)

    def test_initial_session_fetch_is_not_restarted_by_column_changes(self) -> None:
        app = self.source.split("function App(){", 1)[1]
        apply_session = app.split("const applySession = useCallback", 1)[1]
        apply_session = apply_session.split("// Replace state from a mutation response", 1)[0]
        initial_fetch = app.split("// initial session fetch", 1)[1]
        initial_fetch = initial_fetch.split("// load user settings", 1)[0]

        self.assertIn("const boardColumnsRef = useRef(boardColumns)", app)
        self.assertIn("boardColumnsRef.current = boardColumns", app)
        self.assertIn("boardColumnsRef.current", apply_session)
        self.assertIn("}, []);", apply_session)
        self.assertIn("}, [applySession]);", initial_fetch)
        self.assertNotIn("}, [boardColumns]);", apply_session)

    def test_elapsed_time_updates_do_not_run_twice_per_second(self) -> None:
        loading = self.source.split("function LoadingOverlay", 1)[1]
        loading = loading.split("function RecognitionPageReviewStage", 1)[0]

        self.assertNotIn("setInterval(tick, 500)", loading)
        self.assertNotIn("setInterval(() => setNow(Date.now()), 500)", loading)
        self.assertGreaterEqual(loading.count("1000"), 3)

    def test_background_job_interval_depends_on_stable_boolean(self) -> None:
        panel = self.source.split("function BackgroundJobsPanel", 1)[1]
        panel = panel.split("function RecognitionCancelBanner", 1)[0]

        self.assertIn("const hasRunningJob = visibleJobs.some", panel)
        self.assertIn("}, [hasRunningJob]);", panel)
        self.assertNotIn("}, [visibleJobs]);", panel)

    def test_global_pointer_tracking_is_passive(self) -> None:
        tooltip = self.source.split("function TooltipLayer", 1)[1]
        tooltip = tooltip.split("function TopBar", 1)[0]

        self.assertIn("if (event.buttons)", tooltip)
        self.assertIn(
            "document.addEventListener('pointermove', onPointerMove, { capture: true, passive: true })",
            tooltip,
        )

    def test_rail_drag_hit_testing_is_coalesced_per_animation_frame(self) -> None:
        items_rail = self.source.split("function ItemsRail({", 1)[1]
        items_rail = items_rail.split("function BoardStage({", 1)[0]
        pointer_move = items_rail.split("const movePointerDrag = (event) => {", 1)[1]
        pointer_move = pointer_move.split("const finishPointerDrag", 1)[0]

        self.assertIn("sourceIdSet: new Set(sourceIds)", items_rail)
        self.assertIn("dragVisualFrameRef.current = window.requestAnimationFrame", items_rail)
        self.assertIn("activeDrag.sourceIdSet", items_rail)
        self.assertNotIn("findPointerDropTarget(", pointer_move)
        self.assertIn("for (const { item } of visibleItemRows)", items_rail)

    def test_tile_images_decode_async_and_gpu_hints_are_drag_scoped(self) -> None:
        board = board_stylesheet()
        tile_image = self.source.split("function TileImage", 1)[1]
        tile_image = tile_image.split("function canPreviewImageFile", 1)[0]
        base_tile_css = board.split(".stage-tile{", 1)[1].split("}", 1)[0]
        positioning_css = board.split(".stage-tile.positioning{", 1)[1].split("}", 1)[0]

        self.assertIn('loading="lazy"', tile_image)
        self.assertIn('decoding="async"', tile_image)
        self.assertNotIn("will-change", base_tile_css)
        self.assertIn("will-change: transform", positioning_css)

    def test_stage_tile_contain_image_is_top_aligned_without_changing_its_box(self) -> None:
        board = board_stylesheet()
        base_image_css = board.split(".tile-img{", 1)[1].split("}", 1)[0]
        stage_image_css = board.split(".stage-tile .tile-img{", 1)[1].split("}", 1)[0]

        self.assertIn("width: 100%; height: 100%", base_image_css)
        self.assertIn("object-fit: contain", base_image_css)
        self.assertIn("object-position: center top", stage_image_css)

    def test_board_estimate_bar_reserves_space_without_covering_the_board(self) -> None:
        board = board_stylesheet()
        stage_css = board.split("/* Stage (big board) */", 1)[1]
        stage_wrap_css = stage_css.split(".stage-wrap{", 1)[1].split("}", 1)[0]
        stage_board_css = stage_css.split(".stage-board{", 1)[1].split("}", 1)[0]
        estimate_css = stage_css.split(".stage-estimate-bar{", 1)[1].split("}", 1)[0]

        self.assertIn("display: flex", stage_wrap_css)
        self.assertIn("flex-direction: column", stage_wrap_css)
        self.assertIn("gap: 10px", stage_wrap_css)
        self.assertIn("max-width: var(--stage-preview-max-width)", stage_board_css)
        self.assertIn("max-width: var(--stage-preview-max-width)", estimate_css)
        self.assertIn(".stage-wrap:has(.stage-estimate-bar)", board)

    def test_board_estimate_contract_wraps_and_keeps_controls_touch_sized(self) -> None:
        board = board_stylesheet()
        estimate_track_css = board.split(".stage-estimate-track,", 1)[1].split("}", 1)[0]
        estimate_action_css = board.split(".stage-estimate-action{", 1)[1].split("}", 1)[0]

        self.assertIn("flex-wrap: wrap", estimate_track_css)
        self.assertIn("min-width: 44px", estimate_action_css)
        self.assertIn("min-height: 44px", estimate_action_css)
        self.assertIn("@media (max-width: 1100px)", board)
        self.assertIn(".stage-estimate-track", board)

    def test_board_grid_and_active_slot_guides_are_visible_but_noninteractive(self) -> None:
        board = board_stylesheet()
        grid_css = board.split(".page-divider.is-classin-grid{", 1)[1].split("}", 1)[0]
        slot_gap_css = board.split(".active-slot-gap{", 1)[1].split("}", 1)[0]
        next_boundary_css = board.split(".active-next-boundary{", 1)[1].split("}", 1)[0]
        active_tile_css = board.split(".stage-tile.active{", 1)[1].split("}", 1)[0]

        self.assertIn("rgba(244,237,224,.28)", grid_css)
        self.assertIn("pointer-events: none", slot_gap_css)
        self.assertIn("rgba(158,201,240,.055)", slot_gap_css)
        self.assertIn("pointer-events: none", next_boundary_css)
        self.assertIn("outline: 2px solid", active_tile_css)
        self.assertIn(".stage-tile.paper.active", board)

    def test_recognition_preview_scrolls_all_pages_but_lazily_loads_later_images(self) -> None:
        stage = self.source.split("function RecognitionPageReviewStage", 1)[1]
        stage = stage.split("function TileImage", 1)[0]

        self.assertIn("pageRows.map((row, pageIndex) =>", stage)
        self.assertIn("pageRows.map(({ page, problems: pageProblems }, pageIndex)", stage)
        self.assertIn('data-recognition-page-index={pageIndex}', stage)
        self.assertIn("loading={pageIndex < 2 ? 'eager' : 'lazy'}", stage)
        self.assertEqual(stage.count("filePreviewUrl(page.sourceImageUri)"), 1)
        self.assertIn('decoding="async"', stage)
        self.assertIn("fetchPriority={pageIndex < 2 ? 'high' : 'auto'}", stage)

    def test_center_panels_request_display_sized_images_only(self) -> None:
        self.assertIn("const CENTER_PANEL_PREVIEW_MAX_DIMENSION = 1024", self.source)
        self.assertGreaterEqual(
            self.source.count("filePreviewUrl(page.sourceImageUri)"),
            4,
        )
        self.assertIn(
            "<TileImage item={it} previewMaxDimension={CENTER_PANEL_PREVIEW_MAX_DIMENSION} />",
            self.source,
        )
        tile_image = self.source.split("function TileImage", 1)[1]
        tile_image = tile_image.split("function canPreviewImageFile", 1)[0]
        self.assertIn("filePreviewUrl(url, previewMaxDimension)", tile_image)
        self.assertIn("const displayUrl = previewMaxDimension", tile_image)
        self.assertIn("src={displayUrl}", tile_image)

    def test_mobile_sidebar_keeps_scrolling_and_selection_tools_compact(self) -> None:
        board = board_stylesheet()

        self.assertIn("touch-action: pan-y", board)
        self.assertIn(".item .grip{", board)
        self.assertIn(".stage-tile .tile-hd{", board)
        self.assertIn("touch-action: none", board)
        self.assertIn("overscroll-behavior: contain", board)
        self.assertIn("scrollbar-gutter: stable", board)
        self.assertIn("overscroll-behavior-inline: contain", board)
        self.assertIn(".problem-order-status .rail-selection-tools{", board)
        self.assertIn(".items .problem-order-status.is-selection > span{", board)

    def test_smooth_scroll_respects_reduced_motion(self) -> None:
        smooth_scroll = self.source.split("function smoothScrollTo", 1)[1]
        smooth_scroll = smooth_scroll.split("const Icon = {", 1)[0]

        self.assertIn("prefers-reduced-motion: reduce", smooth_scroll)
        self.assertIn("duration = 0", smooth_scroll)

    def test_review_scroll_zoom_and_images_are_stable_for_long_documents(self) -> None:
        review_stage = self.source.split("function ReviewStage", 1)[1]
        review_stage = review_stage.split("// ─── LEFT:", 1)[0]
        board = board_stylesheet()

        self.assertIn("const reviewScrollSyncFrameRef = useRef(null)", review_stage)
        self.assertIn("if (reviewScrollSyncFrameRef.current != null) return", review_stage)
        self.assertIn("reviewScrollSyncFrameRef.current = window.requestAnimationFrame", review_stage)
        self.assertIn("reviewWheelDeltaRef.current += evt.deltaY", review_stage)
        self.assertIn("if (reviewWheelFrameRef.current != null) return", review_stage)
        self.assertIn("cancelSmoothScroll(reviewWrapRef.current)", review_stage)
        self.assertIn("width={Math.max(1, Number(page.width) || 1)}", review_stage)
        self.assertIn("height={Math.max(1, Number(page.height) || 1)}", review_stage)
        self.assertIn("loading={pageIndex < 2 ? 'eager' : 'lazy'}", review_stage)
        self.assertIn('decoding="async"', review_stage)
        self.assertIn("scrollbar-gutter: stable", board)

    def test_large_upload_is_rejected_before_base64_encoding(self) -> None:
        post_export = self.source.split("async function postExport", 1)[1]
        post_export = post_export.split("function formatApiError", 1)[0]

        self.assertIn("const MAX_EXPORT_JSON_BYTES = 64 * 1024 * 1024", self.source)
        self.assertIn("function estimatedExportPayloadBytes", self.source)
        self.assertIn("function assertExportPayloadFits", self.source)
        self.assertLess(
            post_export.index("assertExportPayloadFits(files)"),
            post_export.index("Promise.all(files.map"),
        )
        self.assertIn("파일을 나누어 등록하거나 PDF를 압축해 주세요", self.source)
        self.assertIn("`${fallbackMessage} · 파일을 나누어 등록하세요`", self.source)


if __name__ == "__main__":
    unittest.main()
