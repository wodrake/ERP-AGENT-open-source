"""Read configuration from the running graph, not Runtime (which has no config)."""
def current_thread_id(runtime=None) -> str:
    from langgraph.config import get_config

    try:
        config = get_config()
    except RuntimeError:
        # Compatibility for direct middleware tests / older callers.
        config = getattr(runtime, "config", {})
    value = config.get("configurable", {}).get("thread_id") if isinstance(config, dict) else None
    return str(value) if value is not None else ""
