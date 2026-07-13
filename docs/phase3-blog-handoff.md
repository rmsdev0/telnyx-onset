# Handoff: Phase 3 interruption benchmark blog post

## Mission

Write a technically rigorous, readable blog post about building and validating a
common acoustic benchmark for voice-agent interruption, then comparing a strict
local-VAD trigger with a strict transcript trigger in the same full-duplex
agent.

The post should lead with the measured outcome, but the deeper story is how the
measurement was rescued from provider media limitations, packet-loss ambiguity,
natural-pause false stops, and an initially incomplete classifier. The result is
credible because each failure was preserved and used to strengthen the method,
not hidden or retried away.

Suggested length: 1,800–2,800 words plus a compact methods appendix.

## Current project state

- Phase 2 acoustic-boundary validation: complete.
- Manual waveform review: complete for SIP attempts 5 and 6.
- Independent evidence review: complete.
- Phase 3 strict runtime: complete.
- Live qualification: complete and passed at the final frozen detector.
- Final matched collection: complete, 40 attempts per condition.
- Final preregistered analysis: complete and reproducible.
- Repository validation: 233 tests, Ruff, and strict mypy passed.
- Branch: `duplex`, clean and pushed.
- Latest report commit: `4d51ea1d5aca815fadf6d1abd3c87e910deb6f0e`.
- Pull request: <https://github.com/rmsdev0/telnyx-onset/pull/5>, open and
  mergeable when last checked.

## Reader-facing thesis

Within this controlled SIP harness, scripted fixture, and onset implementation,
making the local VAD decision the sole interruption trigger stopped returned
agent audio a median **640.2 ms earlier** than waiting for the first eligible
transcript. VAD also had the higher observed success rate in the final sample.

This is an implementation- and fixture-specific result. It is not evidence that
every VAD is better than every STT engine, and it is not a physical-device or
speakerphone AEC study.

## Headline final data

All counts are scheduled attempts. Failed and ineligible attempts were never
replaced.

| Condition | Attempted | Eligible | Successful | Success / attempted | Success / eligible |
|---|---:|---:|---:|---:|---:|
| Strict local VAD | 40 | 35 | 32 | 80.0% | 91.4% |
| Strict transcript | 40 | 33 | 28 | 70.0% | 84.8% |

Latency is defined only for successful eligible trials and runs from the
harness-side emitted-stimulus boundary to the harness-side returned-audio stop
boundary.

| Condition | n | Median | IQR | p90 | Range | 95% bootstrap CI for median |
|---|---:|---:|---:|---:|---:|---:|
| Strict local VAD | 32 | 400.7 ms | 359.9–441.2 ms | 501.5 ms | 260.2–720.6 ms | 361.5–440.0 ms |
| Strict transcript | 28 | 1040.9 ms | 856.5–1414.7 ms | 1690.7 ms | 600.9–1999.7 ms | 900.6–1211.5 ms |

Predeclared comparison:

- Design: unpaired, independent trials.
- Direction: transcript minus VAD.
- Median difference: **640.193 ms**.
- Bootstrap: percentile interval for each condition median, 10,000 resamples.
- Analysis seed: `2026071305`.
- No p99 headline and no cross-process clock subtraction.

The sanitized 80-row plotting table is
[`phase3-blog-final-trials.csv`](phase3-blog-final-trials.csv). It contains only
trial id, attempt index, condition, eligibility, success, terminal outcome,
overlapping failure codes, and successful-trial acoustic latency. It excludes
audio, host timestamps, call identifiers, phone numbers, URLs, and secrets.
Its SHA-256 is
`0f91dbfce2c5c905d7c1ee9c98d5b4760b5e1b259d713cc72da9426c7d1e0b74`.

## Local action-path decomposition

The interruption bookkeeping after an eligible trigger was sub-millisecond in
both conditions:

| Agent-local metric | VAD median | Transcript median |
|---|---:|---:|
| Trigger to interruption request | 0.506 ms | 0.495 ms |
| Request to media-epoch invalidation | 0.330 ms | 0.561 ms |
| Request to clear start | 0.357 ms | 0.585 ms |
| Clear send duration | 0.036 ms | 0.046 ms |

