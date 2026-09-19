"""Exception hierarchy for the bot.

Everything that can reject an action raises one of these, so the CLI can
distinguish "the exchange said no" from "our own risk gate said no" from
"the plan was malformed".
"""


class OkxBotError(Exception):
    """Base for every error this package raises."""


class ConfigError(OkxBotError):
    """Missing or contradictory configuration."""


class OkxApiError(OkxBotError):
    """Non-zero ``code`` in an OKX response envelope."""

    def __init__(self, code: str, msg: str, path: str = "", data=None):
        self.code = code
        self.msg = msg
        self.path = path
        self.data = data
        super().__init__(f"OKX {code} on {path or '<unknown>'}: {msg}")


class OkxHttpError(OkxBotError):
    """Transport-level failure (status code, timeout, unparseable body)."""


class PlanError(OkxBotError):
    """A TradePlan failed schema or sanity validation."""


class RiskRejection(OkxBotError):
    """The risk gate refused to let an otherwise valid plan through."""

    def __init__(self, reasons):
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))
