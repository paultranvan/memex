"""memex — base de connaissance alimentée par les sessions d'agents."""

__version__ = "0.1.0"

# Toute invite envoyée par le pipeline commence par ce marqueur : les adaptateurs
# ignorent les sessions qui le portent, pour que memex ne s'archive pas lui-même.
PIPELINE_SENTINEL = "[memex:pipeline]"
