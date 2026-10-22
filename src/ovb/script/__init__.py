"""Keyboard-driven UI Automation spatial navigator sources.

These six modules are loaded by ``element_navigation_runtime`` instead of being
imported the normal way, because the same files are exported wholesale into the
standalone ``orthofocus`` project. That export copies them next to each other
and runs them under a different entry point, so they keep sibling imports by
bare module name and must not mention this package.

They therefore are not importable as ``ovb.script.<name>``: importing them that
way would resolve their sibling imports against the package and break the
export. ``element_navigation_runtime`` loads them by file path under the bare
names they expect.
"""
