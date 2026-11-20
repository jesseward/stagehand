"""
Configuration schema for this plugin, built from easynews_config.cxml.
"""
from ..toolbox.xmlconfig import load

config = load(__package__, 'easynews_config.cxml', module='stagehand.searchers.config')
