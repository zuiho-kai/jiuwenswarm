import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { settingsActionIcons } from '../../../../assets/settings';
import { Button, Select } from '../../../../components/ui';
import { FormDialog } from '../../../../components/form';
import { SettingsConfirmDialog } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';
import './A2ADuplexSettings.css';

type Draft = { mode: string; backend: string; model: string; apiBase: string; apiKey: string };
const BACKENDS = ['sdk', 'jev', 'mindshub', 'clef'] as const;
const MODES = ['off', 'shadow', 'active'] as const;

function secretPlaceholder(value: unknown): string {
  const length = Number(value);
  return Number.isSafeInteger(length) && length > 0 ? '*'.repeat(length) : '';
}

export function A2ADuplexSettings({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const source = useSettingsSource();
  const [draft, setDraft] = useState<Draft | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const value = (key: string) => String(source.values[key] ?? '');
  const configured = Boolean(value('duplex_router_model_name') || value('duplex_router_model'));
  const model = value('duplex_router_model_name') || value('duplex_router_model') || t('settingsPanel.agent.a2aNotConfigured');
  const backend = value('duplex_router_backend') || 'sdk';

  useEffect(() => {
    if (!source.values.duplex_router_api_key) return;
  }, [source.values.duplex_router_api_key]);

  const openEditor = () => {
    setError('');
    setDraft({
      mode: value('duplex_router_mode') || 'active',
      backend,
      model: value('duplex_router_model_name') || value('duplex_router_model'),
      apiBase: value('duplex_router_api_base'),
      apiKey: '',
    });
  };

  const saveDraft = async () => {
    if (!draft || !draft.model.trim()) {
      setError(t('settingsPanel.validation.required'));
      return;
    }
    setBusy(true);
    setError('');
    try {
      const updates: Record<string, string> = {
        duplex_router_mode: draft.mode,
        duplex_router_backend: draft.backend,
        duplex_router_model_name: draft.model.trim(),
      };
      if (draft.apiBase.trim()) updates.duplex_router_api_base = draft.apiBase.trim();
      if (draft.apiKey.trim()) updates.duplex_router_api_key = draft.apiKey.trim();
      await source.save(updates, 'a2a-duplex-router');
      setDraft(null);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setBusy(false);
    }
  };

  const clear = async () => {
    setBusy(true);
    setError('');
    try {
      await source.save({
        duplex_router_mode: 'off',
        duplex_router_model_name: '',
        duplex_router_api_base: '',
        duplex_router_api_key: '',
      }, 'a2a-duplex-router-clear');
      setDeleteOpen(false);
    } catch (clearError) {
      setError(clearError instanceof Error ? clearError.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      {error ? <div className="a2a-duplex-settings__error" role="alert">{error}</div> : null}
      <div className="settings-agent-media__model-card a2a-duplex-settings__card">
        <div className="a2a-duplex-settings__summary">
          <strong className="settings-agent-media__model-name">{model}</strong>
          <small>{configured ? `${backend} · ${value('duplex_router_mode') || 'active'}` : t('settingsPanel.agent.a2aNotConfigured')}</small>
        </div>
        <div className="settings-agent-media__actions">
          <Button
            variant="quiet"
            size="sm"
            icon={<settingsActionIcons.edit aria-hidden />}
            title={t('common.modify')}
            aria-label={t('common.modify')}
            disabled={disabled || !isConnected || busy}
            onClick={openEditor}
            data-testid="settings-a2a-edit-btn"
          />
          {configured ? (
            <Button
              variant="quiet"
              size="sm"
              icon={<settingsActionIcons.delete aria-hidden />}
              title={t('common.delete')}
              aria-label={t('common.delete')}
              disabled={disabled || !isConnected || busy}
              onClick={() => setDeleteOpen(true)}
              data-testid="settings-a2a-delete-btn"
            />
          ) : null}
        </div>
      </div>
      {draft ? (
        <FormDialog
          open
          title={t('settingsPanel.agent.a2aDuplexRouter')}
          submitting={busy}
          confirmLabel={t('common.save')}
          cancelLabel={t('common.cancel')}
          dialogClassName="settings-model-dialog"
          testIdPrefix="settings-a2a-config-dialog"
          onConfirm={() => void saveDraft()}
          onCancel={() => { if (!busy) setDraft(null); }}
        >
          <div className="a2a-duplex-settings__fields">
            <label><span>{t('settingsPanel.fields.duplex_router_mode.title')}</span><Select value={draft.mode} options={MODES.map((item) => ({ value: item, label: t(`settingsPanel.options.duplexRouterMode${item === 'off' ? 'Off' : item === 'shadow' ? 'Shadow' : 'Active'}`) }))} onChange={(next) => setDraft({ ...draft, mode: next })} /></label>
            <label><span>{t('settingsPanel.fields.duplex_router_backend.title')}</span><Select value={draft.backend} options={BACKENDS.map((item) => ({ value: item, label: item }))} onChange={(next) => setDraft({ ...draft, backend: next })} /></label>
            <label><span>{t('settingsPanel.fields.duplex_router_model_name.title')}</span><input value={draft.model} onChange={(event) => setDraft({ ...draft, model: event.target.value })} autoComplete="off" /></label>
            <label><span>{t('settingsPanel.fields.duplex_router_api_base.title')}</span><input value={draft.apiBase} onChange={(event) => setDraft({ ...draft, apiBase: event.target.value })} placeholder="https://api.example.com/v1" autoComplete="off" /></label>
            <label><span>{t('settingsPanel.fields.duplex_router_api_key.title')}</span><input type="password" value={draft.apiKey} placeholder={secretPlaceholder(source.values.duplex_router_api_key)} onChange={(event) => setDraft({ ...draft, apiKey: event.target.value })} autoComplete="off" /></label>
          </div>
          <p className="a2a-duplex-settings__hint">{t('settingsPanel.agent.a2aDefaultsHint')}</p>
        </FormDialog>
      ) : null}
      <SettingsConfirmDialog
        open={deleteOpen}
        title={t('settingsPanel.agent.a2aDeleteTitle')}
        message={t('settingsPanel.agent.a2aDeleteConfirm')}
        confirming={busy}
        error={error}
        onConfirm={() => void clear()}
        onCancel={() => { if (!busy) setDeleteOpen(false); }}
      />
    </>
  );
}
