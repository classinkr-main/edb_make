(function createDemoGate() {
  "use strict";

  const $ = id => document.getElementById(id);
  const state = { active: false, authenticated: false, endsAt: 0, timer: null, reset: () => {}, busy: false };

  function message(text) {
    $("demo-message").textContent = text || "";
    $("demo-message").hidden = !text;
  }

  function lock(text, inactive = false) {
    clearTimeout(state.timer);
    state.authenticated = false;
    if (inactive) state.active = false;
    state.reset();
    $("demo-app").hidden = true;
    $("demo-session").hidden = true;
    $("demo-gate").hidden = false;
    $("demo-login").hidden = !state.active;
    $("demo-password").value = "";
    message(text);
  }

  function canParse() {
    if (state.active && state.endsAt <= Date.now()) {
      lock("시연 기간이 끝났어요. 일반 무료 체험을 이용해 주세요.", true);
    }
    return state.active && state.authenticated;
  }

  function unlock(endsAt) {
    const end = Date.parse(endsAt);
    if (!Number.isFinite(end) || end <= Date.now()) {
      lock("시연 기간이 끝났어요. 일반 무료 체험을 이용해 주세요.", true);
      return;
    }
    state.active = true;
    state.authenticated = true;
    state.endsAt = end;
    $("demo-gate").hidden = true;
    $("demo-app").hidden = false;
    $("demo-session").hidden = false;
    $("demo-expiration").textContent = `시연 이용 가능: ${new Date(end).toLocaleString("ko-KR", { timeZone: "Asia/Seoul" })}까지 (한국시간)`;
    clearTimeout(state.timer);
    state.timer = setTimeout(() => canParse(), Math.min(end - Date.now() + 50, 2147483647));
  }

  function errorBody(text) {
    try { return JSON.parse(text).error || {}; } catch (_) { return {}; }
  }

  function handleResponse(status, text) {
    const error = errorBody(text);
    if (status === 401 || error.code === "demo_auth_required") {
      lock("시연 인증이 만료됐어요. 비밀번호를 다시 입력해 주세요.");
      return true;
    }
    if (error.code === "demo_unavailable" || status === 404) {
      lock("지금은 시연 기간이 아니에요. 일반 무료 체험을 이용해 주세요.", true);
      return true;
    }
    return false;
  }

  async function loadConfig() {
    $("demo-config-retry").hidden = true;
    try {
      const response = await fetch("/api/demo/config", { cache: "no-store", credentials: "same-origin" });
      if (!response.ok) throw new Error("config unavailable");
      const config = await response.json();
      state.active = config.active === true;
      state.endsAt = Date.parse(config.ends_at);
      if (!state.active || !Number.isFinite(state.endsAt) || state.endsAt <= Date.now()) {
        lock("지금은 시연 기간이 아니에요. 일반 무료 체험을 이용해 주세요.", true);
      } else if (config.authenticated === true) {
        unlock(config.ends_at);
      } else {
        lock("안내받은 시연 비밀번호를 입력해 주세요.");
      }
      return config;
    } catch (_) {
      lock("시연 정보를 불러오지 못했어요. 연결을 확인한 뒤 다시 시도해 주세요.", true);
      $("demo-config-retry").hidden = false;
      return {};
    }
  }

  async function login(event) {
    event.preventDefault();
    const button = $("demo-login-button");
    if (button.disabled || !state.active) return;
    button.disabled = true;
    message("비밀번호를 확인하고 있어요.");
    try {
      const request = fetch("/api/demo/login", {
        method: "POST", credentials: "same-origin",
        headers: { "content-type": "application/json", "X-Demo-Request": "1" },
        body: JSON.stringify({ password: $("demo-password").value }),
      });
      $("demo-password").value = "";
      const response = await request;
      const text = await response.text();
      if (response.ok) {
        const payload = JSON.parse(text);
        if (payload.authenticated === true) unlock(payload.ends_at);
        else lock("인증을 확인하지 못했어요. 다시 입력해 주세요.");
      } else if (response.status === 401) {
        lock("시연 비밀번호를 확인해 주세요.");
      } else if (!handleResponse(response.status, text)) {
        message(response.status === 429
          ? "비밀번호 확인 요청이 많아요. 잠시 후 다시 시도해 주세요."
          : "시연을 시작하지 못했어요. 새로고침 후 다시 시도해 주세요.");
      }
    } catch (_) {
      message("서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.");
    } finally {
      $("demo-password").value = "";
      button.disabled = false;
    }
  }

  async function logout() {
    if (state.busy) return;
    $("demo-logout").disabled = true;
    try {
      const response = await fetch("/api/demo/logout", {
        method: "POST", credentials: "same-origin", headers: { "X-Demo-Request": "1" },
      });
      lock(response.ok
        ? "로그아웃했어요. 다시 이용하려면 비밀번호를 입력해 주세요."
        : "서버에서 로그아웃을 완료하지 못했어요. 연결을 확인해 주세요.");
    } catch (_) {
      lock("서버에서 로그아웃을 완료하지 못했어요. 연결을 확인해 주세요.");
    } finally {
      $("demo-logout").disabled = false;
    }
  }

  window.TRIAL_DEMO = {
    initialize(reset) {
      state.reset = reset;
      $("demo-login").addEventListener("submit", login);
      $("demo-logout").addEventListener("click", logout);
      $("demo-config-retry").addEventListener("click", () => window.location.reload());
      // A restored browser-history snapshot must not resurrect an authenticated view.
      window.addEventListener("pageshow", event => {
        if (event.persisted) {
          lock("시연 인증을 다시 확인하고 있어요.", true);
          loadConfig();
        }
      });
      document.addEventListener("visibilitychange", () => {
        if (!document.hidden) canParse();
      });
      return loadConfig();
    },
    canParse,
    handleResponse,
    setBusy(busy) {
      state.busy = busy;
      $("demo-logout").disabled = busy;
    },
  };
})();
