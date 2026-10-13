/**
 * A2A duplex supervisor settings: flat config keys <-> editor draft.
 *
 * Two provider kinds:
 * - typed_choice: the server returns the decision probability (presets jev / clef).
 * - logprob_llm: a general LLM; the probability comes from logprobs
 *   (presets team_model / openai / custom).
 */

export type DuplexProvider = 'typed_choice' | 'logprob_llm';
export type TypedPreset = 'jev' | 'clef';
export type LlmPreset = 'team_model' | 'openai' | 'custom';
export type DuplexPreset = TypedPreset | LlmPreset;

export const DUPLEX_PROVIDERS: readonly DuplexProvider[] = ['typed_choice', 'logprob_llm'];
export const TYPED_PRESETS: readonly TypedPreset[] = ['jev', 'clef'];
export const LLM_PRESETS: readonly LlmPreset[] = ['team_model', 'openai', 'custom'];
export const CLEF_MODELS = ['clef', 'clef-flash'] as const;

export const JEV_DEFAULT_ENDPOINT = 'https://api.typesafe.ai/v1/systemone';
export const MINDSHUB_DECISIONS_ENDPOINT = 'https://api.mindshub.ai/v1/decisions';
export const TYPED_DEFAULT_MODELS: Record<TypedPreset, string> = { jev: 'jev-1.13.0', clef: 'clef' };
export const LLM_DEFAULT_API_BASES: Record<LlmPreset, string> = {
  team_model: '',
  openai: 'https://api.openai.com/v1',
  custom: '',
};
export const LLM_DEFAULT_MODELS: Record<LlmPreset, string> = {
  team_model: '',
  openai: 'gpt-4o-mini',
  custom: '',
};

export type DuplexDraft = {
  mode: 'off' | 'active';
  provider: DuplexProvider;
  preset: DuplexPreset;
  /** team_model: name from the team model pool. */
  model_name: string;
  /** openai / custom. */
  api_base: string;
  /** jev: full decision URL. */
  endpoint: string;
  /** jev / clef / openai / custom. */
  model: string;
  api_key: string;
  account_id: string;
  timeout_seconds: string;
};

/** Normalized view of the saved settings. */
export type DuplexSettingsView = Omit<DuplexDraft, 'api_key'> & {
  enabled: boolean;
  hasApiKey: boolean;
};

type Values = Record<string, unknown>;

const text = (values: Values, key: string) => String(values[key] ?? '').trim();

const isTyped = (value: string): value is TypedPreset => (TYPED_PRESETS as readonly string[]).includes(value);
const isLlm = (value: string): value is LlmPreset => (LLM_PRESETS as readonly string[]).includes(value);

export const isTypedPreset = (preset: DuplexPreset): preset is TypedPreset => isTyped(preset);

/** Map a pre-provider config (backend: sdk / jev / mindshub / clef) to the new shape. */
function readLegacy(
  values: Values,
): Pick<DuplexDraft, 'provider' | 'preset' | 'model_name' | 'api_base' | 'endpoint' | 'model'> {
  const backend = text(values, 'duplex_router_backend') || 'sdk';
  const modelName = text(values, 'duplex_router_model_name');
  const apiBase = text(values, 'duplex_router_api_base');
  if (backend === 'jev') {
    const base = (apiBase || 'https://api.typesafe.ai/v1').replace(/\/+$/, '');
    const path = text(values, 'duplex_router_endpoint_path') || 'systemone';
    return {
      provider: 'typed_choice',
      preset: 'jev',
      model_name: '',
      api_base: '',
      endpoint: `${base}/${path}`,
      model: modelName || TYPED_DEFAULT_MODELS.jev,
    };
  }
  if (backend === 'clef') {
    return {
      provider: 'typed_choice',
      preset: 'clef',
      model_name: '',
      api_base: '',
      endpoint: '',
      model: text(values, 'duplex_router_model') || TYPED_DEFAULT_MODELS.clef,
    };
  }
  if (backend === 'mindshub') {
    // MindsHub chat ignores logprobs; its decisions endpoint is JEV-compatible.
    return {
      provider: 'typed_choice',
      preset: 'jev',
      model_name: '',
      api_base: '',
      endpoint: MINDSHUB_DECISIONS_ENDPOINT,
      model: TYPED_DEFAULT_MODELS.jev,
    };
  }
  return {
    provider: 'logprob_llm',
    preset: 'team_model',
    model_name: modelName,
    api_base: '',
    endpoint: '',
    model: '',
  };
}

export function readDuplexSettings(values: Values): DuplexSettingsView {
  const mode = text(values, 'duplex_router_mode') === 'active' ? 'active' : 'off';
  const rawEnabled = values.duplex_router_enabled;
  const enabled =
    rawEnabled === undefined || rawEnabled === ''
      ? mode === 'active'
      : rawEnabled === true || String(rawEnabled).trim().toLowerCase() === 'true';
  const common = {
    mode,
    enabled,
    account_id: text(values, 'duplex_router_account_id'),
    timeout_seconds: text(values, 'duplex_router_timeout_seconds') || '2',
    hasApiKey: Boolean(text(values, 'duplex_router_api_key')),
  } as const;
  const provider = text(values, 'duplex_router_provider');
  if (provider !== 'typed_choice' && provider !== 'logprob_llm') return { ...common, ...readLegacy(values) };
  const typedPreset = text(values, 'duplex_router_typed_preset');
  const llmPreset = text(values, 'duplex_router_llm_preset');
  if (provider === 'typed_choice') {
    const preset: TypedPreset = isTyped(typedPreset) ? typedPreset : 'jev';
    return {
      ...common,
      provider,
      preset,
      model_name: '',
      api_base: '',
      endpoint: preset === 'jev' ? text(values, 'duplex_router_typed_endpoint') || JEV_DEFAULT_ENDPOINT : '',
      model: text(values, 'duplex_router_typed_model') || TYPED_DEFAULT_MODELS[preset],
    };
  }
  const preset: LlmPreset = isLlm(llmPreset) ? llmPreset : 'team_model';
  return {
    ...common,
    provider,
    preset,
    model_name: preset === 'team_model' ? text(values, 'duplex_router_llm_model_name') : '',
    api_base:
      preset === 'team_model' ? '' : text(values, 'duplex_router_llm_api_base') || LLM_DEFAULT_API_BASES[preset],
    endpoint: '',
    model: preset === 'team_model' ? '' : text(values, 'duplex_router_llm_model') || LLM_DEFAULT_MODELS[preset],
  };
}

