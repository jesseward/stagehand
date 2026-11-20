"""
Configuration schema for this plugin, built from email_config.cxml.
"""
from ..toolbox.xmlconfig import load

config = load(__package__, 'email_config.cxml', module='stagehand.notifiers.config')
