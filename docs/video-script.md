# Fix My Tutorial demo video script

> Look and voice approved by Arun on 2026-10-15: keynote style (black background, smooth motion, zoom-punches
> into the live UI, royalty-free music ducked under the voice, no narration repeated as on-screen text, nothing
> personal on screen). Voice (changed by Arun on 2026-10-16): Sarvam `bulbul:v4-flash`, speaker
> `shubh_en_narration`, pace about 1.1.
>
> Hard ceiling **3:00**. This draft targets about **2:40** at the 190 words per minute measured on the last cut.
>
> **Before rendering, settle these two (do not record until both are final):**
> 1. Beat 9: replace `ROT_INDEX_STAT` with the real headline from `rot_index/results.json`.
> 2. Beats 4 and 11: if Token Factory Sandboxes access is granted and the demo runs on it, say
>    "Token Factory Sandbox"; if we fall back to Docker, keep "isolated sandbox" and drop the Sandboxes claim in beat 11.
> 3. Features added on 2026-10-15 after this draft: "When it broke" (release, date, how long it worked), "Tell the
>    author" (pre-filled GitHub issue) and the Rot Index "Releases that broke the most tutorials" list. Propose where
>    they fit (likely the report beat and beat 9) and get Arun's OK on the revised lines before recording.

Every number spoken below must be real and reproducible:

| Claim | Where it comes from |
|---|---|
| "Tutorial Rot" issue in NVIDIA's OpenShell repo | github.com/NVIDIA/OpenShell/issues/2998 |
| 13 of 13 broken tutorials fixed, both working ones untouched | `docs/eval-results.md` (final live run) |
| about a cent per fix, about a minute | eval table: $0.012 average model cost, median 71 s |
| four Nemotron models, one API | `src/fixmytutorial/config.py` model routing |
| ROT_INDEX_STAT | `rot_index/results.json` (pending) |

---

## Beat sheet

| # | On screen |
|---|---|
| 1 | A real tutorial page (scikit-learn Boston housing), then its code failing with the `load_boston` ImportError |
| 2 | The OpenShell "Tutorial Rot" issue title, zoom-punch |
| 3 | Home page: paste the tutorial link, press Run it |
| 4 | Steps lighting up one by one, checkpoint indicator after each |
| 5 | The failing step turns red; "What broke" card with the key error line |
| 6 | Evidence list: the scikit-learn release note quote, zoom on the citation |
| 7 | The race: three fix cards running in parallel, one turns green "applied to the tutorial" |
| 8 | Replay bar filling from scratch, then the "Fixed" verdict |
| 9 | Report page: diff with cited reason, the badge; then the Rot Index board headline |
| 10 | Black card: the four Nemotron roles (Super, Lightning, Nano, Ultra) appearing one by one |
| 11 | Diagram: checkpoint, then three branches from it; spend guard caps |
| 12 | Eval table, 13 of 13 highlighted |
| 13 | Home page again, URL, end card |

> **16 Oct 2026, as recorded:** the demo run is the TensorFlow-Examples "Hello World" notebook (2016), per Arun's
> ask for old blogs. Beat 9 drops the break date (no source states the exact TensorFlow release, and the finder
> never guesses) and the Rot Index number (the 2013-2018 index is built tomorrow). Beat 10 says "a few cents".
> Voice-only spelling: "Raht" so the voice says "Rot". Narration checked word by word with local Whisper.

<!-- narration-start -->

Here's one of the most popular TensorFlow tutorials on GitHub, written in 2016. Follow it today, and it breaks before it can even say hello. Not because the author got it wrong, but because the library moved on.

---

This happens so often that NVIDIA's own OpenShell repo has an open issue called "Tutorial Rot". Learners hit errors the author never saw, and give up.

---

So I made Fix My Tutorial. Paste any Python or JavaScript tutorial. It checks whether the tutorial still works today, and if it doesn't, it fixes it.

---

Nemotron 3 Super reads the page and turns it into steps, keeping the code exactly as written. Each step runs in a Nebius Token Factory Sandbox, with a checkpoint saved after every step.

---

Here, creating the session fails. Nemotron 3.5 Lightning reads the error and works out what kind of break it is: an API that was removed.

---

Then Tavilee searches the docs and release notes, and pulls out the exact passage that explains it, so every fix has a source you can check.

---

Nemotron 3 Ultra proposes up to three fixes: move to the new API, pin an older version, or work around it. They race in parallel sandboxes from the last good checkpoint. The first one that really works wins. A fix that only hides the error is thrown out before it runs.

---

Then the whole tutorial is replayed from scratch. Only if that passes do we call it fixed.

---

You get a report with the exact change, the reason and the source behind it. One click opens a GitHub issue for the tutorial's author, already filled in.

---

Under the hood, four Nemotron models share the work through one Nebius Token Factory API. The big model only runs when something is actually broken, so a typical fix costs just a few cents.

---

Sandbox checkpoints and branches let several fixes race at once, and a spending guard keeps every run inside its budget.

---

On our live test set of thirteen tutorials broken by real library changes, it fixed all thirteen, and left the working ones untouched.

---

For learners, it means the tutorial finally works. For the people who write docs, it means knowing the day a tutorial breaks. Try it on the one that broke for you.

## Changes for the 2026-10-15 features (beat 9 approved by Arun on 2026-10-16 and applied above)

Beat 9, replacing the current line (about 6 seconds longer, still under 3:00):

> You get a report with the exact change, the reason and the source behind it, and when it broke: WHEN_STAT. One click sends the fix to the tutorial's author as a GitHub issue. Across our Rot Index of real tutorials, ROT_INDEX_STAT.

- WHEN_STAT comes from the live demo run's "When it broke" card, word for word (for the scikit-learn Boston housing tutorial, expect a release-notes answer, since scikit-learn releases before Python 3.12 support can't install in the sandbox). Never write it from memory.
- On screen: zoom-punch on the "When it broke" card, then the "Open an issue" button.
- Optional for beat 9's second half: the Rot Index "Releases that broke the most tutorials" list instead of only the headline number.
