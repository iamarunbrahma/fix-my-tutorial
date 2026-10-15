# Feedback on Nebius Token Factory, Sandboxes and NVIDIA Nemotron

Running log kept during the build (2026-10-15 to 2026-10-27). Each item: what we did, what worked, what needs work.

## Onboarding (zero to hello world)
- Billing setup with a bank card is mandatory before the console works at all, including for hackathon promo credits. The card can be charged automatically if the balance goes negative; there is no hard "stop at zero" option, so we built our own spend cap in code. A prepaid-only mode or a hard stop at zero would make promo credits safer for students and hackathon participants.
- The hackathon promo form emails a code within minutes; applying it is a separate step under Top up. Clear once found.
- The Structured output docs Python sample is missing commas after `"type": "json_schema"` and after the closing `}` of `response_format`, so it does not run as copied.

## Sandboxes (beta)
Observations are added here during Tasks 13 and 22 (access time, limits, timeout reporting, Docker vs Contree timing).

## Nemotron models
Observations are added here during Tasks 5 and 22 (JSON-schema adherence per model, reasoning output, latency, cost per run).
