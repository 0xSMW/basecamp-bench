# Project orientation

This repository contains the product, design, and API source material for a
Basecamp 5 frontend or backend prototype.

- **INIT.md** — product + domain + sample-seed spec. §1 product model; §2 shell/tools; §3 "Launch the new website" seed; §4 Recording/domain rules; §7 surface checklist for screenshot-backed screens; §8 track-specific FE/BE notes.
- **DESIGN.md** — design tokens from production (light default, dark via two identical override blocks, 10px root font). Don't invent values — use these tokens.
- **reference/screens/** — 9 curated captures of the real app (dark mode); cite them when matching visuals.
- **reference/basecamp-sdk/** — OpenAPI spec + SPEC.md + behavior model (BE contract).

When the spec and your instinct disagree, the spec wins. When the spec is
silent, use the screenshots and `DESIGN.md` for frontend decisions and the SDK
pack for backend decisions. Prefer explicit stubs over misleading success.
