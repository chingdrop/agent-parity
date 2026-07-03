"""agent-parity core pipeline package.

Vendor connectors, AD export parsing, and the pandas correlation engine.
This package is deliberately free of Django and Celery imports so the same
code can run synchronously (management command) or distributed (Celery tasks).
"""
