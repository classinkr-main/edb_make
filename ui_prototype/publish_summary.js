(function attachPublishSummaryHelpers(root, factory) {
  const helpers = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = helpers;
  }
  root.EDB_PUBLISH_SUMMARY = helpers;
})(typeof globalThis !== "undefined" ? globalThis : window, function createPublishSummaryHelpers() {
  function positiveNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? Math.max(0, number) : 0;
  }

  const CLASSIN_PREFLIGHT_ISSUE_LABELS = {
    board_placement_overlap: "판서 배치 겹침",
    duplicate_problem_id: "문항 ID 중복",
    duplicate_problem_number: "중복 번호",
    low_ink_problem_image: "이미지 내용 부족",
    missing_problem_id: "문항 ID 누락",
    missing_problem_image: "문항 이미지 없음",
    passage_missing_child_questions: "문항 누락",
    passage_group_source_reuse: "지문 겹침",
    passage_review_queue_remaining: "지문 확인 필요",
    review_flags_remaining: "검수 플래그 남음",
    small_problem_image: "문항 이미지 작음",
    source_problem_bbox_overlap: "문항 영역 겹침",
    unreadable_problem_image: "문항 이미지 흐림",
  };
  const PASSAGE_REVIEW_REASON_LABELS = {
    cross_page_passage_group: "페이지 이어짐",
    hwp_text_fallback_problem: "HWP 텍스트 fallback",
    marker_document_continuation: "문서 이어짐 표시",
    passage_cross_page_merge_check: "병합 확인",
    passage_fragment: "지문 본문",
    passage_group_source_reuse: "지문 겹침",
    passage_missing_child_questions: "문항 누락",
    source_problem_bbox_overlap: "문항 영역 겹침",
  };

  function classinPreflightIssueLabel(type) {
    const normalized = String(type || "").trim();
    return CLASSIN_PREFLIGHT_ISSUE_LABELS[normalized] || normalized || "기타 주의";
  }

  function classinPreflightIssueLabels(preflight) {
    const issues = Array.isArray(preflight?.issues) ? preflight.issues : [];
    const counts = new Map();
    issues.forEach(issue => {
      const type = String(issue?.type || issue?.issueType || issue?.issue_type || "").trim();
      const key = type || "unknown";
      counts.set(key, (counts.get(key) || 0) + 1);
    });
    return Array.from(counts.entries()).map(([type, count]) => `${classinPreflightIssueLabel(type)} ${count}`);
  }

  function classinPreflightIssueTypes(preflight) {
    const issues = Array.isArray(preflight?.issues) ? preflight.issues : [];
    const types = issues.map(issue => String(issue?.type || issue?.issueType || issue?.issue_type || "").trim())
      .filter(Boolean);
    return Array.from(new Set(types));
  }

  function normalizeBlockingProblemIds(raw) {
    const ids = Array.isArray(raw?.blockingProblemIds)
      ? raw.blockingProblemIds
      : Array.isArray(raw?.blocking_problem_ids)
        ? raw.blocking_problem_ids
        : [];
    return Array.from(new Set(ids.map(id => String(id || "").trim()).filter(Boolean)));
  }

  function normalizePublishPreflightBlock(raw) {
    if (!raw || typeof raw !== "object") return null;
    const errorKind = String(raw.errorKind || raw.error_kind || "").trim();
    const classinPreflight = raw.classinPreflight || raw.classin_preflight || {};
    const preflightStatus = String(
      raw.classinPreflightStatus
      || raw.classin_preflight_status
      || classinPreflight.status
      || ""
    ).trim();
    if (errorKind !== "publish_preflight_blocked" && preflightStatus !== "blocked") {
      return null;
    }
    const issueLabels = classinPreflightIssueLabels(classinPreflight);
    const issueSummaryLabel = String(
      raw.classinPreflightIssueSummaryLabel
      || raw.classin_preflight_issue_summary_label
      || issueLabels.join(" · ")
    ).trim();
    const issueCount = positiveNumber(
      raw.classinPreflightIssueCount
      ?? raw.classin_preflight_issue_count
      ?? classinPreflight.issueCount
      ?? classinPreflight.issue_count
    );
    const blockingIssueTypes = Array.isArray(raw.blockingIssueTypes)
      ? raw.blockingIssueTypes
      : Array.isArray(raw.blocking_issue_types)
        ? raw.blocking_issue_types
        : [];
    const issueTypes = Array.from(new Set([
      ...classinPreflightIssueTypes(classinPreflight),
      ...blockingIssueTypes.map(type => String(type || "").trim()).filter(Boolean),
    ]));
    const message = String(
      raw.error
      || "ClassIn 사전점검에서 제작 전 확인 문제가 발견되어 EDB 제작을 중단했습니다."
    ).trim();
    const toastLabel = [message, issueSummaryLabel].filter(Boolean).join(" ");
    const blockingProblemIds = normalizeBlockingProblemIds(raw);
    return {
      blocked: true,
      errorKind: errorKind || "publish_preflight_blocked",
      message,
      classinPreflight,
      issueCount,
      issueTypes,
      blockingProblemIds,
      blocking_problem_ids: blockingProblemIds,
      issueLabels,
      issueSummaryLabel,
      toastLabel,
    };
  }

  function formatRecordCountLabel(summary) {
    const explicit = String(summary?.recordCountLabel || summary?.record_count_label || "").trim();
    if (explicit) return explicit;
    const supplemental = positiveNumber(summary?.supplementalItemCount ?? summary?.supplemental_item_count);
    const coreRaw = Number(summary?.coreProblemCount ?? summary?.core_problem_count);
    if (supplemental > 0 && Number.isFinite(coreRaw)) {
      return `${Math.max(0, coreRaw)}문항 + 자료 ${supplemental}`;
    }
    const recordCount = positiveNumber(
      summary?.recordCount
      ?? summary?.record_count
      ?? summary?.recordCountActual
      ?? summary?.record_count_actual
    );
    return `${recordCount}개 자료`;
  }

  function normalizePassageGroups(raw, session = null) {
    const groups = raw?.passageGroups || raw?.passage_groups || session?.passageGroups || session?.passage_groups || [];
    return Array.isArray(groups)
      ? groups.filter(group => group && typeof group === "object")
      : [];
  }

  function passageProblemCount(groups) {
    return groups.reduce((total, group) => {
      const problemNumbers = group.problemNumbers || group.problem_numbers || group.childProblemNumbers || group.child_problem_numbers;
      if (Array.isArray(problemNumbers) && problemNumbers.length > 0) {
        return total + new Set(problemNumbers.map(value => String(value).trim()).filter(Boolean)).size;
      }
      const rawCount = positiveNumber(group.problemCount ?? group.problem_count);
      const fragmentCount = positiveNumber(group.fragmentProblemCount ?? group.fragment_problem_count);
      return total + Math.max(0, rawCount - fragmentCount);
    }, 0);
  }

  function crossPagePassageGroupCount(groups) {
    return groups.filter(group => Boolean(group.continuesAcrossPages || group.continues_across_pages)).length;
  }

  function normalizePassageReviewItems(raw, session = null) {
    const items = raw?.passageReviewItems
      || raw?.passage_review_items
      || session?.passageReviewItems
      || session?.passage_review_items
      || [];
    return Array.isArray(items)
      ? items.filter(item => item && typeof item === "object")
      : [];
  }

  function countCrossPagePassageReviewItems(items) {
    return items.filter(item => Boolean(item.continuesAcrossPages || item.continues_across_pages)).length;
  }

  function passageReviewReasonLabel(reason) {
    const normalized = String(reason || "").trim();
    return PASSAGE_REVIEW_REASON_LABELS[normalized] || normalized;
  }

  function passageReviewReasonCodes(items) {
    const seen = new Set();
    const codes = [];
    items.forEach(item => {
      const rawCodes = Array.isArray(item?.reviewReasonCodes)
        ? item.reviewReasonCodes
        : Array.isArray(item?.review_reason_codes)
          ? item.review_reason_codes
          : [];
      rawCodes.forEach(code => {
        const normalized = String(code || "").trim();
        if (!normalized || seen.has(normalized)) return;
        seen.add(normalized);
        codes.push(normalized);
      });
    });
    return codes;
  }

  function formatPassageReviewReasonLabel(items) {
    return passageReviewReasonCodes(items)
      .map(passageReviewReasonLabel)
      .filter(Boolean)
      .join(", ");
  }

  function formatPassageReviewLabel({ itemCount, crossPageCount }) {
    if (itemCount <= 0) return "";
    const parts = [`지문 확인 ${itemCount}`];
    if (crossPageCount > 0) parts.push(`페이지 이어짐 ${crossPageCount}`);
    return parts.join(" · ");
  }

  function normalizeSourceProblemOverlapGroups(raw, session = null) {
    const groups = raw?.sourceProblemOverlapGroups
      || raw?.source_problem_overlap_groups
      || session?.sourceProblemOverlapGroups
      || session?.source_problem_overlap_groups
      || [];
    return Array.isArray(groups)
      ? groups.filter(group => group && typeof group === "object")
      : [];
  }

  function formatSourceProblemOverlapLabel({ groupCount, groups }) {
    if (groupCount <= 0) return "";
    return `문항 영역 겹침 ${groupCount}건`;
  }

  function formatSourceProblemOverlapDetailLabel(groups) {
    return groups.map(group => {
      const pageId = String(group?.sourcePageId || group?.source_page_id || "").trim();
      const ratio = Number(group?.overlapAreaRatio ?? group?.overlap_area_ratio ?? 0);
      const percent = Number.isFinite(ratio) && ratio > 0 ? `${Math.round(ratio * 100)}%` : "";
      return [pageId, percent].filter(Boolean).join(" ");
    }).filter(Boolean).join(", ");
  }

  function normalizePassageGroupSourceReuseGroups(raw, session = null) {
    const groups = raw?.passageGroupSourceReuseGroups
      || raw?.passage_group_source_reuse_groups
      || session?.passageGroupSourceReuseGroups
      || session?.passage_group_source_reuse_groups
      || [];
    return Array.isArray(groups)
      ? groups.filter(group => group && typeof group === "object")
      : [];
  }

  function formatPassageGroupSourceReuseLabel({ groupCount, groups }) {
    if (groupCount <= 0) return "";
    return `지문 겹침 ${groupCount}건`;
  }

  function formatPassageGroupSourceReuseDetailLabel(groups) {
    return groups.map(group => {
      const groupId = String(group?.passageGroupId || group?.passage_group_id || "").trim();
      const ratio = Number(group?.overlapAreaRatio ?? group?.overlap_area_ratio ?? 0);
      const percent = Number.isFinite(ratio) && ratio > 0 ? `${Math.round(ratio * 100)}%` : "";
      return [groupId, percent].filter(Boolean).join(" ");
    }).filter(Boolean).join(", ");
  }

  function formatPassageGroupLabel({ groupCount, problemCount, crossPageCount }) {
    if (groupCount <= 0) return "";
    const parts = [`지문 묶음 ${groupCount}`, `${problemCount}문항`];
    if (crossPageCount > 0) parts.push(`페이지 이어짐 ${crossPageCount}`);
    return parts.join(" · ");
  }

  function normalizeLayoutDiagnostics(raw, session = null) {
    const diag = raw?.layoutDiagnostics
      || raw?.layout_diagnostics
      || session?.layoutDiagnostics
      || session?.layout_diagnostics
      || {};
    return diag && typeof diag === "object" ? diag : {};
  }

  function formatLayoutDiagnosticsLabel(diag) {
    if (!diag || typeof diag !== "object") return "";
    const explicit = String(diag.label || "").trim();
    if (explicit) return explicit;
    const autoExtendedCount = positiveNumber(diag.autoExtendedCount ?? diag.auto_extended_count);
    const overlapRiskCount = positiveNumber(diag.overlapRiskCount ?? diag.overlap_risk_count);
    const maxRendered = positiveNumber(diag.maxRenderedHeightPages ?? diag.max_rendered_height_pages);
    const parts = [];
    if (autoExtendedCount > 0) parts.push(`긴 이미지 자동 확장 ${autoExtendedCount}`);
    if ((autoExtendedCount > 0 || overlapRiskCount > 0) && maxRendered > 0) parts.push(`최대 ${maxRendered.toFixed(2)}p`);
    if (overlapRiskCount > 0) parts.push(`겹침 위험 ${overlapRiskCount}`);
    return parts.join(" · ");
  }

  function normalizeEdbParts(raw, fallback = {}) {
    const rawParts = Array.isArray(raw?.edbParts)
      ? raw.edbParts
      : Array.isArray(raw?.edb_parts)
        ? raw.edb_parts
        : [];
    const parts = rawParts
      .filter(part => part && typeof part === "object")
      .map((part, index) => {
        const edbPath = String(part.edbPath || part.edb_path || "").trim();
        const edbFileUri = String(part.edbFileUri || part.edb_file_uri || "").trim();
        const edbFileName = String(
          part.edbFileName
          || part.edb_file_name
          || (edbPath ? edbPath.split(/[\\/]/).pop() : "")
          || `classin_part${String(index + 1).padStart(2, "0")}.edb`
        ).trim();
        const exists = part.edbFileExists ?? part.edb_file_exists;
        return {
          ...part,
          partIndex: positiveNumber(part.partIndex ?? part.part_index ?? index + 1) || index + 1,
          part_index: positiveNumber(part.partIndex ?? part.part_index ?? index + 1) || index + 1,
          partCount: positiveNumber(part.partCount ?? part.part_count ?? rawParts.length) || rawParts.length,
          part_count: positiveNumber(part.partCount ?? part.part_count ?? rawParts.length) || rawParts.length,
          edbFileName,
          edb_file_name: edbFileName,
          edbPath,
          edb_path: edbPath,
          edbFileUri,
          edb_file_uri: edbFileUri,
          edbFileExists: exists === undefined ? true : exists !== false,
          edb_file_exists: exists === undefined ? true : exists !== false,
          recordCount: positiveNumber(part.recordCount ?? part.record_count),
          record_count: positiveNumber(part.recordCount ?? part.record_count),
          placementCount: positiveNumber(part.placementCount ?? part.placement_count),
          placement_count: positiveNumber(part.placementCount ?? part.placement_count),
          flowEndPages: positiveNumber(part.flowEndPages ?? part.flow_end_pages),
          flow_end_pages: positiveNumber(part.flowEndPages ?? part.flow_end_pages),
          pageCountHint: positiveNumber(part.pageCountHint ?? part.page_count_hint),
          page_count_hint: positiveNumber(part.pageCountHint ?? part.page_count_hint),
        };
      });
    if (!parts.length && (fallback.edbFileName || fallback.edbPath || fallback.edbFileUri)) {
      const fallbackName = fallback.edbFileName || (fallback.edbPath ? fallback.edbPath.split(/[\\/]/).pop() : "classin.edb");
      const fallbackExists = fallback.edbFileExists ?? fallback.edb_file_exists;
      parts.push({
        partIndex: 1,
        part_index: 1,
        partCount: 1,
        part_count: 1,
        edbFileName: fallbackName,
        edb_file_name: fallbackName,
        edbPath: fallback.edbPath || "",
        edb_path: fallback.edbPath || "",
        edbFileUri: fallback.edbFileUri || "",
        edb_file_uri: fallback.edbFileUri || "",
        edbFileExists: fallbackExists === undefined ? true : fallbackExists !== false,
        edb_file_exists: fallbackExists === undefined ? true : fallbackExists !== false,
        recordCount: positiveNumber(fallback.recordCount),
        record_count: positiveNumber(fallback.recordCount),
        placementCount: positiveNumber(fallback.placementCount),
        placement_count: positiveNumber(fallback.placementCount),
        flowEndPages: positiveNumber(fallback.flowEndPages),
        flow_end_pages: positiveNumber(fallback.flowEndPages),
        pageCountHint: positiveNumber(fallback.pageCountHint),
        page_count_hint: positiveNumber(fallback.pageCountHint),
      });
    }
    return parts;
  }

  function normalizePublishSummary(raw, session = null) {
    if (!raw || typeof raw !== "object") return null;
    const recordCount = positiveNumber(raw.recordCount ?? raw.record_count ?? raw.recordCountActual ?? raw.record_count_actual);
    const recordCountActual = positiveNumber(raw.recordCountActual ?? raw.record_count_actual ?? recordCount);
    const coreProblemCount = positiveNumber(raw.coreProblemCount ?? raw.core_problem_count);
    const supplementalItemCount = positiveNumber(raw.supplementalItemCount ?? raw.supplemental_item_count);
    const pageCountHint = positiveNumber(raw.pageCountHint ?? raw.page_count_hint);
    const outerSize = positiveNumber(raw.outerSize ?? raw.outer_size);
    const edbFileName = String(raw.edbFileName || raw.edb_file_name || "").trim();
    const edbPath = String(raw.edbPath || raw.edb_path || "").trim();
    const outputDir = String(raw.outputDir || raw.output_dir || session?.output_dir || session?.outputDir || "").trim();
    const edbFileUri = String(raw.edbFileUri || raw.edb_file_uri || session?.edb_file_uri || session?.edbFileUri || "").trim();
    const edbFileExists = raw.edbFileExists ?? raw.edb_file_exists;
    const edbParts = normalizeEdbParts(raw, {
      edbFileName,
      edbPath,
      edbFileUri,
      edbFileExists,
      recordCount: recordCountActual || recordCount,
      pageCountHint,
    });
    const classinReview = raw.classinReview || raw.classin_review || session?.classinReview || session?.classin_review || {};
    const classinReviewStatus = String(
      raw.classinReviewStatus
      || raw.classin_review_status
      || classinReview.status
      || ""
    ).trim();
    const classinReviewStatusLabel = String(
      raw.classinReviewStatusLabel
      || raw.classin_review_status_label
      || classinReview.statusLabel
      || classinReview.status_label
      || (classinReviewStatus === "passed" ? "ClassIn 확인 완료" : "")
    ).trim();
    const classinHandoffUri = String(
      raw.classinHandoffUri
      || raw.classin_handoff_uri
      || session?.classin_handoff_uri
      || session?.classinHandoffUri
      || ""
    ).trim();
    const classinHandoffMarkdownUri = String(
      raw.classinHandoffMarkdownUri
      || raw.classin_handoff_markdown_uri
      || session?.classin_handoff_markdown_uri
      || session?.classinHandoffMarkdownUri
      || ""
    ).trim();
    const classinHandoffStatus = String(
      raw.classinHandoffStatus
      || raw.classin_handoff_status
      || session?.classin_handoff_status
      || session?.classinHandoffStatus
      || ""
    ).trim();
    const rawReadyForClassIn = raw.readyForClassIn ?? raw.ready_for_classin ?? session?.readyForClassIn ?? session?.ready_for_classin;
    const readyForClassIn = rawReadyForClassIn === undefined
      ? classinHandoffStatus === "ready_for_classin_review"
      : rawReadyForClassIn !== false;
    const classinHandoffStatusLabel = String(
      raw.classinHandoffStatusLabel
      || raw.classin_handoff_status_label
      || (classinHandoffStatus
        ? (readyForClassIn ? "ClassIn 전달 준비" : "ClassIn 전달 주의")
        : "")
    ).trim();
    const classinPreflight = raw.classinPreflight || raw.classin_preflight || session?.classinPreflight || session?.classin_preflight || {};
    const classinPreflightStatus = String(
      raw.classinPreflightStatus
      || raw.classin_preflight_status
      || classinPreflight.status
      || ""
    ).trim();
    const classinPreflightIssueCount = positiveNumber(
      raw.classinPreflightIssueCount
      ?? raw.classin_preflight_issue_count
      ?? classinPreflight.issueCount
      ?? classinPreflight.issue_count
    );
    const rawClassinPreflightPassed = raw.classinPreflightPassed ?? raw.classin_preflight_passed ?? classinPreflight.passed;
    const classinPreflightPassed = rawClassinPreflightPassed === undefined
      ? classinPreflightStatus === "passed"
      : rawClassinPreflightPassed !== false;
    const classinPreflightStatusLabel = String(
      raw.classinPreflightStatusLabel
      || raw.classin_preflight_status_label
      || (classinPreflightStatus
        ? (classinPreflightPassed ? "ClassIn 사전점검 OK" : `ClassIn 사전점검 주의 ${classinPreflightIssueCount}`)
        : "")
    ).trim();
    const classinPreflightIssueLabelList = Array.isArray(raw.classinPreflightIssueLabels)
      ? raw.classinPreflightIssueLabels.map(label => String(label || "").trim()).filter(Boolean)
      : classinPreflightIssueLabels(classinPreflight);
    const classinPreflightIssueSummaryLabel = String(
      raw.classinPreflightIssueSummaryLabel
      || raw.classin_preflight_issue_summary_label
      || classinPreflightIssueLabelList.join(" · ")
    ).trim();
    const passageGroups = normalizePassageGroups(raw, session);
    const passageGroupCount = positiveNumber(
      raw.passageGroupCount
      ?? raw.passage_group_count
      ?? session?.passageGroupCount
      ?? session?.passage_group_count
      ?? passageGroups.length
    );
    const normalizedPassageProblemCount = positiveNumber(
      raw.passageProblemCount
      ?? raw.passage_problem_count
      ?? session?.passageProblemCount
      ?? session?.passage_problem_count
      ?? passageProblemCount(passageGroups)
    );
    const normalizedCrossPagePassageGroupCount = positiveNumber(
      raw.crossPagePassageGroupCount
      ?? raw.cross_page_passage_group_count
      ?? session?.crossPagePassageGroupCount
      ?? session?.cross_page_passage_group_count
      ?? crossPagePassageGroupCount(passageGroups)
    );
    const passageGroupLabel = String(
      raw.passageGroupLabel
      || raw.passage_group_label
      || formatPassageGroupLabel({
        groupCount: passageGroupCount,
        problemCount: normalizedPassageProblemCount,
        crossPageCount: normalizedCrossPagePassageGroupCount,
      })
    ).trim();
    const passageReviewItems = normalizePassageReviewItems(raw, session);
    const passageReviewItemCount = positiveNumber(
      raw.passageReviewItemCount
      ?? raw.passage_review_item_count
      ?? session?.passageReviewItemCount
      ?? session?.passage_review_item_count
      ?? passageReviewItems.length
    );
    const crossPagePassageReviewItemCount = positiveNumber(
      raw.crossPagePassageReviewItemCount
      ?? raw.cross_page_passage_review_item_count
      ?? session?.crossPagePassageReviewItemCount
      ?? session?.cross_page_passage_review_item_count
      ?? countCrossPagePassageReviewItems(passageReviewItems)
    );
    const passageReviewLabel = String(
      raw.passageReviewLabel
      || raw.passage_review_label
      || formatPassageReviewLabel({
        itemCount: passageReviewItemCount,
        crossPageCount: crossPagePassageReviewItemCount,
      })
    ).trim();
    const passageReviewReasonLabel = String(
      raw.passageReviewReasonLabel
      || raw.passage_review_reason_label
      || formatPassageReviewReasonLabel(passageReviewItems)
    ).trim();
    const sourceProblemOverlapGroups = normalizeSourceProblemOverlapGroups(raw, session);
    const sourceProblemOverlapGroupCount = positiveNumber(
      raw.sourceProblemOverlapGroupCount
      ?? raw.source_problem_overlap_group_count
      ?? session?.sourceProblemOverlapGroupCount
      ?? session?.source_problem_overlap_group_count
      ?? sourceProblemOverlapGroups.length
    );
    const sourceProblemOverlapDetailLabel = String(
      raw.sourceProblemOverlapDetailLabel
      || raw.source_problem_overlap_detail_label
      || formatSourceProblemOverlapDetailLabel(sourceProblemOverlapGroups)
      || raw.sourceProblemOverlapLabel
      || raw.source_problem_overlap_label
      || ""
    ).trim();
    const sourceProblemOverlapLabel = String(
      sourceProblemOverlapGroupCount > 0
        ? formatSourceProblemOverlapLabel({
          groupCount: sourceProblemOverlapGroupCount,
          groups: sourceProblemOverlapGroups,
        })
        : (raw.sourceProblemOverlapLabel || raw.source_problem_overlap_label || "")
    ).trim();
    const passageGroupSourceReuseGroups = normalizePassageGroupSourceReuseGroups(raw, session);
    const passageGroupSourceReuseGroupCount = positiveNumber(
      raw.passageGroupSourceReuseGroupCount
      ?? raw.passage_group_source_reuse_group_count
      ?? session?.passageGroupSourceReuseGroupCount
      ?? session?.passage_group_source_reuse_group_count
      ?? passageGroupSourceReuseGroups.length
    );
    const passageGroupSourceReuseDetailLabel = String(
      raw.passageGroupSourceReuseDetailLabel
      || raw.passage_group_source_reuse_detail_label
      || formatPassageGroupSourceReuseDetailLabel(passageGroupSourceReuseGroups)
      || raw.passageGroupSourceReuseLabel
      || raw.passage_group_source_reuse_label
      || ""
    ).trim();
    const passageGroupSourceReuseLabel = String(
      passageGroupSourceReuseGroupCount > 0
        ? formatPassageGroupSourceReuseLabel({
          groupCount: passageGroupSourceReuseGroupCount,
          groups: passageGroupSourceReuseGroups,
        })
        : (raw.passageGroupSourceReuseLabel || raw.passage_group_source_reuse_label || "")
    ).trim();
    const layoutDiagnostics = normalizeLayoutDiagnostics(raw, session);
    const layoutDiagnosticsLabel = String(
      raw.layoutDiagnosticsLabel
      || raw.layout_diagnostics_label
      || formatLayoutDiagnosticsLabel(layoutDiagnostics)
    ).trim();
    const outputDirExists = raw.outputDirExists ?? raw.output_dir_exists;
    if (!edbFileName && !edbPath && !edbFileUri && !edbParts.length) return null;
    const normalizedEdbPartCount = edbParts.length
      ? edbParts.length
      : positiveNumber(raw.edbPartCount ?? raw.edb_part_count);
    const normalizedEdbSplit = edbParts.length
      ? edbParts.length > 1
      : Boolean(raw.edbSplit ?? raw.edb_split);
    const summary = {
      validated: raw.validated !== false,
      statusLabel: String(raw.statusLabel || raw.status_label || "제작 완료"),
      edbFileName: edbFileName || (edbPath ? edbPath.split("/").pop() : "classin.edb"),
      edbPath,
      edbFileUri,
      edbParts,
      edbPartCount: normalizedEdbPartCount,
      edbSplit: normalizedEdbSplit,
      outputDir,
      classinReview,
      classinReviewStatus,
      classinReviewStatusLabel,
      classinReviewPassed: (raw.classinReviewPassed ?? raw.classin_review_passed) === undefined
        ? classinReviewStatus === "passed"
        : (raw.classinReviewPassed ?? raw.classin_review_passed) !== false,
      classinHandoffUri,
      classinHandoffMarkdownUri,
      classinHandoffStatus,
      classinHandoffStatusLabel,
      readyForClassIn,
      classinPreflight,
      classinPreflightStatus,
      classinPreflightStatusLabel,
      classinPreflightPassed,
      classinPreflightIssueCount,
      classinPreflightIssueLabels: classinPreflightIssueLabelList,
      classinPreflightIssueSummaryLabel,
      passageGroups,
      passageGroupCount,
      passageProblemCount: normalizedPassageProblemCount,
      crossPagePassageGroupCount: normalizedCrossPagePassageGroupCount,
      passageGroupLabel,
      passageReviewItems,
      passageReviewItemCount,
      crossPagePassageReviewItemCount,
      passageReviewLabel,
      passageReviewReasonLabel,
      sourceProblemOverlapGroups,
      sourceProblemOverlapGroupCount,
      sourceProblemOverlapLabel,
      sourceProblemOverlapDetailLabel,
      passageGroupSourceReuseGroups,
      passageGroupSourceReuseGroupCount,
      passageGroupSourceReuseLabel,
      passageGroupSourceReuseDetailLabel,
      layoutDiagnostics,
      layoutDiagnosticsLabel,
      edbFileExists: edbFileExists === undefined ? true : edbFileExists !== false,
      outputDirExists: outputDirExists === undefined ? Boolean(outputDir) : outputDirExists !== false,
      recordCount,
      recordCountActual,
      coreProblemCount,
      supplementalItemCount,
      pageCountHint,
      outerSize,
      publishedAt: String(raw.publishedAt || raw.published_at || "").trim(),
    };
    summary.canDownload = summary.edbParts.some(part => Boolean(part.edbFileUri) && part.edbFileExists !== false)
      || (Boolean(summary.edbFileUri) && summary.edbFileExists !== false);
    summary.canOpenEdbFile = summary.edbParts.some(part => Boolean(part.edbPath) && part.edbFileExists !== false)
      || (Boolean(summary.edbPath) && summary.edbFileExists !== false);
    summary.canOpenOutputDir = Boolean(summary.outputDir) && summary.outputDirExists !== false;
    summary.canOpenClassinHandoff = Boolean(summary.classinHandoffMarkdownUri || summary.classinHandoffUri);
    summary.canMarkClassinReviewComplete = summary.canOpenEdbFile && !summary.classinReviewPassed;
    summary.recordCountLabel = formatRecordCountLabel({ ...raw, ...summary });
    return summary;
  }

  function formatPublishTime(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleString("ko-KR", {
      timeZone: "Asia/Seoul",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function formatPublishHistoryMeta(summary) {
    const recordLabel = formatRecordCountLabel(summary);
    const timeLabel = formatPublishTime(summary?.publishedAt || summary?.published_at);
    return [timeLabel, recordLabel].filter(Boolean).join(" · ") || recordLabel;
  }

  return {
    classinPreflightIssueLabel,
    classinPreflightIssueLabels,
    normalizePublishPreflightBlock,
    formatPublishHistoryMeta,
    formatRecordCountLabel,
    formatPublishTime,
    formatPassageGroupLabel,
    formatSourceProblemOverlapLabel,
    formatPassageGroupSourceReuseLabel,
    formatLayoutDiagnosticsLabel,
    normalizePublishSummary,
  };
});
