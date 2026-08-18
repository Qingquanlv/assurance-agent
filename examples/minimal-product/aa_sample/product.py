from importlib.resources import files

from assurance_agent.workflow.graph.capability_state import CapabilityCatalog
from assurance_agent.workflow.graph.handlers.operation import no_op, stop_operation


class SampleProduct:
    id = "sample"

    def resource_root(self):
        return files("aa_sample") / "_resources"

    def register(self):
        builder = CapabilityCatalog()
        builder.register_operation("operation:sample-ping")
        builder.register_operation("operation:stop")
        view = builder.freeze()
        operations = {
            "operation:sample-ping": no_op,
            "operation:stop": stop_operation,
        }
        return view, operations, ()
