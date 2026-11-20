# -----------------------------------------------------------------------------
# xmlconfig.py - build config schemas from cxml files
# -----------------------------------------------------------------------------
# Copyright 2006-2014 Dirk Meyer, Jason Tackaberry
#
# Originally from kaa.base, ported to Python 3 by Jason Tackaberry
#
# This library is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License version
# 2.1 as published by the Free Software Foundation.
#
# This library is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301 USA
# -----------------------------------------------------------------------------
"""
Builds :class:`~stagehand.toolbox.config.Config` objects from XML schema
(``.cxml``) files.

Older versions generated Python modules from these files at build time.
The schema is now loaded at import time, so the package works directly from
a source checkout.
"""
from __future__ import annotations

__all__ = ['load', 'from_string']

import importlib.resources
from typing import Any
from xml.dom import minidom
from xml.dom.minidom import Element

from .config import Base, Config, Dict, Group, List, Var

_TYPES: dict[str, type] = {'str': str, 'unicode': str, 'int': int, 'float': float, 'bool': bool, 'bytes': bytes}


def _get_value(value: str, type: str | None) -> Any:
    """
    Converts the string value from the XML into a Python value, either of the
    given type, or inferred from the value itself.
    """
    if type:
        if type == 'bool':
            return value.lower() in ('1', 'true')
        if type == 'bytes':
            return value.encode('ascii')
        return _TYPES[type](value) if value else _TYPES[type]()
    if value.lower() == 'true':
        return True
    if value.lower() == 'false':
        return False
    if value.isdigit():
        return int(value)
    if value.replace('.', '').isdigit() and value.count('.') == 1:
        return float(value)
    return value


def _format_content(node: Element) -> str:
    """
    Returns the raw XML of all children of the node, dedented.  This allows
    markup (like HTML) to be used inside description elements.
    """
    s = ''.join(c.toxml() for c in node.childNodes).replace('\t', '        ')
    spaces = ''
    while s:
        if s[0] == ' ':
            spaces += ' '
            s = s[1:]
        elif s[0] == '\n':
            spaces = ''
            s = s[1:]
        else:
            break
    return s.replace('\n' + spaces, '\n').strip()


def _children(node: Element, *names: str) -> list[Element]:
    return [n for n in node.childNodes if isinstance(n, Element) and n.nodeName in names]


_SCHEMA_NODES = ('var', 'group', 'list', 'dict')


def _build(node: Element, module: str | None = None) -> Base:
    kwargs: dict[str, Any] = {}
    if node.getAttribute('name'):
        kwargs['name'] = node.getAttribute('name')
    for desc in _children(node, 'desc'):
        kwargs['desc'] = _format_content(desc)

    kind = node.nodeName
    if kind == 'var':
        defaults: dict[str, str] = {}
        for container in _children(node, 'defaults'):
            for default in _children(container, 'default'):
                defaults[default.getAttribute('platform')] = ''.join(c.toxml() for c in default.childNodes)
        if defaults:
            kwargs['defaults'] = defaults

        default_value = node.getAttribute('default')
        vartype = node.getAttribute('type')
        if default_value or (vartype and not defaults):
            kwargs['default'] = _get_value(default_value, vartype)
        if node.getAttribute('scramblekey'):
            kwargs['scramblekey'] = node.getAttribute('scramblekey')
        for enum in _children(node, 'enum', 'values')[:1]:
            values = []
            for value in _children(enum, 'value'):
                text = value.firstChild
                assert isinstance(text, minidom.Text)
                values.append(_get_value(text.data, value.getAttribute('type')))
            kwargs['type'] = tuple(values)
        return Var(**kwargs)

    schema = [_build(child) for child in _children(node, *_SCHEMA_NODES)]
    if kind == 'config':
        return Config(schema=schema, module=module, **kwargs)
    elif kind == 'group':
        return Group(schema=schema, **kwargs)

    # list or dict
    container_defaults = {s.getAttribute('key'): _get_value(s.getAttribute('value'), None)
                          for s in _children(node, 'set')}
    if container_defaults:
        kwargs['defaults'] = container_defaults
    child_schema: Any = schema[0] if len(schema) == 1 else schema
    if kind == 'dict':
        if node.getAttribute('type'):
            kwargs['type'] = _TYPES[node.getAttribute('type')]
        return Dict(schema=child_schema, **kwargs)
    return List(schema=child_schema, **kwargs)


def from_string(xml: str, module: str | None = None) -> Config:
    """
    Returns a Config object built from the given cxml string.

    :param module: the name of the module the config belongs to, which is
                   recorded in the header of saved config files
    """
    tree = minidom.parseString(xml).documentElement
    if tree is None or tree.nodeName != 'config':
        raise ValueError('not a valid cxml document (root element must be <config>)')
    config = _build(tree, module)
    assert isinstance(config, Config)
    return config


def load(package: str, resource: str, module: str | None = None) -> Config:
    """
    Returns a Config object built from a cxml file shipped inside a package.

    :param package: the package containing the cxml file (e.g. ``stagehand``)
    :param resource: the file name of the cxml (e.g. ``config.cxml``)
    """
    xml = importlib.resources.files(package).joinpath(resource).read_text(encoding='utf-8')
    return from_string(xml, module)
