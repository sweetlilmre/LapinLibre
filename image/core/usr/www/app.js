// SPDX-License-Identifier: MIT
// LapinLibre's web page: talks to the rabbit's API under cgi-bin/ (same origin, the browser's own
// basic-auth login), parses every reply as JSON from its text, and puts every value into the page
// as text, never as HTML. Each section is an object in SECTIONS with an init(), and optionally
// refresh() (loads its own data) and renderState() (shows the API's state from status). Actions
// are sent as POST; each card then shows the GET URL that does the same, for home automation.
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);

  // --- the API ---

  // A request to cgi-bin/<path>: GET for reads, POST (form-encoded) for actions. Resolves to
  // {status, text, data}, where data is the parsed JSON or null.
  async function api(path, params, method) {
    const query = new URLSearchParams(params || {}).toString();
    const options = { method: method || "GET", credentials: "same-origin", cache: "no-store" };
    let url = "cgi-bin/" + path;
    if (options.method === "POST") {
      options.headers = { "Content-Type": "application/x-www-form-urlencoded" };
      options.body = query;
    } else if (query) {
      url += "?" + query;
    }
    const response = await fetch(url, options);
    const text = await response.text();
    let data = null;
    try {
      data = JSON.parse(text);
    } catch (e) {
      data = null;
    }
    return { status: response.status, text: text, data: data };
  }

  // The error a reply carries, in words, or "" when it succeeded.
  function problemOf(reply) {
    if (reply.data && String(reply.data.return) === "0") return "";
    if (reply.data && reply.data.msg) return String(reply.data.msg);
    return "The rabbit answered with HTTP " + reply.status + ".";
  }

  function showProblem(text) {
    const p = $("problem");
    p.textContent = text;
    p.hidden = !text;
  }

  // --- page building, always as text ---

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (key === "text") node.textContent = value;
      else if (key === "className") node.className = value;
      else node.setAttribute(key, value);
    }
    for (const child of children || []) node.appendChild(child);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  // A titled list of [label, value] rows; empty values show as a dash.
  function group(title, rows) {
    const list = el("dl");
    for (const [label, value] of rows) {
      list.appendChild(el("dt", { text: label }));
      const shown = value === undefined || value === null || value === "" ? "–" : String(value);
      list.appendChild(el("dd", { text: shown }));
    }
    return el("div", { className: "group" }, [el("h3", { text: title }), list]);
  }

  function code(text) {
    return el("code", { text: text });
  }

  function paragraph(parts, className) {
    const p = el("p", className ? { className: className } : {});
    for (const part of parts) p.appendChild(typeof part === "string" ? document.createTextNode(part) : part);
    return p;
  }

  // --- formatting ---

  function duration(seconds) {
    const s = Number(seconds) || 0;
    const days = Math.floor(s / 86400);
    const hours = Math.floor((s % 86400) / 3600);
    const minutes = Math.floor((s % 3600) / 60);
    const parts = [];
    if (days) parts.push(days + (days === 1 ? " day" : " days"));
    if (hours || days) parts.push(hours + " h");
    parts.push(minutes + " min");
    return parts.join(" ");
  }

  // A size in kB as MB, or as GB from 1024 MB on.
  function megabytes(kb) {
    const n = Number(kb);
    if (!isFinite(n)) return "";
    const mb = n / 1024;
    return mb >= 1024 ? (mb / 1024).toFixed(1) + " GB" : mb.toFixed(1) + " MB";
  }

  // df gives whole percents, so 0 means under 1%.
  function space(freeKb, usedPercent) {
    const used = String(usedPercent) === "0" ? "<1%" : usedPercent + "%";
    return megabytes(freeKb) + " free (" + used + " used)";
  }

  // --- the status ---

  // LapinLibre's own status, and the whole of the API's status (sleep, LED, ears).
  let lastStatus = null;
  let apiState = null;

  const status = {
    timer: null,
    later: null,
    running: null,

    init() {
      $("status-refresh").addEventListener("click", () => status.refresh());
      // Every 30 s while the page is visible.
      status.timer = setInterval(() => {
        if (document.visibilityState === "visible") status.refresh();
      }, 30000);
    },

    // One status request at a time: a refresh asked for while one runs gets that one's outcome.
    refresh() {
      if (!status.running) {
        status.running = status.load().finally(() => {
          status.running = null;
        });
      }
      return status.running;
    },

    // A refresh in the background a while after the last action, so the rabbit's CPU serves the
    // actions first.
    soon() {
      if (status.later) clearTimeout(status.later);
      status.later = setTimeout(() => {
        status.later = null;
        status.refresh();
      }, 10000);
    },

    async load() {
      let reply;
      try {
        reply = await api("status");
      } catch (e) {
        showProblem("The rabbit doesn't answer.");
        return false;
      }
      const problem = problemOf(reply);
      if (problem) {
        showProblem(problem);
        return false;
      }
      showProblem("");
      apiState = reply.data;
      lastStatus = reply.data.lapinlibre || {};
      status.render(lastStatus);
      renderStates();
      tags.fromRabbit(lastStatus.learning_left);
      return true;
    },

    render(s) {
      const name = s.hostname || "lapinlibre";
      $("subtitle").textContent = name + ".local" + (s.ip ? " – " + s.ip : "");
      $("telnet-warning").hidden = s.telnet !== "1";
      const groups = $("status-groups");
      clear(groups);
      groups.appendChild(group("Rabbit", [
        ["Name", name],
        ["Reach it at", name + ".local" + (s.ip ? " or " + s.ip : "")],
        ["Build", s.build],
        ["Build id", s.build_id],
        ["Web user", s.web_user],
      ]));
      groups.appendChild(group("Network", [
        ["Wi-Fi network", s.wifi_ssid],
        ["Signal (link quality)", s.wifi_signal],
        ["Address", s.address_mode === "fixed" ? "fixed" : "from the router (DHCP)"],
        ["IP address", s.ip],
        ["Netmask", s.netmask],
        ["Gateway", s.gateway],
        ["DNS", s.dns],
        ["MAC address", s.mac],
      ]));
      groups.appendChild(group("System", [
        ["Time", s.time],
        ["Time zone", s.timezone],
        ["Up for", duration(s.uptime)],
        ["Load", s.load],
        ["Memory", megabytes(s.mem_free_kb) + " free of " + megabytes(s.mem_total_kb)],
        ["Flash (/usr)", space(s.usr_free_kb, s.usr_used_percent)],
        ["USB stick", s.usb_mounted === "1" ? space(s.usb_free_kb, s.usb_used_percent) : "not mounted"],
      ]));
      groups.appendChild(group("Servers", [
        ["Language", s.language],
        ["App server", s.app_server],
        ["Speech server", s.speech_server],
        ["Controller", s.controller],
      ]));
      const daemons = el("ul", { className: "daemons" });
      for (const [daemon, up] of Object.entries(s.daemons || {})) {
        daemons.appendChild(el("li", { className: up === "1" ? "up" : "down", text: daemon + (up === "1" ? " running" : " stopped") }));
      }
      groups.appendChild(el("div", { className: "group wide" }, [el("h3", { text: "Daemons" }), daemons]));
      $("status-updated").textContent = "Updated " + new Date().toLocaleTimeString() + ".";
    },
  };

  // --- actions on the rabbit ---

  // The GET URL of an API request, for copying into home automation.
  function getUrl(path, params) {
    const base = location.protocol + "//" + location.host + location.pathname.replace(/[^/]*$/, "");
    const query = new URLSearchParams(params || {}).toString();
    return base + "cgi-bin/" + path + (query ? "?" + query : "");
  }

  // A card's result line, and its box with the last action's URL (made from the template).
  function cardParts(card) {
    let details = card.querySelector(".get-url");
    if (!details) {
      details = $("get-url-template").content.firstElementChild.cloneNode(true);
      details.hidden = true;
      card.insertBefore(details, card.querySelector(".result"));
    }
    return { result: card.querySelector(".result"), details: details };
  }

  function showResult(card, text, kind) {
    const result = cardParts(card).result;
    result.textContent = text;
    result.className = "result" + (kind ? " " + kind : "");
  }

  function showUrl(card, path, params) {
    const details = cardParts(card).details;
    details.querySelector(".url").textContent = getUrl(path, params);
    details.hidden = false;
  }

  // A refusal in plain words: asleep and busy get their own sentences.
  function refusal(msg) {
    if (msg.indexOf("asleep") >= 0 && msg.indexOf("already") < 0) {
      return "The rabbit is asleep, so it takes no LED, ear, sound or speech requests. Wake it up first.";
    }
    if (msg.indexOf("application") >= 0) {
      return "An application is running on the rabbit: it takes no requests from this page until the application ends.";
    }
    if (msg.indexOf("busy") >= 0) return "The rabbit is busy (" + msg + "). Try again in a moment.";
    return msg.charAt(0).toUpperCase() + msg.slice(1) + ".";
  }

  // Shows the API's state (from the last status, with what actions changed since) in each section.
  function renderStates() {
    if (!apiState) return;
    for (const section of SECTIONS) {
      if (section.renderState) section.renderState(apiState);
    }
  }

  // What a successful action changed in the API's state, applied at once from its reply; the
  // status is refreshed in the background later for the rest.
  function applyAction(path, params, data) {
    if (!apiState) return;
    if (path === "leds" && data.no_memory !== "1") {
      apiState.led_color = String(data.color);
      apiState.led_pulse = String(data.pulse);
    } else if (path === "sleep") {
      apiState.sleep = "1";
      apiState.sleep_time = String(Math.floor(Date.now() / 1000));
    } else if (path === "wakeup") {
      apiState.sleep = "0";
      apiState.sleep_time = "0";
      apiState.led_color = "00FF00";
      apiState.led_pulse = "1";
    } else if (path === "ears_mode") {
      apiState.ears_disabled = params.disable === "1" ? "1" : "0";
    }
    renderStates();
  }

  // An action: sends it, shows its GET URL (made from shown instead of params when given, so a
  // password can be left out of it) and its outcome in the card as soon as the rabbit answers,
  // and what it changed; the status follows in the background. Resolves to the reply's data, or
  // null when it failed.
  async function act(cardId, path, params, done, shown) {
    const card = $(cardId);
    showUrl(card, path, shown || params);
    showResult(card, "Working…", "");
    let reply;
    try {
      reply = await api(path, params, "POST");
    } catch (e) {
      showResult(card, "The rabbit doesn't answer.", "problem");
      return null;
    }
    const problem = problemOf(reply);
    if (problem) {
      showResult(card, refusal(problem), "problem");
      return null;
    }
    showResult(card, typeof done === "function" ? done(reply.data) : done || "Done.", "");
    applyAction(path, params || {}, reply.data);
    status.soon();
    return reply.data;
  }

  function hexColor(input) {
    return input.value.replace("#", "").toUpperCase();
  }

  function onClick(id, handler) {
    $(id).addEventListener("click", handler);
  }

  const sleep = {
    init() {
      onClick("sleep-go", () => act("sleep", "sleep", {}, "The rabbit is asleep."));
      onClick("wake-go", () => act("sleep", "wakeup", {}, "The rabbit is awake."));
      onClick("wake-silent", () => act("sleep", "wakeup", { silent: "1" }, "The rabbit is awake."));
    },

    renderState(s) {
      let text = "Awake.";
      if (s.sleep === "1") {
        const since = Number(s.sleep_time);
        text = since > 0 ? "Asleep since " + new Date(since * 1000).toLocaleString() + "." :
          "Asleep (by its own schedule: waking it from here doesn't change that).";
      }
      $("sleep-state").textContent = text;
    },
  };

  // The swatches' colours (the LED shows red, green and blue light mixed). A swatch only fills in the
  // colour; "Set the LED" sends it.
  const SWATCHES = [["Red", "#ff0000"], ["Orange", "#ff8800"], ["Yellow", "#ffff00"], ["Green", "#00ff00"],
    ["Cyan", "#00ffff"], ["Blue", "#0000ff"], ["Purple", "#8800ff"], ["White", "#ffffff"]];

  const led = {
    init() {
      const swatches = $("led-swatches");
      for (const [name, value] of SWATCHES) {
        const button = el("button", { type: "button", className: "swatch", title: name, "aria-label": name });
        button.style.backgroundColor = value;
        button.addEventListener("click", () => {
          $("led-color").value = value;
        });
        swatches.appendChild(button);
      }
      onClick("led-set", () => led.send());
      onClick("led-off", () => act("led", "leds", { color: "000000" }, "The LED is off."));
    },

    send() {
      const params = { color: hexColor($("led-color")) };
      if ($("led-pulse").checked) {
        params.pulse = "1";
        params.color2 = hexColor($("led-color2"));
        params.speed = String(Math.min(10000, Math.max(25, parseInt($("led-speed").value, 10) || 700)));
        $("led-speed").value = params.speed;
      }
      act("led", "leds", params, "The LED is set.");
    },

    renderState(s) {
      const state = $("led-state");
      clear(state);
      const chip = el("span", { className: "chip" });
      chip.style.backgroundColor = "#" + s.led_color;
      state.appendChild(document.createTextNode("Last set through the API: "));
      state.appendChild(chip);
      state.appendChild(document.createTextNode(" #" + s.led_color + (s.led_pulse === "1" ? ", pulsing." : ", fixed.")));
    },
  };

  function earPosition(id) {
    const n = Math.min(16, Math.max(0, parseInt($(id).value, 10) || 0));
    $(id).value = String(n);
    return String(n);
  }

  const ears = {
    disabled: false,

    init() {
      onClick("ears-move", () => {
        const params = { left: earPosition("ears-left"), right: earPosition("ears-right") };
        if (!$("ears-recalibrate").checked) params.noreset = "1";
        act("ears", "ears", params, "The ears are moving.");
      });
      onClick("ears-reset", () => act("ears", "ears_reset", {}, "The ears are recalibrating."));
      onClick("ears-random", () => {
        const params = $("ears-recalibrate").checked ? {} : { noreset: "1" };
        act("ears", "ears_random", params, (d) => "The ears are moving to " + d.left + " and " + d.right + ".");
      });
      onClick("ears-toggle", () => {
        const disable = ears.disabled ? "0" : "1";
        act("ears", "ears_mode", { disable: disable },
          disable === "1" ? "The API's ear moves are off." : "The API's ear moves are on.");
      });
    },

    renderState(s) {
      ears.disabled = s.ears_disabled === "1";
      $("ears-state").textContent = ears.disabled ?
        "The API's ear moves are off: this page and the API don't move the ears." :
        "The API's ear moves are on.";
      $("ears-toggle").textContent = ears.disabled ? "Turn the API's ear moves on" : "Turn the API's ear moves off";
    },
  };

  const sound = {
    cardId: "sound",
    init() {
      onClick("sound-url-play", () => {
        const url = $("sound-url").value.trim();
        if (url) act("sound", "sound", { url: url }, "Playing.");
      });
      for (const button of document.querySelectorAll("#sound [data-cmd]")) {
        button.addEventListener("click", () => act("sound", "sound_control", { cmd: button.dataset.cmd }, "Done."));
      }
    },

    async refresh() {
      let reply;
      try {
        reply = await api("sound_list");
      } catch (e) {
        return;
      }
      const list = $("sound-list");
      clear(list);
      if (problemOf(reply)) return;
      for (const item of reply.data.sounds || []) {
        const button = el("button", { type: "button", text: "Play" });
        button.addEventListener("click", () => act("sound", "sound", { id: item.id }, "Playing " + item.id + "."));
        list.appendChild(el("li", {}, [el("span", { text: item.id }), button]));
      }
      if (!list.firstChild) list.appendChild(el("li", { text: "No sounds found." }));
    },
  };

  const speech = {
    cardId: "speech",
    init() {
      onClick("speech-go", () => {
        const text = $("speech-text").value.trim();
        if (!text) {
          showResult($("speech"), "Type something to say first.", "problem");
          return;
        }
        const params = { text: text };
        if ($("speech-voice").value) params.voice = $("speech-voice").value;
        act("speech", "tts", params, (d) => "Speaking (" + d.voicelanguage + ", " + d.voicegender + ").");
      });
      onClick("speech-clock", () => act("speech", "apps/clock", {}, "Saying the time."));
    },

    async refresh() {
      let reply;
      try {
        reply = await api("voice_list");
      } catch (e) {
        return;
      }
      if (problemOf(reply)) return;
      const select = $("speech-voice");
      const chosen = select.value;
      while (select.options.length > 1) select.remove(1);
      for (const voice of reply.data.voices || []) {
        select.appendChild(el("option", { value: voice.id, text: voice.lang + " (" + voice.id + ")" }));
      }
      select.value = chosen;
      if (select.value !== chosen) select.value = "";
    },
  };

  function snapshotUrl(name) {
    return "cgi-bin/snapshot_get?" + new URLSearchParams({ filename: name }).toString();
  }

  const camera = {
    cardId: "camera",
    liveUrl: null,

    init() {
      onClick("camera-shot", async () => {
        const data = await act("camera", "snapshot", {}, "Picture taken.");
        if (data) camera.refresh();
      });
      onClick("camera-view", () => camera.live());
      onClick("camera-clear", () => {
        $("camera-clear-confirm").hidden = false;
        $("camera-clear").hidden = true;
      });
      onClick("camera-clear-no", () => camera.unconfirm());
      onClick("camera-clear-yes", async () => {
        camera.unconfirm();
        const data = await act("camera", "clear_snapshots", {}, "All pictures are deleted.");
        if (data) camera.refresh();
      });
    },

    unconfirm() {
      $("camera-clear-confirm").hidden = true;
      $("camera-clear").hidden = false;
    },

    // A picture shown here and not stored: the reply is the JPEG itself, or an error as text.
    async live() {
      const card = $("camera");
      showUrl(card, "snapshot_view", {});
      showResult(card, "Taking a picture…", "");
      let response;
      try {
        response = await fetch("cgi-bin/snapshot_view", {
          method: "POST", credentials: "same-origin", cache: "no-store",
          headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: "",
        });
      } catch (e) {
        showResult(card, "The rabbit doesn't answer.", "problem");
        return;
      }
      if ((response.headers.get("Content-Type") || "").indexOf("image/jpeg") === 0) {
        const blob = await response.blob();
        if (camera.liveUrl) URL.revokeObjectURL(camera.liveUrl);
        camera.liveUrl = URL.createObjectURL(blob);
        $("camera-live-img").src = camera.liveUrl;
        $("camera-live").hidden = false;
        showResult(card, "Live picture taken.", "");
        status.soon();
        return;
      }
      let data = null;
      try {
        data = JSON.parse(await response.text());
      } catch (e) {
        data = null;
      }
      const reply = { status: response.status, data: data };
      showResult(card, refusal(problemOf(reply) || "no picture came"), "problem");
    },

    async refresh() {
      let reply;
      try {
        reply = await api("snapshot_list");
      } catch (e) {
        return;
      }
      const gallery = $("camera-gallery");
      clear(gallery);
      if (problemOf(reply)) return;
      const shots = (reply.data.snapshots || []).slice().reverse();
      for (const shot of shots) {
        const name = String(shot.id);
        const label = name.replace(/^snapshot_/, "").replace(/\.jpg$/, "").replace(/_/g, " ");
        const link = el("a", { href: snapshotUrl(name), target: "_blank", rel: "noopener" });
        const thumb = el("img", { src: snapshotUrl(name.replace(/\.jpg$/, ".thumb.gif")), alt: "Picture of " + label, loading: "lazy" });
        // Without a thumbnail, the link says so beside the time.
        thumb.addEventListener("error", () => {
          thumb.remove();
          link.insertBefore(el("span", { className: "no-thumb", text: "(no thumbnail)" }), link.firstChild);
        });
        link.appendChild(thumb);
        link.appendChild(el("span", { className: "when", text: label }));
        gallery.appendChild(el("li", {}, [link, camera.deleter(name, label)]));
      }
      if (!gallery.firstChild) gallery.appendChild(el("li", { className: "note", text: "No pictures stored." }));
    },

    // A picture's bin button, which asks before deleting the picture and its thumbnail.
    deleter(name, label) {
      const bin = el("button", { type: "button", className: "bin", title: "Delete this picture", "aria-label": "Delete the picture of " + label, text: "\u{1F5D1}" });
      const yes = el("button", { type: "button", className: "danger", text: "Delete" });
      const no = el("button", { type: "button", text: "Cancel" });
      const ask = el("div", { className: "confirm" }, [yes, no]);
      ask.hidden = true;
      bin.addEventListener("click", () => {
        bin.hidden = true;
        ask.hidden = false;
      });
      no.addEventListener("click", () => {
        ask.hidden = true;
        bin.hidden = false;
      });
      yes.addEventListener("click", async () => {
        ask.hidden = true;
        bin.hidden = false;
        const data = await act("camera", "lapinlibre/snapshot_delete", { filename: name }, "The picture is deleted.");
        if (data) camera.refresh();
      });
      return el("div", { className: "picture-actions" }, [bin, ask]);
    },
  };

  // --- RFID tags ---

  // The colour codes a tag can be given, by name.
  const TAG_COLORS = [["1", "Red"], ["2", "Blue"], ["3", "Green"], ["4", "Yellow"], ["5", "Pink"], ["6", "Black"],
    ["7", "Grey"], ["8", "Orange"], ["9", "Purple"], ["10", "White"], ["51", "Brown"]];
  const LEARN_SECONDS = 120;

  // Fills a <select> with [value, label] pairs after its first `keep` options.
  function fillSelect(select, pairs, keep) {
    const chosen = select.value;
    while (select.options.length > keep) select.remove(keep);
    for (const [value, label] of pairs) select.appendChild(el("option", { value: value, text: label }));
    select.value = chosen;
    if (select.value !== chosen && select.options.length) select.selectedIndex = 0;
  }

  async function listOf(path, key) {
    try {
      const reply = await api(path);
      if (!problemOf(reply)) return reply.data[key] || [];
    } catch (e) {
      // The list stays empty.
    }
    return [];
  }

  const tags = {
    cardId: "tags",
    current: null,
    learnEnds: 0,
    learnTimer: null,

    init() {
      const colors = $("tags-color");
      colors.appendChild(el("option", { value: "", text: "Unchanged" }));
      for (const [value, name] of TAG_COLORS) colors.appendChild(el("option", { value: value, text: name }));
      onClick("tags-learn", async () => {
        const data = await act("tags", "rfid_start_record", {}, "Learning: lay each new tag on the rabbit's nose.");
        if (data) {
          tags.learning(Date.now() + LEARN_SECONDS * 1000);
          tags.syncLearning();
        }
      });
      onClick("tags-learn-stop", async () => {
        const data = await act("tags", "rfid_stop_record", {}, "Learning is over.");
        if (data) tags.learning(0);
      });
      onClick("tags-refresh", () => tags.refresh());
      $("tags-kind").addEventListener("change", () => tags.showKind());
      onClick("tags-rename", () => tags.rename());
      onClick("tags-assign", () => tags.assign());
      onClick("tags-url-test", () => tags.testUrl());
      onClick("tags-unassign", async () => {
        if (!tags.current) return;
        if (await act("tags", "rfid_unassign", { tag: tags.current.id }, "The tag has no action now.")) tags.refresh();
      });
      onClick("tags-delete", async () => {
        if (!tags.current) return;
        if (await act("tags", "rfid_delete", { tag: tags.current.id }, "The tag is forgotten.")) {
          tags.close();
          tags.refresh();
        }
      });
      onClick("tags-close", () => tags.close());
      onClick("tags-clear", () => {
        $("tags-clear-confirm").hidden = false;
        $("tags-clear").hidden = true;
      });
      onClick("tags-clear-no", () => tags.unconfirm());
      onClick("tags-clear-yes", async () => {
        tags.unconfirm();
        if (await act("tags", "clear_rfid", {}, "Every tag is forgotten.")) {
          tags.close();
          tags.refresh();
        }
      });
    },

    unconfirm() {
      $("tags-clear-confirm").hidden = true;
      $("tags-clear").hidden = false;
    },

    // Learning's countdown (ends at `until`, in ms; 0 when it is off), shown on the card and in the
    // editor. Meanwhile the list is refreshed, and the rabbit asked how long is left, every 3 s.
    learning(until) {
      tags.learnEnds = until;
      if (tags.learnTimer) clearInterval(tags.learnTimer);
      tags.learnTimer = null;
      const tick = () => {
        const left = Math.round((tags.learnEnds - Date.now()) / 1000);
        const on = left > 0;
        const state = $("tags-learning");
        state.textContent = on ? "Learning is on for " + left + " s more: lay each new tag on the nose. " +
          "Tags don't run their actions until learning ends." : "Learning is off.";
        state.classList.toggle("warning", on);
        const inEditor = $("tags-editor-learning");
        inEditor.textContent = on ? "Learning is on for " + left + " s more: this tag won't run its action until it ends. " +
          "Stop learning to try it now." : "";
        inEditor.hidden = !on;
        $("tags-learn").hidden = on;
        $("tags-learn-stop").hidden = !on;
        if (!on && tags.learnTimer) {
          clearInterval(tags.learnTimer);
          tags.learnTimer = null;
          tags.refresh();
        } else if (on && left % 3 === 0) {
          tags.refresh();
          tags.syncLearning();
        }
      };
      if (until) tags.learnTimer = setInterval(tick, 1000);
      tick();
    },

    // The rabbit's own learning time left (seconds, from lapinlibre/status): the countdown follows
    // it, so a reloaded page or another browser shows the same.
    fromRabbit(left) {
      const seconds = parseInt(left, 10) || 0;
      const until = seconds > 0 ? Date.now() + seconds * 1000 : 0;
      if ((until === 0) !== (tags.learnEnds === 0) || Math.abs(until - tags.learnEnds) > 2000) tags.learning(until);
    },

    async syncLearning() {
      try {
        const reply = await api("lapinlibre/status");
        if (!problemOf(reply)) tags.fromRabbit(reply.data.learning_left);
      } catch (e) {
        // The countdown goes on as it was.
      }
    },

    async refresh() {
      const list = $("tags-list");
      let reply;
      try {
        reply = await api("rfid_list_ext");
      } catch (e) {
        return;
      }
      clear(list);
      if (problemOf(reply)) {
        list.appendChild(el("li", { className: "problem", text: problemOf(reply) }));
        return;
      }
      for (const tag of reply.data.tags || []) {
        const action = tag.cmd === "0" ? "no action" : tag.cmd_name === "URL" ? "a URL request" :
          tag.cmd_name === "KAROTZ" ? "a rabbit action" : String(tag.cmd_name);
        const about = el("span", {}, [
          el("strong", { text: tag.name || "(no name)" }), document.createTextNode(" "),
          el("span", { className: "tag-id", text: tag.id }), el("br"),
          document.createTextNode(String(tag.type_name).toLowerCase() + ", " + String(tag.color_name).toLowerCase() + ", " + action),
        ]);
        const button = el("button", { type: "button", text: "Edit" });
        button.addEventListener("click", () => tags.edit(tag));
        list.appendChild(el("li", {}, [about, button]));
      }
      if (!list.firstChild) list.appendChild(el("li", { className: "note", text: "No tags learned yet." }));
      if (tags.current && !(reply.data.tags || []).some((t) => t.id === tags.current.id)) tags.close();
    },

    edit(tag) {
      tags.current = tag;
      $("tags-editor").hidden = false;
      $("tags-editor-title").textContent = (tag.name || "Tag") + " (" + tag.id + ")";
      $("tags-name").value = tag.name || "";
      $("tags-color").value = TAG_COLORS.some(([v]) => v === tag.color) ? tag.color : "";
      $("tags-url-answer").hidden = true;
      tags.learning(tags.learnEnds);
      $("tags-editor").scrollIntoView({ block: "nearest" });
      tags.loadAction(tag.id);
    },

    // Fills the action's inputs with what the rabbit stores for the tag (lapinlibre/tag_action):
    // the endpoint and its settings, or the URL and user (a stored password is never sent back).
    async loadAction(id) {
      $("tags-kind").value = "";
      $("tags-tts-text").value = "";
      $("tags-url").value = "";
      $("tags-url-user").value = "";
      $("tags-url-password").value = "";
      $("tags-url-password-note").hidden = true;
      let reply;
      try {
        reply = await api("lapinlibre/tag_action", { tag: id });
      } catch (e) {
        reply = null;
      }
      if (!tags.current || tags.current.id !== id) return;
      const a = reply && !problemOf(reply) ? reply.data : {};
      let kind = "";
      if (a.cmd === "3") kind = "url";
      else if (a.cmd === "5" && $("tags-kind").querySelector('option[value="' + a.endpoint + '"]')) kind = a.endpoint;
      $("tags-kind").value = kind;
      await tags.showKind();
      if (kind === "url") {
        $("tags-url").value = a.url || "";
        $("tags-url-user").value = a.user || "";
        $("tags-url-password-note").hidden = a.password_set !== "1";
        return;
      }
      const q = new URLSearchParams(a.query || "");
      switch (kind) {
        case "tts":
          $("tags-tts-text").value = q.get("text") || "";
          break;
        case "sound":
          if (q.get("id")) $("tags-sound-id").value = q.get("id");
          break;
        case "leds":
          if (/^[0-9A-Fa-f]{6}$/.test(q.get("color") || "")) $("tags-led-color").value = "#" + q.get("color").toLowerCase();
          $("tags-led-pulse").checked = q.get("pulse") === "1";
          break;
        case "ears":
          $("tags-ears-left").value = q.get("left") || "0";
          $("tags-ears-right").value = q.get("right") || "0";
          break;
        case "apps/moods":
          $("tags-mood-id").value = q.get("id") || "";
          break;
        case "stories":
          if (q.get("id")) $("tags-story-id").value = q.get("id");
          break;
      }
    },

    close() {
      tags.current = null;
      $("tags-editor").hidden = true;
    },

    async rename() {
      if (!tags.current) return;
      const params = { tag: tags.current.id, name: $("tags-name").value.trim() };
      if ($("tags-color").value) params.color = $("tags-color").value;
      if (await act("tags", "rfid_rename", params, "Saved.")) tags.refresh();
    },

    // Shows the inputs of the chosen action, and fills its lists.
    async showKind() {
      const kind = $("tags-kind").value;
      for (const block of document.querySelectorAll("#tags .kind")) block.hidden = block.dataset.kind !== kind;
      if (kind === "sound") {
        const sounds = await listOf("sound_list", "sounds");
        fillSelect($("tags-sound-id"), sounds.map((s) => [s.id, s.id]), 0);
      } else if (kind === "apps/moods") {
        const moods = await listOf("moods_list", "moods");
        fillSelect($("tags-mood-id"), moods.map((m) => [m.id, m.text]), 1);
      } else if (kind === "stories") {
        const stories = await listOf("stories_list", "stories");
        fillSelect($("tags-story-id"), stories.map((s) => [s.id, s.name]), 0);
      }
    },

    // The query of a rabbit action from its inputs, or null when one is missing.
    actionParams(kind) {
      switch (kind) {
        case "tts": {
          const text = $("tags-tts-text").value.trim();
          return text ? { text: text } : null;
        }
        case "sound":
          return $("tags-sound-id").value ? { id: $("tags-sound-id").value } : null;
        case "sound_control":
          return { cmd: "stop" };
        case "leds":
          return $("tags-led-pulse").checked ? { color: hexColor($("tags-led-color")), pulse: "1" } :
            { color: hexColor($("tags-led-color")) };
        case "ears":
          return { left: earPosition("tags-ears-left"), right: earPosition("tags-ears-right") };
        case "apps/moods":
          return $("tags-mood-id").value ? { id: $("tags-mood-id").value } : {};
        case "stories":
          return $("tags-story-id").value ? { id: $("tags-story-id").value } : null;
        default:
          return {};
      }
    },

    urlParams() {
      const params = { url: $("tags-url").value.trim() };
      const user = $("tags-url-user").value;
      const password = $("tags-url-password").value;
      if (user || password) {
        params.user = user;
        params.password = password;
      }
      return params;
    },

    // The parameters as shown in the action's URL: never the password itself.
    shownUrlParams(params) {
      const shown = Object.assign({}, params);
      if (shown.password) shown.password = "<password>";
      return shown;
    },

    async assign() {
      if (!tags.current) return;
      const kind = $("tags-kind").value;
      const card = $("tags");
      if (!kind) {
        showResult(card, "Choose an action first.", "problem");
        return;
      }
      if (kind === "url") {
        const params = Object.assign({ tag: tags.current.id }, tags.urlParams());
        if (!params.url) {
          showResult(card, "Type the URL first.", "problem");
          return;
        }
        if (await act("tags", "rfid_assign_url", params, "The tag sends this request now.", tags.shownUrlParams(params))) tags.refresh();
        return;
      }
      const query = tags.actionParams(kind);
      if (!query) {
        showResult(card, "Fill in the action's details first.", "problem");
        return;
      }
      const text = new URLSearchParams(query).toString();
      const target = "/cgi-bin/" + kind + (text ? "?" + text : "");
      if (await act("tags", "rfid_assign_karotz", { tag: tags.current.id, url: btoa(target) }, "The tag runs this action now.")) tags.refresh();
    },

    async testUrl() {
      const card = $("tags");
      const params = tags.urlParams();
      if (!params.url) {
        showResult(card, "Type the URL first.", "problem");
        return;
      }
      if (tags.current) params.tag = tags.current.id;
      const answer = $("tags-url-answer");
      const data = await act("tags", "rfid_test_url", params, "The server answered.", tags.shownUrlParams(params));
      answer.textContent = data ? String(data.msg || "") || "(an empty answer)" : "";
      answer.hidden = !data;
    },
  };

  // --- radio, moods and stories from the USB key ---

  const stick = {
    cardId: "stick",
    init() {
      onClick("stick-refresh", () => stick.refresh());
      onClick("mood-random", () => act("stick", "apps/moods", {}, (d) => "Playing the mood " + d.moods + "."));
    },

    // A list of items, each with its label and a play button.
    fill(list, items, label, play) {
      clear(list);
      for (const item of items) {
        const button = el("button", { type: "button", text: "Play" });
        button.addEventListener("click", () => play(item));
        list.appendChild(el("li", {}, [el("span", { text: label(item) }), button]));
      }
      if (!list.firstChild) list.appendChild(el("li", { className: "note", text: "None found." }));
    },

    async refresh() {
      const [radios, moods, stories] = await Promise.all([listOf("radios_list", "streams"),
        listOf("moods_list", "moods"), listOf("stories_list", "stories")]);
      stick.fill($("radio-list"), radios, (r) => r.name || r.url,
        (r) => act("stick", "sound", { url: r.url }, "Playing " + (r.name || r.url) + "."));
      stick.fill($("mood-list"), moods, (m) => m.text,
        (m) => act("stick", "apps/moods", { id: m.id }, "Playing the mood " + m.text + "."));
      stick.fill($("story-list"), stories, (s) => s.name,
        (s) => act("stick", "stories", { id: s.id }, "Playing the story " + s.name + "."));
      $("mood-random").hidden = moods.length === 0;
      $("stick-help").hidden = radios.length > 0 && moods.length > 0 && stories.length > 0;
    },
  };

  // --- the logs ---

  const logs = {
    cardId: "logs",
    init() {
      $("logs-refresh").addEventListener("click", () => logs.refresh());
      $("logs-source").addEventListener("change", () => logs.refresh());
      $("logs-save").addEventListener("click", () => logs.save());
    },

    async refresh() {
      const lines = Math.min(1000, Math.max(1, parseInt($("logs-lines").value, 10) || 200));
      $("logs-lines").value = String(lines);
      let reply;
      try {
        reply = await api("lapinlibre/logs", { source: $("logs-source").value, lines: String(lines) });
      } catch (e) {
        $("logs-text").textContent = "The rabbit doesn't answer.";
        return;
      }
      // The log comes as plain text; an error comes as JSON.
      if (reply.status !== 200 || (reply.data && reply.data.return === "1")) {
        $("logs-text").textContent = problemOf(reply);
        return;
      }
      $("logs-text").textContent = reply.text || "(the log is empty)";
      $("logs-text").scrollTop = $("logs-text").scrollHeight;
    },

    save() {
      const text = $("logs-text").textContent;
      const stamp = new Date().toISOString().replace(/[:.]/g, "-");
      const name = ((lastStatus && lastStatus.hostname) || "lapinlibre") + "-" + $("logs-source").value + "-" + stamp + ".log";
      const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
      const link = el("a", { href: url, download: name });
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    },
  };

  // --- SSH help ---

  const ssh = {
    cardId: "ssh",
    init() {},

    async refresh() {
      const body = $("ssh-body");
      let reply;
      try {
        reply = await api("lapinlibre/ssh");
      } catch (e) {
        return;
      }
      clear(body);
      const problem = problemOf(reply);
      if (problem) {
        body.appendChild(paragraph([problem], "problem"));
        return;
      }
      const s = reply.data;
      const name = (s.hostname || "lapinlibre") + ".local";
      if (s.enabled === "1") {
        body.appendChild(paragraph(["SSH logins are possible with the key below. There is no password login."]));
      } else {
        body.appendChild(paragraph(["SSH logins aren't possible yet: no key is installed. There is no password login."], "warning"));
      }
      body.appendChild(el("h3", { text: "Connect" }));
      const commands = el("ul", { className: "commands" });
      commands.appendChild(el("li", {}, [code("ssh root@" + name)]));
      if (s.ip) commands.appendChild(el("li", {}, [code("ssh root@" + s.ip)]));
      body.appendChild(commands);
      body.appendChild(paragraph(["Both users, ", code((s.users || []).join(" and ")), ", log in with the same key."]));

      body.appendChild(el("h3", { text: "Installed keys" }));
      if ((s.keys || []).length === 0) {
        body.appendChild(paragraph(["None."]));
      } else {
        const list = el("ul", { className: "keys" });
        for (const key of s.keys) {
          list.appendChild(el("li", {}, [
            el("span", { className: "key-type", text: key.type }),
            document.createTextNode(" "),
            el("span", { text: key.comment || "(no comment)" }),
            el("br"),
            code(key.fingerprint || "fingerprint unknown"),
          ]));
        }
        body.appendChild(list);
      }

      body.appendChild(el("h3", { text: "The rabbit's host keys" }));
      if ((s.host_keys || []).length === 0) {
        body.appendChild(paragraph(["Not made yet: the rabbit makes them at the first SSH login."]));
      } else {
        body.appendChild(paragraph(["On your first connection, check that SSH shows one of these fingerprints:"]));
        const list = el("ul", { className: "keys" });
        for (const key of s.host_keys) {
          list.appendChild(el("li", {}, [el("span", { className: "key-type", text: key.type }), el("br"), code(key.fingerprint || "fingerprint unknown")]));
        }
        body.appendChild(list);
      }

      body.appendChild(el("h3", { text: "Add or change the key" }));
      body.appendChild(paragraph(["Put your public key, all on one line, in ", code("SSH_PUBKEY="), " in ",
        code("lapinlibre.txt"), " on the LapinLibre USB key, then put the key in the rabbit and switch it on: the install applies it."]));
      if (s.telnet === "1") {
        body.appendChild(paragraph(["Telnet is on as well: it needs no password at all. Turn it off with ", code("TELNET=no"), "."], "warning"));
      }
    },
  };

  // --- restart ---

  const restart = {
    init() {
      $("reboot").addEventListener("click", () => {
        $("reboot-confirm").hidden = false;
        $("reboot").hidden = true;
      });
      $("reboot-no").addEventListener("click", () => restart.reset());
      $("reboot-yes").addEventListener("click", () => restart.go());
    },

    reset() {
      $("reboot-confirm").hidden = true;
      $("reboot").hidden = false;
    },

    async go() {
      $("reboot-confirm").hidden = true;
      const state = $("reboot-state");
      let reply;
      try {
        reply = await api("reboot", {}, "POST");
      } catch (e) {
        state.textContent = "The rabbit doesn't answer.";
        restart.reset();
        return;
      }
      const problem = problemOf(reply);
      if (problem) {
        state.textContent = problem;
        restart.reset();
        return;
      }
      state.textContent = "The rabbit is restarting…";
      // It goes down about 10 s after answering; then wait for it to answer again.
      const started = Date.now();
      await new Promise((resolve) => setTimeout(resolve, 20000));
      for (;;) {
        let back = false;
        try {
          const r = await api("lapinlibre/status");
          back = r.status === 200 && r.data && r.data.return === "0";
        } catch (e) {
          back = false;
        }
        if (back) break;
        if (Date.now() - started > 300000) {
          state.textContent = "The rabbit hasn't answered for five minutes. Check its nose light and its network.";
          restart.reset();
          return;
        }
        state.textContent = "The rabbit is restarting… (" + Math.round((Date.now() - started) / 1000) + " s)";
        await new Promise((resolve) => setTimeout(resolve, 5000));
      }
      state.textContent = "The rabbit is back.";
      restart.reset();
      refreshAll();
    },
  };

  const SECTIONS = [status, sleep, led, ears, sound, speech, camera, tags, stick, logs, ssh, restart];

  // The sections' own lists, each loaded the first time its section comes into view (or all at
  // once, side by side, where the browser can't tell), so opening the page asks the rabbit only
  // for the status first.
  let watcher = null;
  const seen = new Set();

  function watchSections() {
    if (watcher) watcher.disconnect();
    seen.clear();
    const lists = SECTIONS.filter((s) => s !== status && s.refresh);
    if (!("IntersectionObserver" in window)) {
      for (const section of lists) section.refresh();
      return;
    }
    const byCard = new Map();
    watcher = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        const section = byCard.get(entry.target);
        if (!entry.isIntersecting || !section || seen.has(section)) continue;
        seen.add(section);
        watcher.unobserve(entry.target);
        section.refresh();
      }
    }, { rootMargin: "200px 0px" });
    for (const section of lists) {
      const card = $(section.cardId);
      if (!card) {
        section.refresh();
        continue;
      }
      byCard.set(card, section);
      watcher.observe(card);
    }
  }

  async function refreshAll() {
    const ok = await status.refresh();
    if (!ok) return;
    $("sections").hidden = false;
    watchSections();
  }

  for (const section of SECTIONS) section.init();
  refreshAll();
})();
