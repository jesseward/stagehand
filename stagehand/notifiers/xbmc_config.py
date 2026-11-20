"""
Configuration schema for this plugin, built from xbmc_config.cxml.
"""
from ..toolbox.xmlconfig import load

config = load(__package__, 'xbmc_config.cxml', module='stagehand.notifiers.config')
