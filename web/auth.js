"use strict";
/* 가벼운 로그인 잠금. web/auth.json 에 계정이 있으면 로그인해야 화면이 보인다.
   비밀번호는 PBKDF2 해시로만 저장돼 있다. 정적 사이트라 서버 인증이 아니므로 완전한 보안은 아니다(README 참고).
   관리자(role=admin)만 body.is-admin 이 붙어 .admin-only 영역(Claude 키·캡처 읽기)이 보인다. */
(async function () {
  const $ = (s) => document.querySelector(s);
  const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
  const unhex = (s) => Uint8Array.from(s.match(/../g) || [], (h) => parseInt(h, 16));
  async function derive(pw, saltHex, iterations) {
    const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(pw), "PBKDF2", false, ["deriveBits"]);
    return hex(await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt: unhex(saltHex), iterations }, key, 256));
  }
  const unlock = (user) => {
    document.body.classList.remove("locked");
    document.body.classList.toggle("is-admin", !user || user.role === "admin");   // 계정이 하나도 없으면 잠금 없음
    $("#gate").hidden = true;
    const out = $("#logout");
    if (out) { out.hidden = !user; out.textContent = user ? `${user.id} · 로그아웃` : ""; }
    window.AUTH = user ? { id: user.id, role: user.role } : null;
  };
  let cfg = null;
  try { cfg = await (await fetch(`auth.json?t=${Date.now()}`, { cache: "no-store" })).json(); } catch { /* 잠금 없음 */ }
  const users = cfg?.users || [];
  if (!users.length) return unlock(null);

  const saved = (() => { try { return sessionStorage.getItem("auth_user"); } catch { return null; } })();
  const prev = users.find((u) => u.id === saved);
  if (prev) return unlock(prev);

  $("#gate").hidden = false;
  let fails = 0;
  $("#gateForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const id = $("#gateId").value.trim().toLowerCase(), pw = $("#gatePw").value, msg = $("#gateMsg"), btn = $("#gateBtn");
    btn.disabled = true; msg.textContent = "확인 중…";
    const u = users.find((x) => x.id === id);
    let ok = false;
    try { ok = !!u && (await derive(pw, u.salt, cfg.iterations || 200000)) === u.hash; } catch { msg.textContent = "이 브라우저에서는 로그인을 확인할 수 없습니다 (https 또는 localhost 필요)."; btn.disabled = false; return; }
    if (ok) { try { sessionStorage.setItem("auth_user", u.id); } catch { /* noop */ } $("#gatePw").value = ""; return unlock(u); }
    fails++;
    await new Promise((r) => setTimeout(r, Math.min(5000, 600 * fails)));   // 연속 실패하면 점점 느리게
    msg.textContent = "아이디 또는 비밀번호가 맞지 않습니다."; btn.disabled = false;
  });
  $("#logout")?.addEventListener("click", () => { try { sessionStorage.removeItem("auth_user"); } catch { /* noop */ } location.reload(); });
})();
