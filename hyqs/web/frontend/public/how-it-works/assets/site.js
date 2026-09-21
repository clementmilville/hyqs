(function () {
  // Scroll position: a fresh navigation to a chapter always opens at its top, even
  // when the browser would restore a remembered position. Back and forward still
  // return to where the reader was, restored from sessionStorage by us.
  try {
    if ("scrollRestoration" in history) history.scrollRestoration = "manual";
    const key = "hyqs-scroll:" + location.pathname;
    const navType = (performance.getEntriesByType("navigation")[0] || {}).type || "navigate";
    let userDrove = false;
    ["wheel", "touchstart", "keydown"].forEach((t) =>
      addEventListener(
        t,
        () => {
          userDrove = true;
        },
        { passive: true, once: true }
      )
    );
    const place = () => {
      if (userDrove || location.hash) return;
      if (navType === "back_forward") {
        let y = 0;
        try {
          y = parseInt(sessionStorage.getItem(key) || "0", 10) || 0;
        } catch (_e) {}
        if (y > 0) window.scrollTo(0, y);
      } else if (window.scrollY !== 0) {
        window.scrollTo(0, 0);
      }
    };
    place();
    addEventListener("DOMContentLoaded", place);
    addEventListener("load", place);
    setTimeout(place, 120);
    addEventListener("pagehide", () => {
      try {
        sessionStorage.setItem(key, String(window.scrollY));
      } catch (_e) {}
    });
    addEventListener("pageshow", (e) => {
      if (e.persisted) place();
    });
  } catch (_e) {}
  const body = document.body;
  const q = (s, r) => (r || document).querySelector(s),
    qa = (s, r) => [...(r || document).querySelectorAll(s)];

  // ---- reading mode: skim (default) or deep, remembered per visitor ----
  let mode = "skim";
  try {
    const m = localStorage.getItem("hyqs-site-read");
    if (m === "deep" || m === "skim") mode = m;
  } catch (_e) {}
  const applyMode = () => {
    body.dataset.read = mode;
    qa(".rail .mode").forEach((b) =>
      b.setAttribute("aria-pressed", b.dataset.mode === mode ? "true" : "false")
    );
    qa("section").forEach((sec) => {
      const open = sec.classList.contains("open") || mode === "deep";
      qa("[data-deep]", sec).forEach((el) => {
        el.hidden = !open;
      });
      const t = q(".details-toggle", sec);
      if (t) {
        t.setAttribute("aria-expanded", open ? "true" : "false");
        t.textContent = open ? "Hide the details" : "Show the details";
      }
    });
  };
  qa(".rail .mode").forEach((b) =>
    b.addEventListener("click", () => {
      mode = b.dataset.mode;
      try {
        localStorage.setItem("hyqs-site-read", mode);
      } catch (_e) {}
      applyMode();
    })
  );
  qa(".details-toggle").forEach((t) =>
    t.addEventListener("click", () => {
      const sec = t.closest("section");
      sec.classList.toggle("open");
      applyMode();
    })
  );
  // a hash link to a section opens it
  const openHash = () => {
    if (location.hash) {
      const sec = q(location.hash);
      if (sec && sec.tagName === "SECTION") {
        sec.classList.add("open");
        applyMode();
      }
    }
  };
  window.addEventListener("hashchange", openHash);
  applyMode();
  openHash();

  // ---- theme: light / dark, remembered; default follows the system ----
  const themeBtn = q("#themeBtn");
  if (themeBtn) {
    const root = document.documentElement;
    const effective = () =>
      root.getAttribute("data-theme") ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const paint = () => {
      const e = effective();
      themeBtn.textContent = e === "dark" ? "☀" : "☾";
      themeBtn.title = e === "dark" ? "Switch to light" : "Switch to dark";
    };
    themeBtn.addEventListener("click", () => {
      const next = effective() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try {
        localStorage.setItem("hyqs-site-theme", next);
      } catch (_e) {}
      paint();
    });
    paint();
  }

  // ---- progress bar + reveal ----
  const bar = q(".progress i");
  const onScroll = () => {
    if (!bar) return;
    const h = document.documentElement;
    const max = h.scrollHeight - h.clientHeight;
    bar.style.width = (max > 0 ? Math.min(100, (100 * h.scrollTop) / max) : 0) + "%";
  };
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();
  if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver(
      (es) =>
        es.forEach((e) => {
          if (e.isIntersecting) {
            e.target.classList.add("in");
            io.unobserve(e.target);
          }
        }),
      { rootMargin: "0px 0px -10% 0px" }
    );
    qa("section.reveal").forEach((s) => io.observe(s));
  } else {
    qa("section.reveal").forEach((s) => s.classList.add("in"));
  }

  // ---- in-page nav highlight (hub) ----
  const links = qa(".rail nav a[href^='#']");
  if (links.length) {
    const secs = links.map((a) => q(a.getAttribute("href"))).filter(Boolean);
    const io2 = new IntersectionObserver(
      (es) => {
        es.forEach((e) => {
          if (e.isIntersecting) {
            links.forEach((l) =>
              l.classList.toggle("on", l.getAttribute("href") === "#" + e.target.id)
            );
          }
        });
      },
      { rootMargin: "-40% 0px -55% 0px" }
    );
    secs.forEach((s) => io2.observe(s));
  }

  // ---- screenshot tours ----
  qa("[data-tour]").forEach((t) => {
    const btns = qa(".list button", t),
      img = q("img", t),
      cap = q("figcaption", t);
    const staticSrc = img && img.dataset.static ? img.getAttribute("src") : null;
    const show = (b) => {
      btns.forEach((x) => x.setAttribute("aria-selected", x === b ? "true" : "false"));
      if (img) {
        img.src = staticSrc || b.dataset.shot;
        img.alt = b.dataset.alt || b.textContent;
      }
      cap.innerHTML = b.dataset.cap || "";
    };
    btns.forEach((b) => b.addEventListener("click", () => show(b)));
    if (btns[0]) show(btns[0]);
  });

})();

