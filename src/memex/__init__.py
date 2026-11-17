"""memex — a knowledge base fed by agent sessions."""

__version__ = "0.1.0"

# Every prompt sent by the pipeline starts with this marker: the adapters skip
# the sessions that carry it, so that memex does not archive itself.
PIPELINE_SENTINEL = "[memex:pipeline]"
