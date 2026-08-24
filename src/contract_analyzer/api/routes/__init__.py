"""One module per resource. Each is a thin `parse -> call the library -> shape
the response`; anything a handler would otherwise decide for itself lives a
layer down, where the CLI can reach it too."""

from . import analyses, chat, documents, health, metrics

__all__ = ["analyses", "chat", "documents", "health", "metrics"]
