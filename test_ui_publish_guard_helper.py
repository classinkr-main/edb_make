from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from layout_template_schema import LayoutTemplate, ProblemLayoutInput
from placement_engine import place_problems


PROJECT_ROOT = Path(__file__).resolve().parent


def run_node(script: str) -> None:
    subprocess.run(["node", "-e", script], cwd=PROJECT_ROOT, check=True)


class TestUiPublishGuardHelper(unittest.TestCase):
    def test_one_problem_flow_uses_same_absolute_12_grid_across_layers(self) -> None:
        heights = [0.8, 1.43, 0.8]
        # 1.43 now auto-fits into a single 1.2p slot (fit-to-slot shrink).
        expected_starts = [0.0, 1.2, 2.4]
        placements = place_problems(
            [
                ProblemLayoutInput(
                    problem_id=f"p{index + 1}",
                    actual_content_height_pages=height,
                )
                for index, height in enumerate(heights)
            ],
            template=LayoutTemplate(name="publish-guard-contract", base_slot_height_pages=1.2),
        )
        self.assertEqual(expected_starts, [placement.start_y_pages for placement in placements])

        run_node(
            r"""
            const fs = require('fs');
            const vm = require('vm');
            const { simulatedBoardPlacements } = require('./ui_prototype/publish_guard.js');
            const source = fs.readFileSync('./ui_prototype/app.jsx', 'utf8');
            const start = source.indexOf('const FIXED_LEFT_ZONE_RATIO =');
            const end = source.indexOf('const INITIAL_ITEMS =');
            if (start < 0 || end < 0) throw new Error('placement helper bounds not found');
            const sandbox = {};
            sandbox.globalThis = sandbox;
            sandbox.normalizeInputIntent = value => value;
            vm.runInNewContext(
              source.slice(start, end) + '\n'
                + 'globalThis.reflowItemsForBoardOrder = reflowItemsForBoardOrder;\n',
              sandbox
            );

            const items = [0.8, 1.43, 0.8].map((heightFrac, index) => ({
              id: `p${index + 1}`,
              heightFrac,
              placementScaleRatio: 1,
            }));
            const expectedStarts = [0, 1.2, 2.4];
            const startsFor = placements => placements.map(item => Number(item.startYPages.toFixed(6)));
            const uiStarts = startsFor(sandbox.reflowItemsForBoardOrder(items, 1.2, 1));
            const guardStarts = startsFor(simulatedBoardPlacements(items, { slotHeightPages: 1.2 }));
            if (JSON.stringify(uiStarts) !== JSON.stringify(expectedStarts)) {
              throw new Error(`UI placement contract mismatch: ${JSON.stringify(uiStarts)}`);
            }
            if (JSON.stringify(guardStarts) !== JSON.stringify(expectedStarts)) {
              throw new Error(`publish guard placement contract mismatch: ${JSON.stringify(guardStarts)}`);
            }
            """
        )

    def test_guard_matches_ui_for_near_boundary_auto_scale(self) -> None:
        run_node(
            r"""
            const fs = require('fs');
            const vm = require('vm');
            const { simulatedBoardPlacements } = require('./ui_prototype/publish_guard.js');
            const source = fs.readFileSync('./ui_prototype/app.jsx', 'utf8');
            const start = source.indexOf('const FIXED_LEFT_ZONE_RATIO =');
            const end = source.indexOf('const INITIAL_ITEMS =');
            if (start < 0 || end < 0) throw new Error('placement helper bounds not found');
            const sandbox = {};
            sandbox.globalThis = sandbox;
            sandbox.normalizeInputIntent = value => value;
            vm.runInNewContext(
              source.slice(start, end) + '\n'
                + 'globalThis.reflowItemsForBoardOrder = reflowItemsForBoardOrder;\n',
              sandbox
            );

            const startsFor = placements => placements.map(item => Number(item.startYPages.toFixed(6)));
            const closeItems = [
              { id: 'close', heightFrac: 3.62, placementScaleRatio: 1 },
              { id: 'after-close', heightFrac: 0.8, placementScaleRatio: 1 },
            ];
            const closeUi = sandbox.reflowItemsForBoardOrder(closeItems, 1.2, 1);
            const closeGuard = simulatedBoardPlacements(closeItems, { slotHeightPages: 1.2 });
            if (JSON.stringify(startsFor(closeUi)) !== JSON.stringify([0, 3.6])) {
              throw new Error(`UI did not auto-scale 3.62p to 3.6p: ${JSON.stringify(closeUi)}`);
            }
            if (JSON.stringify(startsFor(closeGuard)) !== JSON.stringify(startsFor(closeUi))) {
              throw new Error(`guard near-boundary flow mismatch: ${JSON.stringify(closeGuard)}`);
            }
            if (Number(closeGuard[0].renderedBottomYPages.toFixed(6)) !== 3.6) {
              throw new Error(`guard did not render 3.62p at the 3.6p boundary: ${JSON.stringify(closeGuard[0])}`);
            }

            const farItems = [
              { id: 'far', heightFrac: 3.2, placementScaleRatio: 1 },
              { id: 'after-far', heightFrac: 0.8, placementScaleRatio: 1 },
            ];
            const farUi = sandbox.reflowItemsForBoardOrder(farItems, 1.2, 1);
            const farGuard = simulatedBoardPlacements(farItems, { slotHeightPages: 1.2 });
            if (JSON.stringify(startsFor(farUi)) !== JSON.stringify([0, 3.6])) {
              throw new Error(`UI incorrectly shrank 3.2p to 2.4p: ${JSON.stringify(farUi)}`);
            }
            if (JSON.stringify(startsFor(farGuard)) !== JSON.stringify(startsFor(farUi))) {
              throw new Error(`guard far-boundary flow mismatch: ${JSON.stringify(farGuard)}`);
            }
            if (farGuard[0].requestedScale !== 1) {
              throw new Error(`guard exceeded the 20% auto-scale tolerance: ${JSON.stringify(farGuard[0])}`);
            }

            const cutoffCases = [
              { heightFrac: 6.21, expectedStarts: [0, 6] },
              { heightFrac: 1.55, expectedStarts: [0, 2.4] },
            ];
            for (const testCase of cutoffCases) {
              const items = [
                { id: `boundary-${testCase.heightFrac}`, heightFrac: testCase.heightFrac, placementScaleRatio: 1 },
                { id: `after-${testCase.heightFrac}`, heightFrac: 0.8, placementScaleRatio: 1 },
              ];
              const ui = sandbox.reflowItemsForBoardOrder(items, 1.2, 1);
              const guard = simulatedBoardPlacements(items, { slotHeightPages: 1.2 });
              if (JSON.stringify(startsFor(ui)) !== JSON.stringify(testCase.expectedStarts)) {
                throw new Error(`unexpected UI 6% boundary flow: ${JSON.stringify(ui)}`);
              }
              if (JSON.stringify(startsFor(guard)) !== JSON.stringify(startsFor(ui))) {
                throw new Error(`guard 6% boundary flow mismatch: ${JSON.stringify(guard)}`);
              }
            }

            const exactItems = [
              { id: 'exact-long-boundary', heightFrac: 24, placementScaleRatio: 1 },
              { id: 'after-exact', heightFrac: 0.8, placementScaleRatio: 1 },
            ];
            const exactUi = sandbox.reflowItemsForBoardOrder(exactItems, 1.2, 1);
            const exactGuard = simulatedBoardPlacements(exactItems, { slotHeightPages: 1.2 });
            if (JSON.stringify(startsFor(exactUi)) !== JSON.stringify([0, 24])) {
              throw new Error(`UI shrank an exact long boundary: ${JSON.stringify(exactUi)}`);
            }
            if (JSON.stringify(startsFor(exactGuard)) !== JSON.stringify(startsFor(exactUi))) {
              throw new Error(`guard exact-boundary flow mismatch: ${JSON.stringify(exactGuard)}`);
            }
            if (exactGuard[0].requestedScale !== 1) {
              throw new Error(`guard scaled an exact long boundary: ${JSON.stringify(exactGuard[0])}`);
            }

            const continuousItems = [
              { id: 'continuous', heightFrac: 3.62, placementScaleRatio: 1, inputIntent: 'page-as-is' },
              { id: 'continuous-after', heightFrac: 0.8, placementScaleRatio: 1, inputIntent: 'page-as-is' },
            ];
            const continuousUi = sandbox.reflowItemsForBoardOrder(continuousItems, 1.2, 1);
            const continuousGuard = simulatedBoardPlacements(continuousItems, { slotHeightPages: 1.2 });
            if (JSON.stringify(startsFor(continuousUi)) !== JSON.stringify([0, 3.62])) {
              throw new Error(`UI auto-scaled continuous flow: ${JSON.stringify(continuousUi)}`);
            }
            if (JSON.stringify(startsFor(continuousGuard)) !== JSON.stringify(startsFor(continuousUi))) {
              throw new Error(`guard continuous exclusion mismatch: ${JSON.stringify(continuousGuard)}`);
            }
            """
        )

    def test_noncontinuous_flow_ignores_stale_saved_span(self) -> None:
        run_node(
            r"""
            const { simulatedBoardPlacements } = require('./ui_prototype/publish_guard.js');
            const placements = simulatedBoardPlacements([
              {
                id: 'stale',
                heightFrac: 0.8,
                startYPages: 0,
                snappedNextStartYPages: 4.8,
                placementScaleRatio: 1,
              },
              { id: 'after', heightFrac: 0.8, placementScaleRatio: 1 },
            ]);
            const starts = placements.map(item => Number(item.startYPages.toFixed(6)));
            if (JSON.stringify(starts) !== JSON.stringify([0, 1.2])) {
              throw new Error(`stale noncontinuous span leaked into flow: ${JSON.stringify(placements)}`);
            }
            if (Number(placements[0].snappedNextStartYPages.toFixed(6)) !== 1.2) {
              throw new Error(`stale noncontinuous next start was preserved: ${JSON.stringify(placements[0])}`);
            }
            """
        )

    def test_guard_matches_ui_for_legacy_noncontinuous_high_scale(self) -> None:
        run_node(
            r"""
            const fs = require('fs');
            const vm = require('vm');
            const { simulatedBoardPlacements } = require('./ui_prototype/publish_guard.js');
            const source = fs.readFileSync('./ui_prototype/app.jsx', 'utf8');
            const start = source.indexOf('const FIXED_LEFT_ZONE_RATIO =');
            const end = source.indexOf('const INITIAL_ITEMS =');
            if (start < 0 || end < 0) throw new Error('placement helper bounds not found');
            const sandbox = {};
            sandbox.globalThis = sandbox;
            sandbox.normalizeInputIntent = value => value;
            vm.runInNewContext(
              source.slice(start, end) + '\n'
                + 'globalThis.reflowItemsForBoardOrder = reflowItemsForBoardOrder;\n',
              sandbox
            );

            const items = [
              { id: 'legacy', heightFrac: 1.1, placementScaleRatio: 2.4 },
              { id: 'next', heightFrac: 0.8, placementScaleRatio: 1 },
            ];
            const uiPlacements = sandbox.reflowItemsForBoardOrder(items, 1.2, 1);
            const guardPlacements = simulatedBoardPlacements(items, { slotHeightPages: 1.2 });
            const uiStarts = uiPlacements.map(item => Number(item.startYPages.toFixed(6)));
            const guardStarts = guardPlacements.map(item => Number(item.startYPages.toFixed(6)));
            if (JSON.stringify(uiStarts) !== JSON.stringify([0, 3.6])) {
              throw new Error(`unexpected UI legacy flow: ${JSON.stringify(uiPlacements)}`);
            }
            if (JSON.stringify(guardStarts) !== JSON.stringify(uiStarts)) {
              throw new Error(`guard legacy flow mismatch: ${JSON.stringify(guardPlacements)}`);
            }
            if (guardPlacements[0].requestedScale !== 2.4 || guardPlacements[0].renderedBottomYPages !== 2.64) {
              throw new Error(`guard clamped persisted legacy scale: ${JSON.stringify(guardPlacements[0])}`);
            }

            const nearBoundaryLegacyItems = [
              { id: 'legacy-near-boundary', heightFrac: 1.1, placementScaleRatio: 2.2 },
              { id: 'after-legacy-near-boundary', heightFrac: 0.8, placementScaleRatio: 1 },
            ];
            const nearBoundaryLegacyUi = sandbox.reflowItemsForBoardOrder(nearBoundaryLegacyItems, 1.2, 1);
            const nearBoundaryLegacyGuard = simulatedBoardPlacements(nearBoundaryLegacyItems, { slotHeightPages: 1.2 });
            if (JSON.stringify(nearBoundaryLegacyUi.map(item => item.startYPages)) !== JSON.stringify([0, 3.6])) {
              throw new Error(`UI changed a legacy scale near a boundary: ${JSON.stringify(nearBoundaryLegacyUi)}`);
            }
            if (
              JSON.stringify(nearBoundaryLegacyGuard.map(item => item.startYPages))
              !== JSON.stringify(nearBoundaryLegacyUi.map(item => item.startYPages))
            ) {
              throw new Error(`guard legacy near-boundary flow mismatch: ${JSON.stringify(nearBoundaryLegacyGuard)}`);
            }
            if (
              nearBoundaryLegacyGuard[0].requestedScale !== 2.2
              || nearBoundaryLegacyGuard[0].renderedBottomYPages !== 2.42
            ) {
              throw new Error(`guard auto-scaled a legacy item: ${JSON.stringify(nearBoundaryLegacyGuard[0])}`);
            }

            const ordinary = simulatedBoardPlacements([
              { id: 'ordinary', heightFrac: 1.1, scaleRatio: 2.4 },
            ])[0];
            if (ordinary.requestedScale !== 1.6) {
              throw new Error(`ordinary non-persisted scale bypassed 1.6 limit: ${JSON.stringify(ordinary)}`);
            }
            const extremeLegacy = simulatedBoardPlacements([
              { id: 'extreme-legacy', heightFrac: 1.1, placement_scale_ratio: 9 },
            ])[0];
            if (extremeLegacy.requestedScale !== 3) {
              throw new Error(`legacy compatibility bypassed 3x safety cap: ${JSON.stringify(extremeLegacy)}`);
            }
            """
        )

    def test_guard_matches_ui_for_continuous_current_rendered_flow(self) -> None:
        run_node(
            r"""
            const fs = require('fs');
            const vm = require('vm');
            const { simulatedBoardPlacements } = require('./ui_prototype/publish_guard.js');
            const source = fs.readFileSync('./ui_prototype/app.jsx', 'utf8');
            const start = source.indexOf('const FIXED_LEFT_ZONE_RATIO =');
            const end = source.indexOf('const INITIAL_ITEMS =');
            if (start < 0 || end < 0) throw new Error('placement helper bounds not found');
            const sandbox = {};
            sandbox.globalThis = sandbox;
            sandbox.normalizeInputIntent = value => value;
            vm.runInNewContext(
              source.slice(start, end) + '\n'
                + 'globalThis.reflowItemsForBoardOrder = reflowItemsForBoardOrder;\n',
              sandbox
            );

            const startsFor = placements => placements.map(item => Number(item.startYPages.toFixed(6)));
            const nextStartsFor = placements => placements.map(item => Number(item.snappedNextStartYPages.toFixed(6)));
            const staleItems = [
              {
                id: 'continuous',
                heightFrac: 0.8,
                startYPages: 0,
                snappedNextStartYPages: 2.4,
                placementScaleRatio: 1,
                inputIntent: 'page-as-is',
              },
              {
                id: 'continuous-after',
                heightFrac: 0.5,
                placementScaleRatio: 1,
                placementMode: 'continuous-page-as-is',
              },
            ];
            const staleUi = sandbox.reflowItemsForBoardOrder(staleItems, 1.2, 1);
            const staleGuard = simulatedBoardPlacements(staleItems, { slotHeightPages: 1.2 });
            if (JSON.stringify(startsFor(staleUi)) !== JSON.stringify([0, 0.8])) {
              throw new Error(`unexpected UI continuous flow: ${JSON.stringify(staleUi)}`);
            }
            if (JSON.stringify(startsFor(staleGuard)) !== JSON.stringify(startsFor(staleUi))) {
              throw new Error(`guard retained stale continuous span: ${JSON.stringify(staleGuard)}`);
            }
            if (JSON.stringify(nextStartsFor(staleUi)) !== JSON.stringify([0.8, 1.3])) {
              throw new Error(`unexpected UI continuous next starts: ${JSON.stringify(staleUi)}`);
            }
            if (JSON.stringify(nextStartsFor(staleGuard)) !== JSON.stringify(nextStartsFor(staleUi))) {
              throw new Error(`guard continuous next starts mismatch: ${JSON.stringify(staleGuard)}`);
            }

            const fitWidthItems = [
              {
                id: 'fit-width',
                heightFrac: 0.8,
                placementScaleRatio: 3,
                inputIntent: 'page-as-is',
                startYPages: 0,
                snappedNextStartYPages: 4.8,
              },
              { id: 'after-fit-width', heightFrac: 0.5, placementScaleRatio: 1, inputIntent: 'page-as-is' },
            ];
            const fitWidthUi = sandbox.reflowItemsForBoardOrder(fitWidthItems, 1.2, 1);
            const fitWidthGuard = simulatedBoardPlacements(fitWidthItems, { slotHeightPages: 1.2 });
            if (JSON.stringify(startsFor(fitWidthUi)) !== JSON.stringify([0, 2.4])) {
              throw new Error(`unexpected UI fit-width flow: ${JSON.stringify(fitWidthUi)}`);
            }
            if (JSON.stringify(startsFor(fitWidthGuard)) !== JSON.stringify(startsFor(fitWidthUi))) {
              throw new Error(`guard fit-width flow mismatch: ${JSON.stringify(fitWidthGuard)}`);
            }
            if (Number(fitWidthGuard[0].snappedNextStartYPages.toFixed(6)) !== 2.4) {
              throw new Error(`fit-width rendered height was not preserved: ${JSON.stringify(fitWidthGuard[0])}`);
            }
            """
        )

    def test_detects_source_problem_bbox_overlap_before_publish(self) -> None:
        run_node(
            """
            const { findSourceProblemOverlaps } = require('./ui_prototype/publish_guard.js');
            const overlaps = findSourceProblemOverlaps([
              {
                id: 'p21',
                title: '21.',
                sourcePageId: 'page-001',
                bbox: { left: 40, top: 100, width: 520, height: 320 },
              },
              {
                id: 'p22',
                title: '22.',
                sourcePageId: 'page-001',
                bbox: { left: 60, top: 125, width: 500, height: 300 },
              },
            ]);
            if (overlaps.length !== 1) {
              throw new Error(`expected 1 source overlap, got ${overlaps.length}`);
            }
            const issue = overlaps[0];
            if (issue.type !== 'source_problem_bbox_overlap') {
              throw new Error(`unexpected issue type ${issue.type}`);
            }
            if (issue.problemId !== 'p21' || issue.nextProblemId !== 'p22') {
              throw new Error(`unexpected ids ${issue.problemId}/${issue.nextProblemId}`);
            }
            if (JSON.stringify(issue.problemIds) !== JSON.stringify(['p21', 'p22'])) {
              throw new Error(`missing focus problem ids ${JSON.stringify(issue.problemIds)}`);
            }
            if (issue.sourcePageId !== 'page-001') {
              throw new Error(`unexpected page ${issue.sourcePageId}`);
            }
            if (!(issue.overlapAreaRatio >= 0.8)) {
              throw new Error(`expected high overlap ratio, got ${issue.overlapAreaRatio}`);
            }
            """
        )

    def test_ignores_source_bbox_overlap_across_different_pages(self) -> None:
        run_node(
            """
            const { findSourceProblemOverlaps } = require('./ui_prototype/publish_guard.js');
            const overlaps = findSourceProblemOverlaps([
              {
                id: 'p21',
                title: '21.',
                sourcePageId: 'page-001',
                bbox: { left: 40, top: 100, width: 520, height: 320 },
              },
              {
                id: 'p22',
                title: '22.',
                sourcePageId: 'page-002',
                bbox: { left: 60, top: 125, width: 500, height: 300 },
              },
            ]);
            if (overlaps.length !== 0) {
              throw new Error(`expected no cross-page overlap, got ${JSON.stringify(overlaps)}`);
            }
            """
        )

    def test_ignores_shared_passage_enclosing_bbox_overlap(self) -> None:
        run_node(
            """
            const { findSourceProblemOverlaps } = require('./ui_prototype/publish_guard.js');
            const overlaps = findSourceProblemOverlaps([
              {
                id: 'page-008-passage-43-45',
                title: '지문 43~45',
                sourcePageId: 'page-008',
                bbox: { left: 0, top: 137.24, width: 1914, height: 1477.57 },
                passageGroupId: 'page-008-passage-43-45',
                passageRole: 'passage_fragment',
              },
              {
                id: 'page-008-passage-41-42',
                title: '지문 41~42',
                sourcePageId: 'page-008',
                bbox: { left: 0, top: 141.93, width: 891, height: 1424.16 },
                passageGroupId: 'page-008-passage-41-42',
                passageRole: 'passage_fragment',
              },
            ]);
            if (overlaps.length !== 0) {
              throw new Error(`expected no supplemental passage overlap, got ${JSON.stringify(overlaps)}`);
            }
            """
        )

    def test_routes_same_passage_child_overlap_to_dedicated_guard(self) -> None:
        run_node(
            """
            const {
              findPassageGroupSourceReuse,
              findSourceProblemOverlaps,
            } = require('./ui_prototype/publish_guard.js');
            const problems = [
              {
                id: 'p43',
                sourcePageId: 'page-008',
                bbox: { left: 42, top: 120, width: 520, height: 430 },
                passageGroupId: 'page-008-passage-43-45',
                passageRole: 'child_question',
              },
              {
                id: 'p44',
                sourcePageId: 'page-008',
                bbox: { left: 48, top: 132, width: 510, height: 410 },
                passageGroupId: 'page-008-passage-43-45',
                passageRole: 'child_question',
              },
            ];
            const genericIssues = findSourceProblemOverlaps(problems);
            const passageIssues = findPassageGroupSourceReuse(problems);
            if (genericIssues.length !== 0 || passageIssues.length !== 1) {
              throw new Error(`unexpected generic/passage issues ${genericIssues.length}/${passageIssues.length}`);
            }
            """
        )

    def test_detects_passage_group_source_reuse_before_publish(self) -> None:
        run_node(
            """
            const { findPassageGroupSourceReuse } = require('./ui_prototype/publish_guard.js');
            const issues = findPassageGroupSourceReuse([
              {
                id: 'p22',
                title: '22.',
                sourcePageId: 'page-004',
                bbox: { left: 42, top: 120, width: 520, height: 430 },
                passageGroupId: 'hwp-continuation-passage-22-26',
                passageRole: 'child_question',
              },
              {
                id: 'p23',
                title: '23.',
                sourcePageId: 'page-004',
                bbox: { left: 48, top: 132, width: 510, height: 410 },
                passageGroupId: 'hwp-continuation-passage-22-26',
                passageRole: 'child_question',
              },
            ]);
            if (issues.length !== 1) {
              throw new Error(`expected 1 passage reuse issue, got ${issues.length}`);
            }
            const issue = issues[0];
            if (issue.type !== 'passage_group_source_reuse') {
              throw new Error(`unexpected issue type ${issue.type}`);
            }
            if (issue.passageGroupId !== 'hwp-continuation-passage-22-26') {
              throw new Error(`unexpected passage group ${issue.passageGroupId}`);
            }
            if (issue.problemId !== 'p22' || issue.nextProblemId !== 'p23') {
              throw new Error(`unexpected ids ${issue.problemId}/${issue.nextProblemId}`);
            }
            if (JSON.stringify(issue.problemIds) !== JSON.stringify(['p22', 'p23'])) {
              throw new Error(`missing focus problem ids ${JSON.stringify(issue.problemIds)}`);
            }
            if (!(issue.overlapAreaRatio >= 0.8)) {
              throw new Error(`expected high overlap ratio, got ${issue.overlapAreaRatio}`);
            }
            """
        )

    def test_reserves_requested_scale_height_before_publish(self) -> None:
        run_node(
            """
            const { findBoardPlacementOverlaps, simulatedBoardPlacements } = require('./ui_prototype/publish_guard.js');
            const overlaps = findBoardPlacementOverlaps([
              { id: 'p13', name: '13. 긴 지문', heightFrac: 1.1, placementScaleRatio: 1.4 },
              { id: 'p14', name: '14. 하위 문항', heightFrac: 0.8, placementScaleRatio: 1.0 },
            ]);
            if (overlaps.length !== 0) {
              throw new Error(`expected auto-reserved scale height, got ${JSON.stringify(overlaps)}`);
            }
            const placements = simulatedBoardPlacements([
              { id: 'p13', name: '13. 긴 지문', heightFrac: 1.1, placementScaleRatio: 1.4 },
              { id: 'p14', name: '14. 하위 문항', heightFrac: 0.8, placementScaleRatio: 1.0 },
            ]);
            if (placements[0].snappedNextStartYPages !== 2.4) {
              throw new Error(`expected first item to reserve 2.4 pages, got ${placements[0].snappedNextStartYPages}`);
            }
            if (placements[1].startYPages !== 2.4) {
              throw new Error(`expected next item to start at 2.4 pages, got ${placements[1].startYPages}`);
            }
            """
        )

    def test_allows_safe_adjacent_placements(self) -> None:
        run_node(
            """
            const { findBoardPlacementOverlaps } = require('./ui_prototype/publish_guard.js');
            const overlaps = findBoardPlacementOverlaps([
              { id: 'p13', name: '13. 긴 지문', heightFrac: 1.1, placementScaleRatio: 1.0 },
              { id: 'p14', name: '14. 하위 문항', heightFrac: 0.8, placementScaleRatio: 1.0 },
            ]);
            if (overlaps.length !== 0) {
              throw new Error(`expected no overlap, got ${JSON.stringify(overlaps)}`);
            }
            """
        )


if __name__ == "__main__":
    unittest.main()
