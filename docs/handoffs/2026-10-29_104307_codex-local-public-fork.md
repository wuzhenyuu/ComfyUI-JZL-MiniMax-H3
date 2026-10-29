# Local Codex adapter: public fork handoff

- Date: 2026-10-29 (Asia/Shanghai).
- Upstream baseline: wjluoxiao/ComfyUI-JZL-MiniMax-H3 at 66b8550e37783c0745e672cf43de7f28b7265224.
- Fork branch: `codex/local-codex-20261029`; package version: `0.9.09`.
- Status: additive implementation and local checks completed. Synchronization is identified by the commit containing this document.

## Scope and behavior

Adds a separately selected ChatGPT-login Codex text provider, a native image generation/editing node, and CLI model discovery in both text configuration dialogs. Existing providers, defaults, workflow input order, H3 model loading and precision are preserved. The original MIT license and author notices remain in place.

The optional Codex route depends on a compatible sibling `comfyui-codex-cli-nodes` execution layer. This fork does not redistribute that layer and currently provides no public automatic installer for it. Codex functionality is unavailable without the layer; existing providers remain usable. Codex configuration loading requires Python 3.11+, while the original plugin requirement remains Python 3.10+.

## Verification

- Real local Codex CLI 0.162.1: one text request and one two-reference native image request with gpt-6.1-sol/high completed. The image was 1536x1024 with matching IMAGE/MASK tensors.
- Real CLI app-server model/list and an isolated CPU ComfyUI instance: both text dialogs discover a selectable model list; refresh, save/reopen, unlisted saved values, and existing HTTP values were checked.
- Offline V3 schema, reference ordering/batch expansion, dimensions, alpha masks, invalid output/error/cancellation contracts, catalog pagination/cache/error/EOF cleanup, Python/JavaScript parsing and diff checks passed.
- These checks do not certify every listed model, image permission, exact prompt compliance, a clean installation without the external execution layer, or the complete Windows child-process tree/remote cancellation. No H3 inference was required for this adapter verification.

## Publication boundary

Only reviewed source, usage documentation, package version and this handoff are added. Local runtime configuration changes, downloaded runtimes, credentials, caches, private state, screenshots and test outputs are excluded. This branch is not a Comfy Registry release and is not a pull request to the upstream author.

## Usage and next steps

See [CODEX_LOCAL.md](../CODEX_LOCAL.md). Reload ComfyUI and refresh the browser after updating. Select Codex explicitly for text, or add the independent image node. Model discovery does not generate model output; generation uses the logged-in account's online service and quota. Doubao desktop support remains a separate feasibility study and is not implemented in this branch.
