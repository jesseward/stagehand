"""
Stagehand's web interface, served with aiohttp.
"""
from .jobs import asyncweb, notify, notify_after, webcoroutine
from .server import WebServer, create_app

__all__ = ['WebServer', 'asyncweb', 'create_app', 'notify', 'notify_after', 'server', 'webcoroutine']

#: The process-wide web server instance.
server = WebServer()
