// SPDX-License-Identifier: AGPL-3.0-only
// Boundary controls for the review page's main player (#seek-audio). Its
// data-origin-ms is the recording time of its first sample: 0 for the whole-recording
// preview, the service start for the service_hq fallback.
(function () {
  "use strict";
  function parseHms(text) {
    var m = /^(?:(?:(\d+):)?(\d{1,2}):)?(\d+(?:\.\d+)?)$/.exec(text.trim());
    if (!m) return null;
    return ((+(m[1] || 0)) * 3600 + (+(m[2] || 0)) * 60 + parseFloat(m[3])) * 1000;
  }
  function formatHms(ms) {
    var tenths = Math.max(0, Math.floor(ms / 100));
    var s = Math.floor(tenths / 10);
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    return Math.floor(s / 3600) + ":" + pad(Math.floor(s / 60) % 60) + ":" + pad(s % 60) + "." + (tenths % 10);
  }
  function player() {
    var audio = document.getElementById("seek-audio");
    return audio && { audio: audio, origin: parseInt(audio.dataset.originMs || "0", 10) };
  }
  function positionMs(p) {
    return p.origin + p.audio.currentTime * 1000;
  }
  document.addEventListener("DOMContentLoaded", function () {
    var p = player();
    var readout = document.getElementById("player-position");
    if (!p || !readout) return;
    var update = function () { readout.textContent = formatHms(positionMs(p)); };
    ["timeupdate", "seeked", "loadedmetadata"].forEach(function (e) { p.audio.addEventListener(e, update); });
  });
  // Approve/publish only what is saved: compare every form field against the loaded
  // revision (its defaultValue) on each edit, so changing a field back re-enables them.
  function editableFields(form) {
    return Array.prototype.filter.call(form.elements, function (el) {
      return el.name && "defaultValue" in el && el.type !== "hidden";
    });
  }
  function normalized(text) { return text.replace(/\r\n?/g, "\n"); }
  function trackUnsaved() {
    var form = document.getElementById("review-form");
    if (!form) return;
    var fields = editableFields(form);
    var revert = document.getElementById("revert-form");
    var update = function () {
      var dirty = fields.some(function (el) { return normalized(el.value) !== normalized(el.defaultValue); });
      var note = document.getElementById("unsaved-note");
      if (note) note.hidden = !dirty;
      if (revert) revert.disabled = !dirty;
      document.querySelectorAll("button[data-needs-saved]").forEach(function (button) {
        button.disabled = dirty || button.hasAttribute("data-busy");
      });
    };
    form.addEventListener("input", update);
    form.addEventListener("change", update);
    if (revert) revert.addEventListener("click", function () {
      fields.forEach(function (el) { el.value = el.defaultValue; });
      update();
    });
    document.body.addEventListener("htmx:afterSwap", update);
    update();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", trackUnsaved);
  else trackUnsaved();
  // Media events don't bubble, so capture: one player at a time, including htmx-inserted ones.
  document.addEventListener("play", function (event) {
    document.querySelectorAll("audio, video").forEach(function (media) {
      if (media !== event.target && !media.paused) media.pause();
    });
  }, true);
  document.addEventListener("click", function (event) {
    var button = event.target.closest("button.play-from");
    if (!button) return;
    var p = player();
    var field = document.getElementById(button.dataset.field);
    if (!p || !field) return;
    var ms = parseHms(field.value);
    if (ms === null) return;
    p.audio.currentTime = Math.max(0, (ms - p.origin) / 1000);
    p.audio.play();
    p.audio.scrollIntoView({ block: "nearest" });
  });
})();
