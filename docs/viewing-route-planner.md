# 智慧睇樓路線 MVP

## Goal

Connect a selected client's private shortlist to a saved, optimized viewing itinerary inside the authenticated Workbench.

## Required flow

- Select client and 2–8 shortlisted properties.
- Set date, departure time, start/end location, per-property viewing duration, and driving/taxi or public-transit mode.
- Optimize route without fabricating unavailable travel data.
- Show numbered map markers and a full arrival/viewing/departure timeline.
- Support manual reorder and recalculation.
- Save/reopen plans.
- Create a cryptographically random, revocable, read-only, noindex client share link.
- Provide Google Maps navigation links per leg.

## Safety

- Workbench Basic Auth on all planning writes and private reads.
- Only properties belonging to the selected client's shortlist.
- Share pages must exclude internal notes and Workbench controls.
- Same-origin APIs; no CORS expansion.
- Missing coordinates are flagged, never guessed.
- No publish, review, import, AI generation, or listing-status mutation.
- Do not expose secrets.

## Staging gate

- Full tests, auth/ownership/input/provider/share/revocation coverage.
- Desktop/mobile Playwright flow.
- Before/after DB counts and production health unchanged.
- Isolated staging only until Johnny accepts.
