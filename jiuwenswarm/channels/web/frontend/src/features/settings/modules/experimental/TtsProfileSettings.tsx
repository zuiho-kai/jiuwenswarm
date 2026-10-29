import { useCallback, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Select } from '../../../../components/ui';
import { SettingRow } from '../../components';
import { SettingItemRenderer } from '../../components/SettingItemRenderer';
import type { SettingsCustomItemProps } from '../../registry/types';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';
import { buildConfigSavePayload } from '../../services/settingsContract';
import { AgentConfigDialog, type SaveConfig } from '../agent/AgentSettings';

type VoiceCatalog = {
  default_url: string;
  default_model: string;
  default_voice: string;
  models: string[];
  voices: Array<{ id: string; name: string; models: string[] }>;
};

export function TtsProfileSettings({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected, request, saveQueue } = useSettingsServices();
  const source = useSettingsSource();
  const [editingUrl, setEditingUrl] = useState(false);
  const onUrlEditingChange = useCallback((_id: string, editing: boolean) => setEditingUrl(editing), []);
  const [configuringKey, setConfiguringKey] = useState(false);
  const [switchProvider, setSwitchProvider] = useState<string | null>(null);
  const provider = source.values.tts_provider === 'qwen_omni' ? 'qwen_omni' : 'cosyvoice';
  const catalogs = source.values.tts_voice_catalogs as Record<string, VoiceCatalog> | undefined;
  const catalog = catalogs?.[provider];
  const modelKey = 'tts_model';
  const voiceKey = 'tts_voice';
  const model = String(source.values[modelKey] ?? '');
  const voice = String(source.values[voiceKey] ?? '');
  const keyField = 'tts_api_key';
  const keyTitle = `settingsPanel.fields.${keyField}.title`;
  const keyConfigured = source.values.tts_key_configured === true;
  const voices = catalog?.voices.filter((entry) => entry.models.includes(model)) ?? [];
  const busy =
    disabled ||
    !isConnected ||
    ['tts_provider', 'tts_api_base', modelKey, voiceKey].some((key) => source.savingKeys.has(key));
  // Keep the submitted credential out of the shared frontend settings state.
  const saveTts: SaveConfig = async (updates, operation) => {
    const result = await saveQueue.enqueue(operation, () =>
      request('config.save_all', buildConfigSavePayload(updates), { timeoutMs: 600_000 }),
    );
    const { tts_api_key: key, ...settings } = updates;
    source.patchLocal({ ...settings, ...(key !== undefined ? { tts_key_configured: Boolean(key) } : {}) });
    return result;
  };

  return (
    <>
      <SettingRow
        title={<span data-testid="settings-tts-provider-title">{t('settingsPanel.fields.tts_provider.title')}</span>}
        description={
          <span data-testid="settings-tts-provider-description">
            {t('settingsPanel.fields.tts_provider.description')}
          </span>
        }
      >
        <Select
          aria-label={t('settingsPanel.fields.tts_provider.title')}
          data-testid="settings-tts-provider-select"
          value={provider}
          disabled={busy || editingUrl || !catalog}
          options={[
            { value: 'cosyvoice', label: t('settingsPanel.tts.cosyvoice') },
            { value: 'qwen_omni', label: t('settingsPanel.tts.qwen') },
          ]}
          onChange={(next) => {
            if (next !== provider && catalogs?.[next]) setSwitchProvider(next);
          }}
        />
      </SettingRow>
      <SettingItemRenderer
        key={`${provider}-url`}
        item={{ id: 'tts-api-base', component: 'input', key: 'tts_api_base' }}
        disabled={busy}
        onEditingChange={onUrlEditingChange}
      />
      <SettingRow
        title={<span data-testid="settings-tts-key-title">{t(keyTitle)}</span>}
        description={
          <span data-testid="settings-tts-key-status">
            {keyConfigured ? t('settingsPanel.common.configured') : t('settingsPanel.common.notConfigured')}
          </span>
        }
      >
        <Button
          disabled={busy || editingUrl || !catalog}
          onClick={() => setConfiguringKey(true)}
          data-testid="settings-tts-key-configure-btn"
          data-variant={provider}
        >
          {t('settingsPanel.common.configure')}
        </Button>
      </SettingRow>
      <SettingRow
        title={<span data-testid="settings-tts-model-title">{t('settingsPanel.fields.tts_model.title')}</span>}
        description={
          <span data-testid="settings-tts-model-description">{t('settingsPanel.fields.tts_model.description')}</span>
        }
      >
        <Select
          aria-label={t('settingsPanel.fields.tts_model.title')}
          data-testid="settings-tts-model-select"
          value={model}
          disabled={busy || editingUrl || !catalog}
          options={catalog?.models.map((value) => ({ value, label: value })) ?? []}
          onChange={(next) => {
            if (!catalog?.models.includes(next)) return;
            const compatible = catalog.voices.filter((entry) => entry.models.includes(next));
            const nextVoice = compatible.find((entry) => entry.id === voice)?.id ?? compatible[0]?.id;
            if (!nextVoice) return;
            void source.save({ [modelKey]: next, [voiceKey]: nextVoice }, 'tts-model').catch(() => undefined);
          }}
        />
      </SettingRow>
      {configuringKey ? (
        <AgentConfigDialog
          titleKey={keyTitle}
          fields={[keyField]}
          config={{}}
          save={saveTts}
          onClose={() => setConfiguringKey(false)}
        />
      ) : null}
      {switchProvider && catalogs?.[switchProvider] ? (
        <AgentConfigDialog
          titleKey="settingsPanel.tts.switchTitle"
          fields={['tts_api_base', 'tts_api_key']}
          config={{ tts_api_base: catalogs[switchProvider].default_url }}
          save={(updates, operation) =>
            saveTts(
              {
                ...updates,
                tts_provider: switchProvider,
                tts_model: catalogs[switchProvider].default_model,
                tts_voice: catalogs[switchProvider].default_voice,
              },
              operation,
            )
          }
          onClose={() => setSwitchProvider(null)}
        />
      ) : null}
      <SettingRow
        title={<span data-testid="settings-tts-voice-title">{t('settingsPanel.fields.tts_voice.title')}</span>}
        description={
          <span data-testid="settings-tts-voice-description">{t('settingsPanel.fields.tts_voice.description')}</span>
        }
      >
        <Select
          aria-label={t('settingsPanel.fields.tts_voice.title')}
          data-testid="settings-tts-voice-select"
          value={voice}
          disabled={busy || editingUrl || !voices.length}
          options={voices.map((entry) => ({ value: entry.id, label: entry.name }))}
          onChange={(next) => {
            if (voices.some((entry) => entry.id === next))
              void source.save({ [voiceKey]: next }, 'tts-voice').catch(() => undefined);
          }}
        />
      </SettingRow>
    </>
  );
}
