(function () {
  var slides = [].slice.call(document.querySelectorAll(".slide"));
  if (!slides.length) return;
  var q = function (s) { return document.querySelector(s); };
  var bar = q(".progress"), dots = q(".dots"), count = q(".count");
  var prevBtn = q("#prev"), nextBtn = q("#next"), playBtn = q("#play"), themeBtn = q("#theme");
  var cur = 0, timer = null, playing = false;
  var DWELL = 15000; // long enough to read a slide, since nobody is narrating it

  slides.forEach(function (s, i) {
    var d = document.createElement("button");
    d.type = "button";
    d.setAttribute("aria-label", "Slide " + (i + 1));
    d.addEventListener("click", function () { stop(); show(i); });
    dots.appendChild(d);
  });
  var dotEls = [].slice.call(dots.children);

  function show(i, replace) {
    cur = Math.max(0, Math.min(slides.length - 1, i));
    slides.forEach(function (s, k) {
      var on = k === cur;
      s.classList.toggle("cur", on);
      s.setAttribute("aria-hidden", on ? "false" : "true");
    });
    dotEls.forEach(function (d, k) { d.classList.toggle("on", k === cur); });
    count.textContent = cur + 1 + " / " + slides.length;
    bar.style.width = ((cur + 1) / slides.length) * 100 + "%";
    prevBtn.disabled = cur === 0;
    nextBtn.disabled = cur === slides.length - 1 && !playing;
    var id = "#" + slides[cur].id;
    if (location.hash !== id) {
      try { history[replace ? "replaceState" : "pushState"](null, "", id); } catch (e) { }
    }
    // give the browser a head start on the next slide's screenshot
    var nxt = slides[cur + 1] && slides[cur + 1].querySelector("img");
    if (nxt && !nxt.dataset.warm) { nxt.dataset.warm = "1"; var p = new Image(); p.src = nxt.src; }
    if (playing) arm();
  }

  function go(step) { show(cur + step); }

  function arm() {
    clearTimeout(timer);
    timer = setTimeout(function () {
      if (cur >= slides.length - 1) { stop(); return; }
      show(cur + 1);
    }, DWELL);
  }
  function start() {
    playing = true;
    playBtn.textContent = "Pause";
    playBtn.setAttribute("aria-label", "Pause the deck");
    try { localStorage.setItem("hyqs-deck-play", "1"); } catch (e) { }
    arm();
    show(cur);
  }
  function stop() {
    playing = false;
    clearTimeout(timer);
    playBtn.textContent = "Play";
    playBtn.setAttribute("aria-label", "Play the deck on its own");
    try { localStorage.removeItem("hyqs-deck-play"); } catch (e) { }
    nextBtn.disabled = cur === slides.length - 1;
  }

  playBtn.addEventListener("click", function () { playing ? stop() : start(); });
  prevBtn.addEventListener("click", function () { stop(); go(-1); });
  nextBtn.addEventListener("click", function () { stop(); go(1); });
  [].slice.call(document.querySelectorAll(".zone")).forEach(function (z) {
    z.addEventListener("click", function () { stop(); go(z.classList.contains("prev") ? -1 : 1); });
  });

  document.addEventListener("keydown", function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var k = e.key;
    if (k === "ArrowRight" || k === "PageDown" || k === " " || k === "Enter") { e.preventDefault(); stop(); go(1); }
    else if (k === "ArrowLeft" || k === "PageUp") { e.preventDefault(); stop(); go(-1); }
    else if (k === "Home") { e.preventDefault(); stop(); show(0); }
    else if (k === "End") { e.preventDefault(); stop(); show(slides.length - 1); }
    else if (k === "p" || k === "P") { playing ? stop() : start(); }
    else if (k === "Escape") { location.href = "/how-it-works/"; }
  });

  var x0 = null, y0 = null;
  document.addEventListener("touchstart", function (e) {
    x0 = e.touches[0].clientX; y0 = e.touches[0].clientY;
  }, { passive: true });
  document.addEventListener("touchend", function (e) {
    if (x0 === null) return;
    var dx = e.changedTouches[0].clientX - x0, dy = e.changedTouches[0].clientY - y0;
    if (Math.abs(dx) > 55 && Math.abs(dx) > Math.abs(dy)) { stop(); go(dx < 0 ? 1 : -1); }
    x0 = y0 = null;
  }, { passive: true });

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) clearTimeout(timer);
    else if (playing) arm();
  });

  window.addEventListener("popstate", function () {
    var i = slides.findIndex(function (s) { return "#" + s.id === location.hash; });
    if (i >= 0 && i !== cur) { stop(); show(i, true); }
  });

  if (themeBtn) {
    themeBtn.addEventListener("click", function () {
      var root = document.documentElement;
      var dark = root.getAttribute("data-theme")
        ? root.getAttribute("data-theme") === "dark"
        : matchMedia("(prefers-color-scheme: dark)").matches;
      var next = dark ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("hyqs-site-theme", next); } catch (e) { }
    });
  }

  var startAt = slides.findIndex(function (s) { return "#" + s.id === location.hash; });
  show(startAt >= 0 ? startAt : 0, true);
  var wantsPlay = false;
  try { wantsPlay = localStorage.getItem("hyqs-deck-play") === "1"; } catch (e) { }
  if (wantsPlay || /[?&]play\b/.test(location.search)) start();
})();
