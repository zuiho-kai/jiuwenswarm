"""Frontend contribution for Team voice meetings; Gateway owns the audio stream."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.extensions.sdk import ApplicationPluginExtension, FrontendContribution


class MeetingApplicationPlugin(ApplicationPluginExtension):
    plugin_id = "meeting"

    async def initialize(self, config: Any) -> None:
        del config

    async def shutdown(self) -> None:
        return None

    def frontend_contributions(self) -> tuple[FrontendContribution, ...]:
        return (FrontendContribution(
            id="meeting", nav_key="app:meeting", title="Team meetings",
            title_i18n_key="meeting.title", render_mode="none", component="meeting", position=76,
        ),)


async def register_extensions(registry: Any) -> list[MeetingApplicationPlugin]:
    extension = MeetingApplicationPlugin()
    registry.register_application_plugin(extension)
    return [extension]
