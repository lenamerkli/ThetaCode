The `webpage_to_markdown` loads a webpage and tries to turn them into Markdown. It also tries to display important content. If that is not sufficient for the task, use `curl`.
<tool_call>
<tool_name>bash</tool_name>
<command>/home/agent/software/webpage_to_markdown "https://livebench.ai/"</command>
<timeout>60</timeout>
<max_chars>1000</max_chars>
</tool_call>
<tool_response>
<stdout>
# LiveBench
### A Challenging, Contamination-Free LLM Benchmark
LiveBench appeared as a [Spotlight Paper](https://openreview.net/forum?id=sKYHBTAxVa) in ICLR 2025.  
This work is sponsored by [Abacus.AI](https://abacus.ai)
Leaderboard[Details](https://livebench.ai/#/details)[Code](https://github.com/livebench/livebench)[Data](https://huggingface.co/collections/livebench/livebench-67eaef9bb68b45b17a197a98)[Paper](https://arxiv.org/abs/2406.19314)
## Introduction
Introducing **LiveBench** : a benchmark for LLMs designed with test set contamination and objective evaluation in mind. It has the following properties:
  * LiveBench limits potential contamination by releasing new questions regularly.
  * Each question has verifiable, objective ground-truth answers, eliminating the need for an LLM judge.
  * LiveBench currently contains a set of 23 diverse tasks across 7 categories, and we will release new, harder tasks over time.


**We will evaluate your model on LiveBench!** Open a 
</tool_response>
