from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from tradingagents.agents.utils.analysis_context import effective_tool_cutoff
from tradingagents.dataflows.interface import route_to_vendor


@tool
def get_prediction_markets(
    topic: Annotated[
        str,
        "Event topic/keyword, e.g. 'Fed rate cut', 'recession 2026', "
        "'US election', or a sector/company event.",
    ],
    curr_date: Annotated[
        str,
        "Analysis date in YYYY-MM-DD. Historical dates return unavailable "
        "instead of querying a current-only market feed.",
    ],
    limit: Annotated[int | None, "Max markets to return; omit for a default of 6"] = None,
    state: Annotated[dict[str, Any] | None, InjectedState] = None,
) -> str:
    """
    Retrieve live, market-implied probabilities for forward-looking events from
    prediction markets (Polymarket): Fed decisions, recession, elections,
    geopolitics, crypto. Returns the most-traded open markets matching the
    topic, each with its implied probability, traded volume, resolution date,
    and recent move. Uses the configured prediction_markets vendor.

    Args:
        topic (str): Event keyword(s) to search
        curr_date (str): Analysis date in YYYY-MM-DD
        limit (int): Max markets to return; omit for a default of 6

    Returns:
        str: A formatted markdown report of matching prediction markets
    """
    cutoff = effective_tool_cutoff(state, curr_date) or curr_date
    return route_to_vendor("get_prediction_markets", topic, limit, curr_date=cutoff)
