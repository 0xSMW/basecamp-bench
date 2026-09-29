# Basecamp Bench comparison for 2026-09-29

Claude Sonnet 5.5 High completed both tracks in one local run, and GPT-6 Sol High evaluated both submissions. It ranks third by combined score, behind Fable 5.1 and Astra. Its backend score of 9.011 is second only to Astra's 9.305. Rows are ordered by frontend plus backend score. Total time sums implementation and evaluation stage durations, including stages that overlapped in wall-clock time. Total cost sums implementation and evaluation cost across both tracks.

| Model | Frontend | Backend | Total time | Total cost |
|---|---:|---:|---:|---:|
| Claude Fable 5.1 | 8.183 | 8.965 | 3:04:06 | $92.96 |
| GPT-6 Astra | 7.607 | 9.305 | 1:36:31 | $25.57 |
| **Claude Sonnet 5.5 High** | **7.575** | **9.011** | **2:18:20** | **$52.30** |
| Claude Opus 5 | 7.583 | 8.390 | 2:26:31 | $78.11 |
| Claude Fable 5 | 7.578 | 8.392 | 2:06:41 | $85.87 |
| Claude Opus 5.5 High | 6.625 | 8.683 | 1:59:02 | $32.30 |
| Grok 4.6 | 7.104 | 7.926 | 0:57:58 | $10.85 |
| Grok 4.7 High | 6.650 | 7.605 | 4:20:06 | ≥$10.46 |
| Claude Sonnet 5 | 6.982 | 7.243 | 1:27:09 | $36.23 |
| Grok 4.5 | 6.384 | 7.278 | 0:36:48 | $9.30 |
| Claude Opus 4.8 | 6.587 | 6.590 | 1:31:21 | $29.83 |
| GPT-5.5 | 5.670 | 7.084 | 0:44:15 | $10.94 |
| GPT-5.6 Sol High (latest r5) | 5.780 | 6.930 | 0:51:21 | $13.94 |
| Muse Spark 1.3 | 5.745 | 6.711 | 0:42:24 | $4.58 |
| GPT-6 Sol High | 6.212 | 6.155 | 1:33:42 | $6.73 |
| Gemini 3.8 Flash High | 5.700 | 6.483 | 0:38:17 | $6.07 |
| GPT-6 Luna Max | 5.434 | 6.695 | ~5:17:32 | ≥$3.30 |
| Gemini 3.7 Flash High | 5.608 | 5.800 | 0:30:59 | $7.54 |
| Gemini 3.1 Pro High | 3.190 | 3.872 | 0:22:07 | $4.57 |
| Gemini 3.5 Flash High | 0.000 | 0.100 | 0:12:47 | $3.48 |
| GLM 5.2 | 0.000 | 0.050 | 0:38:25 | $1.55 |
| 0x Alpha Max (unranked, no FE evaluation) | — | 7.956 | 1:03:17 | $3.56 |

Sonnet 5.5 used `claude-sonnet-5-5` at high effort in the Claude Code harness. Its frontend ran under FE contract 2026-09-05.1 and its backend under BE contract 2026-07-11.2. The run manifest and artifacts passed `verify-run`. The frontend took 1:25:47 and cost $37.77. The backend took 52:34 and cost $14.54.

Chrome refused the local `file:` URL, so the frontend evaluation used source inspection, the submitted 491-line DOM test (127 routes, 1,114 passing assertions), and independent Node scenarios. The judge found that door and cloud-link forms accept `javascript:` URLs, that `#/1/search?q=%` throws before the view error boundary, and that a version-4 stored object with no `seq` gives the next project a `null` ID. The backend evaluator matched all 203 operations, validated 77 GET responses against their schemas, and ran 79 focused assertions in process because the sandbox blocked sockets. Its main finding is that JSON and octet-stream routes accept `text/plain` bodies.

Rows from before September 23 were judged by GPT-5.6 Sol High, and later rows by GPT-6 Sol High. Historical values come from the [September comparison data](../september-2026-comparison.json), except GPT-5.6 Sol, which keeps the latest r5 repetition used in the two previous comparisons. Grok 4.7 and GPT-6 Luna include GPT-6 Sol continuations, and their costs are lower bounds, as the [September 23 comparison](../2026-09-23-sol6-luna6-grok47/comparison.md) describes. Opus 5.5 figures are from the [September 24 comparison](../2026-09-24-opus55/comparison.md). The main `basecamp-bench-report.html` now includes Sonnet 5.5, Opus 5.5, GPT-6 Sol, GPT-6 Luna, and Grok 4.7.

Evidence: [FE attempt](../../runs/2026-09-29T11-21-04Z--fe-be--anthropic-claude-sonnet-5-5--20260929t112104z-4341d4/attempts/fe-claude-sonnet-55-anthropic-claude-sonnet-5-5-r1--c0bd0c29.json), [BE attempt](../../runs/2026-09-29T11-21-04Z--fe-be--anthropic-claude-sonnet-5-5--20260929t112104z-4341d4/attempts/be-claude-sonnet-55-anthropic-claude-sonnet-5-5-r1--02457076.json), [FE evaluation](../../runs/2026-09-29T11-21-04Z--fe-be--anthropic-claude-sonnet-5-5--20260929t112104z-4341d4/evaluations/fe-claude-sonnet-55-anthropic-claude-sonnet-5-5-r1--c0bd0c29/judge-sol6-openai-gpt-6-sol--c1b6048f/output/report.md), [BE evaluation](../../runs/2026-09-29T11-21-04Z--fe-be--anthropic-claude-sonnet-5-5--20260929t112104z-4341d4/evaluations/be-claude-sonnet-55-anthropic-claude-sonnet-5-5-r1--02457076/judge-sol6-openai-gpt-6-sol--fb3e8546/output/report.md), and [run manifest](../../runs/2026-09-29T11-21-04Z--fe-be--anthropic-claude-sonnet-5-5--20260929t112104z-4341d4/run-manifest.json). Run artifacts beneath `runs/` are local and ignored by Git.
