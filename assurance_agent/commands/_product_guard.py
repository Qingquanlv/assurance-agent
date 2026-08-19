from assurance_agent.workflow.graph.capability_state import current_product_id


def require_assurance_product() -> None:
    product_id = current_product_id()
    if product_id != "assurance":
        raise SystemExit(f"command requires product 'assurance', current product is {product_id!r}")