The observed acoustic difference is therefore not explained by local
interruption bookkeeping after the trigger. Do not subtract these agent-local
timestamps from harness timestamps; the report intentionally prohibits that
cross-process arithmetic.

## Failure and eligibility data

Failure categories overlap. They must not be summed and described as unique
failed trials.

| Failure category | VAD count | Transcript count |
|---|---:|---:|
| `agent_never_spoke` | 5 | 7 |
| `stimulus_started_without_agent_audio` | 5 | 6 |
| `call_transport_failure` | 3 | 7 |
| `trigger_not_observed` | 0 | 2 |
| `acoustic_stop_not_found` | 0 | 1 |
| `caller_turn_duplicated` | 0 | 1 |
| `caller_turn_lost` | 0 | 1 |
| `instrumentation_failure` | 0 | 1 |
| `stimulus_delivery_failed` | 0 | 1 |

Terminal outcomes are a different dimension from classification:

| Terminal outcome | VAD | Transcript | Total |
|---|---:|---:|---:|
| Clean capture pending classification | 37 | 33 | 70 |
| Post-stimulus echo detected | 1 | 4 | 5 |
| Post-stimulus response not observed | 2 | 2 | 4 |
| Agent audio not observed | 0 | 1 | 1 |

A clean harness terminal does not necessarily mean an eligible or successful
trial. For example, fixture onset could land on an acoustic pause, making the
trial ineligible even though capture completed normally.

The exact 20 final non-success rows and every successful latency are preserved
in the sanitized CSV.

## The narrative arc

### 1. The first measurement surface failed

The original plan tried to use provider media-stream surfaces as the controlled
boundary. Twenty-six bounded live attempts established four controlling
behaviors on this account and topology:

1. A bidirectional stream did not expose a usable provider-outbound track for
   the same leg.
2. Inbound-direction surfaces on the agent leg mirrored the agent's own injected
   audio rather than providing an isolated caller reference.
3. Receive-only streams inherited the leg media context and did not honor the
   hoped-for codec override.
4. A Call Control-answered harness endpoint did not transmit RTP, starving the
   intended delivery-gated topology.

This evidence is in `bench/PHASE2_REPORT.md`, especially the attempt log and
Amendment 1. Do not compress this into “the API was buggy.” The provider
surfaces were behaving consistently; they simply could not satisfy the
registered measurement topology.

### 2. The benchmark moved to an external SIP endpoint

Amendment 1 replaced the provider-stream approximation with a local SIP media
endpoint. The harness placed the call, transmitted PCMU, recorded returned
audio, retained a local transmit reference, and timestamped emission and
capture on one monotonic clock.

The blog can describe this as the point where the benchmark finally measured
the acoustic behavior it claimed to measure.

### 3. The 31st live attempt produced the first reviewable capture

SIP attempt 5, run `p2-96ddebf9e4d6eb36`, was the first
`CAPTURE_COMPLETE_PENDING_REVIEW` across the project's first 31 live attempts.
It also experienced one lost packet out of 1,244. The loss was represented as a
191 ms declared void in the greeting rather than silently concealed or treated
as usable evidence.

Manual review confirmed:

- the void ended 1.12 s before the stop hold;
- it touched no certified boundary;
- transmitted fixture correlation was 0.998;
- there were zero active returned-audio frames during fixture transmission;
- the later response was below the 0.85 fixture-echo threshold;
- transport had zero tx lateness and no anomaly beyond the declared loss void.

This is a strong reliability vignette and a good candidate for the opening or
middle turn of the article.

### 4. Calibration initially appeared complete

Three controls were run under the SIP topology:

- Agent-only baseline: `p2-1de191a7ca4e9f33`.
- No-stimulus control: `p2-f57fc9336a1784c4`.
- Echo control: `p2-fb90d83bd3e307a2`.

The original finite detector grid used seven labeled spans, producing 1,120
records and 84 passing candidates. The preregistered rule selected the simplest
passing threshold and shortest passing hold:

- RMS statistic;
- 20 ms window;
- −42 dBFS activity and silence thresholds;
- 100 ms minimum-active arm;
- 300 ms sustained-silence hold.

