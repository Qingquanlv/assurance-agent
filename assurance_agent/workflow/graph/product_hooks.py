"""Product hooks the graph kernel invokes. Implementation lives in ``workflow.core``."""

from assurance_agent.workflow.core.product_hooks import (  # noqa: F401
    ProductHooks,
    ProductHooksMissing,
    current_product_hooks,
    install_product_hooks,
    reset_product_hooks,
)
