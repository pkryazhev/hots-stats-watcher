#!/usr/bin/env python
#
# Copyright 2015-2020 Blizzard Entertainment. Subject to the MIT license.
# See the included LICENSE file for more information.
#

import importlib.util
import os
import re
import sys


def _import_protocol(base_path, protocol_module_name):
    """
    Import a module from a base path, used to import protocol modules.

    `imp` (used by the original Blizzard implementation) was removed in
    Python 3.12, so this uses importlib instead while keeping the same
    "import protocolXXXXX.py from an arbitrary directory" behavior.
    """

    # Try to return the module if it's been loaded already
    if protocol_module_name in sys.modules:
        return sys.modules[protocol_module_name]

    file_path = os.path.join(base_path, protocol_module_name + ".py")
    if not os.path.isfile(file_path):
        # Matches the ImportError imp.find_module() used to raise when the
        # module couldn't be found - callers (resolve_protocol) rely on this.
        raise ImportError("No module named {0!r}".format(protocol_module_name))

    spec = importlib.util.spec_from_file_location(protocol_module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError("No module named {0!r}".format(protocol_module_name))

    module = importlib.util.module_from_spec(spec)
    sys.modules[protocol_module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        del sys.modules[protocol_module_name]
        raise
    return module


def list_all(base_path=None):
    """
    Returns a list of current protocol version file names in the versions module sorted by name.
    """
    if base_path is None:
        base_path = os.path.dirname(__file__)
    pattern = re.compile(r'protocol[0-9]+\.py$')
    files = [f for f in os.listdir(base_path) if pattern.match(f)]
    files.sort()
    return files


def latest():
    """
    Import the latest protocol version in the versions module (directory)
    """
    # Find matching protocol version files
    base_path = os.path.dirname(__file__)
    files = list_all(base_path)

    # Sort using version number, take latest
    latest_version = files[-1]

    # Convert file to module name
    module_name = latest_version.split('.')[0]

    # Perform the import
    return _import_protocol(base_path, module_name)


def build(build_version):
    """
    Get the module for a specific build version
    """

    base_path = os.path.dirname(__file__)
    return _import_protocol(base_path, 'protocol{0:05d}'.format(build_version))
