"""file_check backend application-module directory.

Official entry points add this directory to ``sys.path`` and import one shared
top-level module identity.  ``backend.*`` package imports are intentionally not
a supported runtime topology because duplicating ``decision_store`` would split
process-local mutation locks and authorization receipts.
"""
