(function attachTrialLogic(root, factory) {
  const logic = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = logic;
  }
  root.TRIAL_LOGIC = logic;
})(typeof globalThis !== "undefined" ? globalThis : window, function createTrialLogic() {
  const FEATURES = ["edb", "image", "edit", "ai", "scan", "limit_pages", "limit_size", "limit_daily"];

  const POPUPS = {
    limit_daily: {
      title: "오늘의 무료 체험을 모두 사용했어요",
      body: "프리미엄으로 더 누려보세요! 설치형 앱에서는 횟수 걱정 없이 시험지를 처리할 수 있어요.",
    },
    limit_pages: {
      title: "무료 체험은 앞 {max}쪽까지예요",
      body: "나머지 {rest}쪽도 프리미엄에서 한 번에 나눠 보세요! 시험지 한 권을 통째로 처리할 수 있어요.",
    },
    limit_size: {
      title: "무료 체험은 {mb}MB까지 올릴 수 있어요",
      body: "스캔본·고화질 시험지도 프리미엄에서 그대로 처리해 보세요.",
    },
    edb: {
      title: "클래스인 칠판으로 바로 보내보세요",
      body: "문항을 칠판에 자동 배치해 EDB 파일로 만들어 드려요. 프리미엄 기능이에요.",
    },
    image: {
      title: "잘라낸 문항을 수업 자료로 써보세요",
      body: "문항 이미지를 한 번에 저장하는 건 프리미엄 기능이에요.",
    },
    edit: {
      title: "문항 경계를 직접 다듬어 보세요",
      body: "합치기·나누기·영역 조정은 프리미엄에서 할 수 있어요.",
    },
    scan: {
      title: "스캔본·사진은 프리미엄 AI 인식으로",
      body: "무료 체험은 글자가 들어 있는 PDF(예: 모의고사 원본 PDF)만 나눠 드려요. 프리미엄 AI 정밀 인식으로 스캔본·사진도 문항을 나눠 보세요!",
    },
    ai: {
      title: "더 정확한 인식이 필요하신가요?",
      body: "프리미엄 AI 정밀 인식으로 스캔본·사진도 더 깔끔하게 나눠 드려요.",
    },
  };

  const MESSAGES = {
    bad_type: "PDF 파일만 올릴 수 있어요.",
    busy: "지금은 체험이 어려워요. 잠시 후 다시 시도해 주세요.",
    empty: "빈 파일이에요. 다른 PDF를 골라 주세요.",
    image_not_supported: "무료 체험은 글자가 들어 있는 PDF만 나눠 드려요.",
    network: "연결에 문제가 있어요. 잠시 후 다시 시도해 주세요.",
    parse_failed: "이 파일은 처리하지 못했어요.",
    too_large: "무료 체험은 4MB까지 올릴 수 있어요.",
  };

  function fill(template, context) {
    return template.replace(/\{(\w+)\}/g, (match, key) =>
      context && context[key] !== undefined && context[key] !== null ? String(context[key]) : match,
    );
  }

  function popupContent(feature, context) {
    const known = FEATURES.includes(feature) ? feature : "ai";
    const copy = POPUPS[known];
    return {
      feature: known,
      badge: "✦ 프리미엄",
      title: fill(copy.title, context),
      body: fill(copy.body, context),
      inquiryLabel: "프리미엄 도입 문의",
      closeLabel: "계속 체험하기",
    };
  }

  function precheckFile(file, limits) {
    const name = String((file && file.name) || "");
    const type = String((file && file.type) || "").toLowerCase();
    const size = Number((file && file.size) || 0);
    const isPdf = type === "application/pdf" || /\.pdf$/i.test(name);
    const isImage = type.startsWith("image/") || /\.(png|jpe?g|heic|heif|webp|gif|bmp|tiff?)$/i.test(name);
    if (!isPdf && isImage) {
      return { code: "image_not_supported", feature: "scan", message: MESSAGES.image_not_supported };
    }
    if (!isPdf) {
      return { code: "bad_type", feature: null, message: MESSAGES.bad_type };
    }
    if (size <= 0) {
      return { code: "empty", feature: null, message: MESSAGES.empty };
    }
    if (limits && size > Number(limits.max_bytes)) {
      return { code: "too_large", feature: "limit_size", message: MESSAGES.too_large };
    }
    return null;
  }

  function interpretError(status, bodyText) {
    let parsed = null;
    try {
      parsed = JSON.parse(bodyText);
    } catch (error) {
      parsed = null;
    }
    const error = parsed && typeof parsed === "object" ? parsed.error : null;
    if (error && typeof error === "object" && typeof error.code === "string") {
      const feature = FEATURES.includes(error.feature) ? error.feature : null;
      const message = typeof error.message === "string" && error.message ? error.message : MESSAGES.parse_failed;
      return { code: error.code, feature, message };
    }
    // Vercel platform errors (payload too large, timeouts) arrive without our JSON body.
    if (status === 413) {
      return { code: "too_large", feature: "limit_size", message: MESSAGES.too_large };
    }
    if (status === 500 || status === 502 || status === 504) {
      return { code: "parse_failed", feature: "ai", message: MESSAGES.parse_failed };
    }
    if (status === 503) {
      return { code: "busy", feature: null, message: MESSAGES.busy };
    }
    return { code: "network", feature: null, message: MESSAGES.network };
  }

  function percent(value) {
    const clamped = Math.min(100, Math.max(0, value));
    return `${Math.round(clamped * 1000) / 1000}%`;
  }

  function regionStyle(bbox, page) {
    const left = Math.max(0, bbox.left);
    const top = Math.max(0, bbox.top);
    const right = Math.min(page.width, bbox.left + bbox.width);
    const bottom = Math.min(page.height, bbox.top + bbox.height);
    return {
      left: percent((left / page.width) * 100),
      top: percent((top / page.height) * 100),
      width: percent((Math.max(0, right - left) / page.width) * 100),
      height: percent((Math.max(0, bottom - top) / page.height) * 100),
    };
  }

  function summarize(payload) {
    const problems = Array.isArray(payload.problems) ? payload.problems : [];
    const passageCount = problems.filter(problem => problem.number === null || problem.number === undefined).length;
    const questionCount = problems.length - passageCount;
    let headline = "문항을 찾지 못했어요";
    if (questionCount > 0 && passageCount > 0) {
      headline = `문항 ${questionCount}개와 지문 ${passageCount}개를 찾았어요`;
    } else if (questionCount > 0) {
      headline = `문항 ${questionCount}개를 찾았어요`;
    } else if (passageCount > 0) {
      headline = `지문 ${passageCount}개를 찾았어요`;
    }
    const seconds = `${(Math.max(0, Number(payload.elapsed_ms) || 0) / 1000).toFixed(1)}초`;
    return { headline, seconds, questionCount, passageCount };
  }

  function pagesBanner(payload) {
    const source = Number(payload.source_page_count) || 0;
    const processed = Number(payload.processed_page_count) || 0;
    const max = Number(payload.processed_page_limit) || processed;
    if (source <= processed) {
      return null;
    }
    const rest = source - processed;
    return {
      text: `✦ 무료 체험은 앞 ${max}쪽까지예요 · 나머지 ${rest}쪽은 프리미엄으로`,
      feature: "limit_pages",
      context: { max, rest },
    };
  }

  function problemLabel(problem) {
    if (problem.number !== null && problem.number !== undefined) {
      return `${problem.number}번`;
    }
    return problem.title ? String(problem.title) : "지문";
  }

  function remainingText(remaining) {
    return remaining > 0 ? `오늘 ${remaining}회 남음` : "오늘 무료 체험을 모두 사용했어요";
  }

  function isSafeImageSource(value) {
    return typeof value === "string" && value.startsWith("data:image/jpeg;base64,");
  }

  return {
    FEATURES,
    interpretError,
    isSafeImageSource,
    pagesBanner,
    popupContent,
    precheckFile,
    problemLabel,
    regionStyle,
    remainingText,
    summarize,
  };
});