Corrected SIP attempt 6, `p2-eba91f36fcd7334f`, then passed delivery,
transport, echo, waveform, manual-review, and independent-review gates.

### 5. Qualification caught false confidence

The 300 ms detector mislabeled a natural pause as a stop in qualification trial
`p3q-020`; old response audio resumed before the transcript-triggered clear.
That campaign was discarded in full.

The false-stop span was pooled as a new condition-independent natural-pause
label. Repeating the unchanged grid over eight labels produced 1,280 records
and 76 passing candidates, selecting 400 ms.

The 400 ms replacement campaign then exposed three more natural-pause false
stops: `p3q-r1b-001`, `p3q-r1b-003`, and `p3q-r1b-008`. It also exposed a
classifier omission: named harness terminal failures did not automatically
produce `call_transport_failure`. The campaign was again discarded in full.

Repeating the same grid over eleven labels produced 1,760 records and 32 passing
candidates. The same selection rule retained every detector setting except the
hold, which advanced to 600 ms.

The key methodological point: recalibration used pooled natural-pause failures,
not between-condition latency effects. Invalid qualification latencies never
entered final analysis.

Calibration evidence hashes:

- Original: `21467eebbbaa842ccacd8be828b0fd5432e3b2d51ae9ad5361a0bd1848e53267`.
- Recalibration 1: `ce3f7a72516227f26194dc526028a1a4c9b309133f6a82af8ad62982e5835d7a`.
- Recalibration 2: `c099ed91a3e97a71b8cc19d5dc0cbea747ae5bfe745534db0d478a46e0397a7d`.

### 6. The final qualification passed

With the 600 ms profile:

- Transcript: 10 attempted, 10 eligible, 10 successful.
- VAD: 10 attempted, 8 eligible, 8 successful.
- Two VAD attempts were predeclared onset-on-pause eligibility collisions.
- Zero stale-audio resumptions, terminal failures, transport/configuration
  faults, duplicate actions, or lost/duplicated turns occurred among eligible
  trials.

Qualification remains excluded from final analysis.

### 7. The final 40+40 result

The final order used balanced-block seed `2026071306`. The conditions shared
runtime, media path, STT, TTS, fixture, detector, clear path, and capture
topology. Their only allowed difference was which observed signal could request
interruption.

The final data are the headline table near the top of this handoff.

## Frozen experimental details

- Synthetic caller fixture text: “I need a table for two.”
- Fixture schedule: 1,000 ms after confirmed active agent playback.
- Frozen natural-end reference: 3,200 ms from active onset.
- Harness mode: external SIP media endpoint.
- Wire audio: PCMU, 8 kHz, mono.
- Analysis audio: PCM16, 16 kHz, mono.
- Frame duration: 20 ms.
- Detector: RMS, −42/−42 dBFS, 100 ms arm, 600 ms hold.
- Fixture correlation echo threshold: 0.85.
- Required fixture delivery: 69 packets / 11,040 bytes.
- Final profile SHA-256:
  `4ccca3cd4b63d32da4d10a302889898b815e2303a7d3f210264fb8cd9cc4d2a2`.
- Canonical fixture SHA-256:
  `b551b31085400d710378f7ff6da10ce46b2b600795e9d61cd7dd12c46f3e2096`.
- Emitted PCMU-derived fixture SHA-256:
  `36b7e68736a59d5bf94fc2a14ab63ec19e2765ad22b9b9eb8135e306fea60eb0`.
- Final session SHA-256:
  `db53852068a58d6787c308d4cad9da5bf64cacb4a819800cf92e9a30f3745b3a`.
- Final analysis SHA-256:
  `fe8d9169d4269629fbab713bf599a6a484043ce940a0ad5dd71debf94d4c814d`.

## Source hierarchy

Use these committed sources first:

1. `bench/PHASE3_REPORT.md` — final qualification and final comparison.
2. `docs/phase3-blog-final-trials.csv` — sanitized plotting table.
3. `bench/measurement_profile.json` — frozen detector, topology, fixture, and
   evidence identifiers.
4. `bench/phase3_final_manifest.json` — frozen order, seeds, profile hash, and
   no-replacement policy.