/** Model shown in the summary row. */
export function duplexSummaryModel(view: Pick<DuplexDraft, 'preset' | 'model_name' | 'model'>): string {
  return view.preset === 'team_model' ? view.model_name : view.model;
}

export function createDuplexDraft(view: DuplexSettingsView): DuplexDraft {
  return {
    mode: view.mode,
    provider: view.provider,
    preset: view.preset,
    model_name: view.model_name,
    api_base: view.api_base,
    endpoint: view.endpoint,
    model: view.model,
    api_key: '',
    account_id: view.account_id,
    timeout_seconds: view.timeout_seconds,
  };
}

/** Switch preset; keep saved values when returning to the saved preset, else use preset defaults. */
export function selectDuplexPreset(draft: DuplexDraft, preset: DuplexPreset, saved: DuplexSettingsView): DuplexDraft {
  const provider: DuplexProvider = isTypedPreset(preset) ? 'typed_choice' : 'logprob_llm';
  if (preset === saved.preset) {
    return {
      ...draft,
      provider,
      preset,
      model_name: saved.model_name,
      api_base: saved.api_base,
      endpoint: saved.endpoint,
      model: saved.model,
    };
  }
  if (isTypedPreset(preset)) {
    return {
      ...draft,
      provider,
      preset,
      model_name: '',
      api_base: '',
      endpoint: preset === 'jev' ? JEV_DEFAULT_ENDPOINT : '',
      model: TYPED_DEFAULT_MODELS[preset],
    };
  }
  return {
    ...draft,
    provider,
    preset,
    model_name: preset === 'team_model' ? saved.model_name : '',
    api_base: LLM_DEFAULT_API_BASES[preset],
    endpoint: '',
    model: LLM_DEFAULT_MODELS[preset],
  };
}

/** Switch kind; picks the saved preset of that kind, else its first preset. */
export function selectDuplexProvider(
  draft: DuplexDraft,
  provider: DuplexProvider,
  saved: DuplexSettingsView,
): DuplexDraft {
  if (provider === draft.provider) return draft;
  const preset = saved.provider === provider ? saved.preset : provider === 'typed_choice' ? 'jev' : 'team_model';
  return selectDuplexPreset(draft, preset, saved);
}

/** Field ids shown for a preset, in display order. */
export function duplexPresetFields(
  preset: DuplexPreset,
): Array<'model_name' | 'endpoint' | 'api_base' | 'model' | 'api_key' | 'account_id'> {
  if (preset === 'team_model') return ['model_name'];
  if (preset === 'jev') return ['endpoint', 'model', 'api_key'];
  if (preset === 'clef') return ['account_id', 'api_key', 'model'];
  return ['api_base', 'model', 'api_key'];
}

/** Returns the first missing required field when the supervisor is active, or null. */
export function missingDuplexField(draft: DuplexDraft): string | null {
  if (draft.mode !== 'active') return null;
  for (const field of duplexPresetFields(draft.preset)) {
    if (field === 'api_key') continue; // may already be stored
    if (!String(draft[field]).trim()) return field;
  }
  return null;
}

/** Flat config.set updates for a draft. */
export function buildDuplexUpdates(draft: DuplexDraft): Record<string, string> {
  const updates: Record<string, string> = {
    duplex_router_mode: draft.mode,
    duplex_router_enabled: draft.mode === 'active' ? 'true' : 'false',
    duplex_router_provider: draft.provider,
    duplex_router_timeout_seconds: draft.timeout_seconds.trim(),
  };
  if (isTypedPreset(draft.preset)) {
    updates.duplex_router_typed_preset = draft.preset;
    updates.duplex_router_typed_model = draft.model.trim();
    if (draft.preset === 'jev') updates.duplex_router_typed_endpoint = draft.endpoint.trim();
    else updates.duplex_router_account_id = draft.account_id.trim();
  } else {
    updates.duplex_router_llm_preset = draft.preset;
    if (draft.preset === 'team_model') {
      updates.duplex_router_llm_model_name = draft.model_name.trim();
    } else {
      updates.duplex_router_llm_api_base = draft.api_base.trim();
      updates.duplex_router_llm_model = draft.model.trim();
    }
  }
  if (draft.preset !== 'team_model' && draft.api_key.trim()) updates.duplex_router_api_key = draft.api_key.trim();
  return updates;
}

/** Disable the supervisor and clear the shared secrets and team model choice. */
export function buildDuplexClearUpdates(): Record<string, string> {
  return {
    duplex_router_mode: 'off',
    duplex_router_enabled: 'false',
    duplex_router_llm_model_name: '',
    duplex_router_api_key: '',
    duplex_router_account_id: '',
  };
}
