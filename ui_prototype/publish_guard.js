(function(root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  root.EDB_PUBLISH_GUARD = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function() {
  const DEFAULT_SLOT_HEIGHT_PAGES = 1.2;
  const DEFAULT_HEIGHT_PAGES = 0.8;
  const DEFAULT_SCALE_RATIO = 1.0;
  const PLACEMENT_EPSILON_PAGES = 1e-9;
  const PLACEMENT_SCALE_MIN = 0.6;
  const PLACEMENT_SCALE_MAX = 1.6;
  const PLACEMENT_FIT_WIDTH_SCALE_MAX = 3.0;
  const AUTO_SCALE_MAX_REDUCTION_RATIO = 0.2;
  const MIN_HEIGHT_PAGES = 0.12;
  const OVERLAP_TOLERANCE_PAGES = 0.01;
  const SOURCE_BBOX_OVERLAP_RATIO = 0.65;
  const PASSAGE_GROUP_SOURCE_REUSE_RATIO = 0.65;

  function finiteNumber(value, fallback) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function firstNumber(value, keys, fallback) {
    for (const key of keys) {
      if (value && Object.prototype.hasOwnProperty.call(value, key)) {
        return finiteNumber(value[key], fallback);
      }
    }
    return fallback;
  }

  function clamp01(value) {
    return Math.max(0, Math.min(1, finiteNumber(value, 0)));
  }

  function snapUpPages(value, slotHeightPages = DEFAULT_SLOT_HEIGHT_PAGES) {
    const slot = finiteNumber(slotHeightPages, DEFAULT_SLOT_HEIGHT_PAGES);
    if (slot <= 0 || !Number.isFinite(value) || value <= 0) return 0;
    return Math.ceil((value - PLACEMENT_EPSILON_PAGES) / slot) * slot;
  }

  function problemIdFor(item, index) {
    const raw = item?.id ?? item?.problemId ?? item?.problem_id ?? `item-${index}`;
    return String(raw || "").trim();
  }

  function problemTitleFor(item, fallback) {
    return String(item?.name || item?.title || item?.problemNumber || item?.problem_number || fallback || "").trim();
  }

  function sourcePageIdFor(item) {
    return String(item?.sourcePageId || item?.source_page_id || item?.pageId || item?.page_id || "").trim();
  }

  function passageGroupIdFor(item) {
    return String(item?.passageGroupId || item?.passage_group_id || item?.metadata?.passageGroupId || item?.metadata?.passage_group_id || "").trim();
  }

  function passageRoleFor(item) {
    return String(item?.passageRole || item?.passage_role || item?.metadata?.passageRole || item?.metadata?.passage_role || "").trim();
  }

  function riskFlagsFor(item) {
    const raw = item?.riskFlags
      || item?.risk_flags
      || item?.metadata?.riskFlags
      || item?.metadata?.risk_flags
      || [];
    return new Set((Array.isArray(raw) ? raw : []).map(flag => String(flag || "").trim()).filter(Boolean));
  }

  function isSupplementalProblem(item, index) {
    if (passageRoleFor(item) === "passage_fragment") return true;
    if (item?.supplementalItem || item?.supplemental_item || item?.metadata?.supplementalItem || item?.metadata?.supplemental_item) {
      return true;
    }
    if (riskFlagsFor(item).has("marker_document_continuation") || item?.metadata?.marker_document_continuation) {
      return true;
    }
    return problemIdFor(item, index).endsWith("-continuation");
  }

  function numberOrNull(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function bboxFor(item) {
    const raw = item?.bbox || item?.sourceBbox || item?.source_bbox;
    if (!raw || typeof raw !== "object") return null;
    const left = numberOrNull(raw.left ?? raw.x);
    const top = numberOrNull(raw.top ?? raw.y);
    const width = numberOrNull(raw.width ?? raw.w);
    const height = numberOrNull(raw.height ?? raw.h);
    let right = numberOrNull(raw.right);
    let bottom = numberOrNull(raw.bottom);
    if (left === null || top === null) return null;
    if (width !== null) right = left + width;
    if (height !== null) bottom = top + height;
    if (right === null || bottom === null || right <= left || bottom <= top) return null;
    return { left, top, right, bottom, width: right - left, height: bottom - top };
  }

  function rounded(value) {
    return Number(value.toFixed(6));
  }

  function bboxPayload(bbox) {
    return {
      left: rounded(bbox.left),
      top: rounded(bbox.top),
      width: rounded(bbox.width),
      height: rounded(bbox.height),
    };
  }

  function normalizeIdFilter(value) {
    if (!value) return null;
    const raw = value instanceof Set ? Array.from(value) : Array.isArray(value) ? value : [];
    if (!raw.length) return null;
    return new Set(raw.map(item => String(item || "").trim()).filter(Boolean));
  }

  function usesContinuousPageFlow(item) {
    const inputIntent = String(item?.inputIntent ?? item?.input_intent ?? "")
      .trim()
      .toLowerCase()
      .replaceAll("_", "-");
    if (inputIntent) return inputIntent === "page-as-is";
    const placementMode = String(item?.placementMode ?? item?.placement_mode ?? "")
      .trim()
      .toLowerCase()
      .replaceAll("_", "-");
    return placementMode === "continuous" || placementMode === "continuous-page-as-is";
  }

  function itemAllowsOverflow(item) {
    // Mirrors the backend rule: overflow-allowed content (Korean passages,
    // reading-heavy sets) keeps its natural height instead of being
    // auto-scaled into a single slot.
    return Boolean(
      item?.overflowAllowed
      ?? item?.overflow_allowed
      ?? (item?.readingHeavy || item?.reading_heavy)
    );
  }

  function scaleNearPreviousBoundary(startYPages, heightPages, scaleRatio, slotHeightPages) {
    if (scaleRatio < DEFAULT_SCALE_RATIO || slotHeightPages <= 0) return scaleRatio;
    const renderedBottomYPages = startYPages + heightPages * scaleRatio;
    const previousBoundaryYPages = Math.floor(
      (renderedBottomYPages + PLACEMENT_EPSILON_PAGES) / slotHeightPages
    ) * slotHeightPages;
    if (renderedBottomYPages - previousBoundaryYPages <= PLACEMENT_EPSILON_PAGES) {
      return scaleRatio;
    }
    const targetScale = (previousBoundaryYPages - startYPages) / heightPages;
    const scaleReductionRatio = scaleRatio > 0
      ? (scaleRatio - targetScale) / scaleRatio
      : Infinity;
    return targetScale >= PLACEMENT_SCALE_MIN
      && targetScale < scaleRatio
      && scaleReductionRatio <= AUTO_SCALE_MAX_REDUCTION_RATIO + PLACEMENT_EPSILON_PAGES
      ? targetScale
      : scaleRatio;
  }

  function simulatedBoardPlacements(items, options = {}) {
    const slotHeightPages = finiteNumber(options.slotHeightPages, DEFAULT_SLOT_HEIGHT_PAGES);
    const problemIds = normalizeIdFilter(options.sessionProblemIds);
    const placements = [];
    let cursorPages = 0;

    (Array.isArray(items) ? items : []).forEach((item, index) => {
      if (!item || typeof item !== "object") return;
      const problemId = problemIdFor(item, index);
      if (!problemId || (problemIds && !problemIds.has(problemId))) return;

      const heightPages = Math.max(
        MIN_HEIGHT_PAGES,
        firstNumber(item, ["heightFrac", "actualHeightPages", "actual_height_pages"], DEFAULT_HEIGHT_PAGES)
      );
      const continuous = usesContinuousPageFlow(item);
      const startYPages = rounded(continuous
        ? Math.max(0, cursorPages)
        : snapUpPages(cursorPages, slotHeightPages));
      const persistedScale = numberOrNull(item.placementScaleRatio ?? item.placement_scale_ratio);
      const preserveLegacyScale = !continuous
        && persistedScale !== null
        && persistedScale > PLACEMENT_SCALE_MAX;
      // Only persisted placement fields opt into the legacy ceiling. Generic
      // scaleRatio input remains subject to the current 1.6 regular-item limit.
      const normalizedScale = Math.max(
        0,
        Math.min(
          continuous || preserveLegacyScale ? PLACEMENT_FIT_WIDTH_SCALE_MAX : PLACEMENT_SCALE_MAX,
          firstNumber(item, ["placementScaleRatio", "placement_scale_ratio", "scaleRatio"], DEFAULT_SCALE_RATIO)
        )
      );
      const requestedScale = continuous || preserveLegacyScale || itemAllowsOverflow(item)
        ? normalizedScale
        : scaleNearPreviousBoundary(startYPages, heightPages, normalizedScale, slotHeightPages);
      const renderedHeightPages = heightPages * requestedScale;
      const snappedNextStartYPages = rounded(continuous
        ? startYPages + renderedHeightPages
        : snapUpPages(startYPages + renderedHeightPages, slotHeightPages));
      const slotSpanPages = Math.max(renderedHeightPages, snappedNextStartYPages - startYPages);
      const verticalRoomPages = Math.max(0, slotSpanPages - renderedHeightPages);
      const yRatio = verticalRoomPages > 0.001
        ? clamp01(firstNumber(item, ["placementYRatio", "placement_y_ratio", "yRatio"], 0))
        : 0;
      const renderedTopYPages = rounded(startYPages + yRatio * verticalRoomPages);
      const renderedBottomYPages = rounded(renderedTopYPages + renderedHeightPages);

      placements.push({
        problemId,
        problemTitle: problemTitleFor(item, problemId),
        startYPages,
        renderedTopYPages,
        renderedBottomYPages,
        snappedNextStartYPages,
        heightPages,
        requestedScale,
      });
      cursorPages = snappedNextStartYPages;
    });

    return placements;
  }

  function findSourceProblemOverlaps(problems, options = {}) {
    const threshold = finiteNumber(options.overlapAreaRatio, SOURCE_BBOX_OVERLAP_RATIO);
    const groups = new Map();
    (Array.isArray(problems) ? problems : []).forEach((problem, index) => {
      if (!problem || typeof problem !== "object") return;
      if (riskFlagsFor(problem).has("hwp_text_fallback_problem") || isSupplementalProblem(problem, index)) return;
      const sourcePageId = sourcePageIdFor(problem);
      const bbox = bboxFor(problem);
      if (!sourcePageId || !bbox) return;
      const group = groups.get(sourcePageId) || [];
      group.push({ problem, bbox, index, passageGroupId: passageGroupIdFor(problem) });
      groups.set(sourcePageId, group);
    });

    const issues = [];
    groups.forEach((group, sourcePageId) => {
      group.sort((a, b) => (
        a.bbox.top - b.bbox.top
        || a.bbox.left - b.bbox.left
        || String(problemIdFor(a.problem, a.index)).localeCompare(String(problemIdFor(b.problem, b.index)))
      ));
      for (let index = 0; index < group.length; index += 1) {
        const current = group[index];
        const currentArea = current.bbox.width * current.bbox.height;
        if (currentArea <= 0) continue;
        for (let nextIndex = index + 1; nextIndex < group.length; nextIndex += 1) {
          const next = group[nextIndex];
          if (current.passageGroupId && current.passageGroupId === next.passageGroupId) continue;
          const nextArea = next.bbox.width * next.bbox.height;
          if (nextArea <= 0) continue;
          const intersectionWidth = Math.max(0, Math.min(current.bbox.right, next.bbox.right) - Math.max(current.bbox.left, next.bbox.left));
          const intersectionHeight = Math.max(0, Math.min(current.bbox.bottom, next.bbox.bottom) - Math.max(current.bbox.top, next.bbox.top));
          const intersectionArea = intersectionWidth * intersectionHeight;
          if (intersectionArea <= 0) continue;
          const overlapAreaRatio = intersectionArea / Math.min(currentArea, nextArea);
          if (overlapAreaRatio < threshold) continue;
          const unionArea = currentArea + nextArea - intersectionArea;
          const problemIds = [
            problemIdFor(current.problem, current.index),
            problemIdFor(next.problem, next.index),
          ];
          issues.push({
            type: "source_problem_bbox_overlap",
            severity: "warning",
            problemId: problemIds[0],
            problemTitle: problemTitleFor(current.problem, problemIds[0]),
            nextProblemId: problemIds[1],
            nextProblemTitle: problemTitleFor(next.problem, problemIds[1]),
            problemIds,
            problem_ids: problemIds,
            sourcePageId,
            overlapAreaRatio: rounded(overlapAreaRatio),
            intersectionOverUnion: unionArea > 0 ? rounded(intersectionArea / unionArea) : 0,
            intersectionAreaPx: rounded(intersectionArea),
            sourceBBoxOverlapThreshold: threshold,
            bbox: bboxPayload(current.bbox),
            nextBbox: bboxPayload(next.bbox),
          });
        }
      }
    });
    return issues;
  }

  function findPassageGroupSourceReuse(problems, options = {}) {
    const threshold = finiteNumber(options.overlapAreaRatio, PASSAGE_GROUP_SOURCE_REUSE_RATIO);
    const groups = new Map();
    (Array.isArray(problems) ? problems : []).forEach((problem, index) => {
      if (!problem || typeof problem !== "object") return;
      const passageGroupId = passageGroupIdFor(problem);
      const sourcePageId = sourcePageIdFor(problem);
      const bbox = bboxFor(problem);
      if (!passageGroupId || !sourcePageId || !bbox) return;
      if (riskFlagsFor(problem).has("hwp_text_fallback_problem") || isSupplementalProblem(problem, index)) return;
      const key = `${passageGroupId}\\n${sourcePageId}`;
      const group = groups.get(key) || [];
      group.push({ problem, bbox, index, passageGroupId, sourcePageId });
      groups.set(key, group);
    });

    const issues = [];
    groups.forEach(group => {
      if (group.length < 2) return;
      group.sort((a, b) => (
        a.bbox.top - b.bbox.top
        || a.bbox.left - b.bbox.left
        || String(problemIdFor(a.problem, a.index)).localeCompare(String(problemIdFor(b.problem, b.index)))
      ));
      for (let index = 0; index < group.length; index += 1) {
        const current = group[index];
        const currentArea = current.bbox.width * current.bbox.height;
        if (currentArea <= 0) continue;
        for (let nextIndex = index + 1; nextIndex < group.length; nextIndex += 1) {
          const next = group[nextIndex];
          const nextArea = next.bbox.width * next.bbox.height;
          if (nextArea <= 0) continue;
          const intersectionWidth = Math.max(0, Math.min(current.bbox.right, next.bbox.right) - Math.max(current.bbox.left, next.bbox.left));
          const intersectionHeight = Math.max(0, Math.min(current.bbox.bottom, next.bbox.bottom) - Math.max(current.bbox.top, next.bbox.top));
          const intersectionArea = intersectionWidth * intersectionHeight;
          if (intersectionArea <= 0) continue;
          const overlapAreaRatio = intersectionArea / Math.min(currentArea, nextArea);
          if (overlapAreaRatio < threshold) continue;
          const unionArea = currentArea + nextArea - intersectionArea;
          const problemIds = [
            problemIdFor(current.problem, current.index),
            problemIdFor(next.problem, next.index),
          ];
          issues.push({
            type: "passage_group_source_reuse",
            severity: "warning",
            problemId: problemIds[0],
            problemTitle: problemTitleFor(current.problem, problemIds[0]),
            nextProblemId: problemIds[1],
            nextProblemTitle: problemTitleFor(next.problem, problemIds[1]),
            problemIds,
            problem_ids: problemIds,
            passageGroupId: current.passageGroupId,
            sourcePageId: current.sourcePageId,
            overlapAreaRatio: rounded(overlapAreaRatio),
            intersectionOverUnion: unionArea > 0 ? rounded(intersectionArea / unionArea) : 0,
            intersectionAreaPx: rounded(intersectionArea),
            passageGroupSourceReuseThreshold: threshold,
            bbox: bboxPayload(current.bbox),
            nextBbox: bboxPayload(next.bbox),
          });
        }
      }
    });
    return issues;
  }

  function findBoardPlacementOverlaps(items, options = {}) {
    const tolerancePages = finiteNumber(options.tolerancePages, OVERLAP_TOLERANCE_PAGES);
    const placements = simulatedBoardPlacements(items, options);
    const issues = [];
    for (let index = 0; index < placements.length - 1; index += 1) {
      const current = placements[index];
      const next = placements[index + 1];
      const overlapPages = current.renderedBottomYPages - next.renderedTopYPages;
      if (overlapPages <= tolerancePages) continue;
      issues.push({
        type: "board_placement_overlap",
        severity: "warning",
        problemId: current.problemId,
        problemTitle: current.problemTitle,
        nextProblemId: next.problemId,
        nextProblemTitle: next.problemTitle,
        renderedBottomYPages: Number(current.renderedBottomYPages.toFixed(6)),
        nextTopYPages: Number(next.renderedTopYPages.toFixed(6)),
        overlapPages: Number(overlapPages.toFixed(6)),
      });
    }
    return issues;
  }

  return {
    findBoardPlacementOverlaps,
    findPassageGroupSourceReuse,
    findSourceProblemOverlaps,
    simulatedBoardPlacements,
  };
});
