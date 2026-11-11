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
