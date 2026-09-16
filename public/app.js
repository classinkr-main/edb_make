(function startTrialPage() {
  "use strict";

  const logic = window.TRIAL_LOGIC;
  const demo = document.body.dataset.trialMode === "demo" ? window.TRIAL_DEMO : null;
  const TURNSTILE_SCRIPT = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
  // Long enough for a visitor to notice and finish an interactive challenge.
  const TOKEN_WAIT_MS = 60000;

  const state = {
    config: { inquiry_url: "https://classin.co.kr/contact", turnstile_site_key: null, max_bytes: 4000000, max_pages: 4, daily_limit: 3 },
    widgetId: null,
    token: null,
    tokenWaiters: [],
    busy: false,
    configReady: null,
    lastPayload: null,
    popupContext: {},
  };

  const $ = id => document.getElementById(id);
  const views = { upload: $("view-upload"), processing: $("view-processing"), result: $("view-result") };

  function showView(name) {
    for (const [key, element] of Object.entries(views)) {
      element.hidden = key !== name;
    }
    window.scrollTo({ top: 0 });
  }

  function showUploadMessage(message) {
    const element = $("upload-message");
    element.textContent = message || "";
    element.hidden = !message;
    if (message) {
      showUploadHint("");
    }
  }

  function showUploadHint(message) {
    const element = $("upload-hint");
    element.textContent = message || "";
    element.hidden = !message;
  }

  function sendEvent(feature, action) {
    if (demo) return; // Keep event demonstrations out of the public conversion funnel.
    const body = JSON.stringify({ feature, action });
    try {
      if (navigator.sendBeacon && navigator.sendBeacon("/api/event", new Blob([body], { type: "text/plain" }))) {
        return;
      }
    } catch (error) {
      // fall through to fetch
    }
    fetch("/api/event", { method: "POST", body, keepalive: true, headers: { "content-type": "text/plain" } }).catch(() => {});
  }

  function openPremium(feature, context) {
    const content = logic.popupContent(feature, { mb: Math.floor(state.config.max_bytes / 1000000), max: state.config.max_pages, ...context });
    $("premium-badge").textContent = content.badge;
    $("premium-title").textContent = content.title;
    $("premium-body").textContent = content.body;
    const inquiry = $("premium-inquiry");
    inquiry.textContent = content.inquiryLabel;
    inquiry.href = state.config.inquiry_url;
    inquiry.dataset.feature = content.feature;
    $("premium-close").textContent = content.closeLabel;
    const dialog = $("premium-dialog");
    if (!dialog.open) {
      dialog.showModal();
    }
    sendEvent(content.feature, "open");
  }

  function resolveTokenWaiters(token) {
    const waiters = state.tokenWaiters.splice(0);
    for (const resolve of waiters) {
      resolve(token);
    }
  }

  function waitForToken() {
    if (!state.config.turnstile_site_key) {
      return Promise.resolve(null);
    }
    if (state.token) {
      return Promise.resolve(state.token);
    }
    return new Promise(resolve => {
      state.tokenWaiters.push(resolve);
      setTimeout(() => resolve(state.token), TOKEN_WAIT_MS);
    });
  }

  function resetToken() {
    state.token = null;
    if (window.turnstile && state.widgetId !== null) {
      window.turnstile.reset(state.widgetId);
    }
  }

  function loadTurnstile(siteKey) {
    const script = document.createElement("script");
    script.src = TURNSTILE_SCRIPT;
    script.async = true;
    script.onload = () => {
      state.widgetId = window.turnstile.render("#turnstile-widget", {
        sitekey: siteKey,
        action: "parse",
        language: "ko",
        callback: token => {
          state.token = token;
          resolveTokenWaiters(token);
        },
        "expired-callback": () => {
          state.token = null;
        },
        "error-callback": () => {
          state.token = null;
          // Waiters would otherwise sit out the full timeout for a token that cannot come.
          resolveTokenWaiters(null);
        },
      });
    };
    script.onerror = () => showUploadMessage("확인 도구를 불러오지 못했어요. 새로고침 후 다시 시도해 주세요.");
    document.head.appendChild(script);
  }

  async function loadConfig() {
    try {
      if (demo) {
        const config = await demo.initialize(() => {
          state.lastPayload = null;
          $("pages").replaceChildren();
          $("problems").replaceChildren();
          $("file-input").value = "";
          if ($("premium-dialog").open) $("premium-dialog").close();
          showView("upload");
        });
        state.config = { ...state.config, ...config, turnstile_site_key: null };
      } else {
        const response = await fetch("/api/config", { cache: "no-store" });
        if (response.ok) {
          state.config = { ...state.config, ...(await response.json()) };
        }
      }
    } catch (error) {
      // keep defaults; the upload will report connection problems
    }
    const mb = Math.floor(state.config.max_bytes / 1000000);
    $("limits-text").textContent = demo
      ? `앞 ${state.config.max_pages}쪽 · ${mb}MB · 시연 기간에는 반복 이용 가능`
      : `앞 ${state.config.max_pages}쪽 · ${mb}MB · 하루 ${state.config.daily_limit}회`;
    $("header-inquiry").href = state.config.inquiry_url;
    if (state.config.turnstile_site_key) {
      loadTurnstile(state.config.turnstile_site_key);
    }
  }

  function startElapsedTimer() {
    const startedAt = performance.now();
    const label = $("processing-elapsed");
    label.textContent = "0초";
    const timer = setInterval(() => {
      label.textContent = `${Math.floor((performance.now() - startedAt) / 1000)}초`;
    }, 500);
    return () => clearInterval(timer);
  }

  async function handleFile(file) {
    // Clear on every path so choosing the same file again fires "change" again.
    $("file-input").value = "";
    if (state.busy || !file) {
      return;
    }
    state.busy = true;
    let stopTimer = () => {};
    try {
      // Without the config we do not know whether a Turnstile token is required.
      await state.configReady;
      if (demo && !demo.canParse()) return;
      if (demo) demo.setBusy(true);
      showUploadMessage("");
      const precheck = logic.precheckFile(file, state.config);
      if (precheck) {
        if (precheck.feature) {
          openPremium(precheck.feature);
        } else {
          showUploadMessage(precheck.message);
        }
        return;
      }

      // Wait on the upload view: the widget lives there and may need a click.
      if (state.config.turnstile_site_key && !state.token) {
        showUploadHint("아래 확인을 완료하면 바로 시작해요.");
      }
      const token = await waitForToken();
      showUploadHint("");
      if (state.config.turnstile_site_key && !token) {
        showUploadMessage("사람 확인이 아직 끝나지 않았어요. 확인 상자를 완료한 뒤 다시 올려 주세요.");
        return;
      }

      $("processing-file").textContent = file.name;
      showView("processing");
      stopTimer = startElapsedTimer();
      let status = 0;
      let text = "";
      try {
        const headers = { "content-type": "application/pdf" };
        if (token) {
          headers["x-turnstile-token"] = token;
        }
        if (demo) headers["X-Demo-Request"] = "1";
        const response = await fetch(demo ? "/api/demo/parse" : "/api/parse", {
          method: "POST", body: file, headers, credentials: "same-origin",
        });
        status = response.status;
        text = await response.text();
      } catch (error) {
        status = 0;
      } finally {
        resetToken();
      }

      if (demo && (demo.handleResponse(status, text) || !demo.canParse())) return;
      if (status === 200) {
        let payload = null;
        try {
          payload = JSON.parse(text);
        } catch (error) {
          payload = null;
        }
        if (payload && Array.isArray(payload.pages)) {
          renderResult(payload);
          return;
        }
      }
      const problem = logic.interpretError(status, text);
      showView("upload");
      // Say what happened even when a premium popup follows, e.g. a parse failure.
      showUploadMessage(problem.message);
      if (problem.feature) {
        openPremium(problem.feature);
      }
    } finally {
      stopTimer();
      state.busy = false;
      if (demo) demo.setBusy(false);
      $("file-input").value = "";
    }
  }

  function setActive(problemId, source) {
    for (const element of document.querySelectorAll("[data-problem-id]")) {
      element.classList.toggle("is-active", element.dataset.problemId === problemId);
    }
    const target =
      source === "region"
        ? document.querySelector(`.problem-card[data-problem-id="${CSS.escape(problemId)}"]`)
        : document.querySelector(`.region[data-problem-id="${CSS.escape(problemId)}"]`);
    if (target) {
      target.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }

  function renderPages(payload) {
    const container = $("pages");
    container.replaceChildren();
    const pagesById = new Map();
    for (const page of payload.pages) {
      const figure = document.createElement("figure");
      figure.className = "page-figure";
      if (logic.isSafeImageSource(page.preview)) {
        const image = document.createElement("img");
        image.src = page.preview;
        image.alt = `${page.index + 1}쪽 미리보기`;
        image.width = page.width;
        image.height = page.height;
        figure.appendChild(image);
      }
      const caption = document.createElement("figcaption");
      caption.textContent = `${page.index + 1}쪽`;
      figure.appendChild(caption);
      container.appendChild(figure);
      pagesById.set(page.page_id, { page, figure });
    }
    for (const problem of payload.problems) {
      const label = logic.problemLabel(problem);
      problem.regions.forEach((region, regionIndex) => {
        const target = pagesById.get(region.page_id);
        if (!target) {
          return;
        }
        const button = document.createElement("button");
        button.type = "button";
        button.className = problem.number === null ? "region region--passage" : "region";
        button.dataset.problemId = problem.problem_id;
        button.setAttribute("aria-label", `${label} 영역`);
        Object.assign(button.style, logic.regionStyle(region.bbox, target.page));
        if (regionIndex === 0) {
          const tag = document.createElement("span");
          tag.className = "region__label";
          tag.textContent = label;
          button.appendChild(tag);
        }
        button.addEventListener("click", () => setActive(problem.problem_id, "region"));
        target.figure.appendChild(button);
      });
    }
  }

  function renderProblems(payload) {
    const list = $("problems");
    list.replaceChildren();
    for (const problem of payload.problems) {
      const item = document.createElement("li");
      item.className = "problem-card";
      item.dataset.problemId = problem.problem_id;
      item.tabIndex = 0;

      const head = document.createElement("div");
      head.className = "problem-card__head";
      const label = document.createElement("span");
      label.className = "problem-card__label";
      label.textContent = logic.problemLabel(problem);
      head.appendChild(label);
      if (problem.needs_review) {
        const chip = document.createElement("span");
        chip.className = "review-chip";
        chip.textContent = "확인 필요";
        head.appendChild(chip);
      }
      item.appendChild(head);

      if (logic.isSafeImageSource(problem.preview)) {
        const image = document.createElement("img");
        image.src = problem.preview;
        image.alt = `${logic.problemLabel(problem)} 미리보기`;
        image.loading = "lazy";
        item.appendChild(image);
      }

      if (problem.needs_review) {
        const ai = document.createElement("button");
        ai.type = "button";
        ai.className = "text-button problem-card__ai";
        ai.textContent = "AI로 더 정확하게 ✦";
        ai.addEventListener("click", event => {
          event.stopPropagation();
          openPremium("ai");
        });
        item.appendChild(ai);
      }

      const activate = () => setActive(problem.problem_id, "card");
      item.addEventListener("click", activate);
      item.addEventListener("keydown", event => {
        // Keys pressed on buttons inside the card belong to those buttons.
        if (event.target !== item) {
          return;
        }
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          activate();
        }
      });
      list.appendChild(item);
    }
  }

  function renderResult(payload) {
    state.lastPayload = payload;
    const summary = logic.summarize(payload);
    $("result-title").textContent = summary.headline;
    $("result-meta").textContent = `${summary.seconds} · 앞 ${payload.processed_page_count}쪽 처리`;
    $("remaining-text").textContent = demo
      ? "시연 기간에는 횟수 제한 없이 다시 이용할 수 있어요"
      : logic.remainingText(payload.remaining_today);

    const banner = logic.pagesBanner(payload);
    const bannerButton = $("pages-banner");
    bannerButton.hidden = !banner;
    if (banner) {
      bannerButton.textContent = `${banner.text} →`;
      state.popupContext = banner.context;
    }

    const empty = payload.problems.length === 0;
    $("empty-result").hidden = !empty;
    $("result-body").hidden = empty;
    renderPages(payload);
    renderProblems(payload);
    showView("result");
    if (empty) {
      openPremium("ai");
    }
  }

  function bindEvents() {
    const input = $("file-input");
    input.addEventListener("change", () => handleFile(input.files && input.files[0]));

    const dropzone = $("dropzone");
    for (const type of ["dragenter", "dragover"]) {
      dropzone.addEventListener(type, event => {
        event.preventDefault();
        dropzone.classList.add("is-dragging");
      });
    }
    for (const type of ["dragleave", "drop"]) {
      dropzone.addEventListener(type, () => dropzone.classList.remove("is-dragging"));
    }
    // A file dropped anywhere must not make the browser navigate away to the PDF.
    document.addEventListener("dragover", event => {
      event.preventDefault();
    });
    document.addEventListener("drop", event => {
      event.preventDefault();
      dropzone.classList.remove("is-dragging");
      if (views.upload.hidden || state.busy) {
        return;
      }
      const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
      handleFile(file);
    });

    document.addEventListener("click", event => {
      const trigger = event.target.closest("[data-premium]");
      if (trigger) {
        openPremium(trigger.dataset.premium);
      }
    });

    $("pages-banner").addEventListener("click", () => openPremium("limit_pages", state.popupContext));
    $("premium-inquiry").addEventListener("click", event => sendEvent(event.currentTarget.dataset.feature || "ai", "inquiry"));
    $("retry-button").addEventListener("click", () => {
      showUploadMessage("");
      showView("upload");
    });

    const dialog = $("premium-dialog");
    dialog.addEventListener("click", event => {
      if (event.target === dialog) {
        dialog.close();
      }
    });
  }

  bindEvents();
  state.configReady = loadConfig();
})();
