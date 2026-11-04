# Release assets

Download the evidence archives from the [v1.0.0 release](https://github.com/charlesjlee/anthropic-agentic-misalignment-2025-replication/releases/tag/v1.0.0). The [manifest](manifest.json) and [SHA256SUMS](SHA256SUMS) give the asset names, sizes and checksums.

| Asset | Contents |
| --- | --- |
| [Historical evidence](https://github.com/charlesjlee/anthropic-agentic-misalignment-2025-replication/releases/download/v1.0.0/datacenter-historical-2026-10-29.tar.gz) | Historical reproduction, validation/review evidence and source snapshots |
| [Update evidence](https://github.com/charlesjlee/anthropic-agentic-misalignment-2025-replication/releases/download/v1.0.0/datacenter-update-2026-11-03.zip) | 15-model update, raw calls, retries, scores, settings and source snapshots |

Check downloads against `SHA256SUMS` before extraction. These are data archives, separate from GitHub's generated source-code archives. Workstation-specific paths in client metadata are replaced with portable placeholders; each archive includes `privacy-redactions.json` documenting the transformation and refreshed integrity references. Model-visible prompts, visible responses and scores are retained.

- **Historical:** extract into the repository root; experiment paths start with `evidence/`. Then follow the [historical guide](../experiments/historical/README.md).
- **Update:** extract into a separate empty directory so the archived source does not overwrite the current repository. Follow the [update guide](../experiments/update/README.md), passing the extraction directory with `--root`, or verify directly:

```powershell
python experiments/update/run.py verify --archive path/to/datacenter-update-2026-11-03.zip
```

Verification makes no model calls and needs no API keys. Archives retain historical utilities omitted from the current source tree. `LICENSE` accompanies the archive downloads; it contains the MIT grant and upstream copyright notices.

MIT covers our code, documentation, original figures and results. The upstream MIT notices are included in `LICENSE`; the reproduced original Anthropic Figure 11 retains its existing rights.
