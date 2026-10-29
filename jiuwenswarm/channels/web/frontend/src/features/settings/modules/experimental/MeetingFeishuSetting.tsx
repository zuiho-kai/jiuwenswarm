import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '../../../../components/ui';
import { SettingRow, SettingsConfirmDialog } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { parseConfigBoolean } from '../../services/settingsContract';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';
import { AgentConfigDialog } from '../agent/AgentSettings';

const fields = ['meeting_feishu_app_id', 'meeting_feishu_app_secret'] as const;

export function MeetingFeishuSetting({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const { values, savingKeys, save, patchLocal } = useSettingsSource();
  const [configuring, setConfiguring] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [clearError, setClearError] = useState('');
  const busy = fields.some((field) => savingKeys.has(field));
  const configured = parseConfigBoolean(values.meeting_feishu_configured);
  const title = t('meeting.feishu.title');

  const saveCredentials = async (updates: Record<string, string>, operation: string) => {
    const result = await save(updates, operation);
    // Config reads return only status, never the saved App Secret.
    patchLocal({
      meeting_feishu_app_secret: '',
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
          disabled={disabled || !isConnected || busy}
          onClick={() => setConfiguring(true)}
          data-testid="settings-meeting-feishu-configure-btn"
        >
          {t(configured ? 'common.modify' : 'settingsPanel.common.configure')}
        </Button>
        {configured ? (
          <Button
            disabled={disabled || !isConnected || busy}
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
