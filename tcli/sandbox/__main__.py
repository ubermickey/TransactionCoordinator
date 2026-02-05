"""Entry point: python -m tcli.sandbox [module ...]"""
import sys
from .runner import SandboxRunner

runner = SandboxRunner()
modules = sys.argv[1:] or ["core", "gates", "signatures", "contingencies", "parties", "disclosures", "calendar", "security", "pdf_viewer"]
runner.run(modules)
runner.report()
sys.exit(0 if runner.all_passed else 1)
