#!/usr/bin/env node
/**
 * Check that the Council keeps its state across a catalogue refresh and a dropped stream (UIV2-15).
 *
 *   node scripts/council_state_walk.mjs --base-url http://127.0.0.1:PORT --token TOKEN \
 *     --screenshots docs/media/uiv2-v15-states
 *
 * The pane had three ways to disagree with the server without saying so:
 *
 *   * a catalogue poll rebuilt every model picker, and a picker whose id was not in the new read
 *     fell through to the catalogue default - so the next dispatch ran a model nobody picked;
 *   * a stream could end with no `summon_done` and no `error` (a sleeping laptop, a proxy that
 *     reaps an idle socket), and the "Connecting..." card stayed up for ever while nothing said
 *     the reply had not been saved;
 *   * `selectHallSession()` returned on a failed read without a word, a closed session accepted a
 *     summon, and the composer could write into a session that no longer existed.
 *
 * What it proves, against a running instance:
 *
 *   1. a picked seat id survives a catalogue read that drops it, and the picker says so once;
 *   2. the same holds for a picker the Council does not own (`chatModel`) - where the silent
 *      substitution actually happened;
 *   3. the Council seat pickers are left alone while a dispatch holds them;
 *   4. choosing a role does not replace the model that was picked;
 *   5. a closed session cannot be dispatched into from the pane, and the reason is on the control;
 *   6. the closed notice offers the write that reopens it;
 *   7. a Human message reopens the session, keeps the pinned conclusion, and says both;
 *   8. a stream this browser drops is reconciled: the pane says the seat's reply was not saved,
 *      names the model and the number of characters that arrived, and draws them as unsaved;
 *   9. a session deleted outside the pane is named as gone, and its transcript is kept;
 *  10. a seat the server says is still answering is stated as running, polled, and the poll can be
 *      stopped;
 *  11. no console error is raised by any of it.
 *
 * Auth: the shell keeps its token in `sessionStorage` for its own origin, seeded before the first
 * document exactly as `scripts/capture_screenshots.mjs` does. Seeding it anywhere else (or in
 * `localStorage`) leaves every read at 401, and the 401 path is not what this walk is measuring.
 */
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";

const DEFAULTS = {
  baseUrl: "",
  token: "",
  tokenKey: "m5_api_token",
  theme: "dark",
  settleMs: 250,
  timeoutMs: 120000,
  screenshots: "",
  viewport: "1440x900",
};

const REQUEST = "Summarise the tradeoffs of running the studio fully offline.";
const CONCLUSION = "Offline is cheaper and slower: pin one model, keep the runtime local, and accept a smaller context.";

function parseArgs(argv) {
  const args = { ...DEFAULTS, json: false, captureOnly: false };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = () => {
      const value = argv[i + 1];
      if (value === undefined) throw new Error(`${arg} requires a value`);
      i += 1;
      return value;
    };
    switch (arg) {
      case "--base-url": args.baseUrl = next(); break;
      case "--token": args.token = next(); break;
      case "--theme": args.theme = next(); break;
      case "--settle-ms": args.settleMs = Number(next()); break;
      case "--timeout-ms": args.timeoutMs = Number(next()); break;
      case "--screenshots": args.screenshots = next(); break;
      case "--viewport": args.viewport = next(); break;
      case "--json": args.json = true; break;
      case "--capture-only": args.captureOnly = true; break;
      case "--help":
      case "-h":
        console.log(usage());
        process.exit(0);
        break;
      default:
        throw new Error(`unknown argument: ${arg}`);
    }
  }
  if (!args.baseUrl) throw new Error("--base-url is required (start one with `python3 scripts/demo_tour.py --serve`)");
  return args;
}

