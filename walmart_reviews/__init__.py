"""Walmart review downloader.

This package is intentionally independent from ``amazon_reviews``.  The two
sites have different markup, pagination, blocking behaviour and output
requirements; sharing code would make every Walmart change a risk to the
working Amazon scraper.
"""

__version__ = "1.0.0"

from .models import WalmartPage, WalmartReview
from .store import WalmartStore

__all__ = ["WalmartReview", "WalmartPage", "WalmartStore", "__version__"]
