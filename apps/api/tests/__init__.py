"""API test suite.

A package (rather than loose modules) so pytest imports ``conftest`` exactly
once, as ``tests.conftest``. Without this the fixtures and the modules that do
``from tests.conftest import ...`` end up with two separate copies of the
module - and of the shared route-coverage recorder.
"""
