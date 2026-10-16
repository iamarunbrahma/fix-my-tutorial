# Devpost "Additional info" answers (draft)

Fields marked ARUN need Arun's own answer or confirmation.

- Submitter type: Individual. Organization: N/A. Country: India. Canada province: N/A.
- Track: Coding and agentic engineering. New or existing: New.
- Repository: https://github.com/iamarunbrahma/fix-my-tutorial (private until approved to go public).
- Demo URL: https://fix-my-tutorial.onrender.com (saved in the draft 16 Oct 2026, also as the Try it out link).
- Tavily: Yes. IRL city: none.

## Which model(s) did you use, and why that size/variant?
Four NVIDIA Nemotron models through Nebius Token Factory, chosen by role. Nemotron 3 Super (120B-A12B) plans each run, reading a whole tutorial into a strict JSON plan, and writes the run summary. Nemotron 3.5 Lightning triages failures: fast, cheap classification of an error log. Nemotron 3 Nano (30B-A3B) judges fuzzy expectations such as "prints the accuracy". Nemotron 3 Ultra (550B-A55B) writes the repairs, the hard reasoning over the error, the code and the cited evidence, and it only runs when a step actually breaks. That split keeps a fixed tutorial at about $0.012 in model calls.

## Rate Nemotron's output quality (1-10) - ARUN picks the number
Suggested explanation: Ultra's repairs were the highlight: in our final live evaluation it fixed 13 of 13 broken tutorials (12 in the first round), each fix citing the changelog or issue it relied on. The weak spot was faithfulness inside structured output: Super occasionally invented output checks, dropped a tutorial's code block, or double-escaped newlines, and Lightning sometimes appended text after a valid JSON object. We added deterministic guards for each.

## Fine-tune, prompt-engineer, or out of the box?
Out of the box with prompt engineering: role-specific system prompts, JSON-schema structured output validated with pydantic, one retry with the validation error appended, and deterministic checks in code around every model decision (coverage of the page's code, required citations, rejecting fixes that hide failures, a clean replay before "fixed").

## How did Nemotron compare to other models?
We built only on Nemotron for this project, so we have no controlled comparison. What stood out was having the whole size range, from Nano to Ultra, behind one API, which made routing each task to the cheapest model that can do it straightforward.

## Most valuable Nebius capabilities
Token Factory's OpenAI-compatible inference API: one client and one key for four Nemotron sizes, so routing by role is a lookup and retries and spend accounting are shared. JSON-schema structured output made every stage a typed function. Published per-token prices let us build a spend guard that reserves an upper-bound cost before each call. We run Nemotron purely through Token Factory's serverless API (no GPU instances to manage), with up to four runs in parallel. Token Factory Sandboxes run every tutorial step on the live demo: an image is saved after each step, and the repair race starts several candidate fixes from the same checkpoint in parallel sandboxes.

## Recommend Nemotron on Nebius (1-10) - ARUN
## Inference experience vs previous environments (1-10) - ARUN

## Improvements that would have helped
1. A prepaid-only mode or a hard stop at zero balance, so promo credits never risk a card charge.
2. A "use remaining credits, then stop" option at trial end; today "stop usage" also stops keys while promo balance remains.
3. Clearer promo-code errors: ours said only "Promocode apply limit exceeded".
4. Consistent model IDs between the catalog and the API (Nemotron-3.5-Lightning vs nvidia/Nemotron-3_5-Lightning).
5. Faster or self-serve Sandboxes beta access for hackathon participants.
6. Lightning sometimes appends text after a valid JSON object in json_schema mode, and sometimes echoes part of its input (a JSON list) or writes a long plain-text reasoning passage before the object, so a 300-token cap ran out before any JSON.
7. The Structured output Python sample in the docs is missing two commas.
8. One clear path to Sandboxes activation: the beta form gives no timeline, and the "email contree@nebius.com" route is only in the Set up access page.
9. A promo code that fails with "Promocode apply limit exceeded" should say which limit was hit and whether another program's credits can still be applied.

## What do you most hope to see from the Nemotron team next? - ARUN (suggestion)
Stronger faithfulness in structured output when asked to preserve content exactly, and a small, fast model with Ultra-level code repair.

## Which AI tools did you use?
Claude Code (Anthropic) as a coding assistant for implementation, tests and code review. At runtime the app uses NVIDIA Nemotron models via Nebius Token Factory and the Tavily API.

## ARUN only
- Age of majority confirmation; not an employee/agent of the sponsors.
- Level of learning; AI value for career.