/* visitor beacon: no cookies, no storage identifier, no IP stored, no canvas or font probing.
   Sends only traits the browser reports on request, so two people on identically
   imaged laptops behind one network are counted separately. */
(function () {
  var read = function (fn, fallback) {
    try {
      var value = fn();
      return value == null ? fallback : value;
    } catch (_e) {
      return fallback;
    }
  };
  var send = function (extra) {
    try {
      var q =
        read(function () {
          return new URLSearchParams(location.search).get("r");
        }, "") || "";
      var opts =
        read(function () {
          return Intl.DateTimeFormat().resolvedOptions();
        }, {}) || {};
      var p = {
        path: read(function () {
          return location.pathname;
        }, "/"),
        ref: q.slice(0, 64),
        lang: read(function () {
          return navigator.language;
        }, ""),
        langs: read(function () {
          return (navigator.languages || []).slice(0, 6).join(",");
        }, ""),
        tz: opts.timeZone || "",
        hour_cycle: opts.hourCycle || "",
        screen_w: read(function () {
          return screen.width;
        }, 0),
        screen_h: read(function () {
          return screen.height;
        }, 0),
        avail_w: read(function () {
          return screen.availWidth;
        }, 0),
        avail_h: read(function () {
          return screen.availHeight;
        }, 0),
        win_w: read(function () {
          return window.innerWidth;
        }, 0),
        win_h: read(function () {
          return window.innerHeight;
        }, 0),
        dpr: read(function () {
          return window.devicePixelRatio;
        }, 1),
        hw: read(function () {
          return navigator.hardwareConcurrency;
        }, 0),
        mem: read(function () {
          return navigator.deviceMemory;
        }, 0),
        touch: read(function () {
          return navigator.maxTouchPoints;
        }, 0),
        scheme: read(function () {
          return matchMedia("(prefers-color-scheme: dark)").matches;
        }, false)
          ? "dark"
          : "light",
        platform: read(function () {
          return (
            (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform
          );
        }, ""),
        referrer: read(function () {
          return document.referrer;
        }, ""),
      };
      if (extra) {
        p.os_version = extra.os_version || "";
        p.arch = extra.arch || "";
      }
      var body = JSON.stringify(p);
      if (navigator.sendBeacon) {
        navigator.sendBeacon("/api/public/page-view", body);
      } else {
        fetch("/api/public/page-view", {
          method: "POST",
          keepalive: true,
          body: body,
        }).catch(function () {});
      }
    } catch (_e) {}
  };
  try {
    var ua = read(function () {
      return navigator.userAgentData;
    }, null);
    if (ua && ua.getHighEntropyValues) {
      ua.getHighEntropyValues(["platformVersion", "architecture", "bitness"])
        .then(function (h) {
          send({
            os_version: String(h.platformVersion || "").slice(0, 24),
            arch: (String(h.architecture || "") + String(h.bitness || "")).slice(0, 24),
          });
        })
        .catch(function () {
          send(null);
        });
    } else {
      send(null);
    }
  } catch (_e) {
    send(null);
  }
})();
