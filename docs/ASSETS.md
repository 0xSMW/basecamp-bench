# Asset provenance and release policy

Public release is fail-closed: every bundled non-code reference needs an owner, source, permission/license, modification record, SHA-256 hash, and a `distributable: true` decision in the reference-pack manifest.

| Material | Current owner/source | Permission status | Public release decision |
|---|---|---|---|
| User-provided account screenshots under `Repo/reference/screens/` | Project owner; captures of the project owner's account and data | Fair use | Distributable with source hashes recorded in the reference-pack manifest |
| SDK/OpenAPI material under `Repo/reference/basecamp-sdk/` | Basecamp, LLC; `github.com/basecamp/basecamp-sdk` at commit `cc8e9772e72970c2164d59039bf111179aed0d98` | MIT | Distributable with the upstream copyright/license notice and source hashes |
| `Repo/INIT.md`, `Repo/DESIGN.md`, prompts, contracts, runner, and project documentation | Basecamp Bench contributors | Apache-2.0 | Distributable under the repository license |

The runner must reject publication mode when the selected reference-pack manifest is missing, hash-mismatched, or contains any asset without `distributable: true`. Local mode may use a private pack and must label every result ineligible for public redistribution.