function usage() {
  return [
    "usage: node scripts/council_state_walk.mjs --base-url URL [--token TOKEN]",
    "",
    "  --screenshots DIR   write the state screenshots (closed, reopened, lost turn, gone, running)",
    "  --theme light|dark  boot the shell in that theme",
    "  --viewport WxH      CSS pixels for the shots (default 1440x900)",
    "  --capture-only      take the shots and report the steps without failing on them (for a baseline)",
    "  --json              print one JSON object instead of the step log",
  ].join("\n");
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

function resolveChrome() {
  const candidates = [process.env.STUDIO_SCREENSHOT_CHROME, "/tmp/m5b/tmp/chromium", "/tmp/chromium", "/usr/bin/chromium", "/usr/bin/google-chrome"];
  return candidates.filter(Boolean)[0];
}

async function loadPuppeteer() {
  const failures = [];
  const candidates = [
    process.env.STUDIO_PUPPETEER_CORE,
    "puppeteer-core",
    resolve(process.cwd(), "node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js"),
    resolve(process.cwd(), "tests/browser/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js"),
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      const module = await import(candidate.startsWith("/") ? `file://${candidate}` : candidate);
      return module.default || module;
    } catch (error) {
      failures.push(`${candidate}: ${error.message}`);
    }
  }
  throw new Error(`puppeteer-core is not importable. Install it or set STUDIO_PUPPETEER_CORE.\n${failures.join("\n")}`);
}

async function seedAuth(page, args, origin) {
  await page.evaluateOnNewDocument(
    (key, token, own) => {
      if (window.top === window && location.origin === own) window.sessionStorage.setItem(key, token);
    },
    args.tokenKey,
    args.token,
    origin,
  );
  await page.evaluateOnNewDocument(
    (theme, own) => {
      if (window.top !== window || location.origin !== own) return;
      window.localStorage.setItem("m5_theme", theme);
    },
    args.theme,
    origin,
  );
}

async function openHall(page, args, origin) {
  await seedAuth(page, args, origin);
  await page.goto(origin, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
  await sleep(args.settleMs * 10);
  await page.evaluate(() => switchTab("hall"));
  await sleep(args.settleMs * 8);
}

const createSession = (page, title) =>
  page.evaluate(async (label, request) => {
    const response = await fetch("/api/hall/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request, title: label }),
    });
    return (await response.json()).id;
  }, title, REQUEST);

const closeSession = (page, id, conclusion) =>
  page.evaluate(async (sessionId, text) => {
    const response = await fetch(`/api/hall/sessions/${encodeURIComponent(sessionId)}/close`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conclusion: text, author: "Lead Arbiter (demo)" }),
    });
    return (await response.json()).status;
  }, id, conclusion);

