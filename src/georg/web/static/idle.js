// SPDX-License-Identifier: AGPL-3.0-only
// Stop-when-idle (docs/deploy-aws.md): keep the host on while someone is listening or
// working in this tab, and warn a minute before the idle watchdog powers it off.
// Polling /idle-status does not count as activity; an untouched tab lets the host stop.
(function () {
  "use strict";
  var KEEPALIVE_MS = 60000, POLL_MS = 30000, WARNING_POLL_MS = 10000, WARN_S = 60;
  var csrf = document.querySelector('meta[name="csrf-token"]');
  if (!csrf || !window.fetch) return;
  var lastInput = 0, lastKeepalive = 0, deadline = null, gone = false, banner, message;

  function playing() {
    return Array.prototype.some.call(document.querySelectorAll("audio"), function (a) {
      return !a.paused && !a.ended;
    });
  }
  function keepalive() {
    if (!gone) hide();
    lastKeepalive = Date.now();
    fetch("/keepalive", { method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": csrf.content } })
      .catch(function () {});
  }
  function hide() {
    deadline = null;
    if (banner) banner.hidden = true;
  }
  function countdown() {
    if (deadline === null || gone) return;
    var left = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
    message.textContent = "Georg will shut down in " + left + " s because nothing is happening.";
  }
  function shutDown() {
    gone = true;
    var link = document.createElement("a");
    link.href = location.href;
    link.textContent = "Reload the page";
    message.textContent = "Georg has shut down. ";
    message.appendChild(link);
    message.appendChild(document.createTextNode(" to start it again."));
    banner.querySelector(".idle-keep").hidden = true;
    banner.hidden = false;
  }
  function poll() {
    var abort = window.AbortController ? new AbortController() : null;
    var timer = abort && setTimeout(function () { abort.abort(); }, 8000);
    fetch("/idle-status", {
      credentials: "same-origin", redirect: "manual", signal: abort && abort.signal,
      headers: { Accept: "application/json" },
    })
      .then(function (r) {
        if (r.type === "opaqueredirect") return null; // logged out: stop quietly
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (s) {
        if (s === null) return;
        if (!s.enabled || s.busy || s.seconds_left === null || s.seconds_left > WARN_S) {
          hide();
        } else {
          deadline = Date.now() + s.seconds_left * 1000;
          countdown();
          banner.hidden = false;
        }
        schedule();
      })
      .catch(function () {
        if (deadline !== null) shutDown(); else schedule();
      })
      .then(function () { if (timer) clearTimeout(timer); });
  }
  function schedule() {
    if (!gone) setTimeout(poll, deadline === null ? POLL_MS : WARNING_POLL_MS);
  }

  document.addEventListener("DOMContentLoaded", function () {
    banner = document.getElementById("idle-banner");
    if (!banner) return;
    message = banner.querySelector(".idle-message");
    banner.querySelector(".idle-keep").addEventListener("click", function () { keepalive(); hide(); });
    ["keydown", "pointerdown", "scroll", "input"].forEach(function (name) {
      document.addEventListener(name, function () { lastInput = Date.now(); }, { capture: true, passive: true });
    });
    setInterval(function () {
      var active = playing() || Date.now() - lastInput < KEEPALIVE_MS;
      if (active && Date.now() - lastKeepalive >= KEEPALIVE_MS) keepalive();
      countdown();
    }, 1000);
    poll();
  });
})();
