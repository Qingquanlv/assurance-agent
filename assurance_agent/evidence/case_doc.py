from importlib import import_module
import sys

sys.modules[__name__] = import_module("assurance_kernel.evidence.case_doc")
