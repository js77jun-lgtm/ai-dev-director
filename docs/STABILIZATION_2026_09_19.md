# AI DEV stabilization — 2026-09-19

The active ProposalBuilder now includes baseline source files even when they are write-protected. Explicit DOCX/Markdown/text references are extracted before invoking the CLI. Missing references fail with a concrete path/error. Omission from context is not evidence that a file does not exist.

Invocation evidence resets before each Builder call. Failed attempts and successful retry counts are retained. The active proposal runner retries an output-token-limit failure once using a complete compact response instruction. Empty responses retain the existing two-attempt bound.

Orders support required_outputs and reference_paths. HEAD prompts request those fields. Missing nonempty deliverable files make verification fail and flow through the existing independent review and repair loop. File existence does not substitute for behavior testing or review.

Manual role readiness gets one fresh probe before stopping. No provider roles are silently exchanged. scripts/retry_transport_run.py RUN-ID explicitly reopens eligible CLI failures and runs the existing resume pipeline; scope/review failures cannot use this route. Historical failure transitions remain intact.

Legacy recovery.json no longer overrides FAIL with a successful display state. Its evidence is shown as manual recovery with full-pipeline revalidation outstanding.

Validation includes protected-source input, real DOCX extraction, missing reference handling, stale evidence replacement, output-limit retry, and rejection of scope-failure reopening. The actual sample report referenced by the failed order extracts to 1,859 characters.

Limitations: large code changes still use full-file JSON proposals; output-limit retries can exhaust. Required outputs must be declared by HEAD. Existing TRIAD ACTIVE records do not prove a live worker. The failed product run has not been promoted to PASS by these system changes.
