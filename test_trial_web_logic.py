from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def run_node(script: str) -> None:
    subprocess.run(
        ["node", "-e", "const assert = require('node:assert/strict');\n" + script],
        cwd=PROJECT_ROOT,
        check=True,
    )


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class TestTrialWebLogic(unittest.TestCase):
    def test_popup_content_fills_templates_and_falls_back_to_ai(self) -> None:
        run_node(
            """
            const logic = require('./public/trial_logic.js');
            const pages = logic.popupContent('limit_pages', { max: 3, rest: 13 });
            assert.equal(pages.title, '무료 체험은 앞 3쪽까지예요');
            assert.match(pages.body, /나머지 13쪽도 프리미엄에서/);
            assert.equal(pages.badge, '✦ 프리미엄');
            assert.equal(pages.inquiryLabel, '프리미엄 도입 문의');
            assert.equal(pages.closeLabel, '계속 체험하기');
            assert.equal(logic.popupContent('limit_size', { mb: 4 }).title, '무료 체험은 4MB까지 올릴 수 있어요');
            assert.equal(logic.popupContent('limit_daily').title, '오늘의 무료 체험을 모두 사용했어요');
            assert.match(logic.popupContent('limit_daily').body, /프리미엄으로 더 누려보세요!/);
            const unknown = logic.popupContent('nope');
            assert.equal(unknown.feature, 'ai');
            for (const feature of ['edb', 'image', 'edit', 'ai', 'scan', 'limit_pages', 'limit_size', 'limit_daily']) {
              const content = logic.popupContent(feature, { max: 3, rest: 1, mb: 4 });
              assert.equal(content.feature, feature);
              assert.ok(!/[{}]/.test(content.title + content.body), feature + ' left a template hole');
            }
            """
        )

    def test_popup_content_never_leaves_template_holes_without_context(self) -> None:
        run_node(
            """
            const logic = require('./public/trial_logic.js');
            for (const feature of logic.FEATURES) {
              for (const context of [undefined, {}, { max: 3 }, { mb: 4 }, { rest: 5 }]) {
                const content = logic.popupContent(feature, context);
                assert.ok(!/[{}]/.test(content.title + content.body), feature + ' ' + JSON.stringify(context));
              }
            }
            const serverRejection = logic.popupContent('limit_pages', { max: 3, mb: 4 });
            assert.equal(serverRejection.title, '페이지가 너무 많은 파일이에요');
            assert.match(serverRejection.body, /프리미엄에서는 시험지 한 권을 통째로/);
            assert.equal(logic.popupContent('limit_pages', { max: 3, rest: 13 }).title, '무료 체험은 앞 3쪽까지예요');
            assert.equal(logic.popupContent('limit_size', {}).feature, 'limit_size');
            """
        )

    def test_precheck_file(self) -> None:
        run_node(
            """
            const { precheckFile } = require('./public/trial_logic.js');
            const limits = { max_bytes: 4000000 };
            assert.equal(precheckFile({ name: 'exam.pdf', type: 'application/pdf', size: 1000 }, limits), null);
            assert.equal(precheckFile({ name: 'EXAM.PDF', type: '', size: 1000 }, limits), null);
            assert.deepEqual(
              precheckFile({ name: 'photo.jpg', type: 'image/jpeg', size: 1000 }, limits),
              { code: 'image_not_supported', feature: 'scan', message: '무료 체험은 글자가 들어 있는 PDF만 나눠 드려요.' },
            );
            assert.equal(precheckFile({ name: 'scan.HEIC', type: '', size: 10 }, limits).feature, 'scan');
            assert.deepEqual(
              precheckFile({ name: 'notes.hwp', type: '', size: 1000 }, limits),
              { code: 'bad_type', feature: null, message: 'PDF 파일만 올릴 수 있어요.' },
            );
            assert.equal(precheckFile({ name: 'big.pdf', type: 'application/pdf', size: 4000001 }, limits).code, 'too_large');
            assert.equal(precheckFile({ name: 'big.pdf', type: 'application/pdf', size: 4000001 }, limits).feature, 'limit_size');
            assert.equal(precheckFile({ name: 'empty.pdf', type: 'application/pdf', size: 0 }, limits).code, 'empty');
            """
        )

    def test_interpret_error(self) -> None:
        run_node(
            """
            const { interpretError } = require('./public/trial_logic.js');
            const body = JSON.stringify({ error: { code: 'no_text_layer', message: '무료 체험은 글자가 들어 있는 PDF만 나눠 드려요.', feature: 'scan' } });
            assert.deepEqual(interpretError(422, body), { code: 'no_text_layer', feature: 'scan', message: '무료 체험은 글자가 들어 있는 PDF만 나눠 드려요.' });
            assert.equal(interpretError(413, 'Request Entity Too Large').feature, 'limit_size');
            assert.equal(interpretError(504, '<html>FUNCTION_INVOCATION_TIMEOUT</html>').feature, 'ai');
            assert.equal(interpretError(500, '').code, 'parse_failed');
            assert.deepEqual(interpretError(503, 'x'), { code: 'busy', feature: null, message: '지금은 체험이 어려워요. 잠시 후 다시 시도해 주세요.' });
            assert.equal(interpretError(0, '').code, 'network');
            assert.equal(interpretError(418, '{"error": "nope"}').code, 'network');
            const injected = interpretError(422, JSON.stringify({ error: { code: 'x', message: 42, feature: 'evil' } }));
            assert.equal(injected.feature, null);
            assert.equal(typeof injected.message, 'string');
            """
        )

    def test_region_style_percentages_are_clamped(self) -> None:
        run_node(
            """
            const { regionStyle } = require('./public/trial_logic.js');
            assert.deepEqual(
              regionStyle({ left: 60, top: 80, width: 300, height: 400 }, { width: 600, height: 800 }),
              { left: '10%', top: '10%', width: '50%', height: '50%' },
            );
            assert.deepEqual(
              regionStyle({ left: -10, top: 790, width: 700, height: 100 }, { width: 600, height: 800 }),
              { left: '0%', top: '98.75%', width: '100%', height: '1.25%' },
            );
            assert.deepEqual(
              regionStyle({ left: 1, top: 1, width: 1, height: 1 }, { width: 3, height: 3 }),
              { left: '33.333%', top: '33.333%', width: '33.333%', height: '33.333%' },
            );
            """
        )

    def test_summary_banner_labels_and_remaining(self) -> None:
        run_node(
            """
            const logic = require('./public/trial_logic.js');
            const payload = {
              elapsed_ms: 1720,
              source_page_count: 16,
              processed_page_count: 3,
              processed_page_limit: 3,
              problems: [{ number: null, title: '지문 1~3' }, { number: 1, title: '1.' }, { number: 2, title: '2.' }],
            };
            assert.deepEqual(logic.summarize(payload), { headline: '문항 2개와 지문 1개를 찾았어요', seconds: '1.7초', questionCount: 2, passageCount: 1 });
            assert.equal(logic.summarize({ ...payload, problems: [{ number: 5 }] }).headline, '문항 1개를 찾았어요');
            assert.equal(logic.summarize({ ...payload, problems: [] }).headline, '문항을 찾지 못했어요');
            assert.deepEqual(logic.pagesBanner(payload), {
              text: '✦ 무료 체험은 앞 3쪽까지예요 · 나머지 13쪽은 프리미엄으로',
              feature: 'limit_pages',
              context: { max: 3, rest: 13 },
            });
            assert.equal(logic.pagesBanner({ ...payload, source_page_count: 3 }), null);
            assert.equal(logic.problemLabel({ number: 7, title: '7.' }), '7번');
            assert.equal(logic.problemLabel({ number: null, title: '지문 4~9' }), '지문 4~9');
            assert.equal(logic.problemLabel({ number: null, title: '' }), '지문');
            assert.equal(logic.remainingText(2), '오늘 2회 남음');
            assert.equal(logic.remainingText(0), '오늘 무료 체험을 모두 사용했어요');
            assert.equal(logic.isSafeImageSource('data:image/jpeg;base64,AAAA'), true);
            assert.equal(logic.isSafeImageSource('javascript:alert(1)'), false);
            assert.equal(logic.isSafeImageSource('https://evil.example/x.jpg'), false);
            """
        )

    def test_board_preview_helpers(self) -> None:
        run_node(
            """
            const logic = require('./public/trial_logic.js');
            assert.equal(logic.isSafeImageSource('data:image/webp;base64,AAAA'), true);
            assert.equal(logic.isSafeImageSource('data:image/jpeg;base64,AAAA'), true);
            assert.equal(logic.isSafeImageSource('data:image/svg+xml;base64,AAAA'), false);
            assert.equal(logic.isSafeImageSource('https://example.com/x.png'), false);
            assert.equal(logic.isSafeImageSource(null), false);
            const raw = 'data:image/webp;base64,RAW';
            const board = 'data:image/webp;base64,BOARD';
            assert.equal(logic.hasBoardPreviews({ board_previews: true, problems: [{ preview: raw, board }] }), true);
            assert.equal(logic.hasBoardPreviews({ board_previews: true, problems: [{ preview: raw, board: null }] }), false);
            assert.equal(logic.hasBoardPreviews({ board_previews: false, problems: [{ preview: raw, board }] }), false);
            assert.equal(logic.hasBoardPreviews({ problems: [] }), false);
            assert.equal(logic.hasBoardPreviews(null), false);
            assert.equal(logic.cardImageSource({ preview: raw, board }, 'board'), board);
            assert.equal(logic.cardImageSource({ preview: raw, board }, 'raw'), raw);
            assert.equal(logic.cardImageSource({ preview: raw, board: null }, 'board'), raw);
            assert.equal(logic.cardImageSource({ preview: 'javascript:alert(1)', board: null }, 'raw'), null);
            assert.equal(logic.cardImageSource(null, 'board'), null);
            """
        )


class TestTrialPageMarkup(unittest.TestCase):
    PAGES = ("public/index.html", "public/demo/index.html")

    def test_both_pages_have_the_preview_toggle_and_the_new_wait_copy(self) -> None:
        for relative in self.PAGES:
            html = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(page=relative):
                self.assertIn('id="preview-toggle"', html)
                self.assertIn('data-mode="raw" aria-pressed="true"', html)
                self.assertIn('data-mode="board" aria-pressed="false"', html)
                self.assertIn('class="problems-panel"', html)
                self.assertIn("보통 15~30초 걸려요", html)
                self.assertNotIn("10~20초", html)

    def test_app_script_switches_cards_through_the_shared_logic(self) -> None:
        script = (PROJECT_ROOT / "public/app.js").read_text(encoding="utf-8")
        self.assertIn("logic.cardImageSource(", script)
        self.assertIn("logic.hasBoardPreviews(", script)
        self.assertIn('"problem-card--board"', script)
        css = (PROJECT_ROOT / "public/style.css").read_text(encoding="utf-8")
        self.assertIn(".problem-card--board img", css)
        self.assertIn('.preview-toggle button[aria-pressed="true"]', css)


if __name__ == "__main__":
    unittest.main()
