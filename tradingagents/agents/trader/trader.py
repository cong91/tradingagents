"""Trader: turns the Research Manager's investment plan into a concrete transaction proposal."""

from __future__ import annotations

from langchain_core.messages import AIMessage

from tradingagents.agents.context import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_portfolio_context_from_state,
)
from tradingagents.agents.schemas import TraderProposal, render_trader_proposal
from tradingagents.agents.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)


def create_trader(llm):
    structured_llm = bind_structured(llm, TraderProposal, "Trader")

    def trader_node(state):
        company_name = state["company_of_interest"]
        instrument_context = get_instrument_context_from_state(state)
        investment_plan = state["investment_plan"]
        # The research plan digests the debate but loses exact price structure;
        # give the Trader the technical market report so entry/stop levels are
        # grounded in real ATR / support-resistance / current price (#1167). The
        # report is empty when the user did not select the market analyst, so
        # only offer it (and the grounding instruction) when it has content.
        market_report = (state["market_report"] or "").strip()
        portfolio_context = get_portfolio_context_from_state(state)
        execution_context = (state.get("execution_context") or "").strip()
        futures_context = state.get("asset_type") == "crypto" and bool(execution_context)

        if market_report:
            grounding = (
                "Ground concrete price levels (entry, stop-loss, position sizing) in the technical "
                "market report's price structure -- current price, support/resistance, ATR, and "
                "volatility -- and use the research plan for direction and strategy. "
            )
            report_section = f"Technical Market Report:\n{market_report}\n\n"
        else:
            grounding = ""
            report_section = ""

        # A backslash inside an f-string expression is only valid on Python 3.12+;
        # the repo targets 3.10, so the block is built as a variable.
        futures_context_block = (
            f"Futures Account and Market Context:\n{execution_context}\n\n"
            if futures_context else ""
        )

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a trading agent analyzing market data to make investment decisions. "
                    "Based on your analysis, provide a specific recommendation to buy, sell, or hold. "
                    + grounding
                    # Entry/stop are numeric price fields. Asking for concrete
                    # levels invites a percentage ("15%"), which is not a price
                    # and fails the structured parse (#1288).
                    + "State entry price and stop-loss as absolute price levels in the "
                    "instrument's quote currency (for example 189.5), never a percentage "
                    "or a range; convert a percentage distance to the price level it "
                    "implies, or omit the field if you cannot state a number. "
                    + (
                        "For crypto futures, propose margin mode, leverage, risk budget, and "
                        "margin budget in a FuturesExecutionProposal, plus entry/stop/take-profit. "
                        "Justify the choice using the current account, volatility, stop distance, "
                        "funding, and reward/risk; keep every term inside the hard limits the "
                        "account context lists. A Buy/Overweight plan is a long proposal and a "
                        "Sell/Underweight plan is a short proposal when the closed candles confirm "
                        "the direction. Do not maximize leverage; if a required context item is "
                        "missing, omit the proposal, say which item was missing, and let the "
                        "Portfolio Manager abstain on your terms rather than inventing numbers. "
                        if futures_context else ""
                    )
                    + NO_EXTERNAL_TOOLS
                    + get_language_instruction()
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Here is the research team's investment plan for {company_name}. "
                    f"{instrument_context}\n\n"
                    f"{report_section}"
                    f"{portfolio_context}\n\n"
                    f"{futures_context_block}"
                    f"Proposed Investment Plan:\n{investment_plan}\n\n"
                    "Make an informed, strategic trading decision.\n\n"
                    "## Output\n\n"
                    "Write these sections, in this order, starting with the action "
                    "on its own line:\n\n"
                    "- **Action**: exactly one of Buy / Hold / Sell. A research "
                    "recommendation of Overweight is a Buy and Underweight is a Sell, "
                    "sized by how strong the case is; conflict alone is not a Hold. "
                    "A Sell here proposes shorting the futures contract — the Portfolio "
                    "Manager still authorizes the entry explicitly.\n"
                    "- **Reasoning**: why, against the plan and the price structure\n"
                    "- **Entry Price**, **Stop Loss**, **Position Sizing**: when you can state them"
                    + (
                        "\n- **Futures Execution Proposal**: margin mode, leverage, risk_fraction, "
                        "margin_fraction, entry, stop, target, and evidence-based rationale"
                        if futures_context else ""
                    )
                ),
            },
        ]

        decision_holder = {}
        fallback_holder = {}
        trader_plan = invoke_structured_or_freetext(
            structured_llm,
            llm,
            messages,
            render_trader_proposal,
            "Trader",
            on_result=lambda decision: decision_holder.update(decision=decision),
            on_fallback=lambda reason: fallback_holder.update(reason=reason),
            structured_retry_instruction=(
                "Return the TraderProposal schema now. Do not answer with prose outside the "
                "schema fields; if futures context exists, set futures_execution_intent explicitly."
            ) if futures_context else None,
        )
        decision = decision_holder.get("decision")
        trader_decision_telemetry = {
            "action": decision.action.value if decision is not None else None,
            "futures_execution_intent": (
                decision.futures_execution_intent.value
                if decision is not None
                else "structured_terms_unavailable" if futures_context and fallback_holder else "abstain"
            ),
            "futures_execution_proposal_present": bool(
                decision is not None and decision.futures_execution is not None
            ),
            "structured_output": decision is not None,
        }

        return {
            "messages": [AIMessage(content=trader_plan)],
            "trader_investment_plan": trader_plan,
            "trader_decision_telemetry": trader_decision_telemetry,
        }

    return trader_node
