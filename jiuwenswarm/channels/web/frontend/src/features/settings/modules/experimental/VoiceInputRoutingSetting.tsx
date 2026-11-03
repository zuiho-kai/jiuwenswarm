import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Select } from '../../../../components/ui';
import { getVoiceInputRoutingMode, setVoiceInputRoutingMode } from '../../../voice/routingMode';
import { SettingRow } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';

export function VoiceInputRoutingSetting({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const [mode, setMode] = useState(getVoiceInputRoutingMode);
  return (
    <SettingRow
      title={<span data-testid="settings-voice-input-routing-title">{t('voiceStreaming.routing.title')}</span>}
      description={
        <span data-testid="settings-voice-input-routing-description">{t('voiceStreaming.routing.description')}</span>
      }
    >
      <Select
        aria-label={t('voiceStreaming.routing.title')}
        data-testid="settings-voice-input-routing-select"
        disabled={disabled}
        value={mode}
        options={[
          { value: 'leader', label: t('voiceStreaming.routing.leader') },
          { value: 'none', label: t('voiceStreaming.routing.none') },
        ]}
        onChange={(next) => {
          if (next !== 'leader' && next !== 'none') return;
          setVoiceInputRoutingMode(next);
          setMode(next);
        }}
      />
    </SettingRow>
  );
}
