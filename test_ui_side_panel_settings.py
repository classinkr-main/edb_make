"""Right panel contract: every control lives where its decision is made.

The settings tab used to open on three hard-coded "ON" badges and a 368px
conversion table while the AI switch sat two screens down; session-wide
layout sat on top of a single problem's options. These tests pin the layout
that replaced it.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
APP = (PROJECT_ROOT / "ui_prototype" / "app.jsx").read_text(encoding="utf-8")
CSS = (PROJECT_ROOT / "ui_prototype" / "board.css").read_text(encoding="utf-8")


def component(name: str) -> str:
    """Source of one top-level component, up to the next top-level function."""

    return APP.split(f"function {name}(", 1)[1].split("\nfunction ", 1)[0]


SIDE_PANEL = component("SidePanel")
LAYOUT_CONTROLS = component("BoardLayoutControls")
ITEM_TAB = SIDE_PANEL.split("{tab === 'item' && (", 1)[1].split("{tab === 'placement' && (", 1)[0]
PLACEMENT_TAB = SIDE_PANEL.split("{tab === 'placement' && (", 1)[1].split("{tab === 'board' && (", 1)[0]
SETTINGS_TAB = SIDE_PANEL.split("{tab === 'board' && (", 1)[1]


class TestSettingsTabHoldsRealSettings(unittest.TestCase):
    def test_no_hardcoded_badges_pose_as_settings(self) -> None:
        self.assertNotIn(">ON</span>", SETTINGS_TAB)
        self.assertNotIn("칠판 동작", SETTINGS_TAB)

    def test_bulk_actions_left_the_settings_tab(self) -> None:
        self.assertNotIn("일괄 작업", SETTINGS_TAB)
        self.assertNotIn("applyToAll('s1')", SIDE_PANEL)
        self.assertNotIn("applyToAll('s2')", SIDE_PANEL)

    def test_accent_picker_no_longer_overrides_the_token_pairs(self) -> None:
        self.assertNotIn("setAccent", SIDE_PANEL)
        self.assertNotIn("ACCENTS.map", SIDE_PANEL)

    def test_sections_are_ordered_by_how_often_they_matter(self) -> None:
        order = ("AI <span", "칠판 <span", "문서 변환 <span", "앱 업데이트 <span", "문제 신고 <span")
        positions = [SETTINGS_TAB.index(label) for label in order]

        self.assertEqual(sorted(positions), positions)

    def test_ai_switch_key_and_recognition_share_the_first_section(self) -> None:
        ai_section = SETTINGS_TAB.split("AI <span", 1)[1].split("칠판 <span", 1)[0]

        self.assertIn("AI 전체 사용", ai_section)
        self.assertIn("Gemini API 키", ai_section)
        self.assertIn("onClick={onRecognizeSession}", ai_section)
        self.assertIn("현재 자료 문제 인식", ai_section)
        self.assertEqual(1, APP.count("onClick={onRecognizeSession}"))

    def test_saved_key_collapses_to_a_status_row(self) -> None:
        self.assertIn("const [keyEditorOpen, setKeyEditorOpen] = useState(false);", SIDE_PANEL)
        self.assertIn("(!userSettings?.hasGeminiApiKey || keyEditorOpen) && (", SETTINGS_TAB)
        self.assertIn("onClick={() => setKeyEditorOpen(true)}", SETTINGS_TAB)
        self.assertIn("setKeyEditorOpen(false); }}", SETTINGS_TAB)

    def test_conversion_diagnostics_never_open_on_their_own(self) -> None:
        self.assertNotIn("setHangulDetailsExpanded(hangulDetailsOpen)", APP)
        self.assertIn("const [hangulDetailsExpanded, setHangulDetailsExpanded] = useState(false);", SIDE_PANEL)
        self.assertIn("aria-expanded={hangulDetailsExpanded ? 'true' : 'false'}", SETTINGS_TAB)

    def test_upload_method_moved_out_of_settings(self) -> None:
        self.assertNotIn("intent-control", SETTINGS_TAB)
        self.assertNotIn("업로드 옵션", SETTINGS_TAB)


class TestBoardColour(unittest.TestCase):
    def test_swatches_are_keyboard_reachable_radios(self) -> None:
        self.assertIn('role="radiogroup" aria-label="칠판 배경"', SETTINGS_TAB)
        swatch = SETTINGS_TAB.split("BOARD_COLORS.map(c => (", 1)[1].split("))}", 1)[0]

        self.assertIn("<button", swatch)
        self.assertNotIn("<div", swatch)
        self.assertIn('role="radio"', swatch)
        self.assertIn("aria-checked={boardColor === c}", swatch)
        self.assertIn("aria-label={BOARD_COLOR_LABELS[c] || c}", swatch)
        self.assertIn(".swatches .sw:focus-visible{", CSS)

    def test_board_colour_survives_a_reload(self) -> None:
        init = APP.split("function initialTweakValues(){", 1)[1].split("\n}", 1)[0]

        self.assertIn("useTweaks(useMemo(initialTweakValues, []))", APP)
        self.assertIn("writeStoredPreference(BOARD_COLOR_KEY, v)", APP)
        self.assertIn("BOARD_COLORS.includes(value)", init)


class TestPlacementOwnsSessionLayout(unittest.TestCase):
    def test_layout_is_a_collapsible_placement_section(self) -> None:
        self.assertIn("<BoardLayoutControls", PLACEMENT_TAB)
        self.assertNotIn("<BoardLayoutControls", ITEM_TAB)
        self.assertIn("open={boardLayoutOpen || placementScope === 'all'}", PLACEMENT_TAB)
        self.assertIn("<strong>전체 레이아웃</strong>", LAYOUT_CONTROLS)
        self.assertIn("aria-expanded={open}", LAYOUT_CONTROLS)
        self.assertIn("<small>{summary}</small>", LAYOUT_CONTROLS)

    def test_permanently_disabled_column_buttons_are_gone(self) -> None:
        self.assertNotIn("[1,2,3].map", LAYOUT_CONTROLS)
        self.assertIn("1열로 되돌리기", LAYOUT_CONTROLS)

    def test_compact_gap_warning_only_shows_in_compact_mode(self) -> None:
        self.assertIn("gapMode === LAYOUT_GAP_MODE_COMPACT ? (", LAYOUT_CONTROLS)
        self.assertIn('className="layout-gap-hint"', LAYOUT_CONTROLS)

    def test_publish_guard_points_at_the_new_home(self) -> None:
        self.assertIn("배치 탭의 전체 레이아웃에서 1열로 되돌린 뒤", APP)
        self.assertNotIn("설정에서 1열로 바꾼 뒤", APP)


class TestItemTab(unittest.TestCase):
    def test_bulk_apply_sits_on_the_step_choice_it_applies(self) -> None:
        header = ITEM_TAB.split("처리 방식 선택 <span", 1)[1].split('<div className="steps">', 1)[0]

        self.assertIn('className="section-hd-action"', header)
        self.assertIn("onClick={() => applyToAll(item.step)}", header)
        # Review view already has the confirm bar's own "전체 적용".
        self.assertIn("!showItemConfirmBar", header)


class TestRememberedPanelState(unittest.TestCase):
    def test_tab_and_open_sections_are_remembered(self) -> None:
        self.assertIn(
            "useStoredPreference(RIGHT_PANEL_TAB_KEY, 'item', value => RIGHT_PANEL_TABS.includes(value))",
            SIDE_PANEL,
        )
        self.assertIn("useStoredPreference(ADVANCED_SETTINGS_OPEN_KEY, false, isStoredBoolean)", SIDE_PANEL)
        self.assertIn("useStoredPreference(BOARD_LAYOUT_OPEN_KEY, false, isStoredBoolean)", SIDE_PANEL)

    def test_storage_failures_never_break_rendering(self) -> None:
        for name in ("readStoredPreference", "writeStoredPreference"):
            with self.subTest(helper=name):
                body = APP.split(f"function {name}(", 1)[1].split("\n}", 1)[0]
                self.assertIn("try {", body)
                self.assertIn("catch (_error)", body)


class TestTabNavigation(unittest.TestCase):
    def test_tabs_announce_and_honour_alt_digit_shortcuts(self) -> None:
        for digit in ("1", "2", "3"):
            with self.subTest(digit=digit):
                self.assertEqual(1, SIDE_PANEL.count(f'aria-keyshortcuts="Alt+{digit}"'))
        self.assertIn("{ Digit1: 'item', Digit2: 'placement', Digit3: 'board' }", APP)

    def test_shortcuts_stay_out_of_text_fields_and_other_chords(self) -> None:
        handler = SIDE_PANEL.split("const onKeyDown = (evt) => {", 1)[1].split("};", 1)[0]

        self.assertIn("isEditableKeyboardTarget(evt.target)", handler)
        self.assertIn("evt.metaKey || evt.ctrlKey || evt.shiftKey", handler)

    def test_choosing_a_problem_leaves_the_settings_tab(self) -> None:
        effect = SIDE_PANEL.split("const lastItemIdRef = useRef(", 1)[1].split("}, [item?.id]);", 1)[0]

        self.assertIn("tab === 'board'", effect)
        self.assertIn("setTab('item')", effect)
        # A session finishing its load is not a choice.
        self.assertIn("previousId != null", effect)


class TestStatusChips(unittest.TestCase):
    def test_no_status_chip_paints_its_own_background_inline(self) -> None:
        inline = re.findall(r"className=\{?[`\"]pos-tag[^>]*?style=\{\{\s*background", APP)

        self.assertEqual([], inline, "status chips must use the tone-* classes")

    def test_conversion_status_returns_tone_names(self) -> None:
        body = APP.split("function hangulRuntimeStatusMeta(hangul){", 1)[1].split("\n}", 1)[0]
        tones = set(re.findall(r"tone: '([^']+)'", body))

        self.assertEqual({"neutral", "ok", "warn", "danger"}, tones)
        for tone in tones:
            with self.subTest(tone=tone):
                self.assertIn(f".pos-tag.tone-{tone}{{", CSS)

    def test_update_status_returns_tone_names(self) -> None:
        body = SIDE_PANEL.split("const updateStatusTone = ", 1)[1].split(";", 1)[0]
        # Only the values each branch yields, not the statuses it compares against.
        tones = set(re.findall(r"[?:]\s*'([a-z]+)'", body))

        self.assertEqual({"danger", "accent", "ok", "neutral"}, tones)
        self.assertNotIn("var(--", body)
        for tone in tones:
            with self.subTest(tone=tone):
                self.assertIn(f".pos-tag.tone-{tone}{{", CSS)


if __name__ == "__main__":
    unittest.main()
