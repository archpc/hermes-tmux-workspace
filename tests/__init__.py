"""Test package marker: lets `python3 -m unittest discover -s tests -t .` import
test modules on Python 3.9, where discovery requires the start directory to be a
real package (namespace-package discovery only landed in 3.11)."""
