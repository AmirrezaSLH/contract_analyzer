"""Generation: one tool-using agent loop, two finishers.

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
"""

from .client import AnswerUnavailable, Usage, get_client
from .prompts import PromptLibrary, get_prompts

__all__ = ["AnswerUnavailable", "PromptLibrary", "Usage", "get_client", "get_prompts"]
