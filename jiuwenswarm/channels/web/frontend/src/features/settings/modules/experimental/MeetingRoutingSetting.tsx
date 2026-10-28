import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Select } from '../../../../components/ui';
import { getMeetingRoutingMode, setMeetingRoutingMode } from '../../../teamMeeting/routingMode';
import { SettingRow } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';

export function MeetingRoutingSetting({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const [mode, setMode] = useState(getMeetingRoutingMode);
  return (
    <SettingRow
      title={<span data-testid="settings-meeting-routing-title">{t('meeting.routing.title')}</span>}
      description={<span data-testid="settings-meeting-routing-description">{t('meeting.routing.description')}</span>}
    >
      <Select
        aria-label={t('meeting.routing.title')}
        data-testid="settings-meeting-routing-select"
        disabled={disabled}
        value={mode}
        options={[
          { value: 'leader', label: t('meeting.routing.leader') },
          { value: 'none', label: t('meeting.routing.none') },
        ]}
        onChange={(next) => {
          if (next !== 'leader' && next !== 'none') return;
          setMeetingRoutingMode(next);
          setMode(next);
        }}
      />
    </SettingRow>
  );
}
