"""Portfolio Manager: synthesises the risk-analyst debate into the final decision.

Uses LangChain's ``with_structured_output`` so the LLM produces a typed
``PortfolioDecision`` directly, in a single call. Its rating is the run's
``final_rating``, and the decision is rendered to markdown as
``final_trade_decision`` for the memory log, CLI display and saved reports.
When a provider does not expose structured output, the agent falls back to
free-text generation and the rating is read from that text.
"""

from __future__ import annotations

from tradingagents.agents.context import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_portfolio_context_from_state,
)
from tradingagents.agents.rating import parse_rating
from tradingagents.agents.schemas import (
    FuturesExecutionIntent,
    PortfolioDecision,
    render_pm_decision,
)
from tradingagents.agents.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)


def create_portfolio_manager(llm):
    structured_llm = bind_structured(llm, PortfolioDecision, "Portfolio Manager")

    def portfolio_manager_node(state) -> dict:
        instrument_context = get_instrument_context_from_state(state)
        portfolio_context = get_portfolio_context_from_state(state)
        execution_context = (state.get("execution_context") or "").strip()
        futures_context = state.get("asset_type") == "crypto" and bool(execution_context)

        history = state["risk_debate_state"]["history"]
        risk_debate_state = state["risk_debate_state"]
        research_plan = state["investment_plan"]
        trader_plan = state["trader_investment_plan"]

        past_context = state.get("past_context", "")
        lessons_line = (
            f"- Lessons from prior decisions and outcomes:\n{past_context}\n"
            if past_context
            else ""
        )
        # Backslash-in-f-string-expression is Python 3.12+ syntax; the repo
        # targets 3.10, so the block is built as a variable.
        futures_context_block = (
            f"**Current Futures Account and Market Context:**\n{execution_context}"
            if futures_context else ""
        )

        prompt = f"""As the Portfolio Manager, synthesize the risk analysts' debate and deliver the final trading decision.

{instrument_context}

{portfolio_context}

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction to enter or add to position
- **Overweight**: Favorable outlook, gradually increase exposure
- **Hold**: Maintain current position, no action needed
- **Underweight**: Reduce exposure, take partial profits
- **Sell**: Exit position or avoid entry

**Context:**
- Research Manager's investment plan: **{research_plan}**
- Trader's transaction proposal: **{trader_plan}**
{lessons_line}
**Risk Analysts Debate History:**
{history}

{futures_context_block}

---

Ground every conclusion in specific evidence from the analysts. The risk debate always contains conflicting stances; deciding which is stronger is the job, so conflict alone is not a reason to Hold. Commit to the stronger case, sized by how decisively it wins. Choose Hold only when the evidence is still balanced after that weighing, or too thin to support a call; do not force a direction to appear decisive. Weigh the analysts on their merits, independent of speaking order.

For a crypto futures decision, review the Trader's execution proposal against the full risk debate and the current account context. The Trader's terms are a proposal, not an instruction: you own the final choice of margin mode, leverage, risk_fraction, margin_fraction, entry, stop-loss, and take-profit.

Authorize futures_execution_intent = enter when you are opening a new futures position and every criterion below holds — the bar is symmetric for longs and shorts:
1. Setup direction: Buy/Overweight is a fresh long and Sell/Underweight is a fresh short; neither rating alone is an entry instruction. The closed 1h and 4h candles in the account context must confirm that direction — a doji or a direction mismatch fails this criterion.
2. Executable bracket: the context's mark price and engine ATR brackets give real levels — the stop must sit on the invalidation side of the entry at prices the venue can fill. If the context's brackets are unavailable, propose your own stop and take-profit together and name the missing bracket as a risk factor — an unavailable bracket is not an automatic abstain.
3. Reward/risk and carry: the distance to the take-profit must justify the stop distance after the funding cost of holding (positive funding penalizes a long, negative penalizes a short); open interest and the funding regime are evidence for or against the entry, and an unavailable metric is named as a risk factor rather than ignored.
4. Account constraints and effective caps: available margin, open positions, and the hard limits listed in the context must leave room for the proposed leverage, risk_fraction, and margin_fraction — propose within those caps, never above them; the execution layer refuses an out-of-limits proposal instead of resizing it.

When all four criteria hold, enter with a complete FuturesExecutionProposal; a qualifying setup must not default to abstain, and being an Underweight or Sell call is not by itself a reason to withhold a short. Set manage_existing when the decision reduces, exits, or protects an existing position without opening a new one. Set abstain only when a criterion above genuinely fails or the account context is missing or insufficient, and say which one. Never put execution terms only in prose: the proposal fields are the instruction, and prose is never parsed into an order. Do not maximize leverage; select it from volatility, stop distance, the instrument's leverage tier, available margin, and expected reward/risk.

## Output

Write these sections, in this order, starting with the rating on its own line:

- **Rating**: exactly one of Buy / Overweight / Hold / Underweight / Sell
- **Executive Summary**: the call and how to act on it
- **Investment Thesis**: the evidence that decided it, and what would change it
- **Futures Execution Intent**: enter / manage_existing / abstain (crypto futures only)

{NO_EXTERNAL_TOOLS}{get_language_instruction()}"""

        decision_holder = {}
        fallback_holder = {}
        final_trade_decision = invoke_structured_or_freetext(
            structured_llm,
            llm,
            prompt,
            render_pm_decision,
            "Portfolio Manager",
            on_result=lambda decision: decision_holder.update(decision=decision),
            on_fallback=lambda reason: fallback_holder.update(reason=reason),
            structured_retry_instruction=(
                "Return the PortfolioDecision schema now. Set futures_execution_intent to "
                "enter, manage_existing, or abstain explicitly. If intent is enter, include "
                "a complete FuturesExecutionProposal; never put execution terms only in prose."
            ) if futures_context else None,
        )
        final_rating = (
            decision_holder["decision"].rating.value
            if decision_holder.get("decision") is not None
            else parse_rating(final_trade_decision)
        )

        new_risk_debate_state = {
            "history": risk_debate_state["history"],
            "aggressive_history": risk_debate_state["aggressive_history"],
            "conservative_history": risk_debate_state["conservative_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "latest_speaker": "Judge",
            "current_aggressive_response": risk_debate_state["current_aggressive_response"],
            "current_conservative_response": risk_debate_state["current_conservative_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }
        decision = decision_holder.get("decision")
        pm_decision_telemetry = {
            "rating": decision.rating.value if decision is not None else None,
            "futures_execution_intent": (
                decision.futures_execution_intent.value
                if decision is not None
                else "structured_terms_unavailable" if futures_context and fallback_holder else "abstain"
            ),
            "futures_execution_proposal_present": bool(
                decision is not None and decision.futures_execution is not None
            ),
            "futures_execution_status": (
                "structured" if decision is not None
                else "fallback_report_only" if futures_context and fallback_holder
                else "not_applicable"
            ),
            "structured_output": decision is not None,
        }

        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": final_trade_decision,
            "final_rating": final_rating,
            "pm_decision_telemetry": pm_decision_telemetry,
            "futures_execution_proposal": (
                decision.futures_execution.model_dump()
                if futures_context
                and decision is not None
                and decision.futures_execution_intent == FuturesExecutionIntent.ENTER
                and decision.futures_execution is not None
                else {}
            ),
            "futures_execution_intent": (
                decision.futures_execution_intent.value
                if futures_context and decision is not None
                else "structured_terms_unavailable" if futures_context and fallback_holder else "abstain"
            ),
            "futures_execution_status": (
                "structured" if futures_context and decision is not None
                else "fallback_report_only" if futures_context and fallback_holder
                else "not_applicable"
            ),
        }

    return portfolio_manager_node