5. `BENCHMARK_PLAN.md` — preregistered definitions, gates, taxonomy, sample
   policy, and limitations.
6. `bench/PHASE2_REPORT.md` — provider-path failure, SIP rescue, manual review,
   controls, and recalibration history.
7. `bench/SIP_HARNESS_SPEC.md` — detailed harness behavior.
8. `bench/phase3_analysis.py` — exact aggregate and bootstrap implementation.

Local ignored evidence, available in this workspace but not guaranteed in a
fresh clone:

- `bench/artifacts/phase3_final_session.json` — full sanitized final session.
- `bench/artifacts/phase3_final_analysis.json` — exact aggregate output.
- `bench/artifacts/phase3_qualification_session_recalibration2.json` — passing
  qualification session.
- `bench/artifacts/phase3_qualification_analysis_recalibration2.json` — passing
  qualification aggregate.
- `bench/artifacts/calibration_labels.json` — all labeled calibration spans.
- `bench/artifacts/calibration_evidence_recalibration2.json` — all 1,760 final
  calibration records.
- `bench/artifacts/calibration_selection.json` — final selection rule/result.
- Per-call artifact directories referenced by session records — RX/TX WAV,
  frame metadata, harness events, agent events, manifest, and classification.

The local artifact tree is approximately 168 MB. Audio is synthetic but remains
gitignored by policy.

## Existing visual and audio assets

### Corrected reviewed capture, preferred clean waveform figure

- PNG:
  `bench/artifacts/p2-eba91f36fcd7334f/review/waveform_review.png`
- Self-contained HTML with embedded audio:
  `bench/artifacts/p2-eba91f36fcd7334f/review/manual_review.html`
- Extracted summary:
  `bench/artifacts/p2-eba91f36fcd7334f/review/review_summary.json`

### Attempt 5, preferred loss-void reliability figure

- Self-contained HTML with embedded audio:
  `bench/artifacts/p2-96ddebf9e4d6eb36/review/sip_attempt5_review.html`
- Extracted data:
  `bench/artifacts/p2-96ddebf9e4d6eb36/review/review_data.json`
- Portable generators:
  `build_review.py` and `gen_review_html.py` in the same directory.
- Private signed-session copy:
  <https://claude.ai/code/artifact/d2b631a9-3b70-4cd0-b47c-fe78bd4bd0e3>

Do not publish embedded audio automatically. Confirm publication intent first,
even though the speech is synthetic.

## Figures the blog still needs

1. **Latency distribution:** jittered successful-trial points for each condition
   with median, IQR, p90, and median CI. Use the committed CSV. Avoid a bar-only
   chart.
2. **Outcome accounting:** attempted → eligible → successful counts per
   condition, accompanied by terminal-failure counts.
3. **Measurement architecture:** caller/SIP harness → agent → returned audio,
   showing the common harness clock and separate agent-local milestones.
4. **Calibration timeline:** 300 ms → 400 ms → 600 ms, labeling the discarded
   campaigns and pooled natural-pause evidence.
5. **Annotated waveform:** use the corrected attempt-6 PNG for the clean
   boundary explanation, or attempt 5 when discussing the loss void.

Every latency figure must appear with, or immediately next to, attempted,
eligible, successful, and failure counts.

## Suggested article structure

1. **Open on attempt 31.** A packet was missing, yet this was the first capture
   trustworthy enough to review because the loss was declared instead of
   hidden.
2. **State the question.** How much earlier does strict local VAD stop the agent
   than strict transcript arrival when everything else is held fixed?
3. **Explain why the obvious measurement was invalid.** Summarize the 26
   provider-surface attempts and the topology constraint they established.
4. **Introduce the SIP common boundary.** Explain tx reference, returned rx,
   common clock, synthetic fixture, and manual review.
5. **Show qualification doing its job.** Tell the 300 → 400 → 600 ms story and
   why the earlier campaigns were discarded.
6. **Present the final result.** Lead with the 400.7 vs 1040.9 ms medians and
   640.2 ms difference, then success rates and failure accounting.
7. **Interpret carefully.** The post-trigger action path is sub-millisecond;
   trigger availability drives the difference in this implementation.
