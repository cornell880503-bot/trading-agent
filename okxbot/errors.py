"""Exception hierarchy for the bot.

Everything that can reject an action raises one of these, so the CLI can
distinguish "the exchange said no" from "our own risk gate said no" from
"the plan was malformed".
"""


class OkxBotError(Exception):
    """Base for every error this package raises."""


class ConfigError(OkxBotError):
    """Missing or contradictory configuration."""


# OKX's authentication errors are terse and several of them describe the same
# symptom ("API key doesn't exist") for very different causes. Each entry here
# names the thing to change, because the code alone sends people re-pasting a
# key that was correct all along.
ERROR_HINTS = {
    "50119": (
        "the key is valid but unknown to THIS host. OKX runs separate regional "
        "entities and an account exists on exactly one of them. Set base_url in "
        "config.yaml to the host your account lives on -- whatever domain your "
        "browser shows when you view the API page (e.g. https://my.okx.com) -- "
        "not the global www.okx.com."
    ),
    "50101": (
        "right host, wrong environment. A live key cannot be used against the "
        "demo endpoint, or the reverse. Demo keys are issued separately, from "
        "the API page while the site is in Demo Trading mode. Note that this "
        "build sends the demo header unless OKX_LIVE_TRADING is set to the "
        "exact opt-in phrase."
    ),
    "50110": (
        "the request's source IP is not on the key's whitelist. Run "
        "'curl -4 -s https://checkip.amazonaws.com' ON THIS MACHINE and compare "
        "it to the key's Trusted IP field. A dual-stack host that prefers IPv6 "
        "will present an address the whitelist has never seen."
    ),
    "50113": (
        "signature mismatch, which almost always means the secret carries "
        "characters you cannot see: a trailing space, or a CRLF line ending "
        "from an editor that saved .env in DOS format."
    ),
    "50114": "the passphrase is wrong. It is the one set when creating the key, not the login password.",
    "50102": (
        "the request timestamp is outside OKX's tolerance, so this machine's "
        "clock has drifted. Fix with 'timedatectl set-ntp true'."
    ),
    "50111": "the API key header itself was rejected -- check OKX_API_KEY for a typo or truncation.",
}


class OkxApiError(OkxBotError):
    """Non-zero ``code`` in an OKX response envelope."""

    def __init__(self, code: str, msg: str, path: str = "", data=None):
        self.code = code
        self.msg = msg
        self.path = path
        self.data = data
        self.hint = ERROR_HINTS.get(str(code))
        text = f"OKX {code} on {path or '<unknown>'}: {msg}"
        if self.hint:
            text += f"\n  hint: {self.hint}"
        super().__init__(text)


class OkxHttpError(OkxBotError):
    """Transport-level failure (status code, timeout, unparseable body)."""


class PlanError(OkxBotError):
    """A TradePlan failed schema or sanity validation."""


class RiskRejection(OkxBotError):
    """The risk gate refused to let an otherwise valid plan through."""

    def __init__(self, reasons):
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))
