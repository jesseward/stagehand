"""
The global Stagehand configuration, built from the schema in ``config.cxml``.
"""
from .toolbox.xmlconfig import load

config = load(__package__, 'config.cxml', module='stagehand.config')

# Importing the plugin packages causes plugins to load and attach their own
# config groups (e.g. config.searchers.easynews).  This must happen after
# ``config`` is defined above, since the plugins import it.
from . import notifiers, retrievers, searchers  # noqa: E402, F401
