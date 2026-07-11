# Contributing

Changes must preserve comparability and fail-closed behavior.

1. Open an issue describing the defect or proposed contract change.
2. Keep runner fixes separate from methodology changes.
3. Add unit or fake-harness integration coverage for behavioral changes.
4. Run the full offline test suite and contract/schema validation.
5. Never include credentials, paid-run artifacts, proprietary reference material, or personal absolute paths.

Changing a dimension, weight, anchor, evaluator directive, scoring mapping, or required artifact requires a new contract version and changelog entry. Existing published contracts are immutable. Adapter changes must include output fixtures and prove command redaction, timeout cleanup, and usage parsing. Pull requests that weaken snapshot integrity, schema strictness, publication eligibility, or secret handling will not be accepted.
