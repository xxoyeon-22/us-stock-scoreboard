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
  // 가입 신청: 브라우저에서 소금·해시를 만들어 ‘신청 코드’로 보여 준다. 관리자가 승인(tools/users.py approve)해야 계정이 생긴다.
  const showSign = (on) => { $("#gateForm").hidden = on; $("#signForm").hidden = !on; };
  $("#gateToSign").addEventListener("click", () => showSign(true));
  $("#signBack").addEventListener("click", () => showSign(false));
  $("#signForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const id = $("#signId").value.trim().toLowerCase(), pw = $("#signPw").value, msg = $("#signMsg");
    if (!/^[a-z0-9_.-]{2,32}$/.test(id)) { msg.textContent = "아이디는 영문 소문자·숫자·_.- 2~32자로 만드세요."; return; }
    if (users.some((u) => u.id === id)) { msg.textContent = "이미 있는 아이디입니다."; return; }
    if (pw.length < 8) { msg.textContent = "비밀번호는 8자 이상으로 만드세요."; return; }
    if (pw !== $("#signPw2").value) { msg.textContent = "비밀번호 확인이 다릅니다."; return; }
    $("#signBtn").disabled = true; msg.textContent = "만드는 중…";
    try {
      const iterations = cfg.iterations || 200000, salt = hex(crypto.getRandomValues(new Uint8Array(16)));
      const code = btoa(JSON.stringify({ v: 1, id, salt, hash: await derive(pw, salt, iterations), iterations }));
      $("#signCode").value = code; $("#signCode").hidden = false; $("#signCopy").hidden = false; $("#signKakao").hidden = false;
      $("#signPw").value = ""; $("#signPw2").value = "";
      msg.textContent = "아래 노란 버튼을 누르면 코드가 복사되고 오픈채팅이 열립니다. 채팅에 붙여 넣어 보내면 관리자가 승인하고, 승인되면 이 아이디와 비밀번호로 로그인할 수 있습니다.";
    } catch { msg.textContent = "코드를 만들지 못했습니다 (https 또는 localhost 필요)."; }
    $("#signBtn").disabled = false;
  });
  $("#signCopy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("#signCode").value); $("#signMsg").textContent = "복사했습니다. 관리자에게 보내세요."; }
    catch { $("#signCode").select(); $("#signMsg").textContent = "코드를 선택해 직접 복사하세요."; }
  });
  $("#signKakao").addEventListener("click", () => {      // 링크는 그대로 열리고, 그 전에 코드를 클립보드에 복사한다
    try { navigator.clipboard.writeText($("#signCode").value); $("#signMsg").textContent = "코드를 복사했습니다. 오픈채팅에 붙여 넣어 보내세요."; } catch { /* 직접 복사 */ }
  });
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
