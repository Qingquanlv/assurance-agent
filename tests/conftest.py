"""Shared pytest hooks.

The deleted legacy package is no longer selected during collection.
Product tests import assurance_product themselves.
"""

pytest_plugins = (
    "tests.product.composition_harness",
    "tests.product.product_runner",
    "tests.product.cli_support",
    "tests.product.checkpoint_r_support",
)
