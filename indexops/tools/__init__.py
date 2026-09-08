"""Plain, independently testable tool functions. Wrapped as MCP tools in mcp_server.py."""
from indexops.tools.monitoring import get_recent_runs, get_run_metrics, get_pipeline_config, get_index_stats
from indexops.tools.investigation import compare_source_vs_index, sample_mismatched_tickets, get_airflow_task_logs
from indexops.tools.rag import search_knowledge_base
from indexops.tools.remediation import reindex_affected_tickets, trigger_pipeline_rerun

ALL_TOOLS = [
    get_recent_runs,
    get_run_metrics,
    get_pipeline_config,
    get_airflow_task_logs,
    compare_source_vs_index,
    sample_mismatched_tickets,
    get_index_stats,
    search_knowledge_base,
    reindex_affected_tickets,
    trigger_pipeline_rerun,
]

__all__ = [f.__name__ for f in ALL_TOOLS] + ["ALL_TOOLS"]
