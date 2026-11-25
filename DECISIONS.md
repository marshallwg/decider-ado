2026-11-25 — Require unchanged scoring inputs and positions when resuming

  Context: Resume attached saved scores by observation position without checking the current inputs, permitting silent misattachment after reordering.
  Decision: Compare exact input strings at every frozen sample position before model calls or replacement of results. Reject invalid checkpoint rows without automatic rescoring. Changes outside the original sample are allowed; empty-memory resume still loads scores directly.
  Alternatives: Persistent identifiers and automatic reordering were deferred to preserve the existing command interface. Comparing sorting variable names cannot establish observation identity. Automatically rescoring damaged rows was rejected because it could incur duplicate charges.
  Reversible? yes
