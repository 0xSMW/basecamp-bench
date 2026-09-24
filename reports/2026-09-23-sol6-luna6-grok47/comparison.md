# Basecamp Bench comparison — 2026-09-23

Final comparison: the five highest-scoring prior models, the latest configured GPT-5.6 Sol repetition, and the three completed candidates. Rows are ordered by FE+BE score sum.

| Model | Frontend | Backend | Total time | Total cost |
|---|---:|---:|---:|---:|
| Claude Fable 5.1 | 8.183 | 8.965 | 03:04:06 | $92.960 |
| GPT-6 Astra | 7.607 | 9.305 | 01:36:31 | $25.570 |
| Claude Opus 5 | 7.583 | 8.390 | 02:26:31 | $78.110 |
| Claude Fable 5 | 7.578 | 8.392 | 02:06:41 | $85.870 |
| Grok 4.6 | 7.104 | 7.926 | 00:57:58 | $10.850 |
| Grok 4.7 (High) | 6.650 | 7.605 | 04:20:06 | ≥$10.464 |
| GPT-5.6 Sol (High, latest r5) | 5.780 | 6.930 | 00:51:21 | $13.935 |
| GPT-6 Sol (High) | 6.212 | 6.155 | 01:33:42 | $6.735 |
| GPT-6 Luna (Max) | 5.434 | 6.695 | ~05:17:32 | ≥$3.301 |

The Luna and Grok rows each combine the accepted FE and BE scores as requested. Luna FE is from valid retry `id-47c46a90`; its BE score 6.695 is the contract-weighted result for a Sol Light continuation of the preserved Luna BE source. Grok BE is valid carry-forward `id-944a969a`; its FE score is from a Sol Light continuation of the preserved Grok FE source. Those continuation scores are included in the single model rows, with their changed authorship stated here. Interrupted Luna attempts are excluded by the run's correction sidecars. Both full-lineage time and cost include failed attempts and continuations; costs are lower bounds where a call has no usage receipt. The Luna rescue elapsed time is approximate. Times sum attempt-stage durations (not parallel wall time). Current Codex cost values are token-rate estimates; Grok CLI costs are session-reported, not invoices. No service-tier receipt was available. Historical values use the selected-attempt comparison; GPT-5.6 Sol uses the latest complete run's configured r5 submissions, not the best repetitions. Prior evaluations used GPT-5.6 Sol High; the new evaluations used GPT-6 Sol High. Known smoke/setup spend of $0.32627426 is excluded; one timed-out evaluator smoke has unknown cost.

## Evidence

- Historical baseline: [selected comparison](../september-2026-comparison.json). Latest GPT-5.6 Sol r5 evidence: [FE attempt](../../runs/2026-07-12T12-56-53Z--fe-be--anthropic-claude-sonnet-5_openai-gpt-5-6-sol--20260712t125653z-ebee13/attempts/fe-codex-openai-gpt-5-6-sol-r5--dfc58145.json) and [BE attempt](../../runs/2026-07-12T12-56-53Z--fe-be--anthropic-claude-sonnet-5_openai-gpt-5-6-sol--20260712t125653z-ebee13/attempts/be-codex-openai-gpt-5-6-sol-r5--43852910.json).
- GPT-6 Sol: [FE attempt](../../runs/2026-09-23T09-03-31Z--fe-be--openai-gpt-6-luna_openai-gpt-6-sol_xai-grok-4-7--20260923t090331z-18a02e/attempts/fe-sol6-openai-gpt-6-sol-r1--8462c674.json) and [BE attempt](../../runs/2026-09-23T09-03-31Z--fe-be--openai-gpt-6-luna_openai-gpt-6-sol_xai-grok-4-7--20260923t090331z-18a02e/attempts/be-sol6-openai-gpt-6-sol-r1--9c5e48cd.json).
- GPT-6 Luna: [valid FE attempt](../../runs/2026-09-23T10-11-59Z--fe--openai-gpt-6-luna--20260923t101159z-a53f4e/attempts/fe-luna6-retry-openai-gpt-6-luna-r1--47c46a90.json) and [FE evaluation](../../runs/2026-09-23T10-11-59Z--fe--openai-gpt-6-luna--20260923t101159z-a53f4e/evaluations/fe-luna6-retry-openai-gpt-6-luna-r1--47c46a90/judge-sol6-openai-gpt-6-sol--408d49bc/output/result.json); [BE continuation evaluation](../../runs/2026-09-23T09-03-31Z--fe-be--openai-gpt-6-luna_openai-gpt-6-sol_xai-grok-4-7--20260923t090331z-18a02e/recovery/luna-be-sol-light/assisted-evaluation/output/result.json), [continuation provenance](../../runs/2026-09-23T09-03-31Z--fe-be--openai-gpt-6-luna_openai-gpt-6-sol_xai-grok-4-7--20260923t090331z-18a02e/recovery/luna-be-sol-light/assisted-evaluation/provenance.json), [FE exclusion](../../runs/2026-09-23T09-03-31Z--fe-be--openai-gpt-6-luna_openai-gpt-6-sol_xai-grok-4-7--20260923t090331z-18a02e/corrections/exclusion-id-2ed5113e.json), and [BE exclusion](../../runs/2026-09-23T09-03-31Z--fe-be--openai-gpt-6-luna_openai-gpt-6-sol_xai-grok-4-7--20260923t090331z-18a02e/corrections/exclusion-id-e012a51e.json).
- Grok 4.7: [BE carry-forward attempt](../../runs/2026-09-23T10-43-44Z--be--xai-grok-4-7--20260923t104344z-6182ed/attempts/be-grok47-carry-xai-grok-4-7-r1--944a969a.json); [FE continuation attempt](../../runs/2026-09-23T11-35-19Z--fe--openai-gpt-6-sol--20260923t113519z-ed9bae/attempts/fe-sol-light-grok47-fe-openai-gpt-6-sol-r1--f6503f4b.json) and [FE evaluation](../../runs/2026-09-23T11-35-19Z--fe--openai-gpt-6-sol--20260923t113519z-ed9bae/evaluations/fe-sol-light-grok47-fe-openai-gpt-6-sol-r1--f6503f4b/judge-sol6-openai-gpt-6-sol--407c90ad/output/result.json). Failed attempt and continuation-seed records are preserved under [the canonical full-run directory](../../runs/2026-09-23T09-03-31Z--fe-be--openai-gpt-6-luna_openai-gpt-6-sol_xai-grok-4-7--20260923t090331z-18a02e/).

Historical evaluator-cost adjustments and provenance are documented in [the comparison source data](../september-2026-comparison.json). The exact Grok retry cost receipts and smoke/setup receipts are in the run artifacts and `/tmp/basecamp-bench-pricing-evidence-20260923.md`.
