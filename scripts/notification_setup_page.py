"""Render the self-contained local Discord notification setup page."""

from __future__ import annotations

from html import escape
from typing import Final

_STYLE: Final = """
:root {
  color-scheme: light dark;
  --surface-canvas: #f7f6f3;
  --surface-panel: #ffffff;
  --surface-input: #fbfbfa;
  --text-primary: #191919;
  --text-secondary: #62605d;
  --text-on-accent: #ffffff;
  --border-default: #d9d7d2;
  --accent-primary: #4f46b8;
  --accent-hover: #433a9f;
  --success-surface: #edf3ec;
  --success-text: #285a31;
  --error-surface: #fdebec;
  --error-text: #8a2927;
  --focus-ring: #4f46b8;
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-6: 24px;
  --space-8: 32px;
  --space-12: 48px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --surface-canvas: #08090a;
    --surface-panel: #0f1011;
    --surface-input: #191a1b;
    --text-primary: #f7f8f8;
    --text-secondary: #a7adb7;
    --border-default: #34343a;
    --accent-primary: #7170ff;
    --accent-hover: #828fff;
    --success-surface: #122b1b;
    --success-text: #8de3a1;
    --error-surface: #321719;
    --error-text: #ffaaa5;
    --focus-ring: #a5a4ff;
  }
}
* { box-sizing: border-box; }
html { min-block-size: 100%; background: var(--surface-canvas); }
body {
  min-block-size: 100dvh;
  margin: 0;
  display: grid;
  place-items: center;
  padding: var(--space-8) var(--space-4);
  background: var(--surface-canvas);
  color: var(--text-primary);
  font-family: "Segoe UI Variable", "Noto Sans KR", system-ui, sans-serif;
  font-size: 16px;
  line-height: 1.6;
}
main { inline-size: min(100%, 560px); }
.panel {
  padding: clamp(24px, 6vw, 48px);
  border: 1px solid var(--border-default);
  border-radius: 12px;
  background: var(--surface-panel);
}
.eyebrow {
  margin: 0 0 var(--space-2);
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: .08em;
  text-transform: uppercase;
}
h1 {
  margin: 0;
  font-size: 28px;
  font-weight: 650;
  line-height: 1.25;
  letter-spacing: -.02em;
}
.lead { margin: var(--space-3) 0 var(--space-8); color: var(--text-secondary); }
form { display: grid; gap: var(--space-4); }
label { font-size: 14px; font-weight: 600; }
input {
  inline-size: 100%;
  min-block-size: 48px;
  margin-block-start: var(--space-2);
  padding: 0 var(--space-3);
  border: 1px solid var(--border-default);
  border-radius: 6px;
  outline: 0;
  background: var(--surface-input);
  color: var(--text-primary);
  font: 16px/1.4 "Cascadia Mono", "SFMono-Regular", Consolas, monospace;
}
input:hover { border-color: var(--text-secondary); }
input:focus-visible, button:focus-visible {
  outline: 3px solid var(--focus-ring);
  outline-offset: 2px;
}
.help, .privacy {
  margin: var(--space-2) 0 0;
  color: var(--text-secondary);
  font-size: 14px;
}
button {
  min-block-size: 48px;
  padding: 0 var(--space-4);
  border: 1px solid transparent;
  border-radius: 6px;
  background: var(--accent-primary);
  color: var(--text-on-accent);
  cursor: pointer;
  font: 600 16px/1.2 "Segoe UI Variable", "Noto Sans KR", system-ui, sans-serif;
  transition: background-color 150ms ease-out, opacity 150ms ease-out;
}
button:hover { background: var(--accent-hover); }
button:active { opacity: .84; }
button:disabled { cursor: wait; opacity: .58; }
.result {
  min-block-size: 24px;
  margin: 0;
  padding: var(--space-3);
  border-radius: 6px;
  color: var(--text-secondary);
  font-size: 14px;
}
.result:empty { display: none; }
.result.error { background: var(--error-surface); color: var(--error-text); }
.result.success { background: var(--success-surface); color: var(--success-text); }
.privacy {
  margin-block-start: var(--space-6);
  padding-block-start: var(--space-4);
  border-block-start: 1px solid var(--border-default);
}
[hidden] { display: none !important; }
@media (max-width: 420px) {
  body { padding: var(--space-4); }
  .panel { padding: var(--space-6); }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; }
}
"""

_SCRIPT: Final = """
const form = document.querySelector("#setup-form");
const field = document.querySelector("#webhook-url");
const button = document.querySelector("#submit");
const result = document.querySelector("#result");
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  button.disabled = true;
  button.textContent = "연결 확인 중…";
  form.setAttribute("aria-busy", "true");
  result.className = "result";
  result.setAttribute("role", "status");
  result.textContent = "Discord로 테스트 알림을 보내고 있습니다.";
  const body = new URLSearchParams(new FormData(form));
  try {
    const response = await fetch(location.pathname, {
      method: "POST",
      headers: {"Content-Type": "application/x-www-form-urlencoded"},
      body
    });
    const payload = await response.json();
    if (!response.ok) {
      result.className = "result error";
      result.setAttribute("role", "alert");
      result.textContent = payload.message || "연결하지 못했습니다. 주소를 확인해 다시 시도하세요.";
      field.setAttribute("aria-invalid", "true");
      field.focus();
      return;
    }
    field.value = "";
    form.hidden = true;
    result.className = "result success";
    result.textContent = "연결되었습니다. 이 창을 닫아도 됩니다.";
  } catch (_error) {
    result.className = "result error";
    result.setAttribute("role", "alert");
    result.textContent = "로컬 설정 연결이 끊겼습니다. Codex에서 설정 링크를 다시 열어주세요.";
  } finally {
    form.removeAttribute("aria-busy");
    if (!form.hidden) {
      button.disabled = false;
      button.textContent = "연결 테스트 및 저장";
    }
  }
});
"""


def render_setup_page(csrf_token: str, script_nonce: str) -> bytes:
    """Return one UTF-8 page without reflecting a webhook or setup URL."""
    csrf = escape(csrf_token, quote=True)
    nonce = escape(script_nonce, quote=True)
    document = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="Codex Must Work Discord 알림 로컬 연결">
  <title>CMW Discord 알림 연결</title>
  <style>{_STYLE}</style>
</head>
<body>
  <main>
    <section class="panel" aria-labelledby="title">
      <p class="eyebrow">Codex Must Work</p>
      <h1 id="title">Discord 알림 연결</h1>
      <p class="lead">CMW가 병목과 회복을 알려줄 Discord 웹훅을 연결합니다.</p>
      <form id="setup-form" novalidate>
        <input type="hidden" name="csrf_token" value="{csrf}">
        <div>
          <label for="webhook-url">웹훅 주소</label>
          <input id="webhook-url" name="webhook_url" type="password" required
            autocomplete="off" autocapitalize="none" spellcheck="false"
            aria-describedby="webhook-help">
          <p class="help" id="webhook-help">Discord에서 복사한 웹훅 주소 전체를 붙여넣으세요.</p>
        </div>
        <button id="submit" type="submit">연결 테스트 및 저장</button>
      </form>
      <p id="result" class="result" role="status" aria-live="polite"></p>
      <p class="privacy">이 주소는 Codex 대화에 들어가지 않고 이 PC에만 저장됩니다.</p>
    </section>
  </main>
  <script nonce="{nonce}">{_SCRIPT}</script>
</body>
</html>"""
    return document.encode("utf-8")
