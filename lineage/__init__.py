"""lineage — a fleet manager for Windows built on one primitive.

Inventory, patch applicability, compliance, targeting and self-service
eligibility are the same thing: an expression evaluated on the endpoint.
See lineage/relevance.py.
"""

__version__ = "0.1.0"

from .relevance import Relevance, RelevanceError, Version  # noqa: F401

__all__ = ["Relevance", "RelevanceError", "Version", "__version__"]
