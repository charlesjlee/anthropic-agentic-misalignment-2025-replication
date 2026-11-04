# Datacenter replication and newer-model update

Reproduction of the datacenter scenario from Anthropic's 2025 *Agentic Misalignment: How LLMs Could Be Insider Threats*, followed by an update on selected newer models.

- Historical reproduction: four model configurations, six conditions and 100 trials per condition (2,400 valid trials).
- Newer-model update: 15 model configurations under their documented interfaces, six conditions and 100 trials per condition (9,000 valid trials).
- All update model-condition cells scored 0/100 harmful actions under the original automated scoring rule.
- Saved actor/judge responses, frozen settings, failed attempts, labelled replacements and offline verification are included in the data assets.

Download both evidence archives and `SHA256SUMS`. The historical archive extracts into the repository root; the update archive extracts into a separate directory. See `release/README.md` for verification instructions.

The results describe the documented scenario and harnesses. Zero observed harmful actions is not a general safety guarantee. Provider/interface and judge substitutions are recorded in the repository.

Our contributions, including code, documentation and data, are MIT licensed. `LICENSE` accompanies the archives; upstream materials and the original Anthropic figure retain their existing rights.

Client metadata uses portable paths in place of workstation-specific locations. Each archive includes a privacy-redaction record and refreshed checksums. Model-visible prompts, visible responses and original behavioral scores are retained.
