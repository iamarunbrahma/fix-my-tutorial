(() => {
  const initial = JSON.parse(document.getElementById("initial").textContent);
  const runId = initial.id;
  const sandbox = document.querySelector("main").dataset.sandbox || "a sandbox";
  const $ = (s) => document.querySelector(s);
  // Builds DOM with text nodes only: tutorial titles, model output and quotes are untrusted.
  const el = (tag, attrs = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) k === "class" ? (n.className = v) : n.setAttribute(k, v);
    for (const k of kids) n.append(k instanceof Node ? k : document.createTextNode(String(k ?? "")));
    return n;
  };
  const STAGES = {
    queued: "Waiting to start", ingesting: "Reading the tutorial", planning: "Planning the steps with Nemotron 3 Super",
    executing: `Running the steps in ${sandbox}`, repairing: "Repairing a broken step",
    replaying: "Replaying the fixed tutorial from scratch", done: "Finished", failed: "Stopped",
  };
  const STATUS = { pending: "Waiting", running: "Running", passed: "Passed", failed: "Failed", repairing: "Repairing",
    repaired: "Repaired" };
  const VERDICTS = { still_works: "Still works", fixed: "Fixed", could_not_fix: "Could not fix",
    not_runnable: "Not runnable", error: "Check failed" };
  const CATEGORY = {
    removed_api: "A function or class was removed or renamed", renamed_package: "A package or module was renamed",
    version_conflict: "Package versions no longer fit together", dead_resource: "A file, dataset or URL no longer exists",
    changed_cli: "A command or flag changed", tutorial_typo: "A mistake in the tutorial itself",
    environment: "Something missing in the environment", timeout: "A step never finished", unknown: "Something else broke",
  };
  const state = { steps: [], status: {}, output: {}, selected: null, replayDone: 0 };

  const setStage = (s) => { $("#stage").textContent = STAGES[s] || s; };
  const pill = (status, label) => el("span", { class: "status", "data-status": status }, label);

  function renderSteps() {
    const list = $("#steps");
    list.replaceChildren();
    for (const s of state.steps) {
      const st = state.status[s.id] || "pending";
      const button = el("button", { type: "button", class: "step", "aria-current": String(s.id === state.selected) },
        pill(st, STATUS[st] || st),
        el("span", { class: "min-w-0 flex-1 truncate" }, s.title),
        el("span", { class: "text-muted-foreground font-mono text-[11px]" }, s.kind));
      if (s.implicit) button.append(el("span", { class: "badge", "data-variant": "outline" }, "added"));
      button.addEventListener("click", () => { state.selected = s.id; renderSteps(); renderOutput(); });
      list.append(el("li", {}, button));
    }
  }

  function renderOutput() {
    const s = state.steps.find((x) => x.id === state.selected);
    $("#steptitle").textContent = s ? s.title : "Select a step";
    $("#stepmeta").textContent = s ? `${s.id} · ${s.kind}${s.path ? ` · ${s.path}` : ""}` : "";
    $("#code").textContent = s ? s.content : "";
    $("#output").textContent = (state.output[state.selected] || (s ? "No output yet." : "")).replace(/^\s*\n/, "");
  }

  function onPlan(ev) {
    state.steps = ev.steps || [];
    if (ev.title) $("#title").textContent = ev.title;
    if (ev.note) $("#note").textContent = ev.note;
    if (!state.selected && state.steps.length) state.selected = state.steps[0].id;
    renderSteps(); renderOutput();
  }

  function onStep(ev) {
    if (ev.phase === "replay") {
      $("#replay").hidden = false;
      if (ev.status === "passed") state.replayDone += 1;
      const total = Math.max(1, state.steps.length);
      $("#replaybar").firstElementChild.style.width = `${Math.min(100, (100 * state.replayDone) / total)}%`;
      $("#replay-count").textContent = `${Math.min(state.replayDone, total)} / ${total} steps`;
      return;
    }
    state.status[ev.step_id] = ev.status;
    if (ev.output !== undefined) state.output[ev.step_id] = ev.output;
    if (ev.status === "running" || ev.status === "failed" || ev.status === "repairing") state.selected = ev.step_id;
    renderSteps(); renderOutput();
  }

  function onDiagnosis(ev) {
    $("#diagnosis").hidden = false;
    $("#diag-cat").textContent = CATEGORY[ev.category] || ev.category;
    $("#diag-line").textContent = ev.key_line || "";
    $("#sources").replaceChildren(el("li", { class: "text-muted-foreground text-sm" },
      "Searching docs, changelogs and issues with Tavily…"));
  }

  function onEvidence(ev) {
    const list = $("#sources");
    list.replaceChildren();
    if (!ev.items.length) {
      list.append(el("li", { class: "text-muted-foreground text-sm" }, "No sources found. Repairing from the error alone."));
    }
    for (const e of ev.items) {
      let host = "";
      try { host = new URL(e.url).hostname; } catch (_) { host = ""; }
      list.append(el("li", { class: "source" },
        el("a", { href: e.url, target: "_blank", rel: "noopener" }, e.title),
        el("span", { class: "text-muted-foreground ml-2 text-xs" }, host),
        el("q", {}, e.quote)));
    }
  }

  function onCandidates(ev) {
    $("#repair").hidden = false;
    $("#round").textContent = `Round ${ev.round}`;
    const box = $("#race");
    box.replaceChildren();
    if (!ev.candidates.length) {
      box.append(el("p", { class: "text-muted-foreground text-sm" }, "No usable fix was proposed this round."));
    }
    ev.candidates.forEach((c, i) => {
      const edits = el("ul", { class: "text-muted-foreground flex list-disc flex-col gap-1 pl-4 text-xs" });
      for (const e of c.edits) {
        edits.append(el("li", {}, `${e.op === "replace" ? "Change" : "Add after"} ${e.step_id}: ${e.reason}`));
      }
      box.append(el("div", { class: "candidate", id: `cand-${i}`, "data-result": "running" },
        el("div", { class: "flex items-center justify-between gap-2" },
          el("span", { class: "font-semibold capitalize" }, c.strategy), pill("running", "Running")),
        el("p", { class: "text-muted-foreground leading-relaxed" }, c.rationale), edits,
        el("p", { class: "result text-muted-foreground text-xs" }, "")));
    });
  }

  function setCandidate(index, result, status, label, text) {
    const n = document.getElementById(`cand-${index}`);
    if (!n) return;
    n.dataset.result = result;
    n.querySelector(".status").replaceWith(pill(status, label));
    n.querySelector(".result").textContent = text;
  }

  function onCandidateResult(ev) {
    if (ev.passed) setCandidate(ev.index, "passed", "passed", "Passed", "");
    else setCandidate(ev.index, "failed", "failed", "Failed", ev.error || "");
  }

  function onWinner(ev) {
    setCandidate(ev.index, "winner", "passed", "Applied", "Passed · applied to the tutorial");
  }

  function onVerdict(ev) {
    setStage(ev.verdict === "error" ? "failed" : "done");
    $("#workspace").hidden = state.steps.length === 0;  // nothing ran: no empty step panels
    $("#stage-dot").replaceWith(Object.assign(pill(ev.verdict, VERDICTS[ev.verdict] || ev.verdict), { id: "stage-dot" }));
    const v = $("#verdict");
    v.hidden = false;
    v.replaceChildren(
      el("div", { class: "flex flex-col gap-3" },
        el("div", {}, pill(ev.verdict, VERDICTS[ev.verdict] || ev.verdict)),
        el("p", { class: "leading-relaxed text-pretty" }, ev.summary || ev.reason || "")));
    if (ev.note) {
      v.append(el("div", { class: "alert", role: "status" }, el("h2", {}, ev.note)));
    }
    v.append(el("div", {}, el("a", { class: "btn", href: `/r/${runId}` }, "See the report")));
  }

  if (initial.plan) onPlan({ steps: initial.plan.steps, title: initial.title, note: initial.plan.note });
  setStage(initial.status);
  renderSteps(); renderOutput();

  const es = new EventSource(`/run/${runId}/events`);
  const on = (t, f) => es.addEventListener(t, (m) => f(JSON.parse(m.data)));
  on("stage", (e) => setStage(e.stage));
  on("queue", (e) => { $("#stage").textContent = `Waiting in line: position ${e.position}`; });
  on("plan", onPlan);
  on("step", onStep);
  on("diagnosis", onDiagnosis);
  on("evidence", onEvidence);
  on("candidates", onCandidates);
  on("candidate_result", onCandidateResult);
  on("winner", onWinner);
  on("verdict", onVerdict);
  on("done", () => es.close());
})();
