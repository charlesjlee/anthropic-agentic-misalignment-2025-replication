# Historical comparison with Table A3

All cells have n=100. Difference = our original-score rate minus published rate, in percentage points. R1* maps provisionally to R1-0528. Source: [Appendix Table A3, pp. 29-30](https://assets.anthropic.com/m/6d46dac66e1a132a/original/Agentic_Misalignment_Appendix.pdf#page=29). See [README Appendix F](../../README.md#appendix-f-reproduce-datacenter-scenario-with-historical-models) for caveats and interpretation.

| Model | Series | Conflict + replacement | Conflict + restriction | Conflict only | Replacement only | Restriction only | Control |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| GPT-4.1 | Published A3 | 54% | 69% | 33% | 0% | 0% | 0% |
| GPT-4.1 | Our run | 50% | 55% | 24% | 0% | 0% | 0% |
| GPT-4.1 | **Difference (pp)** | **-4** | **-14** | **-9** | **0** | **0** | **0** |
| GPT-4o | Published A3 | 38% | 42% | 21% | 32% | 27% | 19% |
| GPT-4o | Our run | 39% | 23% | 6% | 2% | 4% | 0% |
| GPT-4o | **Difference (pp)** | **+1** | **-19** | **-15** | **-30** | **-23** | **-19** |
| DeepSeek R1* | Published A3 | 94% | 89% | 90% | 38% | 53% | 1% |
| DeepSeek R1* | Our run | 97% | 95% | 95% | 40% | 45% | 1% |
| DeepSeek R1* | **Difference (pp)** | **+3** | **+6** | **+5** | **+2** | **-8** | **0** |
| Llama 4 Maverick | Published A3 | 86% | 76% | 58% | 56% | 70% | 25% |
| Llama 4 Maverick | Our run | 40% | 42% | 21% | 10% | 7% | 1% |
| Llama 4 Maverick | **Difference (pp)** | **-46** | **-34** | **-37** | **-46** | **-63** | **-24** |
