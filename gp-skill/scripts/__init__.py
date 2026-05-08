"""A-Stock Advisor Skill - scripts package.

Layers (dependencies only go downward):
  signals    (combines all of below)
  technical, fundamental, risk, paper_trading, backtest  (pure logic)
  fetch_data, broker_adapter, notify                      (external IO)

No module in this package should import anthropic / claude / OpenAI SDKs.
All LLM interaction is the caller's job (usually Claude via SKILL.md).
"""
__version__ = "1.0.0"
