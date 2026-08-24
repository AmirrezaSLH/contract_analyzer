"""Generation: one tool-using agent loop, two finishers, three agents.

The model drives retrieval itself. It is given `search_contract` and
`get_section`, writes its own queries, picks the mode and the depth, and
loops until it has enough evidence or a hard cap stops it. Everything a tool
returns lands in an evidence ledger (`E1`, `E2`, ...), which is what the two
surfaces are built from:

* **analysis** (`analysis.py`) -- a structured `ComplianceResult`, its quotes
  verified deterministically against the ledger;
* **chat** (`chat.py`) -- a streamed, cited answer whose quotes are extracted
  by the API from the ledger's passages.

They cannot end in the same kind of request: `output_config.format` and
citations are incompatible (a 400), which is why there are two finishers and
not one. The loop, the tools, the client, the prompt library and the caps are
shared. See docs/generation.md.

The analysis surface is three named agents rather than one call, and
`docs/agents/` is the map:

* the **Analyzer** (`agent.py`, `analysis.py`, `tools.py`) searches, drafts
  and corrects its own structure;
* the **Evaluator** (`evaluator.py`) is handed the quotes and the claims and
  nothing else, and judges whether the evidence carries them;
* the **Router** (`router.py`) is what the harness calls: it invokes the other
  two, decides what happens next, and is the reason the critic's view is
  narrow enough to be worth having.

Chat is untouched by any of it -- evaluation is a compliance-analysis concern.
"""

from .analysis import analyze_criterion
from .chat import AnswerResult, chat
from .client import AnswerUnavailable, Usage, get_client
from .evaluator import EvaluationFailed, evaluate
from .prompts import PromptLibrary, get_prompts
from .router import cross_criterion_check, route_criterion

__all__ = [
    "AnswerResult",
    "AnswerUnavailable",
    "EvaluationFailed",
    "PromptLibrary",
    "Usage",
    "analyze_criterion",
    "chat",
    "cross_criterion_check",
    "evaluate",
    "get_client",
    "get_prompts",
    "route_criterion",
]
