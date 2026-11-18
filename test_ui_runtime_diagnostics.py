from __future__ import annotations

import subprocess
import unittest
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent

def board_document() -> str:
    """board.html plus the stylesheet it links.

    The editor theme used to be an inline <style> block; it now lives in
    ui_prototype/board.css. These assertions cover both halves of the document.
    """

    ui_root = PROJECT_ROOT / "ui_prototype"
    return (
        (ui_root / "board.html").read_text(encoding="utf-8")
        + "\n"
        + (ui_root / "board.css").read_text(encoding="utf-8")
    )



def run_node(script: str) -> None:
    subprocess.run(["node", "-e", script], cwd=PROJECT_ROOT, check=True)


class TestUiRuntimeDiagnostics(unittest.TestCase):
    def test_review_target_queue_keeps_empty_page_progress_stable(self) -> None:
        run_node(
            r"""
            const fs = require('fs');
            const vm = require('vm');
            const source = fs.readFileSync('./ui_prototype/app.jsx', 'utf8');
            const start = source.indexOf('const REVIEW_STATUS_META =');
            const end = source.indexOf('function normalizePublishSummary');
            if (start < 0 || end < 0) throw new Error('review helper bounds not found');
            const sandbox = {};
            sandbox.globalThis = sandbox;
            vm.runInNewContext(source.slice(start, end), sandbox);

            const session = {
              problems: [
                { id: 'p1', sourcePageId: 'page-1', reviewStatus: 'normal', bbox: { width: 10, height: 10 } },
                { id: 'p2', sourcePageId: 'page-2', reviewStatus: 'normal', bbox: { width: 10, height: 10 } },
              ],
              pages: [
                { id: 'page-1', problemIds: ['p1'], reviewStatus: 'normal' },
                { id: 'page-2', problemIds: ['p2'], reviewStatus: 'normal' },
                { id: 'page-3', problemIds: [], reviewStatus: 'failed', riskFlags: [] },
                { id: 'page-4', problemIds: [], reviewStatus: 'failed', riskFlags: [] },
              ],
            };
            const before = sandbox.sessionReviewSummary(session);
            const beforeFlow = sandbox.reviewFlowState(before);
            if (beforeFlow.total !== 4 || beforeFlow.reviewed !== 2 || beforeFlow.remaining !== 2) {
              throw new Error(`unexpected initial flow: ${JSON.stringify(beforeFlow)}`);
            }
            if (before.reviewTargetStatusCounts.check_needed !== 2 || before.reviewTargetStatusCounts.failed !== 0) {
              throw new Error(`unexpected target counts: ${JSON.stringify(before.reviewTargetStatusCounts)}`);
            }
            if (before.unresolvedReviewTargets.map(target => target.id).join(',') !== 'page:page-3,page:page-4') {
              throw new Error(`unexpected queue: ${JSON.stringify(before.unresolvedReviewTargets)}`);
            }

            session.pages[2] = {
              ...session.pages[2],
              reviewStatus: 'normal',
              pageReviewConfirmed: true,
              pageReviewDecision: 'no_passage',
            };
            const afterFlow = sandbox.reviewFlowState(sandbox.sessionReviewSummary(session));
            if (afterFlow.total !== 4 || afterFlow.reviewed !== 3 || afterFlow.remaining !== 1) {
              throw new Error(`progress denominator changed after confirmation: ${JSON.stringify(afterFlow)}`);
            }
            """
        )

    def test_keyboard_help_uses_native_platform_modifiers(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        platform_block = "const RUNTIME_PLATFORM" + source.split(
            "const RUNTIME_PLATFORM", 1
        )[1].split("function reportRuntimeDiagnostic", 1)[0]
        run_node(
            """
            const vm = require('vm');
            function labels(platform) {
              const sandbox = { navigator: { platform } };
              sandbox.globalThis = sandbox;
              vm.runInNewContext(process.env.PLATFORM_BLOCK + `
                globalThis.labels = {
                  primary: PRIMARY_MODIFIER_LABEL,
                  primaryName: PRIMARY_MODIFIER_NAME,
                  alternate: ALTERNATE_MODIFIER_LABEL,
                  alternateName: ALTERNATE_MODIFIER_NAME,
                };
              `, sandbox);
              return sandbox.labels;
            }
            const mac = labels('MacIntel');
            if (JSON.stringify(mac) !== JSON.stringify({
              primary: '⌘', primaryName: 'Command', alternate: 'Option', alternateName: 'Option'
            })) throw new Error(`unexpected mac labels: ${JSON.stringify(mac)}`);
            const windows = labels('Win32');
            if (JSON.stringify(windows) !== JSON.stringify({
              primary: 'Ctrl', primaryName: 'Control', alternate: 'Alt', alternateName: 'Alt'
            })) throw new Error(`unexpected windows labels: ${JSON.stringify(windows)}`);
            """.replace("process.env.PLATFORM_BLOCK", repr(platform_block))
        )

        self.assertIn('className="problem-order-copy"', source)
        self.assertIn("{PRIMARY_MODIFIER_LABEL}/Shift 선택", source)
        self.assertIn("${ALTERNATE_MODIFIER_NAME}+↑↓ 이동", source)
        self.assertNotIn("<kbd>Ctrl/Cmd</kbd>", source)

    def test_session_revision_never_regresses_from_late_preview_response(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        revision_block = "let latestServerSessionRevision" + source.split(
            "let latestServerSessionRevision", 1
        )[1].split("function withExpectedSessionRevision", 1)[0]
        run_node(
            revision_block
            + """
            const epoch1 = '00000000000000000001-epoch-1';
            const epoch2 = '00000000000000000002-epoch-2';
            captureSessionRevision({ session: { problems: [] }, sessionRevision: 7, sessionEpoch: epoch1 });
            const stalePayload = { session: { problems: [{ id: 'old' }] }, sessionRevision: 5, sessionEpoch: epoch1 };
            if (!sessionResponseRevisionIsStale(stalePayload)) throw new Error('late session was not rejected');
            captureSessionRevision(stalePayload);
            if (latestServerSessionRevision !== 7) throw new Error(`revision regressed to ${latestServerSessionRevision}`);
            if (sessionResponseRevisionIsStale({ session: { problems: [] }, sessionRevision: 8, sessionEpoch: epoch1 })) throw new Error('new session was rejected');
            captureSessionRevision({ session: { problems: [] }, sessionRevision: 1, sessionEpoch: epoch2 });
            if (latestServerSessionRevision !== 1 || latestServerSessionEpoch !== epoch2) throw new Error('new server epoch was not accepted');
            const retiredPayload = { session: { problems: [{ id: 'retired' }] }, sessionRevision: 9, sessionEpoch: epoch1 };
            if (!sessionResponseRevisionIsStale(retiredPayload)) throw new Error('retired server epoch was accepted');
            const unseenOldEpoch = '00000000000000000000-unseen-old';
            if (!sessionResponseRevisionIsStale({ session: { problems: [] }, sessionRevision: 99, sessionEpoch: unseenOldEpoch })) {
              throw new Error('unseen older server epoch was accepted');
            }
            captureSessionRevision({ session: { problems: [] }, sessionRevision: 99, sessionEpoch: unseenOldEpoch });
            if (latestServerSessionEpoch !== epoch2 || latestServerSessionRevision !== 1) {
              throw new Error('unseen older server epoch replaced the current server');
            }
            """
        )

    def test_upload_processing_waits_for_initial_session_hydration(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        app = source.split("function App()", 1)[1]

        self.assertIn("const [initialSessionLoaded, setInitialSessionLoaded] = useState(false)", app)
        self.assertIn("if (!cancelled) setInitialSessionLoaded(true)", app)
        process_queue = app.split("const processQueuedFiles = useCallback", 1)[1].split(
            "const cancelRecognitionReview", 1
        )[0]
        self.assertIn("if (!initialSessionLoaded)", process_queue)
        self.assertIn(
            "queueBusy={!initialSessionLoaded || !!loading || hasRunningQueueRecognition || hasPendingSessionConflict}",
            app,
        )

    def test_side_panel_exposes_four_edge_manual_crop_controls(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        side_panel = source.split("function SidePanel", 1)[1]
        side_panel = side_panel.split("function BugReportDialog", 1)[0]

        self.assertIn("여백 자르기", side_panel)
        self.assertIn("mutateSession?.('crop'", side_panel)
        self.assertIn("cropDraft.leftRatio", side_panel)
        self.assertIn("cropDraft.rightRatio", side_panel)
        self.assertIn("cropDraft.topRatio", side_panel)
        self.assertIn("cropDraft.bottomRatio", side_panel)
        self.assertIn("manual-crop-state", side_panel)
        self.assertIn("변경한 여백을 적용할 수 있어요", side_panel)
        self.assertIn("여백 변경 없음", side_panel)

    def test_side_panel_splits_material_details_from_placement_controls(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        side_panel = source.split("function SidePanel", 1)[1]
        side_panel = side_panel.split("function LoadingOverlay", 1)[0]
        preview_block = side_panel.split("className={`item-preview ${previewExpanded ? 'is-expanded' : 'is-collapsed'}`}", 1)[1]
        preview_block = preview_block.split("<div className=\"panel-section-hd\">", 1)[0]
        detail_block = side_panel.split("detail-settings-toggle", 1)[1]

        # Collapsed on first use; afterwards the last open/closed choice is remembered.
        self.assertIn(
            "const [advancedSettingsOpen, setAdvancedSettingsOpen] = "
            "useStoredPreference(ADVANCED_SETTINGS_OPEN_KEY, false, isStoredBoolean)",
            side_panel,
        )
        self.assertIn("setAdvancedSettingsOpen(true)", side_panel)
        self.assertIn("aria-expanded={advancedSettingsOpen}", side_panel)
        self.assertIn("상세 설정", side_panel)
        self.assertNotIn("className=\"ptools\"", preview_block)
        self.assertIn("className=\"ptools detail-tools\"", detail_block)
        self.assertIn("자르기 · 업스케일", detail_block)
        self.assertIn("tab === 'placement'", side_panel)
        self.assertIn("placement-position-control", side_panel)
        self.assertIn("const [cropPresetsOpen, setCropPresetsOpen] = useState(false)", side_panel)
        self.assertIn("자르기 프리셋", side_panel)
        self.assertIn("showItemConfirmBar", side_panel)
        self.assertIn("view === 'review'", side_panel)

    def test_side_panel_image_preview_is_collapsed_by_default_and_accessible(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        html = board_document()
        side_panel = source.split("function SidePanel", 1)[1]
        side_panel = side_panel.split("function LoadingOverlay", 1)[0]

        self.assertIn("const [previewExpanded, setPreviewExpanded] = useState(false)", side_panel)
        self.assertIn('className="item-preview-toggle"', side_panel)
        self.assertIn("aria-expanded={previewExpanded}", side_panel)
        self.assertIn('aria-controls="item-image-preview-content"', side_panel)
        self.assertIn("{previewExpanded && (", side_panel)
        self.assertIn('id="item-image-preview-content"', side_panel)
        self.assertIn("setPreviewExpanded(true)", side_panel)
        self.assertIn(".item-preview-toggle{", html)
        self.assertIn(".item-preview.is-expanded .item-preview-toggle{", html)

    def test_side_panel_exposes_three_tabs_and_group_placement_rules(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        html = board_document()
        side_panel = source.split("function SidePanel", 1)[1]
        side_panel = side_panel.split("function LoadingOverlay", 1)[0]

        self.assertIn('role="tablist"', side_panel)
        self.assertEqual(side_panel.count('role="tab"'), 3)
        self.assertIn("자료 <span", side_panel)
        self.assertIn("자료 위치, 크기, 지문 묶음 배치", side_panel)
        self.assertIn("AI, 칠판 배경, 문서 변환, 앱 정보", side_panel)
        self.assertIn("지문 한 번만 배치", side_panel)
        self.assertIn("문항 함께 이동", side_panel)
        self.assertIn("공간 부족 시 다음 칠판", side_panel)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", html)

    def test_main_controls_are_compact_by_default(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        html = board_document()
        topbar = source.split("function TopBar", 1)[1]
        topbar = topbar.split("function ReviewStage", 1)[0]
        rail = source.split("function ItemsRail", 1)[1]
        rail = rail.split("function BoardStage", 1)[0]
        board_stage = source.split("function BoardStage", 1)[1]
        board_stage = board_stage.split("function downloadPublishSummary", 1)[0]
        layout_controls = source.split("function BoardLayoutControls", 1)[1]
        layout_controls = layout_controls.split("function SidePanel", 1)[0]
        side_panel = source.split("function SidePanel", 1)[1]
        side_panel = side_panel.split("function BugReportDialog", 1)[0]

        self.assertIn("topbar-actions", topbar)
        self.assertIn("topbar-more-menu", topbar)
        self.assertIn("더보기", topbar)
        self.assertIn("파일 추가", rail)
        self.assertIn("hasSessionItems ? 'is-compact'", rail)
        self.assertIn("stage-fit-btn", board_stage)
        self.assertIn("stage-preview-controls", board_stage)
        self.assertIn("보기 {Math.round(previewZoom * 100)}%", board_stage)
        self.assertIn("선택 전체", board_stage)
        self.assertIn("boardPreviewZoomForRenderedHeight", board_stage)
        self.assertIn("activeFitsPreview", board_stage)
        self.assertIn("스크롤 필요", board_stage)
        self.assertIn("공백 방지", board_stage)
        self.assertIn("is-auto-fit", board_stage)
        self.assertIn(".stage-preview-controls", html)
        self.assertNotIn("title=\"자동 정렬\"", board_stage)
        self.assertIn("한 줄 ${columnCount}개", board_stage)
        self.assertIn("연속 이어붙임", board_stage)
        self.assertIn("칸 {p.columnIndex + 1}", board_stage)
        self.assertIn("한 줄 자료 수", layout_controls)
        self.assertIn("실제 EDB 제작은 현재 1열만 검증됨", layout_controls)
        # Multi-column export is not verified yet: show the fixed value instead of
        # two permanently disabled buttons, and keep a way back if a stored
        # session still carries 2 or 3 columns.
        self.assertNotIn("[1,2,3].map", layout_controls)
        self.assertIn("다열 Export 검증 후 제공할 예정입니다", layout_controls)
        self.assertIn("setBoardColumns?.(BOARD_COLUMN_MIN)", layout_controls)
        self.assertIn("2·3열은 실제 EDB 배치 검증 전입니다", source)
        # The drag magnet is always on; it lives in the board stage code, not
        # as a hard-coded "ON" badge dressed up as a setting.
        self.assertIn("function resolveBoardDragMagnet", source)
        self.assertNotIn("드래그 마그넷", side_panel)
        self.assertNotIn("열 수", side_panel)
        self.assertNotIn("열 가이드 자동 정렬", side_panel)
        self.assertIn(".drop-zone.is-compact", html)
        self.assertIn(".topbar-more-menu", html)

    def test_board_preview_zoom_uses_usable_classin_page_height(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        start = source.index("const FIXED_LEFT_ZONE_RATIO")
        end = source.index("function normalizePlacementXRatio")
        self.assertGreaterEqual(start, 0)
        self.assertGreater(end, start)
        helper_block = source[start:end]

        run_node(
            """
            const vm = require('vm');
            const sandbox = {};
            sandbox.globalThis = sandbox;
            vm.runInNewContext(process.env.HELPER_BLOCK + `
              globalThis.previewResult = {
                base: boardPreviewBasePageHeight(362, 26, 70),
                fit: boardPreviewZoomForRenderedHeight(2.72),
                min: normalizeBoardPreviewZoom(0.01),
                max: normalizeBoardPreviewZoom(9),
              };
            `, sandbox);
            const result = sandbox.previewResult;
            if (Math.abs(result.base - (266 / 1.2)) > 0.001) {
              throw new Error(`usable ClassIn height mismatch: ${JSON.stringify(result)}`);
            }
            if (Math.abs(result.fit - (1.08 / 2.72)) > 0.001) {
              throw new Error(`selected fit zoom mismatch: ${JSON.stringify(result)}`);
            }
            if (result.min !== 0.12 || result.max !== 1.4) {
              throw new Error(`preview zoom clamp mismatch: ${JSON.stringify(result)}`);
            }
            """.replace("process.env.HELPER_BLOCK", repr(helper_block))
        )
    def test_compact_controls_expose_hover_tooltips(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        html = board_document()
        topbar = source.split("function TopBar", 1)[1]
        topbar = topbar.split("function ReviewStage", 1)[0]
        side_panel = source.split("function SidePanel", 1)[1]
        side_panel = side_panel.split("function LoadingOverlay", 1)[0]

        self.assertIn("function TooltipLayer", source)
        self.assertIn("<TooltipLayer />", source)
        self.assertIn("document.addEventListener('pointerover'", source)
        self.assertIn("document.addEventListener('pointermove'", source)
        self.assertIn("document.addEventListener('focusin'", source)
        self.assertIn("data-tooltip", topbar)
        self.assertIn("보드 배치 화면으로 이동", topbar)
        self.assertIn("현재 배치로 EDB 파일 제작", topbar)
        self.assertIn("선택한 자료의 처리 방식과 세부 편집", side_panel)
        self.assertIn("여백 자르기와 업스케일 상세 설정", side_panel)
        self.assertIn("자료 위치, 크기, 지문 묶음 배치", side_panel)
        self.assertIn(".ui-tooltip", html)
        self.assertIn("position: fixed", html)

    def test_review_zoom_only_scales_problem_canvas(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        html = board_document()
        review_stage = source.split("function ReviewStage", 1)[1]
        review_stage = review_stage.split("function ItemsRail", 1)[0]

        self.assertIn("function ReviewCanvasZoomShell", source)
        self.assertIn("centerReviewZoomScrollers", review_stage)
        self.assertIn("reviewWrapRef.current?.querySelectorAll?.('.review-canvas-scroll')", review_stage)
        self.assertIn("className=\"review-zoom-range\"", review_stage)
        self.assertIn("문제 이미지만 확대/축소", review_stage)
        self.assertIn("<ReviewCanvasZoomShell>", review_stage)
        self.assertIn(".review-page{\n    width: 100%;", html)
        self.assertIn(".review-canvas-zoom-shell", html)
        self.assertIn("width: calc(100% * var(--review-zoom, 1))", html)

    def test_placement_tab_exposes_scoped_dynamic_scale_controls(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        html = board_document()
        side_panel = source.split("function SidePanel", 1)[1]
        side_panel = side_panel.split("function LoadingOverlay", 1)[0]
        publish_panel = source.split("function PublishResultPanel", 1)[1]
        publish_panel = publish_panel.split("function SidePanel", 1)[0]

        self.assertIn("const maxScale = maxPlacementScaleRatio(item)", side_panel)
        self.assertIn("placementScalePercent", side_panel)
        self.assertIn("className=\"scale-range\"", side_panel)
        self.assertIn("placement-width-options", side_panel)
        self.assertIn("placementScope", side_panel)
        self.assertIn("--range-progress", side_panel)
        self.assertIn("너비 맞춤 이어붙임", side_panel)
        fit_width_flow = source.split("if (patch.fitWidth) {", 1)[1]
        fit_width_flow = fit_width_flow.split("if (Object.prototype.hasOwnProperty.call(patch, 'scaleRatio'))", 1)[0]
        fit_width_helper = source.split("function fitWidthContinuousPageItem", 1)[1]
        fit_width_helper = fit_width_helper.split("function isContinuousPlacementItem", 1)[0]
        self.assertIn("return fitWidthContinuousPageItem(next, patch);", fit_width_flow)
        self.assertIn("PLACEMENT_FIT_WIDTH_SCALE_RATIO", source)
        board_stage = source.split("function BoardStage", 1)[1]
        board_stage = board_stage.split("function downloadPublishSummary", 1)[0]
        self.assertIn("contentW / PLACEMENT_FIT_WIDTH_SCALE_RATIO", board_stage)
        self.assertIn("next.inputIntent = 'page-as-is';", fit_width_helper)
        self.assertIn("next.placementMode = 'continuous-page-as-is';", fit_width_helper)
        self.assertIn("PLACEMENT_FIT_WIDTH_SCALE_RATIO", fit_width_helper)
        self.assertIn("heightPages * targetScale", fit_width_helper)
        self.assertIn("next.snappedNextStartYPages = Number((startPages + slotSpanPages).toFixed(6));", fit_width_helper)
        self.assertNotIn("snapUpPages(startPages + slotSpanPages)", fit_width_helper)
        self.assertIn("publish-result-panel ${open ? 'open' : 'is-collapsed'}", publish_panel)
        self.assertIn("제작 결과 펼치기", publish_panel)
        self.assertIn(".placement-scale-row input.scale-range", html)
        self.assertIn(".placement-width-options", html)
        self.assertIn(".placement-position-control", html)
        self.assertIn(".publish-result-panel.is-collapsed", html)

    def test_passage_only_preset_targets_fragments_and_applies_reading_width(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        side_panel = source.split("function SidePanel", 1)[1]
        side_panel = side_panel.split("function LoadingOverlay", 1)[0]
        item_mapper = source.split("function mapProblemToItem(problem, idx){", 1)[1]
        item_mapper = item_mapper.split("async function fetchLatestSession", 1)[0]
        preset = side_panel.split("const applyPlacementPreset = (preset) => {", 1)[1]
        preset = preset.split("const updateCropDraft", 1)[0]

        self.assertIn("passageGroupId:", item_mapper)
        self.assertIn("passageRole:", item_mapper)
        self.assertIn("passageRange:", item_mapper)
        self.assertIn("supplementalItem:", item_mapper)
        self.assertIn("const selectedPassageFragments = selectedGroupItems.filter(isPassageFragmentProblem);", side_panel)
        self.assertIn("placementPreset === 'passage-only'", side_panel)
        self.assertIn("setPlacements?.(selectedPassageFragments.map(target => ({", preset)
        self.assertIn("scaleRatio: maxPlacementScaleRatio(target)", preset)
        self.assertIn("fitWidth: true", preset)
        self.assertIn("setBoardColumns?.(1)", preset)
        self.assertIn("value === 'passage-only' && passageFragmentCount === 0", side_panel)
        self.assertIn("['passage-only', '지문 전체 너비']", side_panel)
        self.assertIn("이미 추출된 지문 본문만 1열 최대 읽기 폭으로 맞춥니다", side_panel)

    def test_placement_apply_persists_preview_adjustments(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        side_panel = source.split("function SidePanel", 1)[1]
        side_panel = side_panel.split("function LoadingOverlay", 1)[0]
        save_flow = source.split("const savePlacement = async () => {", 1)[1]
        save_flow = save_flow.split("const reorder = (fromId, toId", 1)[0]

        self.assertIn("setPlacement, setPlacements, savePlacement, mutateSession, mutating", side_panel)
        self.assertIn("const saved = await savePlacement?.();", side_panel)
        self.assertIn("onClick={() => { void commitPlacement(); }}", side_panel)
        self.assertIn("disabled={!item || mutating}", side_panel)
        self.assertIn("layoutGapMode", save_flow)
        self.assertIn("materializeSessionForItems(", save_flow)
        self.assertIn("placementPersistenceSignature(candidate) === placementPersistenceSignature(session)", save_flow)
        self.assertIn("postRestoreBoardLayoutWithConflictRetry(candidate, boardColumns)", save_flow)
        self.assertIn("if (result.conflictBase) snapshotBefore = result.conflictBase;", save_flow)
        self.assertIn("setSession(restored);", save_flow)
        self.assertIn("setActiveId(currentId => (", save_flow)
        self.assertIn("appendBoundedHistory(prev, snapshotBefore, UNDO_HISTORY_LIMIT)", save_flow)
        self.assertIn("savePlacement={savePlacement}", source)
        self.assertIn("placementScaleRatio: initialScale,", source)

    def test_placement_updates_are_batched_and_conflict_safe(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        side_panel = source.split("function SidePanel", 1)[1]
        side_panel = side_panel.split("function LoadingOverlay", 1)[0]
        batch_flow = source.split("const setPlacements = (updates) => {", 1)[1]
        batch_flow = batch_flow.split("const savePlacement = async () => {", 1)[0]
        retry_flow = source.split("async function postRestoreBoardLayoutWithConflictRetry", 1)[1]
        retry_flow = retry_flow.split("async function postClassinReviewResult", 1)[0]

        self.assertIn("const patchById = new Map", batch_flow)
        self.assertIn("return applyPlacementPatchToItem(x, patch);", batch_flow)
        self.assertIn("setPlacements={setPlacements}", source)
        self.assertIn("if (setPlacements) setPlacements(updates);", side_panel)
        self.assertIn("selectedPassageFragments.map(target => ({", side_panel)
        preset_flow = side_panel.split("const applyPlacementPreset", 1)[1].split("const commitPlacement", 1)[0]
        self.assertNotIn("setPlacementApplied(true)", preset_flow)

        self.assertIn("assertPlacementSaveSession(await postRestore(candidate))", retry_flow)
        self.assertIn("error?.code !== 'session_conflict'", retry_flow)
        self.assertIn("captureSessionRevision(conflictPayload);", retry_flow)
        self.assertIn("rebaseSessionBoardLayout(latestSession, candidate, boardColumns)", retry_flow)
        self.assertIn("conflictBase: cloneSession(latestSession)", retry_flow)
        self.assertIn("invalid_placement_save_response", source)

    def test_board_dividers_and_page_indicator_use_true_classin_12_grid(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        board = board_document()
        board_stage = source.split("function BoardStage({", 1)[1].split("function downloadPublishSummary", 1)[0]

        self.assertIn("function classinBoardPageNumberAtOffset(offsetPages)", source)
        self.assertIn("const offsetPages = i * DEFAULT_SLOT_HEIGHT_PAGES", board_stage)
        self.assertIn("top: offsetPages * pageH", board_stage)
        self.assertIn("className={`page-divider is-classin-grid", board_stage)
        self.assertIn("ClassIn {divider.classinPage}p", board_stage)
        self.assertIn("Math.max(1, previewEstimate.classinPageCount)", board_stage)
        self.assertIn(">ClassIn</span>", board_stage)
        self.assertIn("C{tileClassinStartPage}–C{tileClassinEndPage}", board_stage)
        self.assertIn("`예상 C${previewEstimate.classinPageCount}`", board_stage)
        self.assertIn(".page-divider .label .classin-page", board)

    def test_board_preview_exposes_estimate_and_active_gap_guides(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        board_stage = source.split("function BoardStage({", 1)[1].split("function downloadPublishSummary", 1)[0]

        self.assertIn('className="stage-estimate-bar"', board_stage)
        self.assertIn('role="status"', board_stage)
        self.assertIn('aria-live="polite"', board_stage)
        self.assertIn("예상 ClassIn {previewEstimate.classinPageCount}페이지", board_stage)
        self.assertIn('className="active-slot-gap"', board_stage)
        self.assertIn('className="active-next-boundary"', board_stage)
        self.assertIn('aria-hidden="true"', board_stage)
        self.assertIn("activeRemovableGapPages", board_stage)
        self.assertIn("{ gapAfterPages: 0 }", board_stage)
        self.assertIn("여백 {activeRemovableGapPages.toFixed(1)}p 삭제", board_stage)

    def test_board_settings_supports_compact_layout_and_gap_override_reset(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        side_panel = source.split("function SidePanel", 1)[1]
        side_panel = side_panel.split("function LoadingOverlay", 1)[0]
        layout_controls = source.split("function BoardLayoutControls", 1)[1]
        layout_controls = layout_controls.split("function SidePanel", 1)[0]
        item_tab = side_panel.split("{tab === 'item' && (", 1)[1]
        item_tab = item_tab.split("{tab === 'placement' && (", 1)[0]
        placement_tab = side_panel.split("{tab === 'placement' && (", 1)[1]
        placement_tab = placement_tab.split("{tab === 'board' && (", 1)[0]
        settings_tab = side_panel.split("{tab === 'board' && (", 1)[1]
        materialize = source.split("function materializeSessionForItems", 1)[1]
        materialize = materialize.split("function rebaseSessionBoardLayout", 1)[0]

        self.assertIn("문항 사이 여백", layout_controls)
        self.assertIn("1.2 맞춤", layout_controls)
        self.assertIn("빈틈 없이", layout_controls)
        self.assertIn("경계 공백 방지", layout_controls)
        self.assertIn("최대 6% 이내", layout_controls)
        self.assertIn("사용자가 조절한 배율은 유지", layout_controls)
        self.assertIn("ClassIn 화면 경계를 걸칠 수", layout_controls)
        self.assertIn("개별 여백 삭제", layout_controls)
        self.assertIn("resetPlacementGaps", layout_controls)
        # Session-wide layout sits with placement, not on top of one problem's options.
        self.assertIn("view === 'board'", placement_tab)
        self.assertIn("<BoardLayoutControls", placement_tab)
        self.assertIn("forcedOpen={placementScope === 'all'}", placement_tab)
        self.assertNotIn("<BoardLayoutControls", item_tab)
        self.assertNotIn("<BoardLayoutControls", settings_tab)
        self.assertNotIn("칠판 동작", settings_tab)
        self.assertIn("window.confirm('전체 문항 사이의 1.2 맞춤 여백을 제거할까요?", source)
        self.assertIn("snapshot.layoutGapMode = normalizedGapMode", materialize)
        self.assertIn("snapshot.layout_gap_mode = normalizedGapMode", materialize)
        self.assertIn("const strandedProblems = snapshot.problems.filter", materialize)
        self.assertIn("const mergedProblems = orderedProblems.concat(strandedProblems)", materialize)
        self.assertIn("placementGapAfterPages", source)
        self.assertIn("placementAutoFitDisabled", source)
        self.assertIn("layoutGapMode: normalizeLayoutGapMode", source)

    def test_admin_pin_update_is_only_offered_for_a_new_version(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        side_panel = source.split("function SidePanel", 1)[1]
        side_panel = side_panel.split("function LoadingOverlay", 1)[0]

        self.assertIn('{updateInfo?.updateAvailable && <div className="update-install-card">', side_panel)
        self.assertIn("관리자 PIN 업데이트", side_panel)
        self.assertIn("관리자 PIN 확인 후 업데이트", side_panel)
        self.assertIn("const requestUpdateInstall = () =>", side_panel)
        self.assertIn("window.confirm(", side_panel)

    def test_left_sidebar_filters_recognized_material_without_destructive_recognition_target(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        board = board_document()
        items_rail = source.split("function ItemsRail({", 1)[1].split("function BoardStage({", 1)[0]
        side_panel = source.split("function SidePanel", 1)[1]
        side_panel = side_panel.split("function LoadingOverlay", 1)[0]
        export_request = source.split("async function postExport(files", 1)[1]
        export_request = export_request.split("function formatApiError", 1)[0]
        queue_source = source.split("const processQueuedFiles = useCallback(async (mode, targetKey = null) => {", 1)[1]
        queue_source = queue_source.split("const cancelRecognitionReview = useCallback", 1)[0]

        self.assertIn('role="group" aria-label="자료 모아보기"', items_rail)
        self.assertIn("['all', '전체', materialCounts.all]", items_rail)
        self.assertIn("['questions', '문항', materialCounts.questions]", items_rail)
        self.assertIn("['passages', '공통 지문', materialCounts.passages]", items_rail)
        self.assertIn("items.filter(item => !isPassageFragmentProblem(item)).length", items_rail)
        self.assertIn("items.filter(isPassageFragmentProblem).length", items_rail)
        self.assertIn("모아보기 중 · 순서 변경은 전체 보기에서", items_rail)
        self.assertNotIn('aria-label="인식 대상"', side_panel)
        self.assertIn("contentTarget: resolvedContentTarget", export_request)
        self.assertIn("content_target: resolvedContentTarget", export_request)
        self.assertIn("contentTarget: DEFAULT_CONTENT_TARGET", queue_source)
        self.assertIn("processQueuedFiles('passage-only', key)", items_rail)
        self.assertIn("onClick={() => processQueuedFiles('passage-only')}", items_rail)
        self.assertIn("<strong>지문만 추출</strong>", items_rail)
        self.assertIn("const isPassageOnly = mode === 'passage-only';", queue_source)
        self.assertIn("const isRecognition = mode === 'recognize' || isPassageOnly;", queue_source)
        self.assertIn("contentTarget: isPassageOnly ? 'shared-passages' : DEFAULT_CONTENT_TARGET", queue_source)
        self.assertIn("contentTarget: isPassageOnly ? 'shared-passages' : DEFAULT_CONTENT_TARGET", source)
        self.assertIn("const CONTENT_TARGETS = new Set(['all', 'questions', 'shared-passages']);", source)
        self.assertIn("return CONTENT_TARGETS.has(normalized) ? normalized : DEFAULT_CONTENT_TARGET;", source)
        self.assertIn("normalizeContentTarget(review?.contentTarget) === 'shared-passages'", source)
        self.assertIn("공통 지문 ${summary.problems}개", source)
        self.assertIn(".material-filter", board)

    def test_left_sidebar_supports_modifier_multiselect_group_move_and_bulk_delete(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        board = board_document()
        items_rail = source.split("function ItemsRail({", 1)[1].split("function BoardStage({", 1)[0]
        reorder_flow = source.split("const reorder = (fromId, toId", 1)[1].split("const removeItem", 1)[0]
        remove_flow = source.split("const removeItem = async (id, options = {}) =>", 1)[1]
        remove_flow = remove_flow.split("const addMockSample", 1)[0]
        selected_step_flow = source.split("const applySelectedStep = (problemIds, step) =>", 1)[1]
        selected_step_flow = selected_step_flow.split("const classifySelected", 1)[0]

        self.assertIn("selectedItemIds, setSelectedItemIds", items_rail)
        self.assertIn("applySelectionClick(", items_rail)
        self.assertIn("selectionKeyboardCommand(", items_rail)
        self.assertIn("Control+A Meta+A Shift+ArrowUp Shift+ArrowDown Escape", items_rail)
        self.assertIn("sourceIds: drag.ids", items_rail)
        self.assertIn("adjacentGroupReorderCommand(items, sourceIds, direction)", items_rail)
        self.assertIn("removeItem(orderedSelectedIds[0], { problemIds: orderedSelectedIds });", items_rail)
        self.assertIn("선택 문제 일괄 작업", items_rail)
        self.assertNotIn("onClassifySelected?.(orderedSelectedIds, 'shared-passage')", items_rail)
        self.assertIn("onClassifySelected?.(orderedSelectedIds, 'question')", items_rail)
        self.assertIn("onDownloadSelected?.(orderedSelectedIds)", items_rail)
        self.assertIn("reorderItemGroupForDrop(items, sourceIds, toId, dropPosition)", reorder_flow)
        self.assertIn("await mutateSession('exclude', { problemIds });", remove_flow)
        self.assertIn("showActionToast(", remove_flow)
        self.assertIn("stepLabel(nextStep)", selected_step_flow)
        self.assertNotIn("processingStepLabel", selected_step_flow)
        self.assertIn(".item.is-selected", board)
        self.assertIn(".problem-order-status.is-selection", board)
        self.assertIn("updatePointerDragVisual(event.clientX, event.clientY)", items_rail)
        self.assertIn("resetPointerDragVisual()", items_rail)
        self.assertIn("pressedItemId === itemId ? 'is-pressed' : ''", items_rail)
        self.assertIn(".item.is-pressed:not(.dragging)", board)
        self.assertIn("ReactDOM.createPortal(", items_rail)
        self.assertIn("className=\"rail-drag-overlay\"", items_rail)
        self.assertIn("{dragPreview.count}개 이동", items_rail)
        self.assertIn("displayedItemRows.map", items_rail)
        self.assertIn("rail.setPointerCapture?.(event.pointerId)", items_rail)
        self.assertIn("translate3d(", board)
        self.assertIn("var(--drag-preview-x, 0)", board)
        self.assertIn(".rail-drag-overlay .item.rail-drag-overlay-card", board)
        self.assertIn("String(dropTarget?.id || '') === itemId", items_rail)
        self.assertIn("dropTargetLayoutSignature", items_rail)
        self.assertIn("className=\"rail-drop-slot\"", items_rail)
        self.assertIn("data-drop-position=\"before\"", items_rail)
        self.assertIn("data-drop-position=\"after\"", items_rail)
        self.assertIn(".item.drop-before", board)
        self.assertIn("@keyframes rail-drop-slot-open", board)
        self.assertIn("@keyframes rail-drop-slot-line", board)
        self.assertNotIn("@keyframes rail-drop-slot-marker", board)
        self.assertNotIn(".rail-drop-slot::after", board)
        self.assertIn("animation: rail-drop-slot-open", board)

    def test_item_editor_can_correct_question_and_shared_passage_classification(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        side_panel = source.split("function SidePanel", 1)[1].split("function LoadingOverlay", 1)[0]
        mutation = source.split("const mutateSession = useCallback(async (action, args) => {", 1)[1]
        mutation = mutation.split("const retryAiSession", 1)[0]

        self.assertIn('className="item-classification"', side_panel)
        self.assertIn('role="radiogroup" aria-label="선택 자료 분류"', side_panel)
        self.assertIn("classification: 'question'", side_panel)
        self.assertIn("classification: 'shared-passage'", side_panel)
        self.assertIn("이미 잘라진 독립 지문 이미지에만 사용 · 원본 재추출은 왼쪽 버튼", side_panel)
        self.assertIn(">독립 지문 이미지</button>", side_panel)
        self.assertIn("action === 'classify' ? '자료 분류를 저장하는 중…'", mutation)
        self.assertIn("action === 'classify' ? '자료 분류를 변경했어요'", mutation)

    def test_page_png_queue_register_applies_fit_width_page_flow(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        queue_source = source.split("const processQueuedFiles = useCallback(async (mode, targetKey = null) => {", 1)[1]
        queue_source = queue_source.split("const cancelRecognitionReview = useCallback", 1)[0]
        session_helper = source.split("function fitWidthPageAsIsSession", 1)[1]
        session_helper = session_helper.split("function mergeSessions", 1)[0]

        self.assertIn("const registeredSession = isManualSplit ? s : fitWidthPageAsIsSession(s, { fileName, boardColumns });", queue_source)
        self.assertIn("let sessionToApply = registeredSession;", queue_source)
        self.assertIn("const merged = mergeSessions(currentSnapshot, registeredSession, fileName, boardColumns);", queue_source)
        self.assertIn("} else if (!isManualSplit && sessionToApply !== s) {", queue_source)
        self.assertIn("sessionToApply = await postRestore(sessionToApply);", queue_source)
        self.assertIn("return fitWidthContinuousPageItem", session_helper)

    def test_reorder_reflows_saved_board_page_positions(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        self.assertIn("function reflowItemsForBoardOrder", source)
        materialize = source.split("function materializeSessionForItems", 1)[1]
        materialize = materialize.split("function mergeSessions", 1)[0]
        apply_state = source.split("function applyItemStateToProblem", 1)[1]
        apply_state = apply_state.split("function confirmedItemState", 1)[0]
        reorder_flow = source.split("const reorder = (fromId, toId", 1)[1]
        reorder_flow = reorder_flow.split("const removeItem", 1)[0]
        remove_flow = source.split("const removeItem = async (id, options = {}) =>", 1)[1]
        remove_flow = remove_flow.split("const addMockSample", 1)[0]

        self.assertIn("const reflowedItems = reflowItemsForBoardOrder(", materialize)
        self.assertIn("normalizedGapMode", materialize)
        self.assertIn("next.startYPages", apply_state)
        self.assertIn("next.snappedNextStartYPages", apply_state)
        self.assertIn("options?.resetPlacement ? resetItems : reordered", reorder_flow)
        self.assertIn("layoutGapMode", reorder_flow)
        self.assertIn("materializeSessionForItems(session, nextItems, fileName, boardColumns, layoutGapMode)", reorder_flow)
        self.assertIn("setSession(nextSession)", reorder_flow)
        self.assertIn("postRestore(nextSession)", reorder_flow)
        self.assertIn("appendBoundedHistory(prev, snapshotBefore, UNDO_HISTORY_LIMIT)", reorder_flow)
        self.assertIn("setActiveId(fromId)", reorder_flow)
        self.assertIn("if (session)", remove_flow)
        self.assertIn("await mutateSession('exclude', { problemId: id })", remove_flow)
        self.assertIn("() => { void undoMutation(); }", remove_flow)
        self.assertIn("reflowItemsForBoardOrder(items.filter", remove_flow)

    def test_undo_keeps_the_current_workspace_and_active_problem(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        undo_flow = source.split("const undoMutation = useCallback(async () => {", 1)[1]
        undo_flow = undo_flow.split("// Ctrl/Cmd+Z", 1)[0]
        self.assertIn("const viewBeforeUndo = view;", undo_flow)
        self.assertIn("const activeBeforeUndo = activeId;", undo_flow)
        self.assertIn("setView(viewBeforeUndo);", undo_flow)
        self.assertIn("setActiveId(activeBeforeUndo);", undo_flow)

    def test_reorder_motion_avoids_drag_frame_wide_rerenders(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        items_rail = source.split("function ItemsRail({", 1)[1].split("function BoardStage({", 1)[0]
        board_stage = source.split("function BoardStage({", 1)[1].split("// ─── RIGHT:", 1)[0]

        self.assertIn(
            "}, [itemOrderSignature, draggingLayoutSignature, dropTargetLayoutSignature]);",
            items_rail,
        )
        self.assertIn("}, [boardOrderSignature, pageH, contentW, columnCount]);", board_stage)
        self.assertIn("setCurrentPage(prev => prev === nextPage ? prev : nextPage);", board_stage)
        self.assertIn("nearestPlacementIndex(layout.positions, c.scrollTop + 24)", board_stage)
        self.assertNotIn("setScrollTop(scroll.scrollTop)", board_stage)
        self.assertNotIn("layout.positions\n      .map", board_stage)
        self.assertIn("for (const { item } of visibleItemRows)", items_rail)
        self.assertIn("const rect = el.getBoundingClientRect();", items_rail)
        self.assertIn("drag.lastClientY", items_rail)
        self.assertIn("sourceIdSet: new Set(sourceIds)", items_rail)
        self.assertIn("activeDrag.sourceIdSet", items_rail)
        self.assertIn("window.addEventListener('pointermove', drag.windowPointerMove", items_rail)
        self.assertIn("window.addEventListener('blur', drag.windowBlur)", items_rail)
        self.assertIn("removeRailDragWindowListeners(drag)", items_rail)
        self.assertIn("scrollContainerContentTop(itemRect, railRect, rail.scrollTop)", items_rail)
        self.assertIn("Math.max(18, railRect.height * 0.35)", items_rail)
        self.assertNotIn("|| dropTargetRef.current", items_rail)
        self.assertIn("itemLayoutAnimationsRef.current.get(it.id)", items_rail)
        self.assertIn("duration: isMovedItem ? 480 : 380", items_rail)
        self.assertIn("|| event.shiftKey", items_rail)
        self.assertIn("|| event.ctrlKey", items_rail)
        self.assertIn("|| event.metaKey", items_rail)
        self.assertIn('className="grip"', items_rail)
        self.assertIn("onPointerDown={e => startPointerDrag(e, it.id)}", items_rail)
        self.assertIn("dragVisualFrameRef.current = window.requestAnimationFrame", items_rail)
        self.assertNotIn("rail.querySelectorAll('.item[data-item-id]')", items_rail)

    def test_sidebar_selection_anchor_tracks_external_selection_changes(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        items_rail = source.split("function ItemsRail({", 1)[1].split("function BoardStage({", 1)[0]

        self.assertIn("const railSelectionSnapshotRef = useRef('')", items_rail)
        self.assertIn("const railOwnsAnchor =", items_rail)
        self.assertIn("railSelectionSnapshotRef.current === currentSelectionKey", items_rail)
        self.assertIn("railSelectionSnapshotRef.current = selection.selectedIds.join('|')", items_rail)

    def test_board_drag_uses_header_handle_and_exposes_selection_state(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        board_stage = source.split("function BoardStage({", 1)[1].split("// ─── RIGHT:", 1)[0]

        self.assertIn('className="tile-hd"', board_stage)
        self.assertIn("onPointerDown={e => beginPositionDrag(e, it, p)}", board_stage)
        self.assertIn("aria-pressed={selectedIds?.has(String(it.id)) ? 'true' : 'false'}", board_stage)

    def test_reorder_has_keyboard_accessibility_and_failed_save_rollback(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        board = board_document()
        items_rail = source.split("function ItemsRail({", 1)[1].split("function BoardStage({", 1)[0]
        reorder_flow = source.split("const reorder = (fromId, toId", 1)[1].split("const removeItem", 1)[0]

        self.assertIn("e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')", items_rail)
        self.assertIn('aria-describedby="problem-order-help"', items_rail)
        self.assertIn("filterActive ? '필터된 문제' : '순서 변경 가능한 문제'", items_rail)
        self.assertIn("Control+A Meta+A Shift+ArrowUp Shift+ArrowDown Escape", items_rail)
        self.assertIn('role="status" aria-live="polite"', items_rail)
        self.assertIn("pendingKeyboardFocusRef.current = item.id", items_rail)
        self.assertIn("setItems(rollbackItems)", reorder_flow)
        self.assertIn("setSession(snapshotBefore)", reorder_flow)
        self.assertIn("순서 저장 실패 · 이전 순서로 복구했습니다", reorder_flow)
        self.assertIn("return true", reorder_flow)
        self.assertIn(".item:focus-visible", board)
        self.assertIn(".problem-order-status", board)
        self.assertIn("@media (min-width: 701px) and (max-width: 920px)", board)
        self.assertIn(".item .actions .item-download-action", board)

    def test_hangul_runtime_helpers_include_hwp_renderer(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        summary_helper = source.split("function hangulRuntimeSummary", 1)[1]
        summary_helper = summary_helper.split("function hangulRuntimeToolRows", 1)[0]
        tool_rows = source.split("function hangulRuntimeToolRows", 1)[1]
        tool_rows = tool_rows.split("function listUnique", 1)[0]

        self.assertIn("hangul.hwpRenderers", summary_helper)
        self.assertIn("렌더 ${rendererCount}", summary_helper)
        self.assertIn("HWP 렌더", tool_rows)
        self.assertIn("hangul.hwpRenderers || []", tool_rows)

    def test_board_settings_exposes_app_update_controls(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        side_panel = source.split("function SidePanel", 1)[1]
        side_panel = side_panel.split("function LoadingOverlay", 1)[0]

        self.assertIn("앱 업데이트", side_panel)
        self.assertIn("업데이트 확인", side_panel)
        self.assertIn("다운로드 열기", side_panel)
        self.assertIn("const updateDownloadUrl = updateInfo?.downloadUrl || updateInfo?.latest?.downloadUrl || ''", side_panel)
        self.assertIn("updateStatus === 'invalid_feed'", side_panel)
        self.assertIn("피드 오류", side_panel)
        self.assertIn("isUpdateArchitectureMismatch(updateInfo)", side_panel)
        self.assertIn("이 기기용 업데이트 파일 없음", side_panel)
        self.assertIn("현재 아키텍처용 설치 파일을 선택해 주세요", source)
        self.assertIn("updateArchitectureSteps.map", side_panel)
        self.assertIn("unsupported_architecture", source)
        self.assertIn("update_architecture_mismatch", source)
        self.assertIn("updateArchitectureNotice(info)", source)
        self.assertIn("disabled={updateActionBusy || !updateDownloadUrl}", side_panel)
        self.assertIn("if (updateBusy)", source)
        self.assertIn("fetch('/api/app/update')", source)
        self.assertIn("fetch('/api/system/open-url'", source)

    def test_shipped_bundle_contains_app_update_controls(self) -> None:
        bundle = (PROJECT_ROOT / "ui_prototype" / "app.bundle.js").read_text(encoding="utf-8")
        compact_bundle = re.sub(r"\s+", "", bundle)

        self.assertIn("updateActionBusy||!updateDownloadUrl", compact_bundle)
        self.assertIn("invalid_feed", bundle)
        self.assertIn("피드 오류", bundle)
        self.assertIn("fetch('/api/app/update')", bundle)
        self.assertIn("fetch('/api/system/open-url'", bundle)

    def test_review_summary_surfaces_hwp_cache_hits(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        review_stage = source.split("<span className=\"review-summary-title\">검수 요약</span>", 1)[1]
        review_stage = review_stage.split("{reviewSummary.warningPreview &&", 1)[0]
        summary_helper = source.split("function sessionReviewSummary(session)", 1)[1]
        summary_helper = summary_helper.split("function normalizePublishSummary", 1)[0]

        self.assertIn("HWP 캐시", review_stage)
        self.assertIn("hwpCacheHitPageCount", review_stage)
        self.assertIn("hwpCacheHitPageCount", summary_helper)
        self.assertIn("hwpRendererCacheHitCount", summary_helper)
        self.assertIn("hwpNormalizedCacheHitCount", summary_helper)

    def test_review_summary_surfaces_ai_stage_counts(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        review_stage = source.split("<span className=\"review-summary-title\">검수 요약</span>", 1)[1]
        review_stage = review_stage.split("{reviewSummary.warningPreview &&", 1)[0]
        summary_helper = source.split("function sessionReviewSummary(session)", 1)[1]
        summary_helper = summary_helper.split("function normalizePublishSummary", 1)[0]

        self.assertIn("reviewSummary.aiStages.map", review_stage)
        self.assertIn("aiStageChipText(stage)", review_stage)
        self.assertIn("aiStageTooltip(stage)", review_stage)
        self.assertIn("normalizeAiStageSummaries(session)", summary_helper)
        self.assertIn("aiStages", summary_helper)

    def test_review_summary_surfaces_hwp_segmentation_risk_counts(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        review_stage = source.split("<span className=\"review-summary-title\">검수 요약</span>", 1)[1]
        review_stage = review_stage.split("{reviewSummary.warningPreview &&", 1)[0]
        summary_helper = source.split("function sessionReviewSummary(session)", 1)[1]
        summary_helper = summary_helper.split("function normalizePublishSummary", 1)[0]

        self.assertIn("HWP 과분할", review_stage)
        self.assertIn("hwpOversegmentationCount", review_stage)
        self.assertIn("hwpProblemCountMismatchCount", summary_helper)
        self.assertIn("hwp_problem_count_mismatch", summary_helper)
        self.assertIn("hwpOversegmentationCount", summary_helper)
        self.assertIn("hwp_oversegmentation", summary_helper)

    def test_review_summary_surfaces_duplicate_problem_number_groups(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        review_stage = source.split("<span className=\"review-summary-title\">검수 요약</span>", 1)[1]
        review_stage = review_stage.split("{reviewSummary.warningPreview &&", 1)[0]
        summary_helper = source.split("function sessionReviewSummary(session)", 1)[1]
        summary_helper = summary_helper.split("function normalizePublishSummary", 1)[0]
        risk_meta = source.split("const RISK_FLAG_META = {", 1)[1]
        risk_meta = risk_meta.split("};", 1)[0]

        self.assertIn("중복 번호", review_stage)
        self.assertIn("duplicateProblemNumberGroups", review_stage)
        self.assertIn("duplicateProblemNumberGroups", summary_helper)
        self.assertIn("duplicate_problem_number_groups", summary_helper)
        self.assertIn("duplicateProblemNumberLabel", summary_helper)
        self.assertIn("duplicate_problem_number", risk_meta)
        self.assertIn("중복 번호", risk_meta)

    def test_review_summary_surfaces_source_problem_overlap_groups(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        review_stage = source.split("<span className=\"review-summary-title\">검수 요약</span>", 1)[1]
        review_stage = review_stage.split("{reviewSummary.warningPreview &&", 1)[0]
        summary_helper = source.split("function sessionReviewSummary(session)", 1)[1]
        summary_helper = summary_helper.split("function normalizePublishSummary", 1)[0]
        risk_meta = source.split("const RISK_FLAG_META = {", 1)[1]
        risk_meta = risk_meta.split("};", 1)[0]

        self.assertIn("문항 영역 겹침", review_stage)
        self.assertIn("sourceProblemOverlapGroups", review_stage)
        self.assertIn("sourceProblemOverlapGroups", summary_helper)
        self.assertIn("source_problem_overlap_groups", summary_helper)
        self.assertIn("sourceProblemOverlapLabel", summary_helper)
        self.assertIn("source_problem_bbox_overlap", risk_meta)
        self.assertIn("문항 영역 겹침", risk_meta)

    def test_app_labels_passage_missing_child_preflight_issue(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        risk_meta = source.split("const RISK_FLAG_META = {", 1)[1]
        risk_meta = risk_meta.split("};", 1)[0]
        preflight_meta = source.split("const CLASSIN_PREFLIGHT_ISSUE_LABELS = {", 1)[1]
        preflight_meta = preflight_meta.split("};", 1)[0]

        self.assertIn("passage_missing_child_questions", risk_meta)
        self.assertIn("문항 누락", risk_meta)
        self.assertIn("passage_missing_child_questions", preflight_meta)
        self.assertIn("문항 누락", preflight_meta)

    def test_review_summary_surfaces_passage_group_source_reuse_groups(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        review_stage = source.split("<span className=\"review-summary-title\">검수 요약</span>", 1)[1]
        review_stage = review_stage.split("{reviewSummary.warningPreview &&", 1)[0]
        summary_helper = source.split("function sessionReviewSummary(session)", 1)[1]
        summary_helper = summary_helper.split("function normalizePublishSummary", 1)[0]

        self.assertIn("지문 겹침", review_stage)
        self.assertIn("passageGroupSourceReuseGroups", review_stage)
        self.assertIn("passageGroupSourceReuseDetailLabel", review_stage)
        self.assertIn("passage_group_source_reuse", review_stage)
        self.assertIn("passageGroupSourceReuseGroups", summary_helper)
        self.assertIn("passage_group_source_reuse_groups", summary_helper)
        self.assertIn("passageGroupSourceReuseLabel", summary_helper)
        self.assertIn("passageGroupSourceReuseDetailLabel", summary_helper)

        run_node(
            r"""
            const fs = require('fs');
            const vm = require('vm');
            const source = fs.readFileSync('./ui_prototype/app.jsx', 'utf8');
            const start = source.indexOf('const REVIEW_STATUS_META =');
            const end = source.indexOf('function normalizePublishSummary');
            if (start < 0 || end < 0) throw new Error('review summary helper bounds not found');
            const sandbox = {};
            sandbox.globalThis = sandbox;
            vm.runInNewContext(source.slice(start, end), sandbox);
            const summary = sandbox.sessionReviewSummary({
              passageGroupSourceReuseGroups: [
                {
                  passageGroupId: 'hwp-text-passage-31-34',
                  sourcePageId: 'page-004',
                  problemIds: ['p31', 'p32'],
                  overlapAreaRatio: 0.92,
                },
              ],
              passageGroupSourceReuseGroupCount: 1,
              problems: [
                {
                  id: 'p31',
                  reviewStatus: 'check_needed',
                  riskFlags: ['passage_group_source_reuse'],
                  bbox: { width: 10, height: 10 },
                },
                {
                  id: 'p32',
                  reviewStatus: 'check_needed',
                  riskFlags: ['passage_group_source_reuse'],
                  bbox: { width: 10, height: 10 },
                },
              ],
              pages: [{ id: 'page-004', problemIds: ['p31', 'p32'], riskFlags: [] }],
            });
            if (summary.passageGroupSourceReuseGroups.length !== 1) {
              throw new Error(`expected one source reuse group, got ${summary.passageGroupSourceReuseGroups.length}`);
            }
            if (summary.passageGroupSourceReuseGroupCount !== 1) {
              throw new Error(`expected source reuse count 1, got ${summary.passageGroupSourceReuseGroupCount}`);
            }
            if (summary.passageGroupSourceReuseLabel !== '지문 겹침 1건') {
              throw new Error(`expected short passage group label, got ${summary.passageGroupSourceReuseLabel}`);
            }
            if (!summary.passageGroupSourceReuseDetailLabel.includes('hwp-text-passage-31-34')) {
              throw new Error(`expected passage group detail, got ${summary.passageGroupSourceReuseDetailLabel}`);
            }
            if (!summary.passageGroupSourceReuseDetailLabel.includes('92%')) {
              throw new Error(`expected overlap percent detail, got ${summary.passageGroupSourceReuseDetailLabel}`);
            }
            """
        )

    def test_review_summary_surfaces_passage_groups(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        board_html = board_document()
        review_stage = source.split("function ReviewStage", 1)[1]
        review_stage = review_stage.split("// ─── LEFT:", 1)[0]
        passage_id_helper = source.split("function passageGroupIdFor(problem)", 1)[1]
        passage_id_helper = passage_id_helper.split("function isPassageProblem(problem)", 1)[0]
        passage_helper = source.split("function collectPassageGroupSummary(session)", 1)[1]
        passage_helper = passage_helper.split("function sessionReviewSummary(session)", 1)[0]
        summary_helper = source.split("function sessionReviewSummary(session)", 1)[1]
        summary_helper = summary_helper.split("function normalizePublishSummary", 1)[0]

        self.assertIn("prev === 'passage' ? 'all' : 'passage'", review_stage)
        self.assertIn("지문 묶음", review_stage)
        self.assertIn("이어짐", review_stage)
        self.assertIn("passageGroupCount", review_stage)
        self.assertIn("passageContinuationBlockCount", review_stage)
        self.assertIn("review-bbox-passage", review_stage)
        self.assertIn("review-bbox-passage-tag", review_stage)
        self.assertIn(".review-bbox-passage", board_html)
        self.assertIn("passageGroupCount", summary_helper)
        self.assertIn("passageProblemCount", summary_helper)
        self.assertIn("passageContinuationBlockCount", summary_helper)
        self.assertIn("fragmentProblemCount", passage_helper)
        self.assertIn("problemNumbers", passage_helper)
        self.assertIn("passageRole", passage_helper)
        self.assertIn("passageGroupIdFor(problem)", passage_helper)
        self.assertIn("passagePreQuestionContinuationBlockIds", passage_helper)
        self.assertIn("passage_pre_question_continuation_block_ids", passage_helper)
        self.assertIn("passageGroupId", passage_id_helper)
        self.assertIn("passage_group_id", passage_id_helper)
        risk_meta = source.split("const RISK_FLAG_META = {", 1)[1]
        risk_meta = risk_meta.split("};", 1)[0]
        self.assertIn("passage_cross_page_merge_check", risk_meta)
        self.assertIn("병합 확인", risk_meta)

    def test_board_uses_prebuilt_bundle_without_browser_babel(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        board_html = board_document()

        self.assertIn("app.bundle.js?v=frontend-bundle-", board_html)
        self.assertNotIn("app.js?v=", board_html)
        self.assertNotIn("vendor/babel.min.js", board_html)
        self.assertNotIn('type="text/babel"', board_html)
        self.assertIn("EDB_REPORT_RUNTIME_ERROR", board_html)
        self.assertIn("class AppErrorBoundary", source)
        self.assertIn("requiredWindowHelper(PUBLISH_GUARD", source)

    def test_review_summary_surfaces_passage_review_queue(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        review_stage = source.split("<span className=\"review-summary-title\">검수 요약</span>", 1)[1]
        review_stage = review_stage.split("{reviewSummary.warningPreview &&", 1)[0]
        queue_helper = source.split("function collectPassageReviewSummary(session", 1)[1]
        queue_helper = queue_helper.split("function sessionReviewSummary(session)", 1)[0]
        summary_helper = source.split("function sessionReviewSummary(session)", 1)[1]
        summary_helper = summary_helper.split("function normalizePublishSummary", 1)[0]

        self.assertIn("지문 확인", review_stage)
        self.assertIn("passageReviewLabel", review_stage)
        self.assertIn("passageReviewItems", queue_helper)
        self.assertIn("passage_review_items", queue_helper)
        self.assertIn("actionableProblemIds", queue_helper)
        self.assertIn("unresolvedPassageReviewItems", queue_helper)
        self.assertIn("passageReviewItemCount", queue_helper)
        self.assertIn("crossPagePassageReviewItemCount", queue_helper)
        self.assertIn("passageReviewItems", summary_helper)
        self.assertIn("passageReviewLabel", summary_helper)
        self.assertIn("passageReviewReasonLabel", review_stage)
        self.assertIn("passageReviewReasonLabel", queue_helper)
        self.assertIn("passageReviewReasonLabel", summary_helper)

    def test_review_summary_passage_review_chip_filters_only_queue_items(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        review_stage = source.split("function ReviewStage", 1)[1]
        review_stage = review_stage.split("// ─── LEFT:", 1)[0]
        queue_helper = source.split("function collectPassageReviewSummary(session", 1)[1]
        queue_helper = queue_helper.split("function sessionReviewSummary(session)", 1)[0]

        self.assertIn("passageReviewProblemIds", queue_helper)
        self.assertIn("passageReviewItemProblemIds", queue_helper)
        self.assertIn("reviewFilter === 'passage-review'", review_stage)
        self.assertIn("'passage-review' ? 'all' : 'passage-review'", review_stage)
        self.assertIn("passageReviewProblemIds", review_stage)
        self.assertIn("problemMatchesReviewFilter(problem, reviewFilter, { passageReviewProblemIds })", review_stage)

    def test_review_summary_removes_confirmed_passage_review_queue_items(self) -> None:
        run_node(
            r"""
            const fs = require('fs');
            const vm = require('vm');
            const source = fs.readFileSync('./ui_prototype/app.jsx', 'utf8');
            const start = source.indexOf('const REVIEW_STATUS_META =');
            const end = source.indexOf('function normalizePublishSummary');
            if (start < 0 || end < 0) throw new Error('review summary helper bounds not found');
            const sandbox = {};
            sandbox.globalThis = sandbox;
            vm.runInNewContext(source.slice(start, end), sandbox);
            const session = {
              review_summary: {
                actionableNeedsReviewCount: 2,
                reviewStatusCounts: { all: 2, normal: 0, check_needed: 2, failed: 0 },
                needsReviewCount: 2,
                riskFlagCounts: { passage_cross_page_merge_check: 2 },
              },
              passageReviewItemCount: 1,
              crossPagePassageReviewItemCount: 1,
              passageReviewItems: [
                {
                  numberLabel: '31-32',
                  problemIds: ['p31', 'p32'],
                  continuesAcrossPages: true,
                  reviewReasonCodes: ['cross_page_passage_group', 'passage_missing_child_questions'],
                },
              ],
              problems: [
                {
                  id: 'p31',
                  reviewStatus: 'normal',
                  riskFlags: [],
                  bbox: { width: 10, height: 10 },
                },
                {
                  id: 'p32',
                  reviewStatus: 'normal',
                  riskFlags: [],
                  bbox: { width: 10, height: 10 },
                },
              ],
              pages: [{ id: 'page-1', problemIds: ['p31', 'p32'], riskFlags: [] }],
            };
            const summary = sandbox.sessionReviewSummary(session);
            if (summary.reviewStatusCounts.check_needed !== 0) {
              throw new Error(`expected confirmed check_needed count 0, got ${summary.reviewStatusCounts.check_needed}`);
            }
            if (summary.needsReviewCount !== 0) {
              throw new Error(`expected confirmed needs review count 0, got ${summary.needsReviewCount}`);
            }
            if (summary.actionableNeedsReviewCount !== 0) {
              throw new Error(`expected confirmed actionable count 0, got ${summary.actionableNeedsReviewCount}`);
            }
            if (summary.passageReviewItemCount !== 0) {
              throw new Error(`expected confirmed passage review count 0, got ${summary.passageReviewItemCount}`);
            }
            if (summary.crossPagePassageReviewItemCount !== 0) {
              throw new Error(`expected confirmed cross-page passage review count 0, got ${summary.crossPagePassageReviewItemCount}`);
            }
            if (summary.passageReviewProblemIds.length !== 0) {
              throw new Error(`expected no confirmed passage review problem ids, got ${summary.passageReviewProblemIds.join(',')}`);
            }
            if (summary.passageReviewLabel !== '') {
              throw new Error(`expected empty confirmed passage review label, got ${summary.passageReviewLabel}`);
            }
            """
        )

    def test_review_summary_keeps_check_needed_passage_review_queue_without_flags(self) -> None:
        run_node(
            r"""
            const fs = require('fs');
            const vm = require('vm');
            const source = fs.readFileSync('./ui_prototype/app.jsx', 'utf8');
            const start = source.indexOf('const REVIEW_STATUS_META =');
            const end = source.indexOf('function normalizePublishSummary');
            if (start < 0 || end < 0) throw new Error('review summary helper bounds not found');
            const sandbox = {};
            sandbox.globalThis = sandbox;
            vm.runInNewContext(source.slice(start, end), sandbox);
            const session = {
              review_summary: {
                actionableNeedsReviewCount: 0,
                reviewStatusCounts: { all: 2, normal: 1, check_needed: 1, failed: 0 },
                needsReviewCount: 1,
                riskFlagCounts: {},
              },
              passageReviewItemCount: 1,
              crossPagePassageReviewItemCount: 1,
              passageReviewItems: [
                {
                  numberLabel: '31-32',
                  problemIds: ['p31', 'p32'],
                  continuesAcrossPages: true,
                  reviewReasonCodes: ['cross_page_passage_group', 'passage_missing_child_questions'],
                },
              ],
              problems: [
                {
                  id: 'p31',
                  reviewStatus: 'check_needed',
                  riskFlags: [],
                  bbox: { width: 10, height: 10 },
                },
                {
                  id: 'p32',
                  reviewStatus: 'normal',
                  riskFlags: [],
                  bbox: { width: 10, height: 10 },
                },
              ],
              pages: [{ id: 'page-1', problemIds: ['p31', 'p32'], riskFlags: [] }],
            };
            const summary = sandbox.sessionReviewSummary(session);
            if (summary.passageReviewItemCount !== 1) {
              throw new Error(`expected unresolved passage review count 1, got ${summary.passageReviewItemCount}`);
            }
            if (summary.crossPagePassageReviewItemCount !== 1) {
              throw new Error(`expected unresolved cross-page count 1, got ${summary.crossPagePassageReviewItemCount}`);
            }
            if (summary.passageReviewProblemIds.join(',') !== 'p31,p32') {
              throw new Error(`expected passage ids p31,p32, got ${summary.passageReviewProblemIds.join(',')}`);
            }
            if (!summary.passageReviewLabel.includes('지문 확인 1')) {
              throw new Error(`expected passage review label, got ${summary.passageReviewLabel}`);
            }
            if (summary.passageReviewReasonLabel !== '페이지 이어짐, 문항 누락') {
              throw new Error(`expected passage reason label, got ${summary.passageReviewReasonLabel}`);
            }
            """
        )

    def test_publish_guard_warns_for_unresolved_passage_review_queue_without_flags(self) -> None:
        source = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
        publish_flow = source.split("const onPublish = async () =>", 1)[1]
        publish_flow = publish_flow.split("const placements = Object.fromEntries", 1)[0]
        self.assertIn("publishReviewWarningMessage(sessionForPublish, publishReviewSummary)", publish_flow)

        run_node(
            r"""
            const fs = require('fs');
            const vm = require('vm');
            const source = fs.readFileSync('./ui_prototype/app.jsx', 'utf8');
            const start = source.indexOf('const REVIEW_STATUS_META =');
            const end = source.indexOf('function normalizePublishSummary');
            if (start < 0 || end < 0) throw new Error('review summary helper bounds not found');
            const sandbox = {};
            sandbox.globalThis = sandbox;
            vm.runInNewContext(source.slice(start, end), sandbox);
            if (typeof sandbox.publishReviewWarningMessage !== 'function') {
              throw new Error('publishReviewWarningMessage missing');
            }
            const session = {
              passageReviewItemCount: 1,
              crossPagePassageReviewItemCount: 1,
              passageReviewItems: [
                {
                  numberLabel: '31-32',
                  problemIds: ['p31', 'p32'],
                  continuesAcrossPages: true,
                },
              ],
              problems: [
                {
                  id: 'p31',
                  reviewStatus: 'check_needed',
                  riskFlags: [],
                  bbox: { width: 10, height: 10 },
                },
                {
                  id: 'p32',
                  reviewStatus: 'normal',
                  riskFlags: [],
                  bbox: { width: 10, height: 10 },
                },
              ],
              pages: [{ id: 'page-1', problemIds: ['p31', 'p32'], riskFlags: [] }],
            };
            const summary = sandbox.sessionReviewSummary(session);
            const warning = sandbox.publishReviewWarningMessage(session, summary);
            if (!warning) throw new Error('expected unresolved passage review warning');
            if (!warning.message.includes('지문 확인 1')) {
              throw new Error(`expected passage warning line, got ${warning.message}`);
            }
            if (!warning.cancelToast.includes('지문 확인 항목')) {
              throw new Error(`expected passage cancel toast, got ${warning.cancelToast}`);
            }
            if (warning.reviewFilter !== 'passage-review') {
              throw new Error(`expected passage-review focus filter, got ${warning.reviewFilter}`);
            }
            """
        )

    def test_publish_blocked_target_focuses_preflight_problem_ids(self) -> None:
        run_node(
            r"""
            const fs = require('fs');
            const vm = require('vm');
            const source = fs.readFileSync('./ui_prototype/app.jsx', 'utf8');
            const start = source.indexOf('const REVIEW_STATUS_META =');
            const end = source.indexOf('const NON_ACTIONABLE_RISK_FLAGS');
            if (start < 0 || end < 0) throw new Error('publish preflight helper bounds not found');
            const sandbox = {};
            sandbox.globalThis = sandbox;
            vm.runInNewContext(source.slice(start, end), sandbox);
            if (typeof sandbox.publishBlockedTarget !== 'function') {
              throw new Error('publishBlockedTarget missing');
            }
            const overlapTarget = sandbox.publishBlockedTarget({
              issueTypes: ['source_problem_bbox_overlap'],
              classinPreflight: {
                issues: [
                  {
                    type: 'source_problem_bbox_overlap',
                    problemId: 'p21',
                    nextProblemId: 'p22',
                  },
                ],
              },
            });
            if (overlapTarget.view !== 'review') {
              throw new Error(`expected review target, got ${overlapTarget.view}`);
            }
            if (overlapTarget.reviewFocus?.filter !== 'all') {
              throw new Error(`expected all filter, got ${overlapTarget.reviewFocus?.filter}`);
            }
            if (overlapTarget.reviewFocus?.problemIds?.join(',') !== 'p21,p22') {
              throw new Error(`expected p21,p22 focus, got ${overlapTarget.reviewFocus?.problemIds}`);
            }
            const duplicateTarget = sandbox.publishBlockedTarget({
              issueTypes: ['duplicate_problem_number'],
              classinPreflight: {
                issues: [
                  {
                    type: 'duplicate_problem_number',
                    problemIds: ['p7-a', 'p7-b'],
                  },
                ],
              },
            });
            if (duplicateTarget.reviewFocus?.problemIds?.join(',') !== 'p7-a,p7-b') {
              throw new Error(`expected duplicate ids focus, got ${duplicateTarget.reviewFocus?.problemIds}`);
            }
            const passageTarget = sandbox.publishBlockedTarget({
              issueTypes: ['passage_review_queue_remaining'],
              blockingProblemIds: ['p31', 'p32', 'p31-fragment'],
              classinPreflight: {
                issues: [
                  {
                    type: 'passage_review_queue_remaining',
                    problemIds: ['p31', 'p32', 'p31-fragment'],
                  },
                ],
              },
            });
            if (passageTarget.reviewFocus?.filter !== 'passage-review') {
              throw new Error(`expected passage-review filter, got ${passageTarget.reviewFocus?.filter}`);
            }
            if (passageTarget.reviewFocus?.problemIds?.join(',') !== 'p31,p32,p31-fragment') {
              throw new Error(`expected passage ids focus, got ${passageTarget.reviewFocus?.problemIds}`);
            }
            const missingChildTarget = sandbox.publishBlockedTarget({
              issueTypes: ['passage_missing_child_questions'],
              classinPreflight: {
                issues: [
                  {
                    type: 'passage_missing_child_questions',
                    problemIds: ['p31', 'p32', 'p34'],
                    missingChildProblemNumbers: [33],
                  },
                ],
              },
            });
            if (missingChildTarget.reviewFocus?.filter !== 'passage-review') {
              throw new Error(`expected missing child passage-review filter, got ${missingChildTarget.reviewFocus?.filter}`);
            }
            if (missingChildTarget.reviewFocus?.problemIds?.join(',') !== 'p31,p32,p34') {
              throw new Error(`expected missing child focus ids, got ${missingChildTarget.reviewFocus?.problemIds}`);
            }
            const missingChildBlock = sandbox.normalizePublishPreflightBlock({
              errorKind: 'publish_preflight_blocked',
              classinPreflight: {
                status: 'blocked',
                passed: false,
                issueCount: 1,
                issues: [{ type: 'passage_missing_child_questions', problemIds: ['p31', 'p32', 'p34'] }],
              },
            });
            if (!missingChildBlock.toastLabel.includes('제작 전 확인')) {
              throw new Error(`expected generic fallback message, got ${missingChildBlock.toastLabel}`);
            }
            if (missingChildBlock.toastLabel.includes('겹침/중복')) {
              throw new Error(`fallback message should not narrow missing child blocks: ${missingChildBlock.toastLabel}`);
            }
            const boardTarget = sandbox.publishBlockedTarget({ issueTypes: ['board_placement_overlap'] });
            if (boardTarget.view !== 'board' || boardTarget.reviewFocus !== null) {
              throw new Error(`expected board-only target, got ${JSON.stringify(boardTarget)}`);
            }
            """
        )

    def test_publish_preflight_page_chrome_issues_mark_problem_flags(self) -> None:
        run_node(
            r"""
            const fs = require('fs');
            const vm = require('vm');
            const source = fs.readFileSync('./ui_prototype/app.jsx', 'utf8');
            const start = source.indexOf('const REVIEW_STATUS_META =');
            const end = source.indexOf('const NON_ACTIONABLE_RISK_FLAGS');
            if (start < 0 || end < 0) throw new Error('publish preflight helper bounds not found');
            const sandbox = {};
            sandbox.globalThis = sandbox;
            vm.runInNewContext(source.slice(start, end), sandbox);
            if (typeof sandbox.applyPublishPreflightIssuesToSession !== 'function') {
              throw new Error('applyPublishPreflightIssuesToSession missing');
            }
            const session = {
              problems: [
                { id: 'p1', reviewStatus: 'normal', riskFlags: [], bbox: { width: 10, height: 10 } },
                { id: 'p2', reviewStatus: 'normal', riskFlags: [], bbox: { width: 10, height: 10 } },
                { id: 'p3', reviewStatus: 'normal', riskFlags: [], bbox: { width: 10, height: 10 } },
              ],
              pages: [{ id: 'page-1', problemIds: ['p1', 'p2', 'p3'], riskFlags: [] }],
            };
            const blocked = {
              issueTypes: ['step3_page_chrome_artifact'],
              classinPreflight: {
                issues: [
                  { type: 'step3_page_chrome_artifact', problemIds: ['p2'] },
                ],
              },
            };
            const marked = sandbox.applyPublishPreflightIssuesToSession(session, blocked);
            const p1 = marked.problems.find(problem => problem.id === 'p1');
            const p2 = marked.problems.find(problem => problem.id === 'p2');
            if (p1.reviewStatus !== 'normal' || p1.riskFlags.length !== 0) {
              throw new Error(`unexpected p1 mutation: ${JSON.stringify(p1)}`);
            }
            if (p2.reviewStatus !== 'failed') {
              throw new Error(`expected p2 failed, got ${p2.reviewStatus}`);
            }
            if (!p2.riskFlags.includes('step3_page_chrome_artifact')) {
              throw new Error(`expected p2 page chrome flag, got ${p2.riskFlags}`);
            }
            if (!p2.publishPreflightIssues?.some(issue => issue.type === 'step3_page_chrome_artifact')) {
              throw new Error(`expected p2 publish preflight details, got ${JSON.stringify(p2.publishPreflightIssues)}`);
            }
            if (!sandbox.hasPageChromeArtifactFlag(p2)) {
              throw new Error('expected page chrome helper to recognize marked problem');
            }
            const target = sandbox.publishBlockedTarget(blocked);
            if (target.view !== 'review') {
              throw new Error(`expected review target, got ${target.view}`);
            }
            if (target.reviewFocus?.problemIds?.join(',') !== 'p2') {
              throw new Error(`expected p2 focus, got ${target.reviewFocus?.problemIds}`);
            }
            const toast = sandbox.pageChromePreflightBlockToast(blocked);
            if (!toast.includes('3단계 이미지') || !toast.includes('빨간 문제')) {
              throw new Error(`expected page chrome toast, got ${toast}`);
            }
            """
        )


if __name__ == "__main__":
    unittest.main()
