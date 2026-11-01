// SPDX-License-Identifier: AGPL-3.0-only
// "Play from here": seek the service rendition to a boundary field's time.
// The rendition starts at its own service start (data-origin-ms on the source timeline).
(function () {
  "use strict";
  function parseHms(text) {
    var m = /^(?:(?:(\d+):)?(\d{1,2}):)?(\d+(?:\.\d+)?)$/.exec(text.trim());
    if (!m) return null;
    return ((+(m[1] || 0)) * 3600 + (+(m[2] || 0)) * 60 + parseFloat(m[3])) * 1000;
  }
  document.addEventListener("click", function (event) {
    var button = event.target.closest("button.play-from");
    if (!button) return;
    var audio = document.getElementById("seek-audio");
    var field = document.getElementById(button.dataset.field);
    var ms = field && parseHms(field.value);
    if (!audio || ms === null) return;
    var origin = parseInt(audio.dataset.originMs || "0", 10);
    audio.currentTime = Math.max(0, (ms - origin) / 1000);
    audio.play();
    audio.scrollIntoView({ block: "nearest" });
  });
})();
