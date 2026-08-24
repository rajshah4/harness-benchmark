# Security and privacy

Do not commit model-provider credentials, Agent Canvas secrets, Laminar keys, session files, or unsanitized conversation exports.

The published provider ledgers intentionally exclude prompts and responses. The raw run records have conversation identifiers and machine-specific paths removed.

Published traces pass through `scripts/export_traces.py`, which redacts credential-bearing fields, common token formats, authorization values, secret assignments, and machine-specific paths. Canvas deployment metadata is not published.

If you find a credential or private identifier in this repository, report it privately to the repository owner before opening a public issue.
