"""Offline semantic suite uses small interface stubs if SDK/framework aren't installed.

The native integration tests are separate and should run on Windows with real
Pydantic AI and LoopGrid SDK dependencies installed.
"""
import importlib.util
import sys
import types

if importlib.util.find_spec('loopgrid') is None:
    m = types.ModuleType('loopgrid')
    class LoopGrid:  # pragma: no cover - replaced by fixture client
        pass
    m.LoopGrid = LoopGrid
    sys.modules['loopgrid'] = m

if importlib.util.find_spec('pydantic_ai') is None:
    m = types.ModuleType('pydantic_ai')
    m.__path__ = []
    c = types.ModuleType('pydantic_ai.capabilities')
    from dataclasses import dataclass
    @dataclass
    class AbstractCapability:
        id: str | None = None
        description: str | None = None
        defer_loading: bool = False
        __loopgrid_fake__ = True
        def __class_getitem__(cls, item):
            return cls
    c.AbstractCapability = AbstractCapability
    m.capabilities = c
    sys.modules['pydantic_ai'] = m
    sys.modules['pydantic_ai.capabilities'] = c
