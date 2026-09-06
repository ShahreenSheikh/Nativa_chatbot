/**
 * NativaCare Chat Widget
 * ------------------------------------------------------------------
 * Drop-in floating chat bubble for nativacare.com (or any site). Add
 * ONE line before </body> on the real website:
 *
 *   <script src="https://YOUR-BACKEND-URL/widget.js" defer></script>
 *
 * That's it — no other config needed. The script figures out its own
 * backend URL from the <script> tag's own src, so the same file works
 * unmodified in local testing, staging, and production; you never have
 * to hand-edit this file per environment.
 *
 * If you ever need to point the widget at a DIFFERENT backend than the
 * one serving this file (e.g. a CDN-hosted copy), override it with a
 * data attribute:
 *   <script src="https://cdn.example.com/widget.js"
 *           data-api-url="https://your-backend-url" defer></script>
 *
 * Everything below is vanilla JS + injected CSS — no build step, no
 * dependencies, safe to embed on any site regardless of what else runs
 * there.
 */
(function () {
  "use strict";

  // ---------------------------------------------------------------
  // Config
  // ---------------------------------------------------------------
  var thisScript = document.currentScript;
  var API_BASE = (thisScript && thisScript.getAttribute("data-api-url"))
    || (thisScript && new URL(thisScript.src).origin)
    || "";
  var CLINIC_NAME = (thisScript && thisScript.getAttribute("data-clinic-name")) || "NativaCare";
  var SESSION_KEY = "nativacareWidgetSessionId";

  if (!API_BASE) {
    console.error("[NativaCare widget] Could not determine backend URL — " +
      "load this script with a normal <script src> (not inline) or set data-api-url.");
    return;
  }

  function getSessionId() {
    try {
      var id = localStorage.getItem(SESSION_KEY);
      if (!id) {
        id = "web_" + Math.random().toString(36).slice(2) + Date.now().toString(36);
        localStorage.setItem(SESSION_KEY, id);
      }
      return id;
    } catch (e) {
      // Storage blocked (private browsing etc.) — fall back to a
      // session that only lasts this page load.
      return "web_" + Math.random().toString(36).slice(2);
    }
  }

  // ---------------------------------------------------------------
  // Styles — namespaced under #nativacare-widget-root so nothing here
  // can leak onto or be affected by the host page's own CSS.
  // ---------------------------------------------------------------
  var css = "\n" +
    "#nativacare-widget-root, #nativacare-widget-root * { box-sizing: border-box; }\n" +
    "#nativacare-widget-root {\n" +
    "  position: fixed; bottom: 20px; right: 20px; z-index: 2147483000;\n" +
    "  font-family: 'Inter', -apple-system, 'Segoe UI', sans-serif;\n" +
    "}\n" +
    "#nc-bubble {\n" +
    "  width: 60px; height: 60px; border-radius: 999px; border: none; cursor: pointer;\n" +
    "  background: #765a14; color: #fff; box-shadow: 0 8px 24px rgba(118,90,20,0.35);\n" +
    "  display: flex; align-items: center; justify-content: center;\n" +
    "  transition: transform 0.15s ease;\n" +
    "}\n" +
    "#nc-bubble:hover { transform: scale(1.06); }\n" +
    "#nc-bubble svg { width: 28px; height: 28px; }\n" +
    "#nc-panel {\n" +
    "  position: absolute; bottom: 76px; right: 0; width: 360px; max-width: calc(100vw - 32px);\n" +
    "  height: 520px; max-height: calc(100vh - 120px); background: #F9F8F6;\n" +
    "  border-radius: 20px; box-shadow: 0 16px 48px rgba(0,0,0,0.18); border: 1px solid #E5E7EB;\n" +
    "  display: none; flex-direction: column; overflow: hidden;\n" +
    "}\n" +
    "#nc-panel.open { display: flex; }\n" +
    "#nc-header {\n" +
    "  background: #765a14; color: #fff; padding: 16px 18px; display: flex;\n" +
    "  align-items: center; justify-content: space-between; flex-shrink: 0;\n" +
    "}\n" +
    "#nc-header-title { font-family: 'Plus Jakarta Sans', sans-serif; font-weight: 700; font-size: 15px; }\n" +
    "#nc-header-sub { font-size: 11px; opacity: 0.8; margin-top: 1px; }\n" +
    "#nc-close { background: none; border: none; color: #fff; cursor: pointer; padding: 4px; opacity: 0.85; }\n" +
    "#nc-close:hover { opacity: 1; }\n" +
    "#nc-messages {\n" +
    "  flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 10px;\n" +
    "}\n" +
    ".nc-msg { max-width: 82%; padding: 10px 13px; border-radius: 14px; font-size: 13.5px; line-height: 1.45; white-space: pre-wrap; }\n" +
    ".nc-msg-bot { align-self: flex-start; background: #fff; border: 1px solid #E5E7EB; color: #1a1c1e; border-bottom-left-radius: 4px; }\n" +
    ".nc-msg-user { align-self: flex-end; background: #765a14; color: #fff; border-bottom-right-radius: 4px; }\n" +
    ".nc-typing { align-self: flex-start; display: flex; gap: 4px; padding: 12px 14px; background: #fff; border: 1px solid #E5E7EB; border-radius: 14px; border-bottom-left-radius: 4px; }\n" +
    ".nc-typing span { width: 6px; height: 6px; border-radius: 50%; background: #B59449; animation: nc-bounce 1.2s infinite ease-in-out; }\n" +
    ".nc-typing span:nth-child(2) { animation-delay: 0.15s; }\n" +
    ".nc-typing span:nth-child(3) { animation-delay: 0.3s; }\n" +
    "@keyframes nc-bounce { 0%, 60%, 100% { transform: translateY(0); opacity: 0.5; } 30% { transform: translateY(-4px); opacity: 1; } }\n" +
    "#nc-input-row { display: flex; gap: 8px; padding: 12px; border-top: 1px solid #E5E7EB; background: #fff; flex-shrink: 0; }\n" +
    "#nc-input {\n" +
    "  flex: 1; border: 1px solid #E5E7EB; background: #F3F3F6; border-radius: 999px;\n" +
    "  padding: 10px 14px; font-size: 13.5px; font-family: inherit; outline: none;\n" +
    "}\n" +
    "#nc-input:focus { box-shadow: 0 0 0 2px rgba(118,90,20,0.2); }\n" +
    "#nc-send {\n" +
    "  width: 38px; height: 38px; border-radius: 999px; border: none; background: #765a14; color: #fff;\n" +
    "  cursor: pointer; display: flex; align-items: center; justify-content: center; flex-shrink: 0;\n" +
    "}\n" +
    "#nc-send:disabled { opacity: 0.5; cursor: default; }\n" +
    "#nc-send svg { width: 17px; height: 17px; }\n" +
    "@media (max-width: 480px) {\n" +
    "  #nc-panel { position: fixed; inset: 0; bottom: 0; right: 0; width: 100vw; height: 100vh; max-height: 100vh; max-width: 100vw; border-radius: 0; }\n" +
    "  #nativacare-widget-root { bottom: 16px; right: 16px; }\n" +
    "}\n";

  var styleTag = document.createElement("style");
  styleTag.textContent = css;
  document.head.appendChild(styleTag);

  // ---------------------------------------------------------------
  // Markup
  // ---------------------------------------------------------------
  var root = document.createElement("div");
  root.id = "nativacare-widget-root";
  root.innerHTML =
    '<div id="nc-panel">' +
      '<div id="nc-header">' +
        '<div><div id="nc-header-title">' + escapeHtml(CLINIC_NAME) + '</div>' +
        '<div id="nc-header-sub">Usually replies in a few minutes</div></div>' +
        '<button id="nc-close" aria-label="Close chat">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20"><path d="M18 6L6 18M6 6l12 12"/></svg>' +
        '</button>' +
      '</div>' +
      '<div id="nc-messages"></div>' +
      '<div id="nc-input-row">' +
        '<input id="nc-input" type="text" placeholder="Type a message…" autocomplete="off">' +
        '<button id="nc-send" aria-label="Send">' +
          '<svg viewBox="0 0 24 24" fill="currentColor" width="18" height="18"><path d="M2 21l21-9L2 3v7l15 2-15 2z"/></svg>' +
        '</button>' +
      '</div>' +
    '</div>' +
    '<button id="nc-bubble" aria-label="Open chat">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 11.5a8.38 8.38 0 01-.9 3.8 8.5 8.5 0 01-7.6 4.7 8.38 8.38 0 01-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 01-.9-3.8 8.5 8.5 0 014.7-7.6 8.38 8.38 0 013.8-.9h.5a8.48 8.48 0 018 8v.5z"/></svg>' +
    '</button>';
  document.body.appendChild(root);

  var panel = root.querySelector("#nc-panel");
  var bubble = root.querySelector("#nc-bubble");
  var closeBtn = root.querySelector("#nc-close");
  var messagesEl = root.querySelector("#nc-messages");
  var inputEl = root.querySelector("#nc-input");
  var sendBtn = root.querySelector("#nc-send");

  function escapeHtml(s) {
    var d = document.createElement("div");
    d.textContent = s || "";
    return d.innerHTML;
  }

  function addMessage(text, who) {
    var el = document.createElement("div");
    el.className = "nc-msg nc-msg-" + who;
    el.textContent = text;
    messagesEl.appendChild(el);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return el;
  }

  function showTyping() {
    var el = document.createElement("div");
    el.className = "nc-typing";
    el.id = "nc-typing-indicator";
    el.innerHTML = "<span></span><span></span><span></span>";
    messagesEl.appendChild(el);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function hideTyping() {
    var el = document.getElementById("nc-typing-indicator");
    if (el) el.remove();
  }

  var sessionId = getSessionId();
  var sending = false;
  var lastSeenMessageId = 0;
  var pollTimer = null;

  async function sendMessage(text, opts) {
    opts = opts || {};
    if (!text || sending) return;
    sending = true;
    sendBtn.disabled = true;
    if (!opts.silent) addMessage(text, "user");
    inputEl.value = "";
    showTyping();
    try {
      var res = await fetch(API_BASE.replace(/\/$/, "") + "/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, message: text, source: "website" }),
      });
      var data = await res.json();
      hideTyping();
      if (data.reply) {
        addMessage(data.reply, "bot");
      } else if (!res.ok) {
        addMessage("Sorry, something went wrong. Please try again in a moment.", "bot");
      }
      // human_mode: true means staff have taken over and the bot is
      // intentionally silent — no error, just nothing to show yet. The
      // polling loop (see startPolling below) is what surfaces the
      // staff's actual reply once they type one.
    } catch (e) {
      hideTyping();
      addMessage("Sorry, I couldn't reach the server. Please check your connection and try again.", "bot");
    } finally {
      sending = false;
      sendBtn.disabled = false;
      inputEl.focus();
    }
  }

  // Polls for staff replies sent from the dashboard during human takeover.
  // Only relevant while the panel is open — no point polling a closed
  // widget nobody's looking at. 4s interval balances "feels live enough"
  // against not hammering the server for what's normally an idle chat.
  async function pollForStaffReplies() {
    try {
      var res = await fetch(
        API_BASE.replace(/\/$/, "") + "/chat/" + encodeURIComponent(sessionId) +
        "/poll?after_id=" + lastSeenMessageId
      );
      if (!res.ok) return;
      var data = await res.json();
      (data.messages || []).forEach(function (m) {
        addMessage(m.message, "bot");
        lastSeenMessageId = Math.max(lastSeenMessageId, m.id);
      });
    } catch (e) {
      // Silent — a missed poll just means we check again in 4s. No need
      // to alarm the visitor over a single dropped request.
    }
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(pollForStaffReplies, 4000);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function openPanel() {
    panel.classList.add("open");
    inputEl.focus();
    if (!messagesEl.children.length) {
      // Trigger the bot's real greeting (respects demo-mode banner etc.)
      // without showing the trigger word itself as if the visitor typed it.
      sendMessage("hi", { silent: true });
    }
    startPolling();
  }

  function closePanel() {
    panel.classList.remove("open");
    stopPolling();
  }

  bubble.addEventListener("click", function () {
    if (panel.classList.contains("open")) { closePanel(); } else { openPanel(); }
  });
  closeBtn.addEventListener("click", closePanel);
  sendBtn.addEventListener("click", function () { sendMessage(inputEl.value.trim()); });
  inputEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter") sendMessage(inputEl.value.trim());
  });
})();
