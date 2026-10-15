(() => {
  const initial = JSON.parse(document.getElementById("initial").textContent);
  const runId = initial.id;
  const $ = (s) => document.querySelector(s);
  const el = (tag, attrs = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) k === "class" ? (n.className = v) : n.setAttribute(k, v);
    for (const k of kids) n.append(k instanceof Node ? k : document.createTextNode(String(k ?? "")));
    return n;
  };
  const STAGES = {
    queued: "Waiting to start", ingesting: "Reading the tutorial", planning: "Planning the steps with Nemotron 3 Super",
    executing: "Running the steps in a Token Factory Sandbox", repairing: "Repairing a broken step",
    replaying: "Replaying the fixed tutorial from scratch", done: "Finished", failed: "Stopped",
  };
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

  function renderSteps() {
    const ul = $("#steps");
    ul.replaceChildren();
    for (const s of state.steps) {
      const st = state.status[s.id] || "pending";
      const li = el("li", { class: s.id === state.selected ? "sel" : "" },
        el("span", { class: `chip ${st}` }, st), el("span", {}, s.title), el("span", { class: "kind" }, s.kind));
      if (s.implicit) li.append(el("span", { class: "implicit" }, "added"));
      li.addEventListener("click", () => { state.selected = s.id; renderSteps(); renderOutput(); });
      ul.append(li);
    }
  }

  function renderOutput() {
    const s = state.steps.find((x) => x.id === state.selected);
    $("#steptitle").textContent = s ? `${s.id} · ${s.title}` : "Select a step";
    $("#code").textContent = s ? s.content : "";
    $("#output").textContent = state.output[state.selected] || (s ? "No output yet." : "");
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
      const bar = $("#replaybar");
      bar.hidden = false;
      if (ev.status === "passed") state.replayDone += 1;
      bar.firstElementChild.style.width = `${Math.min(100, (100 * state.replayDone) / Math.max(1, state.steps.length))}%`;
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
    $("#sources").replaceChildren(el("li", { class: "muted" }, "Searching docs, changelogs and issues with Tavily"));
  }

  function onEvidence(ev) {
    const ul = $("#sources");
    ul.replaceChildren();
    if (!ev.items.length) ul.append(el("li", { class: "muted" }, "No sources found. Repairing from the error alone."));
    for (const e of ev.items) {
      let host = "";
      try { host = new URL(e.url).hostname; } catch (_) { host = ""; }
      ul.append(el("li", {}, el("a", { href: e.url, target: "_blank", rel: "noopener" }, e.title),
        el("span", { class: "muted" }, host ? ` · ${host}` : ""), el("q", {}, e.quote)));
    }
  }

  function onCandidates(ev) {
    $("#repair").hidden = false;
    $("#round").textContent = `round ${ev.round}`;
    const box = $("#race");
    box.replaceChildren();
    if (!ev.candidates.length) box.append(el("p", { class: "muted" }, "No usable fix was proposed this round."));
    ev.candidates.forEach((c, i) => {
      const edits = el("ul", {});
      for (const e of c.edits) edits.append(el("li", {}, `${e.op === "replace" ? "Change" : "Add after"} ${e.step_id}: ${e.reason}`));
      box.append(el("div", { class: "cand", id: `cand-${i}` }, el("div", { class: "strategy" }, c.strategy),
        el("div", { class: "muted" }, c.rationale), edits, el("div", { class: "result muted" }, "Running")));
    });
  }

  function onCandidateResult(ev) {
    const n = document.getElementById(`cand-${ev.index}`);
    if (!n) return;
    n.classList.add(ev.passed ? "passed" : "failed");
    n.querySelector(".result").textContent = ev.passed ? "Passed" : `Failed: ${ev.error || ""}`;
  }

  function onWinner(ev) {
    const n = document.getElementById(`cand-${ev.index}`);
    if (!n) return;
    n.classList.add("winner");
    n.querySelector(".result").textContent = "Passed · applied to the tutorial";
  }

  function onVerdict(ev) {
    setStage(ev.verdict === "error" ? "failed" : "done");
    const v = $("#verdict");
    v.hidden = false;
    v.replaceChildren(el("span", { class: `pill v-${ev.verdict}` }, VERDICTS[ev.verdict] || ev.verdict),
      el("span", { class: "muted" }, ev.summary || ev.reason || ""),
      el("a", { class: "btn", href: `/r/${runId}` }, "See the report"));
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
