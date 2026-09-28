# Basecamp Bench comparison — 2026-09-24

Claude Opus 5.5 High completed both tracks in one local run. Scores are ordered by frontend plus backend total. Total time sums implementation and evaluation stage durations, including stages that overlapped in wall-clock time. Total cost sums the four recorded attempt costs. Historical rows retain their previously reported figures; the Grok 4.7 and Luna 6 cost figures are lower bounds, and Luna 6's time is approximate.

| Model | Frontend | Backend | Total time | Total cost |
|---|---:|---:|---:|---:|
| Claude Fable 5.1 | 8.183 | 8.965 | 3:04:06 | $92.96 |
| GPT-6 Astra | 7.607 | 9.305 | 1:36:31 | $25.57 |
| Claude Opus 5 | 7.583 | 8.390 | 2:26:31 | $78.11 |
| Claude Fable 5 | 7.578 | 8.392 | 2:06:41 | $85.87 |
| **Claude Opus 5.5 High** | **6.625** | **8.683** | **1:59:02** | **$32.30** |
| Grok 4.6 | 7.104 | 7.926 | 0:57:58 | $10.85 |
| Grok 4.7 High | 6.650 | 7.605 | 4:20:06 | $10.46 |
| Sol 5.6 High | 5.780 | 6.930 | 0:51:21 | $13.94 |
| Sol 6 High | 6.212 | 6.155 | 1:33:42 | $6.73 |
| Luna 6 Max | 5.434 | 6.695 | 5:17:32 | $3.30 |

The Opus 5.5 implementation used `claude-opus-5-5` at high effort; both tracks were evaluated by GPT-6 Sol High. The run manifest and artifacts passed `verify-run`. The backend evaluator checked all 203 canonical operations in-process. Network startup and restart were unverified because its environment blocked sockets. Browser rendering was blocked for the frontend evaluation; the judge also reproduced an HTML injection flaw and a malformed-state startup failure. Scores therefore reflect the saved evaluations, not manual browser acceptance.

Evidence: [FE attempt](../../runs/2026-09-24T02-06-33Z--fe-be--anthropic-claude-opus-5-5--20260924t020633z-c6d2b9/attempts/fe-claude-opus-55-anthropic-claude-opus-5-5-r1--7acacd47.json), [BE attempt](../../runs/2026-09-24T02-06-33Z--fe-be--anthropic-claude-opus-5-5--20260924t020633z-c6d2b9/attempts/be-claude-opus-55-anthropic-claude-opus-5-5-r1--4a4bc92d.json), and [run manifest](../../runs/2026-09-24T02-06-33Z--fe-be--anthropic-claude-opus-5-5--20260924t020633z-c6d2b9/run-manifest.json). Historical source and recovery details are in the [previous comparison](../2026-09-23-sol6-luna6-grok47/comparison.md). Run artifacts beneath `runs/` are local and ignored by Git.
