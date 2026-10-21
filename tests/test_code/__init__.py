"""Tests for the new_code open-source collection layer.

Unit tests mock external toolkits (scip-python, radon, tree-sitter) so they
run without installation. Integration tests are marked skipif when the real
toolkit is unavailable.
"""