async function run(args) {
  const puppeteer = await loadPuppeteer();
  const results = [];
  const record = (step, passed, detail) => {
    const ok = Boolean(passed) || args.captureOnly;
    results.push({ step, passed: Boolean(passed), detail: detail || "", status: ok ? "pass" : "fail" });
    if (!args.json) console.log(`${ok ? "pass" : "FAIL"}  ${step}${detail ? ` — ${detail}` : ""}`);
  };
  const [width, height] = args.viewport.split("x").map(Number);
  const shots = [];
  const shoot = async (page, name, focus = "#hallSummonState", block = "start") => {
    if (!args.screenshots) return "";
    mkdirSync(args.screenshots, { recursive: true });
    const path = resolve(args.screenshots, `${name}.png`);
    // The evidence is the state the pane reports, and that report sits above the transcript:
    // a shot of whatever the pane happened to be scrolled to proves nothing.
    await page.evaluate((selector, align) => {
      const host = document.querySelector(selector);
      if (host && host.scrollIntoView) host.scrollIntoView({ block: align, inline: "nearest" });
    }, focus, block);
    await sleep(args.settleMs);
    await page.screenshot({ path });
    shots.push(path);
    return path;
  };

  const origin = new URL(args.baseUrl).origin;
  const browser = await puppeteer.launch({
    executablePath: resolveChrome(),
    args: (process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "--no-zygote --single-process --disable-gpu --no-sandbox --disable-dev-shm-usage").split(" "),
  });
  const pageErrors = [];
  try {
    const page = await browser.newPage();
    page.on("pageerror", (error) => pageErrors.push(String(error)));
    page.on("console", (message) => { if (message.type() === "error") pageErrors.push(message.text()); });
    await page.setViewport({ width, height, deviceScaleFactor: 1 });
    await openHall(page, args, origin);

    // Each run starts from an empty chronicle: the states below are created here, and a list
    // carrying yesterday's sessions makes the pane in the screenshot say something else.
    await page.evaluate(async () => {
      await fetch("/api/hall/sessions", { method: "DELETE" });
      if (typeof refreshHallSessions === "function") await refreshHallSessions();
    });
    await sleep(args.settleMs * 4);

    // ── 1-4: the picker holds what it was given ────────────────────────────────
    const seeded = await page.evaluate(() => {
      const ids = [...document.querySelectorAll("#hallSeatCoder option")].map((option) => option.value);
      return { ids, chat: document.getElementById("chatModel") ? document.getElementById("chatModel").value : "" };
    });
    const seatPick = seeded.ids.find((id) => id && id !== "__custom__") || "";
    record("the Council pane boots with a pickable seat catalogue", Boolean(seatPick), `${seeded.ids.length} options`);

    const drift = await page.evaluate((picked) => {
      const seat = document.getElementById("hallSeatCoder");
      const chat = document.getElementById("chatModel");
      seat.value = picked;
      if (chat) chat.value = picked;
      const catalog = [{ id: "a-model-the-read-now-prefers", label: "A model the read now prefers", group: "Local", kind: "local" }];
      syncUnifiedSelectors(catalog, catalog, []);
      return {
        seat: seat.value,
        chat: chat ? chat.value : picked,
        unavailable: [...seat.options].some((option) => option.textContent.startsWith("[Unavailable]")),
      };
    }, seatPick);
    record("a seat id survives a catalogue read that no longer carries it", drift.seat === seatPick && drift.unavailable, `kept ${drift.seat}`);
    record("a picker the Council does not own keeps its id too", drift.chat === seatPick, `chatModel kept ${drift.chat}`);

    const frozen = await page.evaluate((picked) => {
      const seat = document.getElementById("hallSeatCoder");
      seat.value = picked;
      councilRoundInProgress = true;
      syncUnifiedSelectors([{ id: "other", label: "Other", group: "Local", kind: "local" }], [{ id: "other", label: "Other", group: "Local", kind: "local" }], []);
      const during = seat.value;
      councilRoundInProgress = false;
      return { before: picked, during };
    }, seatPick);
    record("a dispatch keeps the seat pickers it owns", frozen.during === frozen.before, `${frozen.before} → ${frozen.during}`);

    // The two experiments above replaced the in-memory catalogue on purpose; put the pane's own
    // back before the next step, or the role test would be measuring this walk's leftovers.
    await page.evaluate(async () => { await refreshTelemetry(); });
    await sleep(args.settleMs * 6);
    const roleKeep = await page.evaluate((picked) => {
      const model = document.getElementById("hallModelSelect");
      const role = document.getElementById("hallRoleSelect");
      model.value = picked;
      role.value = "critic";
      role.dispatchEvent(new Event("change"));
      return { picked, after: model.value };
    }, seatPick);
    record("choosing a role keeps the model that was picked", roleKeep.after === roleKeep.picked, `${roleKeep.picked} → ${roleKeep.after}`);

    // ── 5-7: a closed session, and the message that reopens it ─────────────────
    const closedId = await createSession(page, "UIV2-15 closed room");
    await closeSession(page, closedId, CONCLUSION);
    await page.evaluate((id) => selectHallSession(id), closedId);
    await sleep(args.settleMs * 8);
    const closed = await page.evaluate(() => ({
      badge: (document.getElementById("hallStatusBadge") || {}).textContent || "",
      roundDisabled: document.getElementById("hallFullRoundBtn").disabled,
      roundTitle: document.getElementById("hallFullRoundBtn").title,
      singleDisabled: document.getElementById("hallSummonBtn").disabled,
      sendDisabled: document.getElementById("hallSendBtn").disabled,
    }));
    record(
      "a closed session cannot be dispatched into, and the control says why",
      closed.roundDisabled && /closed/i.test(closed.roundTitle) && closed.singleDisabled,
      `${closed.roundTitle}`,
    );
    const noteOnly = await page.evaluate(() => {
      const mode = document.getElementById("hallSendModeSelect");
      mode.value = "post_only";
      mode.dispatchEvent(new Event("change"));
      return { sendDisabled: document.getElementById("hallSendBtn").disabled };
    });
    record(
      "the composer's note-only write stays available on a closed session, because it is what reopens it",
      noteOnly.sendDisabled === false,
      `post_only send disabled=${noteOnly.sendDisabled}`,
    );
    const notice = await page.evaluate(async () => {
      const model = document.getElementById("hallSeatJudge").value || document.getElementById("hallModelSelect").value;
      const ran = await summonAgentPromise("judge", model, "off", false);
      return { ran, notice: window.M5CouncilState ? window.M5CouncilState.notice() : null };
    });
    record(
      "pressing a seat on a closed session explains the 409 instead of sending it",
      notice.ran === false && notice.notice && /closed/i.test(notice.notice.title) && /reopen/i.test(notice.notice.context),
      notice.notice ? notice.notice.title : "no notice",
    );
    record(
      "the closed notice offers the write that reopens the session",
      Boolean(notice.notice && notice.notice.actions.some((label) => /reopen/i.test(label))),
      notice.notice ? notice.notice.actions.join(", ") : "",
    );
    await shoot(page, `closed-session-${args.viewport}-${args.theme}`);

    const reopened = await page.evaluate(async () => {
      document.getElementById("hallHumanInput").value = "One more round, then re-pin the verdict.";
      document.getElementById("hallSendModeSelect").value = "post_only";
      await postHumanHallMessage(false);
      const session = await fetch(`/api/hall/sessions/${encodeURIComponent(activeHallId)}`).then((response) => response.json());
      return {
        status: session.status,
        conclusion: session.conclusion_text || "",
        notice: window.M5CouncilState ? window.M5CouncilState.notice() : null,
      };
    });
    record(
      "a Human message reopens the session and keeps the pinned conclusion",
      reopened.status === "active" && reopened.conclusion.length > 20,
      `status=${reopened.status} · conclusion=${reopened.conclusion.length} chars`,
    );
    record(
      "the reopen is reported, and says the verdict survived it",
      Boolean(reopened.notice && /open again/i.test(reopened.notice.title) && /kept/i.test(reopened.notice.context)),
      reopened.notice ? reopened.notice.title : "no notice",
    );
    await shoot(page, `reopened-session-${args.viewport}-${args.theme}`);

    // ── 8: a stream this browser drops ─────────────────────────────────────────
    const lostId = await createSession(page, "UIV2-15 dropped stream");
    await page.evaluate((id) => selectHallSession(id), lostId);
    await sleep(args.settleMs * 6);
    const dropped = await page.evaluate(async () => {
      const model = document.getElementById("hallSeatArch").value || document.getElementById("hallModelSelect").value;
      const realFetch = window.fetch;
      const controller = new AbortController();
      // The pane's own stream is what has to survive a drop, so the drop is made under it: the
      // summon's body ends cleanly after two frames (a proxy reaping an idle socket looks exactly
      // like this to `consumeSSE`) while the request itself is aborted, which is what the server
      // records as a turn whose reply was never saved.
      window.fetch = async (url, init) => {
        if (!String(url).endsWith("/summon")) return realFetch(url, init);
        const response = await realFetch(url, { ...init, signal: controller.signal });
        const reader = response.body.getReader();
        const body = new ReadableStream({
          async start(stream) {
            for (let frame = 0; frame < 2; frame += 1) {
              const { value, done } = await reader.read();
              if (done) break;
              stream.enqueue(value);
            }
            stream.close();
            controller.abort();
            window.fetch = realFetch;
          },
        });
        return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
      };
      // A preflighted round skips the readiness gate: this step is about the stream ending,
      // not about whether the seat could start.
      try { await summonAgentPromise("architect", model, "off", false, true); } catch (error) { /* the dropped seat is the point */ }
      window.fetch = realFetch;
      // The server records the lost turn in its own finally; the pane re-reads until it sees it.
      let session = null;
      let notice = null;
      for (let attempt = 0; attempt < 80; attempt += 1) {
        session = await window.M5CouncilState.read(activeHallId).then((result) => result.session || null);
        notice = window.M5CouncilState.notice();
        if (session && session.abandoned_turn && notice && /not saved/i.test(notice.title)) break;
        await new Promise((done) => setTimeout(done, 100));
      }
      return {
        abandoned: session && session.abandoned_turn,
        messages: session && session.messages ? session.messages.length : 0,
        notice,
        unsavedCard: (() => {
          const badge = document.querySelector("#hallStream .badge.warn");
          return Boolean(badge && /not saved/i.test(badge.textContent));
        })(),
        drawn: (() => {
          // The card the reader must not mistake for a reply: it carries the badge and the text
          // this browser actually received, in the body or in the reasoning trace.
          const badge = document.querySelector("#hallStream .badge.warn");
          const card = badge && badge.closest(".voice-card");
          if (!card) return "";
          const body = (card.querySelector(".md") || {}).textContent || "";
          const trace = (card.querySelector(".think-box pre") || {}).textContent || "";
          return (body.length >= trace.length ? body : trace).trim();
        })(),
      };
    });
    record(
      "the server records the dropped turn instead of losing it silently",
      Boolean(dropped.abandoned) && Number(dropped.abandoned.streamed_chars) > 0 && Boolean(dropped.abandoned.model),
      dropped.abandoned
        ? `${dropped.abandoned.model} · ${dropped.abandoned.streamed_chars} chars discarded`
        : "no abandoned turn on the session",
    );
    record(
      "the pane says the reply was never saved, naming the model and the discarded characters",
      Boolean(dropped.notice) &&
        /not saved/i.test(dropped.notice.title) &&
        /characters of/i.test(dropped.notice.cause || "") &&
        /none of them reached the transcript/i.test(dropped.notice.cause || ""),
      dropped.notice ? `${dropped.notice.title} — ${dropped.notice.cause}` : "no notice",
    );
    record(
      "the text that arrived is drawn as unsaved rather than as a reply",
      dropped.unsavedCard && dropped.drawn.length > 0,
      dropped.unsavedCard ? `badge card carries ${dropped.drawn.length} chars` : "no unsaved card",
    );
    await shoot(page, `lost-turn-${args.viewport}-${args.theme}`, "#hallStream .badge.warn", "center");

    // ── 9: a session deleted outside the pane ──────────────────────────────────
    const goneId = await createSession(page, "UIV2-15 deleted elsewhere");
    await page.evaluate((id) => selectHallSession(id), goneId);
    await sleep(args.settleMs * 4);
    const gone = await page.evaluate(async (id) => {
      const before = document.querySelectorAll("#hallStream .voice-card").length;
      await fetch(`/api/hall/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
      await selectHallSession(id);
      return {
        before,
        after: document.querySelectorAll("#hallStream .voice-card").length,
        vanished: window.M5CouncilState.vanished(),
        canDispatch: window.M5CouncilState.canDispatch(),
        notice: window.M5CouncilState.notice(),
      };
    }, goneId);
    record(
      "a deleted session is named as gone, with the id",
      gone.vanished && Boolean(gone.notice && /404/.test(gone.notice.title)) && /deleted/.test(gone.notice.cause),
      gone.notice ? gone.notice.title : "no notice",
    );
    record(
      "its transcript is kept and dispatching into it is blocked",
      gone.after === gone.before && gone.canDispatch === false,
      `${gone.before} cards kept · canDispatch=${gone.canDispatch}`,
    );
    await shoot(page, `gone-session-${args.viewport}-${args.theme}`);

    // ── 10: a seat the server says is still answering ──────────────────────────
    // A live session, not the one that was just deleted: the state under test is a turn the
    // server is running, and the pane's own read of a gone session would confound it.
    const runningId = await createSession(page, "UIV2-15 still answering");
    await page.evaluate((id) => selectHallSession(id), runningId);
    await sleep(args.settleMs * 4);
    const runningState = await page.evaluate(async () => {
      await selectHallSession(activeHallId);
      const realFetch = window.fetch;
      const started = Date.now() / 1000;
      window.fetch = async (url, init) => {
        if (String(url).includes("/api/hall/sessions/") && (!init || !init.method || init.method === "GET")) {
          const response = await realFetch(url, init);
          const payload = await response.json();
          window.fetch = realFetch;
          return new Response(JSON.stringify({
            ...payload,
            active_turn: { author: "Staff Architect (demo)", role_key: "architect", model: "demo-model", started_at: started, pin_as_conclusion: false },
          }), { status: 200, headers: { "Content-Type": "application/json" } });
        }
        return realFetch(url, init);
      };
      await window.M5CouncilState.reconcile();
      const notice = window.M5CouncilState.notice();
      const watching = typeof hallTurnWatch === "number" && hallTurnWatch !== 0;
      window.M5CouncilState.stopWatch();
      const stopped = hallTurnWatch === 0;
      window.fetch = realFetch;
      return { notice, watching, stopped };
    });
    record(
      "a seat the server says is answering is stated as running, not as a failure",
      Boolean(runningState.notice && /still answering/i.test(runningState.notice.title)),
      runningState.notice ? runningState.notice.title : "no notice",
    );
    record("the poll it starts can be stopped, and is stopped when the reader says so", runningState.watching && runningState.stopped, `scheduled=${runningState.watching} stopped=${runningState.stopped}`);
    await shoot(page, `still-answering-${args.viewport}-${args.theme}`);

    // ── 11: Clear session is armed, refusal-safe, and a true delete on success ──
    // The first confirmation is refused once by this walk. The pane must keep the same session
    // and offer Retry; only the successful retry may remove the active id and transcript.
    const clearArmed = await page.evaluate(async (id) => {
      await selectHallSession(id);
      const realFetch = window.fetch;
      window.__councilRealFetch = realFetch;
      window.__councilDeleteAttempts = 0;
      window.__councilRefuseDelete = true;
      window.fetch = async (url, init) => {
        const method = String(init && init.method || "GET").toUpperCase();
        if (method === "DELETE" && String(url).endsWith(`/api/hall/sessions/${encodeURIComponent(id)}`)) {
          window.__councilDeleteAttempts += 1;
          if (window.__councilRefuseDelete) {
            window.__councilRefuseDelete = false;
            return new Response(JSON.stringify({ detail: "UIV2-27 refused this delete once" }), {
              status: 503, headers: { "Content-Type": "application/json" },
            });
          }
        }
        return realFetch(url, init);
      };
      const workspace = await fetch("/api/harness/sessions").then((response) => response.json());
      window.__councilWorkspaceBefore = workspace.default_workspace || "";
      document.getElementById("hallClearSessionBtn").click();
      const stillThere = await fetch(`/api/hall/sessions/${encodeURIComponent(id)}`);
      const button = document.getElementById("hallClearSessionBtn");
      return {
        armed: hallClearSessionArmed,
        label: button.textContent.trim(),
        scope: button.title,
        attempts: window.__councilDeleteAttempts,
        status: stillThere.status,
      };
    }, runningId);
    record(
      "Clear session arms without deleting and states the irreversible session-only scope",
      clearArmed.armed && clearArmed.label === "Confirm clear" && clearArmed.attempts === 0
        && clearArmed.status === 200 && /Irreversible/.test(clearArmed.scope)
        && /no archive/.test(clearArmed.scope) && /workspace are never touched/.test(clearArmed.scope),
      `${clearArmed.label}; DELETE requests=${clearArmed.attempts}; session=${clearArmed.status}`,
    );
    await page.evaluate(() => document.getElementById("hallClearSessionBtn").focus());
    await page.keyboard.press("Enter");
    await sleep(args.settleMs * 10);
    const clearRefused = await page.evaluate(async (id) => {
      const response = await fetch(`/api/hall/sessions/${encodeURIComponent(id)}`);
      const notice = document.querySelector("#hallRefusalState .presentation");
      return {
        active: activeHallId,
        data: activeHallData && activeHallData.id,
        status: response.status,
        listed: hallSessions.some((session) => session.id === id),
        attempts: window.__councilDeleteAttempts,
        kind: notice && notice.dataset.kind,
        title: notice && notice.querySelector(".presentation-title")
          ? notice.querySelector(".presentation-title").textContent.trim() : "",
        actions: notice ? [...notice.querySelectorAll(".presentation-actions button")].map((button) => button.textContent.trim()) : [],
      };
    }, runningId);
    record(
      "a refused delete keeps the active session, its transcript and its row, with Retry available",
      clearRefused.active === runningId && clearRefused.data === runningId
        && clearRefused.status === 200 && clearRefused.listed && clearRefused.attempts === 1
        && Boolean(clearRefused.kind) && clearRefused.actions.includes("Retry"),
      `active=${clearRefused.active}; server=${clearRefused.status}; notice=${clearRefused.title}`,
    );
    await page.evaluate(() => {
      const retry = [...document.querySelectorAll("#hallRefusalState .presentation-actions button")]
        .find((button) => button.textContent.trim() === "Retry");
      if (retry) retry.focus();
    });
    await page.keyboard.press("Enter");
    await sleep(args.settleMs * 12);
    const cleared = await page.evaluate(async ({ id, survivors }) => {
      const response = await fetch(`/api/hall/sessions/${encodeURIComponent(id)}`);
      const remaining = await fetch("/api/hall/sessions").then((result) => result.json());
      const workspace = await fetch("/api/harness/sessions").then((result) => result.json());
      const ids = (remaining.sessions || []).map((session) => session.id);
      const result = {
        status: response.status,
        attempts: window.__councilDeleteAttempts,
        activeChanged: activeHallId !== id,
        removed: !ids.includes(id),
        survivors: survivors.every((survivor) => ids.includes(survivor)),
        workspaceKept: (workspace.default_workspace || "") === window.__councilWorkspaceBefore,
        notice: Boolean(document.querySelector("#hallRefusalState .presentation")),
      };
      window.fetch = window.__councilRealFetch;
      return result;
    }, { id: runningId, survivors: [closedId, lostId] });
    record(
      "Retry performs the confirmed delete once the route answers",
      cleared.status === 404 && cleared.attempts === 2 && cleared.activeChanged
        && cleared.removed && !cleared.notice,
      `server=${cleared.status}; DELETE requests=${cleared.attempts}; activeChanged=${cleared.activeChanged}`,
    );
    record(
      "deleting one Council session keeps the other sessions and the configured workspace",
      cleared.survivors && cleared.workspaceKept,
      `survivors=${cleared.survivors}; workspaceKept=${cleared.workspaceKept}`,
    );

    // The deleted-session step asks for a session that is gone, so a 404 in the console is this
    // walk's own doing. An uncaught exception is not.
    const unexpected = pageErrors.filter((text) => !/status of 404/.test(text));
    record("no uncaught error is raised by any of it", unexpected.length === 0, unexpected.slice(0, 3).join(" | "));
  } finally {
    await browser.close();
  }

  const passed = results.filter((r) => r.passed).length;
  if (args.json) {
    console.log(JSON.stringify({ passed, total: results.length, results, shots, page_errors: pageErrors }, null, 2));
  } else {
    console.log(`\n${passed}/${results.length} checks passed · ${shots.length} screenshots${args.captureOnly ? " (capture-only: failures not counted)" : ""}`);
  }
  return args.captureOnly || passed === results.length ? 0 : 1;
}

run(parseArgs(process.argv.slice(2)))
  .then((code) => { process.exitCode = code; })
  .catch((error) => {
    console.error(error.message);
    process.exitCode = 2;
  });
