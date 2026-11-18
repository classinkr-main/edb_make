from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def run_node(script: str) -> None:
    subprocess.run(["node", "-e", script], cwd=PROJECT_ROOT, check=True)


class TestUiPublishSummaryHelper(unittest.TestCase):
    def test_format_record_count_label_splits_core_and_supplemental_counts(self) -> None:
        run_node(
            """
            const { formatRecordCountLabel } = require('./ui_prototype/publish_summary.js');
            const label = formatRecordCountLabel({ coreProblemCount: 45, supplementalItemCount: 1, recordCount: 46 });
            if (label !== '45문항 + 자료 1') {
              throw new Error(`unexpected label ${label}`);
            }
            """
        )

    def test_format_record_count_label_falls_back_to_record_count(self) -> None:
        run_node(
            """
            const { formatRecordCountLabel } = require('./ui_prototype/publish_summary.js');
            const label = formatRecordCountLabel({ recordCountActual: 8 });
            if (label !== '8개 자료') {
              throw new Error(`unexpected fallback label ${label}`);
            }
            """
        )

    def test_normalize_publish_summary_keeps_count_label(self) -> None:
        run_node(
            """
            const { normalizePublishSummary } = require('./ui_prototype/publish_summary.js');
            const summary = normalizePublishSummary({
              edbFileName: 'lesson.edb',
              recordCount: 46,
              coreProblemCount: 45,
              supplementalItemCount: 1,
              recordCountLabel: '45문항 + 자료 1',
            });
            if (summary.recordCountLabel !== '45문항 + 자료 1') {
              throw new Error(`label not preserved: ${summary.recordCountLabel}`);
            }
            if (summary.coreProblemCount !== 45 || summary.supplementalItemCount !== 1) {
              throw new Error('core/supplemental counts not normalized');
            }
            """
        )

    def test_normalize_publish_summary_exposes_layout_diagnostics_label(self) -> None:
        run_node(
            """
            const { normalizePublishSummary } = require('./ui_prototype/publish_summary.js');
            const summary = normalizePublishSummary({
              edbFileName: 'lesson.edb',
              recordCount: 1,
              layoutDiagnostics: {
                autoExtendedCount: 1,
                overlapRiskCount: 0,
                maxRenderedHeightPages: 1.54,
              },
            });
            if (summary.layoutDiagnosticsLabel !== '긴 이미지 자동 확장 1 · 최대 1.54p') {
              throw new Error(`unexpected layout label: ${summary.layoutDiagnosticsLabel}`);
            }
            if (summary.layoutDiagnostics.autoExtendedCount !== 1) {
              throw new Error('layout diagnostics were not preserved');
            }
            """
        )

    def test_normalize_publish_summary_exposes_artifact_availability(self) -> None:
        run_node(
            """
            const { normalizePublishSummary } = require('./ui_prototype/publish_summary.js');
            const summary = normalizePublishSummary({
              edbFileName: 'missing.edb',
              edbPath: '/tmp/missing.edb',
              edbFileUri: '/api/file?path=missing',
              edbFileExists: false,
              outputDir: '/tmp/missing-output',
              outputDirExists: false,
            });
            if (summary.edbFileExists !== false || summary.outputDirExists !== false) {
              throw new Error('artifact existence flags were not preserved');
            }
            if (summary.canDownload !== false || summary.canOpenOutputDir !== false) {
              throw new Error('missing artifacts should disable publish actions');
            }
            if (summary.canOpenEdbFile !== false) {
              throw new Error('missing EDB file should disable local open action');
            }
            """
        )

    def test_normalize_publish_summary_exposes_split_edb_parts(self) -> None:
        run_node(
            """
            const { normalizePublishSummary } = require('./ui_prototype/publish_summary.js');
            const summary = normalizePublishSummary({
              edbParts: [
                {
                  partIndex: 1,
                  partCount: 2,
                  edbFileName: 'lesson_part01.edb',
                  edbPath: '/tmp/lesson_part01.edb',
                  edbFileUri: '/api/file?path=part1',
                  edbFileExists: true,
                  recordCount: 41,
                  pageCountHint: 50,
                },
                {
                  part_index: 2,
                  part_count: 2,
                  edb_file_name: 'lesson_part02.edb',
                  edb_path: '/tmp/lesson_part02.edb',
                  edb_file_uri: '/api/file?path=part2',
                  edb_file_exists: true,
                  record_count: 1,
                  placement_count: 1,
                  flow_end_pages: 1.2,
                  page_count_hint: 50,
                },
              ],
              edbSplit: false,
              edbPartCount: 1,
            });
            if (!summary.edbSplit || summary.edbPartCount !== 2 || summary.edbParts.length !== 2) {
              throw new Error('split EDB metadata was not normalized');
            }
            if (!summary.canDownload || !summary.canOpenEdbFile) {
              throw new Error('existing split EDB parts should enable artifact actions');
            }
            if (summary.edbParts[1].partIndex !== 2 || summary.edbParts[1].edbFileName !== 'lesson_part02.edb') {
              throw new Error('snake_case split part payload was not normalized');
            }
            if (summary.edbParts[1].placementCount !== 1 || summary.edbParts[1].flowEndPages !== 1.2) {
              throw new Error('rendered part usage was not normalized');
            }
            if (summary.edbParts.some(part => part.pageCountHint > 50)) {
              throw new Error('test fixture should model ClassIn-safe 50-page parts');
            }
            """
        )

    def test_normalize_publish_summary_enables_local_edb_open_when_path_exists(self) -> None:
        run_node(
            """
            const { normalizePublishSummary } = require('./ui_prototype/publish_summary.js');
            const summary = normalizePublishSummary({
              edbFileName: 'lesson.edb',
              edbPath: '/tmp/lesson.edb',
              edbFileUri: '/api/file?path=lesson',
              edbFileExists: true,
            });
            if (!summary.canOpenEdbFile) {
              throw new Error('existing EDB path should enable local open action');
            }
            """
        )

    def test_normalize_publish_summary_exposes_classin_handoff_actions(self) -> None:
        run_node(
            """
            const { normalizePublishSummary } = require('./ui_prototype/publish_summary.js');
            const summary = normalizePublishSummary({
              edbFileName: 'lesson.edb',
              edbFileUri: '/api/file?path=lesson',
              classinHandoffUri: '/api/file?path=handoff-json',
              classinHandoffMarkdownUri: '/api/file?path=handoff-md',
            });
            if (summary.classinHandoffUri !== '/api/file?path=handoff-json') {
              throw new Error('handoff json uri not normalized');
            }
            if (summary.classinHandoffMarkdownUri !== '/api/file?path=handoff-md') {
              throw new Error('handoff markdown uri not normalized');
            }
            if (!summary.canOpenClassinHandoff) {
              throw new Error('handoff action should be enabled');
            }
            """
        )

    def test_normalize_publish_summary_exposes_classin_review_status(self) -> None:
        run_node(
            """
            const { normalizePublishSummary } = require('./ui_prototype/publish_summary.js');
            const summary = normalizePublishSummary({
              edbFileName: 'lesson.edb',
              edbPath: '/tmp/lesson.edb',
              edbFileUri: '/api/file?path=lesson',
              edbFileExists: true,
              classinReview: {
                status: 'passed',
                statusLabel: 'ClassIn 확인 완료',
                reviewedAt: '2026-06-14T00:30:00+09:00',
              },
            });
            if (summary.classinReviewStatus !== 'passed') {
              throw new Error(`review status not normalized: ${summary.classinReviewStatus}`);
            }
            if (summary.classinReviewStatusLabel !== 'ClassIn 확인 완료') {
              throw new Error(`review label not normalized: ${summary.classinReviewStatusLabel}`);
            }
            if (!summary.classinReviewPassed) {
              throw new Error('passed review should be exposed');
            }
            if (summary.canMarkClassinReviewComplete !== false) {
              throw new Error('already passed review should not offer completion action');
            }
            """
        )

    def test_normalize_publish_summary_exposes_classin_preflight_status(self) -> None:
        run_node(
            """
            const { normalizePublishSummary } = require('./ui_prototype/publish_summary.js');
            const summary = normalizePublishSummary({
              edbFileName: 'lesson.edb',
              edbFileUri: '/api/file?path=lesson',
              classinPreflight: {
                status: 'needs_attention',
                passed: false,
                issueCount: 2,
                checkedProblemCount: 3,
              },
            });
            if (summary.classinPreflightStatus !== 'needs_attention') {
              throw new Error(`preflight status not normalized: ${summary.classinPreflightStatus}`);
            }
            if (summary.classinPreflightIssueCount !== 2) {
              throw new Error(`preflight issue count not normalized: ${summary.classinPreflightIssueCount}`);
            }
            if (summary.classinPreflightPassed !== false) {
              throw new Error('preflight passed flag should be false');
            }
            if (!summary.classinPreflightStatusLabel.includes('주의 2')) {
              throw new Error(`preflight label missing issue count: ${summary.classinPreflightStatusLabel}`);
            }
            """
        )

    def test_normalize_publish_summary_labels_top_classin_preflight_issues(self) -> None:
        run_node(
            """
            const { normalizePublishSummary } = require('./ui_prototype/publish_summary.js');
            const summary = normalizePublishSummary({
              edbFileName: 'lesson.edb',
              edbFileUri: '/api/file?path=lesson',
              classinPreflight: {
                status: 'needs_attention',
                passed: false,
                issueCount: 5,
                issues: [
                  { type: 'source_problem_bbox_overlap', problemTitle: '21.', nextProblemTitle: '22.' },
                  { type: 'source_problem_bbox_overlap', problemTitle: '31.', nextProblemTitle: '32.' },
                  { type: 'board_placement_overlap', problemTitle: '33.', nextProblemTitle: '34.' },
                  { type: 'review_flags_remaining', problemTitle: '35.' },
                  { type: 'passage_missing_child_questions', problemTitle: '31-34', missingChildProblemNumbers: [33] },
                ],
              },
            });
            if (!Array.isArray(summary.classinPreflightIssueLabels)) {
              throw new Error('preflight issue labels should be an array');
            }
            const labels = summary.classinPreflightIssueLabels.join(' / ');
            if (!labels.includes('문항 영역 겹침 2')) {
              throw new Error(`missing source overlap count: ${labels}`);
            }
            if (!labels.includes('판서 배치 겹침 1')) {
              throw new Error(`missing board overlap count: ${labels}`);
            }
            if (!labels.includes('검수 플래그 남음 1')) {
              throw new Error(`missing review flag count: ${labels}`);
            }
            if (!labels.includes('문항 누락 1')) {
              throw new Error(`missing passage child count: ${labels}`);
            }
            if (summary.classinPreflightIssueSummaryLabel !== '문항 영역 겹침 2 · 판서 배치 겹침 1 · 검수 플래그 남음 1 · 문항 누락 1') {
              throw new Error(`unexpected issue summary: ${summary.classinPreflightIssueSummaryLabel}`);
            }
            """
        )

    def test_normalize_publish_preflight_block_labels_problem_id_issues(self) -> None:
        run_node(
            """
            const { normalizePublishPreflightBlock } = require('./ui_prototype/publish_summary.js');
            const block = normalizePublishPreflightBlock({
              errorKind: 'publish_preflight_blocked',
              classinPreflight: {
                status: 'blocked',
                issueCount: 2,
                issues: [
                  { type: 'missing_problem_id' },
                  { type: 'duplicate_problem_id' },
                ],
              },
            });
            if (block.issueSummaryLabel !== '문항 ID 누락 1 · 문항 ID 중복 1') {
              throw new Error(`unexpected problem ID labels: ${block.issueSummaryLabel}`);
            }
            """
        )

    def test_normalize_publish_preflight_block_labels_server_rejection(self) -> None:
        run_node(
            """
            const { normalizePublishPreflightBlock } = require('./ui_prototype/publish_summary.js');
            const block = normalizePublishPreflightBlock({
              ok: false,
              errorKind: 'publish_preflight_blocked',
              error: 'ClassIn 사전점검에서 겹침/중복 문제가 발견되어 EDB publish를 중단했습니다.',
              classinPreflight: {
                status: 'blocked',
                passed: false,
                issueCount: 4,
                issues: [
                  { type: 'source_problem_bbox_overlap', problemTitle: '21.', nextProblemTitle: '22.' },
                  { type: 'source_problem_bbox_overlap', problemTitle: '31.', nextProblemTitle: '32.' },
                  { type: 'duplicate_problem_number', problemTitle: '7.' },
                  { type: 'passage_missing_child_questions', problemTitle: '31-34', missingChildProblemNumbers: [33] },
                ],
              },
              blockingProblemIds: ['p21', 'p22', 'p7-a'],
            });
            if (!block || !block.blocked) {
              throw new Error('server rejection should normalize to a blocked payload');
            }
            if (block.issueCount !== 4) {
              throw new Error(`issue count not preserved: ${block.issueCount}`);
            }
            if (block.issueSummaryLabel !== '문항 영역 겹침 2 · 중복 번호 1 · 문항 누락 1') {
              throw new Error(`unexpected issue summary: ${block.issueSummaryLabel}`);
            }
            if (!block.toastLabel.includes('문항 영역 겹침 2')) {
              throw new Error(`toast label missing issue summary: ${block.toastLabel}`);
            }
            if (!block.toastLabel.includes('문항 누락 1')) {
              throw new Error(`toast label missing passage child summary: ${block.toastLabel}`);
            }
            if (JSON.stringify(block.blockingProblemIds) !== JSON.stringify(['p21', 'p22', 'p7-a'])) {
              throw new Error(`blocking problem ids not preserved: ${JSON.stringify(block.blockingProblemIds)}`);
            }
            """
        )

    def test_normalize_publish_preflight_block_labels_passage_review_queue(self) -> None:
        run_node(
            """
            const { normalizePublishPreflightBlock } = require('./ui_prototype/publish_summary.js');
            const block = normalizePublishPreflightBlock({
              ok: false,
              errorKind: 'publish_preflight_blocked',
              classinPreflight: {
                status: 'blocked',
                passed: false,
                issueCount: 1,
                issues: [
                  {
                    type: 'passage_review_queue_remaining',
                    problemTitle: '31-32',
                    reviewReasonCodes: ['cross_page_passage_group', 'passage_fragment'],
                  },
                ],
              },
            });
            if (block.issueSummaryLabel !== '지문 확인 필요 1') {
              throw new Error(`unexpected passage review queue summary: ${block.issueSummaryLabel}`);
            }
            if (!block.toastLabel.includes('지문 확인 필요 1')) {
              throw new Error(`toast label missing passage review queue summary: ${block.toastLabel}`);
            }
            if (!block.toastLabel.includes('제작 전 확인')) {
              throw new Error(`toast label should use generic preflight fallback message: ${block.toastLabel}`);
            }
            if (block.toastLabel.includes('겹침/중복')) {
              throw new Error(`toast label should not narrow passage review blocks to overlap/duplicate issues: ${block.toastLabel}`);
            }
            """
        )

    def test_normalize_publish_summary_exposes_passage_group_metrics(self) -> None:
        run_node(
            """
            const { normalizePublishSummary } = require('./ui_prototype/publish_summary.js');
            const summary = normalizePublishSummary({
              edbFileName: 'lesson.edb',
              edbFileUri: '/api/file?path=lesson',
              passageGroups: [
                {
                  id: 'page-1-passage-13-16',
                  problemCount: 4,
                  problemNumbers: [13, 14, 15, 16],
                  continuesAcrossPages: true,
                },
              ],
              passageProblemCount: 4,
              crossPagePassageGroupCount: 1,
            });
            if (summary.passageGroupCount !== 1) {
              throw new Error(`passage group count not normalized: ${summary.passageGroupCount}`);
            }
            if (summary.passageProblemCount !== 4) {
              throw new Error(`passage problem count not normalized: ${summary.passageProblemCount}`);
            }
            if (summary.crossPagePassageGroupCount !== 1) {
              throw new Error(`cross-page group count not normalized: ${summary.crossPagePassageGroupCount}`);
            }
            if (summary.passageGroupLabel !== '지문 묶음 1 · 4문항 · 페이지 이어짐 1') {
              throw new Error(`unexpected passage label: ${summary.passageGroupLabel}`);
            }
            """
        )

    def test_normalize_publish_summary_exposes_passage_review_queue(self) -> None:
        run_node(
            """
            const { normalizePublishSummary } = require('./ui_prototype/publish_summary.js');
            const passageReviewItems = [
              {
                groupId: 'hwp-text-passage-31-34',
                numberLabel: '31-34',
                problemIds: ['p31', 'p32'],
                sourcePageIds: ['page-5', 'page-6'],
                problemCount: 2,
                continuesAcrossPages: true,
                reviewReasonCodes: ['cross_page_passage_group'],
              },
            ];
            const summary = normalizePublishSummary({
              edbFileName: 'lesson.edb',
              edbFileUri: '/api/file?path=lesson',
              passageReviewItems,
            });
            if (summary.passageReviewItemCount !== 1) {
              throw new Error(`passage review item count not normalized: ${summary.passageReviewItemCount}`);
            }
            if (summary.crossPagePassageReviewItemCount !== 1) {
              throw new Error(`cross-page passage review count not normalized: ${summary.crossPagePassageReviewItemCount}`);
            }
            if (summary.passageReviewItems[0].groupId !== 'hwp-text-passage-31-34') {
              throw new Error(`passage review items not preserved`);
            }
            if (summary.passageReviewLabel !== '지문 확인 1 · 페이지 이어짐 1') {
              throw new Error(`unexpected passage review label: ${summary.passageReviewLabel}`);
            }
            """
        )

    def test_normalize_publish_summary_labels_passage_review_reasons(self) -> None:
        run_node(
            """
            const { normalizePublishSummary } = require('./ui_prototype/publish_summary.js');
            const summary = normalizePublishSummary({
              edbFileName: 'lesson.edb',
              edbFileUri: '/api/file?path=lesson',
              passageReviewItems: [
                {
                  groupId: 'hwp-text-passage-31-34',
                  numberLabel: '31-34',
                  problemIds: ['p31', 'p32', 'p34'],
                  continuesAcrossPages: true,
                  reviewReasonCodes: [
                    'cross_page_passage_group',
                    'passage_missing_child_questions',
                    'cross_page_passage_group',
                  ],
                },
              ],
            });
            if (summary.passageReviewReasonLabel !== '페이지 이어짐, 문항 누락') {
              throw new Error(`unexpected passage reason label: ${summary.passageReviewReasonLabel}`);
            }
            if (summary.passageReviewLabel !== '지문 확인 1 · 페이지 이어짐 1') {
              throw new Error(`unexpected passage review label: ${summary.passageReviewLabel}`);
            }
            """
        )

    def test_normalize_publish_summary_exposes_passage_group_source_reuse(self) -> None:
        run_node(
            """
            const { normalizePublishSummary } = require('./ui_prototype/publish_summary.js');
            const summary = normalizePublishSummary({
              edbFileName: 'lesson.edb',
              edbFileUri: '/api/file?path=lesson',
              passageGroupSourceReuseGroups: [
                {
                  passageGroupId: 'hwp-text-passage-31-34',
                  sourcePageId: 'page-004',
                  problemIds: ['p31', 'p32'],
                  overlapAreaRatio: 0.92,
                },
              ],
              passageGroupSourceReuseGroupCount: 1,
            });
            if (summary.passageGroupSourceReuseGroupCount !== 1) {
              throw new Error(`source reuse count not normalized: ${summary.passageGroupSourceReuseGroupCount}`);
            }
            if (summary.passageGroupSourceReuseGroups[0].passageGroupId !== 'hwp-text-passage-31-34') {
              throw new Error('source reuse groups not preserved');
            }
            if (summary.passageGroupSourceReuseLabel !== '지문 겹침 1건') {
              throw new Error(`unexpected source reuse label: ${summary.passageGroupSourceReuseLabel}`);
            }
            if (summary.passageGroupSourceReuseDetailLabel !== 'hwp-text-passage-31-34 92%') {
              throw new Error(`unexpected source reuse detail label: ${summary.passageGroupSourceReuseDetailLabel}`);
            }
            """
        )

    def test_normalize_publish_summary_exposes_source_problem_overlap(self) -> None:
        run_node(
            """
            const { normalizePublishSummary } = require('./ui_prototype/publish_summary.js');
            const summary = normalizePublishSummary({
              edbFileName: 'lesson.edb',
              edbFileUri: '/api/file?path=lesson',
              sourceProblemOverlapGroups: [
                {
                  sourcePageId: 'page-004',
                  problemIds: ['p31', 'p32'],
                  overlapAreaRatio: 0.88,
                },
              ],
              sourceProblemOverlapGroupCount: 1,
            });
            if (summary.sourceProblemOverlapGroupCount !== 1) {
              throw new Error(`source overlap count not normalized: ${summary.sourceProblemOverlapGroupCount}`);
            }
            if (summary.sourceProblemOverlapGroups[0].sourcePageId !== 'page-004') {
              throw new Error('source overlap groups not preserved');
            }
            if (summary.sourceProblemOverlapLabel !== '문항 영역 겹침 1건') {
              throw new Error(`unexpected source overlap label: ${summary.sourceProblemOverlapLabel}`);
            }
            if (summary.sourceProblemOverlapDetailLabel !== 'page-004 88%') {
              throw new Error(`unexpected source overlap detail label: ${summary.sourceProblemOverlapDetailLabel}`);
            }
            """
        )

    def test_normalize_publish_summary_counts_passage_children_without_fragments(self) -> None:
        run_node(
            """
            const { normalizePublishSummary } = require('./ui_prototype/publish_summary.js');
            const summary = normalizePublishSummary({
              edbFileName: 'lesson.edb',
              edbFileUri: '/api/file?path=lesson',
              passageGroups: [
                {
                  id: 'hwp-continuation-passage-22-26',
                  problemCount: 6,
                  detectedProblemCount: 6,
                  fragmentProblemCount: 1,
                  problemNumbers: [22, 23, 24, 25, 26],
                  fragmentProblemIds: ['page-8-continuation'],
                  continuesAcrossPages: true,
                },
              ],
            });
            if (summary.passageProblemCount !== 5) {
              throw new Error(`passage child count should ignore fragments: ${summary.passageProblemCount}`);
            }
            if (summary.passageGroupLabel !== '지문 묶음 1 · 5문항 · 페이지 이어짐 1') {
              throw new Error(`unexpected passage label: ${summary.passageGroupLabel}`);
            }
            """
        )

    def test_normalize_publish_summary_exposes_classin_handoff_readiness(self) -> None:
        run_node(
            """
            const { normalizePublishSummary } = require('./ui_prototype/publish_summary.js');
            const summary = normalizePublishSummary({
              edbFileName: 'lesson.edb',
              edbFileUri: '/api/file?path=lesson',
              classinHandoffStatus: 'needs_attention_before_classin',
              readyForClassIn: false,
            });
            if (summary.classinHandoffStatus !== 'needs_attention_before_classin') {
              throw new Error(`handoff status not normalized: ${summary.classinHandoffStatus}`);
            }
            if (summary.readyForClassIn !== false) {
              throw new Error('readyForClassIn should remain false');
            }
            if (!summary.classinHandoffStatusLabel.includes('주의')) {
              throw new Error(`handoff status label missing warning copy: ${summary.classinHandoffStatusLabel}`);
            }
            """
        )

    def test_format_publish_history_meta_includes_time_and_record_label(self) -> None:
        run_node(
            """
            const { formatPublishHistoryMeta } = require('./ui_prototype/publish_summary.js');
            const label = formatPublishHistoryMeta({
              publishedAt: '2026-06-13T20:18:25+09:00',
              recordCountLabel: '45문항 + 자료 1',
            });
            if (!label.includes('45문항 + 자료 1')) {
              throw new Error(`missing record label: ${label}`);
            }
            if (!label.includes('20:18') && !label.includes('08:18') && !label.includes('오후') && !label.includes('PM')) {
              throw new Error(`missing time: ${label}`);
            }
            """
        )

    def test_format_publish_history_meta_falls_back_to_record_label(self) -> None:
        run_node(
            """
            const { formatPublishHistoryMeta } = require('./ui_prototype/publish_summary.js');
            const label = formatPublishHistoryMeta({
              recordCountActual: 8,
            });
            if (label !== '8개 자료') {
              throw new Error(`unexpected fallback label ${label}`);
            }
            """
        )


if __name__ == "__main__":
    unittest.main()