8. **Close with engineering lessons.** Common boundaries, explicit voids,
   no-replacement attempt counts, qualification before final data, and failure
   taxonomies matter as much as the optimization being tested.
9. **Methods and limitations appendix.** Include frozen settings, seeds, hashes,
   fixture, sample policy, and scope limits.

## Safe claims

- “In this controlled SIP harness, strict local VAD stopped returned agent audio
  a median 640.2 ms earlier than strict transcript triggering.”
- “VAD recorded 32 successful trials out of 40 attempts; transcript recorded 28
  out of 40.”
- “The agent-local work after either eligible trigger was sub-millisecond.”
- “The final detector was selected through a finite condition-independent grid
  and validated in a separate qualification population.”
- “Every scheduled final trial remained in the denominator; failures were not
  replaced.”
- “The common headline metric used two harness-side events on one monotonic
  clock.”

## Claims to avoid

- Do not say VAD is universally 640 ms faster than transcripts.
- Do not generalize to other VAD thresholds, STT engines, voices, fixtures,
  providers, or physical endpoints.
- Do not call the result paired; final trials were independent and unpaired.
- Do not describe the provider as broken. Its surfaces did not satisfy this
  registered measurement topology.
- Do not sum overlapping failure counts as unique failed trials.
- Do not treat clean terminal outcomes as synonymous with eligible or
  successful trials.
- Do not mix qualification latencies with final latencies.
- Do not use historical offleash/provider-boundary latency in the acoustic
  headline table.
- Do not subtract agent-process milestones from harness-process milestones.
- Do not imply the network echo control proves speakerphone or arbitrary-device
  acoustic echo cancellation.
- Do not say the final result is statistically significant unless a new,
  explicitly labeled analysis is performed. The preregistered output provides
  median intervals, not a hypothesis test or bootstrap interval for the median
  difference.

## Reproduction commands

Repository checks:

```bash
cd /Users/rschuetz/Code/telnyx-onset
.venv/bin/ruff check onset bench tests
.venv/bin/mypy onset bench tests
.venv/bin/pytest -q
```

Reproduce the final aggregate from the local ignored session:

```bash
.venv/bin/python -m bench.phase3_analysis \
  --session bench/artifacts/phase3_final_session.json \
  --artifact-root bench/artifacts \
  --output /tmp/phase3-final-analysis-reproduced.json
```

Expected SHA-256 for the canonical stored aggregate is
`fe8d9169d4269629fbab713bf599a6a484043ce940a0ad5dd71debf94d4c814d`.
JSON key formatting may affect a file hash if a different serializer is used;
the in-repo analyzer reproduces the stored object exactly.

## Data handling and publication checks

- The committed CSV and reports are sanitized and safe for drafting.
- Do not print `.env`, Telnyx credentials, phone numbers, call-control ids,
  webhook URLs, or raw server logs into the article.
- Raw audio and detailed artifacts are gitignored.
- Obtain explicit confirmation before publishing any embedded or extracted
  audio, despite its synthetic content.
- Review figure metadata and alt text for local paths, hostnames, or identifiers
  before publication.
- Preserve the distinction between committed aggregate evidence and local
  ignored evidence.

## Title directions

- “The 31st Call: Building an Honest Voice-Agent Interruption Benchmark”
- “VAD vs Transcripts: Measuring Voice-Agent Barge-In on a Common Clock”
- “A 640 ms Interruption Gap, and the Benchmark Work Required to Trust It”
- “Why Our First Voice-Agent Latency Benchmark Was Wrong”

## Open editorial decisions

The next agent can draft without additional benchmark work, but should ask the
maintainer before final publication about:

1. Intended audience: implementation-focused engineers, broader product/voice
   AI readers, or research-methodology readers.
2. Preferred emphasis: benchmark rescue narrative vs final VAD result.
3. Whether Telnyx should be named prominently or described more generically in
   the headline.
4. Whether the synthetic audio clips may be published.
5. Desired length and publication venue.
6. Whether to include full methods inline or as an appendix/repository link.

No new calls, detector changes, trial reclassification, or analysis changes are
needed to begin the blog draft.
