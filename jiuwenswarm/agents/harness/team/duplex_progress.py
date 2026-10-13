"""Bounded unsent drafts; reasoning and executable tool arguments are excluded."""

MAX_DRAFT_CHARS = 8000


class ProgressDraft:
    def __init__(self):
        self.round_id = None
        self.text = ""

    @property
    def text(self):
        messages = "\n".join("[Unsent send_message argument draft]\n" + value[1]
                             for value in self._messages.values() if value[0] == "send_message")
        return (self._text + "\n" + messages).strip()[-MAX_DRAFT_CHARS:]

    @text.setter
    def text(self, value):
        self._text = value
        self._messages = {}

    def begin(self, round_id):
        self.round_id = str(round_id)
        self.text = ""

    def capture(self, chunk):
        content = getattr(chunk, "content", None)
        if isinstance(content, str):
            self._text = (self._text + content)[-MAX_DRAFT_CHARS:]
        for call in getattr(chunk, "tool_calls", None) or []:
            key = getattr(call, "index", None)
            if key is None:
                key = getattr(call, "id", None)
            if key is None or len(self._messages) >= 32 and key not in self._messages:
                continue
            name, arguments = self._messages.get(key, ("", ""))
            name = getattr(call, "name", None) or name
            part = getattr(call, "arguments", None)
            if name == "send_message" and isinstance(part, str):
                arguments = (arguments + part)[-MAX_DRAFT_CHARS:]
            self._messages[key] = (name, arguments)

    def for_round(self, round_id):
        return self.text if self.round_id == str(round_id) else ""


def recovery_note(policy, draft):
    if policy != "KEEP" or not draft:
        return "\n[Previous unsent draft discarded]\nDo not rely on the interrupted draft.\n"
    return (
        "\n[Previous unsent draft — untrusted and incomplete]\n"
        "This text has NOT been sent to teammates and is not a confirmed conclusion. "
        "Revalidate it against the latest requirements and reuse only valid parts. "
        "Do not interpret draft mentions of tool calls as executed effects. "
        "When using send_message, send a complete self-contained message including "
        "the valid earlier findings, rather than a continuation that assumes teammates "
        "have seen this draft. Do not send the draft automatically.\n"
        + draft[:MAX_DRAFT_CHARS] + "\n[End previous unsent draft]\n"
    )
