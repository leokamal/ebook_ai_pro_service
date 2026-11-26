"""Every annotation must evaluate.

Python 3.14 evaluates annotations only on demand, Python 3.11 (production) evaluates them at import. Without
this test, a bad one (e.g. `list[dict]` below a method named `list`) passes on 3.14 and crashes the server.
"""
import importlib
import inspect
import pkgutil

import app


def _functions(module):
    for obj in vars(module).values():
        if getattr(obj, "__module__", None) != module.__name__:
            continue
        if inspect.isfunction(obj):
            yield obj
        elif inspect.isclass(obj):
            yield obj
            for member in vars(obj).values():
                member = getattr(member, "__func__", getattr(member, "fget", member))
                if inspect.isfunction(member):
                    yield member


def test_all_annotations_evaluate():
    broken = []
    for info in pkgutil.walk_packages(app.__path__, "app."):
        for obj in _functions(importlib.import_module(info.name)):
            try:
                inspect.get_annotations(obj)
            except Exception as e:
                broken.append(f"{info.name}.{obj.__qualname__}: {type(e).__name__}: {e}")
    assert broken == []
