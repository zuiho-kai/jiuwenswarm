import { useMemo, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Input, Select, Switch } from '../../../../components/ui';
import { FormDialog } from '../../../../components/form';
import { useSessionStore } from '../../../../stores/sessionStore';
import { SettingRow, SettingsConfirmDialog } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';
import {
  CLEF_MODELS,
  DUPLEX_PROVIDERS,
  LLM_PRESETS,
  TYPED_PRESETS,
  buildDuplexClearUpdates,
  buildDuplexUpdates,
  createDuplexDraft,
  duplexPresetFields,
  duplexSummaryModel,
  missingDuplexField,
  readDuplexSettings,
  selectDuplexPreset,
  selectDuplexProvider,
  type DuplexDraft,
  type DuplexPreset,
  type DuplexProvider,
} from './a2aDuplexConfig';

type DraftField = Exclude<keyof DuplexDraft, 'mode' | 'provider' | 'preset'>;

export function A2ADuplexSettings({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const source = useSettingsSource();
  const availableModels = useSessionStore((state) => state.availableModels);
  const [draft, setDraft] = useState<DuplexDraft | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const [clearOpen, setClearOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const saved = readDuplexSettings(source.values);
  const model = duplexSummaryModel(saved);
  const blocked = disabled || !isConnected || busy;
  const teamModels = useMemo(() => {
    const names = availableModels.map((entry) => entry.model_name).filter(Boolean);
    if (draft?.model_name && !names.includes(draft.model_name)) names.unshift(draft.model_name);
    return [...new Set(names)];
  }, [availableModels, draft?.model_name]);

  const fail = (cause: unknown) =>
    setError(cause instanceof Error ? cause.message : t('settingsPanel.feedback.saveFailed'));

  const toggleEnabled = async (next: boolean) => {
    setBusy(true);
    setError('');
    try {
      await source.save(
        {
          duplex_router_enabled: next ? 'true' : 'false',
          ...(next && saved.mode !== 'active' ? { duplex_router_mode: 'active' } : {}),
        },
        'a2a-duplex-router-enabled',
      );
    } catch (saveError) {
      fail(saveError);
    } finally {
      setBusy(false);
    }
  };

  const openEditor = () => {
    setError('');
    setAdvanced(false);
    setDraft(createDuplexDraft(saved));
  };
  const update = (field: DraftField | 'mode', next: string) =>
    setDraft((current) => current && ({ ...current, [field]: next } as DuplexDraft));

  const saveDraft = async () => {
    if (!draft) return;
    const missing = missingDuplexField(draft);
    if (missing) {
      setError(`${fieldTitle(missing)}: ${t('settingsPanel.validation.required')}`);
      return;
    }
    setBusy(true);
    setError('');
    try {
      await source.save(buildDuplexUpdates(draft), 'a2a-duplex-router');
      setDraft(null);
    } catch (saveError) {
      fail(saveError);
    } finally {
      setBusy(false);
    }
  };

  const clear = async () => {
    setBusy(true);
    setError('');
    try {
      await source.save(buildDuplexClearUpdates(), 'a2a-duplex-router-clear');
      setClearOpen(false);
    } catch (clearError) {
      fail(clearError);
    } finally {
      setBusy(false);
    }
  };

  const fieldTitle = (field: string) =>
    t(`settingsPanel.agent.a2aFields.${field === 'api_key' && draft?.preset === 'clef' ? 'token' : field}`);
  const presetLabel = (preset: DuplexPreset) => t(`settingsPanel.agent.a2aPresets.${preset}`);
  const row = (field: string, control: ReactNode) => (
    <SettingRow
      key={field}
      title={
        <span data-testid="settings-a2a-field-title" data-variant={field}>
          {fieldTitle(field)}
        </span>
      }
    >
      {control}
    </SettingRow>
  );
  const input = (field: DraftField) =>
    row(
      field,
      <Input
        aria-label={fieldTitle(field)}
        type={field === 'api_key' ? 'password' : 'text'}
        value={draft?.[field] ?? ''}
        disabled={busy}
        placeholder={field === 'api_key' && saved.hasApiKey ? '••••••••' : undefined}
        onChange={(next) => update(field, next)}
        autoComplete="off"
        data-testid="settings-a2a-field-input"
        data-variant={field}
      />,
    );
  const select = (
    field: string,
    value: string,
    options: { value: string; label: string }[],
    onChange: (next: string) => void,
  ) =>
    row(
      field,
      <Select
        aria-label={fieldTitle(field)}
        value={value}
        options={options}
        onChange={onChange}
        disabled={busy}
        data-testid="settings-a2a-field-select"
        data-variant={field}
      />,
    );
  const presetField = (field: ReturnType<typeof duplexPresetFields>[number]) => {
    if (!draft) return null;
    if (field === 'model_name' && teamModels.length > 0) {
      return select(
        field,
        draft.model_name,
        teamModels.map((name) => ({ value: name, label: name })),
        (next) => update('model_name', next),
      );
    }
    if (field === 'model' && draft.preset === 'clef') {
      return select(
        field,
        draft.model,
        CLEF_MODELS.map((name) => ({ value: name, label: name })),
        (next) => update('model', next),
      );
    }
    return input(field);
  };

  return (
    <>
      <SettingRow
        title={
          <span data-testid="settings-a2a-model-summary">{model || t('settingsPanel.agent.a2aNotConfigured')}</span>
        }
        description={
          <span data-testid="settings-a2a-status">
            {t(`settingsPanel.agent.a2aProviders.${saved.provider}`)} · {presetLabel(saved.preset)} ·{' '}
            {t(
              saved.enabled && saved.mode === 'active'
                ? 'settingsPanel.options.duplexRouterModeActive'
                : 'settingsPanel.options.duplexRouterModeOff',
            )}
          </span>
        }
      >
        <Switch
          checked={saved.enabled}
          disabled={blocked}
          onChange={(next) => void toggleEnabled(next)}
          aria-label={t('settingsPanel.agent.a2aDuplexRouter')}
          data-testid="settings-a2a-enabled-switch"
        />
        <Button variant="quiet" size="sm" disabled={blocked} onClick={openEditor} data-testid="settings-a2a-edit-btn">
          {t('common.modify')}
        </Button>
        <Button
          variant="quiet"
          size="sm"
          disabled={blocked}
          onClick={() => {
            setError('');
            setClearOpen(true);
          }}
          data-testid="settings-a2a-clear-btn"
        >
          {t('settingsPanel.common.clear')}
        </Button>
      </SettingRow>
      {draft ? (
        <FormDialog
          open
          title={t('settingsPanel.agent.a2aDuplexRouter')}
          submitting={busy}
          confirmDisabled={disabled || !isConnected}
          confirmLabel={t('common.save')}
          cancelLabel={t('common.cancel')}
          testIdPrefix="settings-a2a-config-dialog"
          onConfirm={() => void saveDraft()}
          onCancel={() => {
            if (!busy) setDraft(null);
          }}
        >
          <SettingRow title={<span data-testid="settings-a2a-mode-title">{fieldTitle('mode')}</span>}>
            <Switch
              aria-label={fieldTitle('mode')}
              checked={draft.mode === 'active'}
              onChange={(checked) => update('mode', checked ? 'active' : 'off')}
              disabled={busy}
              data-testid="settings-a2a-mode-switch"
            />
          </SettingRow>
          {select(
            'provider',
            draft.provider,
            DUPLEX_PROVIDERS.map((provider) => ({
              value: provider,
              label: t(`settingsPanel.agent.a2aProviders.${provider}`),
            })),
            (next) => setDraft((current) => current && selectDuplexProvider(current, next as DuplexProvider, saved)),
          )}
          {select(
            'preset',
            draft.preset,
            (draft.provider === 'typed_choice' ? TYPED_PRESETS : LLM_PRESETS).map((preset) => ({
              value: preset,
              label: presetLabel(preset),
            })),
            (next) => setDraft((current) => current && selectDuplexPreset(current, next as DuplexPreset, saved)),
          )}
          {duplexPresetFields(draft.preset).map(presetField)}
          <p data-testid="settings-a2a-defaults-hint">
            {t(`settingsPanel.agent.a2aHints.${draft.preset}`)} {t('settingsPanel.agent.a2aRestartHint')}
          </p>
          <Button
            variant="quiet"
            aria-expanded={advanced}
            onClick={() => setAdvanced(!advanced)}
            data-testid="settings-a2a-advanced-toggle"
          >
            {t('settingsPanel.agent.a2aAdvanced')}
          </Button>
          {advanced ? <div data-testid="settings-a2a-advanced-fields">{input('timeout_seconds')}</div> : null}
          {error ? (
            <div className="settings-page__error" role="alert" data-testid="settings-a2a-error">
              {error}
            </div>
          ) : null}
        </FormDialog>
      ) : null}
      <SettingsConfirmDialog
        open={clearOpen}
        title={t('settingsPanel.agent.a2aClearTitle')}
        message={t('settingsPanel.agent.a2aClearConfirm')}
        confirming={busy}
        error={error}
        confirmLabel={t('settingsPanel.common.clear')}
        confirmVariant="danger"
        onConfirm={() => void clear()}
        onCancel={() => {
          if (!busy) setClearOpen(false);
        }}
      />
    </>
  );
}
