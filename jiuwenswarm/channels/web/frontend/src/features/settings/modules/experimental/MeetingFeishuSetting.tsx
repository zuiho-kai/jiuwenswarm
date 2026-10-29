import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '../../../../components/ui';
import { SettingRow, SettingsConfirmDialog } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { parseConfigBoolean } from '../../services/settingsContract';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';
import { AgentConfigDialog } from '../agent/AgentSettings';

const fields = ['meeting_feishu_app_id', 'meeting_feishu_app_secret'] as const;
type ConnectionState = 'disconnected' | 'connecting' | 'connected' | 'failed';

export function MeetingFeishuSetting({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected, request } = useSettingsServices();
  const { values, savingKeys, save, patchLocal } = useSettingsSource();
  const [configuring, setConfiguring] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [clearError, setClearError] = useState('');
  const [connection, setConnection] = useState<ConnectionState>('disconnected');
  const [connecting, setConnecting] = useState(false);
  const [connectionError, setConnectionError] = useState('');
  const busy = connecting || fields.some((field) => savingKeys.has(field));
  const configured = parseConfigBoolean(values.meeting_feishu_configured);
  const title = t('meeting.feishu.title');
  const active = connection === 'connected' || connection === 'connecting';

  useEffect(() => {
    let cancelled = false;
    if (!isConnected || connecting) {
      if (!isConnected) setConnection('disconnected');
      return;
    }
    const refresh = async () => {
      try {
        const result = await request<{ state: ConnectionState }>('meeting.feishu.status');
        if (!cancelled) setConnection(result.state);
      } catch (error) {
        if (!cancelled) setConnectionError(error instanceof Error ? error.message : String(error));
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [isConnected, request, connecting]);

  const changeConnection = async () => {
    setConnecting(true);
    setConnectionError('');
    try {
      const result = await request<{ state: ConnectionState }>(
        active ? 'meeting.feishu.disconnect' : 'meeting.feishu.connect',
        {},
        { timeoutMs: 35_000 },
      );
      setConnection(result.state);
    } catch (error) {
      setConnection('failed');
      setConnectionError(error instanceof Error ? error.message : String(error));
    } finally {
      setConnecting(false);
    }
  };

  const saveCredentials = async (updates: Record<string, string>, operation: string) => {
    const result = await save(updates, operation);
    patchLocal({
      meeting_feishu_configured: Boolean(updates.meeting_feishu_app_id && updates.meeting_feishu_app_secret),
    });
    return result;
  };

  const clearCredentials = async () => {
    setClearError('');
    try {
      await saveCredentials({ meeting_feishu_app_id: '', meeting_feishu_app_secret: '' }, 'meeting-feishu-clear');
      setClearing(false);
    } catch (error) {
      setClearError(error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed'));
    }
  };

  return (
    <>
      <SettingRow
        title={<span data-testid="settings-meeting-feishu-title">{title}</span>}
        description={<span data-testid="settings-meeting-feishu-description">{t('meeting.feishu.description')}</span>}
        meta={
          <span data-testid="settings-meeting-feishu-status">
            {t(configured ? 'settingsPanel.common.configured' : 'settingsPanel.common.notConfigured')}
          </span>
        }
      >
        <Button
          disabled={disabled || !isConnected || busy || active}
          onClick={() => setConfiguring(true)}
          data-testid="settings-meeting-feishu-configure-btn"
        >
          {t(configured ? 'common.modify' : 'settingsPanel.common.configure')}
        </Button>
        <Button
          disabled={disabled || !isConnected || busy || (!configured && !active)}
          onClick={() => void changeConnection()}
          data-testid="settings-meeting-feishu-connect-btn"
          data-variant={active ? 'disconnect' : 'connect'}
        >
          {t(connecting ? 'meeting.feishu.working' : active ? 'meeting.feishu.disconnect' : 'meeting.feishu.connect')}
        </Button>
        {configured ? (
          <Button
            disabled={disabled || !isConnected || busy || active}
            onClick={() => {
              setClearError('');
              setClearing(true);
            }}
            data-testid="settings-meeting-feishu-clear-btn"
          >
            {t('settingsPanel.common.clear')}
          </Button>
        ) : null}
      </SettingRow>
      <SettingRow
        title={<span data-testid="settings-meeting-feishu-connection-title">{t('meeting.feishu.connection')}</span>}
        description={
          <span data-testid="settings-meeting-feishu-connection-status" role="status">
            {t(`meeting.feishu.states.${connecting ? 'connecting' : connection}`)}
          </span>
        }
      />
      {connectionError ? (
        <div className="settings-page__error" role="alert" data-testid="settings-meeting-feishu-connection-error">
          {connectionError}
        </div>
      ) : null}
      {configuring ? (
        <AgentConfigDialog
          titleKey="meeting.feishu.title"
          fields={fields}
          config={values}
          save={saveCredentials}
          onClose={() => setConfiguring(false)}
        />
      ) : null}
      <SettingsConfirmDialog
        open={clearing}
        title={t('settingsPanel.dialog.clearTitle', { name: title })}
        message={t('settingsPanel.dialog.clearConfirm', { name: title })}
        confirming={busy}
        error={clearError || undefined}
        confirmLabel={t('settingsPanel.common.clear')}
        confirmVariant="danger"
        onConfirm={() => void clearCredentials()}
        onCancel={() => {
          if (!busy) setClearing(false);
        }}
      />
    </>
  );
}
