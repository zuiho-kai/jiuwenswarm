import { useTranslation } from "react-i18next";
import {
  AudioLines,
  ChevronDown,
  ListChecks,
  MessageSquareText,
  Square,
  UsersRound,
  SkipForward,
} from "lucide-react";
import { useSessionStore } from "../../../channels/web/frontend/src/stores/sessionStore";
import { resolveTeamLeaderDisplayName } from "../../../channels/web/frontend/src/features/teamLeaderIdentity";
import {
  cancelMeetingQueued,
  stopMeeting,
  stopMeetingAudio,
  useMeetingSnapshot,
} from "./taskMeetingStore";
import "./MeetingPanel.css";

export function MeetingPanel({ sessionId }: { sessionId: string | null }) {
  const { t, i18n } = useTranslation();
  const snapshot = useMeetingSnapshot();
  const runtime = useSessionStore((state) => state.runtimes[sessionId || ""]);
  const ownsMeeting = !!sessionId && snapshot.sessionId === sessionId;
  const state = ownsMeeting ? snapshot.state : "idle";
  const active = state === "active";
  const canEnd = ["starting", "active", "cleanup_failed"].includes(state);
  const leader =
    resolveTeamLeaderDisplayName(runtime?.teamLeaderIdentity, i18n.language) ||
    t("meeting.leader");
  const contextName = (name: string) =>
    runtime?.teamLeaderMemberIds.includes(name)
      ? leader
      : runtime?.teamMembers.find((item) => item.member_id === name)?.name ||
        name;
  const addressedAgents = snapshot.addressedMembers.map(
    (name) => `@${contextName(name)}`,
  );
  const speaking = active && snapshot.playback === "playing";
  const actions = snapshot.queue.actions || [];

  return (
    <section className="meeting-panel" data-testid="team-area-meeting-panel">
      <div className="meeting-panel__content">
        <header className="meeting-panel__header">
          <div className="meeting-panel__heading">
            <div className="meeting-panel__mark">
              <UsersRound size={20} aria-hidden="true" />
            </div>
            <h2 data-testid="team-area-meeting-title">
              {t("meeting.panel.title")}
            </h2>
            <span
              className="meeting-panel__status"
              role="status"
              data-testid="team-area-meeting-status"
              data-variant={state}
            >
              <i aria-hidden="true" />
              {t(`meeting.panel.states.${state}`)}
            </span>
          </div>
          {state !== "idle" && (
            <button
              type="button"
              className="meeting-panel__button meeting-panel__end"
              disabled={!canEnd}
              onClick={stopMeeting}
              data-testid="team-area-meeting-end-btn"
            >
              <Square size={13} aria-hidden="true" />
              {t(
                state === "cleanup_failed"
                  ? "meeting.task.retryCleanup"
                  : state === "closing"
                    ? "meeting.task.closing"
                    : "meeting.task.stop",
              )}
            </button>
          )}
        </header>

        {state === "idle" ? (
          <div
            className="meeting-panel__empty"
            data-testid="team-area-meeting-empty"
          >
            <UsersRound size={32} aria-hidden="true" />
            <p>{t("meeting.panel.empty")}</p>
          </div>
        ) : (
          <>
            <section
              className="meeting-panel__stage"
              data-testid="team-area-meeting-details"
              data-variant={speaking ? "speaking" : "waiting"}
            >
              <div className="meeting-panel__stage-top">
                <span
                  className="meeting-panel__eyebrow"
                  data-testid="team-area-meeting-playback-label"
                >
                  {t("meeting.panel.playback")}
                </span>
                <span
                  className="meeting-panel__microphone-state"
                  data-variant={active ? snapshot.recording : state}
                  data-testid="team-area-meeting-microphone"
                >
                  <i aria-hidden="true" />
                  {active
                    ? t(`meeting.panel.microphoneStates.${snapshot.recording}`)
                    : t(`meeting.panel.states.${state}`)}
                </span>
              </div>
              <div className="meeting-panel__speaker-row">
                <div className="meeting-panel__speaker-icon">
                  <AudioLines size={28} aria-hidden="true" />
                </div>
                <div
                  className="meeting-panel__speaker"
                  role="status"
                  data-testid="team-area-meeting-playback"
                >
                  {snapshot.playback === "idle"
                    ? t("meeting.panel.noSpeaker")
                    : t(`meeting.playback.${snapshot.playback}`, {
                        speaker: snapshot.speaker,
                      })}
                </div>
                <button
                  type="button"
                  className="meeting-panel__button"
                  disabled={!active || snapshot.playback === "idle"}
                  onClick={stopMeetingAudio}
                  data-testid="team-area-meeting-stop-audio-btn"
                >
                  <SkipForward size={15} aria-hidden="true" />
                  {t("meeting.skipSpeech")}
                </button>
              </div>
              <dl className="meeting-panel__routing">
                <div>
                  <dt data-testid="team-area-meeting-speech-route-label">
                    {t("meeting.panel.speechRoute")}
                  </dt>
                  <dd data-testid="team-area-meeting-speech-route">
                    {snapshot.routingMode === "leader"
                      ? `@${leader}`
                      : t("meeting.routing.none")}
                  </dd>
                </div>
                <div>
                  <dt data-testid="team-area-meeting-addressed-agents-label">
                    {t("meeting.panel.addressedAgents")}
                  </dt>
                  <dd data-testid="team-area-meeting-addressed-agents">
                    {addressedAgents.length
                      ? addressedAgents.join(", ")
                      : t("meeting.panel.noAddressedAgents")}
                  </dd>
                </div>
              </dl>
            </section>

            {state === "cleanup_failed" && (
              <p
                className="meeting-panel__warning"
                role="alert"
                data-testid="team-area-meeting-cleanup-error"
              >
                {t("meeting.errors.cleanupFailed")}
              </p>
            )}

            {snapshot.queue.decision && (
              <section
                className="meeting-panel__decision"
                data-testid="team-area-meeting-decision"
              >
                <h3 data-testid="team-area-meeting-decision-title">
                  {t("meeting.currentDecision")}
                </h3>
                <p>{snapshot.queue.decision}</p>
              </section>
            )}

            <section
              className="meeting-panel__transcript-section"
              data-testid="team-area-meeting-transcript-section"
            >
              <h3 data-testid="team-area-meeting-transcript-title">
                <MessageSquareText size={16} aria-hidden="true" />
                {t("meeting.panel.transcript")}
              </h3>
              <p
                className="meeting-panel__transcript"
                data-testid="team-area-meeting-transcript"
                data-variant={snapshot.lastTranscript ? "received" : "empty"}
              >
                {snapshot.lastTranscript || t("meeting.panel.waitingForSpeech")}
              </p>
            </section>

            {actions.length > 0 && (
              <section
                className="meeting-panel__actions"
                data-testid="team-area-meeting-actions"
              >
                <div className="meeting-panel__section-heading">
                  <h3 data-testid="team-area-meeting-actions-title">
                    <ListChecks size={16} aria-hidden="true" />
                    {t("meeting.actionsTitle")}
                    <span className="meeting-panel__count">
                      {actions.length}
                    </span>
                  </h3>
                  <span
                    className="meeting-panel__meta"
                    data-testid="team-area-meeting-action-phase"
                  >
                    {t(
                      `meeting.actionPhase.${snapshot.queue.phase || "execution"}`,
                    )}
                  </span>
                </div>
                <div
                  className="meeting-panel__action-list"
                  data-testid="team-area-meeting-action-list"
                >
                  {actions.map((action) => (
                    <details
                      key={action.action_id}
                      className="meeting-panel__action"
                      data-testid="team-area-meeting-action"
                      data-variant={action.action_id}
                    >
                      <summary
                        data-testid="team-area-meeting-action-summary"
                        data-variant={action.action_id}
                      >
                        <span className="meeting-panel__action-heading">
                          <span>{action.goal}</span>
                          <small>@{contextName(action.owner)}</small>
                        </span>
                        <span
                          className="meeting-panel__action-status"
                          data-variant={action.status}
                        >
                          {t(`meeting.actionStatus.${action.status}`)}
                        </span>
                        <ChevronDown
                          size={14}
                          className="meeting-panel__chevron"
                          aria-hidden="true"
                        />
                      </summary>
                      <div className="meeting-panel__action-body">
                        <p>
                          {t("meeting.actionReviewer")}: @
                          {contextName(action.reviewer)}
                        </p>
                        <p>
                          {t("meeting.actionAcceptance")}: {action.acceptance}
                        </p>
                        {action.evidence.map((entry, index) => (
                          <p key={index}>{entry}</p>
                        ))}
                        {action.note && <p>{action.note}</p>}
                      </div>
                    </details>
                  ))}
                </div>
              </section>
            )}

            <section
              className="meeting-panel__queue"
              data-testid="team-area-meeting-queue"
            >
              <h3 data-testid="team-area-meeting-queue-title">
                <AudioLines size={16} aria-hidden="true" />
                {t("meeting.queueTitle", {
                  count: snapshot.queue.pending.length,
                })}
              </h3>
              <p
                className="meeting-panel__hint"
                data-testid="team-area-meeting-delivery-note"
              >
                {t("meeting.textDeliveredNote")}
              </p>
              {snapshot.queue.pending.length === 0 && (
                <p
                  className="meeting-panel__queue-empty"
                  data-testid="team-area-meeting-queue-empty"
                >
                  {t("meeting.panel.queueEmpty")}
                </p>
              )}
              <div
                className="meeting-panel__queue-list"
                data-testid="team-area-meeting-queue-list"
              >
                {snapshot.queue.pending.map((item, index) => (
                  <div
                    key={item.request_id}
                    className="meeting-panel__queue-item"
                    data-testid="team-area-meeting-queue-item"
                    data-variant={item.request_id}
                  >
                    <span
                      className="meeting-panel__queue-index"
                      aria-hidden="true"
                    >
                      {String(index + 1).padStart(2, "0")}
                    </span>
                    <div className="meeting-panel__queue-text">
                      <div className="meeting-panel__queue-meta">
                        <strong>@{contextName(item.member_name)}</strong>
                        <span>
                          {t("meeting.queueWait", {
                            seconds: item.wait_seconds,
                          })}
                        </span>
                      </div>
                      <p>{item.speech_text}</p>
                      {item.published === false && (
                        <span className="meeting-panel__warning">
                          {t("meeting.textPending")}
                        </span>
                      )}
                    </div>
                    <button
                      type="button"
                      className="meeting-panel__button meeting-panel__withdraw"
                      disabled={!active}
                      onClick={() => cancelMeetingQueued(item.request_id)}
                      data-testid="team-area-meeting-cancel-queued"
                      data-variant={item.request_id}
                    >
                      {t("meeting.cancelQueued")}
                    </button>
                  </div>
                ))}
              </div>
            </section>
          </>
        )}
      </div>
    </section>
  );
}
