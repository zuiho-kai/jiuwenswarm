"""Human floor-taking decisions through the existing duplex supervisor provider."""

from __future__ import annotations

import json
import logging

from openjiuwen.agent_teams.duplex.classifier import build_classifier
from openjiuwen.agent_teams.duplex.config import DuplexConfig
from openjiuwen.agent_teams.duplex.policy import INTERRUPT, INTERRUPT_THRESHOLD, URGENCY

logger = logging.getLogger(__name__)


async def is_human_interruption(text: str, speaker, config: DuplexConfig | None) -> bool:
    """A failed/uncertain judgment preserves speech and still admits the human text."""
    classifier = None
    try:
        # Reuse Team endpoint/model/key settings even when teammate delivery
        # filtering is off. An unconfigured Team uses the shared JEV defaults.
        if config is None or (config.format != "jev" and not config.api_base):
            config = DuplexConfig(format="jev")
        classifier = build_classifier(config)
        context = json.dumps({
            "activity": (
                "A live voice meeting. Ongoing work is the current agent's spoken explanation. "
                "A human correction, substantive question, disagreement, request to stop, or "
                "change of topic requires yielding the floor before speech continues. "
                "Listening acknowledgements such as 'mm-hmm', 'okay', '嗯', '好的', coughs, "
                "background speech, and requests explicitly for later do not require changing "
                "the ongoing explanation. Judge meaning, not just the presence of a voice."
            ),
            "speaker": speaker.member_name,
            "spoken_summary": speaker.speech_text,
        }, ensure_ascii=False)
        answers = await classifier.classify([URGENCY], context, [f"user: {text}"])
        answer = answers[URGENCY]
        return answer.choice == INTERRUPT and answer.probabilities.get(INTERRUPT, 0) >= INTERRUPT_THRESHOLD
    except Exception:
        logger.warning("Meeting interruption judgment unavailable; retaining current speech", exc_info=True)
        return False
    finally:
        if classifier is not None:
            try:
                await classifier.aclose()
            except Exception:
                logger.warning("Meeting interruption classifier cleanup failed", exc_info=True)
